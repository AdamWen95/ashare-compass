"""CNINFO public disclosure UI adapter, bounded to verified preselected companies.

The stock mapping resolves the UI's opaque orgId, never the security universe.
Bodies stay local. Extractable text is not automatic semantic risk clearance.
"""
from datetime import datetime
import hashlib
from html import unescape
import json
import re

from ..operations.daily import atomic_json
from .contracts import SHANGHAI, aware
from .pdf_text import extract_pdf

SOURCE = "cninfo_company_announcements"
MAPPING = "https://www.cninfo.com.cn/new/data/szse_stock.json"
DIRECTORY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"


def _plain(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("announcement_text_missing")
    return unescape(re.sub(r"<[^>]*>", "", value)).strip()


def resolve_company(mapping, candidate):
    symbol = candidate["symbol"]
    if not re.fullmatch(r"(?:sh|sz)\.\d{6}", symbol):
        raise ValueError("company_identity_invalid")
    rows = mapping.get("stockList") if isinstance(mapping, dict) else None
    if not isinstance(rows, list) or len(rows) > 20000 or not all(isinstance(r, dict) for r in rows):
        raise ValueError("company_mapping_invalid")
    found = [r for r in rows if r.get("code") == symbol[3:]]
    if (len(found) != 1 or found[0].get("zwjc") != candidate["name"] or found[0].get("category") != "A股"
            or not re.fullmatch(r"[A-Za-z0-9]{1,40}", found[0].get("orgId", ""))):
        raise ValueError("company_identity_mismatch")
    return found[0]


def parse_directory(raw, company):
    if not isinstance(raw, dict):
        raise ValueError("announcement_directory_schema")
    count, more, records = raw.get("totalRecordNum"), raw.get("hasMore"), raw.get("announcements")
    if count == 0 and more is False and "announcements" in raw and records is None:
        records = []
    if (type(count) is not int or count < 0 or type(more) is not bool
            or not isinstance(records, list) or len(records) > 30 or count < len(records)
            or not all(isinstance(r, dict) for r in records)):
        raise ValueError("announcement_directory_schema")
    result, seen = [], set()
    for row in records:
        if (row.get("secCode") != company["code"] or _plain(row.get("secName")) != company["zwjc"]
                or row.get("orgId") != company["orgId"]):
            raise ValueError("announcement_identity_mismatch")
        identity = row.get("announcementId")
        if not isinstance(identity, str) or not identity.isdigit() or len(identity) > 30 or identity in seen:
            raise ValueError("announcement_id_invalid_or_duplicate")
        seen.add(identity)
        path = row.get("adjunctUrl", "")
        if (not re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/" + identity + r"\.(?:pdf|PDF)", path)
                or str(row.get("adjunctType", "")).upper() != "PDF"):
            raise ValueError("announcement_url_invalid")
        stamp = row.get("announcementTime")
        if type(stamp) is not int or not 946656000000 <= stamp <= 4102416000000:
            raise ValueError("announcement_time_invalid")
        published = datetime.fromtimestamp(stamp / 1000, SHANGHAI)
        # Provider midnight timestamps cannot establish a precise publication
        # second. Retain this ambiguity instead of inventing morning availability.
        precision = "date" if published.hour == published.minute == published.second == 0 else "datetime"
        result.append({"announcement_id": identity, "title": _plain(row.get("announcementTitle")),
            "original_url": "https://static.cninfo.com.cn/" + path,
            "published_at": published.date().isoformat() if precision == "date" else published.isoformat(),
            "publication_precision": precision})
    return {"records": result, "complete": count == len(records) and more is False, "total": count}


def review_text(text, company):
    compact = re.sub(r"\s+", "", text)
    if (not re.search(r"证券代码[:：]?" + company["code"] + r"(?!\d)", compact)
            or not re.search(r"证券简称[:：]?" + re.escape(company["zwjc"]), compact)):
        raise ValueError("pdf_company_identity_mismatch")
    flags = []
    categories = ((r"不确定性|可能.*损失|违约", "合同履约、经营不确定性或损失相关表述，需核对影响与条件"),
        (r"立案|诉讼|处罚", "调查、诉讼或处罚相关表述，需核对主体与进展"),
        (r"减持|质押", "股东减持或质押相关表述，需核对计划与实际执行"),
        (r"终止|退市|亏损", "终止事项、退市或亏损相关表述，需核对是否适用于公司"))
    for sentence in re.findall(r"[^。！？]+[。！？]?", text):
        sentence = re.sub(r"\s+", "", sentence).strip()
        if re.search(r"不存在虚假记载|无虚假记载|真实[、，]准确[、，]完整|注意投资风险", sentence):
            continue
        for pattern, label in categories:
            if re.search(pattern, sentence) and label not in flags:
                flags.append(label)
    return {"risk_status": "review_required", "risk_flags": flags[:10],
        "notice": "公告正文已取得；关键词仅供定位，尚未完成语义风险核查，不能按无风险处理"}


def collect_company_reviews(session, candidates, *, start, cutoff, directory):
    begin, end = aware(start), aware(cutoff)
    if begin >= end or len(candidates) > 5 or len({r["security_id"] for r in candidates}) != len(candidates):
        raise ValueError("announcement_scope_invalid")
    reviews = {r["security_id"]: {"status": "partial", "risk_status": "unknown", "evidence_ids": [], "gaps": [],
        "announcements": [], "risk_flags": [], "query_start_at": start, "query_cutoff_at": cutoff,
        "notice": "仅核查本期公告窗口；不代表公司不存在历史风险", "model_use_allowed": False} for r in candidates}
    if not candidates:
        return {"reviews": reviews, "status": "not_applicable", "source_id": SOURCE}
    try:
        body, mapping_receipt = session.get(MAPPING, SOURCE, "mapping")
        mapping = json.loads(body)
        if aware(mapping_receipt["fetched_at"]) > end:
            raise ValueError("mapping_first_seen_after_cutoff")
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        for review in reviews.values(): review["gaps"].append("公告身份映射未取得：" + type(exc).__name__)
        atomic_json(directory / "company_reviews.json", reviews)
        return {"reviews": reviews, "status": "partial", "source_id": SOURCE}
    for candidate in candidates:
        review = reviews[candidate["security_id"]]
        try:
            company = resolve_company(mapping, candidate)
            form = {"pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext", "plate": "",
                "stock": company["code"] + "," + company["orgId"], "searchkey": "", "secid": "", "category": "",
                "trade": "", "seDate": begin.date().isoformat() + "~" + end.date().isoformat(),
                "sortName": "", "sortType": "", "isHLtitle": "true"}
            body, receipt = session.get(DIRECTORY, SOURCE, "directory", data=form)
            parsed = parse_directory(json.loads(body), company)
            review["directory_evidence"] = {**receipt, "total": parsed["total"], "mapping_sha256": mapping_receipt["raw_sha256"]}
            review["evidence_ids"].append("cninfo-directory-" + hashlib.sha256((candidate["security_id"] + receipt["raw_sha256"] + start + cutoff).encode()).hexdigest())
            if aware(receipt["fetched_at"]) > end:
                raise ValueError("directory_first_seen_after_cutoff")
            if not parsed["complete"]:
                review["gaps"].append("公告目录超过单页上限或计数不一致，窗口未完整覆盖")
            selected = []
            for item in parsed["records"]:
                published = aware(item["published_at"] + "T00:00:00+08:00" if item["publication_precision"] == "date" else item["published_at"])
                if item["publication_precision"] == "date":
                    if begin.date() <= published.date() <= end.date():
                        review["gaps"].append("公告只有日期精度，无法确定是否属于本期窗口")
                    continue
                if begin <= published <= end:
                    selected.append(item)
                elif published > end:
                    review["gaps"].append("公告目录包含截点后的资料，未纳入本期结论")
            for item in selected:
                try:
                    _, pdf_receipt = session.get(item["original_url"], SOURCE, "pdf")
                    if aware(pdf_receipt["fetched_at"]) > end:
                        raise ValueError("pdf_first_seen_after_cutoff")
                    parsed_pdf = extract_pdf(pdf_receipt["raw_locator"], max_seconds=min(10, session.remaining()))
                    body_review = review_text(parsed_pdf["text"], company)
                    observation = session.observe(SOURCE, item["original_url"], pdf_receipt["raw_sha256"], pdf_receipt["fetched_at"])
                    evidence_id = "cninfo-pdf-" + pdf_receipt["raw_sha256"]
                    text_path = directory / (evidence_id + ".txt")
                    text_path.write_text(parsed_pdf["text"], encoding="utf-8")
                    review["announcements"].append({**item, "evidence_id": evidence_id, "source_id": SOURCE,
                        **observation, "fetched_at": pdf_receipt["fetched_at"], "content_hash": pdf_receipt["raw_sha256"],
                        "raw_locator": pdf_receipt["raw_locator"], "text_locator": str(text_path.resolve()),
                        "pages": parsed_pdf["pages"], "body_status": "extracted", **body_review})
                    review["evidence_ids"].append(evidence_id)
                    review["risk_flags"].extend(body_review["risk_flags"])
                    review["risk_status"] = "review_required"
                except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                    review["announcements"].append({**item, "body_status": "unavailable"})
                    review["gaps"].append("公告正文未完成核查：" + type(exc).__name__)
            if not review["gaps"]:
                review.update(status="ready", covered_until_at=receipt["requested_at"],
                    risk_status="review_required" if selected else "clear")
                if selected:
                    review["gaps"].append("正文语义尚待核验；不能仅凭标题或关键词形成正向推荐")
            review["window_status"] = "complete" if parsed["complete"] else "partial"
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            review["gaps"].append("公司公告核查未完成：" + type(exc).__name__)
        atomic_json(directory / "company_reviews.json", reviews)
    return {"reviews": reviews, "status": "ready" if all(r["status"] == "ready" for r in reviews.values()) else "partial",
        "source_id": SOURCE}
