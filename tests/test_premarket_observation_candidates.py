from copy import deepcopy

import pytest

from ashare_daily.premarket.candidates import finalize_candidates, preselect_candidates
from ashare_daily.sector_selection import digest


def seal(selection):
    body = {key: value for key, value in selection.items() if key not in {"selection_id", "content_hash"}}
    selection["content_hash"] = digest(body)
    selection["selection_id"] = "sector-" + selection["target_date"] + "-" + selection["content_hash"][:20]
    return selection


def inputs(counts=(3, 3, 3)):
    members, evaluations, scores, sectors = [], [], [], []
    for index, count in enumerate(counts):
        sector = chr(ord("a") + index)
        sectors.append({"sector_id": sector, "name": "行业" + sector, "selected_rank": index + 1,
            "direction_kind": "relative_strength", "relative_metrics": {
                "mean_change_pct": -1, "median_change_pct": -1, "advancing_fraction": 0,
                "market_mean_change_pct": -2, "market_advancing_fraction": 0,
                "excess_market_pp": 1, "absolute_up": False, "member_count": 5}})
        for offset in range(count):
            identity = f"{sector}{offset}"
            code = str(600000 + len(members))
            relative = {"sector_id": sector, "sector_mean_change_pct": -1,
                "market_mean_change_pct": -2, "stock_change_pct": -0.5,
                "excess_market_pp": 1.5, "excess_sector_pp": 0.5,
                "amount_cny": 70000000, "market_as_of_date": "2026-10-15",
                "kind": "single_session_relative_strength"}
            members.append({"security_id": identity, "exchange": "SSE", "code": code,
                "name": "公司" + identity, "sector_ids": [sector], "relative_observation": relative})
            evaluations.append({"security_id": identity, "symbol": "sh." + code, "name": "公司" + identity,
                "technical_status": "pass", "eligibility_status": "pass", "technical_conditions": [],
                "eligibility_conditions": [], "metrics": {
                    "relative_return_20": "0.03", "avg_amount_20_cny": "80000000",
                    "adjusted_close": "12", "ma20": "11", "ma60": "10"}})
            scores.append({"security_id": identity, "symbol": "sh." + code, "name": "公司" + identity,
                "status": "available", "score": 90 - len(scores), "components": []})
    selection = seal({"rule_version": "premarket-relative-observation-v1", "observation_only": True,
        "basis_selection_id": "strict-selection", "selection_verified": True,
        "target_date": "2026-10-15", "market_scope": "sse_szse_a", "research_mode": "sector_first",
        "selected_security_count": len(members), "members": members, "selected_sectors": sectors})
    return selection, evaluations, {"selection_id": selection["selection_id"],
        "trade_date": "2026-10-15", "records": scores}


def rebind(values):
    seal(values[0])
    values[2]["selection_id"] = values[0]["selection_id"]


def reviews(preselection):
    return {row["security_id"]: {"status": "ready", "risk_status": "clear", "evidence_ids": ["e1"]}
            for row in preselection["candidates"]}


@pytest.mark.parametrize("stance", ["defend", "hold", "undetermined", "attack"])
def test_market_stance_does_not_remove_valid_relative_observations(stance):
    selected = preselect_candidates(*inputs())
    result = finalize_candidates(selected, company_reviews=reviews(selected), stance={"stance": stance}, events=[])
    assert len(result.get("watch_candidates", [])) == 5
    assert result["focus_candidates"] == result["pending_candidates"] == []
    assert result["new_attack_supported"] is (stance == "attack")
    assert result["observation_only"] is True


def test_each_direction_has_a_turn_then_remaining_slots_use_verified_priority():
    values = inputs()
    before = deepcopy(values)
    result = preselect_candidates(*values)
    assert [row["security_id"] for row in result["candidates"]] == ["a0", "b0", "c0", "a1", "a2"]
    assert result["selection_id"] == values[0]["selection_id"]
    assert result["market_as_of_date"] == "2026-10-15"
    assert result["audit"] == values[1]
    assert result["observation_only"] is True
    assert values == before


def test_single_direction_can_fill_five_company_review_slots():
    selected = preselect_candidates(*inputs((5,)))
    assert [row["security_id"] for row in selected["candidates"]] == ["a0", "a1", "a2", "a3", "a4"]


def test_unknown_eligibility_missing_score_and_failed_technical_remain_conditional():
    values = inputs((3,))
    values[1][0].update(eligibility_status="pending", eligibility_gaps=[{"field": "st", "reason": "ST名单待核"}])
    values[1][1].update(technical_status="unknown", metrics={})
    values[2]["records"][1].update(status="unavailable", score=None)
    values[1][2].update(technical_status="fail", technical_conditions=[{
        "id": "trend", "label": "前复权C(T)>MA20>MA60", "status": "fail", "reason": "收盘价未高于MA20"}])
    selected = preselect_candidates(*values)
    result = finalize_candidates(selected, company_reviews={}, stance={"stance": "defend"}, events=[])
    watched = {row["security_id"]: row for row in result.get("watch_candidates", [])}
    assert set(watched) == {"a0", "a1", "a2"}
    assert all(row["observation_status"] == "conditional" for row in watched.values())
    assert watched["a1"]["technical_score"] is None
    assert "ST名单待核" in "；".join(watched["a0"]["observation_risks"])
    assert "前复权C(T)>MA20>MA60" in "；".join(watched["a2"]["observation_risks"])
    assert all(row["watch_conditions"] and row["invalidation_conditions"] for row in watched.values())
    assert all("公司" in "；".join(row["observation_risks"]) for row in watched.values())


def test_known_ineligible_and_not_applicable_are_excluded_without_losing_other_watch():
    values = inputs((3,))
    values[1][0].update(eligibility_status="fail", exclusion_reasons=["有据ST"])
    values[1][1]["technical_status"] = "not_applicable"
    selected = preselect_candidates(*values)
    assert [row["security_id"] for row in selected["candidates"]] == ["a2"]
    assert {row["security_id"] for row in selected["excluded"]} == {"a0", "a1"}


def test_known_adverse_reviews_and_verified_events_veto_but_unknown_is_visible():
    selected = preselect_candidates(*inputs((4,)))
    company = reviews(selected)
    company["a0"]["risk_status"] = "adverse"
    company["a2"] = {"status": "partial", "risk_status": "review_required", "financial_risk_flags": ["经营现金流为负"],
        "financial_gaps": ["缺少最近一期财务资料"], "evidence_ids": []}
    events = [{"security_ids": ["a1"], "validation_status": "verified", "risk_status": "adverse"},
              {"security_ids": ["a3"], "validation_status": "unverified", "risk_status": "adverse"}]
    result = finalize_candidates(selected, company_reviews=company, stance={"stance": "hold"}, events=events)
    assert [row["security_id"] for row in result.get("watch_candidates", [])] == ["a2", "a3"]
    assert {row["security_id"] for row in result["excluded"]} == {"a0", "a1"}
    row = result["watch_candidates"][0]
    assert "经营现金流为负" in "；".join(row["observation_risks"])
    assert "缺少最近一期财务资料" in "；".join(row["observation_risks"])
    assert row["observation_status"] == "conditional"


def test_relative_resilience_is_not_described_as_an_uptrend_when_history_failed():
    values = inputs((1,))
    values[1][0].update(technical_status="fail", metrics={"relative_return_20": "-0.04"})
    selected = preselect_candidates(*values)
    result = finalize_candidates(selected, company_reviews=reviews(selected), stance={"stance": "defend"}, events=[])
    row = result.get("watch_candidates", [{}])[0]
    reasons = "；".join(row.get("reasons", []))
    assert "相对抗跌" in reasons and "-0.50%" in reasons and "1.50" in reasons and "0.50" in reasons
    assert "2026-10-15" in reasons
    assert "20日" in reasons and "-4" in reasons
    assert "上涨趋势" not in reasons


def test_shared_sector_membership_uses_the_direction_bound_to_relative_numbers():
    values = inputs((1, 1))
    values[0]["members"][0]["sector_ids"] = ["a", "b"]
    rebind(values)
    selected = preselect_candidates(*values)
    assert len({row["security_id"] for row in selected["candidates"]}) == 2
    assert all(row["primary_sector_id"] == row["relative_observation"]["sector_id"] for row in selected["candidates"])


@pytest.mark.parametrize("invalid_score", [None, True, -1, 101, "99"])
def test_unusable_score_is_missing_rather_than_a_numeric_priority(invalid_score):
    values = inputs((2,))
    values[2]["records"][0]["score"] = invalid_score
    selected = preselect_candidates(*values)
    assert [row["security_id"] for row in selected["candidates"]] == ["a1", "a0"]
    assert selected["candidates"][1]["technical_score"] is None


@pytest.mark.parametrize("conflict", ["date", "identity", "score_identity", "member_date", "sector", "hash"])
def test_conflicting_bindings_are_rejected(conflict):
    values = inputs((1,))
    if conflict == "date":
        values[2]["trade_date"] = "2026-10-14"
    elif conflict == "identity":
        values[1][0]["symbol"] = "sz.000001"
    elif conflict == "score_identity":
        values[2]["records"].append({"security_id": "extra", "status": "available", "score": 100})
    elif conflict == "member_date":
        values[0]["members"][0]["relative_observation"]["market_as_of_date"] = "2026-10-14"
        rebind(values)
    elif conflict == "sector":
        values[0]["members"][0]["relative_observation"]["sector_id"] = "other"
        rebind(values)
    elif conflict == "hash":
        values[0]["members"][0]["relative_observation"]["stock_change_pct"] = 6
    with pytest.raises(ValueError, match="binding|hash"):
        preselect_candidates(*values)


def test_empty_observation_pool_is_explicit_and_has_no_fabricated_candidates():
    selected = preselect_candidates(*inputs(()))
    result = finalize_candidates(selected, company_reviews={}, stance={"stance": "defend"}, events=[])
    assert result.get("watch_candidates") == []
    assert result["observation_only"] is True


@pytest.mark.parametrize("metrics", [{}, {"relative_return_20": "0.03", "avg_amount_20_cny": "-1"}])
def test_incomplete_historical_ranking_facts_cannot_claim_verified_observation(metrics):
    values = inputs((1,))
    values[1][0]["metrics"] = metrics
    selected = preselect_candidates(*values)
    result = finalize_candidates(selected, company_reviews=reviews(selected), stance={"stance": "hold"}, events=[])
    row = result["watch_candidates"][0]
    assert row["observation_status"] == "conditional"
    assert any("历史排序" in risk for risk in row["observation_risks"])
