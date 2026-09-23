"""M4 orchestration tests use explicit OFFLINE fixtures in temporary projects only.

FakeServices never contacts a provider and cannot establish live acceptance.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from ashare_daily.operations.daily import run_daily, resolve_times, DailyConfig, input_state, read_runs, publish_report, valid_report, LiveServices
from ashare_daily.market_schemas import SHANGHAI


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 9, 22, 0, tzinfo=SHANGHAI)
T = date(2026, 9, 8)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "OFFLINE-m4-project"
    for relative in ("config/m4.json", "config/m21_100.json", "config/samples/m21_100.json", "config/m3_smoke.json", "config/m3_daily.json", "config/m3_sources.json", "config/eligibility_sources.json"):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    (project / ".env").write_text("MODEL_BASE_URL=https://offline.invalid/v1\nMODEL_NAME=OFFLINE-model\nMODEL_API_KEY=sk-OFFLINE-TEST-ONLY\n", encoding="utf-8")
    return project


class FakeServices:
    verification_kind = "offline_test"

    def __init__(self, *, calendar=True, actual_date=None, message_status="partial", model_status="ok", fail_stage=None):
        self.calendar = calendar
        self.actual_date = actual_date
        self.message_status = message_status
        self.model_status = model_status
        self.fail_stage = fail_stage
        self.calls = []
        self.model_calls = 0

    def check(self, stage):
        self.calls.append(stage)
        if self.fail_stage == stage:
            raise RuntimeError("OFFLINE synthetic failure at " + stage)

    def ensure_calendar(self, database, target, config, work):
        self.check("calendar")
        return self.calendar

    def collect_market(self, project, config, sample, target, work):
        self.check("market")
        return {"status": "partial", "run_directory": str(work / "OFFLINE-market")}

    def freeze_market(self, project, config, target, collection, work):
        self.check("freeze")
        return {"status": "partial", "actual_market_date": (self.actual_date or target).isoformat(),
                "trade_date": target.isoformat(), "snapshot_path": str(work / "OFFLINE-market-input.json")}

    def materials(self, project, config, symbols, times, work):
        self.check("materials")
        return {"status": self.message_status, "source_health": [{"source_id": "OFFLINE-source", "status": self.message_status}],
                "bundle_file": str(work / "OFFLINE-materials.json")}

    def research(self, project, config, market, materials, times, settings, guard, work, skip_model):
        self.check("research")
        status = "skipped" if skip_model else self.model_status
        if status == "ok":
            token = guard.before_attempt()
            guard.after_attempt(token, {"status": "ok", "usage": {"total_tokens": 12}})
            self.model_calls += 1
        out = work / "OFFLINE-research" / str(len(self.calls))
        report = {"verification_kind": "offline_test", "notice": "OFFLINE TEST FIXTURE: no real research",
                  "model_run": {"call_count": 1 if status == "ok" else 0, "status": status},
                  "statuses": {"model": status, "market": "partial", "messages": materials["status"], "generation": "ok"},
                  "actual_generated_at": NOW.isoformat()}
        write_json(out / "daily_brief.json", report)
        files = {"daily_brief.json": hashlib.sha256((out / "daily_brief.json").read_bytes()).hexdigest()}
        for name in ("daily_brief.md", "daily_brief.html", "screening_audit.csv", "claim_evidence_audit.csv", "evidence_catalog.json", "input_snapshot.json", "model_responses.json"):
            (out / name).write_text("OFFLINE TEST FIXTURE", encoding="utf-8")
            files[name] = hashlib.sha256((out / name).read_bytes()).hexdigest()
        write_json(out / "manifest.json", {"schema_version": "m3-report-manifest-v1", "files": files})
        result = {"verification_kind": "offline_test", "trade_date": market["trade_date"], "actual_market_date": market["actual_market_date"],
                  "input_snapshot_id": "OFFLINE-snapshot", "run_directory": str(out),
                  "json": str(out / "daily_brief.json"), "html": str(out / "daily_brief.html")}
        write_json(out / "result.json", result)
        return result


def execute(project, services=None, **kwargs):
    return run_daily(project=project, target=T, now=NOW, services=services or FakeServices(), **kwargs)


def test_dry_run_never_calls_services_or_budget(project):
    services = FakeServices(fail_stage="calendar")
    result = execute(project, services, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["generation_status"] == "not_run"
    assert result["external_calls_allowed"] is False
    assert result["calendar_preview"] == "calendar_unknown"
    assert services.calls == []
    assert not (project / "data/operations/runtime.sqlite3").exists()
    assert "sk-OFFLINE" not in Path(result["run_directory"], "result.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("calendar,status,code", [(False, "non_trading_day", 0), (None, "calendar_unavailable", 2)])
def test_calendar_controls_execution_without_weekday_guess(project, calendar, status, code):
    services = FakeServices(calendar=calendar)
    result = execute(project, services)
    assert result["status"] == status
    assert result["exit_code"] == code
    assert result["generation_status"] == "not_run"
    assert services.calls == ["calendar"]
    assert services.model_calls == 0


def test_calendar_error_stops_all_other_calls(project):
    services = FakeServices(fail_stage="calendar")
    result = execute(project, services)
    assert result["status"] == "calendar_unavailable"
    assert services.calls == ["calendar"]


def test_old_market_does_not_publish_today_or_call_model(project):
    services = FakeServices(actual_date=date(2026, 9, 7))
    result = execute(project, services)
    assert result["status"] == "market_stale"
    assert result["actual_market_date"] == "2026-09-07"
    assert services.calls == ["calendar", "market", "freeze"]
    assert not (project / "outputs/research/m4/latest_report.json").exists()


def test_repeated_completed_identity_reuses_without_any_external_stage(project):
    first = FakeServices()
    report = execute(project, first)
    assert report["generation_status"] == "ok"
    assert first.model_calls == 1
    second = FakeServices(fail_stage="calendar")
    result = execute(project, second)
    assert result["status"] == "reused"
    assert result["reused_from"] == report["run_id"]
    assert second.calls == []
    assert result["report"]["run_directory"] == report["report"]["run_directory"]


def test_changed_cutoff_creates_new_version_and_preserves_previous(project):
    old = execute(project, cutoff="2026-09-08T20:00:00+08:00")
    old_json = Path(old["report"]["json"]).read_bytes()
    services = FakeServices()
    newer = execute(project, services, cutoff="2026-09-08T21:00:00+08:00")
    assert newer["status"] == "partial"
    assert newer["task_identity"] != old["task_identity"]
    assert services.model_calls == 1
    assert newer["report"]["run_directory"] != old["report"]["run_directory"]
    assert Path(old["report"]["json"]).read_bytes() == old_json
    assert newer["query_start_at"] == old["cutoff_at"]


@pytest.mark.parametrize("model_status", ["timeout", "rate_limited", "refused", "missing_configuration", "budget_exhausted"])
def test_model_failure_still_publishes_market_only(project, model_status):
    result = execute(project, FakeServices(model_status=model_status))
    assert result["status"] == "market_only"
    assert result["generation_status"] == "ok"
    assert result["exit_code"] == 1
    assert result["module_statuses"]["model"] == model_status
    assert Path(result["report"]["html"]).is_file()


def test_partial_source_is_explicit_and_other_stages_continue(project):
    services = FakeServices(message_status="failed", model_status="no_eligible_evidence")
    result = execute(project, services)
    assert result["generation_status"] == "ok"
    assert result["sources"][0]["status"] == "failed"
    assert result["module_statuses"]["messages"] == "failed"
    assert result["status"] == "market_only"


def test_failed_new_task_retains_last_report_and_failed_record(project):
    success = execute(project)
    latest_path = project / "outputs/research/m4/latest_report.json"
    previous = latest_path.read_bytes()
    failure = execute(project, FakeServices(fail_stage="market"), force=True)
    assert failure["status"] == "failed"
    assert failure["exit_code"] == 2
    assert latest_path.read_bytes() == previous
    assert Path(success["report"]["html"]).exists()
    assert json.loads(Path(failure["run_directory"], "result.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_force_explicitly_produces_a_distinct_version(project):
    old = execute(project)
    new = execute(project, force=True)
    assert old["task_identity"] == new["task_identity"]
    assert old["report"]["run_directory"] != new["report"]["run_directory"]


def test_config_change_prevents_identity_reuse(project):
    old = execute(project)
    path = project / "config/m4.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    content["model_max_calls_per_day"] -= 1
    write_json(path, content)
    new = execute(project)
    assert new["task_identity"] != old["task_identity"]
    assert new["status"] != "reused"


def test_times_convert_foreign_timezone_and_reject_naive_future():
    times = resolve_times(None, None, None, datetime.fromisoformat("2026-09-08T17:00:00-04:00"), DailyConfig(), [])
    assert times["target_trade_date"] == "2026-09-09"
    assert times["cutoff_at"] == "2026-09-09T05:00:00+08:00"
    with pytest.raises(ValueError, match="时区"):
        resolve_times(None, None, None, datetime(2026, 9, 9), DailyConfig(), [])
    with pytest.raises(ValueError, match="未来"):
        resolve_times(date(2026, 9, 10), None, None, NOW, DailyConfig(), [])


def test_offline_input_database_state_changes_prevent_reuse(project):
    database = project / "data/research/market.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE market_metadata(key TEXT,value TEXT)")
        db.execute("INSERT INTO market_metadata VALUES ('verification_kind','offline_test')")
        db.execute("CREATE TABLE daily_bars(symbol TEXT,trade_date TEXT,raw_hash TEXT)")
        db.execute("CREATE TABLE instruments(symbol TEXT,raw_hash TEXT)")
        db.execute("CREATE TABLE trading_calendar(calendar_date TEXT,raw_hash TEXT)")
        db.execute("INSERT INTO daily_bars VALUES ('sh.600000','2026-09-08','OFFLINE-v1')")
    old = execute(project)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE daily_bars SET raw_hash='OFFLINE-v2'")
    new = execute(project)
    assert new["input_state"] != old["input_state"]
    assert new["status"] != "reused"


def test_scheduled_before_21_is_not_due_and_does_not_collect(project):
    services = FakeServices()
    result = run_daily(project=project, now=NOW.replace(hour=20), scheduled=True, services=services)
    assert result["status"] == "not_due"
    assert services.model_calls == 0
    assert "market" not in services.calls


def test_early_scheduled_previews_do_not_exhaust_later_attempts(project):
    for hour in (18, 19):
        result = run_daily(project=project, now=NOW.replace(hour=hour), scheduled=True, services=FakeServices())
        assert result["status"] == "not_due"
    completed = run_daily(project=project, now=NOW, scheduled=True, services=FakeServices())
    assert completed["generation_status"] == "ok"


def test_two_failed_automatic_attempts_stop_the_third_before_network(project):
    for _ in range(2):
        result = run_daily(project=project, now=NOW, scheduled=True, services=FakeServices(calendar=None))
        assert result["status"] == "calendar_unavailable"
    blocked = FakeServices(fail_stage="calendar")
    result = run_daily(project=project, now=NOW, scheduled=True, services=blocked)
    assert result["status"] == "catchup_limit"
    assert result["exit_code"] == 2
    assert blocked.calls == []


def test_configuration_failure_is_saved_without_secret_values(project):
    (project / ".env").write_text("MODEL_BASE_URL=sk-OFFLINE-CONFIG-INVALID\n", encoding="utf-8")
    services = FakeServices(fail_stage="calendar")
    result = execute(project, services)
    assert result["status"] == "configuration_failed"
    assert result["exit_code"] == 2
    assert result["generation_status"] == "not_run"
    assert services.calls == []
    assert "sk-OFFLINE" not in Path(result["run_directory"], "result.json").read_text(encoding="utf-8")


def test_messages_exception_uses_failure_catalogue_and_still_generates(project, monkeypatch):
    def failed_materials(self, project, config, times, work):
        return {"status": "failed", "source_health": [{"source_id": "OFFLINE-source", "status": "failed", "reason": "OFFLINE exception"}],
                "bundle_file": str(work / "OFFLINE-failure-catalogue.json")}
    monkeypatch.setattr(LiveServices, "failed_materials", failed_materials)
    result = execute(project, FakeServices(fail_stage="materials", model_status="no_eligible_evidence"))
    assert result["generation_status"] == "ok"
    assert result["status"] == "market_only"
    assert result["sources"][0]["status"] == "failed"


@pytest.mark.parametrize("published,precision", [("2026-09-08T22:00:00+08:00", "datetime"), ("2026-09-08", "date")])
def test_later_same_day_evidence_does_not_change_earlier_input_fingerprint(project, published, precision):
    database = project / "data/research/market.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE daily_bars(symbol TEXT,trade_date TEXT,raw_hash TEXT)")
        db.execute("CREATE TABLE instruments(symbol TEXT,raw_hash TEXT)")
        db.execute("CREATE TABLE trading_calendar(calendar_date TEXT,raw_hash TEXT)")
        db.execute("CREATE TABLE m3_evidence(evidence_id TEXT,payload_json TEXT)")
    args = (database, project / "data/research/m21_adjusted", ["sh.600000"], T, "2026-09-08T21:00:00+08:00")
    old = input_state(*args)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO m3_evidence VALUES (?,?)", ("OFFLINE-future", json.dumps({"evidence_id": "OFFLINE-future", "published_at": published, "publication_precision": precision})))
    assert input_state(*args) == old


def test_valid_json_but_wrong_shape_run_does_not_block_remaining_history(project):
    root = project / "outputs/research/m4"
    for name, value in (("bad-list", []), ("bad-null", None), ("bad-string", "OFFLINE"), ("good", {"started_at": NOW.isoformat(), "run_id": "good"})):
        write_json(root / "runs" / name / "result.json", value)
    assert [r["run_id"] for r in read_runs(root)] == ["good"]


def test_valid_report_requires_expected_registered_artifacts(project):
    directory = project / "OFFLINE-malformed-report"
    write_json(directory / "unrelated.json", {"OFFLINE": True})
    write_json(directory / "manifest.json", {"files": {"unrelated.json": hashlib.sha256((directory / "unrelated.json").read_bytes()).hexdigest()}})
    assert valid_report({"run_directory": str(directory)}) is False


def test_publish_does_not_copy_unregistered_files(project):
    result = execute(project)
    source = result["report"]
    directory = Path(source["run_directory"])
    (directory / ".env").write_text("OFFLINE-UNREGISTERED-SECRET-FILE", encoding="utf-8")
    copied = publish_report(source, project / "OFFLINE-second-publication", {"run_id": "OFFLINE-publish-copy"})
    assert not (Path(copied["run_directory"]) / ".env").exists()
    assert Path(copied["html"]).is_file()


def test_interrupted_recovery_uses_read_location_not_stored_arbitrary_path(project):
    actual = project / "outputs/research/m4/runs/OFFLINE-interrupted/result.json"
    unrelated = project / "OFFLINE-unrelated-directory/result.json"
    write_json(unrelated, {"must_remain": "OFFLINE protected content"})
    original = unrelated.read_bytes()
    write_json(actual, {"run_id": "OFFLINE-interrupted", "started_at": NOW.isoformat(), "status": "running",
                       "run_directory": str(unrelated.parent), "generation_status": "not_run"})
    result = execute(project, FakeServices(calendar=False))
    assert result["status"] == "non_trading_day"
    assert unrelated.read_bytes() == original
    recovered = json.loads(actual.read_text(encoding="utf-8"))
    assert recovered["status"] == "interrupted"
    assert Path(recovered["run_directory"]) == actual.parent
