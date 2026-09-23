"""Frozen report policy, historical reader compatibility and clear scope labels."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ashare_daily.reports.observation import (
    build_report, encoded, publish_observation, read_observation_report, sha,
)
from ashare_daily.viewer_dashboard import cards_html
from test_observation_daily import report_inputs, seal, STAMP


def inputs(*, required=False, evidence_status="unknown"):
    selection, observation, context = report_inputs([("pass", "pass")])
    observation["eligibility_policy"] = {"require_delisting_check": required}
    observation["eligibility"]["eligibility_policy"] = deepcopy(observation["eligibility_policy"])
    condition = {"id": "not_delisting_period", "label": "非退市整理期", "required": required,
        "status": evidence_status if required else "not_required", "reason": "保留来源事实"}
    if not required:
        condition.update(evidence_status=evidence_status, policy_reason="本期不将退市整理期作为候选资格条件。")
    observation["eligibility"]["evaluations"][0]["conditions"] = [condition]
    seal(observation["eligibility"])
    seal(observation)
    return selection, observation, context


@pytest.mark.parametrize("evidence_status", ["unknown", "pass", "fail"])
def test_optional_condition_is_frozen_without_claiming_the_evidence_passed(tmp_path, evidence_status):
    original = inputs(evidence_status=evidence_status)
    before = deepcopy(original)
    published = publish_observation(tmp_path, *original, planned_cutoff=STAMP, generated_at=STAMP)
    archived = read_observation_report(tmp_path / "outputs", published["directory"])
    report = archived["report"]
    assert original == before
    assert report["counts"]["observation_count"] == 1
    assert report["eligibility_policy"] == {"require_delisting_check": False}
    condition = report["observations"][0]["eligibility_conditions"][0]
    assert condition["status"] == "not_required" and condition["evidence_status"] == evidence_status
    frozen = json.loads((Path(published["directory"]) / "report_inputs.json").read_bytes())
    assert frozen["eligibility_policy"] == report["eligibility_policy"]
    markdown = archived["files"]["daily_observation.md"].decode()
    assert "本期基本资格筛选不含退市整理期" in markdown
    assert "不代表已完成全部风险核查" in markdown
    assert "本期纳入资格条件的已确认风险继续排除" in markdown
    assert "eligibility_conditions" in archived["files"]["screening_audit.csv"].decode("utf-8-sig")
    assert "本期条件通过" in cards_html(report, report["observations"], False)


def test_old_report_without_policy_keeps_original_scope_and_reader_contract(tmp_path):
    legacy = report_inputs([("pass", "pass")])
    published = publish_observation(tmp_path, *legacy, planned_cutoff=STAMP, generated_at=STAMP)
    # Captured from the pre-policy implementation with this frozen fixture.
    assert published["report_id"] == "observation-c4363ed4e33886806a6a7535"
    archived = read_observation_report(tmp_path / "outputs", published["directory"])
    assert "eligibility_policy" not in archived["report"]
    frozen = json.loads((Path(published["directory"]) / "report_inputs.json").read_bytes())
    assert "eligibility_policy" not in frozen
    assert "基本资格筛选不含退市整理期" not in archived["files"]["daily_observation.md"].decode()
    assert "eligibility_conditions" not in archived["files"]["screening_audit.csv"].decode("utf-8-sig")
    assert "原规则通过" in cards_html(archived["report"], archived["report"]["observations"], False)


def test_rehashed_input_policy_cannot_disagree_with_frozen_report(tmp_path):
    published = publish_observation(tmp_path, *inputs(), planned_cutoff=STAMP, generated_at=STAMP)
    directory = Path(published["directory"])
    frozen = json.loads((directory / "report_inputs.json").read_bytes())
    frozen["eligibility_policy"]["require_delisting_check"] = True
    body = encoded(frozen)
    (directory / "report_inputs.json").write_bytes(body)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["files"]["report_inputs.json"] = sha(body)
    (directory / "manifest.json").write_bytes(encoded(manifest))
    with pytest.raises(ValueError, match="eligibility_policy_inputs_mismatch"):
        read_observation_report(tmp_path / "outputs", directory)


def test_qualification_and_observation_policy_must_agree():
    selection, observation, context = inputs()
    observation["eligibility_policy"]["require_delisting_check"] = True
    seal(observation)
    with pytest.raises(ValueError, match="eligibility_policy_binding_mismatch"):
        build_report(selection, observation, context, planned_cutoff=STAMP, generated_at=STAMP)


@pytest.mark.parametrize("change", ["required", "status", "evidence_status"])
def test_publishing_rejects_policy_condition_mismatch(tmp_path, change):
    selection, observation, context = inputs()
    condition = observation["eligibility"]["evaluations"][0]["conditions"][0]
    condition[change] = {"required": True, "status": "pass", "evidence_status": "not_required"}[change]
    seal(observation["eligibility"])
    seal(observation)
    with pytest.raises(ValueError, match="eligibility_policy_condition_mismatch"):
        publish_observation(tmp_path, selection, observation, context, planned_cutoff=STAMP, generated_at=STAMP)
    assert not (tmp_path / "outputs").exists()


def test_dashboard_explains_excluded_check_and_displays_real_evidence_status():
    from streamlit.testing.v1 import AppTest
    data = build_report(*inputs(), planned_cutoff=STAMP, generated_at=STAMP)
    shown = AppTest.from_string("import streamlit as st\nfrom ashare_daily.viewer_dashboard import _summary, _detail\n"
        + "report = " + repr(data) + "\n_summary(st, report)\n_detail(st, report)").run()
    assert not shown.exception
    assert any("本期基本资格筛选不含退市整理期" in item.value for item in shown.caption)
    contents = "\n".join(frame.value.to_string() for frame in shown.dataframe)
    assert "不参与资格筛选" in contents and "已有证据状态：未知" in contents
    assert "本期不将退市整理期作为候选资格条件" in contents
