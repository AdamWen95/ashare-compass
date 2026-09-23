"""The daily production route, using isolated local inputs and blocked network."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import shutil

import pytest

from ashare_daily.operations import observation as daily
from ashare_daily.operations.daily import DailyConfig, run_daily
from ashare_daily.sector_selection import digest
from ashare_daily.reports.observation import (build_report, publish_observation, read_observation_report,
    scan_observation_reports, RELATIVE_ROOT)

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-09-11T21:05:00+08:00"


def seal(value, key="content_hash"):
    value[key] = digest({k: v for k, v in value.items() if k != key})
    return value


def report_inputs(states=()):
    selection = {"mode": "research", "purpose": "production", "production_eligible": True,
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "target_date": "2026-09-11",
        "cutoff_at": STAMP, "selection_verified": True, "selected_security_count": len(states),
        "selection_status": "selected" if states else "no_matching_sectors", "universe_count": 100,
        "members": [{"security_id": f"TEST-{i}"} for i in range(len(states))], "sectors": [], "selected_count": bool(states)}
    seal(selection)
    selection["selection_id"] = "sector-2026-09-11-" + selection["content_hash"][:20]
    binding = {k: selection[k] for k in ("selection_id", "target_date", "mode", "purpose")}
    rows = []
    qualifications = []
    for i, (technical, eligibility) in enumerate(states):
        rows.append({"security_id": f"TEST-{i}", "symbol": f"sh.{600001+i}", "name": f"测试公司{i}",
            "technical_status": technical, "technical_conditions": [], "metrics": {"adjusted_close": "10", "ma20": "9",
                "ma60": "8", "relative_return_20": str(.1+i*.01), "avg_amount_20_cny": "60000000"},
            "strategy_inputs_ready": technical != "unknown", "cache_target_complete": True, "risk_gaps": []})
        qualifications.append({"security_id": f"TEST-{i}", "eligibility_status": eligibility, "conditions": [],
            "facts": [], "gaps": [{"field": "st", "reason": "未知"}] if eligibility == "pending" else [], "exclusion_reasons": []})
    technical = seal({**binding, "evaluations": rows, "strategy_config": {"max_candidates": 20}}, "result_hash")
    eligibility = seal({**binding, "evaluations": qualifications, "technical_result_hash": technical["result_hash"]})
    observation = seal({**binding, "production_eligible": True, "selection_content_hash": selection["content_hash"],
        "technical": technical, "eligibility": eligibility, "gaps": [], "counts": {"blocking_gap_count": sum(t == "unknown" or t == "pass" and e == "pending" for t, e in states)},
        "status": "observations_ready" if states else "no_matching_sectors", "cutoff_at": STAMP})
    context = {"status": "skipped", "analysis": {"accepted_claims": []}, "model_run": {"status": "skipped", "call_count": 0}}
    return selection, observation, context


def publish(root, states=()):
    selection, observation, context = report_inputs(states)
    return publish_observation(root, selection, observation, context,
        planned_cutoff="2026-09-11T21:00:00+08:00", generated_at=STAMP)


def config(root):
    (root / "config").mkdir()
    for p in (ROOT / "config").glob("*.json"):
        shutil.copyfile(p, root / "config" / p.name)
    return DailyConfig.model_validate_json((root / "config/sector_observation_daily.json").read_text(encoding="utf-8"))


def invoke(root, cfg, **kwargs):
    return daily.run_observation_daily(project=root, config=cfg, config_path="config/sector_observation_daily.json",
        target=None, cutoff=None, start=kwargs.pop("start", None), now=kwargs.pop("now", datetime.fromisoformat(STAMP)),
        dry_run=kwargs.pop("dry_run", False), scheduled=kwargs.pop("scheduled", True), planned=None, **kwargs)


def fake_pipeline(monkeypatch, states=(), context_status="skipped"):
    selection, observation, context = report_inputs(states)
    context["status"] = context_status
    calls = []
    def refresh(*args):
        calls.append("universe")
        return {"status": "verified", "universe_verified": True, "collection_ready": True, "ordinary_a_count": 100}
    def select(*args):
        calls.append("selection")
        return selection
    def observe(*args):
        calls.append("screening")
        assert args[-2:] == ("config/sector_screening_f4s1.json", "config/eligibility_sources.json")
        return observation
    def model(*args, **kwargs):
        calls.append("model")
        return context
    monkeypatch.setattr(daily, "_refresh", refresh)
    monkeypatch.setattr(daily, "select_observation", select)
    monkeypatch.setattr(daily, "_observe", observe)
    monkeypatch.setattr(daily, "_context", model)
    monkeypatch.setattr(daily, "_financials", lambda *args, **kwargs: None)
    monkeypatch.setattr(daily, "_reference_review", lambda *args, **kwargs: None)
    return calls


def test_published_report_preserves_pass_pending_exclusion_and_no_company_requirement(tmp_path):
    result = publish(tmp_path, [("pass", "pass"), ("pass", "pending"), ("pass", "fail"), ("fail", "pass")])
    report = read_observation_report(tmp_path / "outputs", result["directory"])["report"]
    assert report["counts"]["stock_count"] == 4
    assert len(report["observations"]) == len(report["pending"]) == 1
    assert report["observations"][0]["company_materials_status"] == "not_reviewed"
    assert "公司公告" in report["notice"]


def test_zero_production_report_is_valid_and_same_input_reuses(tmp_path):
    first = publish(tmp_path)
    before = {p.name: p.read_bytes() for p in Path(first["directory"]).iterdir()}
    second = publish(tmp_path)
    assert second["reused"] and second["report_id"] == first["report_id"]
    assert before == {p.name: p.read_bytes() for p in Path(first["directory"]).iterdir()}


@pytest.mark.parametrize("mutation", ["engineering", "offline", "unverified", "hash", "observation_offline", "observation_ineligible"])
def test_invalid_inputs_cannot_publish(tmp_path, mutation):
    selection, observation, context = report_inputs()
    if mutation == "engineering": selection["purpose"] = "engineering_validation"
    if mutation == "offline": selection["mode"] = "offline_test"
    if mutation == "unverified": selection["selection_verified"] = False
    if mutation == "hash": observation["content_hash"] = "0" * 64
    if mutation == "observation_offline": observation["mode"] = "offline_test"; seal(observation)
    if mutation == "observation_ineligible": observation["production_eligible"] = False; seal(observation)
    with pytest.raises(ValueError):
        publish_observation(tmp_path, selection, observation, context, planned_cutoff=STAMP, generated_at=STAMP)
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("name", ["daily_observation.json", "daily_observation.md", "report_inputs.json", "screening_audit.csv"])
def test_tampered_report_cannot_read_or_export(tmp_path, name):
    result = publish(tmp_path)
    (Path(result["directory"]) / name).write_bytes(b"changed")
    reports, problems = scan_observation_reports(tmp_path / "outputs")
    assert not reports and len(problems) == 1


def test_html_and_markdown_escape_untrusted_stock_name(tmp_path):
    selection, observation, context = report_inputs([("pass", "pass")])
    observation["technical"]["evaluations"][0]["name"] = '<script>alert(1)</script>'
    seal(observation["technical"], "result_hash")
    observation["eligibility"]["technical_result_hash"] = observation["technical"]["result_hash"]
    seal(observation["eligibility"]); seal(observation)
    result = publish_observation(tmp_path, selection, observation, context, planned_cutoff=STAMP, generated_at=STAMP)
    html = (Path(result["directory"]) / "daily_observation.html").read_text(encoding="utf-8")
    assert '<script>alert(1)</script>' not in html and "&lt;script&gt;" in html


def test_dry_run_scheduled_route_has_no_source_calls(tmp_path, monkeypatch):
    cfg = config(tmp_path); calls = fake_pipeline(monkeypatch)
    result = invoke(tmp_path, cfg, dry_run=True)
    assert result["status"] == "dry_run" and not calls


def test_daily_reuses_finished_report_without_new_sources_or_model(tmp_path, monkeypatch):
    cfg = config(tmp_path); calls = fake_pipeline(monkeypatch)
    first = invoke(tmp_path, cfg)
    assert first["generation_status"] == "ok" and calls == ["universe", "selection", "screening", "model"]
    calls.clear()
    second = invoke(tmp_path, cfg)
    assert second["status"] == "reused" and not calls


def test_explicit_news_window_change_does_not_reuse(tmp_path, monkeypatch):
    cfg = config(tmp_path); calls = fake_pipeline(monkeypatch)
    first = invoke(tmp_path, cfg)
    calls.clear()
    second = invoke(tmp_path, cfg, start="2026-09-07T00:00:00+08:00")
    assert second["task_identity"] != first["task_identity"] and calls


def test_force_keeps_old_report_and_creates_new_run(tmp_path, monkeypatch):
    cfg = config(tmp_path); calls = fake_pipeline(monkeypatch)
    first = invoke(tmp_path, cfg)
    second = invoke(tmp_path, cfg, force=True)
    assert second["run_id"] != first["run_id"] and len(calls) == 8
    assert Path(first["report"]["directory"]).exists()


def test_before_21_no_calendar_quotes_or_model(tmp_path, monkeypatch):
    cfg = config(tmp_path); calls = fake_pipeline(monkeypatch)
    result = invoke(tmp_path, cfg, now=datetime.fromisoformat("2026-09-11T20:59:00+08:00"))
    assert result["status"] == "not_due" and not calls


@pytest.mark.parametrize("status", ["non_trading_day", "universe_blocked"])
def test_calendar_or_universe_stop_never_fills_an_empty_success(tmp_path, monkeypatch, status):
    cfg = config(tmp_path); calls = fake_pipeline(monkeypatch)
    monkeypatch.setattr(daily, "_refresh", lambda *args: {"status": status, "universe_verified": False})
    result = invoke(tmp_path, cfg)
    assert result["status"] == status and not calls and "report" not in result


def test_model_failure_still_publishes_local_pass_list(tmp_path, monkeypatch):
    cfg = config(tmp_path); fake_pipeline(monkeypatch, [("pass", "pass")], "network_error")
    result = invoke(tmp_path, cfg)
    assert result["generation_status"] == "ok" and result["status"] == "partial"
    assert read_observation_report(tmp_path / "outputs", result["report"]["directory"])["report"]["counts"]["observation_count"] == 1


def test_bad_model_settings_are_safe_optional_failure(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    import ashare_daily.research.model_settings as settings
    monkeypatch.setattr(settings, "load_model_settings", lambda *a, **k: (_ for _ in ()).throw(ValueError("private-secret-value")))
    result = daily._context(tmp_path, cfg, {"query_start_at": "2026-09-08T00:00:00+08:00", "cutoff_at": STAMP},
        "test", tmp_path / "context", False)
    assert result["status"] == "context_unavailable" and "private-secret-value" not in json.dumps(result)


def test_real_entrypoint_routes_new_config_and_keeps_legacy_scheduled_block(tmp_path):
    config(tmp_path)
    result = run_daily(project=tmp_path, config_path="config/sector_observation_daily.json", dry_run=True, scheduled=True)
    assert result["scheduled_entry_enabled"] is True
    old = run_daily(project=tmp_path, config_path="config/sector_first_daily.json", dry_run=True, scheduled=True)
    assert old["status"] == "configuration_failed"


def test_viewer_renders_new_report_without_network(tmp_path):
    from test_m4_viewer import app
    publish(tmp_path, [("pass", "pass"), ("pass", "pending")])
    at = app(tmp_path / "outputs").run(timeout=20)
    assert not at.exception
    content = "\n".join(str(item.value) for item in at.markdown)
    assert "公司公告" in content
    assert at.radio(key="observation_view").value == "今日总览"
    assert any("正式研究候选" == item.label for item in at.metric)
