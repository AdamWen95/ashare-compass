"""The benchmark must reuse verified daily calendars without fetching them."""
import json

import pytest

from ashare_daily import sector_benchmark as benchmark
from ashare_daily.calendar import _digest
from ashare_daily.providers import baostock
from test_history_calendar_cache_reuse import configured_windows
from test_sector_history import freeze


@pytest.mark.parametrize("operation", ["read_benchmark", "prepare_benchmark"])
def test_benchmark_accepts_target_day_only_in_universe_cache(tmp_path, monkeypatch, operation):
    cfg = configured_windows(tmp_path)
    selection = freeze(cfg)
    caches = list((tmp_path / "offline_test/f1_calendar").glob("*.json"))
    caches += list((tmp_path / "offline_test/f2_calendar").glob("*.json"))
    before = {path: path.read_bytes() for path in caches}
    monkeypatch.setattr(benchmark, "_permission", lambda root: None)
    monkeypatch.setattr(baostock.BaoStockClient, "query",
        lambda *a, **k: pytest.fail("benchmark must not fetch a calendar or index offline"))

    result = getattr(benchmark, operation)(tmp_path, selection, cfg)

    assert result["status"] == ("missing" if operation == "read_benchmark" else "benchmark_pending")
    assert result["records"] == [] and result["verified"] is False
    assert result["network_requests"] == 0
    if operation == "read_benchmark":
        assert len(result["expected_dates"]) == 21
        assert result["expected_dates"][-1] == "2026-09-10"
    else:
        plan = json.loads((tmp_path / result["run_directory"] / "plan.json").read_text(encoding="utf-8"))
        assert len(plan["expected_dates"]) == 21
        assert plan["expected_dates"][-1] == "2026-09-10"
    assert {path: path.read_bytes() for path in caches} == before


def test_benchmark_keeps_single_cache_configuration_working(tmp_path):
    cfg = configured_windows(tmp_path)
    cfg.pop("universe_config")
    (tmp_path / "offline_test/f1_calendar/latest.json").rename(
        tmp_path / "offline_test/f2_calendar/latest.json")
    selection = freeze(cfg)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}

    result = benchmark.read_benchmark(tmp_path, selection, cfg)

    assert result["status"] == "missing" and result["network_requests"] == 0
    assert len(result["expected_dates"]) == 21
    assert result["expected_dates"][-1] == "2026-09-10"
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.json")} == before


@pytest.mark.parametrize("invalid", ["missing", "hash", "mode", "conflict"])
def test_benchmark_rejects_untrusted_supplemental_calendar(tmp_path, invalid):
    cfg = configured_windows(tmp_path)
    selection = freeze(cfg)
    extra = tmp_path / "offline_test/f1_calendar/latest.json"
    if invalid == "missing":
        extra.unlink()
    else:
        packet = json.loads(extra.read_text(encoding="utf-8"))
        if invalid == "hash":
            packet["content_hash"] = "0" * 64
        else:
            if invalid == "mode":
                packet["mode"] = "research"
            else:
                row = packet["response"]["rows"][0]
                row["is_trading_day"] = "0" if row["is_trading_day"] == "1" else "1"
                packet["response"]["raw_hash"] = baostock.raw_hash(
                    packet["response"]["fields"], packet["response"]["rows"])
            packet["content_hash"] = _digest({k: v for k, v in packet.items() if k != "content_hash"})
        extra.write_text(json.dumps(packet), encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}

    with pytest.raises(ValueError, match="21 exact trusted dates"):
        benchmark.read_benchmark(tmp_path, selection, cfg)

    assert {path: path.read_bytes() for path in tmp_path.rglob("*.json")} == before
