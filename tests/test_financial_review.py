"""Quarterly enrichment never turns missing or future data into passing facts."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest

from ashare_daily import financial_review as finance


CANDIDATES = [{"security_id": "sse-a-600000", "symbol": "sh.600000", "name": "样本"}]
TARGET = "2026-09-21"
CUTOFF = TARGET + "T21:00:00+08:00"
NOW = datetime.fromisoformat("2026-09-22T11:00:00+08:00")


def row(operation, symbol="sh.600000", year=2026, quarter=2):
    result = {field: "1.25" for field in finance.FIELDS[operation]}
    result.update(code=symbol, pubDate="2026-08-15" if quarter == 2 else "2026-04-20",
                  statDate=finance._quarter_end(year, quarter).isoformat())
    return result


def setup_worker(monkeypatch, response=None):
    calls = []
    monkeypatch.setattr(finance, "_now", lambda: NOW)
    def query(operation, symbol, year, quarter, timeout_seconds):
        calls.append((operation, symbol, year, quarter, timeout_seconds))
        return response(operation, symbol, year, quarter) if response else {
            "status": "ok", "fields": finance.FIELDS[operation], "rows": [row(operation, symbol, year, quarter)]}
    monkeypatch.setattr(finance, "_run_query", query)
    return calls


def collect(tmp_path, **kwargs):
    defaults = dict(candidates=CANDIDATES, target_date=TARGET, cutoff_at=CUTOFF,
                    output_directory=tmp_path / "output", cache_directory=tmp_path / "cache")
    defaults.update(kwargs)
    return finance.collect_financial_review(**defaults)


def reseal(packet):
    return finance._seal({key: value for key, value in packet.items() if key != "content_hash"})


def test_available_preserves_raw_units_and_freezes_first_observation(monkeypatch, tmp_path):
    calls = setup_worker(monkeypatch)
    packet = collect(tmp_path)
    assert packet["status"] == "available" and packet["network_requests"] == len(calls) == 2
    assert packet["historical_reconstruction"] is True and packet["model_calls"] == 0
    record = packet["records"][0]
    assert record["profit"]["roeAvg"] == "1.25"
    assert record["cash_flow"]["CFOToNP"] == "1.25"
    assert record["risk_flags"] == [] and record["gaps"]
    for operation in finance.FIELDS:
        provenance = record["provenance"][operation]
        source = json.loads(Path(provenance["source_file"]).read_text(encoding="utf-8"))
        assert provenance["first_observed_at"] == NOW.isoformat()
        assert source["rows"][0] == record[operation]
        assert source["raw_hash"] == provenance["raw_hash"]
    finance.validate_financial_review(packet, candidates=CANDIDATES, target_date=TARGET, cutoff_at=CUTOFF)


def test_second_run_uses_cache_and_preserves_first_seen(monkeypatch, tmp_path):
    calls = setup_worker(monkeypatch)
    first = collect(tmp_path)
    monkeypatch.setattr(finance, "_now", lambda: NOW + timedelta(hours=1))
    second = collect(tmp_path, output_directory=tmp_path / "output2")
    assert len(calls) == 2 and second["network_requests"] == 0 and second["cache_hits"] == 2
    assert first["records"][0]["provenance"]["profit"]["first_observed_at"] == second["records"][0]["provenance"]["profit"]["first_observed_at"]


def test_expired_cache_refreshes_but_unchanged_version_keeps_first_seen(monkeypatch, tmp_path):
    calls = setup_worker(monkeypatch)
    collect(tmp_path)
    monkeypatch.setattr(finance, "_now", lambda: NOW + timedelta(hours=25))
    second = collect(tmp_path, output_directory=tmp_path / "output2")
    assert len(calls) == 4 and second["network_requests"] == 2 and second["cache_hits"] == 0
    assert second["records"][0]["provenance"]["profit"]["first_observed_at"] == NOW.isoformat()
    assert "缓存" in second["records"][0]["gaps"][0]


@pytest.mark.parametrize("change", ["hash", "identity", "future_time", "schema"])
def test_corrupt_cache_is_rejected_not_promoted(monkeypatch, tmp_path, change):
    calls = setup_worker(monkeypatch)
    collect(tmp_path)
    path = next((tmp_path / "cache/latest").glob("*profit*.json"))
    cached = json.loads(path.read_text(encoding="utf-8"))
    if change == "hash":
        cached["rows"][0]["netProfit"] = "999"
    elif change == "identity":
        cached["symbol"] = "sz.000001"
        cached = reseal(cached)
    elif change == "schema":
        cached["schema_version"] = "different"
        cached = reseal(cached)
    else:
        cached["observed_at"] = (NOW + timedelta(hours=1)).isoformat()
        cached = reseal(cached)
    path.write_text(json.dumps(cached), encoding="utf-8")
    second = collect(tmp_path, online=False, output_directory=tmp_path / "second")
    assert len(calls) == 2 and second["network_requests"] == 0
    assert second["records"][0]["profit"] is None and second["status"] == "partial"


def test_future_publication_excluded_and_previous_quarter_used(monkeypatch, tmp_path):
    def response(operation, symbol, year, quarter):
        item = row(operation, symbol, year, quarter)
        if quarter == 2:
            item["pubDate"] = "2026-09-23"
        return {"status": "ok", "fields": finance.FIELDS[operation], "rows": [item]}
    calls = setup_worker(monkeypatch, response)
    packet = collect(tmp_path)
    assert len(calls) == 4 and packet["status"] == "available"
    assert packet["records"][0]["profit"]["statDate"] == "2026-03-31"
    assert [r["status"] for r in packet["receipts"]][:2] == ["not_yet_published"] * 2


def test_newer_partial_data_does_not_mix_in_older_cash_flow(monkeypatch, tmp_path):
    def response(operation, symbol, year, quarter):
        return {"status": "ok", "fields": finance.FIELDS[operation],
                "rows": [] if operation == "cash_flow" and quarter == 2 else [row(operation, symbol, year, quarter)]}
    calls = setup_worker(monkeypatch, response)
    packet = collect(tmp_path)
    assert len(calls) == 2 and packet["status"] == "partial"
    assert packet["records"][0]["cash_flow"] is None


def test_empty_latest_quarter_uses_previous(monkeypatch, tmp_path):
    def response(operation, symbol, year, quarter):
        return {"status": "ok", "fields": finance.FIELDS[operation],
                "rows": [] if quarter == 2 else [row(operation, symbol, year, quarter)]}
    calls = setup_worker(monkeypatch, response)
    packet = collect(tmp_path)
    assert len(calls) == 4 and packet["records"][0]["profit"]["statDate"] == "2026-03-31"


@pytest.mark.parametrize("failure", ["timeout", "source_error", "schema_changed", "worker_failed"])
def test_source_failure_stops_all_following_network_requests(monkeypatch, tmp_path, failure):
    calls = setup_worker(monkeypatch, lambda *_: {"status": failure, "message": "password=SECRET"})
    packet = collect(tmp_path, candidates=CANDIDATES + [{"security_id": "sz-1", "symbol": "sz.000001"}])
    assert len(calls) == packet["network_requests"] == 1
    assert packet["status"] == "unavailable" and len(packet["records"]) == 2
    assert "SECRET" not in json.dumps(packet) and packet["receipts"][0]["status"] == failure


@pytest.mark.parametrize("field,value", [("netProfit", "nan"), ("netProfit", "Infinity"), ("netProfit", 3), ("netProfit", "password=SECRET"), ("code", "sz.000001"), ("statDate", "2026-05-31"), ("pubDate", "2026-01-01")])
def test_malformed_provider_rows_are_rejected_and_stop_source(monkeypatch, tmp_path, field, value):
    def response(operation, *args):
        item = row(operation)
        item[field] = value
        return {"status": "ok", "fields": finance.FIELDS[operation], "rows": [item]}
    calls = setup_worker(monkeypatch, response)
    packet = collect(tmp_path)
    assert len(calls) == 1 and packet["status"] == "unavailable"
    assert packet["records"][0]["profit"] is None and "SECRET" not in json.dumps(packet)


def test_negative_flags_do_not_assert_negative_operating_cash_flow(monkeypatch, tmp_path):
    def response(operation, symbol, year, quarter):
        item = row(operation, symbol, year, quarter)
        for field in ("netProfit", "epsTTM") if operation == "profit" else ("CFOToNP",):
            item[field] = "-1"
        return {"status": "ok", "fields": finance.FIELDS[operation], "rows": [item]}
    setup_worker(monkeypatch, response)
    flags = collect(tmp_path)["records"][0]["risk_flags"]
    assert flags == [finance.NEGATIVE_PROFIT, finance.NEGATIVE_EPS, finance.NEGATIVE_CASH_RATIO]
    assert "不能据此认定" in flags[-1]


def test_query_and_candidate_caps_are_real(monkeypatch, tmp_path):
    calls = setup_worker(monkeypatch)
    candidates = [{"security_id": f"id{i}", "symbol": f"sh.{600000+i}"} for i in range(8)]
    packet = collect(tmp_path, candidates=candidates, max_candidates=3, max_queries=1)
    assert len(calls) == packet["network_requests"] == 1
    assert len(packet["records"]) == 3 and packet["records"][0]["status"] == "partial"


def test_elapsed_budget_causes_no_requests(monkeypatch, tmp_path):
    calls = setup_worker(monkeypatch)
    ticks = iter([0, 5] + [5] * 20)
    monkeypatch.setattr(finance.time, "monotonic", lambda: next(ticks))
    packet = collect(tmp_path, max_seconds=1)
    assert calls == [] and packet["network_requests"] == 0 and packet["status"] == "unavailable"
    assert all(r["status"] == "runtime_limit" for r in packet["receipts"])


def test_empty_candidates_make_no_network_calls(monkeypatch, tmp_path):
    calls = setup_worker(monkeypatch)
    packet = collect(tmp_path, candidates=[])
    assert packet["status"] == "empty" and calls == [] and packet["records"] == []


@pytest.mark.parametrize("change", ["hash", "identity", "date", "cutoff", "risk", "future", "numeric", "period", "historical"])
def test_report_validator_rejects_tampering_even_if_resealed(monkeypatch, tmp_path, change):
    setup_worker(monkeypatch)
    packet = collect(tmp_path)
    record = packet["records"][0]
    if change == "hash":
        packet["network_requests"] += 1
    elif change == "identity":
        record["security_id"] = "other"
    elif change == "date":
        packet["target_date"] = "2026-09-20"
    elif change == "cutoff":
        packet["cutoff_at"] = "2026-09-21T22:00:00+08:00"
    elif change == "risk":
        record["risk_flags"] = ["guaranteed"]
    elif change == "future":
        record["profit"]["pubDate"] = "2026-09-23"
    elif change == "numeric":
        record["profit"]["netProfit"] = "nan"
    elif change == "period":
        record["cash_flow"]["statDate"] = "2026-03-31"
    else:
        record["provenance"]["profit"]["historical_reconstruction"] = False
    if change != "hash":
        packet = reseal(packet)
    with pytest.raises(ValueError):
        finance.validate_financial_review(packet, candidates=CANDIDATES, target_date=TARGET, cutoff_at=CUTOFF)


@pytest.mark.parametrize("kwargs", [{"max_seconds": float("nan")}, {"max_queries": 21}, {"max_candidates": 6}, {"timeout_seconds": 0}, {"cutoff_at": "2026-09-21T21:00:00"}, {"online": 1}])
def test_invalid_caller_configuration_fails_before_requests(monkeypatch, tmp_path, kwargs):
    calls = setup_worker(monkeypatch)
    with pytest.raises(ValueError):
        collect(tmp_path, **kwargs)
    assert calls == []


def test_worker_timeout_does_not_copy_exception_or_partial_output(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 0.5
        raise subprocess.TimeoutExpired("secret-command", 0.5, output="token=SECRET", stderr="password=SECRET")
    monkeypatch.setattr(finance.subprocess, "run", timeout)
    assert finance._run_query("profit", "sh.600000", 2026, 2, 0.5) == {"status": "timeout"}


def test_frozen_response_write_failure_keeps_packet_unavailable(monkeypatch, tmp_path):
    setup_worker(monkeypatch)
    write = finance._write_new
    def fail_response(path, payload):
        if "responses" in path.parts:
            raise OSError("cannot freeze")
        return write(path, payload)
    monkeypatch.setattr(finance, "_write_new", fail_response)
    packet = collect(tmp_path)
    assert packet["status"] == "unavailable"
    assert any("冻结失败" in gap for gap in packet["records"][0]["gaps"])
