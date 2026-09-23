"""No lookahead in selection, no imputation, paired forward observations."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from ashare_daily import reference_study as study
from ashare_daily.providers.baostock import base_result, expected_fields, raw_hash
from ashare_daily.sector_selection import digest


def seal(packet):
    return {**{key: value for key, value in packet.items() if key != "content_hash"},
            "content_hash": digest({key: value for key, value in packet.items() if key != "content_hash"})}


def dates(count):
    # Deliberately contains weekend sessions and nonweekly gaps. The supplied
    # verified calendar, never weekday arithmetic, defines forward horizons.
    start = date(2025, 1, 1)
    return [(start + timedelta(days=index + index // 4)).isoformat() for index in range(count)]


def benchmark_fixture(calendar_dates):
    reference = {"path": "source-response.json", "sha256": "b" * 64}
    return seal({"schema_version": "reference-study-benchmark-v1", "verified": True,
        "source_hash": reference["sha256"], "source_reference": reference, "symbol": "sh.000001",
        "security_type": "index", "adjustment_mode": "index_native",
        "calendar": {"verified": True, "trading_dates": calendar_dates},
        "records": [{"trade_date": day, "close": "1000"} for day in calendar_dates]})


def panel_fixture(count=170, members=6):
    calendar_dates = dates(count)
    securities = {}
    for number in range(1, members + 1):
        rows = {}
        for index, day in enumerate(calendar_dates):
            close = Decimal(100) + Decimal(index * number)
            rows[day] = {"trade_date": day, "close": str(close), "high": str(close + 1),
                "low": str(close - 1), "volume_shares": 1000 + number, "amount_cny": "60000000",
                "amount_unit": "CNY", "tradestatus": True}
        securities[f"id-{number}"] = {"symbol": f"sz.{number:06d}", "name": str(number), "rows": rows, "issues": []}
    return {"dates": calendar_dates, "members": securities, "source_input_hash": "a" * 64, "source_observation_hash": "c" * 64}


def configure(monkeypatch, panel):
    calls = []
    monkeypatch.setattr(study, "validated_panel", lambda *args: panel)
    def score(bars):
        calls.append(deepcopy(bars))
        return {"status": "available", "score": 1100 - bars[-1]["volume_shares"]}
    monkeypatch.setattr(study, "score_reference_technical", score)
    return calls


def compare(panel):
    return study.compare_reference_strategies({}, {}, {}, benchmark_fixture(panel["dates"]))


def test_fixed_paired_groups_and_no_automatic_promotion(monkeypatch):
    panel = panel_fixture()
    calls = configure(monkeypatch, panel)
    packet = compare(panel)
    first = packet["anchors"][0]
    assert packet["status"] == "available"
    assert first["baseline_security_ids"] == ["id-6", "id-5", "id-4", "id-3", "id-2"]
    assert first["reference_security_ids"] == ["id-1", "id-2", "id-3", "id-4", "id-5"]
    assert first["top5_overlap_count"] == 4 and first["top5_overlap_fraction"] == "0.8"
    assert first["forward"]["20"]["status"] == "included"
    assert packet["conclusion"] == "evidence_insufficient_for_promotion"
    assert packet["ranking_change_allowed"] is False and packet["historical_qualification_checked"] is False
    assert packet["network_requests"] == packet["model_calls"] == packet["production_data_writes"] == 0
    assert packet["content_hash"] == digest({key: value for key, value in packet.items() if key != "content_hash"})
    assert len(calls) == len(packet["anchors"]) * 6


def test_indicator_only_sees_120_bars_at_or_before_its_anchor(monkeypatch):
    panel = panel_fixture(320)
    calls = configure(monkeypatch, panel)
    packet = compare(panel)
    assert len(packet["anchors"]) == 37
    for index, anchor in enumerate(packet["anchors"]):
        expected = panel["dates"][anchor["anchor_trading_index"] - 119:anchor["anchor_trading_index"] + 1]
        for bars in calls[index * 6:(index + 1) * 6]:
            assert len(bars) == 120
            assert [bar["trade_date"] for bar in bars] == expected
            assert max(bar["trade_date"] for bar in bars) == anchor["anchor_date"]


def test_future_prices_cannot_change_past_selections(monkeypatch):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    original = compare(panel)["anchors"][0]
    for member in panel["members"].values():
        for day in panel["dates"][120:]:
            row = member["rows"][day]
            for key in ("close", "high", "low"):
                row[key] = str(Decimal(row[key]) * 2)
    altered = compare(panel)["anchors"][0]
    assert altered["baseline_security_ids"] == original["baseline_security_ids"]
    assert altered["reference_security_ids"] == original["reference_security_ids"]
    assert altered["forward"]["20"]["baseline"] != original["forward"]["20"]["baseline"]


@pytest.mark.parametrize("missing_index,horizon_status", [(123, {"5": "excluded", "20": "excluded"}), (130, {"5": "included", "20": "excluded"})])
def test_missing_future_in_only_one_group_excludes_both_without_replacement(monkeypatch, missing_index, horizon_status):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    del panel["members"]["id-1"]["rows"][panel["dates"][missing_index]]
    first = compare(panel)["anchors"][0]
    assert "id-1" not in first["baseline_security_ids"] and "id-1" in first["reference_security_ids"]
    for horizon, status in horizon_status.items():
        forward = first["forward"][horizon]
        assert forward["status"] == status
        if status == "excluded":
            assert forward["baseline"] is forward["reference"] is None
            assert forward["missing"][0]["security_id"] == "id-1"
    assert first["reference_security_ids"] == ["id-1", "id-2", "id-3", "id-4", "id-5"]


def test_unknown_reference_score_uses_common_pool_and_reports_original_difference(monkeypatch):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    def score(bars):
        return {"status": "unavailable", "score": None} if bars[-1]["volume_shares"] == 1006 else {"status": "available", "score": 50}
    monkeypatch.setattr(study, "score_reference_technical", score)
    first = compare(panel)["anchors"][0]
    assert first["technical_pool_count"] == 6 and first["common_scored_pool_count"] == 5
    assert first["score_unavailable_security_ids"] == ["id-6"]
    assert first["original_baseline_security_ids"][0] == "id-6"
    assert "id-6" not in first["baseline_security_ids"] + first["reference_security_ids"]
    assert first["original_baseline_removed_security_ids"] == ["id-6"]
    assert first["baseline_changed_by_common_pool"] is True


@pytest.mark.parametrize("invalid", [{"status": "ok", "score": 60}, {"status": "available", "score": True}, {"status": "available", "score": "60"}])
def test_invalid_scores_never_enter_either_comparison_group(monkeypatch, invalid):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    monkeypatch.setattr(study, "score_reference_technical", lambda bars: invalid)
    packet = compare(panel)
    assert packet["status"] == "insufficient_data"
    assert packet["anchors"][0]["common_scored_pool_count"] == 0
    assert packet["aggregates"]["all"]["horizons"]["20"]["baseline"]["mean_price_change_pct"] is None


def test_missing_past_bars_stay_missing_and_source_issues_stay_in_denominator(monkeypatch):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    missing_date = panel["dates"][100]
    del panel["members"]["id-1"]["rows"][missing_date]
    panel["members"]["id-2"].update(rows={}, issues=["unverified_source"])
    packet = compare(panel)
    first = packet["anchors"][0]
    assert packet["sample_member_count"] == first["sample_member_count"] == 6
    assert packet["source_issue_member_count"] == 1
    assert first["historical_window_excluded_count"] == 2
    assert first["historical_window_exclusions"][0]["missing_dates"] == [missing_date]
    assert first["common_scored_pool_count"] == 4
    assert missing_date not in panel["members"]["id-1"]["rows"]


@pytest.mark.parametrize("invalid_amount", [None, "", "NaN", "-1", "missing_field"])
def test_earlier_amount_missing_or_invalid_excludes_whole_120_day_window(monkeypatch, invalid_amount):
    panel = panel_fixture()
    calls = configure(monkeypatch, panel)
    row = panel["members"]["id-6"]["rows"][panel["dates"][80]]
    if invalid_amount == "missing_field":
        del row["amount_cny"]
    else:
        row["amount_cny"] = invalid_amount
    first = compare(panel)["anchors"][0]
    assert first["historical_window_exclusions"] == [{"security_id": "id-6", "reason": "historical_window_invalid", "missing_dates": []}]
    assert first["technical_pool_count"] == first["common_scored_pool_count"] == 5
    assert "id-6" not in first["original_baseline_security_ids"] + first["baseline_security_ids"] + first["reference_security_ids"]
    assert all(bars[-1]["volume_shares"] != 1006 for bars in calls[:5])


def test_earlier_zero_amount_is_valid_but_does_not_dilute_last_20_day_mean(monkeypatch):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    for day in panel["dates"][:100]:
        panel["members"]["id-6"]["rows"][day]["amount_cny"] = "0"
        panel["members"]["id-6"]["rows"][day]["volume_shares"] = 0
    first = compare(panel)["anchors"][0]
    assert first["historical_window_excluded_count"] == 0
    assert first["technical_pool_count"] == 6
    assert first["baseline_security_ids"][0] == "id-6"


@pytest.mark.parametrize("changes", [
    {"volume_shares": 1006, "amount_cny": "0"},
    {"volume_shares": 0, "amount_cny": "60000000"},
    {"volume_shares": 0, "amount_cny": "0", "tradestatus": None},
    {"volume_shares": 0, "amount_cny": "0", "tradestatus": False},
    {"volume_shares": 1006, "amount_cny": "60000000", "tradestatus": False},
    {"volume_shares": True}, {"volume_shares": -1}, {"volume_shares": "1006"},
    {"amount_unit": "thousand_CNY"}, {"amount_unit": None},
])
def test_full_window_reuses_production_amount_volume_unit_and_status_validation(monkeypatch, changes):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    panel["members"]["id-6"]["rows"][panel["dates"][80]].update(changes)
    first = compare(panel)["anchors"][0]
    assert first["historical_window_exclusions"] == [{"security_id": "id-6", "reason": "historical_window_invalid", "missing_dates": []}]
    assert "id-6" not in first["original_baseline_security_ids"]


def test_baseline_exact_thresholds_and_formula_precision_match_production(monkeypatch):
    from ashare_daily.factors.trend import mean_window, period_return, subtract
    panel = panel_fixture()
    configure(monkeypatch, panel)
    member = panel["members"]["id-6"]
    window = panel["dates"][:120]
    benchmark = {day: Decimal(997 + index) for index, day in enumerate(panel["dates"])}
    for day in window:
        member["rows"][day]["amount_cny"] = "50000000"
    record, reason, missing = study._candidate(member, "id-6", window, benchmark)
    assert reason is None and missing == []
    closes = [member["rows"][day]["close"] for day in window]
    expected_relative = subtract(period_return(closes, 20), period_return([benchmark[day] for day in window[-21:]], 20))
    assert record["relative"] == expected_relative
    assert record["amount"] == mean_window([member["rows"][day]["amount_cny"] for day in window], 20, positive=False)
    member["rows"][window[-1]]["amount_cny"] = "49999999.999999999999"
    assert study._candidate(member, "id-6", window, benchmark)[1] == "technical_conditions_not_met"


def test_baseline_strict_trend_and_relative_gates_match_production(monkeypatch):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    member = panel["members"]["id-6"]
    window = panel["dates"][:120]
    # Exact same 21-point stock/index return fails the strict relative gate.
    same_prices = {day: Decimal(member["rows"][day]["close"]) for day in window}
    assert study._candidate(member, "id-6", window, same_prices)[1] == "technical_conditions_not_met"
    # Equal close and moving averages fail the strict trend gate.
    for day in window:
        member["rows"][day]["close"] = "100"
    falling_index = {day: Decimal(1000 - index) for index, day in enumerate(window)}
    assert study._candidate(member, "id-6", window, falling_index)[1] == "technical_conditions_not_met"


def test_forward_horizons_and_anchor_steps_follow_given_calendar(monkeypatch):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    packet = compare(panel)
    for anchor in packet["anchors"]:
        index = anchor["anchor_trading_index"]
        assert (index - 119) % 5 == 0
        assert anchor["forward"]["5"]["end_date"] == panel["dates"][index + 5]
        assert anchor["forward"]["20"]["end_date"] == panel["dates"][index + 20]
    assert any(date.fromisoformat(day).weekday() >= 5 for day in panel["dates"])


def test_chronological_two_thirds_split_and_missing_counts(monkeypatch):
    panel = panel_fixture(320)
    configure(monkeypatch, panel)
    packet = compare(panel)
    assert packet["split_anchor_index"] == 24
    assert packet["aggregates"]["exploration"]["anchor_count"] == 24
    assert packet["aggregates"]["holdout"]["anchor_count"] == 13
    assert max(anchor["anchor_date"] for anchor in packet["anchors"] if anchor["segment"] == "exploration") < min(anchor["anchor_date"] for anchor in packet["anchors"] if anchor["segment"] == "holdout")
    assert packet["aggregates"]["holdout"]["horizons"]["20"]["included_anchor_count"] == 13
    assert packet["aggregates"]["holdout"]["horizons"]["20"]["baseline"]["stock_observation_count"] == 65
    assert packet["parameters"]["parameters_fitted"] is False


@pytest.mark.parametrize("damage", ["missing", "duplicate", "nan", "unverified", "wrong_symbol", "unsealed"])
def test_missing_or_invalid_full_benchmark_rejects_study(monkeypatch, damage):
    panel = panel_fixture()
    configure(monkeypatch, panel)
    benchmark = benchmark_fixture(panel["dates"])
    if damage == "missing":
        benchmark["records"].pop(0)
    elif damage == "duplicate":
        benchmark["records"].append(deepcopy(benchmark["records"][0]))
    elif damage == "nan":
        benchmark["records"][0]["close"] = "NaN"
    elif damage == "unverified":
        benchmark["verified"] = False
    elif damage == "wrong_symbol":
        benchmark["symbol"] = "sz.399001"
    if damage != "unsealed":
        benchmark = seal(benchmark)
    else:
        benchmark["content_hash"] = "d" * 64
    packet = study.compare_reference_strategies({}, {}, {}, benchmark)
    assert packet["status"] == "insufficient_data" and packet["issues"]
    assert packet["anchors"] == []


def test_less_than_140_dates_and_empty_pool_are_explicit(monkeypatch):
    panel = panel_fixture(139)
    configure(monkeypatch, panel)
    assert "fewer_than_120_history_plus_20_future_bars" in compare(panel)["issues"]
    panel = panel_fixture(170, 0)
    configure(monkeypatch, panel)
    assert compare(panel)["status"] == "insufficient_data"


def response_fixture(count=320):
    calendar_dates = dates(count)
    parameters = {"code": "sh.000001", "start_date": calendar_dates[0], "end_date": calendar_dates[-1],
                  "security_type": "index", "adjustment_mode": "unadjusted"}
    response = base_result("history_f2", parameters)
    fields = expected_fields("history_f2", parameters)
    rows = [{"date": day, "code": "sh.000001", "open": "1000", "high": "1010", "low": "990", "close": "1001", "preclose": "1000", "volume": "10", "amount": "10000"} for day in calendar_dates]
    response.update(ok=True, status="ok", error_code="0", error_msg="success", fields=fields, rows=rows,
        raw_hash=raw_hash(fields, rows), fetched_at="2026-09-22T12:00:00+08:00", login={"ok": True, "error_code": "0", "error_msg": "success"})
    return response, {"verified": True, "trading_dates": calendar_dates}


def normalize(response, calendar, source_hash="b" * 64, reference=None):
    return study.normalize_study_benchmark(response, calendar, source_hash=source_hash,
        source_reference=reference or {"path": "source-response.json", "sha256": source_hash})


def test_normalizer_accepts_original_f2_window_longer_than_366_natural_days():
    response, calendar = response_fixture()
    original = deepcopy(response)
    packet = normalize(response, calendar)
    assert (date.fromisoformat(response["parameters"]["end_date"]) - date.fromisoformat(response["parameters"]["start_date"])).days > 366
    assert len(packet["records"]) == 320 and packet["verified"] is True
    assert response == original
    assert packet["source_hash"] == packet["source_reference"]["sha256"]
    assert packet["network_requests"] == 1 and packet["model_calls"] == packet["production_database_writes"] == 0


@pytest.mark.parametrize("damage", ["code", "date", "nan", "highlow", "negative_volume", "source_hash_alias", "raw_hash", "calendar", "parameters"])
def test_normalizer_rejects_corrupt_or_aliased_source(damage):
    response, calendar = response_fixture()
    reference = {"path": "source-response.json", "sha256": "b" * 64}
    if damage == "code":
        response["rows"][0]["code"] = "sz.000001"
    elif damage == "date":
        response["rows"][0]["date"] = response["rows"][1]["date"]
    elif damage == "nan":
        response["rows"][0]["close"] = "NaN"
    elif damage == "highlow":
        response["rows"][0]["low"] = "1002"
    elif damage == "negative_volume":
        response["rows"][0]["volume"] = "-1"
    elif damage == "source_hash_alias":
        reference["sha256"] = "c" * 64
    elif damage == "calendar":
        calendar["verified"] = False
    elif damage == "parameters":
        response["parameters"]["code"] = "sz.000001"
    response["raw_hash"] = "x" if damage == "raw_hash" else raw_hash(response["fields"], response["rows"])
    with pytest.raises(ValueError):
        normalize(response, calendar, reference=reference)


def load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts/compare_reference_strategy.py"
    spec = importlib.util.spec_from_file_location("reference_study_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_verifies_adjacent_raw_response_and_exact_rebuilt_packet(tmp_path):
    response, calendar = response_fixture()
    source = tmp_path / "source-response.json"
    source.write_text(json.dumps(response), encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    packet = normalize(response, calendar, source_hash, {"path": "D:\\other-machine\\source-response.json", "sha256": source_hash})
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    cli = load_cli()
    assert cli.read_verified_benchmark(path) == packet
    packet["records"][0]["close"] = "1002"
    path.write_text(json.dumps(seal(packet)), encoding="utf-8")
    with pytest.raises(ValueError, match="differs_from_original"):
        cli.read_verified_benchmark(path)
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="source_file_hash_mismatch"):
        cli.read_verified_benchmark(path)


def test_cli_writes_only_new_output_and_never_overwrites_input(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("original", encoding="utf-8")
    cli = load_cli()
    with pytest.raises(ValueError, match="output_must_be_new"):
        cli.main(["--selection", str(output), "--inputs", str(output), "--observation", str(output), "--benchmark", str(output), "--output", str(output)])
    assert output.read_text(encoding="utf-8") == "original"
