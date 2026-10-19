"""Offline regression for an observed universe and its preceding close session."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_daily.calendar import _digest
from ashare_daily.market_foundation import _hash, normalize_baostock_rows
from ashare_daily.premarket import services_market
from ashare_daily.providers import baostock as bs, sector_status as status
from test_observation_quote_status import bridge, fake_sdk
from test_sector_status import DAY, STAMP, fixture as source_fixture, write


def seal_universe(universe):
    body = {key: value for key, value in universe.items() if key not in {"snapshot_id", "content_hash"}}
    universe["content_hash"] = _digest(body)
    universe["snapshot_id"] = "universe-" + universe["requested_date"] + "-" + universe["content_hash"][:20]


def calendar_days(fixture, days):
    _, _, _, folder, manifest, _ = fixture
    path = Path(manifest["calendar"]["source_path"])
    packet = json.loads(path.read_text(encoding="utf-8"))
    response = packet["response"]
    response["parameters"].update(start_date=min(days), end_date=max(days))
    response["rows"] = [{"calendar_date": day, "is_trading_day": "1" if opened else "0"}
                        for day, opened in sorted(days.items())]
    response["raw_hash"] = bs.raw_hash(response["fields"], response["rows"])
    packet["content_hash"] = _digest({key: value for key, value in packet.items() if key != "content_hash"})
    manifest["calendar"].update(source_file_hash=write(path, packet), source_raw_hash=response["raw_hash"])
    write(folder / "manifest.json", manifest)


@pytest.fixture
def morning(source_fixture):
    _, universe, _, folder, manifest, _ = source_fixture
    universe.update(requested_date="2026-09-11", resolved_trade_date="2026-09-11", observed_at=STAMP, cutoff_at=STAMP)
    seal_universe(universe)
    manifest["universe_snapshot_id"] = universe["snapshot_id"]
    calendar_days(source_fixture, {DAY: True, "2026-09-11": True})
    write(folder / "manifest.json", manifest)
    return source_fixture


def apply_morning(fixture, observed_day="2026-09-11"):
    root, universe, quotes, folder, _, _ = fixture
    return status.apply_status_evidence(root, universe, quotes, DAY, folder / "result.json",
                                        universe_observed_date=observed_day)


def test_premarket_market_collection_accepts_prior_session_halt_evidence(bridge, monkeypatch):
    root, packet, sector = bridge
    universe = packet["universe"]
    universe.update(requested_date="2026-09-11", resolved_trade_date="2026-09-11")
    seal_universe(universe)
    calpath = Path(packet["calendar"]["source_path"])
    calpacket = json.loads(calpath.read_text(encoding="utf-8"))
    response = calpacket["response"]
    response["parameters"]["end_date"] = "2026-09-11"
    response["rows"].append({"calendar_date": "2026-09-11", "is_trading_day": "1"})
    response["raw_hash"] = bs.raw_hash(response["fields"], response["rows"])
    calpacket["content_hash"] = _digest({k: v for k, v in calpacket.items() if k != "content_hash"})
    packet["calendar"].update(source_file_hash=write(calpath, calpacket), source_raw_hash=response["raw_hash"])
    fake_sdk(monkeypatch)
    monkeypatch.setattr(services_market, "load_config", lambda *a: sector)
    monkeypatch.setattr(services_market, "load_options", lambda *a: {"max_quote_status_seconds": 5,
        "max_quote_status_queries": 2, "require_delisting_check": False, "strategy_config": "strategy"})
    monkeypatch.setattr(services_market, "load_universe_config", lambda *a: SimpleNamespace(calendar_provider="exchange_calendars"))
    monkeypatch.setattr(services_market, "_read_snapshot", lambda *a, **kw: deepcopy(universe))
    monkeypatch.setattr(services_market, "collect_inputs", lambda *a, **k: deepcopy(packet))
    def history_unavailable(*a, **k):
        raise ValueError("offline_history_unavailable")
    monkeypatch.setattr(services_market, "_observe", history_unavailable)
    monkeypatch.setattr(services_market, "prepare_benchmark", lambda *a, **k: {})
    config = SimpleNamespace(sector_config="sector", observation_config="options", universe_config="universe",
                             eligibility_sources_config="eligibility")
    result = services_market.collect_market(root, config,
        {"report_date": "2026-09-11", "market_as_of_date": DAY, "is_trading_day": True}, root, 10, "morning")
    assert result["market"]["market_as_of_date"] == DAY
    assert result["universe_observed_date"] == "2026-09-11"
    archive = root / sector["output_directory"] / result["selection_id"] / "source_inputs.json"
    frozen = json.loads(archive.read_text(encoding="utf-8"))
    selection = json.loads((archive.parent / "sector_selection.json").read_text(encoding="utf-8"))
    assert selection["selection_verified"] is True
    assert frozen["universe"] == universe
    assert frozen["quotes"]["rows"][0]["full_day_halt_evidence"]["as_of_date"] == DAY


def test_explicit_premarket_context_preserves_dates_hashes_and_historical_replay(morning):
    original = deepcopy(morning[1:3])
    result = apply_morning(morning)
    assert morning[1:3] == original
    assert result["rows"][0]["full_day_halt_evidence"]["as_of_date"] == DAY
    assert result["status_evidence_overlays"][0]["universe_observed_date"] == "2026-09-11"
    assert len(result["file_refs"]) == 5


def test_legacy_default_still_rejects_cross_date_universe(morning):
    root, universe, quotes, folder, _, _ = morning
    with pytest.raises(ValueError, match="universe_date_or_verification"):
        status.apply_status_evidence(root, universe, quotes, DAY, folder / "result.json")


@pytest.mark.parametrize("changes", [
    {"requested_date": "2026-09-10", "resolved_trade_date": "2026-09-10"},
    {"resolved_trade_date": "2026-09-10"},
    {"universe_verified": False}, {"calendar_verified": False}, {"collection_ready": False},
    {"observed_at": "2026-09-10T18:00:00+08:00"},
    {"observed_at": "2026-09-12T18:00:00+08:00"},
])
def test_premarket_rejects_stale_or_unverified_universe_even_if_rehashed(morning, changes):
    _, universe, _, folder, manifest, _ = morning
    universe.update(changes)
    seal_universe(universe)
    manifest["universe_snapshot_id"] = universe["snapshot_id"]
    write(folder / "manifest.json", manifest)
    with pytest.raises(ValueError):
        apply_morning(morning)


def test_premarket_rejects_future_universe(morning):
    _, universe, _, folder, manifest, _ = morning
    universe.update(requested_date="2099-01-01", resolved_trade_date="2099-01-01", observed_at="2099-01-01T08:30:00+08:00")
    seal_universe(universe)
    manifest["universe_snapshot_id"] = universe["snapshot_id"]
    write(folder / "manifest.json", manifest)
    with pytest.raises(ValueError):
        apply_morning(morning, "2099-01-01")


@pytest.mark.parametrize("changed", ["hash", "snapshot", "identity", "quote_identity"])
def test_premarket_rejects_hash_and_security_identity_conflicts(morning, changed):
    _, universe, quotes, folder, manifest, _ = morning
    if changed == "hash":
        universe["content_hash"] = "0" * 64
    elif changed == "snapshot":
        universe["snapshot_id"] = "other-snapshot"
        manifest["universe_snapshot_id"] = universe["snapshot_id"]
    elif changed == "identity":
        manifest["identities"]["sz.000001"]["security_id"] = "conflicting-security"
    else:
        quotes["rows"][0]["identity"]["security_id"] = "conflicting-security"
    write(folder / "manifest.json", manifest)
    with pytest.raises(ValueError):
        apply_morning(morning)


@pytest.mark.parametrize("days", [{DAY: True}, {DAY: True, "2026-09-11": False}])
def test_premarket_requires_verified_observation_session(morning, days):
    calendar_days(morning, days)
    with pytest.raises(ValueError):
        apply_morning(morning)


@pytest.fixture
def weekend(morning):
    _, universe, quotes, folder, manifest, result = morning
    friday, monday = "2026-09-11", "2026-09-14"
    universe.update(requested_date=monday, resolved_trade_date=monday,
                    observed_at=monday + "T08:30:00+08:00", cutoff_at=monday + "T08:30:00+08:00")
    seal_universe(universe)
    manifest.update(target_date=friday, universe_snapshot_id=universe["snapshot_id"], observed_at=universe["observed_at"])
    result["target_date"] = friday
    for row in quotes["rows"]:
        row["trade_date"] = friday
    for cached, entries in ((True, result["cached"]), (False, result["online"])):
        for symbol, entry in entries.items():
            ref = entry["evidence"][0] if cached else entry
            path = Path(ref["source_path" if cached else "path"])
            response = json.loads(path.read_text(encoding="utf-8"))
            response["parameters"].update(start_date=friday, end_date=friday)
            response["rows"][0]["date"] = friday
            response["raw_hash"] = bs.raw_hash(response["fields"], response["rows"])
            ref["source_file_hash"] = write(path, response)
            ref["source_raw_hash" if cached else "raw_hash"] = response["raw_hash"]
            if cached:
                quality = normalize_baostock_rows(response["rows"], security_id=manifest["identities"][symbol]["security_id"],
                    symbol=symbol, start_date=friday, end_date=friday, trading_dates=(friday,))
                ref["fact_hash"] = _hash(quality["records"][0])
    write(folder / "result.json", result)
    calendar_days(morning, {friday: True, "2026-09-12": False, "2026-09-13": False, monday: True})
    return morning


def test_premarket_accepts_friday_halt_proof_for_monday_universe(weekend):
    root, universe, quotes, folder, _, _ = weekend
    result = status.apply_status_evidence(root, universe, quotes, "2026-09-11", folder / "result.json",
                                         universe_observed_date="2026-09-14")
    assert result["rows"][0]["full_day_halt_evidence"]["as_of_date"] == "2026-09-11"
    assert universe["requested_date"] == "2026-09-14"
    assert status.apply_status_evidence(root, universe, result, "2026-09-11", folder / "result.json",
                                        universe_observed_date="2026-09-14") == result


@pytest.mark.parametrize("days, reason", [
    ({"2026-09-11": True, "2026-09-12": False, "2026-09-14": True}, "交易日历缺少"),
    ({"2026-09-11": True, "2026-09-12": False, "2026-09-13": True, "2026-09-14": True}, "previous_session_unverified"),
])
def test_premarket_rejects_gap_or_later_session_in_hashed_calendar(weekend, days, reason):
    root, universe, quotes, folder, _, _ = weekend
    calendar_days(weekend, days)
    with pytest.raises(ValueError, match=reason):
        status.apply_status_evidence(root, universe, quotes, "2026-09-11", folder / "result.json",
                                     universe_observed_date="2026-09-14")
