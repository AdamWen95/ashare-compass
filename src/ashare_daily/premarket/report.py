"""One morning fact model for the reader and all exports."""
from __future__ import annotations

from copy import deepcopy
from datetime import date

from ..sector_selection import digest
from .contracts import SCHEMA, READABLE_SCHEMAS, STANCE_VERSION, RECOMMENDATION_VERSION, aware
from .decisions import decide_stance

NOTICE = "3–10个交易日短线波段研究；姿态是整体市场倾向，评分不是上涨概率。资料缺失不代表没有风险。"


def build_brief(*, schedule, market, stance, candidates, events, coverage, previous, generated_at) -> dict:
    if market.get("market_as_of_date") != schedule["market_as_of_date"]:
        raise ValueError("premarket_market_date_mismatch")
    generated = aware(generated_at)
    old_directions = {d["sector_id"] for d in (previous or {}).get("directions", [])}
    directions = []
    for sector in candidates.get("preselection", {}).get("directions", []):
        directions.append({**deepcopy(sector), "kind": "price_volume",
            "continuity": "延续观察" if sector["sector_id"] in old_directions else "本期量价方向",
            "reason": "上一完整交易日的行业涨跌幅及上涨广度通过规则",
            "invalidation": "趋势、市场广度或新事件削弱依据时重新判断"})
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
    old_ids = {r["security_id"] for r in (previous or {}).get("focus_candidates", [])}
    ids = {r["security_id"] for r in focus}
    report = {"schema_version": SCHEMA, "purpose": "production", "timezone": "Asia/Shanghai", **deepcopy(schedule),
        "title": "盘前研究简报" if schedule["is_trading_day"] else "休市资讯与下一个交易日观察准备",
        "notice": NOTICE, "generated_at": generated.isoformat(), "published_at": None,
        "publication_status": "draft", "stance": deepcopy(stance), "market": deepcopy(market),
        "directions": directions, "focus_candidates": focus,
        "pending_candidates": deepcopy(candidates.get("pending_candidates", [])),
        "excluded_candidates": deepcopy(candidates.get("excluded", [])),
        "screening_audit": deepcopy(candidates.get("preselection", {}).get("audit", [])),
        "overnight_events": deepcopy(events), "coverage": deepcopy(coverage),
        "score_used_for_priority": True, "research_horizon_sessions": [3, 10],
        "rule_versions": {"stance": STANCE_VERSION, "recommendation": RECOMMENDATION_VERSION},
        "baseline_ids": list(candidates.get("preselection", {}).get("baseline_ids", [])),
        "changes_since_previous": {"comparable": previous is not None, "added_ids": sorted(ids-old_ids),
            "removed_ids": sorted(old_ids-ids), "previous_stance": (previous or {}).get("stance", {}).get("stance")},
        "input_hashes": {"market": digest(market), "candidates": digest(candidates), "events": digest(events), "coverage": digest(coverage)},
        "research_status": "ready" if market.get("status") == "ready" and coverage.get("ready") is True else "partial"}
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
    allowed_stances = {"attack", "defend", "hold"} if legacy else {"attack", "defend", "hold", "undetermined"}
    expected_version = "premarket-stance-v1" if legacy else STANCE_VERSION
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
            has_direction=any(d.get("kind") == "price_volume" for d in report.get("directions", [])), adverse_event=adverse)
        if report["stance"] != expected:
            raise ValueError("premarket_stance_evidence_mismatch")
    ids = [r["security_id"] for r in report["focus_candidates"]]
    if len(ids) > 5 or len(ids) != len(set(ids)) or (ids and stance != "attack"):
        raise ValueError("premarket_focus_invalid")


def report_sections(report: dict) -> list:
    stance = report["stance"]
    overview = [("paragraph", stance["label"] + "：" + "；".join(stance["reasons"])),
        ("table", (["时间口径", "记录"], [[label, report[key]] for label, key in (
            ("早报日期", "report_date"), ("目标交易日", "target_session_date"), ("行情截至", "market_as_of_date"),
            ("消息截点", "news_cutoff_at"), ("实际生成", "generated_at"), ("实际发布", "published_at"))]))]
    overview.extend(("paragraph", "现有证据：" + item) for item in stance.get("analysis", []))
    overview.extend(("paragraph", "资料限制：" + item) for item in stance.get("limitations", []))
    directions = [("paragraph", d.get("name", d["sector_id"]) + "：" + d["reason"] + "；" + d["continuity"]) for d in report["directions"]]
    stocks = []
    for label, key in (("重点关注", "focus_candidates"), ("待核查观察", "pending_candidates")):
        stocks.append(("paragraph", label))
        for row in report[key]:
            stocks.extend([("subheading", f"{row.get('name', '')}（{row.get('symbol', '')}）"),
                ("paragraph", "技术评分：" + str(row.get("technical_score", "未取得"))),
                ("paragraph", "；".join(row.get("reasons", [row.get("reason", "资料待查")]))),
                ("paragraph", "观察条件：" + "；".join(row.get("watch_conditions", []))),
                ("paragraph", "失效条件：" + "；".join(row.get("invalidation_conditions", []))),
                ("paragraph", row.get("trade_status_notice", "本日交易状态待核验"))])
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
    if not report["focus_candidates"]:
        stocks.insert(0, ("paragraph", "本期没有完成条件及资料核验的重点推荐，不强行凑数。"))
    news = []
    for event in report["overnight_events"]:
        news.append(("paragraph", ("此前已观察，保留为背景：" if event.get("is_background") else "") + event["text"]))
        news.extend(("paragraph", source.get("public_url", source["original_url"])) for source in event.get("sources", []))
    return [("市场姿态与时间", overview), ("隔夜变化", news or [("paragraph", "暂无已核验的新事件，不代表没有消息。")]),
            ("重点方向", directions), ("关注股票与风险", stocks),
            ("完整筛选明细", [("table", (["证券", "名称", "量价条件", "基本资格", "指标"], [
                [r.get("symbol"), r.get("name"), r.get("technical_status"), r.get("eligibility_status"), str(r.get("metrics", {}))]
                for r in report.get("screening_audit", [])]))]),
            ("资料覆盖", [("paragraph", str(report["coverage"])), ("paragraph", "规则为研究启发式，尚未验证盈利或胜率。")])]
