"""Independent hand-calculable fixtures; dates are a declared synthetic calendar."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from ashare_daily.factors.trend import mean_window, number, period_return
from ashare_daily.screening.engine import evaluate_snapshot
from ashare_daily.screening.settings import StrategyConfig


@pytest.fixture
def input_snapshot():
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(120)]
    config = StrategyConfig(strategy_version="hand-calculated-v1", benchmark_id="sh.000001", benchmark_name="OFFLINE TEST INDEX")
    sample_types = {"sh.000001": "index", "sh.600000": "stock"}
    instruments, raw, series = [], [], {}
    for symbol, kind in sample_types.items():
        instruments.append({"symbol": symbol, "name": "OFFLINE TEST " + symbol, "security_type": kind,
                            "board": "index" if kind == "index" else "mainboard", "exchange": "SH",
                            "ipo_date": "2000-01-01", "out_date": None, "status": "listed"})
        adjusted = []
        for i, day in enumerate(days):
            close = str(i + 1) if kind == "stock" else "100"
            bar = {"symbol": symbol, "trade_date": day, "close": close, "preclose": close,
                   "amount_cny": "50000000", "amount_unit": "CNY", "volume_shares": 5000,
                   "price_unit": "CNY" if kind == "stock" else "index_points",
                   "tradestatus": True if kind == "stock" else None, "is_st": False if kind == "stock" else None,
                   "adjustment_mode": "unadjusted", "quality_flags": []}
            raw.append(bar)
            adjusted.append({"trade_date": day, "close": close})
        series[symbol] = {"provider": "baostock", "security_type": kind, "fetch_version": "OFFLINE TEST ONE RESPONSE",
                          "adjustment_mode": "forward_adjusted" if kind == "stock" else "index_native",
                          "price_unit": "CNY" if kind == "stock" else "index_points", "bars": adjusted, "issues": []}
    return {"snapshot_id": "OFFLINE TEST FIXED INPUT", "verification_kind": "offline_test", "trade_date": days[-1],
            "strategy_config": config.model_dump(mode="json"), "sample_types": sample_types, "trading_dates": days,
            "target_is_trading": True, "calendar_issues": [], "instruments": instruments, "raw_bars": raw,
            "adjusted_data": {"series": series}, "source_issues": {},
            "eligibility_states": {"sh.600000": {"delisting_period": False, "effective_date": days[-1], "evidence_id": "OFFLINE TEST STATUS EVIDENCE"}}}


def stock_result(snapshot):
    return evaluate_snapshot(snapshot)["evaluations"][0]


def condition(row, name):
    return next(value for value in row["conditions"] if value["id"] == name)["status"]


def test_hand_calculated_120_point_golden(input_snapshot):
    report = evaluate_snapshot(input_snapshot)
    row = report["evaluations"][0]
    # Arithmetic series: last 20 are 101..120; last 60 are 61..120.
    assert Decimal(row["ma_short"]) == Decimal("110.5")
    assert Decimal(row["ma_long"]) == Decimal("90.5")
    assert Decimal(row["period_return"]) == Decimal("0.2")  # 120/100 - 1
    assert Decimal(row["benchmark_period_return"]) == 0
    assert Decimal(row["relative_return"]) == Decimal("0.2")
    assert Decimal(row["avg_amount_cny"]) == Decimal("50000000")
    assert row["valid_history_count"] == 120 and row["status"] == "candidate"
    assert report["candidates"][0]["rank"] == 1
    assert report["counts"]["stock_count"] == 1
    assert report["non_stock_records"][0]["symbol"] == "sh.000001"


@pytest.mark.parametrize("count,expected", [(20, None), (21, Decimal("1")), (22, Decimal("0.5"))])
def test_return_requires_exactly_21_points(count, expected):
    values = ["100"] + ["200"] * 20 + ["300"]
    assert period_return(values[:count], 20) == expected


def test_return_missing_interior_not_just_endpoints():
    values = [100] * 21
    values[9] = None
    assert period_return(values, 20) is None


@pytest.mark.parametrize("window,expected", [(20, "110.5"), (60, "90.5")])
def test_mean_uses_last_exact_window(window, expected):
    assert mean_window(range(1, 121), window) == Decimal(expected)
    assert mean_window(range(window - 1), window) is None


@pytest.mark.parametrize("bad", [None, "NaN", "Infinity", "-Infinity", "bad", True, -1, 0])
def test_invalid_price_never_zero_or_forward_filled(bad):
    points = [100] * 60
    points[-10] = bad
    assert mean_window(points, 60) is None
    assert period_return(points, 20) is None


@pytest.mark.parametrize("value,expected", [("50000000", "pass"), ("49999999.99", "fail"), ("50000000.01", "pass"), ("5000", "fail")])
def test_amount_is_yuan_and_inclusive_threshold(input_snapshot, value, expected):
    for bar in input_snapshot["raw_bars"]:
        if bar["symbol"] == "sh.600000":
            bar["amount_cny"] = value
    row = stock_result(input_snapshot)
    assert condition(row, "liquidity") == expected
    assert Decimal(row["avg_amount_cny"]) == Decimal(value)


def test_wrong_amount_unit_is_unknown(input_snapshot):
    input_snapshot["raw_bars"][-1]["amount_unit"] = "ten_thousand_CNY"
    row = stock_result(input_snapshot)
    assert row["avg_amount_cny"] is None
    assert condition(row, "liquidity") == "unknown"
    assert row["status"] == "data_insufficient"


@pytest.mark.parametrize("case", ["close_equals_short", "short_equals_long", "relative_zero"])
def test_strict_trend_and_relative_thresholds(input_snapshot, case):
    values = input_snapshot["adjusted_data"]["series"]["sh.600000"]["bars"]
    if case == "close_equals_short":
        for bar in values[-20:]:
            bar["close"] = "120"
    elif case == "short_equals_long":
        for bar in values[-60:]:
            bar["close"] = "100"
    else:
        for stock_bar, benchmark_bar in zip(values, input_snapshot["adjusted_data"]["series"]["sh.000001"]["bars"]):
            benchmark_bar["close"] = stock_bar["close"]
    row = stock_result(input_snapshot)
    assert condition(row, "relative_strength" if case == "relative_zero" else "trend") == "fail"
    assert row["status"] == "excluded"


@pytest.mark.parametrize("offset", [1, 10, 21])
def test_benchmark_dates_must_all_align(input_snapshot, offset):
    input_snapshot["adjusted_data"]["series"]["sh.000001"]["bars"].pop(-offset)
    row = stock_result(input_snapshot)
    assert row["benchmark_period_return"] is None and row["relative_return"] is None
    assert condition(row, "relative_strength") == "unknown"
    assert row["status"] == "data_insufficient"


def test_benchmark_raw_target_gap_is_reported(input_snapshot):
    input_snapshot["raw_bars"] = [bar for bar in input_snapshot["raw_bars"] if not (bar["symbol"] == "sh.000001" and bar["trade_date"] == input_snapshot["trade_date"])]
    report = evaluate_snapshot(input_snapshot)
    assert report["status"] == "partial"
    assert report["benchmark"]["display_close"] is None
    assert any("展示行情缺失" in issue for issue in report["benchmark"]["issues"])


@pytest.mark.parametrize("kind,board,exchange", [("index", "index", "SH"), ("etf", "fund", "SH"), ("stock", "B", "SZ"), ("stock", "star", "SH"), ("stock", "mainboard", "BJ")])
def test_non_target_instrument_cannot_pass(input_snapshot, kind, board, exchange):
    input_snapshot["instruments"][-1].update(security_type=kind, board=board, exchange=exchange)
    row = stock_result(input_snapshot)
    assert condition(row, "identity") == "fail"
    assert row["status"] == "excluded"


@pytest.mark.parametrize("field", ["security_type", "board", "exchange", "ipo_date", "status"])
def test_unknown_master_data_never_guessed(input_snapshot, field):
    input_snapshot["instruments"][-1][field] = None
    row = stock_result(input_snapshot)
    assert row["status"] == "data_insufficient"
    assert condition(row, "identity") == "unknown"


@pytest.mark.parametrize("field,value,condition_id,expected", [
    ("is_st", True, "not_st", "excluded"), ("tradestatus", False, "not_suspended", "excluded"),
    ("is_st", None, "not_st", "data_insufficient"), ("tradestatus", None, "not_suspended", "data_insufficient"),
])
def test_target_states(input_snapshot, field, value, condition_id, expected):
    input_snapshot["raw_bars"][-1][field] = value
    row = stock_result(input_snapshot)
    assert row["status"] == expected
    assert condition(row, condition_id) == ("unknown" if value is None else "fail")


@pytest.mark.parametrize("change", ["remove", "no_evidence", "wrong_date", "delisting"])
def test_delisting_requires_date_specific_evidence(input_snapshot, change):
    state = input_snapshot["eligibility_states"]["sh.600000"]
    if change == "remove":
        input_snapshot["eligibility_states"] = {}
    elif change == "no_evidence":
        state["evidence_id"] = None
    elif change == "wrong_date":
        state["effective_date"] = "2000-01-01"
    else:
        state["delisting_period"] = True
    row = stock_result(input_snapshot)
    assert row["status"] == ("excluded" if change == "delisting" else "data_insufficient")


def test_history_119_does_not_borrow_extra_points(input_snapshot):
    input_snapshot["adjusted_data"]["series"]["sh.600000"]["bars"].pop(0)
    row = stock_result(input_snapshot)
    assert row["valid_history_count"] == 119
    assert Decimal(row["ma_short"]) == Decimal("110.5")
    assert condition(row, "history") == "unknown"
    assert row["status"] == "data_insufficient"


def test_known_rule_fail_remains_distinct_from_missing_state(input_snapshot):
    input_snapshot["eligibility_states"] = {}
    for bar in input_snapshot["adjusted_data"]["series"]["sh.600000"]["bars"]:
        bar["close"] = "100"
    row = stock_result(input_snapshot)
    assert row["status"] == "excluded" and row["exclusion_reasons"] and row["data_issues"]
    assert condition(row, "not_delisting_period") == "unknown"


def test_sorting_uses_relative_amount_then_id_and_cap(input_snapshot):
    original = "sh.600000"
    for symbol, amount, last in [("sh.600036", "50000001", "120"), ("sh.601398", "50000001", "120"), ("sz.000001", "50000000", "130")]:
        input_snapshot["sample_types"][symbol] = "stock"
        instrument = deepcopy(input_snapshot["instruments"][1])
        instrument.update(symbol=symbol, exchange=symbol[:2].upper())
        input_snapshot["instruments"].append(instrument)
        raw = [dict(bar, symbol=symbol, amount_cny=amount) for bar in input_snapshot["raw_bars"] if bar["symbol"] == original]
        input_snapshot["raw_bars"].extend(raw)
        adjusted = deepcopy(input_snapshot["adjusted_data"]["series"][original])
        adjusted["bars"][-1]["close"] = last
        input_snapshot["adjusted_data"]["series"][symbol] = adjusted
        input_snapshot["eligibility_states"][symbol] = deepcopy(input_snapshot["eligibility_states"][original])
    input_snapshot["strategy_config"]["max_candidates"] = 3
    report = evaluate_snapshot(input_snapshot)
    assert [row["symbol"] for row in report["candidates"]] == ["sz.000001", "sh.600036", "sh.601398"]
    assert report["counts"]["qualified_not_selected_count"] == 1
    assert report["evaluations"][0]["status"] == "qualified_not_selected"


def test_configurable_windows_have_explicit_semantics(input_snapshot):
    input_snapshot["strategy_config"].update(ma_short_days=10, ma_long_days=30, return_days=5, amount_days=5)
    report = evaluate_snapshot(input_snapshot)
    row = report["evaluations"][0]
    assert Decimal(row["ma_short"]) == Decimal("115.5")
    assert Decimal(row["ma_long"]) == Decimal("105.5")
    with localcontext() as ctx:
        ctx.prec = 40
        assert Decimal(row["period_return"]) == Decimal(120) / Decimal(115) - 1
    assert report["metric_windows"] == {"ma_short": 10, "ma_long": 30, "period_return": 5, "avg_amount_cny": 5}


def test_future_input_cannot_change_frozen_t_result(input_snapshot):
    before = evaluate_snapshot(input_snapshot)
    changed = deepcopy(input_snapshot)
    for symbol in changed["sample_types"]:
        changed["raw_bars"].append({"symbol": symbol, "trade_date": "2026-01-01", "close": "99999999"})
        changed["adjusted_data"]["series"][symbol]["bars"].append({"trade_date": "2026-01-01", "close": "99999999"})
    after = evaluate_snapshot(changed)
    assert after == before
    # In addition to equality, independently fixed expected values still hold.
    assert Decimal(after["evaluations"][0]["ma_short"]) == Decimal("110.5")
    assert Decimal(after["evaluations"][0]["period_return"]) == Decimal("0.2")


@pytest.mark.parametrize("fault", ["mixed_mode", "no_version", "wrong_provider", "duplicate"])
def test_adjusted_series_integrity(input_snapshot, fault):
    series = input_snapshot["adjusted_data"]["series"]["sh.600000"]
    if fault == "mixed_mode":
        series["adjustment_mode"] = "unadjusted"
    elif fault == "no_version":
        series["fetch_version"] = None
    elif fault == "wrong_provider":
        series["provider"] = "other"
    else:
        series["bars"].append(deepcopy(series["bars"][-1]))
    row = stock_result(input_snapshot)
    assert row["ma_short"] is None and row["status"] == "data_insufficient"


def test_zero_candidates_fixed_formats(input_snapshot):
    from ashare_daily.reports.m2_render import render_m2_html, render_m2_markdown
    input_snapshot["eligibility_states"] = {}
    report = evaluate_snapshot(input_snapshot)
    report["actual_generated_at"] = "2025-05-01T12:00:00+08:00"
    assert report["candidates"] == []
    for rendered in (render_m2_html(report), render_m2_markdown(report)):
        assert "本期量价预候选为 0" in rendered
        assert "无法判断" in rendered and "OFFLINE TEST" in rendered
