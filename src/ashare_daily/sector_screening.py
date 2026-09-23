"""Pure F3-S calculations over an explicitly frozen selection and fact version.

Only the existing trend formula functions calculate indicators. Four-board scope
and risk/fact separation are new workflow semantics, not new numeric rules.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .factors.trend import mean_window, number, period_return, subtract
from .providers.base import BOARD_EXCHANGES, iso_date
from .sector_selection import digest, verify_selection
from .universe import SHANGHAI


class SectorScreeningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["f3s-screening-config-v1"]
    strategy_version: Literal["trend_research_v1.0.0"]
    data_semantics_version: Literal["f3s-numeric-facts-risk-split-v1", "f4s1-cache-strategy-input-split-v1"]
    market_scope: Literal["sse_szse_a"]
    allowed_boards: list[Literal["sse_main", "szse_main", "chinext", "star"]]
    benchmark_id: Literal["sh.000001"]
    benchmark_name: Literal["上证综合指数"]
    min_history_trading_days: Literal[120]
    ma_short_days: Literal[20]
    ma_long_days: Literal[60]
    return_days: Literal[20]
    amount_days: Literal[20]
    min_avg_amount_cny: Decimal
    max_candidates: Literal[20]
    trend_adjustment_mode: Literal["forward_adjusted"]
    display_adjustment_mode: Literal["unadjusted"]

    @model_validator(mode="after")
    def fixed_version(self):
        if (len(self.allowed_boards) != 4 or set(self.allowed_boards) != set(BOARD_EXCHANGES)
                or self.min_avg_amount_cny != Decimal("50000000")):
            raise ValueError("F3-S preserves the approved four-board scope and existing numeric rule version")
        return self


def validate_screening_config(value):
    return SectorScreeningConfig.model_validate(value).model_dump(mode="json")


def screening_input_hash(value):
    return digest({k: v for k, v in value.items() if k != "input_hash"})


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("source_time_requires_timezone")
    return stamp.astimezone(SHANGHAI)


def _text(value):
    return str(value) if value is not None else None


def _condition(key, label, value, reason):
    return {"id": key, "label": label, "status": "unknown" if value is None else "pass" if value else "fail", "reason": reason}


def _dated(records, expected, *, symbol, security_id=None, adjustment=None):
    result, issues = {}, []
    for row in records:
        try:
            day = iso_date(row.get("trade_date")).isoformat()
            if day not in expected or day in result:
                raise ValueError("duplicate_or_unrequested_record_date")
            if (row.get("symbol", symbol) != symbol or security_id is not None and row.get("symbol") != symbol
                    or security_id is not None and row.get("security_id") != security_id):
                raise ValueError("record_security_identity_mismatch")
            if adjustment is not None and row.get("adjustment_mode") != adjustment:
                raise ValueError("record_adjustment_mode_mismatch")
            result[day] = row
        except (ValueError, TypeError, AttributeError) as exc:
            issues.append(str(exc))
    return result, sorted(set(issues))


def _raw_prices_valid(row):
    if row.get("price_unit") != "CNY" or row.get("volume_unit") != "shares" or row.get("amount_unit") != "CNY":
        return False
    prices = [number(row.get(key)) for key in ("open", "high", "low", "close")]
    if any(v is None or v <= 0 for v in prices):
        return False
    op, high, low, close = prices
    if not low <= op <= high or not low <= close <= high:
        return False
    volume = row.get("volume_shares")
    if type(volume) is not int or volume < 0:
        return False
    if row.get("tradestatus") is False:
        return False
    if row.get("tradestatus") is not True and volume == 0 and number(row.get("amount_cny")) in (None, Decimal(0)):
        return False
    return not (set(row.get("quality_flags", [])) - {"missing_preclose", "missing_amount_cny"})


def _amount_valid(row):
    amount, volume = number(row.get("amount_cny")), row.get("volume_shares")
    return (amount is not None and amount >= 0 and row.get("amount_unit") == "CNY"
        and row.get("tradestatus") is not False and type(volume) is int and volume >= 0
        and not ((amount > 0 and volume == 0) or (amount == 0 and volume > 0))
        and (row.get("tradestatus") is True or amount > 0 or volume > 0))


def _validate_stock_packet(member, packet, expected, cutoff, *, diagnostic=False):
    security_id = member["security_id"]
    symbol = {"SSE": "sh.", "SZSE": "sz."}.get(member.get("exchange"), "unknown.") + member.get("code", "")
    issues = list(packet.get("issues", []))
    packet_dates = packet.get("expected_dates", [])
    if (packet_dates != sorted(set(packet_dates)) or not packet_dates
            or any(day not in expected for day in packet_dates)):
        issues.append("stock_expected_dates_not_in_verified_calendar")
    raw, raw_issues = _dated(packet.get("raw_records", []), packet_dates, symbol=symbol,
        security_id=security_id, adjustment="unadjusted")
    issues += raw_issues
    providers = set()
    hashes = {}
    first_seen = []
    for day, row in raw.items():
        payload = {k: v for k, v in row.items() if k not in {"fact_hash", "first_seen_at"}}
        if row.get("fact_hash") != digest(payload):
            issues.append("raw_fact_hash_mismatch")
        else:
            hashes[day] = row["fact_hash"]
        if row.get("provider") not in {"sina", "baostock", "eastmoney"}:
            issues.append("raw_provider_unrecognized")
        providers.add(row.get("provider"))
        if (row.get("price_unit"),row.get("volume_unit"),row.get("amount_unit")) != ("CNY","shares","CNY"):
            issues.append("raw_field_units_invalid")
        if any(row.get(key) is not None and number(row[key]) is None for key in ("open","high","low","close","amount_cny")):
            issues.append("raw_nonfinite_or_invalid_number")
        prices=[number(row.get(key)) for key in ("open","high","low","close")]
        if all(v is not None for v in prices) and (any(v<=0 for v in prices) or not prices[2]<=prices[0]<=prices[1] or not prices[2]<=prices[3]<=prices[1]):
            issues.append("raw_ohlc_range_invalid")
        if row.get("volume_shares") is not None and (type(row["volume_shares"]) is not int or row["volume_shares"]<0):
            issues.append("raw_volume_not_nonnegative_integer_shares")
        try:
            stamp = _timestamp(row.get("first_seen_at"))
            first_seen.append(stamp.isoformat())
            if stamp > cutoff:
                issues.append("raw_fact_first_seen_after_input_cutoff")
        except (ValueError, TypeError):
            issues.append("raw_fact_first_seen_unverified")
        if row.get("tradestatus") is False and (row.get("volume_shares") not in (0, None) or number(row.get("amount_cny")) not in (Decimal(0), None)):
            issues.append("suspension_volume_amount_conflict")
        if any(row.get(field) is not None and type(row[field]) is not bool for field in ("tradestatus", "is_st")):
            issues.append("raw_risk_status_type_invalid")
    window = packet.get("adjustment_window")
    adjusted, adjustment_issues = {}, []
    if not isinstance(window, dict):
        adjustment_issues.append("complete_adjustment_window_missing")
    else:
        body = {k: v for k, v in window.items() if k not in {"window_id", "content_hash", "first_seen_at", "observations"}}
        hashed = digest(body)
        if window.get("content_hash") != hashed or window.get("window_id") != "f2-window-" + hashed:
            adjustment_issues.append("adjustment_window_hash_mismatch")
        if (window.get("security_id") != security_id or window.get("symbol") != symbol
                or window.get("provider") not in providers or providers != {window.get("provider")}
                or window.get("adjustment_mode") != "forward_adjusted"
                or window.get("expected_dates") != packet_dates
                or not packet_dates or window.get("window_start") != packet_dates[0] or window.get("window_end") != packet_dates[-1]):
            adjustment_issues.append("adjustment_window_identity_source_or_dates_mismatch")
        if window.get("raw_fact_hashes") != hashes or not diagnostic and set(hashes) != set(packet_dates):
            adjustment_issues.append("adjustment_window_not_bound_to_complete_raw_version")
        if not window.get("adjustment_anchor_hash") or not window.get("factor_component_hash") or not window.get("raw_component_hash"):
            adjustment_issues.append("adjustment_anchor_or_source_components_missing")
        observations = window.get("observations", [])
        if not observations:
            adjustment_issues.append("adjustment_source_observation_missing")
        try:
            if _timestamp(window.get("first_seen_at")) > cutoff:
                adjustment_issues.append("adjustment_first_seen_after_input_cutoff")
            for observation in observations:
                if (_timestamp(observation.get("fetched_at")) > cutoff or not observation.get("source_file_hash")
                        or not observation.get("source_response_path") or not observation.get("batch_id")):
                    adjustment_issues.append("adjustment_source_evidence_after_cutoff_or_missing")
        except (ValueError, TypeError):
            adjustment_issues.append("adjustment_source_time_invalid")
        adjusted, more = _dated(window.get("records", []), packet_dates, symbol=symbol,
            security_id=security_id, adjustment="forward_adjusted")
        adjustment_issues += more
        if diagnostic:
            missing = sorted(set(packet_dates)-set(adjusted))
            if (window.get("complete") is not False or not missing or window.get("missing_dates") != missing
                    or set(adjusted) != set(raw)):
                adjustment_issues.append("diagnostic_window_missing_dates_or_raw_binding_invalid")
        elif set(adjusted) != set(packet_dates):
            adjustment_issues.append("adjustment_window_record_dates_incomplete")
        for day, row in adjusted.items():
            if row.get("provider") != window.get("provider") or row.get("price_unit") != "CNY":
                adjustment_issues.append("adjustment_record_source_or_unit_conflict")
            if row.get("tradestatus") is not False and not _raw_prices_valid(row):
                adjustment_issues.append("adjustment_price_fields_not_usable")
            original = raw.get(day, {})
            for field in ("volume_shares", "amount_cny"):
                if number(row.get(field)) != number(original.get(field)):
                    adjustment_issues.append("adjustment_and_raw_nonprice_fields_conflict")
            for field in ("tradestatus", "is_st"):
                if isinstance(row.get(field), bool) and isinstance(original.get(field), bool) and row[field] != original[field]:
                    issues.append("raw_adjusted_risk_status_conflict:"+field)
    return raw, adjusted, sorted(set(issues)), sorted(set(adjustment_issues)), first_seen


def _diagnostic_metrics(member, packet, all_dates, dates, cutoff, benchmark_return, config):
    """Retain calculable observations from one incomplete response, never readiness."""
    window = packet.get("diagnostic_adjustment_window")
    if not isinstance(window, dict):
        return None
    completeness_issues = {"history_calendar_dates_missing", "complete_adjustment_window_missing"}
    diagnostic_packet = {**packet, "adjustment_window": window,
        "issues": [issue for issue in packet.get("issues", []) if issue not in completeness_issues]}
    raw, adjusted, issues, adjustment_issues, _ = _validate_stock_packet(
        member, diagnostic_packet, all_dates, cutoff, diagnostic=True)
    # A known absent day remains a hole in the fixed calendar. Structural errors
    # invalidate the diagnostic too; a partial response is never a stitched window.
    values = []
    amounts = []
    for day in dates:
        original, row = raw.get(day, {}), adjusted.get(day, {})
        price = number(row.get("close"))
        values.append(price if not issues and not adjustment_issues and _raw_prices_valid(original)
            and price is not None and price > 0 else None)
        amounts.append(original.get("amount_cny") if _amount_valid(original) else None)
    short, long = mean_window(values, config.ma_short_days), mean_window(values, config.ma_long_days)
    stock_return = period_return(values, config.return_days)
    return {"complete": False, "eligible_for_technical_result": False,
        "notice": "不完整历史响应的独立诊断值；保留固定日期缺口，不用于完整复权就绪或技术通过",
        "adjustment_window_id": window.get("window_id"), "provider": window.get("provider"),
        "missing_dates": deepcopy(window.get("missing_dates", [])),
        "readiness_issues": [issue for issue in packet.get("issues", []) if issue in completeness_issues],
        "issues": sorted(set(issues+adjustment_issues)), "calculation_dates": dates,
        "metrics": {"adjusted_close": _text(values[-1] if values else None), "ma20": _text(short), "ma60": _text(long),
            "stock_return_20": _text(stock_return), "benchmark_return_20": _text(benchmark_return),
            "relative_return_20": _text(subtract(stock_return, benchmark_return)),
            "avg_amount_20_cny": _text(mean_window(amounts, config.amount_days, positive=False)),
            "valid_history_count": sum(p is not None and number(a) is not None for p, a in zip(values, amounts))}}


def _strategy_validation(member, packet, all_dates, cutoff):
    """Validate one complete or partial source version without a 320-row gate.

    Only the two explicit cache-completeness findings change stage ownership.
    Identity, raw/factor binding, hashes, all source dates/units and provenance
    continue through the same validator. Missing dates are never removed from
    the caller's fixed strategy windows or replaced by invented observations.
    """
    cache_findings = {"history_calendar_dates_missing", "complete_adjustment_window_missing"}
    partial = not packet.get("adjustment_window") and isinstance(packet.get("diagnostic_adjustment_window"), dict)
    source_window = packet.get("diagnostic_adjustment_window") if partial else packet.get("adjustment_window")
    checked = {**packet, "adjustment_window": source_window,
        "issues": [issue for issue in packet.get("issues", []) if issue not in cache_findings]}
    raw, adjusted, issues, adjustment_issues, seen = _validate_stock_packet(
        member, checked, all_dates, cutoff, diagnostic=partial)
    expected = packet.get("expected_dates", [])
    missing_raw, missing_adjusted = sorted(set(expected)-set(raw)), sorted(set(expected)-set(adjusted))
    cache = {"cache_expected_dates": len(expected), "cache_raw_dates": len(raw), "cache_adjusted_dates": len(adjusted),
        "missing_raw_dates": missing_raw, "missing_adjusted_dates": missing_adjusted,
        "cache_target_complete": bool(expected) and not missing_raw and not missing_adjusted and not issues and not adjustment_issues,
        "cache_issues": sorted(set(packet.get("issues", []) + issues + adjustment_issues)),
        "source_window_id": source_window.get("window_id") if source_window else None,
        "source_window_complete": not partial and bool(source_window),
        "price_fill_count": 0, "calendar_dates_skipped": 0}
    return raw, adjusted, issues, adjustment_issues, seen, cache, source_window


def _benchmark(packet, dates, config, cutoff):
    issues = list(packet.get("issues", []))
    expected = dates[-(config.return_days+1):]
    if (packet.get("symbol") != config.benchmark_id or packet.get("security_type") != "index"
            or packet.get("adjustment_mode") != "index_native" or packet.get("price_unit") != "index_points"
            or not packet.get("provider") or not packet.get("fetch_version") or not packet.get("file_refs")):
        issues.append("benchmark_identity_units_or_source_version_missing")
    try:
        if _timestamp(packet.get("first_seen_at")) > _timestamp(packet.get("fetched_at")) or _timestamp(packet.get("fetched_at")) > cutoff:
            issues.append("benchmark_source_time_after_cutoff_or_conflicting")
    except (ValueError, TypeError):
        issues.append("benchmark_source_time_missing")
    rows, more = _dated(packet.get("records", []), dates, symbol=config.benchmark_id)
    issues += more
    for row in rows.values():
        if row.get("price_unit", "index_points") != "index_points" or row.get("quality_flags"):
            issues.append("benchmark_record_unit_or_quality_invalid")
    values = [rows.get(day, {}).get("close") for day in expected]
    value = period_return(values, config.return_days) if len(expected)==config.return_days+1 and not issues else None
    if value is None:
        issues.append("benchmark_aligned_21_point_window_missing")
    return value, {"symbol": config.benchmark_id, "name": config.benchmark_name, "provider": packet.get("provider"),
        "fetch_version": packet.get("fetch_version"), "window_dates": expected, "price_unit": "index_points",
        "return_unit": "ratio", "period_return": _text(value), "complete": value is not None,
        "issues": sorted(set(issues)), "file_refs": deepcopy(packet.get("file_refs", []))}


def _risk(member, packet, raw_today, target, cutoff, name, raw_field=None):
    values, refs, gaps = [], [], []
    candidates = []
    for key in ("statuses", "supplemental_status_evidence"):
        value = member.get(key, {}).get(name)
        if isinstance(value, dict):
            candidates.append(value)
    value = packet.get("risk_evidence", {}).get(name)
    if isinstance(value, dict):
        candidates.append(value)
    for evidence in candidates:
        if evidence.get("value") is None:
            gaps.append(evidence.get("unknown_reason", "risk_status_unknown"))
            continue
        dated = evidence.get("effective_date") == target
        if "as_of_date" in evidence:
            try:
                dated = (evidence.get("verified") is True and evidence.get("as_of_date") == target
                    and iso_date(evidence.get("effective_from")).isoformat() <= target
                    <= iso_date(evidence.get("effective_to")).isoformat()
                    and bool(evidence.get("source_path")) and len(evidence.get("source_file_hash", "")) == 64
                    and len(evidence.get("source_raw_hash", "")) == 64
                    and (name != "suspended" or evidence.get("value") is not True or evidence.get("full_day") is True))
            except (ValueError, TypeError):
                dated = False
        if (not isinstance(evidence.get("value"), bool) or not dated
                or not (evidence.get("evidence_id") or evidence.get("evidence_ids"))):
            gaps.append("risk_evidence_date_or_identity_missing")
            continue
        stamp = evidence.get("observed_at") or evidence.get("fetched_at") or evidence.get("first_seen_at")
        try:
            observed = _timestamp(stamp)
            if observed > cutoff or ("as_of_date" in evidence and (observed.date().isoformat() < target
                    or evidence.get("historical_reconstruction") is not (observed.date().isoformat() > target))):
                raise ValueError()
        except (ValueError, TypeError):
            gaps.append("risk_evidence_observation_time_unverified")
            continue
        values.append(evidence["value"])
        refs.append(deepcopy(evidence))
    if raw_field and isinstance(raw_today.get(raw_field), bool):
        try:
            raw_payload={k:v for k,v in raw_today.items() if k not in {"fact_hash","first_seen_at"}}
            if raw_today.get("fact_hash")==digest(raw_payload) and _timestamp(raw_today.get("first_seen_at")) <= cutoff:
                values.append(not raw_today[raw_field] if name == "suspended" else raw_today[raw_field])
                refs.append({"fact_hash":raw_today["fact_hash"],"field":raw_field,"effective_date":target,
                    "first_seen_at":raw_today["first_seen_at"]})
        except (ValueError, TypeError):
            gaps.append("raw_status_observation_unverified")
    if len(set(values))>1:
        return None, refs, ["conflicting_risk_status_evidence"]
    if values:
        return values[0], refs, []
    return None, refs, sorted(set(gaps or ["risk_status_unknown"]))


def evaluate_selection(selection, inputs, strategy_config):
    """Evaluate every frozen member; engineering and production share this core."""
    config = SectorScreeningConfig.model_validate(strategy_config)
    verify_selection(selection)
    purpose = selection.get("purpose", "production")
    if (inputs.get("schema_version") != "f3s-screening-input-v1" or inputs.get("input_hash") != screening_input_hash(inputs)
            or inputs.get("selection_id") != selection["selection_id"]
            or inputs.get("selection_content_hash") != selection["content_hash"]
            or inputs.get("target_date") != selection["target_date"]
            or inputs.get("mode") != selection.get("mode") or inputs.get("purpose") != purpose
            or selection.get("selection_verified") is not True):
        raise ValueError("screening_requires_matching_verified_frozen_selection_and_input")
    if inputs["mode"] not in {"research", "offline_test"}:
        raise ValueError("screening_data_mode_invalid")
    cutoff = _timestamp(inputs["cutoff_at"])
    if cutoff > datetime.now(SHANGHAI) or cutoff < _timestamp(selection["cutoff_at"]):
        raise ValueError("screening_cutoff_future_or_before_frozen_selection")
    target = iso_date(selection["target_date"]).isoformat()
    calendar = inputs.get("calendar", {})
    all_dates = calendar.get("trading_dates", [])
    calendar_issues = list(calendar.get("issues", []))
    try:
        if (calendar.get("verified") is not True or not all_dates or all_dates != sorted(set(all_dates))
                or any(iso_date(day).isoformat()!=day or day>target for day in all_dates) or all_dates[-1]!=target):
            raise ValueError()
    except (ValueError, TypeError):
        calendar_issues.append("verified_target_trading_calendar_missing")
    dates = all_dates[-config.min_history_trading_days:] if not calendar_issues else []
    packets = inputs.get("securities", {})
    members = selection.get("members", [])
    expected_ids = {m["security_id"] for m in members}
    if not isinstance(packets, dict) or set(packets)-expected_ids:
        raise ValueError("screening_input_contains_out_of_selection_security")
    benchmark_return, benchmark = _benchmark(inputs.get("benchmark", {}), all_dates if not calendar_issues else [], config, cutoff) if members else (None,
        {"symbol":config.benchmark_id,"status":"not_applicable","complete":False,"issues":[],"window_dates":[]})
    evaluations = []
    for member in sorted(members,key=lambda m:m["security_id"]):
        packet = packets.get(member["security_id"], {})
        split_inputs = config.data_semantics_version == "f4s1-cache-strategy-input-split-v1"
        cache, strategy_window = None, None
        if split_inputs:
            raw, adjusted, issues, adjustment_issues, seen, cache, strategy_window = _strategy_validation(member,packet,all_dates,cutoff)
        else:
            raw, adjusted, issues, adjustment_issues, seen = _validate_stock_packet(member,packet,all_dates,cutoff)
        issues += calendar_issues
        if set(dates)-set(raw):
            issues.append("raw_calculation_window_missing_dates")
        if len(dates)<config.min_history_trading_days:
            issues.append("calculation_calendar_shorter_than_120")
        values, amounts = [], []
        for day in dates:
            row, adj = raw.get(day,{}), adjusted.get(day,{})
            price = number(adj.get("close"))
            price_ok = _raw_prices_valid(row) and price is not None and price>0 and not adjustment_issues
            values.append(price if price_ok else None)
            amounts.append(row.get("amount_cny") if _amount_valid(row) else None)
        valid_count = sum(price is not None and number(amount) is not None for price,amount in zip(values,amounts))
        short, long = mean_window(values,config.ma_short_days),mean_window(values,config.ma_long_days)
        stock_return = period_return(values,config.return_days)
        relative = subtract(stock_return,benchmark_return)
        amount = mean_window(amounts,config.amount_days,positive=False) if not calendar_issues else None
        close = values[-1] if values else None
        technical = [
            _condition("history","有效历史不少于120个交易日",True if valid_count>=120 else None,f"末120交易日内有效数值行情{valid_count}日；不填充或缩短窗口"),
            _condition("liquidity","近20交易日平均成交额不少于5000万元",amount>=config.min_avg_amount_cny if amount is not None else None,"均额窗口含T，使用未复权原始成交额CNY" if amount is not None else "固定20日成交额窗口缺失、停牌或单位不符"),
            _condition("trend","前复权C(T)>MA20>MA60",close>short>long if all(v is not None for v in (close,short,long)) else None,("同一来源复权版本固定策略窗口的严格不等式" if split_inputs else "同一完整复权版本的严格不等式") if all(v is not None for v in (close,short,long)) else "复权或固定均线窗口未就绪" if split_inputs else "完整复权或固定均线窗口未就绪"),
            _condition("relative_strength","20交易日收益超过上证综指",relative>0 if relative is not None else None,"股票与固定基准用完全相同的21个交易日点" if relative is not None else "股票或上证综指的21点收益窗口缺失或未对齐"),
            _condition("data_integrity","日期、单位、版本和来源完整性",None if issues or adjustment_issues else True,"；".join(sorted(set(issues+adjustment_issues))) if issues or adjustment_issues else "冻结原价与同一复权版本的策略输入核对通过" if split_inputs else "冻结原价与完整复权版本核对通过"),
        ]
        identity_known = member.get("metadata_verified") is True
        identity_ok = (member.get("security_type")=="ordinary_a" and member.get("board") in config.allowed_boards
            and BOARD_EXCHANGES.get(member.get("board"))==member.get("exchange")) if identity_known else None
        listed = None
        listing, delisting = member.get("listing_date"),member.get("delisting_date")
        if listing and listing>target or delisting and delisting<=target:
            listed=False
        elif listing and member.get("listing_status")=="listed":
            listed=True
        today = raw.get(target,{})
        states, evidence, risk_gaps = {}, {}, []
        for name,field in (("st","is_st"),("suspended","tradestatus"),("delisting_period",None)):
            value,refs,gaps = _risk(member,packet,today,target,cutoff,name,field)
            if field and any(i.endswith(":"+field) for i in issues):
                value=None
                gaps.append("raw_adjusted_risk_status_conflict")
            states[name],evidence[name]=value,refs
            risk_gaps += [name+":"+reason for reason in gaps]
        eligibility=[_condition("identity","沪深四上市板块普通A股",identity_ok,"依据冻结证券类型与板块元数据"),
            _condition("listed","目标日期在有据上市区间",listed,"冻结上市/退市日期与上市状态"),
            *[_condition("not_"+name,"非"+label,not states[name] if isinstance(states[name],bool) else None,
                "目标日状态证据" if states[name] is not None else "目标日风险证据未知或冲突")
                for name,label in (("st","ST"),("suspended","停牌"),("delisting_period","退市整理期"))]]
        eligibility_status="fail" if any(c["status"]=="fail" for c in eligibility) else "pending" if any(c["status"]=="unknown" for c in eligibility) else "pass"
        not_applicable=identity_ok is False or listed is False or states["suspended"] is True and not issues
        if not_applicable:
            technical_status="not_applicable"
            for condition in technical:
                condition.update(status="not_applicable",reason="有据范围外、未上市/已退市或目标日停牌，不用缺失K线替代")
        elif issues or adjustment_issues:
            technical_status="unknown"
        elif any(c["status"]=="fail" for c in technical):
            technical_status="fail"
        elif any(c["status"]=="unknown" for c in technical):
            technical_status="unknown"
        else:
            technical_status="pass"
        label={"pass":"技术条件达标","fail":"技术条件未达标","unknown":"技术条件无法完整计算","not_applicable":"有据不适用"}[technical_status]
        if technical_status=="pass" and eligibility_status=="pending":
            label="技术条件达标、资格待核查"
        metrics={"adjusted_close":_text(close),"ma20":_text(short),"ma60":_text(long),"stock_return_20":_text(stock_return),
            "benchmark_return_20":_text(benchmark_return),"relative_return_20":_text(relative),"avg_amount_20_cny":_text(amount),
            "valid_history_count":valid_count,"display_close":today.get("close")}
        evaluations.append({"security_id":member["security_id"],"symbol":{"SSE":"sh.","SZSE":"sz."}.get(member.get("exchange"),"unknown.")+member.get("code",""),
            "name":member.get("name"),"exchange":member.get("exchange"),"listing_board":member.get("board"),"security_type":member.get("security_type"),
            "sector_ids":deepcopy(member.get("sector_ids",[])),"purpose":purpose,"production_eligible":purpose=="production",
            "selection_id":selection["selection_id"],"input_hash":inputs["input_hash"],"target_date":target,
            "technical_status":technical_status,"eligibility_status":eligibility_status,"observation_label":label,
            "technical_conditions":technical,"eligibility_conditions":eligibility,"metrics":metrics,"risk_gaps":sorted(set(risk_gaps)),
            "risk_states":states,"risk_evidence":evidence,"data_issues":sorted(set(issues+adjustment_issues)),
            "source_limitations":deepcopy(packet.get("source_limitations", packet.get("known_gaps", []))),
            "diagnostic_metrics":_diagnostic_metrics(member,packet,all_dates,dates,cutoff,benchmark_return,config),
            "metric_basis":{"calculation_dates":dates,"ma20_dates":dates[-20:],"ma60_dates":dates[-60:],"return_dates":dates[-21:],
                "amount_dates":dates[-20:],"includes_target":True,"return_unit":"ratio","price_unit":"CNY/share","amount_unit":"CNY",
                "price_basis":"one_complete_forward_adjusted_window","benchmark_basis":"native_index_points","precision_digits":40,
                "history_count_policy":"last_120_calendar_days_numeric_facts_risk_separate","suspended_dates_are_not_removed_from_windows":True},
            "source_versions":{"raw_fact_hashes":{day:row.get("fact_hash") for day,row in raw.items()},
                "raw_providers":sorted({row["provider"] for row in raw.values() if row.get("provider")}),
                "adjustment_provider":packet.get("adjustment_window",{}).get("provider") if packet.get("adjustment_window") else None,
                "adjustment_window_id":packet.get("adjustment_window",{}).get("window_id") if packet.get("adjustment_window") else None,
                "adjustment_anchor_hash":packet.get("adjustment_window",{}).get("adjustment_anchor_hash") if packet.get("adjustment_window") else None,
                "first_seen_at_min":min(seen) if seen else None,"first_seen_at_max":max(seen) if seen else None},
            "raw_facts_ready":len(raw)==len(packet.get("expected_dates",[])) and bool(raw) and all(_raw_prices_valid(r) and _amount_valid(r) for r in raw.values()),
            "adjustment_ready":not adjustment_issues,"latest_raw_date":max(raw,default=None),"technical_rank":None,"candidate_rank":None,
            "company_review_status":"not_started","formal_verified_opportunity":False})
        if split_inputs:
            row = evaluations[-1]
            input_ready = {"history": valid_count >= config.min_history_trading_days,
                "liquidity": amount is not None,
                "trend": all(value is not None for value in (close, short, long)),
                "relative_strength": relative is not None,
                "target_date": bool(dates) and dates[-1] == target and close is not None,
                "data_integrity": not issues and not adjustment_issues}
            row.update(cache_status=cache, cache_target_complete=cache["cache_target_complete"],
                strategy_inputs_ready=all(input_ready.values()), rule_input_readiness=input_ready,
                adjustment_ready=cache["cache_target_complete"],
                strategy_adjustment_ready=bool(dates) and set(dates) <= set(adjusted) and not adjustment_issues,
                history_window_accounted_for=cache["cache_target_complete"])
            row["metric_basis"].update(price_basis="one_source_version_fixed_strategy_windows",
                cache_target_is_not_a_strategy_threshold=True, missing_dates_are_not_skipped=True)
            row["source_versions"].update(strategy_adjustment_window_id=cache["source_window_id"],
                strategy_adjustment_provider=strategy_window.get("provider") if strategy_window else None,
                strategy_adjustment_anchor_hash=strategy_window.get("adjustment_anchor_hash") if strategy_window else None,
                strategy_source_window_complete=cache["source_window_complete"])
    ranked=sorted((r for r in evaluations if r["technical_status"]=="pass"),key=lambda r:(-Decimal(r["metrics"]["relative_return_20"]),-Decimal(r["metrics"]["avg_amount_20_cny"]),r["symbol"],r["security_id"]))
    for rank,row in enumerate(ranked,1):row["technical_rank"]=rank
    candidates=[r for r in ranked if r["eligibility_status"]=="pass"][:config.max_candidates] if purpose=="production" else []
    for rank,row in enumerate(candidates,1):row["candidate_rank"]=rank
    technical_counts=Counter(r["technical_status"] for r in evaluations)
    eligibility_counts=Counter(r["eligibility_status"] for r in evaluations)
    result={"schema_version":"f3s-screening-result-v1","mode":inputs["mode"],"purpose":purpose,
        "production_eligible":purpose=="production","selection_id":selection["selection_id"],"input_hash":inputs["input_hash"],
        "market_scope":"sse_szse_a","research_mode":"sector_first","target_date":target,"cutoff_at":inputs["cutoff_at"],
        "original_selection_cutoff_at":selection["cutoff_at"],"historical_reconstruction":bool(selection.get("historical_reconstruction") or inputs.get("historical_reconstruction") or cutoff.date().isoformat()>target),
        "strategy_config":config.model_dump(mode="json"),"strategy_config_hash":digest(config.model_dump(mode="json")),
        "status":"not_applicable" if not evaluations else "evaluated","evaluations":evaluations,"benchmark":benchmark,
        "counts":{"stock_count":len(evaluations),**{"technical_"+k+"_count":technical_counts[k] for k in ("pass","fail","unknown","not_applicable")},
            **{"eligibility_"+k+"_count":eligibility_counts[k] for k in ("pass","fail","pending")},"candidate_count":len(candidates),
            "formal_verified_opportunity_count":0,"technical_pass_eligibility_pending_count":sum(r["technical_status"]=="pass" and r["eligibility_status"]=="pending" for r in evaluations)},
        "technical_computable_ratio":(technical_counts["pass"]+technical_counts["fail"])/len(evaluations) if evaluations else None,
        "denominator_zero_display":"N/A" if not evaluations else None,"ranking_basis":["relative_return_20_desc","avg_amount_20_cny_desc","symbol_asc","security_id_asc"],
        "model_calls":0,"model_tokens":0,"company_review_status":"not_started","f4_status":"not_started",
        "notice":"真实数据工程验收，不代表自动入选或投资关注" if purpose=="engineering_validation" else "仅确定性量价技术观察；公司、公告和研究核查尚未完成"}
    result["result_hash"]=digest(result)
    return result
