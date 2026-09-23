"""Versioned cache/strategy contracts, retaining old F3-S assertions."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from test_sector_screening import fixture, seal_stock, freeze_input, CONFIG
from ashare_daily.sector_screening import evaluate_selection

NEW = json.loads((Path(__file__).resolve().parents[1] / "config/sector_screening_f4s1.json").read_text(encoding="utf-8"))

def partial(offset):
    selection, inputs = fixture(days=320, purpose="engineering_validation")
    packet = inputs["securities"]["security-0"]
    missing = packet["expected_dates"][offset]
    packet["raw_records"] = [row for row in packet["raw_records"] if row["trade_date"] != missing]
    window = packet["adjustment_window"]
    window["records"] = [row for row in window["records"] if row["trade_date"] != missing]
    window.update(complete=False, diagnostic_only=True, missing_dates=[missing])
    seal_stock(packet)
    packet.update(adjustment_window=None, diagnostic_adjustment_window=window,
        issues=["history_calendar_dates_missing", "complete_adjustment_window_missing"])
    freeze_input(inputs, selection)
    return selection, inputs, packet

def result(selection, inputs):
    freeze_input(inputs, selection)
    return evaluate_selection(selection, inputs, NEW)["evaluations"][0]

def test_extra_cache_gap_does_not_add_a_320_day_strategy_threshold():
    selection, inputs, _ = partial(100)
    before = deepcopy(inputs)
    row = result(selection, inputs)
    assert row["cache_target_complete"] is False and row["adjustment_ready"] is False
    assert row["strategy_inputs_ready"] is True and row["strategy_adjustment_ready"] is True
    assert row["metrics"]["valid_history_count"] == 120 and row["technical_status"] == "pass"
    assert row["eligibility_status"] == "pending" and row["production_eligible"] is False
    assert row["cache_status"]["cache_raw_dates"] == 319
    assert row["cache_status"]["price_fill_count"] == row["cache_status"]["calendar_dates_skipped"] == 0
    assert inputs == before
    # The old explicit semantics keep their historical behavior and assertions.
    assert evaluate_selection(selection, inputs, CONFIG)["evaluations"][0]["technical_status"] == "unknown"

@pytest.mark.parametrize("offset,missing_inputs", [(-80,["history"]),(-40,["history","trend"]),(-10,["history","trend","relative_strength","liquidity"]),(-1,["history","trend","relative_strength","liquidity","target_date"])])
def test_many_rows_never_replace_required_calendar_points(offset, missing_inputs):
    selection, inputs, _ = partial(offset)
    row = result(selection, inputs)
    assert row["technical_status"] == "unknown" and row["strategy_inputs_ready"] is False
    assert row["metrics"]["valid_history_count"] == 119
    assert len(row["metric_basis"]["calculation_dates"]) == 120
    assert all(row["rule_input_readiness"][name] is False for name in missing_inputs)

@pytest.mark.parametrize("kind", ["raw_binding","anchor","version","raw_only_date","adjusted_only_date","unit","hard_error","benchmark"])
def test_partial_cache_still_requires_exact_source_and_strategy_evidence(kind):
    selection, inputs, packet = partial(100)
    window = packet["diagnostic_adjustment_window"]
    if kind == "raw_binding": window["raw_fact_hashes"] = {}
    elif kind == "anchor": window["adjustment_anchor_hash"] = None
    elif kind == "version": window["content_hash"] = "f" * 64
    elif kind == "raw_only_date": window["records"].pop()
    elif kind == "adjusted_only_date": packet["raw_records"].pop()
    elif kind == "unit": packet["raw_records"][-1]["amount_unit"] = "thousand_CNY"
    elif kind == "hard_error": packet["issues"].append("source_identity_conflict")
    elif kind == "benchmark": inputs["benchmark"]["records"].pop()
    row = result(selection, inputs)
    assert row["strategy_inputs_ready"] is False
    assert row["technical_status"] == "unknown"

def test_complete_cache_and_four_boards_share_original_formula():
    selection, inputs = fixture(count=104, days=120, purpose="engineering_validation", boards=["sse_main","szse_main","chinext","star"])
    value = evaluate_selection(selection, inputs, NEW)
    assert len(value["evaluations"]) == 104
    assert all(row["strategy_inputs_ready"] and row["cache_target_complete"] for row in value["evaluations"])
    assert value["counts"]["candidate_count"] == value["model_calls"] == 0

def test_liquidity_failure_is_calculable_not_a_data_gap():
    selection, inputs, packet = partial(100)
    packet["adjustment_window"] = packet.pop("diagnostic_adjustment_window")
    for rows in (packet["raw_records"], packet["adjustment_window"]["records"]):
        for row in rows: row["amount_cny"] = "1000"
    seal_stock(packet)
    packet["diagnostic_adjustment_window"] = packet.pop("adjustment_window")
    row = result(selection, inputs)
    assert row["strategy_inputs_ready"] and row["technical_status"] == "fail"
