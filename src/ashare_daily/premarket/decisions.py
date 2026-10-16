"""Versioned, deterministic overall stance; never an account instruction."""
from .contracts import INDEPENDENT_STANCE_VERSION, STANCE_VERSION, decimal


def decide_stance(market: dict, *, news_ready: bool, has_direction: bool,
                  adverse_event: bool = False, independent: bool = False) -> dict:
    # Producer-verified components remain independent. A subset's sample
    # breadth is deliberately never used as whole-market breadth.
    components = market.get("components", {})
    def usable(name):
        return components.get(name) == "ready" if components else market.get("status") == "ready"
    breadth = middle = close = average = previous = momentum = amount = mean_amount = None
    if usable("breadth"):
        try:
            value, median = decimal(market["advancing_fraction"]), decimal(market["median_change_pct"])
            if not 0 <= value <= 1: raise ValueError("breadth_bounds")
            breadth, middle = value, median
        except (KeyError, TypeError, ValueError):
            pass
    index = market.get("benchmark") or {}
    if usable("trend"):
        try:
            values = [decimal(index[k], positive=True) for k in ("close", "ma20", "previous_ma20")]
            change = decimal(index["return5"])
            if index.get("market_as_of_date") != market.get("market_as_of_date"):
                raise ValueError("index_date_mismatch")
            close, average, previous = values
            momentum = change
        except (KeyError, TypeError, ValueError):
            pass
    if usable("activity"):
        try:
            values = decimal(index["amount"], nonnegative=True), decimal(index["previous_mean_amount5"], positive=True)
            if index.get("market_as_of_date") != market.get("market_as_of_date"):
                raise ValueError("index_date_mismatch")
            amount, mean_amount = values
        except (KeyError, TypeError, ValueError):
            pass
    used = [name for name, value in (("breadth", breadth), ("trend", close), ("activity", amount)) if value is not None]
    analysis, limits = [], list(market.get("preparation_limitations", []))
    if breadth is not None:
        analysis.append(f"已核验全市场上涨家数占比 {breadth * 100:.1f}%，涨跌幅中位数 {middle}%")
    else:
        limits.append("全市场上涨广度未完整核验，局部股票样本不代表全市场")
    if close is not None:
        analysis.append(f"上证指数 {close}，MA20 为 {average}，五日变化 {momentum * 100:.2f}%")
    else:
        limits.append("指数趋势资料未完成核验，本次不能据此判断指数方向")
    if amount is not None:
        analysis.append("指数成交额不低于前五日均值" if amount >= mean_amount else "指数成交额低于前五日均值")
    else:
        limits.append("指数成交活跃度未完成核验，不能声称已经放量确认")
    if news_ready is not True:
        limits.append("消息或公司资料覆盖尚不完整；以下按已有有效量价判断，不代表不存在未发现风险")
    scope = ("whole_market_and_index" if breadth is not None and close is not None else
             "whole_market_breadth" if breadth is not None else "sse_index_trend" if close is not None else "none")
    quality = "insufficient" if scope == "none" else "partial" if limits else "complete"
    def result(stance, reason, reasons, bias="mixed"):
        label = {"attack": "进攻", "defend": "减仓防守", "hold": "按兵不动", "undetermined": "暂无法判断"}[stance]
        if quality == "partial" and stance in {"attack", "defend"}:
            label = ("进攻倾向" if stance == "attack" else "防守倾向") + "（依据有限）"
        return {"rule_version": INDEPENDENT_STANCE_VERSION if independent else STANCE_VERSION, "stance": stance,
                "label": label, "directional_bias": bias,
                "reason_code": reason, "reasons": reasons, "scope": "overall_market_research",
                "basis_scope": scope, "used_components": used if scope != "none" else [],
                "analysis": analysis, "limitations": limits, "evidence_status": quality,
                "conditional": quality != "complete", **({} if independent else {"direction_available": has_direction is True}),
                "probability": None, "validation_status": "heuristic_not_performance_validated",
                "invalidation": ["趋势、市场广度、成交活跃度或事件证据变化时重新判断"],
                "notice": "整体市场研究倾向，不对应个人持仓或交易数量"}
    if scope == "none":
        return result("undetermined", "no_valid_directional_evidence", ["尚无已核验的指数趋势或全市场广度，保留现有事实，但不能编造方向"], "unknown")
    if close is not None and breadth is not None and close < average and breadth <= decimal("0.4"):
        return result("defend", "trend_and_breadth_weak", ["指数低于 MA20，市场上涨家数占比不高于 40%"], "bearish")
    if breadth is not None and breadth <= decimal("0.3") and middle < 0:
        return result("defend", "declines_spreading", ["已核验上涨广度不高于 30%，中位数为负，市场偏弱"], "bearish")
    if breadth is None and close is not None and close < average and average < previous and momentum < 0:
        return result("defend", "index_only_weak", ["已有指数趋势偏弱，倾向防守；尚不能外推为全部股票同步走弱"], "bearish")
    trend_up = None if close is None else close > average and average >= previous and momentum > 0
    breadth_up = None if breadth is None else breadth >= decimal("0.6")
    active = None if amount is None else amount >= mean_amount
    positive = active is not False and ((trend_up is True and breadth_up is not False) or (trend_up is None and breadth_up is True))
    if positive and adverse_event:
        return result("hold", "event_pending", ["偏强量价受到已核验且未解决的市场反面证据削弱，等待风险条件澄清"])
    if positive and not independent and has_direction is not True:
        return result("hold", "no_qualified_direction", ["已有市场指标偏强，但尚无通过规则的关注方向，等待具体方向确认"], "bullish")
    if positive:
        if trend_up is True and breadth_up is True and active is True:
            reason = "指数趋势、市场上涨广度和成交活跃度共同支持进攻倾向" if independent else "指数趋势、市场上涨广度和成交活跃度共同支持，存在合格关注方向"
            return result("attack", "trend_breadth_activity", [reason], "bullish")
        reason = ("已有有效市场指标偏强，给出条件性进攻倾向；缺失项仍待确认" if independent else
                  "已有有效指标偏强且存在合格关注方向，给出条件性进攻倾向；缺失项仍待确认")
        return result("attack", "available_evidence_positive", [reason], "bullish")
    return result("hold", "mixed", ["已有有效信号分歧或未达到偏强条件；等待信号改善，原因不是单纯缺少资料"])
