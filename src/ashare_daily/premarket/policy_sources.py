"""Reviewed PBC/CSRC visible publication windows; texts never sent to a model.

Directory dates guide discovery, not effective dates. In-window policy changes
remain pending semantic review and prevent automatic positive market clearance.
"""
from datetime import date, datetime, time
from html import unescape
import hashlib
import json
import re
from urllib.parse import urljoin

from ..operations.daily import atomic_json
from ..research.sources import _PageParser
from .contracts import SHANGHAI, aware
from .official_alternatives import MOF_INDEX, SZSE_INDEX, parse_mof_index, parse_szse_index, parse_alternative_article

PBC_INDEX = "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
CSRC_INDEX = "https://www.csrc.gov.cn/searchList/a1a078ee0bc54721ab6b148884c784a8?_isAgg=true&_isJson=true&_pageSize=18&_template=index&_rangeTimeGte=&_channelName=&page=1"


def _sorted(rows):
    if not rows or len(rows) > 100 or len({r["url"] for r in rows}) != len(rows):
        raise ValueError("policy_directory_invalid")
    days = [date.fromisoformat(r["date"]) for r in rows]
    if days != sorted(days, reverse=True):
        raise ValueError("policy_directory_order_invalid")
    return rows


def parse_pbc_index(body):
    text, rows = body.decode("utf-8-sig", errors="strict"), []
    cells = re.findall(r"<td\b[^>]*>((?:(?!</td>).)*newslist_style(?:(?!</td>).)*)</td>", text, re.S | re.I)
    for cell in cells:
        match = re.search(r'<a\b[^>]*href="([^"<>]+)"[^>]*>(.*?)</a>', cell, re.S | re.I)
        stamp = re.search(r'<span\b[^>]*class="hui12"[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</span>', cell, re.S)
        if not match or not stamp:
            raise ValueError("pbc_directory_row_invalid")
        url = urljoin(PBC_INDEX, unescape(match[1]))
        if not re.fullmatch(r"https://www\.pbc\.gov\.cn/goutongjiaoliu/113456/113469/\d+/index\.html", url):
            raise ValueError("pbc_article_url_invalid")
        rows.append({"url": url, "date": stamp[1], "title": unescape(re.sub(r"<[^>]*>", "", match[2])).strip()})
    return _sorted(rows)


def parse_csrc_index(body):
    value = json.loads(body)
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, dict):
        raise ValueError("csrc_directory_invalid")
    rows = data.get("results")
    if (data.get("page") != 1 or type(data.get("total")) is not int or data["total"] < 0
            or not isinstance(rows, list) or len(rows) > 18 or data["total"] < len(rows)
            or not all(isinstance(r, dict) and all(isinstance(r.get(k), str) for k in ("url", "title", "publishedTimeStr")) for r in rows)):
        raise ValueError("csrc_directory_invalid")
    result = []
    for row in rows:
        url = urljoin("https://www.csrc.gov.cn/", row["url"])
        if not re.fullmatch(r"https://www\.csrc\.gov\.cn/csrc/c\d+/c\d+/content\.shtml", url):
            raise ValueError("csrc_article_url_invalid")
        stamp = datetime.strptime(row["publishedTimeStr"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        result.append({"url": url, "date": stamp.date().isoformat(), "title": row["title"],
            "directory_published_at": stamp.isoformat(), "directory_exhausted": data["total"] == len(rows)})
    return _sorted(result)


def parse_policy_article(body, provider, item):
    if provider in {"mof", "szse"}:
        return parse_alternative_article(body, provider, item)
    parser = _PageParser(body_class="zoom1" if provider == "pbc" else "detail-news",
        info_class="hui12" if provider == "pbc" else "info", title_class="__unused__")
    parser.feed(body.decode("utf-8-sig", errors="strict"))
    text, info = parser.text("body"), parser.text("info")
    title = parser.meta.get("articletitle", "").strip()
    if not title or title != item["title"] or len(text) < 20 or parser.body_containers != 1 or parser.closed_body_containers != 1:
        raise ValueError("policy_body_identity_or_container_invalid")
    stamps = re.findall(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?", info)
    if not stamps:
        raise ValueError("policy_visible_time_missing")
    visible = stamps[0]
    if visible[:10] != item["date"]:
        raise ValueError("policy_directory_article_date_mismatch")
    if len(visible) == 10:
        published, precision = visible, "date"
    else:
        published = datetime.strptime(visible, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI).isoformat()
        precision = "datetime"
    metadata = parser.meta.get("pubdate")
    directory_stamp = item.get("directory_published_at")
    conflict = bool(metadata and directory_stamp and len(metadata) > 10 and metadata.replace(" ", "T") != directory_stamp[:19])
    return {"title": title, "content": text, "content_type": "abstract" if parser.attachment_urls else "fulltext",
        "published_at": published, "publication_precision": precision, "original_url": item["url"],
        "article_metadata_time": metadata, "directory_published_at": directory_stamp, "time_conflict": conflict,
        "effective_at": None, "effective_time_status": "unverified", "model_use_allowed": False,
        "attachments_read": False, "semantic_review_required": True}


def collect_policy_sources(session, *, start, cutoff, directory, enabled):
    begin, end = aware(start), aware(cutoff)
    health, events, materials = [], [], []
    for provider, source_id, url, parser in (
        ("pbc", "pbc_policy_news", PBC_INDEX, parse_pbc_index),
        ("csrc", "csrc_regulation_news", CSRC_INDEX, parse_csrc_index),
        ("mof", "mof_fiscal_news", MOF_INDEX, parse_mof_index),
        ("szse", "szse_original_news", SZSE_INDEX, parse_szse_index)):
        state = {"source_id": source_id, "status": "disabled", "failures": [], "semantic_review_required": False}
        health.append(state)
        if source_id not in enabled:
            continue
        try:
            body, receipt = session.get(url, source_id, "index")
            if aware(receipt["fetched_at"]) > end:
                raise ValueError("policy_index_first_seen_after_cutoff")
            rows = parser(body)
            # Read one sorted visible page. Older rows establish the lower
            # boundary; if it is not reached, completeness stays unknown.
            lower_covered = rows[-1]["date"] < begin.date().isoformat() or all(r.get("directory_exhausted") for r in rows)
            scoped = [r for r in rows if r.get("in_scope", True)]
            selected = [r for r in scoped if begin.date().isoformat() <= r["date"] <= end.date().isoformat()]
            state.update(index_limit_reached=not lower_covered, article_limit_reached=len(selected) > 2,
                covered_until_at=receipt["requested_at"], visible_count=len(rows), in_window_count=len(selected),
                outside_registered_scope_count=len(rows)-len(scoped),
                acquisition_mode=receipt.get("acquisition_mode", "offline_test"))
            for item in selected[:2]:
                try:
                    article, received = session.get(item["url"], source_id, "article")
                    if aware(received["fetched_at"]) > end:
                        raise ValueError("policy_body_first_seen_after_cutoff")
                    parsed = parse_policy_article(article, provider, item)
                    published = (datetime.combine(date.fromisoformat(parsed["published_at"]), time.max, SHANGHAI)
                        if parsed["publication_precision"] == "date" else aware(parsed["published_at"]))
                    if published < begin:
                        continue
                    content_hash = hashlib.sha256((parsed["title"] + "\n" + parsed["content"]).encode()).hexdigest()
                    observation = session.observe(source_id, item["url"], content_hash, received["fetched_at"])
                    identity = source_id + "-" + content_hash
                    materials.append({**parsed, **observation, "content_hash": content_hash,
                        "evidence_id": identity, "receipt": received, "source_id": source_id})
                    state["semantic_review_required"] = True
                    if published > end:
                        state["failures"].append("policy_publication_time_ambiguous")
                    events.append({"event_id": identity, "text": parsed["title"] + "（已取得正文，影响及生效条件待核验）",
                        "validation_status": "pending", "claim_type": "publication_notice", "risk_status": "unknown",
                        "impact_scope": "unverified", "security_ids": [], "sector_ids": [], "citations": [],
                        "semantic_review_required": True, "is_background": observation["previously_observed"], "sources": [{
                            "source_id": source_id, "evidence_id": identity,
                            "original_url": item["url"], "public_url": parsed.get("public_url", item["url"]),
                            "locator_notice": "按来源链接规则展示首页；原始文章定位保存在私有采集记录" if parsed.get("public_url", item["url"]) != item["url"] else None,
                            "published_at": parsed["published_at"], "publication_precision": parsed["publication_precision"],
                            "first_seen_at": observation["first_seen_at"], "fetched_at": received["fetched_at"], "content_hash": content_hash}]})
                except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                    state["failures"].append("policy_article_" + type(exc).__name__)
            state["status"] = "partial" if state["failures"] else "ok" if selected else "empty_bounded_window"
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            state.update(status="failed", failures=["policy_directory_" + type(exc).__name__])
        atomic_json(directory / "policy_materials.json", materials)
        atomic_json(directory / "policy_health.json", health)
    return {"source_health": health, "events": events, "local_materials": materials}
