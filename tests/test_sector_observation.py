"""Daily observations remain deterministic and keep every frozen member visible."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ashare_daily import sector_observation as workflow
from ashare_daily.sector_eligibility import collect_sector_eligibility, evaluate_sector_eligibility
from ashare_daily.sector_screening import evaluate_selection
from ashare_daily.sector_selection import digest
from test_sector_screening import fixture, freeze_selection


STRATEGY = json.loads((Path(__file__).resolve().parents[1] / "config/sector_screening_f4s1.json").read_text(encoding="utf-8"))


def assessed(tmp_path, *, count=1, known_risk=True):
    selected, inputs = fixture(count=count, known_risk=known_risk)
    technical = evaluate_selection(selected, inputs, STRATEGY)
    facts = collect_sector_eligibility(tmp_path, selected, output_directory="evidence", online=False)
    qualification = evaluate_sector_eligibility(selected, technical, facts, cutoff_at=workflow._now())
    return selected, inputs, technical, qualification


def configure(tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/sector_screening_f4s1.json").write_text(json.dumps(STRATEGY), encoding="utf-8")


def test_observation_cap_preserves_all_members_and_original_ranking(tmp_path):
    selected, _, technical, qualification = assessed(tmp_path, count=23)
    result = workflow.combine_observations(selected, technical, qualification)
    assert len(result["evaluations"]) == result["counts"]["stock_count"] == 23
    assert len(result["observations"]) == result["counts"]["observation_count"] == 20
    assert result["counts"]["qualified_technical_pass_count"] == 23
    assert [row["observation_rank"] for row in result["observations"]] == list(range(1, 21))
    assert [row["symbol"] for row in result["observations"]] == sorted(row["symbol"] for row in result["observations"])
    assert all(row["company_review_status"] == "not_reviewed" and row["research_gaps"] for row in result["observations"])
    assert result["counts"]["formal_verified_opportunity_count"] == 0
    assert result["gaps"] == [] and result["counts"]["blocking_gap_count"] == 0
    assert technical["result_hash"] == digest({k: v for k, v in technical.items() if k != "result_hash"})


def test_missing_qualification_does_not_block_technical_list_or_pass_as_qualified(tmp_path):
    selected, _, technical, qualification = assessed(tmp_path, count=3, known_risk=False)
    result = workflow.combine_observations(selected, technical, qualification)
    assert result["status"] == "qualification_pending"
    assert not result["observations"] and len(result["pending"]) == 3
    assert result["counts"]["technical_pass_count"] == 3
    assert result["counts"]["eligibility_pending_count"] == 3
    assert all(row["qualification_gaps"] for row in result["pending"])
    assert len(result["gaps"]) == result["counts"]["blocking_gap_count"] == 3


def test_negative_qualification_cannot_enter_pending_or_observations(tmp_path):
    selected, _, technical, qualification = assessed(tmp_path)
    qualification["evaluations"][0]["eligibility_status"] = "fail"
    qualification["evaluations"][0]["exclusion_reasons"] = ["OFFLINE_TEST:ST"]
    qualification["content_hash"] = digest({k: v for k, v in qualification.items() if k != "content_hash"})
    result = workflow.combine_observations(selected, technical, qualification)
    assert not result["observations"] and not result["pending"]
    assert result["counts"]["eligibility_fail_count"] == 1
    assert result["evaluations"][0]["exclusion_reasons"] == ["OFFLINE_TEST:ST"]


def test_engineering_and_unverified_selections_are_rejected(tmp_path):
    selected, _ = fixture(purpose="engineering_validation")
    with pytest.raises(ValueError, match="verified production"):
        workflow.run_observation(tmp_path, selected, {}, output_directory="output", online=False)
    selected, _ = fixture()
    selected["selection_verified"] = False
    freeze_selection(selected)
    with pytest.raises(ValueError, match="verified production"):
        workflow.run_observation(tmp_path, selected, {}, output_directory="output", online=False)


def test_foreign_or_modified_inputs_cannot_be_joined(tmp_path):
    selected, _, technical, qualification = assessed(tmp_path)
    bad = deepcopy(qualification)
    bad["evaluations"][0]["eligibility_status"] = "fail"
    with pytest.raises(ValueError, match="binding mismatch"):
        workflow.combine_observations(selected, technical, bad)
    bad["content_hash"] = digest({k: v for k, v in bad.items() if k != "content_hash"})
    bad["technical_result_hash"] = "0" * 64
    bad["content_hash"] = digest({k: v for k, v in bad.items() if k != "content_hash"})
    with pytest.raises(ValueError, match="binding mismatch"):
        workflow.combine_observations(selected, technical, bad)


def test_empty_selection_never_invokes_fetchers_and_freezes_verifiable_files(tmp_path, monkeypatch):
    configure(tmp_path)
    selected, _ = fixture(count=0)
    def unexpected(*args, **kwargs):
        pytest.fail("empty production selection must not fetch history, benchmark or qualification sources")
    for name in ("prepare_history", "prepare_benchmark", "collect_eligibility"):
        monkeypatch.setattr(workflow, name, unexpected)
    result = workflow.run_observation(tmp_path, selected, {}, output_directory="output", online=False)
    assert result["status"] == "no_matching_sectors"
    assert result["network_requests"] == result["counts"]["stock_count"] == 0
    assert not result["observations"] and not result["pending"]
    assert result["production_eligible"] is False and result["mode"] == "offline_test"
    manifest = json.loads((Path(result["evidence_directory"]) / "manifest.json").read_text(encoding="utf-8"))
    import hashlib
    for name, checksum in manifest["files"].items():
        assert hashlib.sha256((Path(result["evidence_directory"]) / name).read_bytes()).hexdigest() == checksum


def test_offline_cached_history_can_produce_observation_without_company_materials(tmp_path, monkeypatch):
    configure(tmp_path)
    selected, inputs = fixture(known_risk=True)
    calls = []
    def prepare(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "ready", "metrics": {"network_requests": 0}}
    monkeypatch.setattr(workflow, "prepare_history", prepare)
    monkeypatch.setattr(workflow, "screening_history_inputs", lambda *a: {
        "calendar": inputs["calendar"], "securities": inputs["securities"], "file_refs": [], "issues": []})
    monkeypatch.setattr(workflow, "prepare_benchmark", lambda *a, **kw: inputs["benchmark"])
    result = workflow.run_observation(tmp_path, selected, {}, output_directory="output", online=False)
    assert calls == [{"max_seconds": 0}]
    assert result["status"] == "observations_ready" and len(result["observations"]) == 1
    assert result["cutoff_at"] > selected["cutoff_at"]
    assert result["historical_reconstruction"] is True
    assert result["counts"]["formal_verified_opportunity_count"] == result["network_requests"] == 0


def test_offline_test_cannot_write_research_or_call_network(tmp_path):
    selected, _ = fixture(count=0)
    with pytest.raises(ValueError, match="offline test data"):
        workflow.run_observation(tmp_path, selected, {}, output_directory="output", online=True)
    with pytest.raises(ValueError, match="production research artifacts"):
        workflow.run_observation(tmp_path, selected, {}, output_directory="outputs/research", online=False)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True, 14401])
def test_invalid_runtime_is_rejected_before_work(tmp_path, budget):
    selected, _ = fixture(count=0)
    with pytest.raises(ValueError, match="runtime budget"):
        workflow.run_observation(tmp_path, selected, {}, output_directory="output", online=False, max_seconds=budget)


def test_exchange_budget_exhaustion_keeps_unknown_without_late_requests(tmp_path, monkeypatch):
    from datetime import date, datetime
    from ashare_daily import qualification_sources
    ticks = iter([0, 2, 2, 2])
    monkeypatch.setattr(qualification_sources.time, "monotonic", lambda: next(ticks, 2))
    requested = []
    result = qualification_sources.collect_eligibility(
        config_path=Path(__file__).resolve().parents[1] / "config/eligibility_sources.json",
        target=date(2026, 9, 11), now=datetime.fromisoformat("2026-09-11T21:01:00+08:00"),
        output_dir=tmp_path / "offline", transport=lambda url: requested.append(url), max_seconds=1)
    assert not requested and result["status"] == "partial"
    assert all(row["status"] == "failed" and "预算" in row["reason"] for row in result["source_health"])
    bundle = json.loads(Path(result["bundle_file"]).read_text(encoding="utf-8"))
    assert bundle["records"] == []
