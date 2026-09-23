"""CLI boundaries: mode separation, gap exit status and replay arguments."""

import json
from pathlib import Path

import pytest

from ashare_daily.cli import main


@pytest.mark.parametrize("arguments", [
    ["brief", "--mode", "demo", "--snapshot", "anything"],
    ["brief", "--mode", "demo", "--date", "2026-09-08", "--fetch-adjusted"],
    ["brief", "--mode", "research", "--date", "2026-09-08", "--empty-candidates"],
    ["screen", "--snapshot", "anything", "--config", "different.json"],
    ["screen", "--snapshot", "anything", "--date", "2026-09-08"],
])
def test_reject_incompatible_modes(arguments):
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2


@pytest.mark.parametrize("status,expected", [("partial", 1), ("market_only", 0), ("non_trading_day", 0)])
def test_gap_report_exit_code_is_not_successful_data_coverage(monkeypatch, capsys, status, expected):
    def stub(**kwargs):
        assert kwargs["target_date"].isoformat() == "2026-09-08"
        assert kwargs["fetch_adjusted"] is False
        return {"status": status, "generation_status": "ok", "verification_kind": "offline_test"}
    monkeypatch.setattr("ashare_daily.m2.run_m2", stub)
    assert main(["brief", "--mode", "research", "--date", "2026-09-08"]) == expected
    assert json.loads(capsys.readouterr().out)["generation_status"] == "ok"


def test_screen_snapshot_passes_only_explicit_replay(monkeypatch, capsys):
    def stub(**kwargs):
        assert kwargs["snapshot"] == "m2-test-id"
        assert kwargs["target_date"] is None
        assert kwargs["fetch_adjusted"] is False
        return {"status": "market_only"}
    monkeypatch.setattr("ashare_daily.m2.run_m2", stub)
    assert main(["screen", "--snapshot", "m2-test-id"]) == 0


def test_missing_real_database_reports_error_without_demo(tmp_path, capsys):
    assert main(["brief", "--mode", "research", "--date", "2026-09-08", "--db", str(tmp_path / "absent.sqlite3"), "--output-dir", str(tmp_path)]) == 1
    assert "真实数据库不存在" in capsys.readouterr().err
    assert not list(tmp_path.rglob("daily_brief.json"))
