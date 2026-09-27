"""Shared calendar caches stay verified, read-only and offline in these tests."""
from datetime import date, timedelta
import json

import pytest

from ashare_daily import sector_history as history
from ashare_daily.calendar import _digest
from ashare_daily.providers import baostock, baostock_f2, sector_status
from ashare_daily.sector_observation import prepare_observation_calendar
from test_f2_baostock import CalendarClient
from test_sector_history import config, freeze


TARGET = date(2026, 9, 10)


def write_calendar(directory, name, start, end):
    parameters = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    fields = baostock.expected_fields("calendar", parameters)
    # Synthetic statuses deliberately do not model weekdays or real sessions.
    rows = [{"calendar_date": (start + timedelta(days=i)).isoformat(),
             "is_trading_day": "1" if (start + timedelta(days=i)).toordinal() % 3 else "0"}
            for i in range((end - start).days + 1)]
    for row in rows:
        if row["calendar_date"] == TARGET.isoformat():
            row["is_trading_day"] = "1"
    response = baostock.base_result("calendar", parameters)
    response.update(ok=True, status="ok", error_code="0", fields=fields, rows=rows,
        fetched_at="2026-09-10T21:00:00+08:00", raw_hash=baostock.raw_hash(fields, rows),
        login={"ok": True, "error_code": "0", "error_msg": "success"})
    packet = {"schema_version": "f1-calendar-cache-v1", "provider": "baostock", "mode": "offline_test",
              "first_seen_at": response["fetched_at"], "response": response}
    packet["content_hash"] = _digest(packet)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path


def cached_windows(root):
    primary, extra = root / "offline_test/f2_calendar", root / "offline_test/f1_calendar"
    write_calendar(primary, "older.json", TARGET - timedelta(days=732), TARGET - timedelta(days=367))
    write_calendar(primary, "recent.json", TARGET - timedelta(days=366), TARGET - timedelta(days=1))
    supplemental = write_calendar(extra, "latest.json", TARGET - timedelta(days=7), TARGET)
    return primary, extra, supplemental


class FailedCalendar(baostock.BaoStockClient):
    def __init__(self):
        super().__init__(max_attempts=1)
        self.calls = []

    def query(self, operation, **parameters):
        self.calls.append(parameters)
        return baostock.base_result(operation, parameters)


def test_adjacent_verified_caches_supply_320_dates_without_network_or_writes(tmp_path):
    primary, extra, supplemental = cached_windows(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    client = FailedCalendar()
    result = baostock_f2.resolve_history_calendar(TARGET, primary, additional_cache_roots=[extra],
        client=client, mode="offline_test")
    assert result["verified"] is True
    assert len(result["trading_dates"]) == 320 and result["trading_dates"][-1] == "2026-09-10"
    assert client.calls == [] and result["responses"] == []
    assert str(supplemental) in {item["path"] for item in result["source_metadata"]}
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.json")} == before


@pytest.mark.parametrize("invalid", ["hash", "provider", "mode", "response_hash", "first_seen", "missing_date"])
def test_invalid_extra_calendar_cannot_complete_the_window(tmp_path, invalid):
    primary, extra, supplemental = cached_windows(tmp_path)
    packet = json.loads(supplemental.read_text(encoding="utf-8"))
    if invalid == "hash":
        packet["content_hash"] = "0" * 64
    else:
        if invalid == "provider":
            packet["provider"] = "other"
        elif invalid == "mode":
            packet["mode"] = "research"
        elif invalid == "response_hash":
            packet["response"]["raw_hash"] = "0" * 64
        elif invalid == "first_seen":
            packet["first_seen_at"] = "2026-09-09T21:00:00+08:00"
        elif invalid == "missing_date":
            packet["response"]["rows"].pop()
            packet["response"]["raw_hash"] = baostock.raw_hash(packet["response"]["fields"], packet["response"]["rows"])
        packet["content_hash"] = _digest({k: v for k, v in packet.items() if k != "content_hash"})
    supplemental.write_text(json.dumps(packet), encoding="utf-8")
    before = supplemental.read_bytes()
    client = FailedCalendar()
    result = baostock_f2.resolve_history_calendar(TARGET, primary, additional_cache_roots=[extra],
        client=client, mode="offline_test")
    assert result["verified"] is False and result["trading_dates"] == []
    assert len(result["cache_rejections"]) == 1
    assert result["cache_rejections"][0]["path"] == str(supplemental)
    assert len(client.calls) == 1 and supplemental.read_bytes() == before


def test_conflicting_extra_calendar_blocks_before_network(tmp_path):
    primary, extra, supplemental = cached_windows(tmp_path)
    packet = json.loads(supplemental.read_text(encoding="utf-8"))
    row = packet["response"]["rows"][0]
    row["is_trading_day"] = "0" if row["is_trading_day"] == "1" else "1"
    packet["response"]["raw_hash"] = baostock.raw_hash(packet["response"]["fields"], packet["response"]["rows"])
    packet["content_hash"] = _digest({k: v for k, v in packet.items() if k != "content_hash"})
    supplemental.write_text(json.dumps(packet), encoding="utf-8")
    client = FailedCalendar()
    result = baostock_f2.resolve_history_calendar(TARGET, primary, additional_cache_roots=[extra],
        client=client, mode="offline_test")
    assert result["verified"] is False and "冲突" in result["validation_error"]
    assert client.calls == []


def test_new_calendar_responses_only_write_the_primary_cache(tmp_path):
    primary, extra = tmp_path / "offline_test/f2", tmp_path / "offline_test/f1"
    supplemental = write_calendar(extra, "latest.json", TARGET, TARGET)
    before = supplemental.read_bytes()
    client = CalendarClient()
    result = baostock_f2.resolve_history_calendar(date(2026, 9, 11), primary,
        additional_cache_roots=(extra,), client=client, mode="offline_test")
    assert result["verified"] is True and len(client.calls) == 2
    assert len(list(primary.glob("*.json"))) == 2
    assert list(extra.glob("*.json")) == [supplemental] and supplemental.read_bytes() == before


def test_offline_resolver_rejects_a_research_additional_cache(tmp_path):
    client = FailedCalendar()
    with pytest.raises(ValueError, match="research"):
        baostock_f2.resolve_history_calendar(TARGET, tmp_path / "offline_test/calendar",
            additional_cache_roots=(tmp_path / "research/calendar",), client=client, mode="offline_test")
    assert client.calls == []


def configured_windows(root):
    primary, extra, _ = cached_windows(root)
    cfg = config()
    cfg.update(calendar_cache=str(primary.relative_to(root)), target_trading_days=320,
               universe_config="offline_test/universe.json")
    (root / cfg["universe_config"]).write_text(json.dumps({"calendar_cache": str(extra.relative_to(root))}), encoding="utf-8")
    return cfg


def test_observation_preparation_reads_the_configured_universe_calendar(tmp_path, monkeypatch):
    cfg = configured_windows(tmp_path)
    monkeypatch.setattr(sector_status, "_permission", lambda root: None)
    monkeypatch.setattr(baostock.BaoStockClient, "query", lambda *a, **k: pytest.fail("verified caches must avoid network"))
    resolve = baostock_f2.resolve_history_calendar
    def offline_resolve(*args, **kwargs):
        assert kwargs.pop("mode") == "research"
        return resolve(*args, **kwargs, mode="offline_test")
    monkeypatch.setattr(baostock_f2, "resolve_history_calendar", offline_resolve)
    result = prepare_observation_calendar(tmp_path, {"target_date": TARGET.isoformat()}, cfg, max_seconds=30)
    assert result["verified"] is True and len(result["trading_dates"]) == 320
    assert result["network_requests"] == 0


def test_history_plan_and_screening_share_the_configured_universe_calendar(tmp_path):
    cfg = configured_windows(tmp_path)
    selected = freeze(cfg)
    result = history.prepare_history(tmp_path, selected, cfg, max_seconds=0)
    assert result["status"] != "calendar_blocked"
    assert result["metrics"]["network_requests"] == 0
    inputs = history.screening_history_inputs(tmp_path, selected, cfg)
    assert inputs["calendar"]["verified"] is True
    assert len(inputs["calendar"]["trading_dates"]) == 320


def test_universe_calendar_path_cannot_escape_project(tmp_path):
    cfg = configured_windows(tmp_path)
    (tmp_path / cfg["universe_config"]).write_text(json.dumps({"calendar_cache": "../outside"}), encoding="utf-8")
    with pytest.raises(ValueError, match="within project root"):
        history.prepare_history(tmp_path, freeze(cfg), cfg, dry_run=True)
