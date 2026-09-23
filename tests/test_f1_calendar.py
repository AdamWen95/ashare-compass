"""F1 calendar cache tests use isolated synthetic source responses only."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from ashare_daily.calendar import resolve_calendar
from ashare_daily.providers.baostock import CALENDAR_FIELDS, base_result, raw_hash


TARGET = date(2026, 9, 11)


class CalendarClient:
    def __init__(self, *, fail=False, closed=False, empty=False, missing=False):
        self.fail, self.closed, self.empty, self.missing = fail, closed, empty, missing
        self.calls = []

    def query(self, operation, **parameters):
        self.calls.append((operation, parameters))
        response = base_result(operation, parameters)
        if self.fail:
            response.update(error_code="10002007", error_msg="网络接收错误")
            return response
        start, end = (date.fromisoformat(parameters[key]) for key in ("start_date", "end_date"))
        # Explicit fixture statuses, not weekday inference; production never
        # generates a calendar. Set only the target flag for the given scenario.
        rows = [{"calendar_date": (start + timedelta(days=i)).isoformat(), "is_trading_day": "1"}
                for i in range((end - start).days + 1)]
        rows[-1]["is_trading_day"] = "0" if self.closed else "1"
        if self.empty:
            rows = []
        if self.missing:
            rows = rows[:-1]
        response.update(ok=True, status="ok" if rows else "empty_confirmed", error_code="0", fields=CALENDAR_FIELDS,
                        rows=rows, raw_hash=raw_hash(CALENDAR_FIELDS, rows),
                        login={"ok": True, "error_code": "0", "error_msg": "success"})
        return response


def test_verified_calendar_saved_and_network_10002007_uses_covering_cache(tmp_path):
    current = resolve_calendar(TARGET, tmp_path, mode="offline_test", client=CalendarClient())
    cached = resolve_calendar(TARGET, tmp_path, mode="offline_test", client=CalendarClient(fail=True))
    assert current["calendar_verified"] and not current["cached"]
    assert cached["calendar_verified"] and cached["cached"]
    assert cached["response"]["error_code"] == "10002007"
    assert cached["resolved_trade_date"] == TARGET.isoformat()
    assert cached["source_response_hash"] == current["source_response_hash"]


@pytest.mark.parametrize("target", [date(2026, 9, 12), date(2026, 10, 1)])
def test_observed_weekend_or_holiday_is_closed_never_shifted_to_previous_date(tmp_path, target):
    result = resolve_calendar(target, tmp_path, mode="offline_test", client=CalendarClient(closed=True))
    assert result["status"] == "non_trading_day"
    assert result["calendar_verified"] is True
    assert result["resolved_trade_date"] is None
    assert result["requested_date"] == target.isoformat()


@pytest.mark.parametrize("client", [CalendarClient(fail=True), CalendarClient(empty=True), CalendarClient(missing=True)])
def test_unknown_empty_or_incomplete_calendar_never_becomes_closed(tmp_path, client):
    result = resolve_calendar(TARGET, tmp_path, mode="offline_test", client=client)
    assert result["status"] == "calendar_unverified"
    assert result["calendar_verified"] is False
    assert result["resolved_trade_date"] is None


def test_out_of_range_cache_is_not_extended_by_weekday_rules(tmp_path):
    resolve_calendar(TARGET, tmp_path, mode="offline_test", client=CalendarClient())
    result = resolve_calendar(TARGET + timedelta(days=1), tmp_path, mode="offline_test", client=CalendarClient(fail=True))
    assert result["status"] == "calendar_unverified"


def test_modified_cache_rejected_without_rewriting_original(tmp_path):
    first = resolve_calendar(TARGET, tmp_path, mode="offline_test", client=CalendarClient())
    path = next(tmp_path.glob("*.json"))
    path.write_text(path.read_text(encoding="utf-8").replace('"is_trading_day": "1"', '"is_trading_day": "0"'), encoding="utf-8")
    result = resolve_calendar(TARGET, tmp_path, mode="offline_test", client=CalendarClient(fail=True))
    assert result["status"] == "calendar_unverified"
    assert result["cache_rejections"]


def test_datetime_must_be_resolved_in_shanghai_before_calling(tmp_path):
    # 16:30 UTC is the next date in China; accepting .date() implicitly here
    # would permit a caller to resolve the wrong exchange date.
    instant = datetime(2026, 9, 10, 16, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="Asia/Shanghai"):
        resolve_calendar(instant, tmp_path, mode="offline_test", client=CalendarClient())


def test_test_client_requires_explicit_mode_and_research_path_is_protected(tmp_path):
    with pytest.raises(ValueError, match="offline_test"):
        resolve_calendar(TARGET, tmp_path, client=CalendarClient())
    with pytest.raises(ValueError, match="research"):
        resolve_calendar(TARGET, tmp_path / "research", client=CalendarClient(), mode="offline_test")


def test_research_rejects_hash_valid_offline_cache(tmp_path, monkeypatch):
    from ashare_daily.providers.baostock import BaoStockClient
    resolve_calendar(TARGET, tmp_path, client=CalendarClient(), mode="offline_test")
    monkeypatch.setattr(BaoStockClient, "query", CalendarClient(fail=True).query)
    result = resolve_calendar(TARGET, tmp_path)
    assert result["calendar_verified"] is False
    assert result["cache_rejections"]


def test_verified_legacy_calendar_is_reused_read_only_and_corrupt_row_blocked(tmp_path):
    from ashare_daily.storage.market import MarketStore
    from ashare_daily.quality.baostock import normalize_calendar
    from ashare_daily.providers.baostock import SHANGHAI
    database = tmp_path / "legacy.sqlite3"
    store = MarketStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO market_metadata VALUES ('verification_kind','offline_test')")
    rows = [{"calendar_date": TARGET.isoformat(), "is_trading_day": "1"}]
    store.store_calendar(normalize_calendar(rows, start_date=TARGET, end_date=TARGET,
        fetched_at=datetime(2026, 9, 10, 21, tzinfo=SHANGHAI), sdk_version="fixture-offline"))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = resolve_calendar(TARGET, tmp_path / "cache", client=CalendarClient(fail=True),
        mode="offline_test", legacy_database=database)
    assert result["calendar_verified"] and result["cached"]
    assert result["source_kind"] == "legacy_sqlite_calendar_row"
    assert result["response"]["error_code"] == "10002007"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE trading_calendar SET is_trading_day=0")
    invalid = resolve_calendar(TARGET, tmp_path / "cache", client=CalendarClient(fail=True),
        mode="offline_test", legacy_database=database)
    assert not invalid["calendar_verified"]
    assert invalid["cache_rejections"]


def test_research_rejects_legacy_database_marked_offline_test(tmp_path, monkeypatch):
    from ashare_daily.storage.market import MarketStore
    from ashare_daily.providers.baostock import BaoStockClient
    database = tmp_path / "legacy.sqlite3"
    MarketStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO market_metadata VALUES ('verification_kind','offline_test')")
    monkeypatch.setattr(BaoStockClient, "query", CalendarClient(fail=True).query)
    result = resolve_calendar(TARGET, tmp_path / "cache", legacy_database=database)
    assert not result["calendar_verified"]
    assert "真实/测试" in result["cache_rejections"][0]["reason"]


def _write_cache(path, *, fetched_at, closed):
    from ashare_daily.calendar import _digest
    response = CalendarClient(closed=closed).query("calendar", start_date=(TARGET - timedelta(days=40)).isoformat(), end_date=TARGET.isoformat())
    response["fetched_at"] = fetched_at
    packet = {"schema_version": "f1-calendar-cache-v1", "provider": "baostock", "mode": "offline_test",
              "first_seen_at": fetched_at, "response": response}
    packet["content_hash"] = _digest(packet)
    path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")


def test_latest_source_revision_wins_over_cache_filename_order(tmp_path):
    _write_cache(tmp_path / "zzz-older.json", fetched_at="2026-09-09T10:00:00+08:00", closed=False)
    _write_cache(tmp_path / "aaa-newer.json", fetched_at="2026-09-10T10:00:00+08:00", closed=True)
    result = resolve_calendar(TARGET, tmp_path, client=CalendarClient(fail=True), mode="offline_test")
    assert result["status"] == "non_trading_day"
    assert result["source_fetched_at"] == "2026-09-10T10:00:00+08:00"
    assert result["cache_path"].endswith("aaa-newer.json")


def test_same_time_conflicting_calendar_revisions_remain_blocked(tmp_path):
    for name, closed in (("aaa", True), ("zzz", False)):
        _write_cache(tmp_path / f"{name}.json", fetched_at="2026-09-10T10:00:00+08:00", closed=closed)
    result = resolve_calendar(TARGET, tmp_path, client=CalendarClient(fail=True), mode="offline_test")
    assert result["status"] == "calendar_unverified"
    assert result["calendar_verified"] is False
    assert result["cache_conflict"] is True
    assert len(result["cache_rejections"]) == 2


def test_future_source_timestamp_is_not_a_valid_calendar_cache(tmp_path):
    _write_cache(tmp_path / "future.json", fetched_at="2099-09-10T10:00:00+08:00", closed=False)
    result = resolve_calendar(TARGET, tmp_path, client=CalendarClient(fail=True), mode="offline_test")
    assert result["status"] == "calendar_unverified"
    assert "未来" in result["cache_rejections"][0]["reason"]
