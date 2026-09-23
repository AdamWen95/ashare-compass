"""Explicit isolated fixtures for the optional daily delisting-period check."""
import json
from pathlib import Path

import pytest

from ashare_daily import sector_observation as workflow
from ashare_daily.operations import observation as daily
from ashare_daily.sector_eligibility import collect_sector_eligibility, evaluate_sector_eligibility
from ashare_daily.sector_screening import evaluate_selection
from ashare_daily.sector_selection import digest
from test_sector_observation import STRATEGY, configure
from test_sector_screening import fixture, freeze_input, freeze_selection


def selected_inputs(delisting=None):
    selection, inputs = fixture(known_risk=True)
    state = selection["members"][0]["statuses"]["delisting_period"]
    state["value"] = delisting
    freeze_selection(selection)
    freeze_input(inputs, selection)
    return selection, inputs


def qualification(tmp_path, delisting=None):
    selection, inputs = selected_inputs(delisting)
    technical = evaluate_selection(selection, inputs, STRATEGY)
    facts = collect_sector_eligibility(tmp_path, selection, output_directory="facts", online=False)
    return selection, technical, facts


@pytest.mark.parametrize("delisting,evidence_status", [(None, "unknown"), (True, "fail"), (False, "pass")])
def test_optional_delisting_never_blocks_or_manufactures_evidence(tmp_path, delisting, evidence_status):
    selection, technical, facts = qualification(tmp_path, delisting)
    cutoff = workflow._now()
    original = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=cutoff)
    revised = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=cutoff, require_delisting_check=False)
    before, after = original["evaluations"][0], revised["evaluations"][0]
    assert before["eligibility_status"] == {None: "pending", True: "fail", False: "pass"}[delisting]
    assert after["eligibility_status"] == "pass"
    assert after["facts"] == before["facts"]
    condition = next(c for c in after["conditions"] if c["id"] == "not_delisting_period")
    assert condition["required"] is False and condition["status"] == "not_required"
    assert condition["evidence_status"] == evidence_status
    assert not after["gaps"] and not after["exclusion_reasons"]
    assert revised["required_fields"] == ["identity", "listed", "st", "suspended"]
    assert revised["eligibility_policy"] == {"require_delisting_check": False}
    assert original["content_hash"] != revised["content_hash"]
    joined = workflow.combine_observations(selection, technical, revised)
    assert joined["counts"]["observation_count"] == 1
    assert not joined["pending"] and not joined["counts"]["blocking_gap_count"]


@pytest.mark.parametrize("field,value,expected", [("st", True, "fail"), ("st", None, "pending"),
    ("suspended", True, "fail"), ("suspended", None, "pending")])
def test_other_risk_requirements_still_block(tmp_path, field, value, expected):
    selection, technical, facts = qualification(tmp_path)
    row = technical["evaluations"][0]
    row["risk_states"][field] = value
    if value is None:
        row["risk_evidence"][field] = []
    technical["result_hash"] = digest({k: v for k, v in technical.items() if k != "result_hash"})
    result = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=workflow._now(), require_delisting_check=False)
    assert result["evaluations"][0]["eligibility_status"] == expected
    assert not workflow.combine_observations(selection, technical, result)["observations"]


@pytest.mark.parametrize("condition_id", ["identity", "listed"])
@pytest.mark.parametrize("status,expected", [("fail", "fail"), ("unknown", "pending")])
def test_identity_and_listing_still_required(tmp_path, condition_id, status, expected):
    selection, technical, facts = qualification(tmp_path)
    condition = next(c for c in technical["evaluations"][0]["eligibility_conditions"] if c["id"] == condition_id)
    condition["status"] = status
    technical["result_hash"] = digest({k: v for k, v in technical.items() if k != "result_hash"})
    result = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=workflow._now(), require_delisting_check=False)
    assert result["evaluations"][0]["eligibility_status"] == expected


def test_technical_failure_is_not_promoted_by_delisting_policy(tmp_path):
    selection, technical, facts = qualification(tmp_path)
    technical["evaluations"][0]["technical_status"] = "fail"
    technical["result_hash"] = digest({k: v for k, v in technical.items() if k != "result_hash"})
    result = evaluate_sector_eligibility(selection, technical, facts, cutoff_at=workflow._now(), require_delisting_check=False)
    assert result["evaluations"][0]["eligibility_status"] == "pass"
    assert not workflow.combine_observations(selection, technical, result)["observations"]


def test_daily_disabled_policy_skips_exchange_request_and_freezes_policy(tmp_path, monkeypatch):
    configure(tmp_path)
    selection, inputs = selected_inputs()
    # This isolated test intercepts every I/O adapter before enabling the online branch.
    selection["mode"] = "research"
    freeze_selection(selection)
    monkeypatch.setattr(workflow, "prepare_observation_calendar", lambda *a, **k: {"network_requests": 0})
    monkeypatch.setattr(workflow, "prepare_history", lambda *a, **k: {"status": "ready", "metrics": {"network_requests": 0}})
    monkeypatch.setattr(workflow, "screening_history_inputs", lambda *a: {
        "calendar": inputs["calendar"], "securities": inputs["securities"], "file_refs": [], "issues": []})
    monkeypatch.setattr(workflow, "prepare_benchmark", lambda *a, **k: inputs["benchmark"])
    def forbidden(*args, **kwargs):
        pytest.fail("disabled delisting check must never request an exchange list")
    monkeypatch.setattr(workflow, "collect_eligibility", forbidden)
    def local_facts(*args, **kwargs):
        kwargs["online"] = False
        return collect_sector_eligibility(*args, **kwargs)
    monkeypatch.setattr(workflow, "collect_sector_eligibility", local_facts)
    result = workflow.run_observation(tmp_path, selection, {}, output_directory="isolated", online=True,
        require_delisting_check=False)
    assert result["qualification_probe_security_ids"]
    assert result["counts"]["observation_count"] == 1
    assert result["exchange_eligibility"]["status"] == "not_required_by_policy"
    assert result["eligibility_policy"] == result["eligibility"]["eligibility_policy"] == {"require_delisting_check": False}
    assert result["network_requests"] == 0
    assert result["content_hash"] == digest({k: v for k, v in result.items() if k != "content_hash"})
    saved = json.loads((Path(result["evidence_directory"]) / "observation.json").read_text(encoding="utf-8"))
    assert saved == result


def test_config_missing_policy_keeps_legacy_default_and_rejects_non_boolean(tmp_path):
    root = Path(__file__).resolve().parents[1]
    configure(tmp_path)
    options = json.loads((root / "config/sector_observation.json").read_text(encoding="utf-8"))
    assert options["require_delisting_check"] is False
    path = tmp_path / "config/options.json"
    options.pop("require_delisting_check")
    path.write_text(json.dumps(options), encoding="utf-8")
    assert daily.load_options(tmp_path, "config/options.json")["require_delisting_check"] is True
    for bad in (0, 1, "false", None):
        options["require_delisting_check"] = bad
        path.write_text(json.dumps(options), encoding="utf-8")
        with pytest.raises(ValueError, match="delisting_policy"):
            daily.load_options(tmp_path, "config/options.json")


def test_daily_dispatch_passes_configured_policy(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(workflow, "run_observation", lambda *a, **kwargs: captured.update(kwargs))
    daily._observe(tmp_path, {}, {}, "isolated", 10, False, "strategy.json", "eligibility.json")
    assert captured["require_delisting_check"] is False


@pytest.mark.parametrize("bad", [0, "false", None])
def test_evaluator_rejects_non_boolean_policy_before_processing(tmp_path, bad):
    with pytest.raises(ValueError, match="policy must be boolean"):
        evaluate_sector_eligibility({}, {}, {}, cutoff_at="unused", require_delisting_check=bad)
    with pytest.raises(ValueError, match="policy must be boolean"):
        workflow.run_observation(tmp_path, {}, {}, output_directory="unused", require_delisting_check=bad)
