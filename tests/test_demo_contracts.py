"""M0-specific contracts: synthetic dates, deterministic units and safe references."""

from datetime import date, datetime, timedelta, timezone

from pydantic import ValidationError
import pytest

from ashare_daily.demo import build_demo_report
from ashare_daily.schemas import ComputedMetric, DailyReport


def test_synthetic_report_is_explicit_and_round_trips(demo_report):
    assert demo_report.mode == "demo"
    assert "DEMO" in demo_report.title
    assert "人工合成" in demo_report.demo_notice
    assert demo_report.trade_date is None
    assert demo_report.model_id is None
    assert demo_report.prompt_version is None
    assert not demo_report.coverage.market_coverage_verified
    assert all(item.security_id.startswith("DEMO.") for item in demo_report.instruments)
    assert all(item.is_synthetic for item in demo_report.evidence)
    assert all(item.provider == "local_synthetic" for item in demo_report.source_health)
    assert DailyReport.model_validate_json(demo_report.model_dump_json()) == demo_report


@pytest.mark.parametrize("scenario", [date(2026, 9, 12), date(2026, 10, 1), date(2000, 1, 1), date(2099, 1, 1)])
def test_any_scenario_date_remains_unverified_and_does_not_backdate_evidence(scenario, generated_at):
    report = build_demo_report(scenario, generated_at=generated_at)
    assert report.scenario_date == scenario
    assert report.trade_date is None
    assert report.actual_generated_at == generated_at
    assert report.cutoff_at == generated_at
    for evidence in report.evidence:
        assert evidence.first_seen_at == generated_at
        assert evidence.fetched_at == generated_at
        assert evidence.published_at is None


@pytest.mark.parametrize("invalid", ["2026-09-09", datetime(2026, 9, 9), None])
def test_builder_requires_date_object(invalid):
    with pytest.raises(ValueError, match="scenario_date"):
        build_demo_report(invalid)


@pytest.mark.parametrize("invalid", ["20260909", "2026-9-9", "2026-09-09T00:00:00", datetime(2026, 9, 9), 1788902400])
def test_report_schema_also_rejects_non_date_inputs(demo_report, invalid):
    data = demo_report.model_dump()
    data["scenario_date"] = invalid
    with pytest.raises(ValidationError):
        DailyReport.model_validate(data)


def test_utc_clock_is_normalized_to_shanghai(scenario_date):
    instant = datetime(2026, 9, 9, 16, 30, tzinfo=timezone.utc)
    report = build_demo_report(scenario_date, generated_at=instant)
    assert report.actual_generated_at.isoformat() == "2026-09-10T00:30:00+08:00"
    timestamps = [report.actual_generated_at, report.cutoff_at]
    timestamps.extend(source.fetched_at for source in report.source_health)
    timestamps.extend(stamp for evidence in report.evidence for stamp in (evidence.first_seen_at, evidence.fetched_at))
    assert all(stamp.tzinfo.key == "Asia/Shanghai" for stamp in timestamps)


def test_naive_time_and_inverted_evidence_timeline_are_rejected(demo_report, scenario_date):
    with pytest.raises(ValueError, match="时区"):
        build_demo_report(scenario_date, generated_at=datetime(2026, 9, 9, 21))
    data = demo_report.model_dump()
    data["actual_generated_at"] = datetime(2026, 9, 10, 9)
    with pytest.raises(ValidationError, match="时区"):
        DailyReport.model_validate(data)
    data = demo_report.model_dump()
    data["evidence"][0]["first_seen_at"] += timedelta(seconds=1)
    with pytest.raises(ValidationError, match="首次观察"):
        DailyReport.model_validate(data)


def test_evidence_after_cutoff_is_rejected(demo_report):
    data = demo_report.model_dump()
    data["evidence"][0]["fetched_at"] += timedelta(seconds=1)
    with pytest.raises(ValidationError, match="截点"):
        DailyReport.model_validate(data)


def test_known_fixture_units_are_deterministic(demo_report):
    metrics = {metric.metric_id: metric for metric in demo_report.metrics}
    # Hand-verifiable synthetic arithmetic: (10.5 / 10) - 1 = 0.05, not 5.
    ratio = metrics["M-DEMO.SH.001-daily_return_ratio"]
    assert ratio.unit == "ratio"
    assert ratio.value == pytest.approx(0.05)
    assert metrics["M-DEMO.SZ.002-daily_return_ratio"].value == pytest.approx(-0.02)
    assert metrics["M-DEMO.SH.003-daily_return_ratio"].value == 0
    assert metrics["M-DEMO.SH.001-volume_shares"].unit == "shares"
    assert metrics["M-DEMO.SH.001-volume_shares"].value == 1_000_000
    assert metrics["M-DEMO.SH.001-amount_cny"].unit == "CNY"
    assert metrics["M-DEMO.SH.001-amount_cny"].value == 10_500_000
    assert metrics["M-MARKET-amount_cny"].value == 36_650_000
    assert metrics["M-MARKET-volume_shares"].value == 3_000_000
    assert [metrics[f"M-MARKET-{key}"].value for key in ("advancers", "decliners", "unchanged")] == [2, 1, 1]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_invalid_numeric_values_are_rejected(demo_report, invalid):
    data = demo_report.metrics[0].model_dump()
    data["value"] = invalid
    with pytest.raises(ValidationError):
        ComputedMetric.model_validate(data)


def test_null_metric_is_preserved_instead_of_becoming_zero(demo_report):
    data = demo_report.metrics[0].model_dump()
    data["value"] = None
    assert ComputedMetric.model_validate(data).value is None


def test_identical_inputs_are_reproducible(scenario_date, generated_at):
    assert build_demo_report(scenario_date, generated_at=generated_at) == build_demo_report(scenario_date, generated_at=generated_at)


def test_zero_candidates_is_a_valid_report(scenario_date, generated_at):
    report = build_demo_report(scenario_date, generated_at=generated_at, empty_candidates=True)
    assert report.candidates == []
    assert report.status == "complete_within_scope"
    assert report.evidence and report.market_review and report.important_news
    assert DailyReport.model_validate_json(report.model_dump_json()).candidates == []


@pytest.mark.parametrize("field,value", [("evidence_ids", ["E-DOES-NOT-EXIST"]), ("metric_ids", ["M-DOES-NOT-EXIST"])])
@pytest.mark.parametrize("location", ["claim", "candidate"])
def test_missing_references_are_rejected(demo_report, field, value, location):
    data = demo_report.model_dump()
    target = data["candidates"][0] if location == "candidate" else data["market_review"][0]
    target[field] = value
    with pytest.raises(ValidationError, match="不存在"):
        DailyReport.model_validate(data)


def test_missing_metric_evidence_is_rejected(demo_report):
    data = demo_report.model_dump()
    data["metrics"][0]["evidence_ids"] = ["E-DOES-NOT-EXIST"]
    with pytest.raises(ValidationError, match="不存在"):
        DailyReport.model_validate(data)


def test_fact_requires_evidence(demo_report):
    data = demo_report.model_dump()
    data["market_review"][0]["evidence_ids"] = []
    with pytest.raises(ValidationError, match="事实观点"):
        DailyReport.model_validate(data)


def test_cross_security_metric_reference_is_rejected(demo_report):
    data = demo_report.model_dump()
    assert data["candidates"][0]["security_id"] == "DEMO.SH.001"
    data["candidates"][0]["metric_ids"] = ["M-DEMO.SZ.002-close"]
    with pytest.raises(ValidationError, match="证券关联不符"):
        DailyReport.model_validate(data)


def test_frozen_evidence_hash_must_match_content(demo_report):
    data = demo_report.model_dump()
    data["evidence"][0]["frozen_content"] += "changed after publication"
    with pytest.raises(ValidationError, match="raw_hash"):
        DailyReport.model_validate(data)


@pytest.mark.parametrize("forbidden", ["positions", "cash_balance", "buy_quantity", "position_weight", "sell_strategy", "orders", "fills"])
@pytest.mark.parametrize("location", ["report", "candidate", "claim"])
def test_extra_trading_fields_are_rejected(demo_report, forbidden, location):
    data = demo_report.model_dump()
    target = data if location == "report" else data["candidates"][0]
    if location == "claim":
        target = target["claims"][0]
    target[forbidden] = "not allowed"
    with pytest.raises(ValidationError) as error:
        DailyReport.model_validate(data)
    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())
