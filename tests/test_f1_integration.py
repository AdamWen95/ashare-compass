"""OFFLINE F1 routing, permissions and legacy compatibility. No data acceptance."""
from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import shutil

import pytest

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.operations.daily import run_daily
from ashare_daily.universe_service import UniverseConfig, baostock_discovery_rows, discover_baostock, sync_date

ROOT = Path(__file__).resolve().parents[1]
TARGET = date(2026, 9, 11)
NOW = datetime(2026, 9, 11, 22, tzinfo=SHANGHAI)


@pytest.fixture
def f1_project(tmp_path):
    project = tmp_path / "OFFLINE-project"
    (project / "config").mkdir(parents=True)
    for name in ("universe.json", "full_market_daily.json", "sse_szse_universe.json", "sse_szse_daily.json", "sse_szse_market.json"):
        shutil.copyfile(ROOT / "config" / name, project / "config" / name)
    return project


def isolated_config(project):
    path = project / "config/universe.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.update(database="data/offline/universe.sqlite3", output_directory="outputs/offline/f1", calendar_cache="data/offline/calendar")
    path.write_text(json.dumps(config), encoding="utf-8")


class Discovery:
    def __init__(self, count=121, status="ok"):
        self.calls = []
        self.count = count
        self.status = status

    def query(self, operation, **params):
        self.calls.append((operation, params))
        rows = [{"code": f"sh.{n:06}", "code_name": f"OFFLINE-{n}", "tradeStatus": "1"} for n in range(self.count)] if operation == "universe" else []
        return {"ok": self.status == "ok", "status": self.status,
                "rows": rows if self.status == "ok" else [], "fetched_at": NOW.isoformat(),
                "raw_hash": "a" * 64, "pagination": {"exhausted": True}, "error_code": "0"}


def calendar(**kwargs):
    return {"status": "verified", "calendar_verified": True, "resolved_trade_date": TARGET.isoformat()}


def test_default_cli_dispatches_authorized_sse_szse_config(monkeypatch, capsys):
    from ashare_daily.cli import main
    import ashare_daily.operations.daily as module
    calls = []
    monkeypatch.setattr(module, "run_daily", lambda **kwargs: calls.append(kwargs) or {"exit_code": 0})
    assert main(["run-daily", "--dry-run"]) == 0
    assert calls[0]["config_path"] == "config/sse_szse_daily.json"
    assert calls[0]["sample_mode"] is False


def test_sample_cli_requires_manual_mode(monkeypatch):
    from ashare_daily.cli import main
    with pytest.raises(SystemExit) as error:
        main(["run-daily", "--sample", "--scheduled"])
    assert error.value.code == 2


def test_f1_preview_needs_no_fixed_sample_env_or_market_database(f1_project):
    result = run_daily(project=f1_project, config_path="config/full_market_daily.json", now=NOW, target=TARGET, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["scope"] == "all_a" and result["configured_stock_count"] is None
    assert result["model_summary"]["call_count"] == 0
    assert result["required_boards"] == ["sse_main", "szse_main", "chinext", "star", "bse"]
    assert not (f1_project / "data").exists()


def test_python_default_is_also_authorized_sse_szse_scope(f1_project):
    result = run_daily(project=f1_project, now=NOW, target=TARGET, dry_run=True)
    assert result["scope"] == "sse_szse_a" and result["status"] == "dry_run"
    assert result["required_boards"] == ["sse_main", "szse_main", "chinext", "star"]


def test_full_market_routes_no_members_to_legacy_pipeline(f1_project, monkeypatch):
    import ashare_daily.universe_service as service
    import ashare_daily.sample_data as legacy
    monkeypatch.setattr(legacy, "load_sample_config", lambda *a, **k: pytest.fail("fixed sample loaded"))
    monkeypatch.setattr(service, "sync_date", lambda **kwargs: {"status": "complete", "universe_verified": True,
        "calendar": {"status": "verified"}, "snapshot_id": "OFFLINE-test", "ordinary_a_count": 10001,
        "resolved_trade_date": TARGET.isoformat(), "board_counts": {b: 1 for b in service.BOARDS}})
    result = run_daily(project=f1_project, config_path="config/full_market_daily.json", now=NOW, target=TARGET)
    assert result["status"] == "f2_pending" and result["configured_stock_count"] == 10001
    assert result["generation_status"] == "not_run" and result["model_summary"]["call_count"] == 0
    assert not (f1_project / "outputs/research/m4/latest_report.json").exists()
    assert not (f1_project / "data/operations/runtime.sqlite3").exists()


@pytest.mark.parametrize("board", ["star", "bse", "chinext"])
def test_scope_cannot_silently_drop_a_board(board):
    config = json.loads((ROOT / "config/universe.json").read_text(encoding="utf-8"))
    config["required_boards"].remove(board)
    with pytest.raises(ValueError, match="五类板块"):
        UniverseConfig.model_validate(config)


def test_unknown_permission_does_not_enable_provider():
    config = json.loads((ROOT / "config/universe.json").read_text(encoding="utf-8"))
    config["sources"][1]["enabled"] = True
    with pytest.raises(ValueError, match="权限"):
        UniverseConfig.model_validate(config)


def test_even_approved_unimplemented_provider_cannot_be_enabled():
    config = json.loads((ROOT / "config/universe.json").read_text(encoding="utf-8"))
    config["sources"][1].update(enabled=True, permission_status="approved")
    with pytest.raises(ValueError, match="适配器"):
        UniverseConfig.model_validate(config)


def test_baostock_discovery_exceeds_100_without_prefix_classification(tmp_path):
    client = Discovery(1201)
    pages, manifests, requests = discover_baostock(client, target=TARGET, directory=tmp_path, mode="offline_test")
    assert len(pages[0]["records"]) == 1201
    assert all(r["board"] == "unknown" and r["security_type"] == "unknown" for r in pages[0]["records"])
    assert manifests[0]["coverage_boards"] == []
    assert [c[0] for c in client.calls] == ["universe", "basic_all"]
    assert requests[0]["row_count"] == 1201


@pytest.mark.parametrize("status", ["permission_denied", "rate_limited", "timeout"])
def test_failed_discovery_never_calls_next_source_operation(tmp_path, status):
    client = Discovery(status=status)
    pages, manifests, _ = discover_baostock(client, target=TARGET, directory=tmp_path, mode="offline_test")
    assert [c[0] for c in client.calls] == ["universe"]
    assert not pages[0]["records"]
    assert manifests[0]["errors"]


def test_status_and_type_do_not_default_to_normal():
    response = Discovery(1).query("universe")
    response["rows"][0]["tradeStatus"] = ""
    response["rows"][0]["code_name"] = "*ST OFFLINE"
    result = baostock_discovery_rows(response, {"ok": False}, TARGET)[0]
    assert result["statuses"]["suspended"]["value"] is None
    assert "st" not in result["statuses"]
    assert result["security_type"] == "unknown" and result["listing_status"] == "unknown"


def test_actual_sync_freezes_every_unknown_member(f1_project):
    isolated_config(f1_project)
    result = sync_date(project=f1_project, config_path="config/universe.json", target=TARGET, now=NOW,
        client=Discovery(121), calendar_resolver=calendar, offline_test=True)
    frozen = json.loads(Path(result["snapshot_path"]).read_text(encoding="utf-8"))
    assert frozen["discovered_unique"] == 121
    assert all(m["research_eligibility"] == "pending_metadata_or_status" for m in frozen["members"])
    assert result["status"] == "blocked" and not result["universe_verified"]
    assert all(b["market_total"] is None for b in result["board_coverage"].values())


def test_calendar_error_prevents_discovery(f1_project):
    isolated_config(f1_project)
    client = Discovery()
    result = sync_date(project=f1_project, config_path="config/universe.json", target=TARGET, now=NOW,
        client=client, calendar_resolver=lambda **k: {"status": "calendar_unverified", "resolved_trade_date": None}, offline_test=True)
    assert not client.calls and result["status"] == "blocked"
    assert "calendar_unverified" in result["blockers"]


@pytest.mark.parametrize("stop", ["permission_denied", "rate_limited"])
def test_calendar_permission_stop_applies_even_when_cache_is_valid(f1_project, stop):
    isolated_config(f1_project)
    client = Discovery()
    result = sync_date(project=f1_project, config_path="config/universe.json", target=TARGET, now=NOW,
        client=client, calendar_resolver=lambda **k: {**calendar(), "cached": True, "response": {"status": stop}}, offline_test=True)
    assert not client.calls and result["source_stop_reason"] == stop


def test_offline_injection_cannot_write_research(f1_project):
    with pytest.raises(ValueError, match="research"):
        sync_date(project=f1_project, config_path="config/universe.json", target=TARGET, now=NOW,
            client=Discovery(), calendar_resolver=calendar, offline_test=True)
    assert not (f1_project / "data/research").exists()


def test_injected_client_requires_offline_provenance(f1_project):
    with pytest.raises(ValueError, match="offline_test"):
        sync_date(project=f1_project, config_path="config/universe.json", target=TARGET, now=NOW, client=Discovery())
