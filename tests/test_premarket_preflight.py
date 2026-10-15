from datetime import datetime

import pytest


CALENDAR = {"2026-10-09": True, "2026-10-10": False, "2026-10-11": False,
    "2026-10-12": True, "2026-10-13": True, "2026-10-14": True, "2026-10-15": True}


def test_preflight_is_real_morning_only_and_uses_verified_calendar():
    from ashare_daily.premarket.preflight import preflight_plan
    plan = preflight_plan(datetime.fromisoformat("2026-10-12T08:35:00+08:00"), CALENDAR)
    assert plan["market_as_of_date"] == "2026-10-09"
    assert plan["master_date"] == "2026-10-12"
    for stamp in ["2026-10-12T19:00:00+08:00", "2026-10-12T08:50:00+08:00"]:
        with pytest.raises(ValueError, match="outside_premarket_window"):
            preflight_plan(datetime.fromisoformat(stamp), CALENDAR)
    with pytest.raises(ValueError, match="calendar"):
        preflight_plan(datetime.fromisoformat("2026-10-15T08:35:00+08:00"), {})


def packets():
    from ashare_daily.premarket.preflight import preflight_plan
    plan = preflight_plan(datetime.fromisoformat("2026-10-15T08:35:00+08:00"), CALENDAR)
    master = {"universe_verified": True, "collection_ready": True, "requested_date": "2026-10-15", "mode": "research"}
    quotes = {"quote_complete": True, "provenance_mode": "online", "verification_kind": "live_network",
        "fetched_at": "2026-10-15T08:36:00+08:00", "rows": [{"symbol": f"s{i}", "identity": {"board": b},
        "source_business_date": "2026-10-14", "date_verified": True, "issues": []} for i,b in enumerate(["sse_main","star","szse_main","chinext"])]}
    return plan, master, quotes


def test_preflight_cannot_promote_sample_to_whole_market_or_business_acceptance():
    from ashare_daily.premarket.preflight import assess_preflight
    result = assess_preflight(*packets())
    assert result["sample_date_semantics_verified"] is True
    assert result["full_market_verified"] is False and result["report_generated"] is False


@pytest.mark.parametrize("failure", ["same_day_quote", "offline", "late", "missing_board", "old_master"])
def test_preflight_rejects_wrong_date_or_incomplete_evidence(failure):
    from ashare_daily.premarket.preflight import assess_preflight
    plan, master, quotes = packets()
    if failure == "same_day_quote": quotes["rows"][0]["source_business_date"] = "2026-10-15"
    elif failure == "offline": quotes["provenance_mode"] = "offline_test"
    elif failure == "late": quotes["fetched_at"] = "2026-10-15T09:00:00+08:00"
    elif failure == "missing_board": quotes["rows"].pop()
    else: master["requested_date"] = "2026-10-14"
    assert assess_preflight(plan, master, quotes)["sample_date_semantics_verified"] is False


def test_outside_window_makes_no_requests_or_output(tmp_path, monkeypatch):
    from ashare_daily.premarket import preflight
    class Evening(datetime):
        @classmethod
        def now(cls, tz=None): return cls.fromisoformat("2026-10-14T19:00:00+08:00")
    monkeypatch.setattr(preflight, "datetime", Evening)
    result = preflight.run_preflight(tmp_path)
    assert result["status"] == "outside_premarket_window" and result["network_requests"] == 0
    assert list(tmp_path.iterdir()) == []


def test_exact_one_request_quote_sample_can_finish_without_enlarging_budget(tmp_path):
    from ashare_daily.providers.sina_sectors import SinaSectorProvider
    from test_sina_sectors import identity, response, quote, PERMISSION
    source = SinaSectorProvider(tmp_path / "one", mode="offline_test", permission=PERMISSION, max_requests=1,
        pause_seconds=0, transport=lambda url: response(url, quote()))
    try:
        packet = source.fetch_quotes([identity()], "2026-09-11")
        assert source.client.calls == 1
        assert packet["quote_complete"] is True
        assert "quote_source_stopped_before_completion" not in packet["issues"]
    finally:
        source.close()


def test_one_request_limit_still_reports_unread_quote_batches(tmp_path):
    from ashare_daily.providers.sina_sectors import SinaSectorProvider
    from test_sina_sectors import identity, response, quote, PERMISSION
    ids = [identity(code=f"{600001+i:06d}") for i in range(81)]
    source = SinaSectorProvider(tmp_path / "limited", mode="offline_test", permission=PERMISSION, max_requests=1,
        pause_seconds=0, transport=lambda url: response(url, "".join(quote(symbol="sh"+item.code) for item in ids[:80])))
    try:
        packet = source.fetch_quotes(ids, "2026-09-11")
        assert source.client.calls == 1 and packet["quote_complete"] is False
        assert len(packet["missing_symbols"]) == 1
        assert "quote_source_stopped_before_completion" in packet["issues"]
    finally:
        source.close()


def test_assessment_consumes_actual_quote_parser_identity_structure():
    from ashare_daily.premarket.preflight import assess_preflight, preflight_plan
    from ashare_daily.providers.sina_sectors import parse_quotes, QUOTE_BASE
    from test_sina_sectors import identity, response, quote
    definitions = [("600001", "SSE", "sse_main"), ("688001", "SSE", "star"),
        ("000001", "SZSE", "szse_main"), ("300001", "SZSE", "chinext")]
    ids = [identity(code=c, exchange=e, board=b) for c,e,b in definitions]
    symbols = [("sh" if e == "SSE" else "sz") + c for c,e,_ in definitions]
    raw = response(QUOTE_BASE + ",".join(symbols), "".join(quote(symbol=s, day="2026-10-12") for s in symbols))
    raw["fetched_at"] = "2026-10-13T08:36:00+08:00"
    rows, missing = parse_quotes(raw, ids, "2026-10-12")
    assert not missing and all(not row["issues"] for row in rows)
    plan = preflight_plan(datetime.fromisoformat("2026-10-13T08:35:00+08:00"), CALENDAR)
    # Synthetic packet tests the boundary shape only; production has no injected
    # transport and the real-run result is separately labelled sample-only.
    master = {"universe_verified": True, "collection_ready": True, "requested_date": "2026-10-13", "mode": "research"}
    packet = {"rows": rows, "quote_complete": True, "provenance_mode": "online", "verification_kind": "live_network", "fetched_at": raw["fetched_at"]}
    assert assess_preflight(plan, master, packet)["sample_date_semantics_verified"] is True
