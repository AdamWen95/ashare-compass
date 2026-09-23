"""The daily reference stage is optional, local, source-bound and report-only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket

import pytest

from ashare_daily import reference_strategy
from ashare_daily.operations import observation as daily
from test_observation_daily import config, fake_pipeline, invoke, report_inputs, seal


def set_policy(root, cfg, change):
    path = root / cfg.observation_config
    options = json.loads(path.read_text(encoding="utf-8"))
    change(options)
    path.write_text(json.dumps(options), encoding="utf-8")


@pytest.mark.parametrize("key,value", [("enabled", 1), ("application", "replace_ranking"),
    ("changes_candidate_ranking", True), ("llm_export", True), ("external_calls", True)])
def test_reference_policy_rejects_implicit_expansion(tmp_path, key, value):
    cfg = config(tmp_path)
    set_policy(tmp_path, cfg, lambda options: options["reference_strategy"].update({key: value}))
    with pytest.raises(ValueError, match="invalid_reference_strategy_policy"):
        daily.load_options(tmp_path, cfg.observation_config)


@pytest.mark.parametrize("missing", [False, True])
def test_disabled_or_absent_reference_policy_skips_stage_and_freezes_no_packet(tmp_path, monkeypatch, missing):
    cfg = config(tmp_path)
    set_policy(tmp_path, cfg, lambda options: options.pop("reference_strategy") if missing else
        options["reference_strategy"].update(enabled=False))
    calls = fake_pipeline(monkeypatch, [("pass", "pass")])
    monkeypatch.setattr(daily, "_reference_review", lambda *a, **k: pytest.fail("disabled reference stage invoked"))
    result = invoke(tmp_path, cfg)
    assert result["generation_status"] == "ok"
    assert calls == ["universe", "selection", "screening", "model"]
    assert "reference_strategy" not in result["module_statuses"]
    assert "reference_summary" not in result
    report = json.loads((Path(result["report"]["directory"]) / "daily_observation.json").read_text(encoding="utf-8"))
    assert "reference_review" not in report


def test_disabled_reference_helper_does_not_read_or_create_files(tmp_path, monkeypatch):
    monkeypatch.setattr(reference_strategy, "load_reference_inputs", lambda *a: pytest.fail("disabled helper read source"))
    assert daily._reference_review(tmp_path, {}, {}, {}, tmp_path / "reference") is None
    assert not (tmp_path / "reference").exists()


def test_daily_reference_attachment_reaches_report_but_not_model_context(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    calls = fake_pipeline(monkeypatch, [("pass", "pass")])
    marker = "SENSITIVE_LOCAL_REFERENCE_SCORE"
    packet = {"content_hash": "fixture", "records": [{"status": "available", "name": marker}]}
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("reference pipeline must not connect"))
    def review(*args, **kwargs):
        calls.append("reference")
        assert args[2]["technical"]["evaluations"]
        return packet
    def model(*args, **kwargs):
        calls.append("model")
        assert marker not in repr(args) + repr(kwargs)
        assert "reference_review" not in repr(args) + repr(kwargs)
        return {"status": "skipped", "model_run": {"status": "skipped", "call_count": 0}}
    def publish(*args, **kwargs):
        assert kwargs["reference_review"] is packet
        assert "reference_review" not in args[3]
        assert marker not in repr(args[3])
        return {"directory": str(tmp_path / "published")}
    monkeypatch.setattr(daily, "_reference_review", review)
    monkeypatch.setattr(daily, "_context", model)
    monkeypatch.setattr(daily, "_publish", publish)
    result = invoke(tmp_path, cfg)
    assert calls == ["universe", "selection", "screening", "reference", "model"]
    assert result["generation_status"] == "ok"
    assert result["module_statuses"]["reference_strategy"] == "shadow_only"
    assert result["reference_summary"] == {"content_hash": "fixture", "scored_count": 1,
        "changes_candidate_ranking": False, "network_requests": 0, "model_calls": 0}


def test_reference_helper_only_uses_matching_frozen_inputs_and_writes_attachment(tmp_path, monkeypatch):
    selected, observed, _ = report_inputs([("pass", "pass")])
    observed["evidence_directory"] = "frozen"
    frozen_inputs = {"sentinel": "frozen-input"}
    options = {"reference_strategy": {"enabled": True}}
    packet = {"content_hash": "fixture", "records": []}
    original = deepcopy(observed)
    def load(directory):
        assert directory == tmp_path / "frozen"
        return deepcopy(selected), frozen_inputs, deepcopy(observed)
    def build(selection, inputs, observation):
        assert selection == selected and inputs is frozen_inputs and observation == observed
        return packet
    monkeypatch.setattr(reference_strategy, "load_reference_inputs", load)
    monkeypatch.setattr(reference_strategy, "build_reference_review", build)
    result = daily._reference_review(tmp_path, selected, observed, options, tmp_path / "reference")
    assert result is packet and observed == original
    assert json.loads((tmp_path / "reference/reference_review.json").read_text(encoding="utf-8")) == packet


@pytest.mark.parametrize("mismatch", ["selection", "observation"])
def test_reference_helper_rejects_other_frozen_input_before_scoring(tmp_path, monkeypatch, mismatch):
    selected, observed, _ = report_inputs([("pass", "pass")])
    observed["evidence_directory"] = "frozen"
    saved_selection, saved_observation = deepcopy(selected), deepcopy(observed)
    (saved_selection if mismatch == "selection" else saved_observation)["other_version"] = True
    monkeypatch.setattr(reference_strategy, "load_reference_inputs", lambda *a: (saved_selection, {}, saved_observation))
    monkeypatch.setattr(reference_strategy, "build_reference_review", lambda *a: pytest.fail("mismatched input scored"))
    with pytest.raises(ValueError, match="differs_from_frozen_files"):
        daily._reference_review(tmp_path, selected, observed, {"reference_strategy": {"enabled": True}}, tmp_path / "reference")
    assert not (tmp_path / "reference").exists()


def test_reference_helper_rejects_source_artifact_tampering(tmp_path, monkeypatch):
    selected, observed, _ = report_inputs([("pass", "pass")])
    observed["evidence_directory"] = "frozen"
    seal(observed)
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    files = {}
    for name, value in (("selection.json", selected), ("screening_inputs.json", {}), ("observation.json", observed)):
        body = json.dumps(value).encode("utf-8")
        (frozen / name).write_bytes(body)
        files[name] = hashlib.sha256(body).hexdigest()
    (frozen / "manifest.json").write_text(json.dumps(seal({"schema_version": "sector-observation-manifest-v1",
        "selection_id": selected["selection_id"], "selection_content_hash": selected["content_hash"],
        "mode": selected["mode"], "purpose": selected["purpose"], "files": files,
        "observation_content_hash": observed["content_hash"]})), encoding="utf-8")
    (frozen / "screening_inputs.json").write_text('{"tampered":true}', encoding="utf-8")
    monkeypatch.setattr(reference_strategy, "build_reference_review", lambda *a: pytest.fail("tampered source scored"))
    with pytest.raises(ValueError, match="reference_frozen_file_hash_mismatch"):
        daily._reference_review(tmp_path, selected, observed, {"reference_strategy": {"enabled": True}}, tmp_path / "reference")
    assert not (tmp_path / "reference").exists()


def test_failed_reference_source_validation_stops_publication_and_model(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    calls = fake_pipeline(monkeypatch, [("pass", "pass")])
    def invalid(*args, **kwargs):
        raise ValueError("reference_frozen_file_hash_mismatch")
    monkeypatch.setattr(daily, "_reference_review", invalid)
    monkeypatch.setattr(daily, "_publish", lambda *a, **k: pytest.fail("invalid source published"))
    result = invoke(tmp_path, cfg)
    assert result["status"] == "failed" and result["generation_status"] == "not_run"
    assert calls == ["universe", "selection", "screening"]
    assert result["model_summary"]["call_count"] == 0
    assert "report" not in result
