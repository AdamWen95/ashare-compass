"""The trial technical score is frozen separately and never promotes a candidate."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from ashare_daily.reports.observation import (
    _check_report, build_report, encoded, publish_observation, read_observation_report, sections, sha,
)
from test_financial_observation_report import collect, inputs as financial_inputs
from test_observation_daily import STAMP, report_inputs, seal
from test_m4_viewer import app, texts


COMMIT = "c1bc1797d0c0f314b728f7d9e7639830e87ff0e4"
COMPONENTS = (("trend", "均线趋势"), ("macd", "MACD"), ("stochastic", "随机指标"),
    ("rsi", "RSI"), ("divergence", "已确认背离"), ("volume", "量比"), ("momentum", "动量"))


def packet(args, *, boosted=()):
    selected, observed, _ = args
    qualifiers = {row["security_id"]: row["eligibility_status"] for row in observed["eligibility"]["evaluations"]}
    records = []
    for index, row in enumerate(observed["technical"]["evaluations"]):
        available = row["technical_status"] not in {"unknown", "not_applicable"}
        indicators = {"close": "100", "ma5": "99" if index in boosted else "100",
            "ma10": "98" if index in boosted else "100", "ma20": "97" if index in boosted else "100",
            "ma60": "96" if index in boosted else "100", "daily_long_alignment": index in boosted,
            "macd_dif": "0", "macd_signal": "0", "macd_previous_dif": "0", "macd_previous_signal": "0",
            "macd_golden_cross": False, "macd_above_zero": False,
            "stochastic_k": "50", "stochastic_d": "50", "stochastic_j": "50", "rsi14": "50",
            "volume_ratio20": "1", "return20_pct": "0", "macd_bottom": False, "rsi_bottom": False,
            "macd_top": False, "rsi_top": False}
        records.append({"security_id": row["security_id"], "symbol": row["symbol"], "name": row["name"],
            "baseline_technical_status": row["technical_status"], "eligibility_status": qualifiers[row["security_id"]],
            "status": "available" if available else "unavailable", "score": (62 if index in boosted else 50) if available else None,
            "components": [{"id": key, "label": label, "points": 12 if key == "trend" and index in boosted else 0,
                "reason": "测试冻结行情的逐项依据"} for key, label in COMPONENTS] if available else [],
            "indicators": indicators if available else {}, "issues": [] if available else ["fixed_history_incomplete"],
            "window_hash": "a" * 64 if available else None, "window_start": "2026-04-01" if available else None,
            "window_end": selected["target_date"] if available else None})
    return seal({"schema_version": "reference-strategy-review-v1", "strategy_version": "reference-technical-120-v1",
        "application": "shadow_only", "changes_candidate_ranking": False, "trade_date": selected["target_date"],
        "source_input_hash": "b" * 64, "source_observation_hash": observed["content_hash"],
        "selection_id": selected["selection_id"], "upstream_commit": COMMIT, "records": records,
        "limitations": ["尚无点时无偏历史验证；只分析入选行业。"]})


def publish(root, states, **options):
    args = report_inputs(states)
    review = packet(args, **options)
    result = publish_observation(root, *args, planned_cutoff=STAMP, generated_at=STAMP, reference_review=review)
    report = read_observation_report(root / "outputs", result["directory"])["report"]
    return result, report


def reference_blocks(report):
    return next(blocks for title, blocks in report["sections"] if title == "参考策略技术辅助评分（试运行）")


def reseal_file(directory, name, value):
    body = encoded(value)
    (directory / name).write_bytes(body)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][name] = sha(body)
    (directory / "manifest.json").write_bytes(encoded(manifest))


def test_reference_score_is_frozen_and_never_reorders_formal_candidates(tmp_path):
    result, report = publish(tmp_path, [("pass", "pass")] * 3 + [("pass", "pending"), ("fail", "pass")], boosted=(0, 3, 4))
    assert [row["security_id"] for row in report["observations"]] == ["TEST-2", "TEST-1", "TEST-0"]
    table = next(value for kind, value in reference_blocks(report) if kind == "table")
    assert [row[1] for row in table[1]] == ["sh.600003", "sh.600002", "sh.600001"]
    assert [row[3] for row in table[1]] == [50, 50, 62]
    rendered = str(reference_blocks(report))
    assert "测试公司3" not in rendered and "测试公司4" not in rendered
    frozen = json.loads((Path(result["directory"]) / "report_inputs.json").read_text(encoding="utf-8"))
    assert frozen["reference_review"] == report["reference_review"]
    assert frozen["source_result_hash"] == report["reference_review"]["source_observation_hash"]
    markdown = (Path(result["directory"]) / "daily_observation.md").read_text(encoding="utf-8")
    assert "并非成功概率，尚未证明优于原策略" in markdown
    assert "均线趋势" in markdown and "+12" in markdown and "测试冻结行情的逐项依据" in markdown
    assert COMMIT in markdown


def test_pending_trial_top_five_is_explicit_and_does_not_include_risk_or_technical_failures(tmp_path):
    states = [("pass", "pending")] * 7 + [("pass", "fail"), ("fail", "pass"), ("unknown", "pending")]
    _, report = publish(tmp_path, states, boosted=(6, 7, 8))
    assert not report["observations"] and len(report["pending"]) == 7
    blocks = reference_blocks(report)
    table = next(value for kind, value in blocks if kind == "table")
    assert [row[1] for row in table[1]] == ["sh.600007", "sh.600001", "sh.600002", "sh.600003", "sh.600004"]
    assert all(row[4] == "资格待查，不是正式推荐" for row in table[1])
    assert "测试公司7" not in str(blocks) and "测试公司8" not in str(blocks) and "测试公司9" not in str(blocks)


def test_empty_trial_has_no_placeholder_candidate(tmp_path):
    _, report = publish(tmp_path, [("fail", "pass"), ("unknown", "pending")])
    assert not report["observations"]
    assert "保留空名单" in str(reference_blocks(report))
    assert not any(kind == "table" for kind, _ in reference_blocks(report))


def test_reference_and_financial_packets_keep_original_focus(tmp_path, monkeypatch):
    args = financial_inputs([("pass", "pass")] * 7)
    base = build_report(*args, planned_cutoff=STAMP, generated_at=STAMP)
    finance = collect(tmp_path, monkeypatch, base["observations"][:5])
    review = packet(args, boosted=(0,))
    result = publish_observation(tmp_path, *args, planned_cutoff=STAMP, generated_at=STAMP,
        financial_review=finance, reference_review=review)
    report = read_observation_report(tmp_path / "outputs", result["directory"])["report"]
    assert [row["security_id"] for row in report["focus_candidates"]] == ["TEST-6", "TEST-5", "TEST-4", "TEST-3", "TEST-2"]
    assert report["financial_review"] == finance
    assert report["reference_review"] == review


@pytest.mark.parametrize("mutation", ["source", "selection", "date", "identity", "gate", "score"])
def test_reference_packet_must_match_frozen_report(mutation):
    args = report_inputs([("pass", "pass")])
    review = packet(args)
    if mutation == "source":
        review["source_observation_hash"] = "0" * 64
    elif mutation == "selection":
        review["selection_id"] = "other-selection"
    elif mutation == "date":
        review["trade_date"] = "2026-09-10"
    elif mutation == "identity":
        review["records"][0]["symbol"] = "sh.600099"
    elif mutation == "gate":
        review["records"][0]["eligibility_status"] = "pending"
    else:
        review["records"][0]["score"] = 99
    seal(review)
    with pytest.raises(ValueError):
        build_report(*args, planned_cutoff=STAMP, generated_at=STAMP, reference_review=review)


def test_reference_report_cannot_rewrite_formal_order_even_after_sections_are_updated(tmp_path):
    _, report = publish(tmp_path, [("pass", "pass")] * 3)
    report["observations"].reverse()
    report["sections"] = json.loads(json.dumps(sections(report), ensure_ascii=False))
    with pytest.raises(ValueError, match="baseline_ranking"):
        _check_report(report)


@pytest.mark.parametrize("mutation", ["missing", "different", "source"])
def test_reference_frozen_input_rejected_even_when_file_manifest_is_resealed(tmp_path, mutation):
    result, report = publish(tmp_path, [("pass", "pass")])
    directory = Path(result["directory"])
    frozen = json.loads((directory / "report_inputs.json").read_text(encoding="utf-8"))
    if mutation == "missing":
        frozen.pop("reference_review")
    elif mutation == "different":
        frozen["reference_review"]["limitations"].append("new limitation")
        seal(frozen["reference_review"])
    else:
        frozen["source_result_hash"] = "0" * 64
    reseal_file(directory, "report_inputs.json", frozen)
    with pytest.raises(ValueError, match="reference_review_frozen_inputs"):
        read_observation_report(tmp_path / "outputs", directory)


def test_reference_input_cannot_be_added_to_a_legacy_report(tmp_path):
    args = report_inputs([("pass", "pass")])
    result = publish_observation(tmp_path, *args, planned_cutoff=STAMP, generated_at=STAMP)
    directory = Path(result["directory"])
    frozen = json.loads((directory / "report_inputs.json").read_text(encoding="utf-8"))
    frozen["reference_review"] = packet(args)
    reseal_file(directory, "report_inputs.json", frozen)
    with pytest.raises(ValueError, match="reference_review_report_packet_missing"):
        read_observation_report(tmp_path / "outputs", directory)


def test_changed_reference_content_needs_a_new_report_identity(tmp_path):
    result, report = publish(tmp_path, [("pass", "pass")])
    directory = Path(result["directory"])
    report["reference_review"]["limitations"].append("new limitation")
    seal(report["reference_review"])
    report["sections"] = json.loads(json.dumps(sections(report), ensure_ascii=False))
    frozen = json.loads((directory / "report_inputs.json").read_text(encoding="utf-8"))
    frozen["reference_review"] = deepcopy(report["reference_review"])
    reseal_file(directory, "daily_observation.json", report)
    reseal_file(directory, "report_inputs.json", frozen)
    with pytest.raises(ValueError, match="observation_report_identity_mismatch"):
        read_observation_report(tmp_path / "outputs", directory)


def test_removing_both_reference_packets_cannot_downgrade_to_unchecked_legacy_report(tmp_path):
    result, report = publish(tmp_path, [("pass", "pass")])
    directory = Path(result["directory"])
    report.pop("reference_review")
    report["sections"] = json.loads(json.dumps(sections(report), ensure_ascii=False))
    frozen = json.loads((directory / "report_inputs.json").read_text(encoding="utf-8"))
    frozen.pop("reference_review")
    reseal_file(directory, "daily_observation.json", report)
    reseal_file(directory, "report_inputs.json", frozen)
    with pytest.raises(ValueError, match="observation_report_identity_mismatch"):
        read_observation_report(tmp_path / "outputs", directory)


def test_reference_packet_is_copied_and_any_content_change_creates_new_report(tmp_path):
    args = report_inputs([("pass", "pass")])
    review = packet(args)
    first = publish_observation(tmp_path, *args, planned_cutoff=STAMP, generated_at=STAMP, reference_review=review)
    review["limitations"].append("different trial version")
    seal(review)
    second = publish_observation(tmp_path, *args, planned_cutoff=STAMP, generated_at=STAMP, reference_review=review)
    assert first["report_id"] != second["report_id"]
    original = read_observation_report(tmp_path / "outputs", first["directory"])["report"]
    assert "different trial version" not in original["reference_review"]["limitations"]


def test_viewer_displays_reference_trial_locally_without_mutating_artifacts(tmp_path, monkeypatch):
    publish(tmp_path, [("pass", "pending")])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("viewer must not connect"))
    before = {str(p): sha(p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
    shown = app(tmp_path / "outputs")
    assert not shown.exception
    assert "参考策略技术辅助评分（试运行）" in texts(shown)
    assert "资格待查，不是正式推荐" in texts(shown)
    assert before == {str(p): sha(p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
