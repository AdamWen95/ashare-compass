"""Synthetic native-index contracts; no real network and no research fixtures."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json

import pytest

from ashare_daily import sector_benchmark as benchmark
from ashare_daily.calendar import _digest
from ashare_daily.providers import baostock as bs
from test_sector_history import validation_config, validation_freeze

DATES = [(date(2026, 8, 21) + timedelta(days=n)).isoformat() for n in range(21)]
STAMP = "2026-09-11T18:00:00+08:00"


def index_response():
    params = dict(code="sh.000001", start_date=DATES[0], end_date=DATES[-1], security_type="index", adjustment_mode="unadjusted")
    rows = [dict(date=day, code="sh.000001", open="100", high="101", low="99", close=str(100+n/100),
        preclose="100", volume="100000", amount="10000000") for n, day in enumerate(DATES)]
    response = bs.base_result("history_f2", params)
    response.update(ok=True, status="ok", error_code="0", rows=rows, fields=bs.HISTORY_INDEX_FIELDS,
        raw_hash=bs.raw_hash(bs.HISTORY_INDEX_FIELDS, rows), fetched_at=STAMP,
        login=dict(ok=True, error_code="0", error_msg="success"), provenance_mode="offline_test", verification_kind="offline_test")
    return response


def test_index_window_uses_native_points_and_exact_21_dates():
    result = benchmark._series(index_response(), DATES, {"calendar": {day: True for day in DATES}}, mode="offline_test")
    assert result["symbol"] == "sh.000001" and result["security_type"] == "index"
    assert result["adjustment_mode"] == "index_native" and result["price_unit"] == "index_points"
    assert len(result["records"]) == 21 and result["expected_dates"] == DATES
    assert all(row["tradestatus"] is row["is_st"] is None for row in result["records"])


@pytest.mark.parametrize("mutation", [
    lambda r: r["parameters"].update(code="sh.000300"),
    lambda r: r["parameters"].update(security_type="stock"),
    lambda r: r["parameters"].update(adjustment_mode="forward_adjusted"),
    lambda r: r["rows"].pop(),
    lambda r: r["rows"].append(deepcopy(r["rows"][0])),
    lambda r: r["rows"][0].update(close="NaN"),
    lambda r: r["rows"][0].update(date="2026-08-20"),
    lambda r: r.update(fetched_at="2099-01-01T21:00:00+08:00"),
    lambda r: r["login"].update(ok=False),
])
def test_index_does_not_accept_other_benchmark_adjustment_or_bad_window(mutation):
    response = index_response()
    mutation(response)
    response["raw_hash"] = bs.raw_hash(response["fields"], response["rows"])
    with pytest.raises(ValueError):
        benchmark._series(response, DATES, {"calendar": {day: True for day in DATES}}, mode="offline_test")


def test_readonly_frozen_benchmark_revalidates_source_and_does_not_call_sdk(tmp_path, monkeypatch):
    cfg = validation_config()
    selection = validation_freeze(cfg)
    directory = tmp_path / cfg["output_directory"] / selection["selection_id"]
    calendar = {"verified": True, "calendar": {day: True for day in DATES}, "trading_dates": DATES}
    monkeypatch.setattr(benchmark, "_dates", lambda *a: (directory, calendar))
    monkeypatch.setattr(benchmark.BaoStockF2Client, "query", lambda *a, **k: pytest.fail("SDK called by read"))
    response = index_response()
    source = directory / "benchmark/source.json"
    signature = benchmark._write_new(source, response)
    series = benchmark._series(response, DATES, calendar, mode="offline_test")
    packet = benchmark._seal({"schema_version": "f3s-benchmark-v1", "selection_id": selection["selection_id"],
        "selection_hash": selection["content_hash"], "purpose": "engineering_validation", "production_eligible": False,
        "series": series, "source_reference": {"path": str(source), "sha256": signature}, "historical_reconstruction": True})
    benchmark._write_new(directory / "benchmark/benchmark_snapshot.json", packet)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    result = benchmark.read_benchmark(tmp_path, selection, cfg)
    assert result["network_requests"] == 0 and result["cache_replay"]
    assert result["records"] == series["records"] and result["production_eligible"] is False
    assert result["source_business_date"] is None and len(result["file_refs"]) == 2
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="source hash"):
        benchmark.read_benchmark(tmp_path, selection, cfg)


def test_offline_test_never_enables_online_benchmark(tmp_path, monkeypatch):
    cfg = validation_config()
    selection = validation_freeze(cfg)
    directory = tmp_path / cfg["output_directory"] / selection["selection_id"]
    monkeypatch.setattr(benchmark, "_dates", lambda *a: (directory, {"trading_dates": DATES}))
    monkeypatch.setattr(benchmark, "_permission", lambda *a: None)
    monkeypatch.setattr(benchmark.BaoStockF2Client, "query", lambda *a, **k: pytest.fail("SDK called"))
    with pytest.raises(ValueError, match="rejects offline_test"):
        benchmark.prepare_benchmark(tmp_path, selection, cfg, online=True)
