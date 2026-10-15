from copy import deepcopy

import pytest


def market_inputs(changes=(1, 1, 1, -1, 0)):
    members = [{"security_id": f"id{i}", "exchange": "SSE", "code": f"60000{i}",
                "security_type": "ordinary_a", "metadata_verified": True} for i in range(len(changes))]
    universe = {"members": members, "ordinary_a_count": len(members), "structural_verified": True}
    rows = [{"symbol": f"sh60000{i}", "trade_date": "2026-09-21", "quote_at": "2026-09-21T15:00:00+08:00",
             "change_pct": str(change), "close": str(100 + change), "reference_price": "100",
             "amount_cny": "1000", "amount_unit": "CNY", "price_unit": "CNY/share",
             "percentage_basis": "source_reference_price"} for i, change in enumerate(changes)]
    dates = [f"2026-09-{i:02}" for i in range(1, 22)]
    benchmark = {"verified": True, "symbol": "sh.000001", "security_type": "index",
                 "adjustment_mode": "index_native", "price_unit": "index_points", "expected_dates": dates,
                 "records": [{"trade_date": d, "close": str(100+i), "amount_cny": "100"}
                             for i, d in enumerate(dates)]}
    return universe, rows, benchmark


def snapshot(inputs):
    from ashare_daily.premarket.market import market_snapshot
    u, q, b = inputs
    return market_snapshot(universe=u, quotes=q, benchmark=b, market_date="2026-09-21")


def decide(market, **kwargs):
    from ashare_daily.premarket.decisions import decide_stance
    return decide_stance(market, news_ready=kwargs.pop("news_ready", True),
                         has_direction=kwargs.pop("has_direction", True), **kwargs)


def test_breadth_includes_flat_in_denominator_and_attack_at_sixty_percent():
    value = snapshot(market_inputs())
    assert value["status"] == "ready"
    assert value["advancing_fraction"] == "0.6"
    assert value["benchmark"]["ma20"] == "110.5"
    assert value["benchmark"]["previous_ma20"] == "109.5"
    assert decide(value)["stance"] == "attack"


@pytest.mark.parametrize("mutation", ["subset", "duplicate", "wrong_date", "bad_units", "bool_price", "nan", "bad_count", "index_gap"])
def test_invalid_component_is_excluded_without_discarding_other_valid_evidence(mutation):
    values = market_inputs()
    u, q, b = values
    if mutation == "subset": q.pop()
    elif mutation == "duplicate": q.append(deepcopy(q[0]))
    elif mutation == "wrong_date": q[0]["trade_date"] = "2026-09-20"
    elif mutation == "bad_units": q[0]["amount_unit"] = "万元"
    elif mutation == "bool_price": q[0]["close"] = True
    elif mutation == "nan": q[0]["change_pct"] = "NaN"
    elif mutation == "bad_count": u["ordinary_a_count"] = 6
    elif mutation == "index_gap": b["records"].pop(0)
    value = snapshot(values)
    assert value["status"] == "partial"
    affected = "trend" if mutation == "index_gap" else "breadth"
    assert value["components"][affected] == "unavailable"
    result = decide(value)
    assert affected not in result["used_components"]
    assert result["conditional"] is True
    assert result["evidence_status"] == "partial"


def test_only_evidenced_full_day_halt_is_removed_from_denominator():
    u, q, b = market_inputs((1, 1, 1, -1))
    u["members"][-1]["statuses"] = {"suspended": {"value": True, "verified": True, "full_day": True,
        "source": "registered", "evidence_id": "halt", "as_of_date": "2026-09-21"}}
    q.pop()
    value = snapshot((u, q, b))
    assert value["halted_count"] == 1
    assert value["advancing_fraction"] == "1"
    assert value["status"] == "ready"


def test_breadth_defense_survives_news_gap_and_known_event_still_matters():
    value = snapshot(market_inputs((-1, -1, -1, -1, 1)))
    assert decide(value)["stance"] == "defend"
    assert decide(value, news_ready=False)["stance"] == "defend"
    assert decide(value, news_ready=False)["limitations"]
    assert decide(snapshot(market_inputs()), adverse_event=True)["reason_code"] == "event_pending"


def test_declining_trend_and_forty_percent_breadth_defend():
    u, q, b = market_inputs((1, 1, -1, -1, -1))
    for i, row in enumerate(b["records"]): row["close"] = str(120-i)
    assert decide(snapshot((u, q, b)))["reason_code"] == "trend_and_breadth_weak"


def test_no_direction_and_unavailable_activity_have_separate_meanings():
    value = snapshot(market_inputs())
    assert decide(value, has_direction=False)["reason_code"] == "no_qualified_direction"
    u, q, b = market_inputs()
    for row in b["records"][-6:-1]: row["amount_cny"] = "0"
    result = decide(snapshot((u, q, b)))
    assert result["stance"] == "attack" and result["conditional"] is True
    assert "activity" not in result["used_components"]
