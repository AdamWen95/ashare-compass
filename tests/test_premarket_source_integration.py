"""Failure and budget boundaries between local sources, the model, and reports."""
from datetime import datetime
import json

import pytest


def test_registered_text_and_local_requests_share_one_budget(tmp_path, source):
    from test_m3_sources import registry, article
    from ashare_daily.research.sources import collect_materials, HttpResponse
    from ashare_daily.premarket.source_http import RequestBudget
    budget = RequestBudget(2, tmp_path / "budget.json")
    budget.reserve("local", "index", "https://www.pbc.gov.cn/")
    calls = []
    def get(url, timeout):
        calls.append(url)
        return HttpResponse(404, b"") if url.endswith("robots.txt") else HttpResponse(200, article())
    result = collect_materials(registry_path=registry(tmp_path, source), start="2026-09-01T00:00:00+08:00",
        cutoff="2026-09-10T08:55:00+08:00", sample_symbols={}, output_dir=tmp_path / "out",
        transport=get, sleep=lambda _: None, request_budget=budget)
    assert budget.used == 2 and len(calls) == 1
    assert not result["evidence"] and result["source_health"][0]["failures"]


@pytest.fixture
def source():
    from test_m3_sources import source as fixture
    return fixture.__wrapped__()


def test_uninterpreted_local_policy_blocks_positive_clearance():
    from ashare_daily.premarket.source_policy import coverage_summary
    registry = {"sources": [{"registration": {"source_id": "pbc", "enabled": True}, "research_roles": ["macro_policy"]}]}
    collection = {"source_health": [{"source_id": "pbc", "status": "ok", "semantic_review_required": True, "failures": []}]}
    assert not coverage_summary(registry, collection, required_roles=["macro_policy"])["ready"]


def test_policy_empty_recent_window_not_perpetually_truncated(tmp_path):
    from ashare_daily.premarket.policy_sources import collect_policy_sources
    class Session:
        def get(self, url, sid, role):
            assert role == "index"
            if sid == "pbc_policy_news":
                body = '<td><font class="newslist_style"><a href="/goutongjiaoliu/113456/113469/20261010001/index.html">旧发布</a></font><span class="hui12">2026-10-10</span></td>'.encode()
            else:
                body = json.dumps({"data": {"page": 1, "rows": 18, "total": 1, "results": [{"title": "旧发布", "url": "//www.csrc.gov.cn/csrc/c100028/c123/content.shtml", "publishedTimeStr": "2026-10-10 16:00:00"}]}}).encode()
            return body, {"fetched_at": "2026-10-13T08:53:00+08:00", "requested_at": "2026-10-13T08:52:00+08:00"}
    result = collect_policy_sources(Session(), start="2026-10-12T08:55:00+08:00", cutoff="2026-10-13T08:55:00+08:00",
        directory=tmp_path, enabled={"pbc_policy_news", "csrc_regulation_news"})
    assert not result["events"]
    assert all(h["status"] == "empty_bounded_window" and not h["index_limit_reached"] for h in result["source_health"] if h["source_id"] in {"pbc_policy_news", "csrc_regulation_news"})
    assert all(h["status"] == "disabled" for h in result["source_health"] if h["source_id"] not in {"pbc_policy_news", "csrc_regulation_news"})


def test_local_events_survive_model_error_and_are_not_given_to_model(tmp_path):
    from test_premarket_daily import setup, Clock, Services, invoke
    from ashare_daily.premarket.archive import scan_briefs
    event = {"event_id": "local", "text": "有新公告，待核验", "validation_status": "pending", "sources": []}
    class Failure(Services):
        def news(self, *args):
            return {**super().news(*args), "local_events": [event]}
        def interpret(self, *args):
            raise RuntimeError("model_unavailable")
    cfg, clock = setup(tmp_path), Clock()
    result = invoke(tmp_path, cfg, Failure(clock), clock)
    assert result["generation_status"] == "ok"
    reports, _ = scan_briefs(tmp_path / "outputs", now=clock.now())
    assert reports[0]["report"]["overnight_events"] == [event]


def test_local_policy_cannot_accidentally_enable_model_export(tmp_path):
    from test_premarket_daily import setup
    from ashare_daily.operations.premarket import load_policy
    cfg = setup(tmp_path)
    path = tmp_path / cfg.premarket_config
    policy = json.loads(path.read_text())
    policy["local_sources"][0]["registration"]["model_use_allowed"] = True
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="local_source_policy"):
        load_policy(tmp_path, cfg)


def test_late_company_directory_never_counts_as_clear(tmp_path):
    from ashare_daily.premarket.announcements import collect_company_reviews
    class Session:
        def get(self, url, sid, role, **kwargs):
            body = {"stockList": [{"code": "300872", "zwjc": "测试公司", "orgId": "opaque", "category": "A股"}]} if role == "mapping" else {"totalRecordNum": 0, "hasMore": False, "announcements": []}
            return json.dumps(body).encode(), {"raw_sha256": "a"*64, "fetched_at": "2026-10-13T08:56:00+08:00" if role == "directory" else "2026-10-13T08:51:00+08:00", "requested_at": "2026-10-13T08:51:00+08:00"}
    result = collect_company_reviews(Session(), [{"symbol": "sz.300872", "name": "测试公司", "security_id": "s"}],
        start="2026-10-12T08:55:00+08:00", cutoff="2026-10-13T08:55:00+08:00", directory=tmp_path)
    assert result["reviews"]["s"]["status"] == "partial"
    assert result["reviews"]["s"]["risk_status"] == "unknown"


def test_cninfo_explicit_zero_and_null_is_bounded_empty():
    from ashare_daily.premarket.announcements import parse_directory
    assert parse_directory({"totalRecordNum": 0, "hasMore": False, "announcements": None}, {})["complete"]
    with pytest.raises(ValueError): parse_directory({"totalRecordNum": 0, "hasMore": False}, {})


def test_live_service_wiring_keeps_local_bodies_out_of_model_bundle(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from pathlib import Path
    from ashare_daily.premarket import services
    from ashare_daily.premarket.source_http import SourceSession
    from ashare_daily.premarket.contracts import SHANGHAI
    from ashare_daily.research.sources import HttpResponse
    now = datetime.now(SHANGHAI)
    old = (now - timedelta(days=2)).date().isoformat()
    def transport(url, data, timeout, limit):
        if url.endswith("robots.txt"): return HttpResponse(404, b"")
        if "pbc.gov.cn" in url:
            return HttpResponse(200, f'<td><font class="newslist_style"><a href="/goutongjiaoliu/113456/113469/20260101001/index.html">旧发布</a></font><span class="hui12">{old}</span></td>'.encode(), "text/html")
        if "csrc.gov.cn" in url:
            body = {"data": {"page": 1, "total": 1, "results": [{"title": "旧发布", "url": "//www.csrc.gov.cn/csrc/c100028/c123/content.shtml", "publishedTimeStr": old + " 16:00:00"}]}}
        elif url.endswith(".json"):
            body = {"stockList": [{"code": "300872", "zwjc": "测试公司", "orgId": "opaque", "category": "A股"}]}
        else:
            assert data["stock"] == "300872,opaque"
            body = {"totalRecordNum": 0, "hasMore": False, "announcements": None}
        return HttpResponse(200, json.dumps(body).encode(), "application/json")
    monkeypatch.setattr(services, "SourceSession", lambda directory, **kwargs: SourceSession(directory, **kwargs, transport=transport, sleep=lambda _: None))
    seen = []
    def collect(**kwargs):
        seen.append(kwargs)
        kwargs["request_budget"].reserve("licensed", "index", "https://www.mofcom.gov.cn/")
        return {"evidence": [{"evidence_id": "licensed-only"}], "bundle_file": "legacy.json",
                "finished_at": datetime.now(SHANGHAI).isoformat()}
    def archive(**kwargs):
        assert kwargs["collection"]["evidence"] == [{"evidence_id": "licensed-only"}]
        return {"bundle_file": "licensed.json"}
    monkeypatch.setattr(services, "collect_materials", collect)
    monkeypatch.setattr(services, "archive_materials", archive)
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "config/premarket_research.json").read_text())
    # This synthetic transport models permitted sources; real registry gates
    # remain disabled where access-rules acceptance failed.
    for entry in policy["local_sources"]:
        entry["registration"]["enabled"] = entry["registration"]["source_id"] in {"pbc_policy_news", "csrc_regulation_news", "cninfo_company_announcements"}
    (tmp_path / "sources.json").write_bytes((root / "config/m3_sources.json").read_bytes())
    config = SimpleNamespace(sources_config="sources.json", database="market.sqlite3")
    result = services.PremarketServices(tmp_path, config, policy).news(
        {"news_cutoff_at": (now + timedelta(seconds=120)).isoformat()},
        {"start_at": (now - timedelta(days=1)).isoformat()},
        {"preselection": {"candidates": [{"symbol": "sz.300872", "name": "测试公司", "security_id": "s"}]}, "entities": {}},
        tmp_path, 110)
    assert result["coverage"]["ready"] is True
    assert result["coverage"]["request_count"] == 8
    assert result["bundle_file"] == "licensed.json"
    assert result["company_reviews"]["s"]["risk_status"] == "clear"
    assert "local_materials" not in seen[0]
