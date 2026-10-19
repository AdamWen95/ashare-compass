"""Bounded relative-strength observations independent of the market stance."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from ..sector_selection import verify_selection
from .contracts import decimal

VERSION = "premarket-observation-v1"


def _indexed(values):
    rows = {row["security_id"]: row for row in values}
    if len(rows) != len(values):
        raise ValueError("premarket_duplicate_identity")
    return rows


def _number(value):
    try:
        return decimal(value)
    except (ValueError, TypeError):
        return None


def _text(value):
    return format(decimal(value), ".2f")


def _relative(member, row, sectors, target):
    relative = member.get("relative_observation", {})
    sector = sectors.get(relative.get("sector_id"))
    if (not sector or relative.get("sector_id") not in member.get("sector_ids", [])
            or relative.get("market_as_of_date") != target
            or relative.get("kind") != "single_session_relative_strength"):
        raise ValueError("premarket_relative_binding_mismatch")
    if member.get("exchange") and member.get("code"):
        expected = {"SSE": "sh.", "SZSE": "sz."}.get(member["exchange"], "unknown.") + member["code"]
        if row.get("symbol") != expected:
            raise ValueError("premarket_candidate_identity_binding_mismatch")
    if member.get("name") is not None and member["name"] != row.get("name"):
        raise ValueError("premarket_candidate_identity_binding_mismatch")
    try:
        stock, market, industry = (decimal(relative[key]) for key in
            ("stock_change_pct", "market_mean_change_pct", "sector_mean_change_pct"))
        excess_market, excess_sector = (decimal(relative[key], positive=True) for key in
            ("excess_market_pp", "excess_sector_pp"))
        metrics = sector["relative_metrics"]
        # Upstream uses rounded deterministic JSON numbers, so allow only sub-nanopoint rounding.
        epsilon = Decimal("0.000000001")
        consistent = (abs(stock - market - excess_market) <= epsilon
            and abs(stock - industry - excess_sector) <= epsilon
            and abs(decimal(metrics["mean_change_pct"]) - industry) <= epsilon
            and abs(decimal(metrics["market_mean_change_pct"]) - market) <= epsilon
            and decimal(relative["amount_cny"]) >= 50_000_000)
    except (ValueError, KeyError, TypeError):
        consistent = False
    if not consistent:
        raise ValueError("premarket_relative_numeric_binding_mismatch")
    return deepcopy(relative), sector


def _historical_ranking(row):
    metrics = row.get("metrics") or {}
    historical = row.get("technical_status") in {"pass", "fail"}
    strength = _number(metrics.get("relative_return_20")) if historical else None
    amount = _number(metrics.get("avg_amount_20_cny")) if historical else None
    return strength, amount if amount is not None and amount >= 0 else None


def _priority(row):
    score = row["technical_score"]
    strength, amount = _historical_ranking(row)
    relative = row["relative_observation"]
    return (score is None, -score if score is not None else 0,
        strength is None, -strength if strength is not None else 0,
        -decimal(relative["excess_market_pp"]), -decimal(relative["excess_sector_pp"]),
        amount is None, -amount if amount is not None else 0,
        -decimal(relative["amount_cny"]), row["security_id"])


def preselect_observations(selection, evaluations, reference):
    verify_selection(selection)
    if (selection.get("selection_verified") is not True
            or selection.get("rule_version") not in {"premarket-relative-observation-v1", "premarket-relative-observation-v2"}
            or reference.get("selection_id") != selection["selection_id"]
            or reference.get("trade_date") != selection["target_date"]):
        raise ValueError("premarket_score_binding_mismatch")
    members, rows, scores = (_indexed(values) for values in
        (selection["members"], evaluations, reference.get("records", [])))
    if set(rows) != set(members) or not set(scores) <= set(members) or len(members) > 15:
        raise ValueError("premarket_candidate_binding_mismatch")
    sectors = sorted(selection.get("selected_sectors", []), key=lambda s: (s["selected_rank"], s["sector_id"]))
    indexed = {sector["sector_id"]: sector for sector in sectors}
    if len(sectors) != len(indexed) or len(sectors) > 3:
        raise ValueError("premarket_direction_binding_mismatch")
    pools, eligible, excluded = {key: [] for key in indexed}, [], []
    for identity, raw in rows.items():
        relative, sector = _relative(members[identity], raw, indexed, selection["target_date"])
        score = scores.get(identity, {})
        if any(key in score and score[key] != raw.get(key) for key in ("symbol", "name", "eligibility_status")):
            raise ValueError("premarket_score_identity_binding_mismatch")
        if "baseline_technical_status" in score and score["baseline_technical_status"] != raw.get("technical_status"):
            raise ValueError("premarket_score_identity_binding_mismatch")
        failed = raw.get("eligibility_status") == "fail" or any(
            condition.get("status") == "fail" and condition.get("required", True)
            for condition in raw.get("eligibility_conditions", []))
        if failed or raw.get("technical_status") == "not_applicable":
            excluded.append({"security_id": identity, "reason": "qualification_not_passed" if failed else "technical_not_applicable",
                "exclusion_reasons": deepcopy(raw.get("exclusion_reasons", []))})
            continue
        value = score.get("score")
        available = (score.get("status") == "available" and type(value) is int and 0 <= value <= 100
            and raw.get("technical_status") in {"pass", "fail"})
        ranking_ready = all(value is not None for value in _historical_ranking(raw))
        row = deepcopy(raw)
        row.update(technical_score=value if available else None,
            score_components=deepcopy(score.get("components", [])) if available else [],
            ranking_metrics_status="available" if ranking_ready else "unavailable",
            sector_ids=[key for key in members[identity].get("sector_ids", []) if key in indexed],
            primary_sector_id=sector["sector_id"], sector_name=sector.get("name", sector["sector_id"]),
            relative_observation=relative, research_status="preselected", current_trade_status="unverified",
            trade_status_notice="报告日交易状态待核验",
            observation_status="verified" if available and ranking_ready and raw.get("technical_status") == "pass"
                and raw.get("eligibility_status") == "pass" else "conditional")
        pools[sector["sector_id"]].append(row)
        eligible.append(row)
    ordered = sorted(eligible, key=_priority)
    chosen = [min(pools[sector["sector_id"]], key=_priority) for sector in sectors if pools[sector["sector_id"]]]
    seen = {row["security_id"] for row in chosen}
    chosen.extend(row for row in ordered if row["security_id"] not in seen)
    baseline = sorted((row for row in eligible if row.get("technical_status") in {"pass", "fail"}
        and _number((row.get("metrics") or {}).get("relative_return_20")) is not None),
        key=lambda row: (-decimal(row["metrics"]["relative_return_20"]), row["security_id"]))
    return {"rule_version": 'premarket-observation-v2' if selection.get('research_coverage') else VERSION,
        **({'research_coverage':deepcopy(selection['research_coverage'])} if selection.get('research_coverage') else {}),
        "observation_only": True, "score_used_for_priority": True,
        "selection_id": selection["selection_id"], "market_as_of_date": selection["target_date"],
        "candidates": deepcopy(chosen[:5]), "pending": [], "excluded": excluded,
        "qualified_count": sum(row.get("eligibility_status") == "pass" and row.get("technical_status") == "pass" for row in eligible),
        "directions": deepcopy(sectors), "baseline_ids": [row["security_id"] for row in baseline[:5]],
        "audit": deepcopy(evaluations), "notice": "相对强势仅供条件观察；技术评分用于研究排序，不是上涨概率；公司核查最多5只"}


def _details(values):
    result = []
    for value in values:
        if isinstance(value, str) and value:
            result.append(value)
        elif isinstance(value, dict):
            text = "：".join(str(value[key]) for key in ("label", "reason") if value.get(key))
            if text:
                result.append(text)
    return result


def _observation_text(row):
    relative = row["relative_observation"]
    change = decimal(relative["stock_change_pct"])
    kind = "相对抗跌" if change < 0 else "单日相对强势"
    reasons = [f"{relative['market_as_of_date']}属于{row['sector_name']}的{kind}观察：当日涨跌幅{_text(change)}%，"
        f"高于行业均值{_text(relative['excess_sector_pp'])}个百分点、高于市场均值{_text(relative['excess_market_pp'])}个百分点；"
        f"当日成交额{_text(decimal(relative['amount_cny']) / 10000)}万元"]
    risks = ["单日相对强弱不能证明未来上涨；资格与行情核验适用于行情日，报告日交易状态仍待核验"]
    if relative.get('comparison_scope') == 'observed_sample':
        reasons = [text.replace('市场均值','已核验样本均值') for text in reasons]
        risks.append('相对表现基于已核验股票样本；缺失证券可能改变行业及整体排名，不代表全市场最优。')
    watch = [f"观察{row['sector_name']}相对市场表现是否延续，以及该股是否继续强于行业和市场",
        "核验报告日非ST、非停牌、上市及证券身份状态"]
    invalidation = ["该股不再强于所属行业或市场，单日相对强势依据失效",
        "出现已核实ST、停牌、上市或证券身份不符，或公司不利事实"]
    metrics = row.get("metrics") or {}
    historical = row.get("technical_status") in {"pass", "fail"}
    relative20 = _number(metrics.get("relative_return_20")) if historical else None
    if relative20 is not None:
        reasons.append(f"已核验20日相对上证收益{_text(relative20 * 100)}个百分点，单列历史表现供复核")
    else:
        risks.append("20日相对表现未完整核验，不能据单日强弱判断历史趋势")
    if row.get("technical_status") != "pass":
        risks.append("历史量价条件未通过" if row.get("technical_status") == "fail" else "历史量价数据尚未完整核验")
        unmet = [condition for condition in row.get("technical_conditions", []) if condition.get("status") != "pass"]
        risks.extend(_details(unmet))
        watch.extend("继续核验并等待满足：" + text for text in _details(unmet))
    else:
        close, short, long = (_number(metrics.get(key)) for key in ("adjusted_close", "ma20", "ma60"))
        if all(value is not None and value > 0 for value in (close, short, long)) and close > short > long:
            reasons.append(f"已核验历史趋势条件：前复权收盘{_text(close)}>MA20 {_text(short)}>MA60 {_text(long)}")
            invalidation.append("前复权收盘>MA20>MA60或20日相对上证表现条件不再满足")
        watch.append("持续复核历史量价条件及20日相对上证表现")
    if row.get("eligibility_status") != "pass":
        risks.append("基本资格尚未完整核验，属于待核观察")
        risks.extend(_details(row.get("eligibility_gaps", row.get("qualification_gaps", []))))
    risks.extend(_details(row.get("risk_gaps", [])))
    if row.get("ranking_metrics_status") != "available":
        risks.append("历史排序所需的20日相对表现或平均成交额未完整核验，仅保留已有单日观察事实")
    if row["technical_score"] is None:
        risks.append("技术评分不可用，未以默认分数代替")
        watch.append("补齐固定历史窗口后重新核验技术评分和趋势条件")
    else:
        reasons.append(f"技术评分{row['technical_score']}，仅用于研究优先级")
    return reasons, watch, invalidation, risks


def finalize_observations(preselection, *, company_reviews, stance, events):
    watch, excluded = [], []
    for raw in preselection["candidates"]:
        row = deepcopy(raw)
        review = deepcopy(company_reviews.get(row["security_id"], {}))
        related = [deepcopy(event) for event in events if event.get("validation_status") == "verified"
            and row["security_id"] in event.get("security_ids", [])]
        row.update(company_review=review, events=related)
        if review.get("risk_status") == "adverse" or any(event.get("risk_status") == "adverse" for event in related):
            row.update(research_status="excluded", reason="company_adverse_evidence")
            excluded.append(row)
            continue
        reasons, conditions, invalidation, risks = _observation_text(row)
        ready = review.get("status") == "ready" and review.get("risk_status") == "clear" and bool(review.get("evidence_ids"))
        if ready:
            reasons.append("已核查本期列示公司资料，所查范围未发现已确认不利事实；不代表完整无风险")
        else:
            risks.append("公司公告、财务或反面证据核查尚未完成，未查到不能解释为没有风险")
            conditions.append("补齐公司公告、财务与反面证据核查，确认不存在否决事实")
            row["observation_status"] = "conditional"
        risks.extend(_details(review.get("financial_risk_flags", [])))
        risks.extend(_details(review.get("financial_gaps", [])))
        risks.extend(_details(review.get("gaps", [])))
        row.update(research_status="watch", reason="conditional_observation" if row["observation_status"] == "conditional" else "qualified_observation",
            reasons=reasons, watch_conditions=list(dict.fromkeys(conditions)), invalidation_conditions=invalidation,
            observation_risks=list(dict.fromkeys(risks)))
        watch.append(row)
    return {"observation_only": True, "watch_candidates": watch, "focus_candidates": [], "pending_candidates": [],
        "excluded": excluded, "preselection": deepcopy(preselection),
        **({"new_attack_supported":stance.get('stance')=='attack'} if 'stance' in stance else {}),
        "notice": "市场状态与观察名单分别判断；名单仅供条件观察，不提供账户交易指令"}
