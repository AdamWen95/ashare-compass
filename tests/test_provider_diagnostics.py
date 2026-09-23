"""Offline diagnostic contracts. No synthetic response is run as an online check."""
from copy import deepcopy
from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ashare_daily import provider_diagnostics as diagnostics
from ashare_daily.calendar import _digest
from ashare_daily.providers.base import MarketDataProvider, ProviderResult
from ashare_daily.providers.baostock import base_result, raw_hash
from ashare_daily.universe import UniverseStore, scope_boards


DAY = "2026-09-10"
STAMP = DAY + "T21:00:00+08:00"


def config(*, scope="sse_szse_a", enabled=False, permission="unconfirmed"):
    return {"schema_version": "f2-market-config-v1", "config_version": "synthetic-config",
            "scope": scope, "provider": "baostock", "permission_status": "approved", "permission_basis": "offline test fixture",
            "model_calls": 0, "target_trading_days": 320, "recheck_days": 5, "max_attempts": 2,
            "timeout_seconds": 20, "pause_seconds": 0.5, "database": "do-not-open-market.sqlite3",
            "checkpoint_database": "do-not-open-jobs.sqlite3", "calendar_cache": "calendar",
            "output_directory": "unchanged-reports", "universe_config": "universe.json",
            "provider_routing": {"schema_version": "f2-provider-routing-v1", "order": ["baostock", "eastmoney"],
                "failure_threshold": 3, "eastmoney": {"enabled": enabled, "permission_status": permission,
                    "permission_basis": "synthetic not authorization", "purpose": "personal_noncommercial_local_research",
                    "permitted_storage": permission == "approved", "permitted_automated_access": permission == "approved",
                    "llm_export": False}}}


def save_config(root, value=None):
    path = root / "config.json"
    path.write_text(json.dumps(value or config()), encoding="utf-8")
    return path


def deny_online_and_database(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("configuration-only check attempted database/client construction")
    monkeypatch.setattr(diagnostics.sqlite3, "connect", forbidden)
    monkeypatch.setattr(diagnostics, "_construct_source", forbidden)
    monkeypatch.setattr(diagnostics, "_read_snapshot", forbidden)


@pytest.mark.parametrize("provider,online,status,reason,code", [
    ("baostock", False, "not_checked", "online_not_requested", 0),
    ("eastmoney", False, "unavailable", "permission_required", 2),
    ("eastmoney", True, "unavailable", "permission_required", 2),
])
def test_default_and_disabled_are_config_only(tmp_path, monkeypatch, provider, online, status, reason, code):
    path = save_config(tmp_path)
    deny_online_and_database(monkeypatch)
    result, actual_code = diagnostics.provider_check(project_root=tmp_path, config_path=path,
        provider=provider, target_date=DAY, online=online)
    assert actual_code == code and result["status"] == status and result["reason"] == reason
    assert result["network_requests"] == 0 and result["source_responses"] == []
    assert result["online_quote_verified"] is False and result["source_business_date"] is None
    assert result["full_market_verified"] is False and result["model_calls"] == 0
    frozen = json.loads(open(result["result_path"], encoding="utf-8").read())
    assert frozen == result
    assert not list(tmp_path.glob("*.sqlite3")) and not (tmp_path / "unchanged-reports").exists()


def test_unconfirmed_enabled_source_is_permission_problem_not_network_failure(tmp_path, monkeypatch):
    path = save_config(tmp_path, config(enabled=True))
    deny_online_and_database(monkeypatch)
    result, code = diagnostics.provider_check(project_root=tmp_path, config_path=path,
        provider="eastmoney", target_date=DAY, online=True)
    assert code == 2 and result["reason"] == "permission_required" and result["network_requests"] == 0


def test_unconfirmed_exact_provider_purpose_is_rejected_before_input_reads(tmp_path, monkeypatch):
    value = config(enabled=True, permission="approved")
    value["provider_routing"]["eastmoney"]["purpose"] = "unconfirmed_different_purpose"
    path = save_config(tmp_path, value)
    deny_online_and_database(monkeypatch)
    result, code = diagnostics.provider_check(project_root=tmp_path, config_path=path,
        provider="eastmoney", target_date=DAY, online=True)
    assert code == 2 and result["reason"] == "permission_required" and result["network_requests"] == 0


def test_all_a_online_cannot_drop_bse_to_pass(tmp_path, monkeypatch):
    path = save_config(tmp_path, config(scope="all_a"))
    deny_online_and_database(monkeypatch)
    result, code = diagnostics.provider_check(project_root=tmp_path, config_path=path,
        provider="baostock", target_date=DAY, online=True)
    assert code == 2 and result["reason"] == "unsupported_scope" and result["network_requests"] == 0


def test_repeated_diagnostics_use_new_evidence_and_preserve_previous_result(tmp_path, monkeypatch):
    path = save_config(tmp_path)
    deny_online_and_database(monkeypatch)
    first, _ = diagnostics.provider_check(project_root=tmp_path, config_path=path, provider="baostock", target_date=DAY)
    before = open(first["result_path"], "rb").read()
    second, _ = diagnostics.provider_check(project_root=tmp_path, config_path=path, provider="baostock", target_date=DAY)
    assert first["result_path"] != second["result_path"] and open(first["result_path"], "rb").read() == before


def snapshot():
    members = [{"security_id": "synthetic-" + board, "code": code, "exchange": exchange, "board": board,
                "metadata_verified": True, "security_type": "ordinary_a", "discovery_classification": "ordinary_a"}
               for code, exchange, board in [("600000", "SSE", "sse_main"), ("000001", "SZSE", "szse_main"),
                                             ("300750", "SZSE", "chinext"), ("688981", "SSE", "star")]]
    result = {"mode": "research", "scope": "sse_szse_a", "requested_date": DAY, "resolved_trade_date": DAY,
        "required_boards": list(scope_boards("sse_szse_a")), "universe_verified": True, "collection_ready": True,
        "calendar_verified": True, "blockers": [], "observed_at": STAMP, "cutoff_at": DAY + "T18:30:00+08:00",
        "source_manifests": [{"permission_status": "approved", "provenance_mode": "online", "complete": True}],
        "members": members, "ordinary_a_count": 4, "board_counts": {m["board"]: 1 for m in members}, "status": "partial"}
    result["content_hash"] = _digest(result)
    result["snapshot_id"] = "universe-" + DAY + "-" + result["content_hash"][:20]
    return result


def test_identity_samples_use_metadata_not_code_prefix():
    value = snapshot()
    # A deliberately unusual fixture proves the metadata mapping, not a stock list.
    value["members"][0]["code"] = "001234"
    selected = diagnostics._select_requests(value, DAY, "sse_szse_a")
    assert selected[0].identity.symbol == "sh.001234"
    assert {r.identity.board for r in selected} == set(scope_boards("sse_szse_a"))
    assert all(r.start_date == r.end_date == DAY and r.expected_dates == (DAY,) for r in selected)


@pytest.mark.parametrize("change", [
    lambda s: s.update(mode="offline_test"), lambda s: s.update(requested_date="2026-09-09"),
    lambda s: s.update(universe_verified=False), lambda s: s["members"][0].update(metadata_verified=False),
    lambda s: s["members"][0].update(security_type="cdr"), lambda s: s["members"][0].update(board="bse", exchange="BSE"),
    lambda s: s["members"].pop(), lambda s: s["source_manifests"][0].update(provenance_mode="offline_test"),
])
def test_invalid_snapshot_never_produces_four_board_samples(change):
    value = snapshot()
    change(value)
    with pytest.raises(ValueError):
        diagnostics._select_requests(value, DAY, "sse_szse_a")


def test_incomplete_source_is_not_misclassified_as_permission_required():
    value = snapshot()
    value["source_manifests"][0]["complete"] = False
    with pytest.raises(ValueError, match="completeness") as caught:
        diagnostics._select_requests(value, DAY, "sse_szse_a")
    assert diagnostics._permission_issue(caught.value) is False
    assert diagnostics._permission_issue(ValueError("未确认用途和权限的来源不能启用")) is True


def test_readonly_ledger_decoder_verifies_hash_without_constructor_or_mutation(tmp_path, monkeypatch):
    value = snapshot()
    path = tmp_path / "synthetic-ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE f1_schema(version,mode); INSERT INTO f1_schema VALUES(1,'research'); "
            "CREATE TABLE f1_universe_snapshots(snapshot_id,requested_date,resolved_trade_date,cutoff_at,first_observed_at,content_hash,status,payload);")
        connection.execute("INSERT INTO f1_universe_snapshots VALUES(?,?,?,?,?,?,?,?)", (value["snapshot_id"], DAY, DAY,
            value["cutoff_at"], STAMP, value["content_hash"], value["status"], json.dumps(value)))
    before = path.read_bytes()
    monkeypatch.setattr(UniverseStore, "__init__", lambda *a, **k: pytest.fail("mutating UniverseStore constructor used"))
    decoded = diagnostics._read_snapshot(tmp_path, config(), DAY,
        SimpleNamespace(scope="sse_szse_a", database=path.name))
    assert decoded == value and path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE f1_universe_snapshots SET content_hash=?", ("0" * 64,))
    with pytest.raises(ValueError, match="hash"):
        diagnostics._read_snapshot(tmp_path, config(), DAY, SimpleNamespace(scope="sse_szse_a", database=path.name))


def cache_packet(*, opened=True, mode="research"):
    params = {"start_date": DAY, "end_date": DAY}
    response = base_result("calendar", params)
    fields = ["calendar_date", "is_trading_day"]
    rows = [{"calendar_date": DAY, "is_trading_day": "1" if opened else "0"}]
    response.update(ok=True, status="ok", error_code="0", fetched_at=STAMP, fields=fields, rows=rows,
                    raw_hash=raw_hash(fields, rows), login={"ok": True, "error_code": "0", "error_msg": "fixture"})
    packet = {"schema_version": "f1-calendar-cache-v1", "mode": mode, "provider": "baostock",
              "first_seen_at": STAMP, "response": response}
    packet["content_hash"] = _digest(packet)
    return packet


def test_calendar_uses_verified_cache_and_rejects_same_time_conflict_and_test_mode(tmp_path):
    folder = tmp_path / "calendar"
    folder.mkdir()
    (folder / "test.json").write_text(json.dumps(cache_packet(mode="offline_test")), encoding="utf-8")
    rejected = diagnostics._cached_calendar(tmp_path, ["calendar"], DAY)
    assert not rejected["calendar_verified"] and len(rejected["cache_rejections"]) == 1
    (folder / "open.json").write_text(json.dumps(cache_packet()), encoding="utf-8")
    accepted = diagnostics._cached_calendar(tmp_path, ["calendar"], DAY)
    assert accepted["calendar_verified"] and accepted["calendar"] == {DAY: True}
    assert accepted["source_file_hash"] == sha256((folder / "open.json").read_bytes()).hexdigest()
    (folder / "closed.json").write_text(json.dumps(cache_packet(opened=False)), encoding="utf-8")
    conflicted = diagnostics._cached_calendar(tmp_path, ["calendar"], DAY)
    assert not conflicted["calendar_verified"] and conflicted["reason"] == "calendar_cache_conflict"


def test_recording_helper_keeps_raw_response_and_metrics_without_online_claim():
    original = {"ok": True, "provenance_mode": "offline_test", "error_code": "0", "raw_hash": "fixture"}
    captured = []

    class OfflineSource(MarketDataProvider):
        name = "eastmoney"

        def fetch_daily_bars(self, request):
            return ProviderResult(self.name, "daily_bars", "partial", original, records=[{"trade_date": DAY}],
                quality={"quote_complete": False}, metrics={"requests": 2, "retries": 1})

    requests = diagnostics._select_requests(snapshot(), DAY, "sse_szse_a")
    result = diagnostics._RecordingProvider(OfflineSource(), lambda *args: captured.append(args)).healthcheck(requests)
    assert result["status"] == "degraded" and len(captured) == 4
    assert all(record[1] is original and record[3]["requests"] == 2 for record in captured)
    assert result["sample_only"] and not result["full_market_verified"]


def test_public_signature_has_no_fake_or_mode_bypass():
    import inspect
    assert set(inspect.signature(diagnostics.provider_check).parameters) == {
        "project_root", "config_path", "provider", "target_date", "online"}


def test_cleanup_closes_each_owned_source_once_even_when_first_close_fails():
    calls = []

    class Source:
        def __init__(self, name, failed=False):
            self.name, self.failed = name, failed

        def close(self):
            calls.append(self.name)
            if self.failed:
                raise OSError("synthetic cleanup failure")

    first, second = Source("first", True), Source("second")
    errors = diagnostics._close_sources(first, second, first, None)
    assert calls == ["first", "second"] and errors == [{"provider": "first", "reason": "synthetic cleanup failure"}]
