"""OFFLINE routing/lock/UI tests. Fixtures cannot establish online acceptance."""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType

import pytest

from ashare_daily.cli import main
from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.operations.daily import atomic_json, read_runs, run_daily
from ashare_daily.operations.lock import AlreadyRunning, ProcessLock
from ashare_daily.viewer import scope_label, status_label, unresolved_daily_failure

ROOT = Path(__file__).resolve().parents[1]
TARGET = date(2026, 9, 11)
NOW = datetime(2026, 9, 11, 22, tzinfo=SHANGHAI)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "OFFLINE-f2-entrypoints"
    (root / "config").mkdir(parents=True)
    for name in ("sse_szse_daily.json", "sse_szse_universe.json", "sse_szse_market.json",
                 "full_market_daily.json", "universe.json"):
        shutil.copyfile(ROOT / "config" / name, root / "config" / name)
    import ashare_daily.operations.daily as daily
    monkeypatch.setattr(daily, "PROJECT", root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def market(monkeypatch):
    """Only test entrypoint dispatch; no actual collector is substituted in research."""
    module = ModuleType("ashare_daily.market_pipeline")
    module.run_market = lambda **kwargs: pytest.fail("unexpected F2 collection")
    module.quality_report = lambda *args, **kwargs: pytest.fail("unexpected quality read")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    import ashare_daily
    monkeypatch.setattr(ashare_daily, "market_pipeline", module, raising=False)
    return module


def universe_result(project, **changes):
    result = {"status": "complete", "scope": "sse_szse_a", "universe_verified": True,
        "collection_ready": True, "research_ready": False, "verification_kind": "offline_test",
        "calendar": {"status": "verified"}, "snapshot_id": "OFFLINE-ENTRYPOINT-SNAPSHOT",
        "snapshot_path": str(project / "OFFLINE-snapshot.json"), "ordinary_a_count": 5218,
        "resolved_trade_date": TARGET.isoformat(),
        "board_counts": {"sse_main": 1701, "szse_main": 1494, "chinext": 1407, "star": 616}}
    result.update(changes)
    return result


def mock_universe(monkeypatch, result):
    import ashare_daily.universe_service as service
    calls = []
    monkeypatch.setattr(service, "sync_date", lambda **kwargs: calls.append(kwargs) or result)
    return calls


def archived(project, identity, *, scope="all_a", **values):
    path = project / "outputs/research/m4/runs" / identity / "result.json"
    atomic_json(path, {"run_id": identity, "scope": scope, "started_at": NOW.isoformat(),
        "target_trade_date": TARGET.isoformat(), "status": "running", "generation_status": "not_run", **values})
    return path


@pytest.mark.parametrize("operation", ["bootstrap", "update"])
def test_market_cli_defaults_date_snapshot_and_existing_lock(project, market, operation, capsys):
    calls = []
    def collect(**kwargs):
        with pytest.raises(AlreadyRunning):
            with ProcessLock(project / "data/operations/daily.lock", "OFFLINE-probe"):
                pass
        calls.append(kwargs)
        return {"status": "f2_partial", "verification_kind": "offline_test"}, 2
    market.run_market = collect
    assert main(["market", operation, "--date", str(TARGET), "--universe-snapshot", "OFFLINE-snapshot.json",
                 "--max-seconds", "1.5"]) == 2
    assert calls == [{"project_root": project, "config_path": "config/sse_szse_market.json", "operation": operation,
        "target_date": TARGET, "universe_snapshot_path": Path("OFFLINE-snapshot.json"), "max_seconds": 1.5}]
    assert json.loads(capsys.readouterr().out)["status"] == "f2_partial"


def test_market_resume_uses_saved_job_instead_of_new_date(project, market):
    calls = []
    market.run_market = lambda **kwargs: calls.append(kwargs) or ({"status": "f2_complete"}, 0)
    assert main(["market", "resume", "--job", "OFFLINE-JOB", "--config", "config/custom.json"]) == 0
    assert calls == [{"project_root": project, "config_path": "config/custom.json", "operation": "resume",
                      "job_id": "OFFLINE-JOB", "max_seconds": None}]


def test_daily_cli_passes_invocation_only_market_limit(project, monkeypatch):
    import ashare_daily.operations.daily as daily
    calls = []
    monkeypatch.setattr(daily, "run_daily", lambda **kwargs: calls.append(kwargs) or {"exit_code": 0})
    assert main(["run-daily", "--date", str(TARGET), "--market-max-seconds", "30"]) == 0
    assert calls[0]["f2_max_seconds"] == 30
    assert calls[0]["config_path"] == "config/sse_szse_daily.json"


@pytest.mark.parametrize("seconds", ["0", "-1", "14401", "inf", "nan"])
def test_daily_cli_rejects_invalid_f2_runtime_limit_before_work(project, seconds):
    with pytest.raises(SystemExit) as error:
        main(["run-daily", "--market-max-seconds", seconds])
    assert error.value.code == 2
    assert not (project / "data").exists()
    assert not (project / "outputs").exists()


def test_daily_cli_sample_does_not_accept_market_runtime_argument(project):
    with pytest.raises(SystemExit) as error:
        main(["run-daily", "--sample", "--market-max-seconds", "30"])
    assert error.value.code == 2
    assert not (project / "data").exists()


def test_market_cli_live_lock_prevents_collector(project, market):
    with ProcessLock(project / "data/operations/daily.lock", "OFFLINE-other-job"):
        assert main(["market", "bootstrap", "--date", str(TARGET)]) == 3


@pytest.mark.parametrize("seconds", ["0", "-1", "inf", "nan", "invalid"])
def test_cli_rejects_unbounded_or_invalid_seconds_before_work(project, market, seconds):
    with pytest.raises(SystemExit) as error:
        main(["market", "update", "--date", str(TARGET), "--max-seconds", seconds])
    assert error.value.code == 2
    assert not (project / "data").exists()


def test_quality_command_can_read_while_another_job_holds_lock(project, market, capsys):
    calls = []
    market.quality_report = lambda *args: calls.append(args) or {"status": "f2_partial", "job_id": "OFFLINE-JOB"}
    lock_path = project / "data/operations/daily.lock"
    with ProcessLock(lock_path, "OFFLINE-collection") as held:
        # Windows forbids reading byte zero while its OS lock is held.
        # Check the lock identity separately and hash every business file.
        before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file() and p != lock_path}
        metadata = dict(held.metadata)
        assert main(["quality", "report", "--job", "OFFLINE-JOB"]) == 0
        assert before == {p: p.read_bytes() for p in project.rglob("*") if p.is_file() and p != lock_path}
        assert metadata == held.metadata
    assert calls == [(project, "config/sse_szse_market.json", "OFFLINE-JOB")]
    assert json.loads(capsys.readouterr().out)["status"] == "f2_partial"


def test_actual_quality_missing_job_does_not_create_database_or_archive(project, capsys):
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert main(["quality", "report", "--job", "OFFLINE-NOT-AN-EXISTING-JOB"]) == 1
    assert "checkpoint database does not exist" in capsys.readouterr().err
    assert before == {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert not (project / "data").exists()


@pytest.mark.parametrize("config", [None, "config/universe.json"])
def test_universe_cli_new_default_and_explicit_legacy_config(project, monkeypatch, config):
    calls = mock_universe(monkeypatch, {"status": "blocked", "universe_verified": False})
    assert main(["universe", "sync", "--date", str(TARGET)] + (["--config", config] if config else [])) == 2
    assert calls[0]["config_path"] == (config or "config/sse_szse_universe.json")


@pytest.mark.parametrize("status,code", [("f2_complete", 0), ("f2_partial", 2), ("f2_blocked", 2)])
def test_daily_f2_status_is_separate_from_report_generation(project, market, monkeypatch, status, code):
    calls = mock_universe(monkeypatch, universe_result(project))
    arguments = []
    def collect(**kwargs):
        with pytest.raises(AlreadyRunning):
            with ProcessLock(project / "data/operations/daily.lock", "OFFLINE-lock-probe"):
                pass
        arguments.append(kwargs)
        return {"status": status, "job_id": "OFFLINE-JOB", "output_directory": "OFFLINE-output",
                "quality_path": "OFFLINE-quality.json", "research_ready": False}, code
    market.run_market = collect
    result = run_daily(project=project, target=TARGET, now=NOW)
    assert result["status"] == status and result["exit_code"] == code
    assert result["scope"] == "sse_szse_a" and result["collection_ready"] is True
    assert result["research_ready"] is False and result["generation_status"] == "not_run"
    assert result["model_summary"] == {"call_count": 0, "status": "not_run"}
    assert result["module_statuses"]["market"] == status
    assert result["module_statuses"]["screening"] == "not_run_F3"
    assert result["module_statuses"]["research"] == "not_run_F4"
    assert result["module_statuses"]["publish"] == "not_run"
    assert result["market_job_id"] == "OFFLINE-JOB"
    assert result["config_versions"]["config/sse_szse_market.json"] == hashlib.sha256(
        (project / "config/sse_szse_market.json").read_bytes()).hexdigest()
    assert len(calls) == 1 and arguments == [{"project_root": project, "config_path": "config/sse_szse_market.json",
        "target_date": TARGET, "operation": "update", "universe_snapshot_path": str(project / "OFFLINE-snapshot.json"),
        "max_seconds": None}]
    assert not (project / "outputs/research/m4/latest_report.json").exists()
    assert not (project / "data/operations/runtime.sqlite3").exists()


def test_daily_market_limit_is_recorded_and_applies_only_to_f2_step(project, market, monkeypatch):
    calls = mock_universe(monkeypatch, universe_result(project))
    arguments = []
    market.run_market = lambda **kwargs: arguments.append(kwargs) or ({"status": "f2_partial"}, 2)
    before = {p: p.read_bytes() for p in (project / "config").glob("*.json")}
    result = run_daily(project=project, target=TARGET, now=NOW, f2_max_seconds=30)
    assert result["status"] == "f2_partial" and result["f2_runtime_limit_seconds"] == 30
    assert len(arguments) == 1 and arguments[0]["max_seconds"] == 30
    assert len(calls) == 1 and "max_seconds" not in calls[0] and "f2_max_seconds" not in calls[0]
    assert before == {p: p.read_bytes() for p in (project / "config").glob("*.json")}
    assert result["model_summary"]["call_count"] == 0


@pytest.mark.parametrize("value", [0, -1, 14401, float("inf"), float("nan"), True, "30"])
def test_python_daily_rejects_invalid_runtime_limit_without_collecting(project, market, value):
    result = run_daily(project=project, target=TARGET, now=NOW, f2_max_seconds=value)
    assert result["status"] == "configuration_failed" and "14400" in result["failure_reason"]
    assert not (project / "data").exists()


@pytest.mark.parametrize("config", ["config/full_market_daily.json", "config/OFFLINE-sample.json"])
def test_runtime_override_requires_an_enabled_f2_step(project, market, config):
    if config.endswith("OFFLINE-sample.json"):
        atomic_json(project / config, {"scope_mode": "sample"})
    result = run_daily(project=project, config_path=config, target=TARGET, now=NOW, f2_max_seconds=30)
    assert result["status"] == "configuration_failed"
    assert "未启用F2行情" in result["failure_reason"]
    assert not (project / "data").exists()


@pytest.mark.parametrize("changes,expected", [
    ({"universe_verified": False}, "universe_blocked"),
    ({"collection_ready": False}, "universe_blocked"),
    ({"scope": "all_a"}, "universe_blocked"),
    ({"snapshot_path": None}, "universe_blocked"),
    ({"resolved_trade_date": "2026-09-10"}, "universe_blocked"),
    ({"calendar": {"status": "calendar_unverified"}}, "calendar_unverified"),
    ({"status": "non_trading_day"}, "non_trading_day"),
])
def test_daily_rejects_incomplete_or_wrong_scope_prerequisites(project, market, monkeypatch, changes, expected):
    mock_universe(monkeypatch, universe_result(project, **changes))
    result = run_daily(project=project, target=TARGET, now=NOW)
    assert result["status"] == expected
    assert result["module_statuses"]["market"] == "not_run_F2"


def test_old_explicit_all_a_still_stops_at_f2_pending(project, market, monkeypatch):
    mock_universe(monkeypatch, universe_result(project, scope="all_a"))
    result = run_daily(project=project, config_path="config/full_market_daily.json", target=TARGET, now=NOW)
    assert result["status"] == "f2_pending" and result["scope"] == "all_a"
    assert "bse" in result["required_boards"]


def test_daily_scope_mismatch_is_configuration_failure(project, market):
    path = project / "config/sse_szse_daily.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["universe_config"] = "config/universe.json"
    atomic_json(path, config)
    result = run_daily(project=project, target=TARGET, now=NOW)
    assert result["status"] == "configuration_failed"
    assert not (project / "data").exists()


def test_scope_change_preserves_other_scope_recovery_and_date_baseline(project, market, monkeypatch):
    other = [archived(project, "OFFLINE-old-all-a"), archived(project, "OFFLINE-old-sample", scope="sample"),
        archived(project, "OFFLINE-prior-report", status="partial", generation_status="ok",
                 cutoff_at="2026-09-10T21:00:00+08:00")]
    before = {p: p.read_bytes() for p in other}
    same = archived(project, "OFFLINE-interrupted-sse", scope="sse_szse_a")
    mock_universe(monkeypatch, universe_result(project, universe_verified=False))
    result = run_daily(project=project, target=TARGET, now=NOW)
    assert result["query_start_at"] == "2026-09-08T00:00:00+08:00"
    assert before == {p: p.read_bytes() for p in other}
    assert json.loads(same.read_text(encoding="utf-8"))["status"] == "interrupted"
    assert len(read_runs(project / "outputs/research/m4", scope="sse_szse_a")) == 2


def test_scope_change_does_not_increase_global_scheduled_attempt_limit(project, market, monkeypatch):
    for identity, scope in [("OFFLINE-failed-all", "all_a"), ("OFFLINE-failed-sample", "sample")]:
        archived(project, identity, scope=scope, status="failed", trigger_kind="scheduled")
    calls = mock_universe(monkeypatch, universe_result(project))
    result = run_daily(project=project, target=TARGET, now=NOW, scheduled=True)
    assert result["status"] == "catchup_limit" and not calls


@pytest.mark.parametrize("kwargs", [{"dry_run": True}, {"now": NOW.replace(hour=15)},
                                  {"now": NOW.replace(hour=20), "scheduled": True}])
def test_f2_entrypoint_retains_dry_run_and_time_constraints(project, market, monkeypatch, kwargs):
    calls = mock_universe(monkeypatch, universe_result(project))
    result = run_daily(project=project, target=TARGET, **({"now": NOW} | kwargs))
    assert result["status"] in {"dry_run", "not_due"} and not calls


def test_viewer_scope_and_stage_labels_keep_f2_distinct_from_daily_report():
    assert scope_label("sse_szse_a") == "沪深A股全市场，暂不含北交所"
    assert "包含北交所" in scope_label("all_a")
    assert "尚未筛选、研究或生成新日报" in status_label("f2_complete")
    assert "缺口" in status_label("f2_partial")
    rows = [{"run_id": "OFFLINE-old", "scope": "all_a", "target_trade_date": str(TARGET),
             "status": "universe_blocked", "exit_code": 2, "started_at": "2026-09-11T22:00:00+08:00"},
            {"run_id": "OFFLINE-new", "scope": "sse_szse_a", "target_trade_date": str(TARGET),
             "status": "f2_partial", "exit_code": 2, "started_at": "2026-09-11T21:00:00+08:00"}]
    assert unresolved_daily_failure(rows, day=str(TARGET), scope="sse_szse_a")["run_id"] == "OFFLINE-new"
    assert unresolved_daily_failure(rows, day=str(TARGET), scope="all_a")["run_id"] == "OFFLINE-old"
    rows[1].update(status="f2_complete", exit_code=0, generation_status="not_run")
    assert unresolved_daily_failure(rows, day=str(TARGET), scope="sse_szse_a") is None
    assert unresolved_daily_failure(rows, day=str(TARGET), scope="all_a")["run_id"] == "OFFLINE-old"
