"""M1 存储离线测试；原始值为手工合成，未执行网络。"""

import csv
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3

import pytest

from ashare_daily.quality.baostock import normalize_bars, normalize_calendar, normalize_instrument
from ashare_daily.storage.market import MarketStore


DAY = date(2026, 9, 8)
FETCHED = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def build_instrument(symbol="sh.600000", kind="stock", **updates):
    row = {"code": symbol, "code_name": "单元测试合成证券", "ipoDate": "1999-11-10", "outDate": "", "type": "1" if kind == "stock" else "2", "status": "1"}
    row.update(updates)
    return normalize_instrument(row, expected_symbol=symbol, expected_type=kind, fetched_at=FETCHED, sdk_version="0.9.3")


def build_bars(day=DAY, fetched_at=FETCHED, inst=None, **updates):
    inst = inst or build_instrument()
    row = {"date": day.isoformat(), "code": inst.symbol, "open": "10", "high": "11", "low": "9", "close": "10.5", "preclose": "10",
           "volume": "100", "amount": "1000", "adjustflag": "3", "tradestatus": "1" if inst.security_type == "stock" else "",
           "isST": "0" if inst.security_type == "stock" else ""}
    row.update(updates)
    return normalize_bars([row], instrument=inst, start_date=day, end_date=day, trading_dates={day}, fetched_at=fetched_at, sdk_version="0.9.3")


def save_calendar(store, start=DAY, end=DAY, nontrading=()):
    rows = [{"calendar_date": (start + timedelta(days=i)).isoformat(), "is_trading_day": "0" if start + timedelta(days=i) in nontrading else "1"}
            for i in range((end - start).days + 1)]
    store.store_calendar(normalize_calendar(rows, start_date=start, end_date=end, fetched_at=FETCHED, sdk_version="0.9.3"))


@pytest.fixture
def store(tmp_path):
    store = MarketStore(tmp_path / "research" / "market.sqlite3")
    store.store_instrument(build_instrument())
    save_calendar(store)
    return store


def test_repeated_insert_updates_fetch_time_without_duplication(store):
    assert store.store_bars(build_bars()) == {"inserted": 1, "updated": 0, "unchanged": 0}
    later = FETCHED + timedelta(days=1)
    assert store.store_bars(build_bars(fetched_at=later)) == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert store.row_count() == 1
    bar = store.read_bars(["sh.600000"], DAY, DAY)[0]
    assert bar.fetched_at == later and bar.first_seen_at == FETCHED
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_bar_versions").fetchone()[0] == 1


def test_revised_content_keeps_prior_snapshot(store):
    original = build_bars()[0]
    store.store_bars([original])
    assert store.store_bars(build_bars(fetched_at=FETCHED + timedelta(hours=1), close="10.75"))["updated"] == 1
    assert store.row_count() == 1
    with sqlite3.connect(store.path) as connection:
        versions = connection.execute("SELECT raw_hash,payload_json FROM daily_bar_versions").fetchall()
    assert len(versions) == 2
    assert json.loads(dict(versions)[original.raw_hash])["close"] == "10.5"


def test_empty_write_returns_zero_and_no_fake_rows(store):
    assert store.store_bars([]) == {"inserted": 0, "updated": 0, "unchanged": 0}
    assert store.row_count() == 0


def test_batch_duplicate_rejected_without_writes(store):
    with pytest.raises(ValueError, match="重复"):
        store.store_bars(build_bars() * 2)
    assert store.row_count() == 0


def test_symbol_batch_atomic_on_missing_calendar(store):
    tomorrow = DAY + timedelta(days=1)
    with pytest.raises(ValueError, match="交易日历"):
        store.store_bars([*build_bars(), *build_bars(day=tomorrow)])
    assert store.row_count() == 0


def test_prevent_older_fetch_overwriting_newer_data(store):
    store.store_bars(build_bars())
    with pytest.raises(ValueError, match="较新"):
        store.store_bars(build_bars(fetched_at=FETCHED - timedelta(days=1), close="10.75"))
    assert str(store.read_bars(["sh.600000"], DAY, DAY)[0].close) == "10.5"


def test_unknown_instrument_cannot_enter_database(store):
    with pytest.raises(ValueError, match="证券身份"):
        store.store_bars(build_bars(inst=build_instrument("sz.000001")))
    assert store.row_count() == 0


def test_preserves_incremental_date_lookup(store):
    store.store_bars(build_bars())
    assert store.stored_dates("sh.600000", DAY - timedelta(days=10), DAY + timedelta(days=10)) == {DAY}
    assert store.stored_dates("sz.000001", DAY, DAY) == set()


def test_gap_is_unknown_not_assumed_suspension(store):
    coverage = store.coverage(["sh.600000"], DAY, DAY, DAY)
    assert coverage["unknown_missing_count"] == 1
    assert coverage["status"] == "partial"
    assert coverage["target_data_status"] == "stale_or_missing"
    assert coverage["actual_latest_data_date"] is None
    assert not coverage["symbol_results"][0]["suspension_dates"]


def test_complete_scope_is_not_full_market(store):
    store.store_bars(build_bars())
    coverage = store.coverage(["sh.600000"], DAY, DAY, DAY)
    assert coverage["status"] == "complete_within_scope"
    assert coverage["target_data_status"] == "current"
    assert coverage["expected_count"] == coverage["covered_count"] == 1
    assert coverage["market_coverage_verified"] is False
    assert "小样本验证" in coverage["scope_notice"]


def test_null_cells_make_quality_incomplete(store):
    store.store_bars(build_bars(volume="", amount=""))
    coverage = store.coverage(["sh.600000"], DAY, DAY)
    assert coverage["status"] == "partial"
    assert coverage["quality_issue_count"] == 1
    assert coverage["covered_count"] == 1


def test_explicit_suspension_explained_but_missing_row_unknown(store):
    store.store_bars(build_bars(tradestatus="0", volume="0", amount="0"))
    coverage = store.coverage(["sh.600000"], DAY, DAY)
    assert coverage["status"] == "complete_within_scope"
    assert coverage["symbol_results"][0]["suspension_dates"] == [DAY.isoformat()]


def test_missing_calendar_never_uses_weekdays(store):
    coverage = store.coverage(["sh.600000"], DAY, DAY + timedelta(days=1))
    assert coverage["missing_calendar_dates"] == ["2026-09-09"]
    assert coverage["status"] == "partial"


def test_new_listing_uses_actual_ipo_date_not_status_guess(store):
    store.store_instrument(build_instrument(ipoDate="2026-09-09"))
    coverage = store.coverage(["sh.600000"], DAY, DAY)
    assert coverage["expected_count"] == 0
    assert coverage["explained_count"] == 1
    assert coverage["symbol_results"][0]["explained_dates"] == [{"date": DAY.isoformat(), "reason": "before_ipo"}]


def test_delisted_status_without_actual_out_date_does_not_erase_missing(store):
    store.store_instrument(build_instrument(status="0"))
    coverage = store.coverage(["sh.600000"], DAY, DAY)
    assert coverage["expected_count"] == 1
    assert coverage["unknown_missing_count"] == 1


def test_nontrading_calendar_status(store):
    day = date(2026, 10, 1)
    save_calendar(store, day, day, [day])
    coverage = store.coverage(["sh.600000"], day, day, day)
    assert coverage["expected_count"] == 0
    assert coverage["target_data_status"] == "non_trading_day"


def test_index_absent_stock_status_does_not_create_false_unknown(store):
    index = build_instrument("sh.000001", "index")
    store.store_instrument(index)
    store.store_bars(build_bars(inst=index))
    coverage = store.coverage([index.symbol], DAY, DAY)
    assert coverage["status"] == "complete_within_scope"
    assert coverage["symbol_results"][0]["status_unknown_dates"] == []


def test_csv_has_bom_units_provenance_scope_and_empty_nulls(store, tmp_path):
    store.store_bars(build_bars(amount=""))
    csv_path = tmp_path / "sample.csv"
    assert store.export_csv(csv_path, ["sh.600000"], DAY, DAY) == 1
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["amount_cny"] == ""
    assert rows[0]["volume_shares"] == "100"
    assert rows[0]["adjustment_mode"] == "unadjusted"
    assert rows[0]["mode"] == "research"
    assert "小样本验证" in rows[0]["scope_notice"]


def test_reject_demo_db_and_unmanaged_database_without_mutation(tmp_path):
    for name, table in [("unmarked.sqlite3", "demo_reports"), ("other.sqlite3", "unrelated")]:
        path = tmp_path / name
        with sqlite3.connect(path) as connection:
            connection.execute(f"CREATE TABLE {table} (value TEXT)")
        original = path.read_bytes()
        with pytest.raises(ValueError):
            MarketStore(path)
        assert path.read_bytes() == original
    with pytest.raises(ValueError, match="DEMO"):
        MarketStore(tmp_path / "demo" / "market.sqlite3")


def test_separate_connection_reads_persisted_state(store):
    store.store_bars(build_bars())
    reopened = MarketStore(store.path)
    assert reopened.row_count() == 1
    assert reopened.calendar_dates(DAY, DAY) == {DAY: True}
    assert reopened.get_instrument("sh.600000").symbol == "sh.600000"
