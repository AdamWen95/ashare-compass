"""Financial supplements remain bound to frozen, deterministic research candidates."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket

import pytest

from ashare_daily import financial_review as finance
from ashare_daily.reports.observation import (
    _check_report, _selection_basis, build_report, encoded, publish_observation,
    read_observation_report, sections, sha,
)
from test_observation_daily import ROOT, STAMP, report_inputs, seal
from test_m4_viewer import app, texts


def inputs(states):
    selected, observed, context = report_inputs(states)
    observed["technical"]["strategy_config"] = json.loads((ROOT / "config/sector_screening_f4s1.json").read_text(encoding="utf-8"))
    seal(observed["technical"], "result_hash")
    observed["eligibility"]["technical_result_hash"] = observed["technical"]["result_hash"]
    seal(observed["eligibility"])
    seal(observed)
    return selected, observed, context


def collect(root, monkeypatch, candidates, *, partial=False, online=True, cutoff=STAMP):
    monkeypatch.setattr(finance, "_now", lambda: datetime.fromisoformat("2026-09-12T10:00:00+08:00"))
    def query(operation, symbol, year, quarter, timeout_seconds):
        if partial and operation == "cash_flow":
            return {"status": "source_error"}
        row = {field: "0.125000" for field in finance.FIELDS[operation]}
        row.update(code=symbol, pubDate="2026-08-15", statDate="2026-06-30")
        if operation == "profit":
            row["netProfit"] = "-10.0000"
        else:
            row["CFOToNP"] = "-0.5000"
            row["ebitToInterest"] = ""
        return {"status": "ok", "fields": finance.FIELDS[operation], "rows": [row]}
    monkeypatch.setattr(finance, "_run_query", query)
    return finance.collect_financial_review(candidates=candidates, target_date="2026-09-11", cutoff_at=cutoff,
        output_directory=root / "source", cache_directory=root / "cache", online=online)


def enhanced(root, monkeypatch, states, **options):
    args = inputs(states)
    base = build_report(*args, planned_cutoff=STAMP, generated_at=STAMP)
    packet = collect(root, monkeypatch, base["observations"][:5], **options)
    result = publish_observation(root, *args, planned_cutoff=STAMP, generated_at=STAMP, financial_review=packet)
    return result, read_observation_report(root / "outputs", result["directory"])["report"]


@pytest.mark.parametrize("states,expected", [
    ([], "cacc8bd7d2a9d31142a67988c213983e8176f2f0182377cb3b97f26440804db7"),
    ([("pass", "pass"), ("pass", "pending"), ("fail", "pass")], "df33dda17fb43383785eee0378ffa101fa1fc4fee0b55d7cb85f6a387980f06f"),
])
def test_legacy_report_bytes_match_previous_version(states, expected):
    report = build_report(*report_inputs(states), planned_cutoff=STAMP, generated_at=STAMP)
    assert sha(encoded(report)) == expected
    assert "financial_review" not in report


def test_focus_retains_original_ranking_and_unknown_eligibility_is_never_promoted(tmp_path, monkeypatch):
    states = [("pass", "pass")] * 7 + [("pass", "pending"), ("pass", "fail")]
    result, report = enhanced(tmp_path, monkeypatch, states)
    expected = [f"TEST-{i}" for i in range(6, -1, -1)]
    assert [row["security_id"] for row in report["observations"]] == expected
    assert [row["security_id"] for row in report["focus_candidates"]] == expected[:5]
    assert [row["security_id"] for row in report["financial_review"]["records"]] == expected[:5]
    assert report["pending"][0]["security_id"] == "TEST-7"
    assert report["counts"]["stock_count"] == 9
    assert all(row["company_materials_status"] == "partial_financial_review" for row in report["focus_candidates"])
    frozen = json.loads((Path(result["directory"]) / "report_inputs.json").read_text(encoding="utf-8"))
    assert frozen["financial_review"] == report["financial_review"]
    assert frozen["focus_security_ids"] == expected[:5]
    assert report["model_context"]["model_run"]["call_count"] == 0


def test_raw_financial_values_dates_risk_and_limitations_are_visible(tmp_path, monkeypatch):
    result, report = enhanced(tmp_path, monkeypatch, [("pass", "pass")])
    markdown = (Path(result["directory"]) / "daily_observation.md").read_text(encoding="utf-8")
    assert "今日方向简报 · A股研究候选" in markdown
    assert "0.125000" in markdown and "12.50%" not in markdown
    assert "-0.5000" in markdown and "不能据此认定经营现金流为负" in markdown
    assert "来源净利润为负" in markdown and "尚未核查的公告" in markdown
    assert "2026-06-30" in markdown and "2026-08-15" in markdown
    assert "2026-09-12T10:00:00+08:00" in markdown and "历史补采资料" in markdown
    assert "后续观察条件" in markdown and "不少于50,000,000.00元" in markdown
    assert "基本面已完成核查" not in markdown
    assert report["financial_review"]["historical_reconstruction"]


def test_selection_description_uses_frozen_parameters():
    description = _selection_basis({"min_history_trading_days": 150, "amount_days": 30,
        "min_avg_amount_cny": "75000000", "ma_short_days": 25, "ma_long_days": 70,
        "return_days": 30, "benchmark_name": "测试基准"})
    assert "150个交易日" in description and "MA25高于MA70" in description
    assert "75,000,000.00元" in description and "近30日表现强于测试基准" in description
    assert "120" not in description


def test_partial_financial_failure_does_not_remove_research_candidate(tmp_path, monkeypatch):
    _, report = enhanced(tmp_path, monkeypatch, [("pass", "pass")], partial=True)
    assert len(report["observations"]) == 1
    record = report["financial_review"]["records"][0]
    assert record["status"] == "partial" and record["cash_flow"] is None
    assert report["observations"][0]["company_materials_status"] == "partial_financial_review"


def test_no_candidates_keeps_empty_focus_and_makes_no_financial_calls(tmp_path, monkeypatch):
    _, report = enhanced(tmp_path, monkeypatch, [("pass", "pending"), ("fail", "pass")])
    assert not report["observations"] and not report["focus_candidates"]
    assert report["financial_review"]["status"] == "empty"
    assert report["financial_review"]["network_requests"] == 0
    assert "本期没有同时满足" in str(report["sections"])


def test_report_rejects_financial_identity_or_cutoff_from_another_selection(tmp_path, monkeypatch):
    args = inputs([("pass", "pass")])
    base = build_report(*args, planned_cutoff=STAMP, generated_at=STAMP)
    packet = collect(tmp_path, monkeypatch, base["observations"], cutoff="2026-09-11T22:00:00+08:00")
    with pytest.raises(ValueError, match="cutoff mismatch"):
        build_report(*args, planned_cutoff=STAMP, generated_at=STAMP, financial_review=packet)
    packet = collect(tmp_path / "other", monkeypatch,
        [{"security_id": "OTHER", "symbol": "sz.000001", "name": "其他公司"}], online=False)
    with pytest.raises(ValueError, match="identity mismatch"):
        build_report(*args, planned_cutoff=STAMP, generated_at=STAMP, financial_review=packet)


@pytest.mark.parametrize("mutation", ["focus", "risk", "review_status"])
def test_frozen_report_semantics_cannot_be_rewritten_even_with_new_sections(tmp_path, monkeypatch, mutation):
    _, report = enhanced(tmp_path, monkeypatch, [("pass", "pass")] * 2)
    if mutation == "focus":
        report["focus_candidates"].reverse()
    elif mutation == "risk":
        packet = report["financial_review"]
        packet["records"][0]["risk_flags"] = []
        packet["content_hash"] = finance._digest({k: v for k, v in packet.items() if k != "content_hash"})
    else:
        for group in ("evaluations", "observations", "focus_candidates"):
            for row in report[group]:
                row["company_materials_status"] = "fully_verified"
    report["sections"] = json.loads(json.dumps(sections(report), ensure_ascii=False))
    with pytest.raises(ValueError):
        _check_report(report)


def test_changed_frozen_financial_input_rejected_after_artifact_manifest_resealed(tmp_path, monkeypatch):
    result, _ = enhanced(tmp_path, monkeypatch, [("pass", "pass")])
    directory = Path(result["directory"])
    frozen = json.loads((directory / "report_inputs.json").read_text(encoding="utf-8"))
    frozen["financial_review"]["status"] = "unavailable"
    body = encoded(frozen)
    (directory / "report_inputs.json").write_bytes(body)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["report_inputs.json"] = sha(body)
    (directory / "manifest.json").write_bytes(encoded(manifest))
    with pytest.raises(ValueError, match="frozen_inputs"):
        read_observation_report(tmp_path / "outputs", directory)


def test_viewer_displays_financial_candidates_without_network_or_mutation(tmp_path, monkeypatch):
    enhanced(tmp_path, monkeypatch, [("pass", "pass"), ("pass", "pending")])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("viewer must not connect"))
    monkeypatch.setattr(finance, "_run_query", lambda *a, **k: pytest.fail("viewer must not collect"))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    shown = app(tmp_path / "outputs")
    assert not shown.exception
    assert "重点研究候选" in texts(shown)
    shown.radio(key="observation_view").set_value("个股详情").run()
    assert not shown.exception
    assert "来源净利润为负" in "\n".join(item.value for item in shown.markdown)
    assert any('<h1>今日方向简报</h1>' in item.value for item in shown.markdown)
    shown.button(key="refresh_display").click().run()
    assert not shown.exception
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
