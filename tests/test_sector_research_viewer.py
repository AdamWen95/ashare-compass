"""Offline dual-report display/identity boundaries, with real shared validation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import socket

import pytest

from ashare_daily.sector_selection import digest
from ashare_daily.viewer import (ArchiveError, SECTOR_RESEARCH_EXPORTS,
    read_sector_research_report, scan_sector_research_reports, sector_research_artifact_bytes,
    scan_reports, scan_runs)
from test_m4_viewer import app, texts as legacy_texts, write_report


def texts(at):
    # New deterministic section paragraphs reuse the existing escaped Markdown
    # renderer; include their visible text as well as legacy text widgets.
    return legacy_texts(at)+"\n"+"\n".join(str(item.value).replace("\\_", "_") for item in at.markdown)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def fixture(output, *, engineering=False, suffix="a"):
    purpose = "engineering_validation" if engineering else "production"
    members = [{"security_id": "OFFLINE-"+str(i), "code": str(600001+i), "exchange": "SSE", "board": "sse_main"}
               for i in range(7)] if engineering else []
    selection = {"schema_version": "f3s-validation-selection-v1" if engineering else "f2s1-sector-selection-v1",
        "purpose": purpose, "production_eligible": not engineering, "mode": "offline_test",
        "automatic_selection": not engineering, "source_selection_id": "sector-OFFLINE-parent" if engineering else None,
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "target_date": "2026-09-11",
        "cutoff_at": "2026-09-11T20:00:00+08:00", "members": members, "selected_security_count": len(members),
        "selection_status": "validation" if engineering else "no_matching_sectors", "industry_comparison_complete": True,
        "catalog_count": 19, "selection_verified": True, "fixture": suffix,
        "sectors": [] if engineering else [{"sector_id": "OFFLINE-sector-"+str(i)} for i in range(19)]}
    signature = digest(selection)
    selection_id = ("validation-sector-" if engineering else "sector-")+"2026-09-11-"+signature[:20]
    selection.update(content_hash=signature, selection_id=selection_id)
    fingerprint = digest({"OFFLINE": signature})
    report_id = "sector-report-"+fingerprint[:24]
    directory = (output/"engineering_validation/f3s"/selection_id/"f4s1/reports"/report_id if engineering else
                 output/"research/sse_szse_a/sector_reports"/selection_id/report_id)
    directory.mkdir(parents=True)
    report = {"schema_version": "f4s1-sector-report-v1", "report_id": report_id,
        "report_kind": "engineering_diagnostic" if engineering else "production_daily", "purpose": purpose,
        "production_eligible": not engineering, "selection_id": selection_id, "selection_content_hash": signature,
        "mode": "offline_test", "market_scope": "sse_szse_a", "research_mode": "sector_first", "trade_date": "2026-09-11",
        "actual_generated_at": "2026-09-12T20:00:00+08:00", "title": "OFFLINE ONLY", "notice": "离线结构测试，不是真实采集",
        "evaluations": [{"security_id": member["security_id"], "symbol": "sh."+member["code"],
            "technical_status": "pass" if index == 0 else "fail", "eligibility_status": "pass" if index < 6 else "fail"}
            for index, member in enumerate(members)],
        "sector_comparison": [] if engineering else [{"sector_id": "OFFLINE-sector-"+str(i)} for i in range(19)],
        "history_gap_accounting": [], "materials": [], "gaps": [],
        "counts": {"stock_count": len(members), "catalog_count": 19, "preselected_count": None if engineering else 0,
            "selected_count": None if engineering else 0, "technical_pass_count": 1 if engineering else 0,
            "technical_fail_count": 6 if engineering else 0, "technical_unknown_count": 0, "technical_not_applicable_count": 0,
            "eligibility_pass_count": 6 if engineering else 0, "eligibility_fail_count": 1 if engineering else 0,
            "eligibility_pending_count": 0, "formal_candidate_count": 0, "model_queue_count": 0},
        "model_calls": 0, "model_tokens": 0, "network_requests": 0, "database_writes": 0, "f4s2_ready": False,
        "conclusion": "OFFLINE 工程核验" if engineering else "本分类体系下无行业满足当前观察规则。",
        "sections": [["OFFLINE工程内容" if engineering else "OFFLINE生产行业比较", [
            ["paragraph", "OFFLINE_ENGINEERING_SEVEN_ONLY" if engineering else "OFFLINE_PRODUCTION_ZERO_ONLY"],
            ["table", [["项目", "状态"], [["仅夹具", "明确隔离"]]]]]]]}
    bundle = {}
    if engineering:
        readiness, accounts, qualifications = [], [], []
        for row in report["evaluations"]:
            row.update(eligibility_conditions=[], eligibility_facts=[], eligibility_gaps=[], exclusion_reasons=[],
                       cache_target_complete=True, strategy_inputs_ready=True, history_window_accounted_for=True)
            ready = {"security_id": row["security_id"], "symbol": row["symbol"], "historical_non_trading_dates": [],
                "cache_status": {"cache_expected_dates": 320, "cache_raw_dates": 320, "cache_adjusted_dates": 320,
                    "missing_raw_dates": [], "missing_adjusted_dates": []}, "cache_target_complete": True,
                "history_window_accounted_for": True, "strategy_inputs_ready": True, "valid_history_count": 320}
            account = {"security_id": row["security_id"], "symbol": row["symbol"], "cache_target_rows": 320,
                "cache_target_complete": True, "evidenced_non_trading_dates": [], "expected_required_rows": 320,
                "actual_raw_rows": 320, "actual_adjusted_rows": 320, "unexplained_raw_dates": [], "unexplained_adjusted_dates": [],
                "history_window_accounted_for": True, "strategy_inputs_ready": True, "fixed_strategy_valid_days": 320,
                "price_rows_added": 0, "calendar_dates_skipped": 0}
            row["expected_history"] = account
            readiness.append(ready)
            accounts.append(account)
            qualifications.append({"security_id": row["security_id"], "eligibility_status": row["eligibility_status"],
                "conditions": [], "facts": [], "gaps": [], "exclusion_reasons": []})
        report["history_gap_accounting"] = accounts
        bundle = {"technical": {"evaluations": deepcopy(report["evaluations"])}, "eligibility": {"evaluations": qualifications},
            "materials": {"packages": []}, "readiness": readiness}
    report["counts"].update(cache_complete_count=len(members), strategy_ready_count=len(members),
        expected_required_rows=320*len(members), actual_raw_rows=320*len(members))
    report["content_hash"] = digest(report)
    inputs = {"schema_version": "f4s1-sector-report-input-v1", "selection_content_hash": signature,
        "purpose": purpose, "production_eligible": not engineering, "actual_generated_at": report["actual_generated_at"],
        "fingerprint": fingerprint, "source_refs": [], "bundle": bundle}
    contents = {"sector_research_report.json": encode(report), "sector_selection.json": encode(selection),
        "report_inputs.json": encode(inputs), "sector_research_report.md": b"# OFFLINE fixture",
        "sector_research_report.html": b"<html><body>OFFLINE fixture</body></html>",
        "eligibility_fields.csv": b"security_id,status\n", "history_gap_accounting.csv": b"security_id,date\n"}
    manifest = {"schema_version": "f4s1-sector-report-manifest-v1", "report_id": report_id, "selection_id": selection_id,
        "selection_content_hash": signature, "purpose": purpose, "production_eligible": not engineering,
        "market_scope": "sse_szse_a", "research_mode": "sector_first", "fingerprint": fingerprint,
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}}
    for name, body in contents.items():
        (directory/name).write_bytes(body)
    (directory/"manifest.json").write_bytes(encode(manifest))
    return directory


def rewrite(directory, name, changes):
    value = json.loads((directory/name).read_bytes())
    value.update(changes)
    if name == "sector_research_report.json":
        value["content_hash"] = digest({key: item for key, item in value.items() if key != "content_hash"})
    (directory/name).write_bytes(encode(value))
    if name != "manifest.json":
        manifest = json.loads((directory/"manifest.json").read_bytes())
        manifest["files"][name] = hashlib.sha256((directory/name).read_bytes()).hexdigest()
        (directory/"manifest.json").write_bytes(encode(manifest))


def test_production_zero_report_is_visible_and_engineering_requires_explicit_choice(tmp_path, monkeypatch):
    fixture(tmp_path)
    fixture(tmp_path, engineering=True)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("viewer must not network"))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    at = app(tmp_path)
    assert not at.exception
    assert "OFFLINE_PRODUCTION_ZERO_ONLY" in texts(at) and "S=0" in texts(at)
    assert "OFFLINE_ENGINEERING_SEVEN_ONLY" not in texts(at)
    assert len(at.get("download_button")) == 0
    at.checkbox(key="prepare_sector_exports_production").set_value(True).run()
    assert len(at.get("download_button")) == 5
    at.selectbox(key="archive_section").select("工程验收（非生产）").run()
    assert not at.exception and "OFFLINE_ENGINEERING_SEVEN_ONLY" in texts(at)
    assert "工程验收观察报告（非生产）" in texts(at)
    assert "OFFLINE_PRODUCTION_ZERO_ONLY" not in texts(at)
    assert len(at.get("download_button")) == 0
    at.checkbox(key="prepare_sector_exports_engineering_validation").set_value(True).run()
    assert len(at.get("download_button")) == 5
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert len(scan_sector_research_reports(tmp_path)[0]) == 1
    assert not scan_runs(tmp_path)[0] and not scan_reports(tmp_path)[0]


def test_engineering_directory_is_not_enumerated_by_default(tmp_path, monkeypatch):
    fixture(tmp_path, engineering=True)
    import ashare_daily.viewer as viewer
    original, calls = viewer.scan_sector_research_reports, []

    def scan(root, *, purpose="production"):
        calls.append(purpose)
        return original(root, purpose=purpose)

    monkeypatch.setattr(viewer, "scan_sector_research_reports", scan)
    at = app(tmp_path)
    assert not at.exception and calls == ["production"]
    assert "OFFLINE_ENGINEERING_SEVEN_ONLY" not in texts(at)
    at.selectbox(key="archive_section").select("工程验收（非生产）").run()
    assert not at.exception and calls[-2:] == ["production", "engineering_validation"]


def test_new_report_keeps_old_sample_report_available_only_as_history(tmp_path):
    fixture(tmp_path)
    write_report(tmp_path)
    at = app(tmp_path)
    assert not at.exception and "所选报告：2026-09-08" not in texts(at)
    at.selectbox(key="archive_section").select("历史样本日报").run()
    assert not at.exception and "所选报告：2026-09-08" in texts(at)


@pytest.mark.parametrize("where", ["manifest.json", "sector_research_report.json", "report_inputs.json", "sector_selection.json"])
@pytest.mark.parametrize("marker", [{"purpose": "engineering_validation"}, {"purpose": None},
    {"purpose": "unknown"}, {"production_eligible": False}, {"production_eligible": 1}])
def test_production_shell_does_not_hide_engineering_or_unknown_identity(tmp_path, where, marker):
    directory = fixture(tmp_path)
    rewrite(directory, where, marker)
    reports, problems = scan_sector_research_reports(tmp_path)
    assert reports == [] and problems
    with pytest.raises(ArchiveError):
        read_sector_research_report(directory, tmp_path)


def test_renamed_engineering_report_cannot_enter_production_directory(tmp_path):
    source = fixture(tmp_path, engineering=True)
    production = fixture(tmp_path)
    for name in ("sector_research_report.json", "report_inputs.json"):
        engineering = json.loads((source/name).read_bytes())
        target = json.loads((production/name).read_bytes())
        target["hidden_input"] = engineering
        rewrite(production, name, target)
    assert not scan_sector_research_reports(tmp_path)[0]
    with pytest.raises(ArchiveError):
        read_sector_research_report(source, tmp_path)


@pytest.mark.parametrize("name", ["sector_research_report.md", "sector_research_report.html", "eligibility_fields.csv", "report_inputs.json"])
def test_public_or_internal_file_tampering_blocks_whole_report(tmp_path, name):
    directory = fixture(tmp_path)
    with (directory/name).open("ab") as stream:
        stream.write(b" changed")
    assert not scan_sector_research_reports(tmp_path)[0]
    with pytest.raises(ArchiveError, match="哈希"):
        read_sector_research_report(directory, tmp_path)


def test_export_rechecks_purpose_and_never_downloads_inputs(tmp_path):
    directory = fixture(tmp_path)
    archive = read_sector_research_report(directory, tmp_path)
    for forbidden in ("report_inputs.json", "sector_selection.json", "../.env", "/arbitrary/path"):
        with pytest.raises(ArchiveError, match="白名单"):
            sector_research_artifact_bytes(archive, forbidden)
    rewrite(directory, "report_inputs.json", {"purpose": "engineering_validation"})
    with pytest.raises(ArchiveError):
        sector_research_artifact_bytes(archive, "sector_research_report.html")


@pytest.mark.parametrize("path", ["../../.env", "https://example.invalid/source", "outputs/../.env.local",
    "outputs/engineering_validation/f3s/validation-sector-hidden/raw.json"])
def test_unsafe_or_engineering_source_locator_is_rejected_without_reading_it(tmp_path, path):
    directory = fixture(tmp_path)
    rewrite(directory, "report_inputs.json", {"source_refs": [{"path": path, "sha256": "a"*64}]})
    with pytest.raises(ArchiveError):
        read_sector_research_report(directory, tmp_path)


def test_unknown_manifest_file_is_rejected_before_any_export(tmp_path):
    directory = fixture(tmp_path)
    manifest = json.loads((directory/"manifest.json").read_bytes())
    manifest["files"]["../../.env"] = "a"*64
    rewrite(directory, "manifest.json", manifest)
    with pytest.raises(ArchiveError, match="文件集合"):
        read_sector_research_report(directory, tmp_path)


def test_duplicate_json_purpose_cannot_be_hidden_by_parser(tmp_path):
    directory = fixture(tmp_path)
    path = directory/"manifest.json"
    data = path.read_text(encoding="utf-8")
    path.write_text(data.replace('"purpose": "production"', '"purpose": "engineering_validation", "purpose": "production"'), encoding="utf-8")
    with pytest.raises(ArchiveError, match="重复"):
        read_sector_research_report(directory, tmp_path)


def test_registered_source_paths_are_not_opened_by_browser(tmp_path):
    directory = fixture(tmp_path)
    missing = tmp_path/"verification/not-downloaded.json"
    rewrite(directory, "report_inputs.json", {"source_refs": [{"path": str(missing), "sha256": "a"*64}]})
    # Source response authentication belongs to publication/replay; the browser
    # verifies its seven registered companions and never initiates acquisition.
    archive = read_sector_research_report(directory, tmp_path)
    assert archive.report["counts"]["stock_count"] == 0 and not missing.exists()


def test_new_reader_is_strictly_bounded_even_for_internal_inputs(tmp_path, monkeypatch):
    directory = fixture(tmp_path)
    import ashare_daily.viewer as viewer
    monkeypatch.setattr(viewer, "MAX_ARTIFACT_BYTES", 20)
    with pytest.raises(ArchiveError, match="大小上限"):
        read_sector_research_report(directory, tmp_path)


@pytest.mark.parametrize("sections", [["not-a-section"], [["坏表格", [["table", [["重复", "重复"], [[1, 2]]]]]]],
    [["坏表格", [["table", [["字段"], [[1, 2]]]]]]], [["任意执行", [["html", "<script>bad()</script>"]]]]])
def test_malformed_sections_never_count_as_readable(tmp_path, sections):
    directory = fixture(tmp_path)
    rewrite(directory, "sector_research_report.json", {"sections": sections})
    assert scan_sector_research_reports(tmp_path)[0] == []


def test_untrusted_paragraph_is_text_not_an_executed_html_or_remote_image(tmp_path, monkeypatch):
    directory = fixture(tmp_path)
    payload = '<script>bad()</script> ![remote](https://example.invalid/not-requested)'
    rewrite(directory, "sector_research_report.json", {"sections": [["不可信原文", [["paragraph", payload]]]]})
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("viewer must not fetch a displayed URL"))
    at = app(tmp_path)
    assert not at.exception
    assert any("\\<script\\>" in element.value for element in at.markdown)
    assert not at.get("imgs") and not at.get("iframe")
