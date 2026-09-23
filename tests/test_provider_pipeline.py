"""Provider fallback through the actual F2 checkpoint/storage pipeline, offline."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from ashare_daily.market_pipeline import load_market_config, quality_report, run_market
from ashare_daily.providers.eastmoney import EastMoneyProvider
from test_f2_market_pipeline import DAY, PREVIOUS, ROOT, Source, calendar, directory_hashes, setup_project


def policy(tmp_path, *, enabled=False):
    path = tmp_path / "market.json"
    config = json.loads(path.read_text("utf-8"))
    template = json.loads((ROOT / "config/sse_szse_market_providers.json").read_text("utf-8"))
    config["config_version"] = "offline-providers-v1"
    config["provider_routing"] = deepcopy(template["provider_routing"])
    if enabled:
        config["provider_routing"]["eastmoney"].update(enabled=True, permission_status="approved",
            permission_basis="offline fixture permission only; not a live authorization", permitted_storage=True, permitted_automated_access=True)
    path.write_text(json.dumps(config), encoding="utf-8")
    return config


class HTTP:
    def __init__(self):
        self.calls = []

    def __call__(self, url, parameters):
        self.calls.append((url, deepcopy(parameters)))
        market, code = parameters["secid"].split(".")
        lines = [",".join((day, "10", "10", "11", "9", "100", "100000", "20", "0", "0", "2"))
                 for day in (PREVIOUS, DAY) if parameters["beg"] <= day.replace("-", "") <= parameters["end"]]
        payload = {"rc": 0, "data": {"code": code, "market": int(market), "klines": lines}}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return {"schema_version": "bounded-http-response-v1", "request": {"url": url, "parameters": parameters},
            "http_status": 200, "body_base64": base64.b64encode(body).decode(), "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_complete": True, "fetched_at": "2026-09-10T22:00:00+08:00", "provenance_mode": "offline_test",
            "verification_kind": "offline_test", "ok": True, "status": "ok", "error_code": "0", "error_msg": "",
            "metrics": {"requests": 1, "retries": 0, "elapsed_seconds": .01, "verification_kind": "offline_test"}}


def run(tmp_path, *, source=None, fallback=None, **kwargs):
    return run_market(project_root=tmp_path, config_path="market.json", target_date=DAY,
        mode="offline_test", client=source or Source(), calendar_resolver=calendar, fallback_provider=fallback, **kwargs)


def test_new_config_is_explicit_and_default_does_not_change_old_resume_hash():
    current = load_market_config(ROOT, "config/sse_szse_market.json")
    new = load_market_config(ROOT, "config/sse_szse_market_providers.json")
    assert "provider_routing" not in current and current["config_version"] == "sse-szse-f2-v1"
    assert new["config_version"] != current["config_version"]
    assert new["provider_routing"]["order"] == ["baostock", "eastmoney"]
    assert new["provider_routing"]["eastmoney"]["enabled"] is False
    for key in ("target_trading_days", "max_attempts", "timeout_seconds", "pause_seconds", "max_run_seconds", "model_calls"):
        assert current[key] == new[key]


def test_disabled_fallback_produces_permission_evidence_with_zero_website_requests(tmp_path):
    setup_project(tmp_path)
    policy(tmp_path)
    result, code = run(tmp_path, source=Source(fail_code="sh.900000"))
    assert code == 2 and result["denominator"] == 4 and not result["market_complete"]
    assert result["metrics"]["fallback_attempts"] == 0
    assert result["metrics"]["provider_metrics"]["eastmoney"]["requests"] == 0
    log = json.loads(Path(result["provider_log_path"]).read_text("utf-8"))
    assert any(e.get("source_status") == "permission_required" for e in log["events"])
    assert result["totals"]["missing_target"] == 1


def test_fallback_saves_real_contract_facts_but_unknown_status_still_blocks_completion(tmp_path):
    setup_project(tmp_path)
    config = policy(tmp_path, enabled=True)
    transport = HTTP()
    em = EastMoneyProvider(permission=config["provider_routing"]["eastmoney"], mode="offline_test", transport=transport)
    result, code = run(tmp_path, source=Source(fail_code="sh.900000"), fallback=em)
    assert code == 2 and not result["structural_market_complete"] and result["denominator"] == 4
    assert len(transport.calls) == 2
    assert result["metrics"]["requests"] == 10
    assert result["metrics"]["fallback_selected"] == 2 and result["metrics"]["fallback_verified"] == 0
    assert result["totals"]["fact_target_present"] == 4 and result["totals"]["valid_target"] == 3
    assert result["totals"]["history_unknown_trading_status_dates"] == 2
    assert result["totals"]["adjustment_ready"] == 3
    assert result["research_ready"] is False and result["model_calls"] == 0
    report = quality_report(tmp_path, "market.json", result["job_id"], mode="offline_test")
    detail = next(r for r in report["details"] if r["code"] == "900000")
    assert detail["raw_providers"] == ["eastmoney"] and not detail["raw_adjustment_source_aligned"]
    assert len(detail["task_issues"]) == 2
    assert all(len(t["provider_attempts"]) == 2 for t in detail["task_issues"])
    with sqlite3.connect(tmp_path / "offline/jobs.sqlite3") as db:
        task = json.loads(db.execute("SELECT result_json FROM f2_tasks ORDER BY position LIMIT 1").fetchone()[0])
    assert task["provenance"]["source_business_date"] is None
    assert task["provenance"]["fallback_level"] == 1
    assert all(ref["provider"] == "eastmoney" for ref in task["saved"]["raw_versions"].values())
    for candidate in task["provider_attempts"]:
        body = Path(candidate["response_path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == candidate["source_file_hash"]
    with sqlite3.connect(tmp_path / "offline/market.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM f2_bar_versions WHERE provider='eastmoney'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM f2_bar_versions WHERE provider='baostock'").fetchone()[0] == 6


def test_old_job_cannot_silently_switch_to_new_provider_policy(tmp_path):
    setup_project(tmp_path)
    old, _ = run(tmp_path, source=Source(interrupt_at=2))
    frozen = Path(old["plan_path"]).read_bytes()
    policy(tmp_path)
    source = Source()
    with pytest.raises(ValueError, match="config|policy"):
        run(tmp_path, source=source, operation="resume", job_id=old["job_id"])
    assert not source.calls and Path(old["plan_path"]).read_bytes() == frozen


def test_successful_new_policy_replay_is_idempotent_and_new_refs_are_provider_scoped(tmp_path):
    setup_project(tmp_path)
    policy(tmp_path)
    first, code = run(tmp_path)
    assert code == 0 and first["provider_routing_version"] == "f2-provider-routing-v1"
    with sqlite3.connect(tmp_path / "offline/market.sqlite3") as db:
        facts_before = db.execute("SELECT * FROM f2_bar_versions").fetchall()
    source = Source()
    repeated, code = run(tmp_path, source=source, operation="resume", job_id=first["job_id"])
    assert code == 0 and not source.calls and repeated["metrics"]["requests"] == 0
    with sqlite3.connect(tmp_path / "offline/market.sqlite3") as db:
        assert db.execute("SELECT * FROM f2_bar_versions").fetchall() == facts_before
    before = directory_hashes(tmp_path)
    quality_report(tmp_path, "market.json", first["job_id"], mode="offline_test")
    assert directory_hashes(tmp_path) == before


def test_interruption_records_started_request_without_claiming_zero_network_activity(tmp_path):
    setup_project(tmp_path)
    policy(tmp_path)
    result, code = run(tmp_path, source=Source(interrupt_at=1))
    assert code == 2 and result["metrics"]["logical_requests_started"] == 1
    assert result["metrics"]["requests"] == 0 and "running" not in result["tasks"]
    log = json.loads(Path(result["provider_log_path"]).read_text("utf-8"))
    interrupted = next(e for e in log["events"] if e["event"] == "request_interrupted")
    assert interrupted["network_activity"] == "unknown_no_completed_response"
    source = Source()
    resumed, code = run(tmp_path, source=source, operation="resume", job_id=result["job_id"])
    assert code == 0 and len(source.calls) == 8 and resumed["model_calls"] == 0


def test_fake_fallback_cannot_enter_research_even_if_local_test_permission_says_approved(tmp_path):
    setup_project(tmp_path)
    config = policy(tmp_path, enabled=True)
    em = EastMoneyProvider(permission=config["provider_routing"]["eastmoney"], mode="offline_test", transport=HTTP())
    with pytest.raises(ValueError, match="injected"):
        run_market(project_root=tmp_path, config_path="market.json", target_date=DAY, fallback_provider=em)
    assert not (tmp_path / "offline/market.sqlite3").exists()
