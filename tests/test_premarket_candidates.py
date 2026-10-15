from copy import deepcopy

import pytest


def inputs():
    members, rows, scores = [], [], []
    for i in range(25):
        sector = "a" if i < 23 else "b"
        members.append({"security_id": f"id{i:02}", "sector_ids": [sector]})
        rows.append({"security_id": f"id{i:02}", "symbol": f"sh.{600000+i}", "name": f"公司{i}",
                     "technical_status": "pass", "eligibility_status": "pass",
                     "metrics": {"relative_return_20": str(1-i/100), "avg_amount_20_cny": "50000000"}})
        scores.append({"security_id": f"id{i:02}", "status": "available", "score": 50+i, "components": []})
    selection = {"selection_id": "selection", "target_date": "2026-10-09", "members": members,
        "selected_sectors": [{"sector_id": "a", "name": "行业甲", "selected_rank": 1},
                             {"sector_id": "b", "name": "行业乙", "selected_rank": 2}]}
    reference = {"selection_id": "selection", "trade_date": "2026-10-09", "records": scores}
    return selection, rows, reference


def preselect(values=None):
    from ashare_daily.premarket.candidates import preselect_candidates
    return preselect_candidates(*(values or inputs()))


def test_all_qualified_rows_participate_and_sectors_round_robin():
    result = preselect()
    assert [r["security_id"] for r in result["candidates"]] == ["id22", "id24", "id21", "id23"]
    assert result["qualified_count"] == 25
    assert result["score_used_for_priority"] is True
    assert result["baseline_ids"] == ["id00", "id01", "id02", "id03", "id04"]
    assert len(result["audit"]) == 25


def test_missing_scores_and_failed_qualification_never_promoted():
    s, rows, ref = inputs()
    ref["records"][22].update(status="unavailable", score=None)
    rows[24]["eligibility_status"] = "pending"
    result = preselect((s, rows, ref))
    assert "id22" not in [r["security_id"] for r in result["candidates"]]
    assert "id24" not in [r["security_id"] for r in result["candidates"]]
    assert any(r["security_id"] == "id22" for r in result["pending"])


def test_duplicate_membership_does_not_duplicate_stock_and_inputs_unchanged():
    values = inputs()
    values[0]["members"][22]["sector_ids"].append("b")
    before = deepcopy(values)
    ids = [r["security_id"] for r in preselect(values)["candidates"]]
    assert len(ids) == len(set(ids))
    assert values == before


def test_reference_date_mismatch_rejected():
    values = inputs()
    values[2]["trade_date"] = "2026-10-08"
    with pytest.raises(ValueError, match="binding"):
        preselect(values)


def test_unknown_company_review_is_pending_and_defense_does_not_recommend_attack():
    from ashare_daily.premarket.candidates import finalize_candidates
    selected = preselect()
    reviews = {"id22": {"status": "ready", "risk_status": "clear", "evidence_ids": ["e1"]},
               "id24": {"status": "ready", "risk_status": "adverse", "evidence_ids": ["e2"]}}
    result = finalize_candidates(selected, company_reviews=reviews, stance={"stance": "attack"}, events=[])
    assert [r["security_id"] for r in result["focus_candidates"]] == ["id22"]
    assert {r["security_id"] for r in result["pending_candidates"]} == {"id21", "id23"}
    assert result["excluded"][0]["security_id"] == "id24"
    assert finalize_candidates(selected, company_reviews=reviews, stance={"stance": "defend"}, events=[])["focus_candidates"] == []
