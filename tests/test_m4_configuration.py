"""M4 project configuration isolation, without exposing real credentials."""
from ashare_daily.research.model_settings import load_model_settings
import json
import pytest
from datetime import date


def test_daily_model_settings_do_not_depend_on_calling_terminal(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MODEL_API_KEY=sk-offline-file-only\nMODEL_NAME=file-model\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_API_KEY", "sk-offline-terminal-only")
    monkeypatch.setenv("MODEL_NAME", "terminal-model")
    local = load_model_settings(env, include_environment=False)
    assert local.api_key.get_secret_value() == "sk-offline-file-only"
    assert local.model_name == "file-model"
    assert "sk-offline" not in str(local.public_dict())
    assert env.read_text(encoding="utf-8") == "MODEL_API_KEY=sk-offline-file-only\nMODEL_NAME=file-model\n"


def test_backup_cli_lock_conflict_has_exit_code_without_writing(monkeypatch, capsys):
    from ashare_daily.cli import main
    from ashare_daily.operations import lock, backup
    def locked(*args, **kwargs):
        raise lock.AlreadyRunning()
    def forbidden(*args, **kwargs):
        raise AssertionError("backup must not execute while the daily job holds its lock")
    monkeypatch.setattr(lock, "ProcessLock", locked)
    monkeypatch.setattr(backup, "create_backup", forbidden)
    assert main(["backup"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "already_running"


def test_calendar_error_preserves_supplier_reason_and_raw_response(tmp_path, monkeypatch):
    from ashare_daily.operations.daily import DailyConfig, LiveServices
    from ashare_daily.providers import baostock
    class Unavailable:
        def __init__(self, *args):
            pass
        def query(self, *args, **kwargs):
            return {"ok":False,"status":"unknown","error_code":"10002007","error_msg":"OFFLINE network failure"}
    monkeypatch.setattr(baostock, "BaoStockClient", Unavailable)
    with pytest.raises(ValueError, match="10002007.*OFFLINE network failure"):
        LiveServices().ensure_calendar(tmp_path / "missing.sqlite3",date(2026,9,9),DailyConfig(),tmp_path / "requests")
    assert json.loads((tmp_path / "requests/calendar-response.json").read_text(encoding="utf-8"))["ok"] is False
