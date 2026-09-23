"""Manual local sector publication dispatch; no real source calls in these tests."""
import json
import sys
from types import ModuleType

import pytest

from ashare_daily.cli import main
from ashare_daily.operations.lock import AlreadyRunning, ProcessLock


def arguments(*extra):
    return ["sector", "publish-report", "--selection", "validation-sector-frozen", *extra]


def install(monkeypatch, tmp_path, function):
    module = ModuleType("ashare_daily.reports.sector_research")
    module.publish_sector_report = function
    monkeypatch.setitem(sys.modules, "ashare_daily.reports.sector_research", module)
    monkeypatch.setattr("ashare_daily.operations.daily.PROJECT", tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("publication must not call acquisition, screening or run-daily")
    monkeypatch.setattr("ashare_daily.operations.daily.run_daily", forbidden)
    monkeypatch.setattr("ashare_daily.sector_pipeline.run_sector", forbidden)
    monkeypatch.setattr("ashare_daily.sector_f4s1.run_f4s1", forbidden)


def test_manual_report_defaults_hold_shared_daily_lock(monkeypatch, tmp_path, capsys):
    calls = []
    def publish(root, **kwargs):
        assert root == tmp_path
        calls.append(kwargs)
        with pytest.raises(AlreadyRunning):
            with ProcessLock(root / "data/operations/daily.lock", "nested-verification"):
                pytest.fail("publication dispatch did not hold shared lock")
        return {"status": "local_report_generated", "network_requests": 0, "model_calls": 0}
    install(monkeypatch, tmp_path, publish)
    assert main(arguments()) == 0
    assert calls == [{"selection_id": "validation-sector-frozen", "revision": None,
        "validation_config_path": "config/sector_validation.json", "dry_run": False, "report_id": None}]
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "local_report_generated", "network_requests": 0, "model_calls": 0, "exit_code": 0}


def test_existing_report_and_revision_are_forwarded_without_recomputing(monkeypatch, tmp_path):
    calls = []
    def publish(root, **kwargs):
        calls.append(kwargs)
        return {"status": "existing_report_verified", "reused_report": True}
    install(monkeypatch, tmp_path, publish)
    assert main(arguments("--revision", "f4s1-frozen-version", "--report-id", "sector-report-original",
        "--validation-config", "config/custom-validation.json")) == 0
    assert calls == [{"selection_id": "validation-sector-frozen", "revision": "f4s1-frozen-version",
        "validation_config_path": "config/custom-validation.json", "dry_run": False, "report_id": "sector-report-original"}]


def test_dry_run_creates_no_lock_or_artifact(monkeypatch, tmp_path):
    def publish(root, **kwargs):
        assert kwargs["dry_run"] is True
        assert not list(root.iterdir())
        return {"status": "dry_run", "network_requests": 0, "database_writes": 0, "model_calls": 0}
    install(monkeypatch, tmp_path, publish)
    assert main(arguments("--dry-run")) == 0
    assert not list(tmp_path.iterdir())


def test_existing_daily_lock_blocks_publication(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("report ran while daily lock was held")
    install(monkeypatch, tmp_path, forbidden)
    with ProcessLock(tmp_path / "data/operations/daily.lock", "existing-daily"):
        assert main(arguments()) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "already_running" and result["exit_code"] == 3


def test_completed_revision_validation_stays_in_report_core(monkeypatch, tmp_path, capsys):
    def publish(root, **kwargs):
        assert kwargs["revision"] is None
        raise ValueError("engineering_report_requires_completed_revision")
    install(monkeypatch, tmp_path, publish)
    assert main(arguments()) == 1
    assert "engineering_report_requires_completed_revision" in capsys.readouterr().err


def test_publication_requires_explicit_frozen_selection():
    with pytest.raises(SystemExit) as result:
        main(["sector", "publish-report"])
    assert result.value.code == 2


@pytest.mark.parametrize("option", ["--online", "--scheduled", "--skip-model", "--force"])
def test_publication_does_not_accept_network_scheduling_or_model_switches(option):
    with pytest.raises(SystemExit) as result:
        main(arguments(option))
    assert result.value.code == 2


def test_production_selection_does_not_default_to_engineering_revision(monkeypatch, tmp_path):
    def publish(root, **kwargs):
        assert kwargs["selection_id"] == "sector-production-frozen"
        assert kwargs["revision"] is None and kwargs["report_id"] is None
        return {"status": "production_empty_report", "engineering_fallback": False}
    install(monkeypatch, tmp_path, publish)
    assert main(["sector", "publish-report", "--selection", "sector-production-frozen"]) == 0
