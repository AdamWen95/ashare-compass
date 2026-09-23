"""Pure M2 evaluation of a frozen input. No database or network access."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ashare_daily.factors.trend import mean_window, number, period_return, subtract
from ashare_daily.screening.settings import StrategyConfig


NOTICE = "小样本验证版，仅基于量价规则，尚未做新闻、公告和研报核查"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def decimal_text(value):
    return str(value) if value is not None else None


def _dated(rows, target):
    result = {}
    duplicate = False
    for row in rows:
        day = row.get("trade_date", "")
        if day > target:
            continue
        if day in result:
            duplicate = True
        result[day] = row
    return result, duplicate


def evaluate_snapshot(snapshot: dict) -> dict:
    """Return stable calculations, including every configured stock's audit."""
    config = StrategyConfig.model_validate(snapshot["strategy_config"])
    target = snapshot["trade_date"]
    dates = [day for day in snapshot["trading_dates"] if day <= target][-config.history_days:]
    calendar_ok = bool(dates) and dates == sorted(set(dates)) and dates[-1] == target and not snapshot.get("calendar_issues")
    instruments = {item["symbol"]: item for item in snapshot["instruments"]}
    raw_by_symbol = {}
    for bar in snapshot["raw_bars"]:
        raw_by_symbol.setdefault(bar["symbol"], []).append(bar)
    series = snapshot.get("adjusted_data", {}).get("series", {})
    source_issues = snapshot.get("source_issues", {})

    def prices(symbol, security_type):
        package = series.get(symbol, {})
        rows, duplicate = _dated(package.get("bars", []), target)
        issues = list(package.get("issues", []))
        mode = "forward_adjusted" if security_type == "stock" else "index_native"
        if package.get("adjustment_mode") != mode or package.get("provider") != "baostock":
            issues.append("调整价格序列缺失或来源/口径不符")
        if not package.get("fetch_version"):
            issues.append("调整价格序列缺少统一获取版本")
        if package.get("price_unit") != ("CNY" if security_type == "stock" else "index_points"):
            issues.append("调整价格单位与证券类别不符")
        if duplicate:
            issues.append("调整价格存在重复日期")
        values = []
        raw, raw_duplicate = _dated(raw_by_symbol.get(symbol, []), target)
        if raw_duplicate:
            issues.append("未复权日线存在重复日期")
        for day in dates:
            row, raw_row = rows.get(day, {}), raw.get(day, {})
            value = number(row.get("close"))
            if security_type == "stock" and raw_row.get("tradestatus") is not True:
                value = None
            values.append(value if value is not None and value > 0 else None)
        if issues or not calendar_ok:
            values = [None] * len(dates)
        return values, list(dict.fromkeys(issues)), rows

    benchmark_inst = instruments.get(config.benchmark_id, {})
    benchmark_values, benchmark_issues, _ = prices(config.benchmark_id, "index")
    if benchmark_inst.get("security_type") != "index" or benchmark_inst.get("board") != "index":
        benchmark_issues.append("基准证券分类缺失或不是指数")
    benchmark_raw, duplicate = _dated(raw_by_symbol.get(config.benchmark_id, []), target)
    benchmark_today = benchmark_raw.get(target, {})
    benchmark_return = period_return(benchmark_values, config.return_days) if not benchmark_issues else None
    if benchmark_return is None:
        benchmark_issues.append("基准收益窗口不足或日期未对齐，不能计算相对收益")
    if not benchmark_today:
        benchmark_issues.append("基准分析日未复权展示行情缺失或过期")
    elif (benchmark_today.get("adjustment_mode") != "unadjusted" or benchmark_today.get("price_unit") != "index_points"
          or benchmark_today.get("amount_unit") != "CNY" or benchmark_today.get("quality_flags")):
        benchmark_issues.append("基准展示行情口径、单位或质量异常")
    benchmark_issues.extend(source_issues.get(config.benchmark_id, []))
    benchmark = {
        "symbol": config.benchmark_id, "name": benchmark_inst.get("name", config.benchmark_name),
        "actual_data_date": max(benchmark_raw, default=None),
        "display_close": benchmark_today.get("close"), "display_preclose": benchmark_today.get("preclose"),
        "daily_return": decimal_text(period_return([benchmark_today.get("preclose"), benchmark_today.get("close")], 1)),
        "period_return": decimal_text(benchmark_return), "price_unit": "index_points",
        "display_adjustment_mode": "unadjusted", "trend_adjustment_mode": "index_native",
        "issues": list(dict.fromkeys(benchmark_issues)),
    }
    evaluations, nonstocks = [], []
    for symbol, expected_type in sorted(snapshot["sample_types"].items()):
        inst = instruments.get(symbol, {})
        if expected_type != "stock":
            nonstocks.append({"symbol": symbol, "name": inst.get("name", symbol), "reason": "指数仅作基准，不进入股票筛选" if expected_type == "index" else "非配置股票样本"})
            continue
        raw, duplicate = _dated(raw_by_symbol.get(symbol, []), target)
        today = raw.get(target, {})
        values, price_issues, adjusted_rows = prices(symbol, "stock")
        issues = list(source_issues.get(symbol, [])) + price_issues
        if not calendar_ok:
            issues.append("分析交易日或交易日历不完整，不能建立固定窗口")
        if not today:
            issues.append("分析交易日行情缺失或过期")
        if any(raw.get(day, {}).get("adjustment_mode") != "unadjusted" or raw.get(day, {}).get("price_unit") != "CNY" or raw.get(day, {}).get("amount_unit") != "CNY" for day in dates):
            issues.append("未复权展示价格、成交额单位不符或日线缺失")
        valid_count = sum(
            value is not None and raw.get(day, {}).get("tradestatus") is True
            and isinstance(raw.get(day, {}).get("is_st"), bool)
            and (number(raw.get(day, {}).get("close")) or Decimal(0)) > 0
            and number(raw.get(day, {}).get("amount_cny")) is not None
            and number(raw.get(day, {}).get("amount_cny")) >= 0
            and not raw.get(day, {}).get("quality_flags")
            for day, value in zip(dates, values)
        )
        ma_short = mean_window(values, config.ma_short_days)
        ma_long = mean_window(values, config.ma_long_days)
        stock_return = period_return(values, config.return_days)
        relative_return = subtract(stock_return, benchmark_return)
        amounts = [raw.get(day, {}).get("amount_cny") if raw.get(day, {}).get("amount_unit") == "CNY" and raw.get(day, {}).get("tradestatus") is True else None for day in dates]
        amount = mean_window(amounts, config.amount_days, positive=False) if calendar_ok else None
        close = values[-1] if values else None
        conditions = []

        def condition(key, label, passed, reason):
            conditions.append({"id": key, "label": label, "status": "unknown" if passed is None else "pass" if passed else "fail", "reason": reason})

        identity_known = all(inst.get(key) is not None for key in ("security_type", "board", "exchange", "ipo_date", "status"))
        identity_pass = (inst.get("security_type") == "stock" and inst.get("board") == config.allowed_board and inst.get("exchange") in config.allowed_exchanges) if identity_known else None
        condition("identity", "沪深主板普通 A 股资格", identity_pass, "主数据明确且属于配置范围" if identity_pass else "证券主数据不足" if identity_pass is None else "证券不是配置的沪深主板普通 A 股")
        ipo, out = inst.get("ipo_date"), inst.get("out_date")
        listed = None
        if ipo and ipo > target or out and out <= target:
            listed = False
        elif ipo and inst.get("status") == "listed":
            listed = True
        condition("listed", "分析日期处于上市区间", listed, "按冻结的上市日期、退市日期和主数据事后核对" if listed else "上市区间不符" if listed is False else "上市状态或日期不足")
        st = today.get("is_st")
        condition("not_st", "分析日非 ST", not st if isinstance(st, bool) else None, "非 ST" if st is False else "分析日为 ST" if st is True else "分析日 ST 状态缺失")
        trading = today.get("tradestatus")
        condition("not_suspended", "分析日正常交易", trading if isinstance(trading, bool) else None, "正常交易" if trading is True else "分析日停牌" if trading is False else "分析日交易状态缺失")
        delisting = snapshot.get("eligibility_states", {}).get(symbol, {})
        delisting_value = delisting.get("delisting_period")
        evidence_valid = delisting.get("effective_date") == target and bool(delisting.get("evidence_id"))
        not_delisting = not delisting_value if isinstance(delisting_value, bool) and evidence_valid else None
        condition("not_delisting_period", "非退市整理期", not_delisting, "有对应分析日期的状态证据" if not_delisting else "分析日处于退市整理期" if not_delisting is False else "BaoStock 已取字段不含退市整理期；缺少对应日期独立证据，无法判断")
        history_pass = True if valid_count >= config.min_history_trading_days else None
        condition("history", f"有效历史不少于 {config.min_history_trading_days} 个交易日", history_pass, f"冻结窗口有效 {valid_count}/{len(dates)} 个交易日" if history_pass else f"有效历史不足：{valid_count} < {config.min_history_trading_days}；未缩短窗口或填充")
        condition("liquidity", f"{config.amount_days} 日平均成交额不少于 {config.min_avg_amount_cny} 元", amount >= config.min_avg_amount_cny if amount is not None else None, "成交额阈值通过" if amount is not None and amount >= config.min_avg_amount_cny else "平均成交额未达到阈值" if amount is not None else "成交额窗口缺失、停牌或单位不符")
        trend = close > ma_short > ma_long if all(value is not None for value in (close, ma_short, ma_long)) else None
        condition("trend", f"前复权 C(T) > MA{config.ma_short_days} > MA{config.ma_long_days}", trend, "趋势条件通过" if trend else "趋势严格不等式不成立" if trend is False else "前复权均线窗口不完整或获取版本无效")
        strength = relative_return > 0 if relative_return is not None else None
        condition("relative_strength", f"{config.return_days} 日相对基准收益 > 0", strength, "相对收益条件通过" if strength else "相对基准收益未大于 0" if strength is False else "股票或基准缺少完全对齐的收益窗口")
        condition("data_integrity", "日期、口径与数据校验", not issues, "冻结输入质量检查通过" if not issues else "；".join(dict.fromkeys(issues)))
        # Structural data errors are unknown, not ordinary failed strategy conditions.
        if issues:
            conditions[-1]["status"] = "unknown"
        failures = [item["reason"] for item in conditions if item["status"] == "fail"]
        unknowns = [item["reason"] for item in conditions if item["status"] == "unknown"]
        # Contradictory source data cannot support a numerical rejection. A known
        # out-of-scope identity can; otherwise separate data faults from rule fails.
        identity_rejected = any(item["id"] == "identity" and item["status"] == "fail" for item in conditions)
        status = "excluded" if identity_rejected else "data_insufficient" if issues else "excluded" if failures else "data_insufficient" if unknowns else "candidate"
        evaluations.append({
            "symbol": symbol, "name": inst.get("name", symbol), "security_type": inst.get("security_type"),
            "analysis_date": target, "actual_data_date": max(raw, default=None), "valid_history_count": valid_count,
            "display_close": today.get("close"), "display_daily_return": decimal_text(period_return([today.get("preclose"), today.get("close")], 1)),
            "price_unit": "CNY", "display_adjustment_mode": "unadjusted", "trend_adjustment_mode": "forward_adjusted",
            "adjusted_close": decimal_text(close), "ma_short": decimal_text(ma_short), "ma_long": decimal_text(ma_long),
            "period_return": decimal_text(stock_return), "benchmark_period_return": decimal_text(benchmark_return),
            "relative_return": decimal_text(relative_return), "avg_amount_cny": decimal_text(amount),
            "status": status, "rank": None, "conditions": conditions,
            "exclusion_reasons": failures, "data_issues": list(dict.fromkeys(unknowns)),
            "selection_reasons": [item["reason"] for item in conditions if item["status"] == "pass"] if status == "candidate" else [],
        })
    eligible = sorted((row for row in evaluations if row["status"] == "candidate"), key=lambda row: (Decimal(row["relative_return"]).copy_negate(), Decimal(row["avg_amount_cny"]).copy_negate(), row["symbol"]))
    for rank, row in enumerate(eligible, 1):
        if rank <= config.max_candidates:
            row["rank"] = rank
        else:
            row["status"] = "qualified_not_selected"
            row["exclusion_reasons"].append(f"条件通过，但排序超出前 {config.max_candidates} 个名额")
    candidates = eligible[:config.max_candidates]
    gaps = list(snapshot.get("calendar_issues", [])) + benchmark["issues"]
    gaps += [f"{row['symbol']}：{reason}" for row in evaluations for reason in row["data_issues"]]
    all_actual_dates = [bar["trade_date"] for bar in snapshot["raw_bars"] if bar["trade_date"] <= target]
    result = {
        "title": "今日方向简报 · 真实行情小样本", "notice": NOTICE, "mode": "research",
        "verification_kind": "offline_test" if snapshot.get("verification_kind") == "offline_test" else "local_real_data",
        "trade_date": target, "actual_market_date": max(all_actual_dates, default=None),
        "timezone": "Asia/Shanghai", "status": "partial" if gaps else "market_only",
        "scope": f"配置小样本：{len(evaluations)} 个股票样本、{len(nonstocks)} 个非股票/基准样本；非全市场。",
        "snapshot_id": snapshot["snapshot_id"], "config_hash": digest(snapshot["strategy_config"]),
        "strategy_version": config.strategy_version, "strategy_config": snapshot["strategy_config"],
        "metric_windows": {"ma_short": config.ma_short_days, "ma_long": config.ma_long_days, "period_return": config.return_days, "avg_amount_cny": config.amount_days},
        "benchmark": benchmark, "counts": {"stock_count": len(evaluations), "candidate_count": len(candidates),
            "excluded_count": sum(row["status"] == "excluded" for row in evaluations),
            "data_insufficient_count": sum(row["status"] == "data_insufficient" for row in evaluations),
            "stocks_with_data_gaps": sum(bool(row["data_issues"]) for row in evaluations),
            "qualified_not_selected_count": sum(row["status"] == "qualified_not_selected" for row in evaluations)},
        "candidates": candidates, "evaluations": evaluations, "non_stock_records": nonstocks,
        "gaps": list(dict.fromkeys(gaps)),
        "boundaries": [
            NOTICE,
            "这是按实际获取时间事后重建的量价检查，不是历史实时报告；当前主数据不等于历史时点主数据档案。",
            "展示价格为未复权；股票均线/跨日收益为同一次获取的前复权序列；指数使用原生点位，收益起止日完全对齐。",
            "有效历史只统计本次冻结窗口内的有效交易日，不代表上市以来全部历史。",
            "全市场涨跌家数、全市场成交额、行业轮动、资金流入、政策受益方向：未覆盖。",
            "新闻、公告、研报及业务催化：未覆盖；量价条件通过也不表示完成综合研究。",
            "退市整理期状态缺少独立日期证据时不具备筛选资格；不从名称、ST 标记或上市状态推断。",
            "没有经验证的上涨概率或收益保证；没有交易操作输出。",
        ],
    }
    if snapshot.get("target_is_trading") is False:
        result["status"] = "non_trading_day"
    result["result_hash"] = digest(result)
    return result
