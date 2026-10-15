from pathlib import Path

import pytest


def test_industry_input_reads_current_master_separately_from_quote_date(tmp_path, monkeypatch):
    from ashare_daily import sector_pipeline as pipeline
    from types import SimpleNamespace
    seen = []
    monkeypatch.setattr(pipeline, "load_universe_config", lambda *a: SimpleNamespace(calendar_cache="cache"))
    monkeypatch.setattr(pipeline, "_permissions_for_inputs", lambda *a: None)
    def read(root, config, day, universe_config):
        seen.append(day)
        return {}
    monkeypatch.setattr(pipeline, "_read_snapshot", read)
    monkeypatch.setattr(pipeline, "_cached_calendar", lambda *a: {"calendar_verified": False})
    with pytest.raises(ValueError, match="calendar_unverified"):
        pipeline.collect_inputs(tmp_path, {"universe_config": "u", "market_scope": "sse_szse_a", "calendar_cache": "cache"},
            "2026-10-09", tmp_path / "sources", max_seconds=1, universe_date="2026-10-12")
    assert seen == ["2026-10-12"]


def test_market_benchmark_can_be_checked_when_no_industry_is_selected(tmp_path, monkeypatch):
    from ashare_daily import sector_benchmark as benchmark
    monkeypatch.setattr(benchmark, "_permission", lambda *a: None)
    monkeypatch.setattr(benchmark, "_dates", lambda *a: (tmp_path, {"trading_dates": ["2026-09-01", "2026-09-21"]}))
    result = benchmark.prepare_benchmark(tmp_path, {"members": [], "selection_id": "s", "content_hash": "h", "mode": "research"},
        {}, online=False, max_seconds=1, required_for_market=True)
    assert result["status"] == "benchmark_pending"
    assert result["network_requests"] == 0


def test_real_service_dry_run_never_loads_model_key():
    from ashare_daily.operations.daily import run_daily
    result = run_daily(project=Path(__file__).resolve().parents[1], config_path="config/premarket_daily.json", dry_run=True)
    assert result["status"] == "dry_run"
    assert result["external_calls"] == 0
    assert result["production_enabled"] is False
