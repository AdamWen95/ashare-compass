"""M1 离线单元测试：字符串均为手工构造，不构成联网证据。"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ashare_daily.market_schemas import AdjustmentMode
from ashare_daily.quality.baostock import DataQualityError, normalize_bars, normalize_calendar, normalize_instrument


FETCHED = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
DAY = date(2026, 9, 8)


def basic_row(symbol="sh.600000", security_type="1"):
    return {"code": symbol, "code_name": "单元测试合成证券", "ipoDate": "1999-11-10", "outDate": "", "type": security_type, "status": "1"}


def instrument(symbol="sh.600000", security_type="stock", fetched_at=FETCHED):
    return normalize_instrument(basic_row(symbol, "1" if security_type == "stock" else "2"),
                                expected_symbol=symbol, expected_type=security_type, fetched_at=fetched_at, sdk_version="0.9.3")


def bar_row(**updates):
    row = {"date": DAY.isoformat(), "code": "sh.600000", "open": "10.0000", "high": "11.0000", "low": "9.0000",
           "close": "10.5000", "preclose": "10.0000", "volume": "123456", "amount": "1234567.8900", "adjustflag": "3",
           "tradestatus": "1", "isST": "0", "turn": "1.25", "pctChg": "5.00"}
    row.update(updates)
    return row


def normalized(rows=None, **kwargs):
    return normalize_bars([bar_row()] if rows is None else rows,
                          instrument=kwargs.pop("instrument", instrument()),
                          start_date=DAY, end_date=DAY, trading_dates={DAY}, fetched_at=FETCHED, sdk_version="0.9.3", **kwargs)


def test_values_units_adjustment_and_timezone():
    bar = normalized()[0]
    assert bar.volume_shares == 123456  # 官方成交量为股，不额外乘 100。
    assert bar.amount_cny == Decimal("1234567.8900")
    assert bar.turnover_ratio == Decimal("0.0125")
    assert bar.pct_change_ratio == Decimal("0.05")
    assert bar.adjustment_mode is AdjustmentMode.UNADJUSTED
    assert bar.fetched_at.hour == 20
    assert str(bar.fetched_at.tzinfo) == "Asia/Shanghai"
    assert bar.price_unit == "CNY"
    assert bar.first_seen_at == bar.fetched_at
    assert bar.parameters == {"code": "sh.600000", "start_date": "2026-09-08", "end_date": "2026-09-08", "frequency": "d", "adjustflag": "3"}


@pytest.mark.parametrize("updates", [
    {"code": "sz.000001"}, {"date": "2026-09-07"}, {"date": "2026/09/08"},
    {"adjustflag": "1"}, {"adjustflag": "2"}, {"adjustflag": ""},
    {"volume": "-1"}, {"volume": "1.5"}, {"amount": "-1"},
    {"close": "NaN"}, {"open": "Inf"}, {"low": "0"}, {"high": "8"},
    {"tradestatus": ""}, {"isST": ""}, {"isST": "2"},
    {"tradestatus": "0"}, {"turn": "-1"},
])
def test_invalid_bar_rejected(updates):
    with pytest.raises(DataQualityError):
        normalized([bar_row(**updates)])


def test_missing_values_stay_null_and_are_flagged():
    bar = normalized([bar_row(open="", amount="", volume="", turn="", pctChg="")])[0]
    assert bar.open is None and bar.amount_cny is None and bar.volume_shares is None
    assert bar.turnover_ratio is None and bar.pct_change_ratio is None
    assert set(bar.quality_flags) == {"missing_open", "missing_amount_cny", "missing_volume_shares"}


def test_index_status_absence_is_not_assumed_normal_stock():
    bar = normalized([bar_row(code="sh.000001", tradestatus="", isST="", turn="")], instrument=instrument("sh.000001", "index"))[0]
    assert bar.price_unit == "index_points"
    assert bar.tradestatus is None and bar.is_st is None
    assert not bar.quality_flags


def test_index_cannot_borrow_stock_status():
    with pytest.raises(DataQualityError, match="指数"):
        normalized([bar_row(code="sh.000001")], instrument=instrument("sh.000001", "index"))


def test_index_official_shape_omits_stock_only_fields():
    row = bar_row(code="sh.000001")
    for field in ("adjustflag", "tradestatus", "isST", "turn"):
        row.pop(field)
    bar = normalized([row], instrument=instrument("sh.000001", "index"))[0]
    assert bar.adjustment_mode is AdjustmentMode.UNADJUSTED
    assert bar.tradestatus is None and bar.is_st is None
    assert bar.price_unit == "index_points"
    assert not bar.quality_flags


def test_index_rejects_unexpected_explicit_adjusted_flag():
    with pytest.raises(DataQualityError, match="复权"):
        normalized([bar_row(code="sh.000001", tradestatus="", isST="", adjustflag="1")], instrument=instrument("sh.000001", "index"))


def test_empty_response_does_not_fabricate_rows():
    assert normalized([]) == []


def test_duplicate_response_is_explicit_error():
    with pytest.raises(DataQualityError, match="重复"):
        normalized([bar_row(), bar_row()])


def test_suspended_and_st_are_source_states():
    bar = normalized([bar_row(tradestatus="0", isST="1", volume="0", amount="0")])[0]
    assert bar.tradestatus is False and bar.is_st is True
    assert bar.volume_shares == 0 and bar.amount_cny == 0


def test_prices_date_must_be_on_supplier_calendar():
    with pytest.raises(DataQualityError, match="交易日历"):
        normalize_bars([bar_row()], instrument=instrument(), start_date=DAY, end_date=DAY,
                       trading_dates=set(), fetched_at=FETCHED, sdk_version="0.9.3")


@pytest.mark.parametrize("symbol,kind,updates", [
    ("sh.600000", "stock", {"type": "2"}),
    ("sh.600000", "stock", {"code": "sz.000001"}),
    ("sh.600000", "stock", {"code_name": ""}),
    ("sh.600000", "stock", {"status": ""}),
    ("sh.600000", "stock", {"ipoDate": "invalid"}),
    ("sh.600000", "stock", {"ipoDate": ""}),
    ("sh.600000", "stock", {"outDate": "1990-01-01"}),
    ("sh.900901", "stock", {}), ("sz.200001", "stock", {}),
    ("sh.688001", "stock", {}), ("sz.300001", "stock", {}),
    ("sh.510300", "stock", {}),
])
def test_identity_rejected(symbol, kind, updates):
    row = basic_row(symbol)
    row.update(updates)
    with pytest.raises(DataQualityError):
        normalize_instrument(row, expected_symbol=symbol, expected_type=kind, fetched_at=FETCHED, sdk_version="0.9.3")


def test_calendar_uses_provider_flag_even_on_weekday():
    rows = [{"calendar_date": "2026-10-01", "is_trading_day": "0"}, {"calendar_date": "2026-10-02", "is_trading_day": "0"}]
    days = normalize_calendar(rows, start_date=date(2026, 10, 1), end_date=date(2026, 10, 2), fetched_at=FETCHED, sdk_version="0.9.3")
    assert not any(day.is_trading_day for day in days)


@pytest.mark.parametrize("rows", [[], [{"calendar_date": "2026-09-08", "is_trading_day": "2"}],
    [{"calendar_date": "2026-09-08", "is_trading_day": "1"}] * 2,
    [{"calendar_date": "2026-09-09", "is_trading_day": "1"}],
])
def test_invalid_or_incomplete_calendar(rows):
    with pytest.raises(DataQualityError):
        normalize_calendar(rows, start_date=DAY, end_date=DAY, fetched_at=FETCHED, sdk_version="0.9.3")


def test_naive_fetch_timestamp_rejected():
    with pytest.raises(DataQualityError, match="时区"):
        normalize_instrument(basic_row(), expected_symbol="sh.600000", expected_type="stock", fetched_at=datetime(2026, 9, 9), sdk_version="0.9.3")
