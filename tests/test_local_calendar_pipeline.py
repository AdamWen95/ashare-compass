"""Network-denied regression for the production sector calendar reader."""
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ashare_daily import provider_diagnostics, sector_pipeline
from ashare_daily.calendar import _digest
from ashare_daily.providers.baostock import BaoStockClient
from ashare_daily.providers.exchange_calendar import build_packet
from test_provider_diagnostics import DAY, STAMP, cache_packet, snapshot


def write_packet(path, packet):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet), encoding="utf-8")


def local_packet():
    packet = build_packet(date.fromisoformat(DAY), mode="research")
    packet.update(first_seen_at=STAMP, fetched_at=STAMP)
    packet["content_hash"] = _digest({key: value for key, value in packet.items() if key != "content_hash"})
    return packet


def baostock_packet(*, opened, stamp):
    packet = cache_packet(opened=opened)
    packet["first_seen_at"] = packet["response"]["fetched_at"] = stamp
    packet["content_hash"] = _digest({key: value for key, value in packet.items() if key != "content_hash"})
    return packet


@pytest.fixture
def production_inputs(tmp_path, monkeypatch):
    universe = snapshot()
    database = tmp_path / "universe.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("CREATE TABLE f1_schema(version,mode); INSERT INTO f1_schema VALUES(1,'research'); "
            "CREATE TABLE f1_universe_snapshots(snapshot_id,requested_date,resolved_trade_date,cutoff_at,first_observed_at,content_hash,status,payload);")
        connection.execute("INSERT INTO f1_universe_snapshots VALUES(?,?,?,?,?,?,?,?)", (
            universe["snapshot_id"], DAY, DAY, universe["cutoff_at"], STAMP,
            universe["content_hash"], universe["status"], json.dumps(universe)))
    sites = [SimpleNamespace(exchange=exchange, enabled=True, permission_status="approved")
             for exchange in ("SSE", "SZSE")]
    sources = [SimpleNamespace(provider="baostock", enabled=True, permission_status="approved"),
               SimpleNamespace(provider="exchange_lists", enabled=True, permission_status="approved", sites=sites)]
    universe_config = SimpleNamespace(scope="sse_szse_a", database=database.name,
                                     calendar_cache="f1-calendar", sources=sources)
    monkeypatch.setattr(sector_pipeline, "load_universe_config", lambda *_: universe_config)
    monkeypatch.setattr(BaoStockClient, "query", lambda *_a, **_k: pytest.fail("calendar network attempted"))
    constructed = []

    class IsolatedSource:
        def __init__(self, *_args, **_kwargs):
            constructed.append(self)
            self.client = SimpleNamespace(calls=0)
            self.closed = False

        def fetch_catalog(self, **_kwargs):
            return {"rows": [], "complete": False, "provenance_mode": "offline_test"}

        def close(self):
            self.closed = True

    monkeypatch.setattr("ashare_daily.providers.sina_sectors.SinaSectorProvider", IsolatedSource)
    config = {"universe_config": "universe.json", "market_scope": "sse_szse_a",
              "calendar_cache": "f2-calendar", "sina": {}, "timeout_seconds": 1,
              "pause_seconds": 0, "taxonomy": "synthetic_catalog"}

    def collect():
        return sector_pipeline.collect_inputs(tmp_path, config, DAY, tmp_path / "evidence", max_seconds=10)

    return collect, constructed, database


def test_collect_inputs_accepts_local_calendar_without_network_and_preserves_source(production_inputs, tmp_path):
    collect, constructed, database = production_inputs
    path = tmp_path / "f1-calendar/local.json"
    write_packet(path, local_packet())
    original, original_database = path.read_bytes(), database.read_bytes()
    result = collect()
    calendar = result["calendar"]
    assert calendar["calendar_verified"] is True and calendar["calendar"] == {DAY: True}
    assert calendar["provider"] == "exchange_calendars"
    assert calendar["verification_kind"] == "versioned_local_calendar"
    assert calendar["source_kind"] == "versioned_local_exchange_calendar"
    assert calendar["source_file_hash"] == sha256(original).hexdigest()
    assert result["network_requests_this_run"] == 0
    assert len(constructed) == 1 and constructed[0].closed
    assert path.read_bytes() == original and database.read_bytes() == original_database
    frozen = json.loads((tmp_path / "evidence/source_inputs.json").read_text(encoding="utf-8"))
    assert frozen["calendar"] == calendar


@pytest.mark.parametrize("baostock_newer", [False, True])
def test_collect_inputs_blocks_cross_provider_conflict_regardless_of_age(production_inputs, tmp_path, baostock_newer):
    collect, constructed, _ = production_inputs
    write_packet(tmp_path / "f1-calendar/local.json", local_packet())
    stamp = DAY + ("T22:00:00+08:00" if baostock_newer else "T20:00:00+08:00")
    write_packet(tmp_path / "f2-calendar/baostock.json", baostock_packet(opened=False, stamp=stamp))
    checked = provider_diagnostics._cached_calendar(tmp_path, ["f1-calendar", "f2-calendar"], DAY)
    assert checked["calendar_verified"] is False and checked["reason"] == "calendar_cache_conflict"
    with pytest.raises(ValueError, match="calendar_unverified_no_weekday_fallback"):
        collect()
    assert not constructed


@pytest.mark.parametrize("invalid", ["mode", "hash"])
def test_collect_inputs_rejects_invalid_local_source_before_collecting(production_inputs, tmp_path, invalid):
    collect, constructed, _ = production_inputs
    packet = deepcopy(local_packet())
    if invalid == "mode":
        packet["mode"] = "offline_test"
        packet["verification_kind"] = "offline_test"
        packet["content_hash"] = _digest({key: value for key, value in packet.items() if key != "content_hash"})
    else:
        packet["content_hash"] = "0" * 64
    write_packet(tmp_path / "f1-calendar/local.json", packet)
    with pytest.raises(ValueError, match="calendar_unverified_no_weekday_fallback"):
        collect()
    assert not constructed


def test_same_provider_revision_keeps_latest_version_semantics(tmp_path):
    write_packet(tmp_path / "calendar/old.json", baostock_packet(opened=False, stamp=DAY + "T20:00:00+08:00"))
    write_packet(tmp_path / "calendar/new.json", baostock_packet(opened=True, stamp=STAMP))
    result = provider_diagnostics._cached_calendar(tmp_path, ["calendar"], DAY)
    assert result["calendar_verified"] is True and result["calendar"] == {DAY: True}
    assert result["source_path"].endswith("new.json")
    assert set(result) == {"calendar_verified", "calendar", "cache_rejections", "cached", "source_path",
                           "source_raw_hash", "source_file_hash", "fetched_at"}


def test_superseded_same_provider_disagreement_does_not_block_matching_current_sources(tmp_path):
    write_packet(tmp_path / "calendar/local.json", local_packet())
    write_packet(tmp_path / "calendar/old.json", baostock_packet(opened=False, stamp=DAY + "T19:00:00+08:00"))
    write_packet(tmp_path / "calendar/new.json", baostock_packet(opened=True, stamp=DAY + "T20:00:00+08:00"))
    result = provider_diagnostics._cached_calendar(tmp_path, ["calendar"], DAY)
    assert result["calendar_verified"] is True and result["calendar"] == {DAY: True}
    assert result["provider"] == "exchange_calendars"


def test_explicit_offline_baostock_provenance_remains_rejected(tmp_path):
    packet = baostock_packet(opened=True, stamp=STAMP)
    packet["response"]["provenance_mode"] = "offline_test"
    packet["content_hash"] = _digest({key: value for key, value in packet.items() if key != "content_hash"})
    write_packet(tmp_path / "calendar/rejected.json", packet)
    result = provider_diagnostics._cached_calendar(tmp_path, ["calendar"], DAY)
    assert result["calendar_verified"] is False
    assert result["cache_rejections"][0]["reason"] == "calendar_response_provenance_invalid"
