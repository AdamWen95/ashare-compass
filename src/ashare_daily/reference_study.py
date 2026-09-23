"""Offline, paired descriptions of two fixed technical selection rules.

This is a historical reconstruction of an already selected industry sample.
Forward close changes describe observations, not executable trades or returns
of an account.  It never changes the production ranking or fetches data.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import re

from .reference_indicators import score_reference_technical
from .reference_strategy import VERSION, validated_panel
from .factors.trend import mean_window, period_return, subtract
from .sector_screening import _amount_valid
from .sector_selection import digest


SCHEMA = "reference-strategy-study-v1"
HORIZONS = (5, 20)
PARAMETERS = {
    "window_bars": 120, "anchor_step_trading_days": 5,
    "forward_horizons_trading_days": list(HORIZONS), "max_candidates": 5,
    "min_avg_amount_20_cny": "50000000", "ma_periods": [20, 60],
    "relative_strength_period": 20, "benchmark_symbol": "sh.000001",
    "baseline_ranking": ["relative_return_20_desc", "avg_amount_20_cny_desc", "symbol_asc", "security_id_asc"],
    "reference_ranking": ["reference_score_desc", "relative_return_20_desc", "avg_amount_20_cny_desc", "symbol_asc", "security_id_asc"],
    "split": "first_floor_two_thirds_anchors_exploration_last_third_holdout",
    "future_missing_policy": "exclude_both_groups_at_that_anchor_and_horizon;no_replacement",
    "forward_measure": "percent_change_from_anchor_close_to_horizon_close",
    "aggregate_weighting": "equal_anchor_weights;within_anchor_equal_stock_weights",
    "parameters_fitted": False,
}
LIMITATIONS = [
    "既有行业入选样本存在选择后偏差，不代表历史全市场候选池。",
    "所有价格来自当前冻结复权版本，属于历史重建，不能伪装成历史实时结果。",
    "缺少逐历史锚点的点时资格证据，只比较技术条件池，不能认定为历史正式推荐。",
    "每5个交易日取样，20日未来观察互相重叠，样本不是独立试验。",
    "保留段仅为按时间切分的描述性观察；未调参，不能消除既有行业筛选和复权版本偏差。",
    "共同可评分池可能小于原技术条件池，保留排除计数和原前5名单。",
    "下一5/20交易日收盘价变化不是可成交回测或账户收益，未建模交易成本或成交可行性。",
    "小样本描述不足以证明参考策略优于原策略，不允许据此修改正式排序。",
]


def _number(value, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_numeric_value")
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ValueError("invalid_numeric_value") from None
    if not number.is_finite() or number < 0 or (not nonnegative and number == 0):
        raise ValueError("invalid_numeric_value")
    return number


def _text(value):
    return None if value is None else format(value, "f")


def _mean(values):
    return sum(values, Decimal(0)) / len(values) if values else None


def _change(start, end):
    return (end / start - 1) * 100


def _seal(value):
    return {**value, "content_hash": digest(value)}


def normalize_study_benchmark(response, calendar, *, source_hash, source_reference):
    """Validate one original history_f2 response without rewriting its window."""
    from .providers.baostock import BaoStockClient, validate_request
    if (not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
            or not isinstance(source_reference, dict) or source_reference.get("sha256") != source_hash
            or not isinstance(source_reference.get("path"), str) or not source_reference["path"]):
        raise ValueError("study_benchmark_source_reference_invalid")
    if not isinstance(calendar, dict) or calendar.get("verified") is not True:
        raise ValueError("study_benchmark_calendar_unverified")
    dates = calendar.get("trading_dates")
    if not isinstance(dates, list) or not dates or dates != sorted(set(dates)):
        raise ValueError("study_benchmark_calendar_invalid")
    if any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day for day in dates):
        raise ValueError("study_benchmark_calendar_invalid")
    if not isinstance(response, dict):
        raise ValueError("study_benchmark_source_invalid")
    params = validate_request("history_f2", response.get("parameters", {}))
    if (not BaoStockClient._valid_worker_result(response, "history_f2", params) or response.get("ok") is not True
            or params.get("code") != "sh.000001" or params.get("security_type") != "index"
            or params.get("adjustment_mode") != "unadjusted"):
        raise ValueError("study_benchmark_source_invalid")
    if not params["start_date"] <= dates[0] <= dates[-1] <= params["end_date"]:
        raise ValueError("study_benchmark_source_window_invalid")
    observed = datetime.fromisoformat(response["fetched_at"])
    if observed > datetime.now(observed.tzinfo) or observed.date().isoformat() < dates[-1]:
        raise ValueError("study_benchmark_observation_before_last_bar")
    if [row["date"] for row in response["rows"]] != dates:
        raise ValueError("study_benchmark_exact_calendar_mismatch")
    records = []
    for row in response["rows"]:
        if row["code"] != "sh.000001":
            raise ValueError("study_benchmark_row_symbol_mismatch")
        try:
            values = {field: _number(row[field]) for field in ("open", "high", "low", "close", "preclose")}
            _number(row["volume"], nonnegative=True)
            _number(row["amount"], nonnegative=True)
        except (KeyError, ValueError):
            raise ValueError("study_benchmark_bar_numeric_invalid") from None
        if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
            raise ValueError("study_benchmark_bar_ohlc_invalid")
        records.append({"trade_date": row["date"], "close": row["close"]})
    return _seal({"schema_version": "reference-study-benchmark-v1", "verified": True, "symbol": "sh.000001",
        "security_type": "index", "adjustment_mode": "index_native", "records": records,
        "source_hash": source_hash, "source_reference": deepcopy(source_reference), "calendar": deepcopy(calendar),
        "fetched_at": response["fetched_at"], "historical_reconstruction": True,
        "network_requests": 1, "model_calls": 0, "production_database_writes": 0})


def _benchmark(benchmark, dates):
    problems, indexed = [], {}
    if not isinstance(benchmark, dict):
        return {}, ["benchmark_packet_missing"], list(dates)
    if benchmark.get("verified") is not True:
        problems.append("benchmark_not_verified")
    if (benchmark.get("schema_version") != "reference-study-benchmark-v1"
            or benchmark.get("content_hash") != digest({key: value for key, value in benchmark.items() if key != "content_hash"})):
        problems.append("benchmark_frozen_packet_hash_invalid")
    reference = benchmark.get("source_reference", {})
    if not isinstance(reference, dict) or reference.get("sha256") != benchmark.get("source_hash") or not reference.get("path"):
        problems.append("benchmark_source_reference_invalid")
    calendar = benchmark.get("calendar", {})
    if not isinstance(calendar, dict) or calendar.get("verified") is not True or calendar.get("trading_dates") != dates:
        problems.append("benchmark_calendar_mismatch")
    if not isinstance(benchmark.get("source_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", benchmark["source_hash"]):
        problems.append("benchmark_source_hash_missing_or_invalid")
    if benchmark.get("symbol") != "sh.000001":
        problems.append("benchmark_must_be_sh_000001")
    if benchmark.get("security_type") != "index" or benchmark.get("adjustment_mode") != "index_native":
        problems.append("benchmark_must_be_native_index")
    records = benchmark.get("records")
    if not isinstance(records, list):
        problems.append("benchmark_records_invalid")
        records = []
    for row in records:
        try:
            day = row["trade_date"]
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or day in indexed:
                raise ValueError
            indexed[day] = _number(row["close"])
        except (KeyError, TypeError, ValueError):
            problems.append("benchmark_invalid_or_duplicate_row")
    missing = [day for day in dates if day not in indexed]
    if missing:
        problems.append("benchmark_full_trading_window_missing")
    if [row.get("trade_date") for row in records if isinstance(row, dict)] != dates:
        problems.append("benchmark_exact_calendar_mismatch")
    return indexed, sorted(set(problems)), missing


def _base_key(record):
    return (-record["relative"], -record["amount"], record["symbol"], record["security_id"])


def _candidate(member, identity, window_dates, benchmark):
    rows = member["rows"]
    missing = [day for day in window_dates if day not in rows]
    if missing:
        return None, "historical_window_missing", missing
    bars = [rows[day] for day in window_dates]
    try:
        closes = [_number(bar["close"]) for bar in bars]
        # Match the production rule's complete 120-day valid input window.
        # The 20-day mean must not hide an earlier absent or invalid amount.
        # Use its exact unit, transaction-status and volume/amount checks too.
        if any(not _amount_valid(bar) for bar in bars):
            return None, "historical_window_invalid", []
        amounts = [_number(bar["amount_cny"], nonnegative=True) for bar in bars]
    except (KeyError, ValueError):
        return None, "historical_window_invalid", []
    short, long = mean_window(closes, 20), mean_window(closes, 60)
    amount = mean_window(amounts, 20, positive=False)
    relative = subtract(period_return(closes, 20), period_return([benchmark[day] for day in window_dates[-21:]], 20))
    if not (amount >= Decimal("50000000") and closes[-1] > short > long and relative > 0):
        return None, "technical_conditions_not_met", []
    record = {"security_id": identity, "symbol": member["symbol"], "name": member.get("name"),
              "relative": relative, "amount": amount}
    scored = score_reference_technical(bars)
    if scored.get("status") == "available" and type(scored.get("score")) is int and 0 <= scored["score"] <= 100:
        record["score"] = scored["score"]
    else:
        record["score"] = None
    return record, None, []


def _group_metrics(identities, members, start_day, end_day, benchmark_change):
    observations = []
    for identity in identities:
        rows = members[identity]["rows"]
        change = _change(_number(rows[start_day]["close"]), _number(rows[end_day]["close"]))
        observations.append({"security_id": identity, "price_change_pct": _text(change),
                             "relative_to_benchmark_percentage_points": _text(change - benchmark_change)})
    changes = [Decimal(row["price_change_pct"]) for row in observations]
    mean_change = _mean(changes)
    return {"stock_count": len(identities), "observations": observations,
            "mean_price_change_pct": _text(mean_change),
            "mean_relative_to_benchmark_percentage_points": _text(mean_change - benchmark_change),
            "up_fraction": _text(Decimal(sum(value > 0 for value in changes)) / len(changes))}


def _paired_forward(members, dates, benchmark, anchor_index, horizon, baseline, reference):
    start_day, end_day = dates[anchor_index], dates[anchor_index + horizon]
    value = {"horizon_trading_days": horizon, "start_date": start_day, "end_date": end_day,
             "status": "excluded", "reasons": [], "missing": [], "baseline": None, "reference": None,
             "reference_minus_baseline_percentage_points": None,
             "benchmark_price_change_pct": _text(_change(benchmark[start_day], benchmark[end_day]))}
    if not baseline or not reference:
        value["reasons"] = ["empty_common_candidate_pool"]
        return value
    for identity in sorted(set(baseline + reference)):
        rows, absent, invalid = members[identity]["rows"], [], []
        for day in dates[anchor_index:anchor_index + horizon + 1]:
            if day not in rows:
                absent.append(day)
                continue
            try:
                _number(rows[day]["close"])
            except (KeyError, ValueError):
                invalid.append(day)
        if absent or invalid:
            value["missing"].append({"security_id": identity, "missing_dates": absent, "invalid_dates": invalid})
    if value["missing"]:
        value["reasons"] = ["paired_forward_window_incomplete"]
        return value
    benchmark_change = Decimal(value["benchmark_price_change_pct"])
    left = _group_metrics(baseline, members, start_day, end_day, benchmark_change)
    right = _group_metrics(reference, members, start_day, end_day, benchmark_change)
    value.update(status="included", baseline=left, reference=right,
                 reference_minus_baseline_percentage_points=_text(Decimal(right["mean_price_change_pct"]) - Decimal(left["mean_price_change_pct"])))
    return value


def _aggregate(anchors):
    horizons = {}
    for horizon in HORIZONS:
        observations = [anchor["forward"][str(horizon)] for anchor in anchors]
        included = [item for item in observations if item["status"] == "included"]
        groups = {}
        for group in ("baseline", "reference"):
            groups[group] = {"stock_observation_count": sum(item[group]["stock_count"] for item in included),
                **{metric: _text(_mean([Decimal(item[group][metric]) for item in included])) for metric in (
                    "mean_price_change_pct", "mean_relative_to_benchmark_percentage_points", "up_fraction")}}
        horizons[str(horizon)] = {"included_anchor_count": len(included), "excluded_anchor_count": len(observations) - len(included),
            "missing_stock_window_count": sum(len(item["missing"]) for item in observations), **groups,
            "mean_paired_difference_percentage_points": _text(_mean([Decimal(item["reference_minus_baseline_percentage_points"]) for item in included])),
            "reference_higher_anchor_count": sum(Decimal(item["reference_minus_baseline_percentage_points"]) > 0 for item in included)}
    return {"anchor_count": len(anchors), "start_date": anchors[0]["anchor_date"] if anchors else None,
        "end_date": anchors[-1]["anchor_date"] if anchors else None, "horizons": horizons}


def compare_reference_strategies(selection, inputs, observation, benchmark):
    """Describe fixed baseline/reference top-five groups using frozen inputs only.

    The caller supplies an independently verified, full-calendar index packet.
    Missing index coverage rejects the whole comparison; missing stock outcomes
    exclude both groups for the same anchor and horizon without replacements.
    """
    panel = validated_panel(selection, inputs, observation)
    dates, members = panel["dates"], panel["members"]
    if dates != sorted(set(dates)) or any(date.fromisoformat(day).isoformat() != day for day in dates):
        raise ValueError("study_calendar_invalid")
    result = {"schema_version": SCHEMA, "reference_version": VERSION, "status": "insufficient_data",
        "source_input_hash": panel["source_input_hash"], "source_observation_hash": panel["source_observation_hash"],
        "benchmark_source_hash": benchmark.get("source_hash") if isinstance(benchmark, dict) else None,
        "benchmark_packet_hash": digest(benchmark) if isinstance(benchmark, dict) else None,
        "parameters": deepcopy(PARAMETERS), "calendar": {"trading_dates": list(dates), "count": len(dates)},
        "sample_member_count": len(members), "source_issue_member_count": sum(bool(member.get("issues")) for member in members.values()),
        "historical_qualification_checked": False, "historical_reconstruction": True,
        "network_requests": 0, "model_calls": 0, "production_data_writes": 0,
        "ranking_change_allowed": False, "conclusion": "evidence_insufficient_for_promotion",
        "issues": [], "missing_benchmark_dates": [], "anchors": [], "aggregates": {}, "limitations": list(LIMITATIONS)}
    benchmark_values, issues, missing = _benchmark(benchmark, dates)
    if len(dates) < 140:
        issues.append("fewer_than_120_history_plus_20_future_bars")
    if issues:
        result.update(issues=sorted(set(issues)), missing_benchmark_dates=missing)
        return _seal(result)
    anchors = []
    with localcontext() as context:
        context.prec = 40
        for anchor_index in range(119, len(dates) - 20, 5):
            window_dates = dates[anchor_index - 119:anchor_index + 1]
            pool, excluded = [], []
            for identity, member in members.items():
                record, reason, missing_dates = _candidate(member, identity, window_dates, benchmark_values)
                if record is not None:
                    pool.append(record)
                elif reason != "technical_conditions_not_met":
                    excluded.append({"security_id": identity, "reason": reason, "missing_dates": missing_dates})
            pool.sort(key=_base_key)
            common = [record for record in pool if record["score"] is not None]
            unscored = [record["security_id"] for record in pool if record["score"] is None]
            original = [record["security_id"] for record in pool[:5]]
            baseline = [record["security_id"] for record in common[:5]]
            reference = [record["security_id"] for record in sorted(common, key=lambda record: (-record["score"], *_base_key(record)))[:5]]
            overlap = len(set(baseline) & set(reference))
            anchors.append({"anchor_date": dates[anchor_index], "anchor_trading_index": anchor_index,
                "history_start_date": window_dates[0], "history_bar_count": len(window_dates),
                "sample_member_count": len(members), "historical_window_excluded_count": len(excluded), "historical_window_exclusions": excluded,
                "technical_pool_count": len(pool), "common_scored_pool_count": len(common),
                "score_unavailable_count": len(unscored), "score_unavailable_security_ids": unscored,
                "original_baseline_security_ids": original, "baseline_security_ids": baseline, "reference_security_ids": reference,
                "baseline_changed_by_common_pool": original != baseline,
                "original_baseline_removed_security_ids": [identity for identity in original if identity not in baseline],
                "top5_overlap_count": overlap, "top5_overlap_fraction": _text(Decimal(overlap) / max(len(baseline), len(reference))) if baseline or reference else None,
                "forward": {str(horizon): _paired_forward(members, dates, benchmark_values, anchor_index, horizon, baseline, reference) for horizon in HORIZONS}})
        split_index = len(anchors) * 2 // 3
        for index, anchor in enumerate(anchors):
            anchor["segment"] = "exploration" if index < split_index else "holdout"
        result.update(status="available" if any(item["forward"]["20"]["status"] == "included" for item in anchors) else "insufficient_data",
            anchors=anchors, split_anchor_index=split_index,
            aggregates={"all": _aggregate(anchors), "exploration": _aggregate(anchors[:split_index]), "holdout": _aggregate(anchors[split_index:])})
    if result["status"] != "available":
        result["issues"] = ["no_complete_paired_20_day_observations"]
    return _seal(result)
