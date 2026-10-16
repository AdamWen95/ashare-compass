"""Synthetic network receipts exercise real collection, archive and model boundaries."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_m3_sources import TEXT, article, registry, source
from ashare_daily.premarket import services, source_http
from ashare_daily.research import model as model_transport, runner, sources
from ashare_daily import sector_observation_model
from ashare_daily.operations import budget, daily
from ashare_daily.storage.market import MarketStore


START = "2026-10-15T08:55:00+08:00"
DEADLINE = "2026-10-16T08:55:00+08:00"
PREPARED = {"preselection": {"candidates": []}, "entities": {}}


def setup_service(tmp_path, monkeypatch, source, *, began, finished, later_article=False):
    stamp = [datetime.fromisoformat(began)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp[0]

    for module in (services, source_http, sources, runner, sector_observation_model, budget, daily):
        monkeypatch.setattr(module, "datetime", Clock)

    def get(url, data, timeout, limit):
        if url.endswith("robots.txt"):
            return sources.HttpResponse(404, b"")
        stamp[0] = datetime.fromisoformat(finished)
        if url.endswith("blocked.html"):
            return sources.HttpResponse(200, article(body=TEXT * 4, published="2026-10-16 08:39"))
        if url.endswith("later.html"):
            return sources.HttpResponse(200, article(
                body="OFFLINE_TEST 后到的独立测试材料，不代表任何真实新闻。" * 3,
                published="2026-10-16 08:41"))
        return sources.HttpResponse(200, article(body=TEXT * 4, published="2026-10-16 08:40"))

    # Replace only I/O: the shared reservation budget, collector, evidence store,
    # bundle validation and context interval checks all run unchanged in tmp_path.
    monkeypatch.setattr(source_http, "_request", get)
    monkeypatch.setattr(model_transport, "http_transport", lambda *args: model_transport.HTTPResponse(
        200, json.dumps({"choices": [{"message": {"content": '{"claims": []}'},
            "finish_reason": "stop"}]}).encode(), {}))
    source["registration"].update(model_use_allowed=True, publish_excerpt_allowed=True)
    if later_article:
        source["pages"].append({"url": "https://example.org/later.html", "content_type": "fulltext"})
    registry(tmp_path, source, max_requests=20, max_attempts=1)
    MarketStore(tmp_path / "market.sqlite3")
    (tmp_path / "model.env").write_text(
        "MODEL_BASE_URL=https://model.example.invalid/v1\nMODEL_NAME=synthetic\n"
        "MODEL_API_KEY=OFFLINE_TEST_ONLY\n", encoding="utf-8")
    config = SimpleNamespace(sources_config="sources.json", database="market.sqlite3",
        env_file="model.env", model_max_calls_per_run=2, model_timeout_seconds=30,
        model_max_output_tokens=1024, model_max_calls_per_day=6)
    policy = {"news_max_requests": 20, "required_roles": ["licensed_news", "macro_policy"],
        "source_roles": {"offline-test": ["licensed_news"]}, "local_sources": []}
    return services.PremarketServices(tmp_path, config, policy), stamp


@pytest.mark.parametrize(("began", "finished"), [
    ("2026-10-16T08:41:00+08:00", "2026-10-16T08:42:00+08:00"),
    ("2026-10-16T08:54:00+08:00", "2026-10-16T08:54:30+08:00"),
    ("2026-10-16T08:54:50+08:00", DEADLINE),
])
def test_finished_collection_archives_and_reaches_model_before_scheduled_cutoff(
        tmp_path, monkeypatch, source, began, finished):
    service, _ = setup_service(tmp_path, monkeypatch, source, began=began, finished=finished)
    schedule = {"news_cutoff_at": DEADLINE}
    news = service.news(schedule, {"start_at": START}, PREPARED, tmp_path, 300)

    assert news["bundle_file"] is not None, news["supplement_error"]
    assert news["supplement_error"] is None
    assert news["collection_cutoff_at"] == finished
    bundle = runner.read_material_bundle(Path(news["bundle_file"]))
    assert bundle["cutoff"] == finished
    assert bundle["evidence"][0]["first_seen_at"] == finished
    assert bundle["evidence"][0]["fetched_at"] == finished
    assert bundle["selection_summary"]["historical_reconstruction"] is False
    assert news["coverage"]["cutoff_at"] == DEADLINE
    assert news["coverage"]["covered_until_at"] == finished
    assert news["coverage"]["request_count"] == 2
    assert news["coverage"]["ready"] is False
    assert news["coverage"]["roles"]["macro_policy"]["status"] == "not_enabled"

    interpreted = service.interpret(news, PREPARED, schedule, tmp_path, "cutoff-test", 30, False)
    assert interpreted["model_run"]["call_count"] == 1
    context = json.loads((tmp_path / "context/model_context.json").read_text(encoding="utf-8"))
    assert context["input_snapshot"]["cutoff_at"] == finished
    assert context["coverage"]["model_input_evidence_count"] == 1
    assert context["coverage"]["historical_reconstruction"] is False
    assert context["model_run"]["configuration"]["max_retries"] == 0
    assert context["model_run"]["configuration"]["max_calls"] == 2
    assert context["model_run"]["daily_budget"]["reserved_attempts"] == 1


def test_completion_horizon_does_not_expand_original_publication_window(tmp_path, monkeypatch, source):
    service, _ = setup_service(tmp_path, monkeypatch, source,
        began="2026-10-16T08:41:00+08:00", finished="2026-10-16T08:42:00+08:00", later_article=True)
    schedule = {"news_cutoff_at": DEADLINE}
    news = service.news(schedule, {"start_at": START}, PREPARED, tmp_path, 300)
    assert news["bundle_file"] is not None, news["supplement_error"]
    bundle = runner.read_material_bundle(Path(news["bundle_file"]))
    assert len(bundle["evidence"]) == 2  # Rejected acquisition remains auditable.
    assert bundle["collection"]["news_cutoff"] == "2026-10-16T08:41:00+08:00"
    assert bundle["collection"]["rejected"][0]["reason"] == "after_cutoff_or_date_precision_ambiguous"

    service.interpret(news, PREPARED, schedule, tmp_path, "publication-test", 30, False)
    context = json.loads((tmp_path / "context/model_context.json").read_text(encoding="utf-8"))
    assert [item["original_url"] for item in context["evidence_catalog"]] == ["https://example.org/news.html"]


def test_late_receipt_stays_archived_but_cannot_enter_model(tmp_path, monkeypatch, source):
    service, _ = setup_service(tmp_path, monkeypatch, source,
        began="2026-10-16T08:54:50+08:00", finished="2026-10-16T08:55:01+08:00")
    schedule = {"news_cutoff_at": DEADLINE}
    news = service.news(schedule, {"start_at": START}, PREPARED, tmp_path, 300)
    assert news["bundle_file"] is not None, news["supplement_error"]
    assert news["collection_cutoff_at"] == DEADLINE
    bundle = runner.read_material_bundle(Path(news["bundle_file"]))
    assert bundle["evidence"][0]["fetched_at"] == "2026-10-16T08:55:01+08:00"
    assert bundle["evidence"][0]["first_seen_at"] == "2026-10-16T08:55:01+08:00"
    assert news["coverage"]["covered_until_at"] is None
    interpreted = service.interpret(news, PREPARED, schedule, tmp_path, "late-test", 30, False)
    assert interpreted["model_run"] == {"status": "no_eligible_evidence", "call_count": 0, "daily_budget": None}


def test_publication_filter_does_not_deduplicate_before_source_permissions(tmp_path, monkeypatch, source):
    service, _ = setup_service(tmp_path, monkeypatch, source,
        began="2026-10-16T08:41:00+08:00", finished="2026-10-16T08:42:00+08:00")
    path = tmp_path / "sources.json"
    configured = json.loads(path.read_text(encoding="utf-8"))
    blocked = json.loads(json.dumps(source))
    blocked["registration"].update(source_id="blocked-news", model_use_allowed=False)
    blocked["pages"] = [{"url": "https://example.org/blocked.html", "content_type": "fulltext"}]
    configured["sources"].append(blocked)
    path.write_text(json.dumps(configured), encoding="utf-8")
    schedule = {"news_cutoff_at": DEADLINE}
    news = service.news(schedule, {"start_at": START}, PREPARED, tmp_path, 300)
    interpreted = service.interpret(news, PREPARED, schedule, tmp_path, "permission-test", 30, False)
    assert interpreted["model_run"]["call_count"] == 1
    context = json.loads((tmp_path / "context/model_context.json").read_text(encoding="utf-8"))
    assert [item["source_id"] for item in context["evidence_catalog"]] == ["offline-test"]


def test_already_reached_deadline_does_not_start_collection(tmp_path, monkeypatch, source):
    service, _ = setup_service(tmp_path, monkeypatch, source, began=DEADLINE, finished=DEADLINE)
    before = (tmp_path / "market.sqlite3").read_bytes()
    news = service.news({"news_cutoff_at": DEADLINE}, {"start_at": START}, PREPARED, tmp_path, 300)
    assert news["coverage"]["request_count"] == 0
    assert news.get("bundle_file") is None
    assert not (tmp_path / "local_sources").exists()
    assert (tmp_path / "market.sqlite3").read_bytes() == before
