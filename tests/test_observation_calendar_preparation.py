"""Online cold start may obtain calendar evidence within a strict query budget."""
import pytest

from ashare_daily.sector_observation import prepare_observation_calendar
from ashare_daily.providers import baostock, baostock_f2, sector_status


def test_calendar_preparation_caps_queries_and_single_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(sector_status, "_permission", lambda root: None)
    calls = []
    def query(client, operation, **parameters):
        assert client.max_attempts == 1
        assert 0 < client.timeout_seconds <= 15
        calls.append(parameters)
        return {"ok": True}
    monkeypatch.setattr(baostock.BaoStockClient, "query", query)
    def resolve(target, cache, **kwargs):
        assert kwargs["history_days"] == 320 and kwargs["mode"] == "research"
        client = kwargs["client"]
        client.query("calendar", start_date="2025-09-22", end_date="2026-09-21")
        client.query("calendar", start_date="2024-09-21", end_date="2025-09-21")
        with pytest.raises(ValueError, match="budget"):
            client.query("calendar", start_date="2023-09-21", end_date="2024-09-20")
        return {"verified": True}
    monkeypatch.setattr(baostock_f2, "resolve_history_calendar", resolve)
    result = prepare_observation_calendar(tmp_path, {"target_date": "2026-09-21"},
        {"calendar_cache": "cache"}, max_seconds=30)
    assert result["network_requests"] == len(calls) == 2


def test_existing_calendar_reuse_makes_no_query(tmp_path, monkeypatch):
    monkeypatch.setattr(sector_status, "_permission", lambda root: None)
    monkeypatch.setattr(baostock_f2, "resolve_history_calendar", lambda *a, **k: {"verified": True})
    monkeypatch.setattr(baostock.BaoStockClient, "query", lambda *a, **k: pytest.fail("cache hit must not query"))
    result = prepare_observation_calendar(tmp_path, {"target_date": "2026-09-21"},
        {"calendar_cache": "cache"}, max_seconds=30)
    assert result["network_requests"] == 0


def test_calendar_permission_checked_before_source(tmp_path, monkeypatch):
    def denied(root):
        raise ValueError("status_source_permission_required")
    monkeypatch.setattr(sector_status, "_permission", denied)
    monkeypatch.setattr(baostock_f2, "resolve_history_calendar", lambda *a, **k: pytest.fail("permission denied"))
    with pytest.raises(ValueError, match="permission"):
        prepare_observation_calendar(tmp_path, {"target_date": "2026-09-21"}, {"calendar_cache": "cache"}, max_seconds=30)
