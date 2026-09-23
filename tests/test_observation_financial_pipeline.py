"""Financial enrichment is bounded to the qualified ranked daily candidates."""
from copy import deepcopy
import json

import pytest

from ashare_daily.operations import observation as daily
from test_observation_daily import config, fake_pipeline, invoke, report_inputs


def test_financial_stage_only_uses_top_qualified_candidates(tmp_path, monkeypatch):
    from ashare_daily import financial_review
    cfg = config(tmp_path)
    options = daily.load_options(tmp_path, cfg.observation_config)
    selection, observation, _ = report_inputs([("pass", "pass")] * 7 + [("pass", "pending"), ("pass", "fail")])
    captured = {}
    def collect(**kwargs):
        captured.update(kwargs)
        return {"status": "complete"}
    monkeypatch.setattr(financial_review, "collect_financial_review", collect)
    original = deepcopy(observation)
    result = daily._financials(tmp_path, selection, observation, options, tmp_path / "review",
        {"cutoff_at": "2026-09-11T21:00:00+08:00"}, max_seconds=45)
    assert result["status"] == "complete"
    assert [r["security_id"] for r in captured["candidates"]] == ["TEST-6", "TEST-5", "TEST-4", "TEST-3", "TEST-2"]
    assert captured["max_seconds"] == 45 and captured["max_queries"] == 20
    assert captured["cutoff_at"] == "2026-09-11T21:00:00+08:00"
    assert observation == original


def test_financial_offline_when_daily_budget_exhausted(tmp_path, monkeypatch):
    from ashare_daily import financial_review
    cfg = config(tmp_path)
    options = daily.load_options(tmp_path, cfg.observation_config)
    selection, observation, _ = report_inputs([("pass", "pass")])
    captured = {}
    monkeypatch.setattr(financial_review, "collect_financial_review", lambda **kwargs: captured.update(kwargs))
    daily._financials(tmp_path, selection, observation, options, tmp_path / "review",
        {"cutoff_at": "2026-09-11T21:00:00+08:00"}, max_seconds=0)
    assert captured["online"] is False and captured["max_seconds"] == .001


def test_disabled_financial_stage_is_noop(tmp_path):
    assert daily._financials(tmp_path, {}, {}, {}, tmp_path / "review", {}, max_seconds=40) is None
    assert not (tmp_path / "review").exists()


@pytest.mark.parametrize("key,value", [("llm_export", True), ("user_authorized", False),
    ("max_candidates", 6), ("max_queries", 21), ("max_seconds", 121), ("timeout_seconds", True)])
def test_financial_config_rejects_permission_or_budget_expansion(tmp_path, key, value):
    cfg = config(tmp_path)
    path = tmp_path / cfg.observation_config
    options = json.loads(path.read_text(encoding="utf-8"))
    options["financial_review"][key] = value
    path.write_text(json.dumps(options), encoding="utf-8")
    with pytest.raises(ValueError):
        daily.load_options(tmp_path, cfg.observation_config)


def test_daily_runs_financials_before_model_without_exporting_packet(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    calls = fake_pipeline(monkeypatch, [("pass", "pass")])
    packet = {"status": "unavailable", "network_requests": 1, "cache_hits": 0,
        "content_hash": "fixture", "records": []}
    def review(*args, **kwargs):
        calls.append("financials")
        return packet
    def publish(*args, **kwargs):
        assert kwargs["financial_review"] is packet
        assert "financial_review" not in args[3]
        return {"directory": str(tmp_path / "published")}
    monkeypatch.setattr(daily, "_financials", review)
    monkeypatch.setattr(daily, "_publish", publish)
    result = invoke(tmp_path, cfg)
    assert calls == ["universe", "selection", "screening", "financials", "model"]
    assert result["generation_status"] == "ok"
    assert result["module_statuses"]["financial_review"] == "unavailable"
