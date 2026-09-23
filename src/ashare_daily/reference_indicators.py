"""Deterministic, independently implemented technical research score.

This is a seven-factor subset of a reviewed reference strategy, not an expected
return or a probability.  It uses one fixed 120-bar, already verified adjusted
price window; callers own trading-calendar, adjustment-version and provenance
checks.  No external source code, model output or network client is used here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Any


WINDOW_BARS = 120
SCHEMA = "reference-technical-score-v1"
PARAMETERS = {
    "window_bars": WINDOW_BARS,
    "base_score": 50,
    "ma_periods": [5, 10, 20, 60],
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "rsi_period": 14,
    "stochastic_period": 14,
    "stochastic_signal_period": 3,
    "divergence_lookback": 30,
    "swing_left_bars": 3,
    "swing_right_bars": 3,
    "volume_mean_period": 20,
    "volume_mean_includes_current": True,
    "return_period": 20,
    "decimal_precision": 40,
    "score_limits": [0, 100],
    "thresholds": {
        "stochastic_low_exclusive": "20", "stochastic_high_exclusive": "80",
        "rsi_low_exclusive": "30", "rsi_high_exclusive": "70",
        "volume_high_inclusive": "1.5", "volume_low_exclusive": "0.7",
        "return20_high_exclusive_pct": "5", "return20_low_exclusive_pct": "-8",
    },
    "points": {
        "long_alignment": 12, "macd_golden_cross": 5, "macd_above_zero_without_cross": 3,
        "stochastic_low": 3, "stochastic_high": -4, "rsi_low": 3, "rsi_high": -4,
        "any_bottom_divergence": 5, "any_top_divergence": -6,
        "high_volume_ratio": 4, "low_volume_ratio": -3,
        "high_return20": 4, "low_return20": -5,
    },
}
WARMUP = {
    "ema": "adjust=False; first close seeds EMA12/26; DIF starts at bar 26",
    "signal": "first valid DIF at bar 26 seeds EMA9; visible from bar 34",
    "rsi": "Wilder alpha=1/14, adjust=False; first difference is zero; visible from bar 14",
    "stochastic": "14-bar high/low K; D is 3-bar arithmetic K mean; J=3K-2D; no imputation",
    "divergence": "last 30 closes only; each pivot needs 3 observed bars on both sides; latest two of each kind",
    "window": "all recursive indicators restart from the first of exactly 120 bars",
}


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _unavailable(issues: list[str]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "score": None,
        "components": [],
        "indicators": {},
        "issues": issues,
        "parameters": deepcopy(PARAMETERS),
        "warmup": dict(WARMUP),
        "window": None,
        "divergence_pivots": {"high_dates": [], "low_dates": []},
    }


def _parse(bars: list[dict]) -> tuple[list[str], list[Decimal], list[Decimal], list[Decimal], list[Decimal]]:
    if not isinstance(bars, list) or len(bars) != WINDOW_BARS:
        raise ValueError("exactly_120_bars_required")
    dates: list[str] = []
    closes: list[Decimal] = []
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    volumes: list[Decimal] = []
    for index, row in enumerate(bars):
        if not isinstance(row, dict):
            raise ValueError(f"bar_{index}:object_required")
        observed_date = row.get("trade_date")
        try:
            if not isinstance(observed_date, str) or date.fromisoformat(observed_date).isoformat() != observed_date:
                raise ValueError
        except ValueError:
            raise ValueError(f"bar_{index}:invalid_trade_date") from None
        if dates and observed_date <= dates[-1]:
            raise ValueError(f"bar_{index}:dates_must_increase_without_duplicates")
        values: dict[str, Decimal] = {}
        for field in ("close", "high", "low"):
            raw = row.get(field)
            try:
                if not isinstance(raw, str) or not raw or len(raw) > 128 or raw.strip() != raw:
                    raise ValueError
                value = Decimal(raw)
                if not value.is_finite() or value <= 0 or abs(value.adjusted()) > 100:
                    raise ValueError
            except (ValueError, InvalidOperation):
                raise ValueError(f"bar_{index}:invalid_{field}") from None
            values[field] = value
        if not values["low"] <= values["close"] <= values["high"]:
            raise ValueError(f"bar_{index}:inconsistent_high_low_close")
        volume = row.get("volume_shares")
        if type(volume) is not int or volume < 0 or volume.bit_length() > 256:
            raise ValueError(f"bar_{index}:invalid_volume_shares")
        dates.append(observed_date)
        closes.append(values["close"])
        highs.append(values["high"])
        lows.append(values["low"])
        volumes.append(Decimal(volume))
    return dates, closes, highs, lows, volumes


def _ema(values: list[Decimal], alpha: Decimal) -> list[Decimal]:
    smoothed = [values[0]]
    for current in values[1:]:
        smoothed.append(alpha * current + (1 - alpha) * smoothed[-1])
    return smoothed


def _rsi(closes: list[Decimal]) -> list[Decimal | None]:
    changes = [Decimal(0)] + [current - previous for previous, current in zip(closes, closes[1:])]
    gains = _ema([max(change, Decimal(0)) for change in changes], Decimal(1) / 14)
    losses = _ema([max(-change, Decimal(0)) for change in changes], Decimal(1) / 14)
    values: list[Decimal | None] = []
    for index, (gain, loss) in enumerate(zip(gains, losses)):
        if index < 13 or gain + loss == 0:
            values.append(None)
        else:
            values.append(100 * gain / (gain + loss))
    return values


def _pivots(closes: list[Decimal]) -> tuple[list[int], list[int]]:
    high_points: list[int] = []
    low_points: list[int] = []
    start = len(closes) - PARAMETERS["divergence_lookback"]
    # The first/last three bars of this 30-bar slice cannot be confirmed pivots.
    for position in range(start + 3, len(closes) - 3):
        neighbourhood = closes[position - 3:position + 4]
        if closes[position] >= max(neighbourhood):
            high_points.append(position)
        if closes[position] <= min(neighbourhood):
            low_points.append(position)
    return high_points[-2:], low_points[-2:]


def _divergence(closes: list[Decimal], dif: list[Decimal], rsi: list[Decimal | None],
                highs: list[int], lows: list[int]) -> tuple[dict[str, bool], list[str]]:
    signals = {"macd_bottom": False, "rsi_bottom": False, "macd_top": False, "rsi_top": False}
    issues: list[str] = []
    for kind, positions in (("bottom", lows), ("top", highs)):
        if len(positions) < 2:
            continue
        earlier, later = positions
        price_moves_outward = closes[later] < closes[earlier] if kind == "bottom" else closes[later] > closes[earlier]
        if not price_moves_outward:
            continue
        for name, values in (("macd", dif), ("rsi", rsi)):
            first, second = values[earlier], values[later]
            if first is None or second is None:
                issues.append(f"{name}_{kind}_divergence_unavailable")
                continue
            signals[f"{name}_{kind}"] = second > first if kind == "bottom" else second < first
    return signals, issues


def score_reference_technical(bars: list[dict]) -> dict[str, Any]:
    """Return a reproducible seven-component technical score or explicit gaps.

    Prices must be finite positive decimal strings and volume nonnegative integer
    shares.  No sorting, truncating, calendar guessing, rounding before thresholds
    or filling missing observations occurs.  The caller must verify that the 120
    dates are the intended consecutive market sessions from one adjusted version.
    """
    result = _unavailable([])
    try:
        dates, closes, highs, lows, volumes = _parse(bars)
    except ValueError as exc:
        result["issues"] = [str(exc)]
        return result
    result["window"] = {"start_date": dates[0], "end_date": dates[-1], "bar_count": WINDOW_BARS}
    with localcontext(Context(prec=PARAMETERS["decimal_precision"])):
        averages = {period: sum(closes[-period:]) / period for period in PARAMETERS["ma_periods"]}
        fast = _ema(closes, Decimal(2) / 13)
        slow = _ema(closes, Decimal(2) / 27)
        dif = [a - b for a, b in zip(fast, slow)]
        # EMA9 begins with the first valid EMA26-derived DIF, never a filled zero.
        signal = _ema(dif[25:], Decimal(2) / 10)
        golden = dif[-1] > signal[-1] and dif[-2] <= signal[-2]
        above_zero = dif[-1] > 0
        rsi = _rsi(closes)
        recent_k: list[Decimal | None] = []
        for end in range(WINDOW_BARS - 3, WINDOW_BARS):
            upper = max(highs[end - 13:end + 1])
            lower = min(lows[end - 13:end + 1])
            recent_k.append(None if upper == lower else 100 * (closes[end] - lower) / (upper - lower))
        k = recent_k[-1]
        d = None if any(value is None for value in recent_k) else sum(recent_k) / 3
        j = None if k is None or d is None else 3 * k - 2 * d
        total_volume = sum(volumes[-20:])
        ratio = None if total_volume == 0 else volumes[-1] * 20 / total_volume
        return20 = (closes[-1] / closes[-21] - 1) * 100
        alignment = closes[-1] > averages[5] > averages[10] > averages[20] > averages[60]
        swing_highs, swing_lows = _pivots(closes)
        divergence, issues = _divergence(closes, dif, rsi, swing_highs, swing_lows)
        result["divergence_pivots"] = {
            "high_dates": [dates[index] for index in swing_highs],
            "low_dates": [dates[index] for index in swing_lows],
        }
        result["indicators"] = {
            "close": _decimal_text(closes[-1]),
            **{f"ma{period}": _decimal_text(value) for period, value in averages.items()},
            "daily_long_alignment": alignment,
            "macd_dif": _decimal_text(dif[-1]),
            "macd_signal": _decimal_text(signal[-1]),
            "macd_previous_dif": _decimal_text(dif[-2]),
            "macd_previous_signal": _decimal_text(signal[-2]),
            "macd_golden_cross": golden,
            "macd_above_zero": above_zero,
            "stochastic_k": _decimal_text(k),
            "stochastic_d": _decimal_text(d),
            "stochastic_j": _decimal_text(j),
            "rsi14": _decimal_text(rsi[-1]),
            "volume_ratio20": _decimal_text(ratio),
            "return20_pct": _decimal_text(return20),
            **divergence,
        }
        if k is None or d is None:
            issues.append("stochastic_zero_range")
        if rsi[-1] is None:
            issues.append("rsi_no_directional_changes")
        if ratio is None:
            issues.append("volume_mean_is_zero")
        result["issues"] = issues
        if issues:
            return result

        components: list[dict[str, Any]] = []
        def add(component_id: str, label: str, points: int, reason: str) -> None:
            components.append({"id": component_id, "label": label, "points": points, "reason": reason})

        add("trend", "均线趋势", 12 if alignment else 0,
            "收盘价 > MA5 > MA10 > MA20 > MA60" if alignment else "未满足严格均线多头排列")
        add("macd", "MACD", 5 if golden else 3 if above_zero else 0,
            "DIF 当日上穿信号线，前一日未在其上" if golden else "DIF 大于零" if above_zero else "无金叉且 DIF 不大于零")
        add("stochastic", "随机指标 K(14)", 3 if k < 20 else -4 if k > 80 else 0,
            "K < 20" if k < 20 else "K > 80" if k > 80 else "20 ≤ K ≤ 80")
        add("rsi", "RSI(14)", 3 if rsi[-1] < 30 else -4 if rsi[-1] > 70 else 0,
            "RSI < 30" if rsi[-1] < 30 else "RSI > 70" if rsi[-1] > 70 else "30 ≤ RSI ≤ 70")
        bottom = divergence["macd_bottom"] or divergence["rsi_bottom"]
        top = divergence["macd_top"] or divergence["rsi_top"]
        add("divergence", "已确认背离", (5 if bottom else 0) - (6 if top else 0),
            "底背离 +5；顶背离 -6" if bottom and top else "至少一个指标底背离 +5" if bottom else "至少一个指标顶背离 -6" if top else "最近两组已确认价格拐点无背离，或不足两组")
        add("volume", "20 日量比", 4 if ratio >= Decimal("1.5") else -3 if ratio < Decimal("0.7") else 0,
            "当日量 / 含当日 20 日均量 ≥ 1.5" if ratio >= Decimal("1.5") else "当日量 / 含当日 20 日均量 < 0.7" if ratio < Decimal("0.7") else "0.7 ≤ 当日量 / 含当日 20 日均量 < 1.5")
        add("momentum", "20 日涨幅", 4 if return20 > 5 else -5 if return20 < -8 else 0,
            "20 日涨幅 > 5%" if return20 > 5 else "20 日涨幅 < -8%" if return20 < -8 else "-8% ≤ 20 日涨幅 ≤ 5%")
        result.update(status="available", score=max(0, min(100, 50 + sum(component["points"] for component in components))), components=components)
    return result
