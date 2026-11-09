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
            "publication_precision": precision,
            "directory_version": hashlib.sha256(json.dumps({k:row.get(k) for k in ("announcementId", "announcementTime", "adjunctUrl", "adjunctSize", "announcementTitle")}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
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


def collect_company_reviews(session, candidates, *, start, cutoff, directory,
                            cache_directory=None, decision_directory=None, query_windows=None):
    from .announcement_review import VERSION, publication_scope, finalize_review
    begin, end = aware(start), aware(cutoff)
    if begin >= end or len(candidates) > 5 or len({r["security_id"] for r in candidates}) != len(candidates):
        raise ValueError("announcement_scope_invalid")
    reviews = {}
    for candidate in candidates:
        identity = candidate['security_id']
        window = (query_windows or {}).get(identity, {})
        effective = window.get('start_at', start)
        if not begin <= aware(effective) < end:
            raise ValueError('company_window_outside_request')
        reviews[identity] = {"status": "partial", "risk_status": "unknown", "evidence_ids": [], "gaps": [],
            "announcements": [], "risk_flags": [], "query_start_at": effective, "query_cutoff_at": cutoff,
            "prior_window_evidence": window.get('prior_window_evidence'), "review_version": VERSION,
            "coverage_origin_at": window.get('coverage_origin_at', effective),
            "notice": "仅核查本期公告窗口；不代表公司不存在历史风险", "model_use_allowed": False}
    if not candidates:
        return {"reviews": reviews, "status": "not_applicable", "source_id": SOURCE}
    try:
        body, mapping_receipt = session.get(MAPPING, SOURCE, "mapping")
        mapping = json.loads(body)
        if aware(mapping_receipt["fetched_at"]) > end:
            raise ValueError("mapping_first_seen_after_cutoff")
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        for review in reviews.values():
            review["gaps"].append("公告身份映射未取得：" + type(exc).__name__)
            finalize_review(review)
        atomic_json(directory / "company_reviews.json", reviews)
        return {"reviews": reviews, "status": "partial", "source_id": SOURCE}
    companies = {}
    # Reserve access to every directory before spending remaining requests on PDFs.
    for candidate in candidates:
        review = reviews[candidate['security_id']]
        effective = aware(review['query_start_at'])
        try:
            company = resolve_company(mapping, candidate)
            companies[candidate['security_id']] = company
            form = {"pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext",
                "plate": "", "stock": company["code"] + "," + company["orgId"], "searchkey": "", "secid": "", "category": "",
                "trade": "", "seDate": effective.date().isoformat() + "~" + end.date().isoformat(),
                "sortName": "", "sortType": "", "isHLtitle": "true"}
            body, receipt = session.get(DIRECTORY, SOURCE, "directory", data=form)
            parsed = parse_directory(json.loads(body), company)
            if aware(receipt["fetched_at"]) > end:
                raise ValueError("directory_first_seen_after_cutoff")
            review["directory_evidence"] = {**receipt, "total": parsed["total"], "mapping_sha256": mapping_receipt["raw_sha256"]}
            review["evidence_ids"].append("cninfo-directory-" + hashlib.sha256((candidate["security_id"] + receipt["raw_sha256"] + review['query_start_at'] + cutoff).encode()).hexdigest())
            review['window_status'] = 'complete' if parsed['complete'] else 'partial'
            if not parsed['complete']:
                review['gaps'].append('公告目录超过单页上限或计数不一致，窗口未完整覆盖')
            for item in parsed['records']:
                scope = publication_scope(item, review['query_start_at'], cutoff, receipt['fetched_at'])
                if scope == 'outside':
                    continue
                if scope == 'future':
                    review['gaps'].append('公告目录包含截点后的资料，未纳入本期结论')
                    continue
                if scope == 'start_boundary':
                    review['gaps'].append('公告日期与查询起点重叠，保留风险核查但不能确认属于本期新增')
                review['announcements'].append({**item, 'window_relation': scope,
                    'directory_observed_at': receipt['fetched_at'], 'body_status': 'pending', 'semantic_status': 'pending'})
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            review['gaps'].append('公司公告目录核查未完成：' + type(exc).__name__)
        atomic_json(directory / 'company_reviews.json', reviews)
    for index in range(max((len(r['announcements']) for r in reviews.values()), default=0)):
        for candidate in candidates:
            review = reviews[candidate['security_id']]
            if index >= len(review['announcements']):
                continue
            item = review['announcements'][index]
            try:
                checked = _read_body(session, candidate, companies[candidate['security_id']], item,
                    directory, cutoff, cache_directory, decision_directory)
                review['announcements'][index] = checked
                review['evidence_ids'].append(checked['evidence_id'])
                review['risk_flags'].extend(checked['risk_flags'])
                if checked.get('semantic_issue'):
                    review.setdefault('semantic_issues', []).append(checked['semantic_issue'])
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                code = str(exc) if re.fullmatch(r'[a-z][a-z0-9_]{1,100}', str(exc)) else 'request_or_parse_failed'
                item.update(body_status='unavailable', body_issue=code, body_error_type=type(exc).__name__)
                message = ('本期正文请求额度用尽，剩余公告保留待查' if code=='source_request_budget_exhausted' else
                    '公告源访问受限，已停止该源请求' if isinstance(exc, PermissionError) else
                    '正文未在本期时限内完成' if isinstance(exc, TimeoutError) else '公告正文获取或解析未完成，已保留诊断')
                review['gaps'].append(message)
    for review in reviews.values():
        finalize_review(review)
    atomic_json(directory / 'company_reviews.json', reviews)
    return {'reviews': reviews, 'status': 'ready' if all(r['status']=='ready' for r in reviews.values()) else 'partial',
            'source_id': SOURCE}


def _read_body(session, candidate, company, item, directory, cutoff, cache_directory, decision_directory):
    from .announcement_cache import load_body, store_body
    from .announcement_review import apply_local_decision
    cached = load_body(cache_directory, candidate, item, cutoff=cutoff)
    if cached:
        text = cached.pop('body_text')
        checked = {**item, **cached, 'previously_observed': True}
    else:
        budget = getattr(session, 'budget', None)
        if budget is not None and budget.used >= budget.maximum:
            raise RuntimeError('source_request_budget_exhausted')
        raw, receipt = session.get(item['original_url'], SOURCE, 'pdf')
        if aware(receipt['fetched_at']) > aware(cutoff):
            raise ValueError('pdf_first_seen_after_cutoff')
        parsed = extract_pdf(receipt['raw_locator'], max_seconds=min(10, session.remaining()))
        text = parsed['text']
        review_text(text, company)
        observation = session.observe(SOURCE, item['original_url'], receipt['raw_sha256'], receipt['fetched_at'])
        text_path = directory/('cninfo-pdf-'+receipt['raw_sha256']+'.txt')
        text_path.write_text(text, encoding='utf-8')
        checked = {**item, **observation, 'fetched_at': receipt['fetched_at'], 'content_hash': receipt['raw_sha256'],
            'raw_locator': receipt['raw_locator'], 'text_locator': str(text_path.resolve()), 'pages': parsed['pages'],
            'cache_reused': False}
        store_body(cache_directory, candidate, item, raw, text, receipt, observation, parsed['pages'])
    checked.update(evidence_id='cninfo-pdf-'+checked['content_hash'], source_id=SOURCE, text_hash=hashlib.sha256(text.encode()).hexdigest(),
        body_status='extracted', **review_text(text, company))
    try:
        return apply_local_decision(checked, candidate, text, decision_directory, cutoff=cutoff)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        checked.update(semantic_status='pending', semantic_issue='本地核查记录未通过校验：'+type(exc).__name__)
        return checked
