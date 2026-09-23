"""Exercise Windows publication failures with isolated explicit OFFLINE reports."""
from pathlib import Path

import pytest

from ashare_daily.operations import backup
from ashare_daily.operations.daily import publish_report, run_daily
from test_m4_daily import FakeServices, NOW, execute, project


def failure(number):
    error = PermissionError("OFFLINE simulated directory publication error")
    if number is not None:
        error.winerror = number
    return error


def injected_rename(monkeypatch, number, *, successful_attempt=None):
    original = Path.rename
    calls, sleeps = [], []
    def rename(path, destination):
        if not path.name.startswith(".pending-"):
            return original(path, destination)
        calls.append((path, destination))
        if len(calls) != successful_attempt:
            raise failure(number)
        return original(path, destination)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    return calls, sleeps


@pytest.mark.parametrize("number", [5, 32, 33])
def test_scheduled_preview_then_windows_retry_does_not_repeat_daily_stages(project, monkeypatch, number):
    for hour in (18, 19):
        preview = run_daily(project=project, now=NOW.replace(hour=hour), scheduled=True, services=FakeServices())
        assert preview["status"] == "not_due"
    calls, sleeps = injected_rename(monkeypatch, number, successful_attempt=3)
    service = FakeServices()
    completed = run_daily(project=project, now=NOW, scheduled=True, services=service)
    assert completed["generation_status"] == "ok"
    assert service.calls == ["calendar", "market", "freeze", "materials", "research"]
    assert service.model_calls == 1
    assert len(calls) == 3 and sleeps == [.05, .1]
    assert Path(completed["report"]["json"]).is_file()
    assert not list((project / "outputs/research/m4/reports").glob("*/.pending-*"))


@pytest.mark.parametrize("number,attempts,delays", [(5, 4, [.05, .1, .2]), (32, 4, [.05, .1, .2]),
    (33, 4, [.05, .1, .2]), (None, 1, []), (2, 1, []), (183, 1, [])])
def test_persistent_or_unrelated_error_preserves_original_report_and_pending_evidence(project, monkeypatch, number, attempts, delays):
    original = execute(project)
    latest_path = project / "outputs/research/m4/latest_report.json"
    old_latest, old_report = latest_path.read_bytes(), Path(original["report"]["json"]).read_bytes()
    calls, sleeps = injected_rename(monkeypatch, number)
    service = FakeServices()
    result = execute(project, service, force=True)
    assert result["status"] == "failed" and result["generation_status"] == "not_run"
    assert len(calls) == attempts and sleeps == delays
    assert service.model_calls == 1
    assert latest_path.read_bytes() == old_latest
    assert Path(original["report"]["json"]).read_bytes() == old_report
    staged = project / "outputs/research/m4/reports" / result["target_trade_date"] / (".pending-" + result["run_id"])
    assert (staged / "manifest.json").is_file() and (staged / "daily_brief.json").is_file()
    assert not staged.with_name(result["run_id"]).exists()


def test_existing_report_destination_cannot_be_overwritten(project, monkeypatch):
    original = execute(project)
    latest = project / "outputs/research/m4/latest_report.json"
    old_latest, old_report = latest.read_bytes(), Path(original["report"]["json"]).read_bytes()
    monkeypatch.setattr(Path, "rename", lambda *args: pytest.fail("existing report must never be overwritten"))
    with pytest.raises(ValueError, match="未覆盖"):
        publish_report(original["report"], project / "outputs/research/m4", {"run_id": original["run_id"]})
    assert latest.read_bytes() == old_latest
    assert Path(original["report"]["json"]).read_bytes() == old_report


def test_target_appearing_during_retry_is_retained_with_pending_and_old_latest(project, monkeypatch):
    original = execute(project)
    latest = project / "outputs/research/m4/latest_report.json"
    old_latest = latest.read_bytes()
    calls, sleeps = injected_rename(monkeypatch, 5)
    def appeared(delay):
        sleeps.append(delay)
        destination = calls[-1][1]
        destination.mkdir()
        (destination / "external.txt").write_bytes(b"OFFLINE existing target")
    monkeypatch.setattr(backup.time, "sleep", appeared)
    result = execute(project, FakeServices(), force=True)
    assert result["status"] == "failed" and "未覆盖" in result["failure_reason"]
    assert len(calls) == 1 and sleeps == [.05]
    assert calls[0][0].is_dir()
    assert (calls[0][1] / "external.txt").read_bytes() == b"OFFLINE existing target"
    assert latest.read_bytes() == old_latest
