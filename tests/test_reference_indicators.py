"""Independent numerical and boundary checks for the reference research subset."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
import math

import pandas as pd
import pytest

from ashare_daily.reference_indicators import _divergence, score_reference_technical


def bars(prices=None):
    prices = prices if prices is not None else [100 + index for index in range(120)]
    # These dates are synthetic fixture labels; production calendar is caller-owned.
    return [{"trade_date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
             "close": str(price), "high": str(Decimal(str(price)) + 1),
             "low": str(Decimal(str(price)) - 1), "volume_shares": 100}
            for index, price in enumerate(prices)]


def points(result, component):
    return next(item["points"] for item in result["components"] if item["id"] == component)


def test_hand_computed_linear_rise_has_seven_components_and_score_61():
    result = score_reference_technical(bars())
    assert result["status"] == "available"
    assert result["score"] == 61  # 50 + 12 + 3 - 4 - 4 + 0 + 0 + 4.
    assert len(result["components"]) == 7 and result["issues"] == []
    assert {key: Decimal(result["indicators"][key]) for key in ("ma5", "ma10", "ma20", "ma60")} == {
        "ma5": Decimal("217"), "ma10": Decimal("214.5"),
        "ma20": Decimal("209.5"), "ma60": Decimal("189.5"),
    }
    assert Decimal(result["indicators"]["rsi14"]) == 100
    assert Decimal(result["indicators"]["volume_ratio20"]) == 1
    assert result["parameters"]["window_bars"] == 120
    assert result["parameters"]["volume_mean_includes_current"] is True
    assert result["window"]["bar_count"] == 120


def test_hand_computed_linear_fall_does_not_invent_a_trend_or_cross():
    result = score_reference_technical(bars([220 - index for index in range(120)]))
    assert result["score"] == 51  # A rule score is explicitly not a return forecast.
    assert Decimal(result["indicators"]["rsi14"]) == 0
    assert result["indicators"]["macd_golden_cross"] is False
    assert points(result, "trend") == points(result, "macd") == 0


def test_matches_independent_pandas_ewm_and_rolling_reference_without_rounding():
    prices = [100 + index / 20 + math.sin(index / 3) * 4 + math.cos(index / 7) for index in range(120)]
    rows = bars(prices)
    for index, row in enumerate(rows):
        row["volume_shares"] = 100 + (index * 31) % 91
    result = score_reference_technical(rows)
    values = result["indicators"]
    close = pd.Series(prices)
    high, low = close + 1, close - 1
    dif = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
    signal = dif.ewm(span=9, adjust=False, min_periods=9).mean()
    diff = close.diff()
    up = diff.where(diff > 0, 0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    down = -diff.where(diff < 0, 0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + up / down)
    k = 100 * (close - low.rolling(14).min()) / (high.rolling(14).max() - low.rolling(14).min())
    d = k.rolling(3).mean()
    expected = {
        "macd_dif": dif.iloc[-1], "macd_signal": signal.iloc[-1],
        "macd_previous_dif": dif.iloc[-2], "macd_previous_signal": signal.iloc[-2],
        "rsi14": rsi.iloc[-1], "stochastic_k": k.iloc[-1],
        "stochastic_d": d.iloc[-1], "stochastic_j": 3 * k.iloc[-1] - 2 * d.iloc[-1],
        "volume_ratio20": rows[-1]["volume_shares"] / pd.Series([row["volume_shares"] for row in rows]).tail(20).mean(),
        "return20_pct": (close.iloc[-1] / close.iloc[-21] - 1) * 100,
    }
    for key, value in expected.items():
        assert float(values[key]) == pytest.approx(value, abs=1e-10), key


@pytest.mark.parametrize("count", [0, 119, 121])
def test_window_is_exact_not_silently_truncated(count):
    result = score_reference_technical(bars([100 + index for index in range(count)]))
    assert result["score"] is None and result["status"] == "unavailable"
    assert result["issues"] == ["exactly_120_bars_required"]


@pytest.mark.parametrize("value", [None, (), {}])
def test_non_list_input_is_rejected(value):
    assert score_reference_technical(value)["status"] == "unavailable"


@pytest.mark.parametrize("field,value", [
    ("close", None), ("close", 219), ("close", "NaN"), ("close", "Infinity"),
    ("close", "0"), ("close", "-1"), ("close", " 219"), ("close", "1e9999999"),
    ("high", "1"), ("low", "999"), ("volume_shares", "100"),
    ("volume_shares", True), ("volume_shares", -1), ("volume_shares", 1.5),
    ("trade_date", "2026-13-01"), ("trade_date", "20260430"),
])
def test_invalid_values_are_not_coerced_or_filled(field, value):
    rows = bars()
    rows[-1][field] = value
    result = score_reference_technical(rows)
    assert result["status"] == "unavailable" and result["score"] is None
    assert result["issues"] and result["components"] == []


@pytest.mark.parametrize("kind", ["duplicate", "reverse", "missing", "not_object"])
def test_input_structure_and_order_are_strict(kind):
    rows = bars()
    if kind == "duplicate":
        rows[-1]["trade_date"] = rows[-2]["trade_date"]
    elif kind == "reverse":
        rows.reverse()
    elif kind == "missing":
        del rows[47]["low"]
    else:
        rows[47] = None
    assert score_reference_technical(rows)["score"] is None


def test_flat_prices_leave_rsi_missing_instead_of_false_overbought():
    result = score_reference_technical(bars([100] * 120))
    assert result["status"] == "unavailable" and result["score"] is None
    assert result["indicators"]["rsi14"] is None
    assert "rsi_no_directional_changes" in result["issues"]


def test_zero_stochastic_range_is_not_replaced_with_50():
    rows = bars([100] * 120)
    for row in rows:
        row["high"] = row["low"] = row["close"]
    result = score_reference_technical(rows)
    assert result["indicators"]["stochastic_k"] is None
    assert result["indicators"]["stochastic_d"] is None
    assert result["indicators"]["stochastic_j"] is None
    assert "stochastic_zero_range" in result["issues"]
    assert result["score"] is None


def test_zero_range_before_final_bar_does_not_make_up_a_signal_average():
    rows = bars([100] * 119 + [101])
    for row in rows[:-1]:
        row["high"] = row["low"] = row["close"]
    result = score_reference_technical(rows)
    assert result["indicators"]["stochastic_k"] is not None
    assert result["indicators"]["stochastic_d"] is None
    assert result["score"] is None


def test_zero_volume_mean_is_unavailable_and_zero_today_remains_zero():
    rows = bars()
    for row in rows[-20:]:
        row["volume_shares"] = 0
    result = score_reference_technical(rows)
    assert result["score"] is None and "volume_mean_is_zero" in result["issues"]
    rows[-2]["volume_shares"] = 100
    result = score_reference_technical(rows)
    assert Decimal(result["indicators"]["volume_ratio20"]) == 0
    assert points(result, "volume") == -3


@pytest.mark.parametrize("prior,last,expected", [
    (37, 57, 4),  # 20*57 / (19*37+57) = 1.5 exactly.
    (37, 56, 0),
    (193, 133, 0),  # 20*133 / (19*193+133) = .7 exactly.
    (193, 132, -3),
])
def test_volume_boundaries_include_current_bar(prior, last, expected):
    rows = bars()
    for row in rows[-20:]:
        row["volume_shares"] = prior
    rows[-1]["volume_shares"] = last
    assert points(score_reference_technical(rows), "volume") == expected


@pytest.mark.parametrize("last,expected", [("105", 0), ("105.0000001", 4), ("92", 0), ("91.9999999", -5)])
def test_twenty_session_return_strict_boundaries_use_twenty_intervals(last, expected):
    rows = bars([100 + (index % 2) for index in range(120)])
    rows[-21].update(close="100", high="101", low="99")
    rows[-1].update(close=last, high=str(Decimal(last) + 1), low=str(Decimal(last) - 1))
    assert points(score_reference_technical(rows), "momentum") == expected


@pytest.mark.parametrize("last,expected", [("104", 0), ("103.9999999", 3), ("116", 0), ("116.0000001", -4)])
def test_stochastic_strict_boundaries_are_not_rounded(last, expected):
    rows = bars([110 + (index % 2) for index in range(120)])
    for row in rows[-16:]:
        row.update(high="120", low="100")
    rows[-1]["close"] = last
    assert points(score_reference_technical(rows), "stochastic") == expected


@pytest.mark.parametrize("gain_weight,loss_weight,expected", [(7, 3, 0), (3, 7, 0), ("7.000001", 3, -4), (3, "7.000001", 3)])
def test_rsi_strict_boundaries_without_display_rounding(gain_weight, loss_weight, expected):
    # With only two nonzero differences (+14a, -13b), Wilder RSI is 100a/(a+b).
    raised = Decimal(100) + 14 * Decimal(gain_weight)
    lowered = raised - 13 * Decimal(loss_weight)
    rows = bars([100] * 118 + [raised, lowered])
    assert points(score_reference_technical(rows), "rsi") == expected


def test_macd_golden_cross_and_above_zero_are_not_double_counted():
    prices = [Decimal(100) + Decimal(index) / 10 for index in range(110)]
    prices += [Decimal(111) - Decimal(index) * Decimal(".4") for index in range(9)] + [118]
    result = score_reference_technical(bars(prices))
    assert result["indicators"]["macd_golden_cross"] is True
    assert result["indicators"]["macd_above_zero"] is True
    assert points(result, "macd") == 5


def test_divergence_is_strict_and_uses_latest_two_confirmed_pivots():
    prices = list(map(Decimal, [100] * 120))
    macd = list(map(Decimal, [0] * 120))
    rsi = list(map(Decimal, [50] * 120))
    prices[95], prices[103], prices[108], prices[115] = map(Decimal, [110, 112, 90, 88])
    macd[95], macd[103], macd[108], macd[115] = map(Decimal, [5, 4, -5, -4])
    rsi[95], rsi[103], rsi[108], rsi[115] = map(Decimal, [75, 70, 25, 30])
    signals, issues = _divergence(prices, macd, rsi, [95, 103], [108, 115])
    assert issues == []
    assert signals == {"macd_bottom": True, "rsi_bottom": True, "macd_top": True, "rsi_top": True}
    prices[103] = prices[95]
    prices[115] = prices[108]
    signals, issues = _divergence(prices, macd, rsi, [95, 103], [108, 115])
    assert not any(signals.values())  # Equality never becomes divergence.


def test_both_divergence_directions_stack_once_with_independent_rsi_check():
    prices = [100 + index % 2 for index in range(90)] + [
        97, 102, 99, 97, 92, 95, 97, 102, 105, 110, 112, 107, 110, 111,
        108, 109, 114, 109, 106, 103, 101, 103, 98, 95, 93, 91, 93, 94, 95, 97,
    ]
    # Visually confirmed lows are at 94 and 115; highs at 100 and 106.
    changes = pd.Series(prices).diff()
    gains = changes.where(changes > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    losses = -changes.where(changes < 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gains / losses)
    assert prices[115] < prices[94] and rsi.iloc[115] > rsi.iloc[94]
    assert prices[106] > prices[100] and rsi.iloc[106] < rsi.iloc[100]
    result = score_reference_technical(bars(prices))
    assert result["indicators"]["rsi_bottom"] is True
    assert result["indicators"]["rsi_top"] is True
    assert points(result, "divergence") == -1  # +5 and -6, each direction only once.
    assert result["score"] == 44


def test_missing_rsi_at_a_relevant_pivot_is_an_explicit_gap():
    prices = [Decimal(100)] * 120
    macd = [Decimal(0)] * 120
    rsi = [Decimal(50)] * 120
    prices[95], prices[103] = Decimal(101), Decimal(102)
    rsi[95] = None
    signals, issues = _divergence(prices, macd, rsi, [95, 103], [])
    assert issues == ["rsi_top_divergence_unavailable"]
    assert signals["rsi_top"] is False


def test_last_three_unconfirmed_extremes_do_not_change_pivot_evidence():
    prices = [100 + (index % 11) for index in range(120)]
    rows = bars(prices)
    before = score_reference_technical(rows)
    for offset in range(1, 4):
        rows[-offset].update(close=str(1000 + offset), high=str(1001 + offset), low=str(999 + offset))
    after = score_reference_technical(rows)
    # These final bars may confirm/deny older pivots, but are never themselves pivots.
    for field in ("high_dates", "low_dates"):
        assert all(value < rows[-3]["trade_date"] for value in after["divergence_pivots"][field])
    rows = bars(prices)
    rows[-1].update(close="10000", high="10001", low="9999")
    last_extreme = score_reference_technical(rows)
    assert rows[-1]["trade_date"] not in last_extreme["divergence_pivots"]["high_dates"]
    assert before["window"] == last_extreme["window"]


def test_final_day_jump_changes_indicators_immediately_not_a_shifted_window():
    rows = bars()
    rows[-1].update(close="1000", high="1001", low="999")
    result = score_reference_technical(rows)
    assert Decimal(result["indicators"]["ma5"]) == Decimal("373.2")
    assert Decimal(result["indicators"]["close"]) == 1000
    assert Decimal(result["indicators"]["stochastic_k"]) > 99


def test_pure_deterministic_and_independent_of_ambient_decimal_context():
    rows = bars()
    saved = deepcopy(rows)
    expected = score_reference_technical(rows)
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        actual = score_reference_technical(rows)
    assert actual == expected
    assert rows == saved
    expected["parameters"]["ma_periods"].clear()
    assert score_reference_technical(rows)["parameters"]["ma_periods"] == [5, 10, 20, 60]
