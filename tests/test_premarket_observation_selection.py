"""Relative observations use complete, bound facts even when all sectors fall."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from ashare_daily.sector_selection import digest, evaluate, verify_selection


DAY = "2026-10-15"
CONFIG = {"taxonomy": "fixture_industry", "output_directory": "outputs/relative"}


def freeze_universe(universe):
    body = {k: v for k, v in universe.items() if k not in {"snapshot_id", "content_hash"}}
    universe["content_hash"] = digest(body)
    universe["snapshot_id"] = "universe-" + universe["requested_date"] + "-" + universe["content_hash"][:20]


def fixture(groups=None, *, unassigned=()):
    groups = groups or [[-1, -2, -2, -2, -3], [-5] * 5]
    members, quotes, catalog, memberships = [], [], [], {}
    for group, changes in enumerate([*groups, list(unassigned)]):
        sector_id = f"sector-{group}"
        symbols = []
        for change in changes:
            index = len(members)
            code = str(600000 + index)
            identity = {"security_id": f"id-{index:03}", "code": code, "exchange": "SSE",
                "board": "sse_main", "security_type": "ordinary_a", "metadata_verified": True}
            members.append({**identity, "name": f"公司{index}", "listing_status": "listed",
                "statuses": {"st": {"value": None}, "suspended": {"value": None}}})
            symbols.append("sh" + code)
            quotes.append({"symbol": symbols[-1], "security_id": identity["security_id"],
                "identity": identity, "trade_date": DAY, "quote_at": DAY + "T15:01:00+08:00",
                "change_pct": change, "amount_cny": 60000000,
                "close": str(Decimal(10) * (1 + Decimal(str(change)) / 100)), "reference_price": "10",
                "percentage_basis": "source_reference_price", "amount_unit": "CNY", "price_unit": "CNY/share"})
        if group < len(groups):
            catalog.append({"sector_id": sector_id, "taxonomy": CONFIG["taxonomy"],
                "kind": "industry", "name": f"行业{group}"})
            memberships[sector_id] = {"complete": True, "boundary_verified": True,
                "provenance_mode": "offline_test", "rows": [{"symbol": s} for s in symbols]}
    universe = {"scope": "sse_szse_a", "mode": "offline_test", "requested_date": DAY,
        "structural_verified": True, "universe_verified": True, "collection_ready": True,
        "ordinary_a_count": len(members), "members": members}
    freeze_universe(universe)
    return {"universe": universe,
        "catalog": {"status": "ok", "complete": True, "boundary_verified": True,
                    "provenance_mode": "offline_test", "rows": catalog},
        "memberships": memberships,
        "quotes": {"provenance_mode": "offline_test", "complete": True, "rows": quotes},
        "calendar": {"calendar_verified": True, "calendar": {DAY: True}}}


def strict(packet):
    freeze_universe(packet["universe"])
    return evaluate(packet["universe"], packet["catalog"], packet["memberships"], packet["quotes"],
        target=DAY, cutoff=DAY + "T21:00:00+08:00", config=CONFIG,
        calendar=packet["calendar"], mode="offline_test")


def select(packet, selection=None):
    from ashare_daily.premarket.selection import build_observation_selection
    return build_observation_selection(selection or strict(packet)[0], packet)


def test_falling_market_keeps_relative_direction_without_claiming_uptrend():
    packet = fixture()
    original, _ = strict(packet)
    before = deepcopy((original, packet))
    result = select(packet, original)
    assert original["selected_count"] == 0
    assert result["selection_status"] == "selected"
    assert result["rule_version"] == "premarket-relative-observation-v1"
    assert result["observation_only"] is True
    assert result["basis_selection_id"] == original["selection_id"]
    assert result["selection_id"] != original["selection_id"]
    assert result["selected_sectors"][0]["relative_metrics"] == {
        "mean_change_pct": -2.0, "median_change_pct": -2.0, "advancing_fraction": 0.0,
        "market_mean_change_pct": -3.5, "market_advancing_fraction": 0.0,
        "excess_market_pp": 1.5, "absolute_up": False, "member_count": 5}
    assert result["selected_sectors"][0]["direction_kind"] == "relative_strength"
    assert [m["security_id"] for m in result["members"]] == ["id-000"]
    assert result["members"][0]["relative_observation"] == {
        "sector_id": "sector-0", "sector_mean_change_pct": -2.0,
        "market_mean_change_pct": -3.5, "stock_change_pct": -1.0,
        "excess_market_pp": 2.5, "excess_sector_pp": 1.0, "amount_cny": 60000000.0,
        "market_as_of_date": DAY, "kind": "single_session_relative_strength"}
    assert result["members"][0]["statuses"]["st"]["value"] is None
    assert verify_selection(result) == result
    assert select(packet, original) == result
    assert (original, packet) == before


def test_equal_weak_sectors_do_not_force_directions_or_stocks():
    result = select(fixture([[-2] * 5, [-2] * 5]))
    assert result["selection_verified"] is True
    assert result["selection_status"] == "no_matching_sectors"
    assert result["selected_sectors"] == result["members"] == []


def test_market_baseline_includes_unassigned_verified_ordinary_shares():
    result = select(fixture([[-2, -3, -3, -3, -4]], unassigned=[-9] * 5))
    assert result["selected_sectors"][0]["relative_metrics"]["market_mean_change_pct"] == -6.0
    assert result["relative_market_baseline"]["sample_count"] == 10


def test_sample_breadth_and_amount_gates_are_independent():
    groups = [[2, 2, 2, 2], [-1, -1, -1, -1, -1], [1, -1, -1, -1, -1], [-8] * 5]
    packet = fixture(groups)
    for quote in packet["quotes"]["rows"][4:9]:
        quote["amount_cny"] = 0
    result = select(packet)
    # Small sector fails sample count; all-negative zero-amount sector fails both
    # breadth and amount; third sector's 1/5 breadth is below market 5/19.
    assert result["selected_sectors"] == []


def test_history_pool_has_three_sectors_and_at_most_five_per_sector():
    packet = fixture([[4] * 7 + [-8], [3] * 7 + [-8], [2] * 7 + [-8], [-10] * 24])
    result = select(packet)
    assert [s["sector_id"] for s in result["selected_sectors"]] == ["sector-0", "sector-1", "sector-2"]
    assert len(result["members"]) == 15
    assert [m["security_id"] for m in result["members"]] == [
        "id-000", "id-001", "id-002", "id-003", "id-004", "id-008", "id-009", "id-010",
        "id-011", "id-012", "id-016", "id-017", "id-018", "id-019", "id-020"]
    assert result["observation_scope"]["max_history_securities"] == 15
    assert result["observation_scope"]["outside_pool_history"] == "not_requested_by_design"


def test_duplicate_membership_yields_one_stock_with_primary_relative_metrics():
    packet = fixture()
    packet["catalog"]["rows"].append({**packet["catalog"]["rows"][0], "sector_id": "sector-2"})
    packet["memberships"]["sector-2"] = deepcopy(packet["memberships"]["sector-0"])
    result = select(packet)
    assert len(result["members"]) == 1
    assert result["members"][0]["sector_ids"] == ["sector-0", "sector-2"]
    assert result["members"][0]["relative_observation"]["sector_id"] == "sector-0"


@pytest.mark.parametrize("field", ["st", "suspended"])
def test_known_adverse_status_excludes_stock_but_unknown_does_not_become_pass(field):
    packet = fixture([[-1, -1, -2, -3, -3], [-5] * 5])
    packet["universe"]["members"][0]["statuses"][field] = {"value": True}
    result = select(packet)
    assert [m["security_id"] for m in result["members"]] == ["id-001"]
    assert result["members"][0]["statuses"][field]["value"] is None


@pytest.mark.parametrize("amount,expected", [(49999999, []), (50000000, ["id-000"])])
def test_stock_liquidity_floor_is_inclusive(amount, expected):
    packet = fixture()
    packet["quotes"]["rows"][0]["amount_cny"] = amount
    assert [m["security_id"] for m in select(packet)["members"]] == expected


def test_confirmed_full_day_halt_excluded_from_market_and_sector_denominators():
    packet = fixture([[-1, -2, -2, -2, -3, -50], [-5] * 5])
    row = packet["quotes"]["rows"][5]
    row["full_day_halt_evidence"] = {"value": True, "verified": True, "full_day": True,
        "as_of_date": DAY, "source": "fixture", "evidence_id": "halt-fixture"}
    row["close"] = 0
    result = select(packet)
    assert result["relative_market_baseline"]["sample_count"] == 10
    assert result["relative_market_baseline"]["halted_count"] == 1
    assert result["selected_sectors"][0]["relative_metrics"]["member_count"] == 5


@pytest.mark.parametrize("field,value", [("trade_date", "2026-10-14"), ("quote_at", DAY + "T22:00:00+08:00"),
    ("close", None), ("amount_unit", "万元")])
def test_missing_or_wrong_date_quotes_block_even_outside_classified_sectors(field, value):
    packet = fixture(unassigned=[-8])
    packet["quotes"]["rows"][-1][field] = value
    result = select(packet)
    assert result["selection_verified"] is False
    assert result["selection_status"] == "selection_blocked"
    assert result["members"] == []
    assert "relative_market_quotes_incomplete" in result["blockers"]


def test_partial_sector_comparison_cannot_be_bypassed_by_strict_selected_sector():
    packet = fixture([[2, 2, 3, 3, 4], [-5] * 5])
    packet["memberships"]["sector-1"]["boundary_verified"] = False
    original, _ = strict(packet)
    assert original["selection_verified"] is True
    assert original["industry_comparison_complete"] is False
    result = select(packet, original)
    assert not result["selection_verified"] and result["members"] == []
    assert "relative_industry_comparison_incomplete" in result["blockers"]


@pytest.mark.parametrize("key", ["catalog", "memberships", "quotes", "calendar", "universe"])
def test_packet_hash_binding_rejects_mutation(key):
    packet = fixture()
    original, _ = strict(packet)
    packet[key]["changed_after_freeze"] = True
    with pytest.raises(ValueError, match="binding|hash"):
        select(packet, original)


@pytest.mark.parametrize("kind", ["quote_id", "quote_identity", "duplicate_master", "bad_board", "missing_provenance"])
def test_bound_but_invalid_identity_or_provenance_is_rejected(kind):
    packet = fixture()
    if kind == "quote_id":
        packet["quotes"]["rows"][0]["security_id"] = "wrong"
    elif kind == "quote_identity":
        packet["quotes"]["rows"][0]["identity"]["code"] = "999999"
    elif kind == "duplicate_master":
        packet["universe"]["members"][1]["security_id"] = "id-000"
    elif kind == "bad_board":
        packet["universe"]["members"][0]["board"] = "chinext"
    else:
        packet["quotes"].pop("provenance_mode")
    with pytest.raises(ValueError, match="identity|provenance"):
        select(packet)


def test_rehashed_forged_comparison_cannot_override_original_packet():
    packet = fixture()
    original, _ = strict(packet)
    original["sectors"][0]["member_security_ids"].pop()
    original.pop("content_hash")
    original.pop("selection_id")
    original["content_hash"] = digest(original)
    original["selection_id"] = "sector-" + DAY + "-" + original["content_hash"][:20]
    with pytest.raises(ValueError, match="binding"):
        select(packet, original)


def test_derived_selection_archives_with_original_packet_and_hash(tmp_path):
    from ashare_daily.sector_pipeline import archive_selection
    packet = fixture()
    original, memberships = strict(packet)
    before = deepcopy(packet)
    original_dir = archive_selection(tmp_path, CONFIG, original, memberships, packet)
    original_bytes = (original_dir / "sector_selection.json").read_bytes()
    result = select(packet, original)
    directory = archive_selection(tmp_path, CONFIG, result, memberships, packet)
    restored = json.loads((directory / "sector_selection.json").read_text(encoding="utf-8"))
    assert verify_selection(restored) == result
    assert restored["config_hash"] == digest(CONFIG)
    assert restored["evidence_hashes"] == original["evidence_hashes"]
    assert (original_dir / "sector_selection.json").read_bytes() == original_bytes
    assert json.loads((directory / "source_inputs.json").read_text(encoding="utf-8")) == before


def test_duplicate_quote_identity_is_rejected_even_for_confirmed_halt():
    packet = fixture()
    packet["universe"]["members"][0]["statuses"]["suspended"] = {
        "value": True, "verified": True, "full_day": True, "as_of_date": DAY,
        "source": "fixture", "evidence_id": "halt-fixture"}
    packet["quotes"]["rows"].append(deepcopy(packet["quotes"]["rows"][0]))
    with pytest.raises(ValueError, match="identity"):
        select(packet)


def test_quote_status_does_not_silently_treat_source_st_one_as_normal():
    packet = fixture([[-1, -1, -2, -3, -3], [-5] * 5])
    packet["quotes"]["rows"][0]["is_st"] = 1
    assert [m["security_id"] for m in select(packet)["members"]] == ["id-001"]


def test_sector_ties_stably_use_id_and_cap_three_directions():
    packet = fixture([[-1, -2, -2, -2, -3]] * 4 + [[-9] * 20])
    packet["catalog"]["rows"].reverse()
    result = select(packet)
    assert [s["sector_id"] for s in result["selected_sectors"]] == ["sector-0", "sector-1", "sector-2"]


def test_known_nonordinary_identity_never_enters_benchmark_or_pool():
    packet = fixture([[-1, -2, -2, -2, -3, 9], [-5] * 5])
    member = packet["universe"]["members"][5]
    member["security_type"] = "cdr"
    packet["universe"]["ordinary_a_count"] -= 1
    result = select(packet)
    assert result["relative_market_baseline"]["sample_count"] == 10
    assert result["selected_sectors"][0]["relative_metrics"]["market_mean_change_pct"] == -3.5
    assert [m["security_id"] for m in result["members"]] == ["id-000"]


@pytest.mark.parametrize("requested_date", ["2026-10-14", "2026-10-16"])
def test_universe_date_outside_market_date_and_actual_cutoff_is_rejected(requested_date):
    packet = fixture()
    packet["universe"]["requested_date"] = requested_date
    with pytest.raises(ValueError, match="date"):
        select(packet)


def test_missing_original_config_hash_cannot_produce_unreadable_archive():
    packet = fixture()
    original, _ = strict(packet)
    original.pop("config_hash")
    original.pop("content_hash")
    original.pop("selection_id")
    original["content_hash"] = digest(original)
    original["selection_id"] = "sector-" + DAY + "-" + original["content_hash"][:20]
    with pytest.raises(ValueError, match="hash"):
        select(packet, original)
