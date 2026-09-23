from copy import deepcopy
from datetime import datetime

import pytest

from ashare_daily.sector_selection import evaluate, verify_selection, parameters, quote_issues

T = "2026-09-11"


def inputs(count=105, industries=1):
    boards = [("SSE", "sse_main"), ("SZSE", "szse_main"), ("SZSE", "chinext"), ("SSE", "star")]
    members, quotes = [], []
    for i in range(count):
        exchange, board = boards[i % 4]
        code = str(600000 + i)
        symbol = {"SSE": "sh", "SZSE": "sz"}[exchange] + code
        members.append(dict(security_id=f"offline-{i}", code=code, exchange=exchange,
            board=board, security_type="ordinary_a", metadata_verified=True,
            statuses={"st": {"value": None}, "suspended": {"value": None}}, name=f"测试{i}"))
        quotes.append(dict(symbol=symbol, trade_date=T, quote_at=T+"T15:00:01+08:00",
            change_pct=2, amount_cny=60000000, close=10.2, reference_price=10,
            percentage_basis="source_reference_price", amount_unit="CNY", price_unit="CNY/share"))
    universe = dict(scope="sse_szse_a", mode="offline_test", universe_verified=True, collection_ready=True,
        members=members, snapshot_id="offline-universe", content_hash="offline", ordinary_a_count=count,
        source_manifests=[{"provider": "test", "source_business_date": None}])
    catalog = dict(status="ok", complete=True, boundary_verified=True, rows=[dict(
        sector_id=f"industry-{i}", taxonomy="sina_industry", kind="industry", name=f"行业{i}") for i in range(industries)])
    memberships = {row["sector_id"]: dict(complete=True, boundary_verified=True,
        rows=[{"symbol": q["symbol"]} for q in quotes]) for row in catalog["rows"]}
    return universe, catalog, memberships, {"rows": quotes}


def calculate(data, **kwargs):
    return evaluate(*data, target=T, cutoff=T+"T21:00:00+08:00", config={"taxonomy": "sina_industry"},
        calendar={"calendar_verified": True, "calendar": {T: True}}, mode="offline_test", **kwargs)


def test_dynamic_union_over_100_and_overlap_has_one_identity():
    value, rows = calculate(inputs(137, 8))
    assert value["preselected_count"] == 6
    assert value["selected_count"] == 3
    assert len(value["members"]) == 137
    assert all(len(m["sector_ids"]) == 3 for m in value["members"])
    assert len(rows) == 137 * 8
    assert {m["board"] for m in value["members"]} == {"sse_main", "szse_main", "chinext", "star"}
    assert verify_selection(value) == value


def test_unknown_risk_preserved_source_business_date_null_and_not_halt():
    value, _ = calculate(inputs())
    assert all(m["statuses"]["st"]["value"] is None for m in value["members"])
    assert value["sectors"][0]["expected_quote_count"] == 105
    assert value["sectors"][0]["full_day_halted"] == []
    assert value["universe_source_business_dates"][0]["source_business_date"] is None
    assert value["research_thresholds"] == {"valid_history_days": 120, "mean_amount_20_cny": 50000000}


@pytest.mark.parametrize("field,value", [("trade_date", None), ("trade_date", "2026-09-10"),
    ("quote_at", "15:00:01"), ("quote_at", T+"T14:59:59+08:00"),
    ("quote_at", T+"T15:00:01"), ("amount_unit", "万元"), ("change_pct", float("nan"))])
def test_date_units_and_numeric_failures_do_not_become_zero_opportunity(field, value):
    data = inputs(1)
    data[3]["rows"][0][field] = value
    if isinstance(value, float):
        assert "quote_numeric_fields_missing" in quote_issues(data[3]["rows"][0], T)
        return
    result, _ = calculate(data)
    assert result["selection_status"] == "selection_blocked"
    assert not result["selection_verified"]
    assert result["sectors"][0]["expected_quote_count"] == 1


def test_real_zero_is_distinct_from_failed_source():
    data = inputs(1)
    data[3]["rows"][0].update(close=10, change_pct=0)
    result, _ = calculate(data)
    assert result["selection_status"] == "no_matching_sectors"
    assert result["selection_verified"] and result["members"] == []
    data[1]["status"] = "network_error"
    failed, _ = calculate(data)
    assert failed["selection_status"] == "selection_blocked"
    assert not failed["selection_verified"]


@pytest.mark.parametrize("change", ["truncate", "duplicate", "unknown", "conflict"])
def test_membership_gaps_are_never_silently_dropped(change):
    data = inputs(2)
    membership = data[2]["industry-0"]
    if change == "truncate":
        membership["boundary_verified"] = False
    elif change == "duplicate":
        membership["rows"].append(deepcopy(membership["rows"][0]))
    elif change == "unknown":
        membership["rows"].append({"symbol": "bj920001"})
    else:
        membership["rows"][0]["security_id"] = "conflicting-id"
    result, rows = calculate(data)
    assert not result["selection_verified"]
    assert result["members"] == []
    assert len(rows) == len(membership["rows"])


@pytest.mark.parametrize("kind,board,exchange", [("cdr", "star", "SSE"), ("b_share", "sse_main", "SSE"),
    ("ordinary_a", "bse", "BSE")])
def test_verified_non_scope_members_preserved_and_excluded(kind, board, exchange):
    data = inputs(2)
    member = data[0]["members"][1]
    old_symbol = data[2]["industry-0"]["rows"][1]["symbol"]
    member.update(security_type=kind, board=board, exchange=exchange)
    symbol = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}[exchange] + member["code"]
    data[2]["industry-0"]["rows"][1]["symbol"] = symbol
    result, rows = calculate(data)
    assert result["selection_verified"] and len(result["members"]) == 1
    assert rows[1]["mapping_status"] == "excluded"


def test_different_taxonomy_and_theme_are_not_merged_into_industry():
    data = inputs(1, 2)
    data[1]["rows"][1].update(taxonomy="other_source", name="行业0", kind="theme")
    result, _ = calculate(data)
    assert result["selected_count"] == 1
    assert result["members"][0]["sector_ids"] == ["industry-0"]
    assert result["industry_comparison_complete"]
    assert result["catalog_count"] == 1 and result["raw_catalog_count"] == 2


def test_count_discrepancy_does_not_cut_complete_boundary():
    data = inputs(131)
    data[2]["industry-0"].update(displayed_count=100, count_discrepancy={"displayed": 100, "actual": 131})
    result, rows = calculate(data)
    assert result["selected_security_count"] == 131
    assert len(rows) == 131
    assert result["sectors"][0]["source_count_discrepancy"]["actual"] == 131


def test_freeze_deterministic_and_member_or_config_change_creates_new_version():
    data = inputs(105, 4)
    result, _ = calculate(data)
    assert calculate(data)[0] == result
    changed = deepcopy(result)
    changed["members"].pop()
    with pytest.raises(ValueError, match="hash"):
        verify_selection(changed)
    data[1]["rows"][0]["name"] = "源更正名称"
    assert calculate(data)[0]["selection_id"] != result["selection_id"]


def test_config_gates_cannot_be_reduced():
    for override in ({"preselect_limit": 7}, {"selected_limit": 4}, {"advancing_fraction_min": 0.49}):
        with pytest.raises(ValueError):
            parameters(override)


def test_production_rejects_offline_data():
    data = inputs()
    result, _ = evaluate(*data, target=T, cutoff=T+"T21:00:00+08:00", config={"taxonomy": "sina_industry"},
        calendar={"calendar_verified": True, "calendar": {T: True}}, mode="research")
    assert not result["selection_verified"]
    assert "test_universe_rejected" in result["blockers"]
    assert "source_provenance_unverified" in result["blockers"]


def test_quote_after_cutoff_cannot_be_selected():
    data = inputs(1)
    data[3]["rows"][0]["quote_at"] = T+"T21:30:00+08:00"
    result, _ = calculate(data)
    assert result["selection_verified"] is False
    assert result["sectors"][0]["missing_quotes"][0]["issues"] == ["quote_after_selection_cutoff"]


def test_verified_full_day_halt_is_separate_evidence_not_unknown_to_normal():
    data = inputs(3)
    quote = data[3]["rows"][0]
    quote["close"] = 0
    failed, _ = calculate(data)
    assert failed["selection_verified"] is False
    quote["full_day_halt_evidence"] = {"value": True, "verified": True, "full_day": True,
        "source": "baostock", "evidence_id": "fixture-only", "as_of_date": T,
        "effective_from": T, "effective_to": T}
    result, _ = calculate(data)
    assert result["selection_verified"]
    assert result["sectors"][0]["expected_quote_count"] == 2
    assert result["sectors"][0]["full_day_halted"] == ["offline-0"]
    assert result["members"][0]["statuses"]["suspended"]["value"] is None
    assert result["members"][0]["supplemental_status_evidence"]["suspended"] == quote["full_day_halt_evidence"]
    assert "supplemental_status_evidence" not in data[0]["members"][0]
    quote["full_day_halt_evidence"]["as_of_date"] = "2026-09-10"
    assert calculate(data)[0]["selection_verified"] is False
