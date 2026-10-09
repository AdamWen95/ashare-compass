"""Versioned local exchange schedules, never synthetic successful SDK responses."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("day,opened", [
    ("2026-10-01", False), ("2026-10-07", False), ("2026-10-08", True),
    ("2026-10-09", True), ("2026-10-10", False), ("2026-09-20", False),
    ("2024-02-09", False), ("2025-10-08", False), ("2025-10-09", True),
])
def test_verified_schedule_includes_holidays_and_makeup_weekends(day, opened):
    from ashare_daily.providers.exchange_calendar import build_packet, verify_packet
    target = date.fromisoformat(day)
    packet = build_packet(target, mode="offline_test")
    metadata, days = verify_packet(packet, mode="offline_test")
    assert days[target] is opened
    assert metadata["provider"] == "exchange_calendars"
    assert packet["calendar"] == "XSHG"
    assert packet["source_version"] == "4.13.2"
    assert packet["source_kind"] == "versioned_local_exchange_calendar"


def test_unreviewed_year_cannot_be_inferred_from_weekdays():
    from ashare_daily.providers.exchange_calendar import build_packet
    with pytest.raises(ValueError, match="coverage"):
        build_packet(date(2027, 1, 4), mode="offline_test")


@pytest.mark.parametrize("corruption", ["hash", "mode", "future", "source", "version", "missing_day", "false_open"])
def test_packet_rejects_corrupt_or_mislabelled_evidence(corruption):
    from ashare_daily.providers.exchange_calendar import build_packet, verify_packet, digest
    packet = deepcopy(build_packet(date(2026, 10, 8), mode="offline_test"))
    if corruption == "hash":
        packet["content_hash"] = "0" * 64
    else:
        if corruption == "mode": packet["mode"] = "research"
        elif corruption == "future": packet["first_seen_at"] = packet["fetched_at"] = "2099-01-01T00:00:00+08:00"
        elif corruption == "source": packet["source_file_sha256"] = "0" * 64
        elif corruption == "version": packet["source_version"] = "999.0"
        elif corruption == "missing_day": packet["calendar_days"].pop("2026-10-07")
        elif corruption == "false_open": packet["calendar_days"]["2026-10-07"] = True
        packet["content_hash"] = digest({k:v for k,v in packet.items() if k != "content_hash"})
    with pytest.raises(ValueError):
        verify_packet(packet, mode="offline_test")


def test_f1_local_calendar_runs_without_baostock_and_reuses_frozen_packet(tmp_path, monkeypatch):
    from ashare_daily.calendar import resolve_calendar
    monkeypatch.setattr("ashare_daily.providers.baostock.BaoStockClient.query",
                        lambda *a, **k: pytest.fail("local calendar must not query BaoStock"))
    first = resolve_calendar(date(2026, 10, 8), tmp_path, mode="offline_test", provider="exchange_calendars")
    assert first["calendar_verified"] and first["resolved_trade_date"] == "2026-10-08"
    assert first["provider"] == "exchange_calendars" and first["response"] == {}
    assert first["network_query_performed"] is False
    source_path = Path(first["cache_path"])
    before = source_path.read_bytes()
    second = resolve_calendar(date(2026, 10, 9), tmp_path, mode="offline_test", provider="exchange_calendars")
    assert second["resolved_trade_date"] == "2026-10-09" and second["cached"]
    assert second["source_fetched_at"] == first["source_fetched_at"]
    assert source_path.read_bytes() == before
    assert len(list(tmp_path.glob("*.json"))) == 1
    closed = resolve_calendar(date(2026, 10, 10), tmp_path, mode="offline_test", provider="exchange_calendars")
    assert closed["status"] == "non_trading_day" and closed["resolved_trade_date"] is None


def test_local_provider_outside_coverage_stays_unverified_without_network(tmp_path, monkeypatch):
    from ashare_daily.calendar import resolve_calendar
    monkeypatch.setattr("ashare_daily.providers.baostock.BaoStockClient.query",
                        lambda *a, **k: pytest.fail("unsupported date must not silently switch providers"))
    result = resolve_calendar(date(2027, 1, 4), tmp_path, mode="offline_test", provider="exchange_calendars")
    assert result["status"] == "calendar_unverified"
    assert not result["calendar_verified"] and not result["network_query_performed"]
    assert "coverage" in result["validation_error"]


def test_f1_rejects_different_sources_disagreeing_about_target(tmp_path):
    from ashare_daily.calendar import resolve_calendar
    from test_f1_calendar import CalendarClient
    target = date(2026, 10, 8)
    resolve_calendar(target, tmp_path, client=CalendarClient(closed=True), mode="offline_test")
    result = resolve_calendar(target, tmp_path, mode="offline_test", provider="exchange_calendars")
    assert not result["calendar_verified"] and result["status"] == "calendar_unverified"
    assert result["cache_conflict"] and "conflict" in result["validation_error"]


def test_baostock_path_cannot_clear_cross_source_conflict_by_retry(tmp_path):
    from ashare_daily.calendar import resolve_calendar
    from test_f1_calendar import CalendarClient
    target = date(2026, 10, 8)
    resolve_calendar(target, tmp_path, client=CalendarClient(closed=True), mode="offline_test")
    resolve_calendar(target, tmp_path, mode="offline_test", provider="exchange_calendars")
    client = CalendarClient(closed=True)
    result = resolve_calendar(target, tmp_path, client=client, mode="offline_test")
    assert not result["calendar_verified"] and result["cache_conflict"]
    assert client.calls == []


def test_default_provider_does_not_silently_relabel_local_evidence(tmp_path):
    from ashare_daily.calendar import resolve_calendar
    from test_f1_calendar import CalendarClient
    target = date(2026, 10, 8)
    resolve_calendar(target, tmp_path, mode="offline_test", provider="exchange_calendars")
    client = CalendarClient(fail=True)
    result = resolve_calendar(target, tmp_path, client=client, mode="offline_test")
    assert len(client.calls) == 1
    assert not result["calendar_verified"] and result["provider"] == "baostock"


def test_new_online_result_conflicting_with_local_calendar_cannot_pass(tmp_path):
    from ashare_daily.calendar import resolve_calendar
    from test_f1_calendar import CalendarClient
    target = date(2026, 10, 8)
    resolve_calendar(target, tmp_path, mode="offline_test", provider="exchange_calendars")
    result = resolve_calendar(target, tmp_path, client=CalendarClient(closed=True), mode="offline_test")
    assert not result["calendar_verified"] and result["cache_conflict"]
