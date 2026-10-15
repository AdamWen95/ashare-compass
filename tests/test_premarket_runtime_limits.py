"""Elapsed-time budgets are bounded independently of model/request quotas."""
from datetime import datetime
import json

import pytest


def test_fifteen_minute_discovery_cap_and_remaining_window():
    from ashare_daily.premarket.timing import universe_budget_seconds
    assert universe_budget_seconds(1200) == 900
    assert universe_budget_seconds(250) == 250
    assert universe_budget_seconds(900, reserve=20) == 880
    assert universe_budget_seconds(15, reserve=20) == 0


@pytest.mark.parametrize("maximum", [True, 0, -1, 901, 900.5])
def test_universe_time_limit_rejects_invalid_or_unbounded_values(maximum):
    from ashare_daily.premarket.timing import universe_budget_seconds
    with pytest.raises(ValueError): universe_budget_seconds(1200, maximum)


def test_daily_adapter_passes_configured_budget_without_changing_total_limit(tmp_path, monkeypatch):
    from ashare_daily.premarket import services_market
    from ashare_daily.premarket.services import PremarketServices
    captured = {}
    def collect(*args, **kwargs):
        captured.update(kwargs)
        assert args[-2] == 1200
        return {"status": "fixture"}
    monkeypatch.setattr(services_market, "collect_market", collect)
    assert PremarketServices(tmp_path, object(), {"universe_max_seconds": 900}).market({}, tmp_path, 1200, "r") == {"status": "fixture"}
    assert captured["universe_max_seconds"] == 900


def test_policy_rejects_discovery_budget_above_market_phase(tmp_path):
    from test_premarket_daily import setup
    from ashare_daily.operations.premarket import load_policy
    cfg = setup(tmp_path)
    path = tmp_path / cfg.premarket_config
    policy = json.loads(path.read_text())
    policy.update(market_max_seconds=600, universe_max_seconds=900)
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="policy"):
        load_policy(tmp_path, cfg)


def test_preflight_expanded_budget_does_not_move_morning_cutoff():
    from test_premarket_preflight import CALENDAR
    from ashare_daily.premarket.preflight import preflight_plan
    from ashare_daily.premarket.timing import universe_budget_seconds
    now = datetime.fromisoformat("2026-10-15T08:45:00+08:00")
    plan = preflight_plan(now, CALENDAR)
    left = (datetime.fromisoformat(plan["expires_at"])-now).total_seconds()
    assert plan["expires_at"] == "2026-10-15T08:50:00+08:00"
    assert universe_budget_seconds(left, reserve=20) == 280
