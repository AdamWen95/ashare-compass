"""Synthetic source fixtures; no live requests or copied announcement text."""
from datetime import datetime
import json

import pytest


def test_shared_budget_counts_robots_failures_and_stops_forbidden_host(tmp_path):
    from ashare_daily.premarket.source_http import SourceSession
    from ashare_daily.research.sources import HttpResponse
    calls = []
    def get(url, data, timeout, limit):
        calls.append(url)
        return HttpResponse(404, b"") if url.endswith("robots.txt") else HttpResponse(403, b"denied")
    session = SourceSession(tmp_path, max_requests=3, max_seconds=10, transport=get, sleep=lambda _: None)
    with pytest.raises(PermissionError):
        session.get("https://www.cninfo.com.cn/new/data/szse_stock.json", "cninfo", "mapping")
    with pytest.raises(PermissionError):
        session.get("https://www.cninfo.com.cn/new/data/szse_stock.json", "cninfo", "mapping")
    assert len(calls) == session.request_count == 2
    assert json.loads((tmp_path / "requests.json").read_text())[1]["http_status"] == 403


def test_pdf_html_response_stops_same_host(tmp_path):
    from ashare_daily.premarket.source_http import SourceSession
    from ashare_daily.research.sources import HttpResponse
    session = SourceSession(tmp_path, max_requests=4, max_seconds=10,
        transport=lambda url, *args: HttpResponse(404, b"") if url.endswith("robots.txt") else HttpResponse(200, b"<html>verify</html>", "text/html"), sleep=lambda _: None)
    with pytest.raises(PermissionError, match="pdf_response_not_pdf"):
        session.get("https://static.cninfo.com.cn/finalpage/2026-10-13/123.PDF", "cninfo", "pdf")
    assert "static.cninfo.com.cn" in session.stopped_hosts


def test_cninfo_map_is_identity_check_not_exchange_or_universe_inference():
    from ashare_daily.premarket.announcements import resolve_company
    mapping = {"stockList": [{"code": "603230", "zwjc": "测试公司", "orgId": "opaque", "category": "A股"}]}
    row = {"symbol": "sh.603230", "name": "测试公司", "security_id": "verified-id"}
    assert resolve_company(mapping, row)["orgId"] == "opaque"
    mapping["stockList"].append({**mapping["stockList"][0], "orgId": "other"})
    with pytest.raises(ValueError, match="identity"):
        resolve_company(mapping, row)


def announcement():
    return {"secCode": "300872", "secName": "测试公司", "orgId": "opaque", "announcementId": "123",
        "announcementTitle": "关于签订合同的公告", "announcementTime": 1791851400000,
        "adjunctUrl": "finalpage/2026-10-13/123.PDF", "adjunctType": "PDF"}


def test_cninfo_zero_totalpages_is_not_empty_and_truncation_not_clear():
    from ashare_daily.premarket.announcements import parse_directory
    record = announcement()
    company = {"code": "300872", "zwjc": "测试公司", "orgId": "opaque"}
    raw = {"totalRecordNum": 1, "hasMore": False, "totalpages": 0, "announcements": [record]}
    parsed = parse_directory(raw, company)
    assert len(parsed["records"]) == 1 and parsed["complete"]
    raw.update(totalRecordNum=31, hasMore=True)
    assert not parse_directory(raw, company)["complete"]
    raw["announcements"][0]["adjunctUrl"] = "https://example.com/evil.pdf"
    with pytest.raises(ValueError, match="url"):
        parse_directory(raw, company)


def test_cninfo_wrong_company_or_missing_counts_fails_closed():
    from ashare_daily.premarket.announcements import parse_directory
    company = {"code": "300872", "zwjc": "测试公司", "orgId": "opaque"}
    raw = {"totalRecordNum": 0, "hasMore": False, "announcements": []}
    assert parse_directory(raw, company)["complete"]
    del raw["totalRecordNum"]
    with pytest.raises(ValueError): parse_directory(raw, company)
    raw.update(totalRecordNum=1, announcements=[{**announcement(), "secCode": "000001"}])
    with pytest.raises(ValueError, match="identity"): parse_directory(raw, company)


def test_pdf_text_must_match_company_and_specific_risks_are_pending():
    from ashare_daily.premarket.announcements import review_text
    company = {"code": "300872", "zwjc": "测试公司"}
    text = "证券代码：300872 证券简称：测试公司\n本公司不存在虚假记载、误导性陈述或重大遗漏。\n合同履约仍存在不确定性，可能造成损失。"
    review = review_text(text, company)
    assert review["risk_status"] == "review_required"
    assert "合同履约" in review["risk_flags"][0]
    assert not any("虚假记载" in flag for flag in review["risk_flags"])
    with pytest.raises(ValueError, match="identity"): review_text(text.replace("300872", "000001"), company)


def test_pdf_worker_rejects_non_pdf_and_scanned_document(tmp_path):
    from ashare_daily.premarket.pdf_text import extract_pdf
    from pypdf import PdfWriter
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"<html>verification</html>")
    with pytest.raises(ValueError): extract_pdf(bad, max_seconds=5)
    PdfWriter().add_blank_page(width=100, height=100)  # separate writer below preserves page
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(bad)
    with pytest.raises(ValueError, match="pdf_text_unavailable"): extract_pdf(bad, max_seconds=5)


def test_pbc_index_uses_visible_date_and_does_not_assume_url_is_publication():
    from ashare_daily.premarket.policy_sources import parse_pbc_index
    page = '<td><font class="newslist_style"><a href="/goutongjiaoliu/113456/113469/202601011234/index.html">公告</a></font><span class="hui12">2026-10-12</span></td>'
    rows = parse_pbc_index(page.encode())
    assert rows[0]["date"] == "2026-10-12"
    with pytest.raises(ValueError): parse_pbc_index(b"<html>script-only</html>")


def test_csrc_json_time_is_metadata_not_proven_effective_time():
    from ashare_daily.premarket.policy_sources import parse_csrc_index, parse_policy_article
    data = {"data": {"page": 1, "rows": 18, "total": 1, "results": [{"title": "某办法征求意见", "url": "//www.csrc.gov.cn/csrc/c100028/c123/content.shtml", "publishedTimeStr": "2026-10-12 16:59:43"}]}}
    row = parse_csrc_index(json.dumps(data).encode())[0]
    html = '<meta name="ArticleTitle" content="某办法征求意见"><meta name="PubDate" content="2026-10-12 17:55:40"><div class="info">日期：2026-10-12 来源：证监会</div><div class="detail-news"><p>该办法现向社会公开征求意见，履行相关程序后发布实施。</p></div>'
    item = parse_policy_article(html.encode(), "csrc", row)
    assert item["published_at"] == "2026-10-12"
    assert item["publication_precision"] == "date"
    assert item["time_conflict"] is True
    assert item["effective_at"] is None
    assert item["model_use_allowed"] is False


def test_empty_complete_company_window_has_auditable_directory_evidence(tmp_path):
    from ashare_daily.premarket.announcements import collect_company_reviews
    class Session:
        request_count = 0
        def get(self, url, sid, role, **kwargs):
            body = {"stockList": [{"code": "300872", "zwjc": "测试公司", "orgId": "opaque", "category": "A股"}]} if role == "mapping" else {"totalRecordNum": 0, "hasMore": False, "announcements": []}
            return json.dumps(body).encode(), {"raw_sha256": "a"*64, "fetched_at": "2026-10-13T08:52:00+08:00", "requested_at": "2026-10-13T08:51:59+08:00", "raw_locator": "fixture"}
    result = collect_company_reviews(Session(), [{"symbol": "sz.300872", "name": "测试公司", "security_id": "s1"}],
        start="2026-10-12T08:55:00+08:00", cutoff="2026-10-13T08:55:00+08:00", directory=tmp_path)
    review = result["reviews"]["s1"]
    assert review["status"] == "ready" and review["risk_status"] == "clear"
    assert review["evidence_ids"] and review["covered_until_at"] == "2026-10-13T08:51:59+08:00"
    assert "窗口" in review["notice"]
