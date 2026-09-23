"""Reference scores retain their frozen source/qualification boundaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from ashare_daily.reference_strategy import (
    build_reference_review, load_reference_inputs, validated_panel, validate_reference_review,
)
from ashare_daily.sector_observation import combine_observations
from ashare_daily.sector_screening import evaluate_selection
from ashare_daily.sector_selection import digest
from test_sector_observation import assessed, STRATEGY
from test_sector_screening import freeze_input, freeze_selection


def seal(value, key="content_hash"):
    value[key] = digest({k: v for k, v in value.items() if k != key})
    return value


def observed(selection, inputs, eligibility):
    technical = evaluate_selection(selection, inputs, STRATEGY)
    eligibility["technical_result_hash"] = technical["result_hash"]
    seal(eligibility)
    return seal({"schema_version": "sector-observation-v1", "mode": selection["mode"],
        "purpose": selection.get("purpose", "production"), "production_eligible": selection["mode"] == "research",
        "selection_id": selection["selection_id"], "selection_content_hash": selection["content_hash"],
        "target_date": selection["target_date"], "source_cutoff_at": selection["cutoff_at"],
        "cutoff_at": eligibility["cutoff_at"], "historical_reconstruction": eligibility["historical_reconstruction"],
        "technical": technical, "eligibility": eligibility,
        **combine_observations(selection, technical, eligibility)})


def sample(tmp_path, *, count=2, known_risk=True):
    selection, inputs, _, eligibility = assessed(tmp_path, count=count, known_risk=known_risk)
    return selection, inputs, observed(selection, inputs, eligibility)


def validate(packet, observation):
    return validate_reference_review(packet, evaluations=observation["evaluations"],
        trade_date=observation["target_date"], source_observation_hash=observation["content_hash"])


def freeze(directory, selection, inputs, observation):
    directory.mkdir()
    files = {}
    for name, value in (("selection.json", selection), ("screening_inputs.json", inputs), ("observation.json", observation)):
        body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        (directory / name).write_bytes(body)
        files[name] = hashlib.sha256(body).hexdigest()
    manifest = seal({"schema_version": "sector-observation-manifest-v1", "selection_id": selection["selection_id"],
        "selection_content_hash": selection["content_hash"], "mode": selection["mode"], "purpose": selection.get("purpose", "production"),
        "files": files, "observation_content_hash": observation["content_hash"]})
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_source_bound_build_is_pure_and_preserves_all_ranks_and_denominators(tmp_path):
    args = sample(tmp_path)
    before = deepcopy(args)
    packet = build_reference_review(*args)
    assert args == before
    assert packet["application"] == "shadow_only" and packet["changes_candidate_ranking"] is False
    assert packet["network_requests"] == packet["model_calls"] == 0
    assert packet["source_input_hash"] == args[1]["input_hash"]
    assert packet["source_observation_hash"] == args[2]["content_hash"]
    assert len(packet["records"]) == 2
    assert all(row["status"] == "available" for row in packet["records"])
    assert packet == build_reference_review(*args)
    validate(packet, args[2])


def test_qualification_pending_remains_pending_even_with_available_scores(tmp_path):
    args = sample(tmp_path, known_risk=False)
    packet = build_reference_review(*args)
    assert not args[2]["observations"] and len(args[2]["pending"]) == 2
    assert all(row["status"] == "available" and row["eligibility_status"] == "pending" for row in packet["records"])
    assert not args[2]["observations"]


def test_empty_selection_has_no_fabricated_record_or_external_work(tmp_path):
    args = sample(tmp_path, count=0)
    packet = build_reference_review(*args)
    assert packet["records"] == [] and packet["network_requests"] == packet["model_calls"] == 0


def test_legacy_selection_with_implicit_production_stays_compatible(tmp_path):
    selection, inputs, observation = sample(tmp_path)
    del selection["purpose"]
    freeze_selection(selection)
    freeze_input(inputs, selection)
    eligibility = observation["eligibility"]
    eligibility.update(selection_id=selection["selection_id"], selection_content_hash=selection["content_hash"])
    observation = observed(selection, inputs, eligibility)
    directory = tmp_path / "legacy"
    freeze(directory, selection, inputs, observation)
    args = load_reference_inputs(directory)
    assert all(row["status"] == "available" for row in build_reference_review(*args)["records"])


@pytest.mark.parametrize("damage", ["raw_hash", "raw_volume", "mixed_adjustment", "missing_day", "future_seen"])
def test_corrupt_or_incomplete_source_version_never_produces_a_score(tmp_path, damage):
    selection, inputs, observation = sample(tmp_path)
    source = inputs["securities"]["security-0"]
    if damage == "raw_hash":
        source["raw_records"][-1]["close"] = "9999"
    elif damage == "raw_volume":
        source["raw_records"][-1]["volume_shares"] = True
    elif damage == "mixed_adjustment":
        source["adjustment_window"]["raw_fact_hashes"][source["expected_dates"][-1]] = "0" * 64
    elif damage == "missing_day":
        source["raw_records"].pop(50)
    else:
        source["raw_records"][-1]["first_seen_at"] = "2099-01-01T00:00:00+08:00"
    freeze_input(inputs, selection)
    observation = observed(selection, inputs, observation["eligibility"])
    packet = build_reference_review(selection, inputs, observation)
    damaged = next(row for row in packet["records"] if row["security_id"] == "security-0")
    healthy = next(row for row in packet["records"] if row["security_id"] == "security-1")
    assert damaged["status"] == "unavailable" and damaged["score"] is None and damaged["issues"]
    assert healthy["status"] == "available"


@pytest.mark.parametrize("field,value", [
    ("schema_version", "other-v1"), ("purpose", "engineering_validation"),
    ("source_cutoff_at", "2026-09-11T17:00:00+08:00"),
    ("cutoff_at", "2026-09-11T17:00:00+08:00"),
    ("historical_reconstruction", False), ("historical_reconstruction", "true"),
    ("production_eligible", True),
])
def test_resealed_observation_metadata_cannot_detach_from_frozen_source(tmp_path, field, value):
    selection, inputs, observation = sample(tmp_path)
    observation[field] = value
    seal(observation)
    with pytest.raises(ValueError, match="metadata_binding"):
        validated_panel(selection, inputs, observation)


def test_changed_input_and_changed_qualification_do_not_reuse_old_join(tmp_path):
    selection, inputs, observation = sample(tmp_path)
    changed = deepcopy(inputs)
    changed["securities"]["security-0"]["raw_records"][-1]["close"] = "1"
    with pytest.raises(ValueError):
        validated_panel(selection, changed, observation)
    observation["eligibility"]["evaluations"][0]["eligibility_status"] = "pending"
    seal(observation["eligibility"])
    seal(observation)
    with pytest.raises(ValueError, match="qualification_binding"):
        validated_panel(selection, inputs, observation)


@pytest.mark.parametrize("damage", ["file", "schema", "selection", "mode", "purpose", "observation"])
def test_file_manifest_content_and_identity_bindings_are_checked(tmp_path, damage):
    args = sample(tmp_path)
    directory = tmp_path / "frozen"
    manifest = freeze(directory, *args)
    assert load_reference_inputs(directory) == args
    if damage == "file":
        with (directory / "screening_inputs.json").open("ab") as output:
            output.write(b" ")
    else:
        field = {"schema": "schema_version", "selection": "selection_id", "mode": "mode", "purpose": "purpose", "observation": "observation_content_hash"}[damage]
        manifest[field] = "wrong"
        seal(manifest)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        load_reference_inputs(directory)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", None, 10.0, "", " 10"])
def test_available_score_rejects_nonfinite_missing_or_nonstrict_numeric_indicators(tmp_path, value):
    args = sample(tmp_path)
    packet = build_reference_review(*args)
    packet["records"][0]["indicators"]["rsi14"] = value
    seal(packet)
    with pytest.raises(ValueError, match="indicator"):
        validate(packet, args[2])


@pytest.mark.parametrize("damage", ["empty", "missing", "boolean", "signal", "range", "points"])
def test_resealed_score_requires_complete_indicators_and_matching_component_values(tmp_path, damage):
    args = sample(tmp_path)
    packet = build_reference_review(*args)
    row = packet["records"][0]
    if damage == "empty":
        row["indicators"] = {}
    elif damage == "missing":
        del row["indicators"]["macd_previous_dif"]
    elif damage == "boolean":
        row["indicators"]["macd_above_zero"] = 1
    elif damage == "signal":
        row["indicators"]["macd_above_zero"] = False
    elif damage == "range":
        row["indicators"]["stochastic_k"] = "101"
    else:
        part = next(part for part in row["components"] if part["id"] == "volume")
        part["points"] = 4
        row["score"] += 4
    seal(packet)
    with pytest.raises(ValueError, match="indicator"):
        validate(packet, args[2])


def test_reference_packet_does_not_promote_unknown_baseline_data(tmp_path):
    args = sample(tmp_path)
    packet = build_reference_review(*args)
    evaluations = deepcopy(args[2]["evaluations"])
    evaluations[0]["technical_status"] = "unknown"
    packet["records"][0]["baseline_technical_status"] = "unknown"
    seal(packet)
    with pytest.raises(ValueError, match="score_invalid"):
        validate_reference_review(packet, evaluations=evaluations, trade_date=args[2]["target_date"])
