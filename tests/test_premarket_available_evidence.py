"""User correction: missing information limits certainty, not the direction itself."""
from copy import deepcopy

import pytest

from test_premarket_market import market_inputs, snapshot, decide


def test_strong_valid_market_still_supports_attack_with_news_gaps():
    result = decide(snapshot(market_inputs()), news_ready=False)
    assert result["stance"] == "attack"
    assert result["evidence_status"] == "partial"
    assert result["limitations"] and result["rule_version"] == "premarket-stance-v2"
    assert result["probability"] is None


def test_weak_valid_market_still_supports_defense_with_news_gaps():
    result = decide(snapshot(market_inputs((-1, -1, -1, -1, 1))), news_ready=False)
    assert result["stance"] == "defend"
    assert result["reason_code"] == "declines_spreading"


def test_index_unavailable_does_not_erase_independently_verified_breadth():
    u, q, b = market_inputs()
    b["records"].pop()
    market = snapshot((u, q, b))
    assert market["status"] == "partial"
    assert market["advancing_fraction"] == "0.6"
    assert market["components"]["breadth"] == "ready"
    result = decide(market, news_ready=False)
    assert result["stance"] == "attack" and result["conditional"] is True
    assert result["basis_scope"] == "whole_market_breadth"


def test_partial_quote_sample_is_not_promoted_to_whole_market_breadth():
    u, q, b = market_inputs()
    q.pop()
    market = snapshot((u, q, b))
    assert market["advancing_fraction"] is None
    result = decide(market, news_ready=False)
    assert result["stance"] == "attack" and result["conditional"] is True
    assert result["basis_scope"] == "sse_index_trend"
    assert "breadth" not in result["used_components"]


def test_missing_turnover_does_not_erase_valid_price_trend():
    u, q, b = market_inputs()
    b["records"][-1]["amount_cny"] = None
    b["records"][-1]["quality_flags"] = ["missing_amount_cny"]
    market = snapshot((u, q, b))
    assert market["benchmark"]["close"] == "120"
    assert market["components"]["activity"] == "unavailable"
    assert decide(market)["stance"] == "attack"
    assert decide(market)["conditional"] is True


def test_index_only_weakness_still_produces_conditional_defense():
    u, q, b = market_inputs()
    q.pop()
    for i, row in enumerate(b["records"]): row["close"] = str(120-i)
    result = decide(snapshot((u, q, b)), news_ready=False)
    assert result["stance"] == "defend" and result["basis_scope"] == "sse_index_trend"


def test_no_valid_core_evidence_is_undetermined_not_hold():
    u, q, b = market_inputs()
    q.pop()
    b["records"].pop()
    result = decide(snapshot((u, q, b)), news_ready=False)
    assert result["stance"] == "undetermined"
    assert result["label"] == "暂无法判断"
    assert result["used_components"] == []


def test_known_adverse_market_event_still_overrides_positive_evidence():
    result = decide(snapshot(market_inputs()), news_ready=False, adverse_event=True)
    assert result["stance"] == "hold" and result["reason_code"] == "event_pending"


def test_no_qualified_direction_keeps_explanation_of_available_bullish_evidence():
    result = decide(snapshot(market_inputs()), news_ready=False, has_direction=False)
    assert result["stance"] == "hold" and result["reason_code"] == "no_qualified_direction"
    assert result["directional_bias"] == "bullish" and result["analysis"]


def test_invalid_component_values_never_become_usable_evidence():
    market = snapshot(market_inputs())
    market["advancing_fraction"] = "NaN"
    market["benchmark"]["close"] = True
    result = decide(market)
    assert result["stance"] == "undetermined"


def test_partial_evidence_does_not_ignore_known_weak_activity():
    market = snapshot(market_inputs())
    market["components"]["trend"] = "unavailable"
    market["benchmark"]["amount"] = "1"
    result = decide(market, news_ready=False)
    assert result["stance"] == "hold" and result["reason_code"] == "mixed"


def test_v2_report_allows_evidenced_attack_and_displays_limitations():
    from test_premarket_report import make_brief
    from ashare_daily.premarket.report import report_sections, validate_brief
    report = make_brief()
    assert report["schema_version"] == "premarket-brief-v2"
    assert report["stance"]["stance"] == "attack" and report["coverage"]["ready"] is False
    assert report["research_status"] == "partial"
    validate_brief(report)
    assert any("资料限制" in str(block) for block in report_sections(report))
    altered = deepcopy(report)
    altered["stance"]["reasons"] = ["与原始证据不一致的结论"]
    with pytest.raises(ValueError, match="stance"):
        validate_brief(altered)
    report["market"]["benchmark"]["close"] = None
    report["market"]["advancing_fraction"] = None
    with pytest.raises(ValueError, match="stance"):
        validate_brief(report)


def test_v1_archive_keeps_its_original_conservative_semantics(tmp_path):
    from test_premarket_report import make_brief, DUE
    from ashare_daily.premarket.report import validate_brief
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    report = make_brief()
    report["schema_version"] = "premarket-brief-v1"
    report["rule_versions"]["stance"] = "premarket-stance-v1"
    report["stance"] = {"rule_version": "premarket-stance-v1", "stance": "hold", "label": "按兵不动",
        "reason_code": "data_insufficient", "reasons": ["历史规则保留"]}
    validate_brief(report)
    publish_brief(tmp_path, report, now=DUE)
    archives, errors = scan_briefs(tmp_path / "outputs", now=DUE)
    assert not errors and archives[0]["report"]["stance"]["reasons"] == ["历史规则保留"]
    report["stance"]["stance"] = "attack"
    with pytest.raises(ValueError): validate_brief(report)


@pytest.mark.parametrize("failure", ["selection", "stock_history"])
def test_production_adapter_keeps_market_facts_when_stock_preparation_fails(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from ashare_daily.premarket import services_market as service
    u, q, b = market_inputs()
    cfg = SimpleNamespace(sector_config="sector", observation_config="obs", universe_config="u", eligibility_sources_config="e")
    monkeypatch.setattr(service, "load_config", lambda *a: {"output_directory": "source"})
    monkeypatch.setattr(service, "load_options", lambda *a: {"max_quote_status_seconds": 1, "max_quote_status_queries": 1,
        "require_delisting_check": False, "strategy_config": "strategy"})
    monkeypatch.setattr(service, "load_universe_config", lambda *a: SimpleNamespace(calendar_provider="exchange_calendars"))
    monkeypatch.setattr(service, "_read_snapshot", lambda *a: None)
    monkeypatch.setattr(service, "collect_inputs", lambda *a, **k: {"universe": u, "quotes": {"rows": q}, "catalog": {}, "memberships": {}, "calendar": {}})
    monkeypatch.setattr(service, "supplement_zero_quotes", lambda project, packet, *a, **k: packet)
    selection = {"selection_verified": failure != "selection", "selection_id": "s", "members": [], "selected_sectors": [], "target_date": "2026-09-21"}
    monkeypatch.setattr(service, "evaluate", lambda *a, **k: (selection, {}))
    # This test injects a stock-stage failure, not source/selection provenance.
    monkeypatch.setattr(service, "build_observation_selection", lambda *a: selection)
    monkeypatch.setattr(service, "archive_selection", lambda *a: None)
    def fail_observation(*a, **k): raise ValueError("stock_history_unavailable")
    monkeypatch.setattr(service, "_observe", fail_observation)
    online = []
    def benchmark(*a, **k):
        online.append(k["online"])
        return b
    monkeypatch.setattr(service, "prepare_benchmark", benchmark)
    result = service.collect_market(tmp_path, cfg, {"report_date": "2026-09-22", "market_as_of_date": "2026-09-21", "is_trading_day": True}, tmp_path, 10, "test")
    assert result["market"]["advancing_fraction"] == "0.6"
    assert result["market"]["benchmark"]["close"] == "120"
    assert result["market"]["preparation_limitations"]
    assert result["preselection"]["candidates"] == []
    # An ambiguous failed history stage must not trigger another online request.
    assert online == [failure == "selection"]


def test_real_index_normalization_can_keep_prices_but_legacy_series_remains_strict():
    from ashare_daily import sector_benchmark as benchmark
    from test_sector_benchmark import index_response, DATES, bs
    raw = index_response()
    raw["rows"][-1]["amount"] = ""
    raw["raw_hash"] = bs.raw_hash(raw["fields"], raw["rows"])
    calendar = {"calendar": {day: True for day in DATES}}
    with pytest.raises(ValueError): benchmark._series(raw, DATES, calendar, mode="offline_test")
    series = benchmark._series(raw, DATES, calendar, mode="offline_test", allow_activity_gaps=True)
    assert series["records"][-1]["quality_flags"] == ["missing_amount_cny"]
    assert series["records"][-1]["close"] is not None and series["activity_verified"] is False
    raw["rows"][-1]["close"] = ""
    raw["raw_hash"] = bs.raw_hash(raw["fields"], raw["rows"])
    with pytest.raises(ValueError): benchmark._series(raw, DATES, calendar, mode="offline_test", allow_activity_gaps=True)


def test_market_price_projection_reuses_failed_raw_response_without_network_or_legacy_overwrite(tmp_path, monkeypatch):
    from ashare_daily import sector_benchmark as benchmark
    from test_sector_benchmark import index_response, DATES, bs, validation_config, validation_freeze
    cfg = validation_config()
    selection = validation_freeze(cfg)
    directory = tmp_path / cfg["output_directory"] / selection["selection_id"]
    calendar = {"verified": True, "calendar": {day: True for day in DATES}, "trading_dates": DATES}
    monkeypatch.setattr(benchmark, "_dates", lambda *a: (directory, calendar))
    monkeypatch.setattr(benchmark, "_permission", lambda *a: None)
    monkeypatch.setattr(benchmark.BaoStockF2Client, "query", lambda *a, **k: pytest.fail("unexpected network retry"))
    raw = index_response()
    raw["rows"][-1]["amount"] = ""
    raw["raw_hash"] = bs.raw_hash(raw["fields"], raw["rows"])
    benchmark._write_new(directory / "benchmark/runs/failed/source-response.json", raw)
    result = benchmark.prepare_benchmark(tmp_path, selection, cfg, online=False, required_for_market=True)
    assert result["verified"] is True and result["activity_verified"] is False
    assert result["network_requests"] == 0
    assert not (directory / "benchmark/benchmark_snapshot.json").exists()
    assert benchmark.read_benchmark(tmp_path, selection, cfg)["verified"] is False
    again = benchmark.prepare_benchmark(tmp_path, selection, cfg, online=False, required_for_market=True)
    assert again["records"] == result["records"] and again["network_requests"] == 0
