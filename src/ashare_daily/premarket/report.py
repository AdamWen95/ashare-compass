"""One morning fact model for the reader and all exports."""
from __future__ import annotations

from copy import deepcopy
from datetime import date

from ..sector_selection import digest
from .contracts import (SCHEMA, READABLE_SCHEMAS, STANCE_VERSION, RECOMMENDATION_VERSION,
    OBSERVATION_SCHEMA, INDEPENDENT_STANCE_VERSION, OBSERVATION_VERSION, aware, decimal)
from .decisions import decide_stance

NOTICE = "3–10个交易日短线波段研究；姿态是整体市场倾向，评分不是上涨概率。资料缺失不代表没有风险。"


def build_brief(*, schedule, market, stance, candidates, events, coverage, previous, generated_at) -> dict:
    if market.get("market_as_of_date") != schedule["market_as_of_date"]:
        raise ValueError("premarket_market_date_mismatch")
    generated = aware(generated_at)
    preselection = candidates.get("preselection", {})
    observation = preselection.get("observation_only") is True
    if observation and preselection.get("market_as_of_date") != schedule["market_as_of_date"]:
        raise ValueError("premarket_observation_date_mismatch")
    if observation and preselection.get("rule_version") != OBSERVATION_VERSION:
        raise ValueError("premarket_observation_version_invalid")
    schema = OBSERVATION_SCHEMA if observation else SCHEMA
    recommendation = OBSERVATION_VERSION if observation else RECOMMENDATION_VERSION
    comparable = (previous is not None and previous.get("schema_version") == schema
        and previous.get("rule_versions", {}).get("recommendation") == recommendation)
    comparable_previous = previous if comparable else {}
    old_directions = {d["sector_id"] for d in comparable_previous.get("directions", [])}
    directions = []
    for sector in preselection.get("directions", []):
        direction = {**deepcopy(sector), "kind": "relative_strength" if observation else "price_volume",
            "continuity": "延续观察" if sector["sector_id"] in old_directions else "本期量价方向",
            "reason": "上一完整交易日的行业涨跌幅及上涨广度通过规则",
            "invalidation": "趋势、市场广度或新事件削弱依据时重新判断"}
        if observation:
            metrics = sector["relative_metrics"]
            label = "相对强势" if metrics["absolute_up"] else "相对抗跌"
            direction.update(continuity="延续观察" if sector["sector_id"] in old_directions else "本期相对表现观察",
                reason=f"{label}：行业单日平均涨跌幅高于全市场，上涨家数占比不低于全市场；单日强弱不代表上涨趋势",
                invalidation="若相对优势消失、广度转弱或出现不利证据，重新核验观察依据")
        directions.append(direction)
    event_sectors = []
    for event in events:
        if event.get("validation_status") != "verified":
            continue
        for sector in event.get("sector_ids", []):
            if sector not in {d["sector_id"] for d in directions} and sector not in event_sectors and len(event_sectors) < 2:
                event_sectors.append(sector)
                directions.append({"sector_id": sector, "name": sector, "kind": "event", "reason": event["text"],
                    "continuity": "事件关注，待行情确认", "invalidation": "关联依据或事件有效性不成立", "event_id": event["event_id"]})
    focus = deepcopy(candidates.get("focus_candidates", []))
    watch = deepcopy(candidates.get("watch_candidates", []))
    candidate_key = "watch_candidates" if observation else "focus_candidates"
    old_ids = {r["security_id"] for r in comparable_previous.get(candidate_key, [])}
    ids = {r["security_id"] for r in (watch if observation else focus)}
    report = {"schema_version": schema, "purpose": "production", "timezone": "Asia/Shanghai", **deepcopy(schedule),
        "title": "盘前研究简报" if schedule["is_trading_day"] else "休市资讯与下一个交易日观察准备",
        "notice": NOTICE, "generated_at": generated.isoformat(), "published_at": None,
        "publication_status": "draft", "stance": deepcopy(stance), "market": deepcopy(market),
        "directions": directions, "focus_candidates": focus,
        "pending_candidates": deepcopy(candidates.get("pending_candidates", [])),
        "excluded_candidates": deepcopy(candidates.get("excluded", [])),
        "screening_audit": deepcopy(candidates.get("preselection", {}).get("audit", [])),
        "overnight_events": deepcopy(events), "coverage": deepcopy(coverage),
        "score_used_for_priority": True, "research_horizon_sessions": [3, 10],
        "rule_versions": {"stance": INDEPENDENT_STANCE_VERSION if observation else STANCE_VERSION,
            "recommendation": recommendation},
        "baseline_ids": list(candidates.get("preselection", {}).get("baseline_ids", [])),
        "changes_since_previous": {"comparable": comparable, "added_ids": sorted(ids-old_ids) if comparable else [],
            "removed_ids": sorted(old_ids-ids) if comparable else [],
            "previous_stance": comparable_previous.get("stance", {}).get("stance")},
        "input_hashes": {"market": digest(market), "candidates": digest(candidates), "events": digest(events), "coverage": digest(coverage)},
        "research_status": "ready" if market.get("status") == "ready" and coverage.get("ready") is True else "partial"}
    if observation:
        report.update(watch_candidates=watch, observation_selection_id=preselection.get("selection_id"))
    validate_brief(report)
    return report


def validate_brief(report: dict) -> None:
    if report.get("schema_version") not in READABLE_SCHEMAS or report.get("purpose") != "production":
        raise ValueError("premarket_report_schema_invalid")
    market_day, report_day, target = [date.fromisoformat(report[k]) for k in ("market_as_of_date", "report_date", "target_session_date")]
    if not market_day < report_day <= target or report["market"]["market_as_of_date"] != str(market_day):
        raise ValueError("premarket_report_date_invalid")
    for key in ("generated_at", "news_cutoff_at", "publication_due_at"):
        aware(report[key])
    stance = report["stance"].get("stance")
    legacy = report["schema_version"] == "premarket-brief-v1"
    observation = report["schema_version"] == OBSERVATION_SCHEMA
    allowed_stances = {"attack", "defend", "hold"} if legacy else {"attack", "defend", "hold", "undetermined"}
    expected_version = "premarket-stance-v1" if legacy else INDEPENDENT_STANCE_VERSION if observation else STANCE_VERSION
    if (stance not in allowed_stances or report.get("score_used_for_priority") is not True
            or report.get("rule_versions", {}).get("stance") != expected_version
            or report["stance"].get("rule_version") != expected_version):
        raise ValueError("premarket_report_policy_invalid")
    if legacy and stance == "attack" and (report["market"].get("status") != "ready" or report["coverage"].get("ready") is not True):
        raise ValueError("premarket_attack_without_evidence")
    if not legacy:
        adverse = any(e.get("validation_status") == "verified" and e.get("risk_status") == "adverse"
            and e.get("impact_scope") == "market" for e in report.get("overnight_events", []))
        expected = decide_stance(report["market"], news_ready=report["coverage"].get("ready") is True,
            has_direction=any(d.get("kind") == "price_volume" for d in report.get("directions", [])),
            adverse_event=adverse, independent=observation)
        if report["stance"] != expected:
            raise ValueError("premarket_stance_evidence_mismatch")
    ids = [r["security_id"] for r in report["focus_candidates"]]
    if len(ids) > 5 or len(ids) != len(set(ids)) or (ids and stance != "attack"):
        raise ValueError("premarket_focus_invalid")
    if observation:
        _validate_observation(report)


def _validate_observation(report):
    """Bind visible observations to the frozen audit and their relative facts."""
    def require(condition):
        if not condition:
            raise ValueError("premarket_observation_contract_invalid")
    def close(left, right):
        # Selection serializes deterministic Decimal calculations as JSON numbers.
        # Match the upstream boundary's sub-nanopoint float-rounding allowance.
        return abs(left - right) <= decimal("0.000000001")
    try:
        require(report["rule_versions"].get("recommendation") == OBSERVATION_VERSION)
        require(not report["focus_candidates"] and not report["pending_candidates"])
        rows, audit = report["watch_candidates"], report.get("screening_audit", [])
        require(isinstance(rows, list) and len(rows) <= 5)
        ids = [row["security_id"] for row in rows]
        audit_ids = [row["security_id"] for row in audit]
        require(all(isinstance(identity, str) and identity for identity in ids + audit_ids))
        require(len(ids) == len(set(ids)) and len(audit_ids) == len(set(audit_ids)))
        lookup = {row["security_id"]: row for row in audit}
        sectors = [d for d in report["directions"] if d.get("kind") != "event"]
        sector_ids = [d["sector_id"] for d in sectors]
        require(len(sectors) <= 3 and len(sector_ids) == len(set(sector_ids)))
        if rows or sectors or audit:
            require(isinstance(report.get("observation_selection_id"), str) and bool(report["observation_selection_id"]))
        sector_facts = {}
        for direction in sectors:
            require(direction.get("kind") == direction.get("direction_kind") == "relative_strength")
            facts = direction["relative_metrics"]
            mean, median, breadth, market_mean, market_breadth, excess = [decimal(facts[key]) for key in (
                "mean_change_pct", "median_change_pct", "advancing_fraction", "market_mean_change_pct",
                "market_advancing_fraction", "excess_market_pp")]
            require(0 <= market_breadth <= breadth <= 1 and excess > 0 and close(mean - market_mean, excess))
            require(type(facts["member_count"]) is int and facts["member_count"] >= 5)
            require(type(facts["absolute_up"]) is bool and facts["absolute_up"] == (mean > 0))
            sector_facts[direction["sector_id"]] = (mean, market_mean)
        for row in rows:
            require(row["security_id"] in lookup)
            frozen = lookup[row["security_id"]]
            require(all(row.get(key) == frozen.get(key) for key in
                ("symbol", "name", "technical_status", "eligibility_status")))
            require(row.get("eligibility_status") != "fail" and row.get("technical_status") != "not_applicable")
            require(row.get("company_review", {}).get("risk_status") != "adverse")
            require(not any(e.get("validation_status") == "verified" and e.get("risk_status") == "adverse"
                and row["security_id"] in e.get("security_ids", []) for e in report["overnight_events"]))
            for key in ("reasons", "watch_conditions", "invalidation_conditions", "observation_risks"):
                require(isinstance(row.get(key), list) and bool(row[key]) and all(isinstance(t, str) and t.strip() for t in row[key]))
            facts = row["relative_observation"]
            require(facts["kind"] == "single_session_relative_strength"
                and facts["market_as_of_date"] == report["market_as_of_date"])
            sector = facts["sector_id"]
            require(sector == row["primary_sector_id"] and sector in row["sector_ids"] and sector in sector_facts)
            mean, market_mean = sector_facts[sector]
            require(decimal(facts["sector_mean_change_pct"]) == mean and decimal(facts["market_mean_change_pct"]) == market_mean)
            stock = decimal(facts["stock_change_pct"])
            require(close(stock - mean, decimal(facts["excess_sector_pp"], positive=True))
                and close(stock - market_mean, decimal(facts["excess_market_pp"], positive=True)))
            require(decimal(facts["amount_cny"], positive=True) >= 50_000_000)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("premarket_observation_contract_invalid") from None


def direction_facts(direction):
    facts = direction.get("relative_metrics")
    if not facts:
        return []
    return [f"单日行业平均 {decimal(facts['mean_change_pct']):.2f}%，中位数 {decimal(facts['median_change_pct']):.2f}%；"
        f"全市场平均 {decimal(facts['market_mean_change_pct']):.2f}%；相对全市场 {decimal(facts['excess_market_pp']):.2f} 个百分点",
        f"行业上涨家数占比 {decimal(facts['advancing_fraction']) * 100:.2f}%，全市场 {decimal(facts['market_advancing_fraction']) * 100:.2f}%；行业样本 {facts['member_count']} 只"]


def observation_facts(row):
    facts = row.get("relative_observation")
    if not facts:
        return []
    return [f"单日涨跌幅 {decimal(facts['stock_change_pct']):.2f}%；行业平均 {decimal(facts['sector_mean_change_pct']):.2f}%，"
        f"全市场平均 {decimal(facts['market_mean_change_pct']):.2f}%",
        f"相对行业 {decimal(facts['excess_sector_pp']):.2f} 个百分点，相对全市场 {decimal(facts['excess_market_pp']):.2f} 个百分点；"
        f"成交额 {decimal(facts['amount_cny']) / 10000:.2f} 万元；行情日 {facts['market_as_of_date']}"]


def report_sections(report: dict) -> list:
    stance = report["stance"]
    overview = [("paragraph", stance["label"] + "：" + "；".join(stance["reasons"])),
        ("table", (["时间口径", "记录"], [[label, report[key]] for label, key in (
            ("早报日期", "report_date"), ("目标交易日", "target_session_date"), ("行情截至", "market_as_of_date"),
            ("消息截点", "news_cutoff_at"), ("实际生成", "generated_at"), ("实际发布", "published_at"))]))]
    overview.extend(("paragraph", "现有证据：" + item) for item in stance.get("analysis", []))
    overview.extend(("paragraph", "资料限制：" + item) for item in stance.get("limitations", []))
    observation = report["schema_version"] == OBSERVATION_SCHEMA
    directions = []
    for direction in report["directions"]:
        directions.append(("paragraph", direction.get("name", direction["sector_id"]) + "：" + direction["reason"] + "；" + direction["continuity"]))
        directions.extend(("paragraph", fact) for fact in direction_facts(direction))
        if observation:
            directions.append(("paragraph", "失效条件：" + direction["invalidation"]))
    stocks = []
    groups = (("观察名单", "watch_candidates"),) if observation else (("重点关注", "focus_candidates"), ("待核查观察", "pending_candidates"))
    for label, key in groups:
        if not observation:
            stocks.append(("paragraph", label))
        for row in report[key]:
            stocks.extend([("subheading", f"{row.get('name', '')}（{row.get('symbol', '')}）"),
                ("paragraph", "技术评分：" + ("未取得" if row.get("technical_score") is None else str(row["technical_score"]))),
                ("paragraph", "；".join(row.get("reasons", [row.get("reason", "资料待查")]))),
                ("paragraph", "观察条件：" + "；".join(row.get("watch_conditions", []))),
                ("paragraph", "失效条件：" + "；".join(row.get("invalidation_conditions", []))),
                ("paragraph", row.get("trade_status_notice", "本日交易状态待核验"))])
            stocks.extend(("paragraph", fact) for fact in observation_facts(row))
            stocks.extend(("paragraph", "风险：" + text) for text in row.get("observation_risks", []))
            review = row.get("company_review", {})
            stocks.extend(("paragraph", "公告待查：" + text) for text in review.get("gaps", []))
            stocks.extend(("paragraph", "公告风险定位：" + text) for text in review.get("risk_flags", []))
            for announcement in review.get("announcements", []):
                stocks.append(("paragraph", "公告：" + announcement["title"] + " · " + announcement["published_at"]
                    + " · " + announcement["original_url"] + ("；正文已取得，语义待核验" if announcement.get("body_status") == "extracted" else "；正文未取得")))
            stocks.extend(("paragraph", "财务风险：" + text) for text in review.get("financial_risk_flags", []))
            stocks.extend(("paragraph", "财务缺口：" + text) for text in review.get("financial_gaps", []))
            if review.get("financial_facts"):
                stocks.append(("paragraph", "财务原始事实（保留来源口径）：" + str(review["financial_facts"])))
    if observation:
        stocks.insert(0, ("paragraph", "观察名单独立于市场姿态；名单内只列研究条件，不构成买入指令。" if report["watch_candidates"] else "本期没有符合相对观察条件的股票，不强行凑数。"))
    elif not report["focus_candidates"]:
        stocks.insert(0, ("paragraph", "本期没有完成条件及资料核验的重点推荐，不强行凑数。"))
    news = []
    for event in report["overnight_events"]:
        news.append(("paragraph", ("此前已观察，保留为背景：" if event.get("is_background") else "") + event["text"]))
        news.extend(("paragraph", source.get("public_url", source["original_url"])) for source in event.get("sources", []))
    return [("市场姿态与时间", overview),
            ("相对表现观察方向" if observation else "重点方向", directions),
            ("观察名单与风险" if observation else "关注股票与风险", stocks),
            ("隔夜变化", news or [("paragraph", "暂无已核验的新事件，不代表没有消息。")]),
            ("完整筛选明细", [("table", (["证券", "名称", "量价条件", "基本资格", "指标"], [
                [r.get("symbol"), r.get("name"), r.get("technical_status"), r.get("eligibility_status"), str(r.get("metrics", {}))]
                for r in report.get("screening_audit", [])]))]),
            ("资料覆盖", [("paragraph", str(report["coverage"])), ("paragraph", "规则为研究启发式，尚未验证盈利或胜率。")])]
