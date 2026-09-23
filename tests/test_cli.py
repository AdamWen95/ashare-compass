"""User-facing offline commands and strict calendar-date parsing."""

import json

import pytest

from ashare_daily.cli import main


def test_doctor_offline_is_explicit_about_unverified_sources(capsys):
    assert main(["doctor", "--offline"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["network_access"] == "disabled"
    assert result["external_sources"] == "not_tested"
    assert result["checks"]


@pytest.mark.parametrize("value", ["20260909", "2026-9-9", "2026-02-30", "2026-09-09T21:00:00", "09/09/2026"])
def test_brief_rejects_non_iso_or_invalid_dates(value, tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["brief", "--mode", "demo", "--date", value, "--output-dir", str(tmp_path)])
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", [
    ["brief", "--mode", "unapproved_mode", "--date", "2026-09-09"],
    ["doctor", "--source", "unapproved_source"],
    ["research-news"],
    ["run-daily"],
])
def test_cli_milestone_command_boundary(command, monkeypatch, capsys):
    if command == ["run-daily"]:
        # This M0 expectation changes deliberately in M4. Verify dispatch with
        # an isolated service boundary; CLI regression must not contact sources.
        import ashare_daily.operations.daily as daily
        received = []
        def offline_run(**kwargs):
            received.append(kwargs)
            return {"status": "offline_cli_fixture", "exit_code": 0}
        monkeypatch.setattr(daily, "run_daily", offline_run)
        assert main(command) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "offline_cli_fixture"
        assert len(received) == 1
        assert received[0]["dry_run"] is False
        assert received[0]["scheduled"] is False
        return
    with pytest.raises(SystemExit) as error:
        main(command)
    assert error.value.code == 2


@pytest.mark.parametrize("extra", [[], ["--empty-candidates"]])
def test_brief_runs_offline_and_emits_three_formats(extra, tmp_path):
    assert main(["brief", "--mode", "demo", "--date", "2026-09-09", "--output-dir", str(tmp_path), *extra]) == 0
    reports = list((tmp_path / "demo" / "2026-09-09").glob("*/daily_brief.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["mode"] == "demo"
    assert report["trade_date"] is None
    assert report["scenario_date"] == "2026-09-09"
    if extra:
        assert report["candidates"] == []
    for suffix in ("md", "html"):
        content = reports[0].with_suffix("." + suffix).read_text(encoding="utf-8")
        assert "DEMO" in content
        assert "人工合成" in content
    assert (tmp_path / "demo" / "demo.sqlite3").is_file()
