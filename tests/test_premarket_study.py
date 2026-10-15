def test_followup_uses_trading_sessions_and_never_fills_unmatured():
    from ashare_daily.premarket.study import followup_observations
    brief = {"market_as_of_date": "2026-10-09", "focus_candidates": [{"security_id": "a"}], "baseline_ids": ["a"]}
    calendar = {"2026-10-09": True, "2026-10-10": False, "2026-10-11": False,
                "2026-10-12": True, "2026-10-13": True, "2026-10-14": True}
    bars = {"a": [{"trade_date": "2026-10-09", "close": "100"},
                  {"trade_date": "2026-10-12", "close": "99"},
                  {"trade_date": "2026-10-13", "close": "102"},
                  {"trade_date": "2026-10-14", "close": "105"}]}
    result = followup_observations(brief, bars, calendar, as_of="2026-10-14")
    rows = {r["horizon"]: r for r in result["records"]}
    assert rows[3]["change_pct"] == "5.00"
    assert rows[5]["change_pct"] is None
    assert rows[10]["change_pct"] is None
    assert result["measure"] == "close_change_not_executable_return"


def test_missing_forward_close_is_unknown_not_zero():
    from ashare_daily.premarket.study import followup_observations
    brief = {"market_as_of_date": "2026-10-09", "focus_candidates": [{"security_id": "a"}], "baseline_ids": []}
    calendar = {"2026-10-09": True, "2026-10-10": False, "2026-10-11": False,
                "2026-10-12": True, "2026-10-13": True, "2026-10-14": True}
    result = followup_observations(brief, {"a": [{"trade_date": "2026-10-09", "close": "100"}]}, calendar, as_of="2026-10-14")
    assert result["records"][0]["status"] == "missing_data"
    assert result["records"][0]["change_pct"] is None
