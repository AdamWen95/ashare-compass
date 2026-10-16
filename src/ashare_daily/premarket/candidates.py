"""Full-pool technical priorities with bounded company review and risk gates."""
from __future__ import annotations

from copy import deepcopy

from .contracts import RECOMMENDATION_VERSION, decimal


def preselect_candidates(selection: dict, evaluations: list[dict], reference: dict) -> dict:
    if selection.get("observation_only") is True:
        from .observation_candidates import preselect_observations
        return preselect_observations(selection, evaluations, reference)
    if (reference.get("selection_id") != selection.get("selection_id")
            or reference.get("trade_date") != selection.get("target_date")):
        raise ValueError("premarket_score_binding_mismatch")
    def unique(values):
        ids = [r["security_id"] for r in values]
        if len(ids) != len(set(ids)):
            raise ValueError("premarket_duplicate_identity")
        return {r["security_id"]: r for r in values}
    members, scores = unique(selection["members"]), unique(reference.get("records", []))
    rows = unique(evaluations)
    if set(rows) != set(members):
        raise ValueError("premarket_candidate_binding_mismatch")
    sectors = sorted(selection.get("selected_sectors", []), key=lambda s: (s["selected_rank"], s["sector_id"]))[:3]
    allowed = {s["sector_id"] for s in sectors}
    pools = {s["sector_id"]: [] for s in sectors}
    pending, excluded, qualified, baseline = [], [], 0, []
    for identity, raw in rows.items():
        if raw.get("technical_status") != "pass" or raw.get("eligibility_status") != "pass":
            excluded.append({"security_id": identity, "reason": "qualification_not_passed"})
            continue
        qualified += 1
        try:
            baseline.append(((-decimal(raw["metrics"]["relative_return_20"]),
                -decimal(raw["metrics"]["avg_amount_20_cny"], nonnegative=True), raw["symbol"], identity), identity))
        except (ValueError, KeyError, TypeError):
            pass  # Ranking gaps are retained below; no substitute metric is invented.
        row, score = deepcopy(raw), scores.get(identity, {})
        value = score.get("score")
        if score.get("status") != "available" or type(value) is not int or not 0 <= value <= 100:
            row.update(research_status="pending", reason="technical_score_unavailable")
            pending.append(row)
            continue
        try:
            strength = decimal(row["metrics"]["relative_return_20"])
            amount = decimal(row["metrics"]["avg_amount_20_cny"], nonnegative=True)
        except (ValueError, KeyError, TypeError):
            row.update(research_status="pending", reason="ranking_metrics_unavailable")
            pending.append(row)
            continue
        row.update(technical_score=value, score_components=deepcopy(score.get("components", [])),
                   sector_ids=[s for s in members[identity].get("sector_ids", []) if s in allowed],
                   research_status="preselected", current_trade_status="unverified",
                   trade_status_notice="本日交易状态待核验")
        for sector in row["sector_ids"]:
            pools[sector].append(((-value, -strength, -amount, identity), row))
    for values in pools.values():
        values.sort(key=lambda pair: pair[0])
    chosen, seen = [], set()
    for _ in range(2):
        for sector in sectors:
            next_row = next((row for _, row in pools[sector["sector_id"]] if row["security_id"] not in seen), None)
            if next_row is None or len(chosen) >= 5:
                continue
            row = deepcopy(next_row)
            row.update(primary_sector_id=sector["sector_id"], sector_name=sector.get("name", sector["sector_id"]))
            chosen.append(row)
            seen.add(row["security_id"])
    return {"rule_version": RECOMMENDATION_VERSION, "score_used_for_priority": True,
            "selection_id": selection["selection_id"], "market_as_of_date": selection["target_date"],
            "candidates": chosen, "pending": pending, "excluded": excluded,
            "qualified_count": qualified, "directions": deepcopy(sectors),
            "baseline_ids": [identity for _, identity in sorted(baseline)[:5]], "audit": deepcopy(evaluations),
            "notice": "技术评分影响研究优先级，不是上涨概率；公司核查限本期预选最多5只"}


def finalize_candidates(preselection: dict, *, company_reviews: dict, stance: dict, events: list[dict]) -> dict:
    if preselection.get("observation_only") is True:
        from .observation_candidates import finalize_observations
        return finalize_observations(preselection, company_reviews=company_reviews, stance=stance, events=events)
    focus, pending, excluded = [], deepcopy(preselection["pending"]), []
    for raw in preselection["candidates"]:
        row = deepcopy(raw)
        review = company_reviews.get(row["security_id"], {})
        related = [e for e in events if e.get("validation_status") == "verified"
                   and row["security_id"] in e.get("security_ids", [])]
        row.update(company_review=deepcopy(review), events=deepcopy(related),
            reasons=[f"所属{row['sector_name']}进入量价观察方向", f"技术评分 {row['technical_score']}，构成可复核"],
            watch_conditions=["继续核验趋势、相对表现与本日交易状态"],
            invalidation_conditions=["趋势条件失效、资格变化或出现削弱研究依据的新证据"])
        adverse = review.get("risk_status") == "adverse" or any(e.get("risk_status") == "adverse" for e in related)
        ready = review.get("status") == "ready" and review.get("risk_status") == "clear" and bool(review.get("evidence_ids"))
        if adverse:
            row.update(research_status="excluded", reason="company_adverse_evidence")
            excluded.append(row)
        elif not ready or stance.get("stance") != "attack":
            row.update(research_status="pending", reason="company_review_incomplete" if not ready else "market_not_attack")
            pending.append(row)
        else:
            row.update(research_status="focus", reason="qualified_and_reviewed")
            focus.append(row)
    return {"focus_candidates": focus, "pending_candidates": pending, "excluded": excluded,
            "preselection": deepcopy(preselection), "new_attack_supported": stance.get("stance") == "attack",
            "notice": "本期不建议据此新增进攻" if stance.get("stance") != "attack" else "关注条件与风险，不提供账户交易指令"}
