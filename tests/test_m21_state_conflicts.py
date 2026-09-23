"""Independent hand-built offline status conflicts, with no provider calls."""

from copy import deepcopy
from datetime import date, timedelta

import pytest

from ashare_daily.screening.engine import digest
from ashare_daily.screening.m21 import M21Config, evaluate_m21


@pytest.fixture
def conflict_snapshot():
    # These dates are an explicit artificial fixture calendar, not a claimed
    # trading calendar. Numeric values are 1..120 so trend/return are hand-checkable.
    dates = [(date(2026, 1, 1) + timedelta(days=offset)).isoformat() for offset in range(120)]
    symbols = {"sh.600000": "stock", "sh.000001": "index"}
    instruments, raw, series = [], [], {}
    for symbol, kind in symbols.items():
        instruments.append({"symbol": symbol, "name": "人工状态冲突股票" if kind == "stock" else "人工恒值基准", "security_type": kind,
                            "board": "mainboard" if kind == "stock" else "index", "exchange": "SH", "ipo_date": "2000-01-01", "out_date": None, "status": "listed"})
        bars = []
        for index, day in enumerate(dates, start=1):
            close = str(index if kind == "stock" else 100)
            bar = {"symbol": symbol, "trade_date": day, "close": close, "preclose": close, "amount_cny": "50000000", "volume_shares": 100,
                   "tradestatus": True if kind == "stock" else None, "is_st": False if kind == "stock" else None,
                   "adjustment_mode": "unadjusted", "price_unit": "CNY" if kind == "stock" else "index_points", "amount_unit": "CNY", "quality_flags": []}
            raw.append(bar)
            bars.append(deepcopy(bar))
        series[symbol] = {"provider": "baostock", "adjustment_mode": "forward_adjusted" if kind == "stock" else "index_native",
                          "fetch_version": "offline-state-fixture", "price_unit": "CNY" if kind == "stock" else "index_points", "issues": [], "bars": bars}
    snapshot = {"verification_kind": "offline_test", "trade_date": dates[-1], "trading_dates": dates, "target_is_trading": True,
                "strategy_config": M21Config(strategy_version="trend_research_v1.0.0", benchmark_id="sh.000001", benchmark_name="人工恒值基准").model_dump(mode="json"), "instruments": instruments, "sample_types": symbols,
                "raw_bars": raw, "adjusted_data": {"series": series}, "source_issues": {}, "calendar_issues": [],
                "eligibility_states": {"sh.600000": {"delisting_period": None, "status": "unknown", "reason": "人工独立资格证据缺失"}}}
    snapshot["snapshot_id"] = "offline-state-fixture-" + digest(snapshot)
    return snapshot


def _row(snapshot):
    return evaluate_m21(snapshot)["evaluations"][0]


def _raw_today(snapshot):
    return next(bar for bar in snapshot["raw_bars"] if bar["symbol"] == "sh.600000" and bar["trade_date"] == snapshot["trade_date"])


@pytest.mark.parametrize("field,raw_value,adjusted_value,condition_id", [
    ("is_st", True, False, "not_st"), ("is_st", False, True, "not_st"),
    ("tradestatus", False, True, "not_suspended"), ("tradestatus", True, False, "not_suspended"),
])
def test_direct_frozen_state_conflict_cannot_claim_qualification_failure(conflict_snapshot, field, raw_value, adjusted_value, condition_id):
    _raw_today(conflict_snapshot)[field] = raw_value
    conflict_snapshot["adjusted_data"]["series"]["sh.600000"]["bars"][-1][field] = adjusted_value
    row = _row(conflict_snapshot)
    condition = next(item for item in row["conditions"] if item["id"] == condition_id)
    assert condition["status"] == "unknown"
    assert "不一致" in condition["reason"]
    assert row["eligibility_status"] == "pending"
    assert row["status"] == "data_insufficient"
    assert row["data_issues"]
    assert not row["exclusion_reasons"]


@pytest.mark.parametrize("field,condition_id", [("is_st", "not_st"), ("tradestatus", "not_suspended")])
def test_freeze_source_issue_is_kept_verbatim_and_prevents_false_known_state(conflict_snapshot, field, condition_id):
    reason = f"{conflict_snapshot['trade_date']} 未复权/调整响应 {field} 不一致，需重新核对来源版本"
    conflict_snapshot["source_issues"]["sh.600000"] = [reason]
    row = _row(conflict_snapshot)
    assert row["technical_screen_status"] == "not_computable"
    assert row["eligibility_status"] == "pending"
    assert row["status"] == "data_insufficient"
    assert next(item for item in row["eligibility_conditions"] if item["id"] == condition_id)["reason"] == reason


@pytest.mark.parametrize("independent_failure", ["identity", "delisting"])
def test_independent_qualification_failure_still_excludes_with_state_conflict(conflict_snapshot, independent_failure):
    _raw_today(conflict_snapshot)["is_st"] = True
    if independent_failure == "identity":
        next(item for item in conflict_snapshot["instruments"] if item["symbol"] == "sh.600000")["board"] = "other"
        failed_id = "identity"
    else:
        conflict_snapshot["eligibility_states"]["sh.600000"].update(delisting_period=True, status="true", effective_date=conflict_snapshot["trade_date"],
                                                                        evidence_id="offline-explicit-positive-evidence", reason="人工独立证据确认退市整理期")
        failed_id = "not_delisting_period"
    row = _row(conflict_snapshot)
    conditions = {item["id"]: item for item in row["conditions"]}
    assert conditions["not_st"]["status"] == "unknown"
    assert conditions[failed_id]["status"] == "fail"
    assert row["eligibility_status"] == "fail" and row["status"] == "excluded"
    assert row["data_issues"]
    report = evaluate_m21(conflict_snapshot)
    assert report["counts"]["eligibility_conclusion_count"] == 1
    assert report["counts"]["eligibility_verified_count"] == 0
    assert report["counts"]["stocks_with_data_gaps"] == 1


def test_missing_adjusted_series_keeps_known_raw_state_and_existing_technical_gap(conflict_snapshot):
    del conflict_snapshot["adjusted_data"]["series"]["sh.600000"]
    row = _row(conflict_snapshot)
    states = {item["id"]: item["status"] for item in row["conditions"]}
    assert states["not_st"] == "pass" and states["not_suspended"] == "pass"
    assert row["technical_screen_status"] == "not_computable" and row["eligibility_status"] == "pending"
    assert row["status"] == "data_insufficient"


def test_consistent_known_st_exclusion_is_preserved(conflict_snapshot):
    _raw_today(conflict_snapshot)["is_st"] = True
    conflict_snapshot["adjusted_data"]["series"]["sh.600000"]["bars"][-1]["is_st"] = True
    row = _row(conflict_snapshot)
    assert next(item for item in row["conditions"] if item["id"] == "not_st")["status"] == "fail"
    assert row["eligibility_status"] == "fail" and row["status"] == "excluded"


def test_prior_date_state_conflict_does_not_fabricate_conflict_on_analysis_day(conflict_snapshot):
    conflict_snapshot["source_issues"]["sh.600000"] = [f"{conflict_snapshot['trading_dates'][0]} 未复权/调整响应 is_st 不一致，需重新核对来源版本"]
    row = _row(conflict_snapshot)
    assert row["technical_screen_status"] == "not_computable"
    assert next(item for item in row["conditions"] if item["id"] == "not_st")["status"] == "pass"
    assert row["status"] == "data_insufficient"


def test_state_conflict_evaluation_does_not_mutate_snapshot(conflict_snapshot):
    _raw_today(conflict_snapshot)["is_st"] = True
    before = deepcopy(conflict_snapshot)
    first = evaluate_m21(conflict_snapshot)
    assert evaluate_m21(conflict_snapshot)["result_hash"] == first["result_hash"]
    assert conflict_snapshot == before
