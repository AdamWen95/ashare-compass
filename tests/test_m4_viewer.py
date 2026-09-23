"""Offline UI fixtures and separately identified, read-only real archive checks."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import socket
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from ashare_daily.viewer import ArchiveError, EXPORTS, artifact_bytes, public_value, read_report, scan_reports, scan_runs, unresolved_daily_failure


ROOT = Path(__file__).resolve().parents[1]


def offline_report(day="2026-09-08", generated="2026-09-09T16:00:00+08:00"):
    return {
        "schema_version": "m3-report-v1", "verification_kind": "offline_test", "title": "离线 UI 测试资料",
        "notice": "OFFLINE TEST：仅验证界面，不是真实新闻或分析", "trade_date": day, "actual_market_date": day,
        "actual_generated_at": generated, "query_start_at": day + "T00:00:00+08:00", "cutoff_at": day + "T23:59:59+08:00",
        "historical_reconstruction": True, "input_snapshot_id": "offline-test-snapshot", "status": "partial",
        "statuses": {"market": "partial", "messages": "partial", "model": "not_run", "generation": "ok", "citation_validation": "not_run"},
        "market": {"counts": {"stock_count": 0, "market_data_success_count": 0, "candidate_count": 0, "pending_eligibility_count": 0},
                   "candidates": [], "pending_eligibility": [], "evaluations": [], "benchmark": {}, "strategy_config": {}},
        "analysis": {"accepted_claims": []}, "model_run": {"status": "not_run", "call_count": 0, "configuration": {"key_present": False}},
        "source_health": [], "source_registry": [], "evidence_catalog": [], "research_objects": [], "gaps": ["离线测试缺口"],
    }


def write_report(output, report=None, version="offline-version-1", *, m4=False):
    report = report or offline_report()
    root = output / ("research/m4/reports" if m4 else "research/m3")
    directory = root / report["trade_date"] / version
    directory.mkdir(parents=True)
    content = {"daily_brief.json": json.dumps(report, ensure_ascii=False), "daily_brief.md": "# OFFLINE TEST",
               "daily_brief.html": "<html><body>OFFLINE TEST</body></html>", "screening_audit.csv": "symbol,status\n",
               "claim_evidence_audit.csv": "claim_id,evidence_id\n", "evidence_catalog.json": '{"evidence": []}'}
    manifest = {"schema_version": "m3-report-manifest-v1", "files": {}}
    for name, text in content.items():
        data = text.encode("utf-8")
        (directory / name).write_bytes(data)
        manifest["files"][name] = hashlib.sha256(data).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def write_run(output, identity, **values):
    directory = output / "research/m4/runs" / identity
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps({"run_id": identity, **values}), encoding="utf-8")


def app(output):
    source = "from pathlib import Path\nfrom ashare_daily.viewer import render_app\nrender_app(Path(" + repr(str(output)) + "))\n"
    return AppTest.from_string(source, default_timeout=15).run()


def texts(at):
    return "\n".join(str(element.value) for name in ["text", "info", "warning", "caption", "subheader", "metric"] for element in getattr(at, name))


def test_empty_archive_is_readable_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("只读界面不能联网"))
    at = app(tmp_path)
    assert not at.exception
    assert "暂无可阅读报告" in texts(at)
    assert "尚无 M4 日任务记录" in texts(at)
    assert not list(tmp_path.rglob("*"))


def test_zero_candidates_partial_and_model_missing_are_separate(tmp_path):
    report = offline_report()
    report["model_run"]["status"] = report["statuses"]["model"] = "missing_configuration"
    write_report(tmp_path, report)
    at = app(tmp_path)
    assert not at.exception
    assert "部分覆盖" in texts(at)
    assert "历史资料补采研究" in texts(at)
    assert len(at.get("download_button")) == 0
    at.radio(key="legacy_report_view").set_value("候选与核验").run()
    assert "正式量价预候选为 0" in texts(at)
    at.radio(key="legacy_report_view").set_value("导出").run()
    assert not at.exception and len(at.get("download_button")) == 6


def test_failed_today_and_readable_old_report_both_visible(tmp_path):
    write_report(tmp_path)
    write_run(tmp_path, "failure", started_at="2026-09-09T21:00:00+08:00", status="failed", exit_code=2,
              target_trade_date="2026-09-09", failure_reason="OFFLINE TEST：网络失败")
    write_run(tmp_path, "preview", started_at="2026-09-09T21:30:00+08:00", status="dry_run", exit_code=0)
    at = app(tmp_path)
    assert not at.exception
    assert "失败" in texts(at) and "2026-09-08" in texts(at)
    assert "最新记录为 dry-run" in texts(at)


def test_failure_then_not_due_keeps_unresolved_alert_and_latest_status(tmp_path):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    write_report(tmp_path)
    write_run(tmp_path, "failure", started_at=today + "T17:00:00+08:00", status="calendar_unavailable", generation_status="not_run",
              exit_code=2, target_trade_date=today)
    write_run(tmp_path, "scheduled-test", started_at=today + "T17:30:00+08:00", status="not_due", generation_status="not_run",
              exit_code=0, target_trade_date=today)
    at = app(tmp_path)
    assert not at.exception
    assert "尚未到盘后运行时间" in texts(at)
    assert "今日尚未解决的失败：交易日历不可用" in texts(at)
    assert "当前可阅读报告的日期" in texts(at)


def test_failure_then_same_target_generation_success_clears_alert(tmp_path):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    write_report(tmp_path)
    write_run(tmp_path, "failure", started_at=today + "T17:00:00+08:00", status="calendar_unavailable", generation_status="not_run",
              exit_code=2, target_trade_date=today)
    write_run(tmp_path, "generated", started_at=today + "T21:00:00+08:00", status="partial", generation_status="ok",
              exit_code=1, target_trade_date=today)
    at = app(tmp_path)
    assert not at.exception
    assert "今日尚未解决的失败" not in texts(at)
    assert "部分覆盖" in texts(at)


def test_other_date_success_or_reuse_does_not_clear_daily_failure():
    rows = [
        {"run_id": "failure", "started_at": "2026-09-09T17:00:00+08:00", "target_trade_date": "2026-09-09", "status": "failed", "exit_code": 2},
        {"run_id": "historical", "started_at": "2026-09-09T18:00:00+08:00", "target_trade_date": "2026-09-08", "status": "partial", "generation_status": "ok"},
        {"run_id": "reuse", "started_at": "2026-09-09T18:30:00+08:00", "target_trade_date": "2026-09-09", "status": "reused", "generation_status": "ok"},
    ]
    assert unresolved_daily_failure(rows, day="2026-09-09")["run_id"] == "failure"
    assert unresolved_daily_failure(rows, day="2026-09-10") is None


@pytest.mark.parametrize("status,code", [("f2_partial", 2), ("f2_complete", 0)])
def test_scoped_f2_status_leaves_old_frozen_report_unchanged(tmp_path, monkeypatch, status, code):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    write_report(tmp_path)
    write_run(tmp_path, "OFFLINE-old-all-a", scope="all_a", started_at=today + "T17:00:00+08:00",
              status="universe_blocked", exit_code=2, target_trade_date=today, generation_status="not_run")
    write_run(tmp_path, "OFFLINE-sse-szse-f2", scope="sse_szse_a", started_at=today + "T21:00:00+08:00",
              status=status, exit_code=code, target_trade_date=today, generation_status="not_run",
              collection_ready=True, research_ready=False)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("查看F2记录不能联网"))
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}
    at = app(tmp_path)
    assert not at.exception
    visible = texts(at)
    assert "沪深A股全市场，暂不含北交所" in visible
    assert "2026-09-08" in visible
    assert "今日尚未解决的失败：配置范围的名单" not in visible
    if code == 0:
        assert "F2行情核验完成，尚未筛选、研究或生成新日报" in visible
        assert "今日尚未解决的失败" not in visible
    else:
        assert "今日尚未解决的失败：F2行情部分完成" in visible
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}


def test_date_version_refresh_and_search_only_read(tmp_path, monkeypatch):
    write_report(tmp_path)
    write_report(tmp_path, offline_report("2026-09-07"), "offline-version-2", m4=True)
    # Any model/source import would violate the viewer boundary. A network call
    # is forbidden even if a future refactor accidentally adds one.
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("只读界面不能联网"))
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}
    at = app(tmp_path)
    at.selectbox(key="report_date").select("2026-09-07").run()
    at.button(key="refresh_display").click().run()
    at.radio(key="legacy_report_view").set_value("候选与核验").run()
    at.text_input(key="stock_search").set_value("测试").run()
    assert not at.exception
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert at.selectbox(key="report_date").value == "2026-09-07"


@pytest.mark.parametrize("file", ["daily_brief.json", "daily_brief.md", "daily_brief.html", "screening_audit.csv", "manifest.json"])
def test_damaged_report_hidden_with_clear_reason(tmp_path, file):
    directory = write_report(tmp_path)
    (directory / file).write_text("damaged", encoding="utf-8")
    reports, problems = scan_reports(tmp_path)
    assert not reports and len(problems) == 1
    at = app(tmp_path)
    assert not at.exception and "损坏的报告版本" in texts(at)
    assert "暂无可阅读报告" in texts(at)


def test_one_damaged_version_does_not_hide_valid_old_version(tmp_path):
    write_report(tmp_path)
    directory = write_report(tmp_path, version="offline-version-2")
    (directory / "daily_brief.json").write_text("{}", encoding="utf-8")
    reports, problems = scan_reports(tmp_path)
    assert [entry.version for entry in reports] == ["offline-version-1"]
    assert len(problems) == 1


@pytest.mark.parametrize("name", ["../.env", ".env", "input_snapshot.json", "model_responses.json", "C:\\Windows\\system.ini", "daily_brief.json/../.env"])
def test_export_only_exact_registered_public_names(tmp_path, name):
    directory = write_report(tmp_path)
    archive = read_report(directory, tmp_path / "research/m3")
    with pytest.raises(ArchiveError, match="白名单"):
        artifact_bytes(archive, name)


def test_download_rechecks_file_hash(tmp_path):
    directory = write_report(tmp_path)
    archive = read_report(directory, tmp_path / "research/m3")
    (directory / "daily_brief.md").write_text("changed after reading", encoding="utf-8")
    with pytest.raises(ArchiveError, match="哈希"):
        artifact_bytes(archive, "daily_brief.md")


def test_malicious_manifest_cannot_redirect_export(tmp_path):
    directory = write_report(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["daily_brief.json"] = {"path": "../../.env", "sha256": "0" * 64}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert scan_reports(tmp_path)[1]


def test_root_escape_refused_before_reading(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ArchiveError, match="越出"):
        read_report(outside, tmp_path / "output")


def test_size_limit(tmp_path, monkeypatch):
    directory = write_report(tmp_path)
    import ashare_daily.viewer as viewer
    monkeypatch.setattr(viewer, "MAX_ARTIFACT_BYTES", 10)
    with pytest.raises(ArchiveError, match="大小"):
        read_report(directory, tmp_path / "research/m3")


@pytest.mark.parametrize("payload", [{"api_key": "credential-offline-test"}, {"reason": "sk-OFFLINE_FAKE_CREDENTIAL_01234567890"}])
def test_secret_in_artifact_blocks_export_and_read(tmp_path, payload):
    report = offline_report()
    report["model_run"].update(payload)
    write_report(tmp_path, report)
    entries, problems = scan_reports(tmp_path)
    assert not entries and "凭据" in problems[0]["问题"]


def test_run_secret_redacted_and_no_log_file_is_followed(tmp_path):
    write_run(tmp_path, "offline-run", started_at="2026-09-09T21:00:00+08:00", status="failed",
              error="Bearer OFFLINE-TEST-TOKEN-0123456789", api_key="offline-private", log_path="../../.env")
    runs, problems = scan_runs(tmp_path)
    assert not problems
    serialized = json.dumps(runs)
    assert "OFFLINE-TEST-TOKEN" not in serialized and "offline-private" not in serialized
    assert public_value({"key_present": True, "api_key": "x"}) == {"key_present": True, "api_key": "[已隐藏]"}


def test_corrupt_run_not_silently_successful(tmp_path):
    write_run(tmp_path, "broken", status="failed")
    (tmp_path / "research/m4/runs/broken/result.json").write_text("[", encoding="utf-8")
    runs, problems = scan_runs(tmp_path)
    assert not runs and problems
    assert not app(tmp_path).exception


def test_untrusted_html_and_markdown_are_literal_text(tmp_path):
    report = offline_report()
    attack = '<script>alert("OFFLINE TEST")</script> ![pixel](https://example.invalid/tracker)'
    report["analysis"]["accepted_claims"] = [{"claim_id": "offline-claim", "claim_type": "fact", "text": attack, "citations": []}]
    write_report(tmp_path, report)
    at = app(tmp_path)
    assert not at.exception
    # Reading prose is escaped Markdown, never a raw HTML/image instruction.
    from ashare_daily.viewer import _label
    assert _label(attack) in [item.value for item in at.markdown]
    from ashare_daily.viewer_dashboard import STYLE, MASTHEAD, SIDEBAR_BRAND
    # Only literal application chrome may use HTML in the legacy reader.
    trusted_chrome = {STYLE.strip(), MASTHEAD, SIDEBAR_BRAND}
    assert all(not item.proto.allow_html or item.value in trusted_chrome for item in at.markdown)


def test_external_title_markdown_cannot_load_tracking_image(tmp_path):
    report = offline_report()
    report["evidence_catalog"] = [{"title": "![外部跟踪](https://example.invalid/tracker)", "content": "OFFLINE TEST"}]
    write_report(tmp_path, report)
    at = app(tmp_path)
    at.radio(key="legacy_report_view").set_value("证据目录").run()
    assert not at.exception
    label = next(item.label for item in at.expander if "tracker" in item.label)
    assert label.startswith(r"\!\[")


def test_formal_research_object_not_mixed_into_event_observation(tmp_path):
    report = offline_report()
    report["research_objects"] = [{"symbol": "sh.600000", "name": "离线示例", "path": "technical_candidate", "association_evidence_ids": []}]
    write_report(tmp_path, report)
    at = app(tmp_path)
    at.radio(key="legacy_report_view").set_value("候选与核验").run()
    assert not at.exception
    assert "本次没有资料关联充分的事件观察对象" in texts(at)


def test_unknown_eligibility_separate_from_formal_candidates(tmp_path):
    report = offline_report()
    row = {"symbol": "sh.600000", "name": "离线示例，非实际证券分析", "technical_screen_status": "pass", "eligibility_status": "pending",
           "status": "data_insufficient", "data_issues": ["离线：退市整理期证据未知"], "conditions": []}
    report["market"].update(evaluations=[row], pending_eligibility=[row])
    report["market"]["counts"].update(stock_count=1, pending_eligibility_count=1)
    write_report(tmp_path, report)
    at = app(tmp_path)
    at.radio(key="legacy_report_view").set_value("候选与核验").run()
    assert not at.exception
    assert "正式量价预候选为 0" in texts(at)
    assert "以下股票不计入正式候选" in texts(at)


@pytest.mark.parametrize("key, value", [("market", []), ("analysis", None), ("source_health", ["bad"]), ("model_run", "bad")])
def test_malformed_schema_never_white_screen(tmp_path, key, value):
    report = offline_report()
    report[key] = value
    write_report(tmp_path, report)
    at = app(tmp_path)
    assert not at.exception
    assert "暂无可阅读报告" in texts(at)


def test_no_collector_or_model_imports_in_viewer():
    import ast
    source = (ROOT / "src/ashare_daily/viewer.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(any(token in module for token in ("research", "providers", "operations")) for module in modules)


def test_existing_real_m3_archive_read_only(tmp_path, monkeypatch):
    """Actual frozen archive, never a claim of new collection/model verification."""
    source = ROOT / "outputs/research/m3/2026-09-08/20260909T162423161713-9381f5bf"
    if not source.exists():
        pytest.skip("本机 M3 真实验收存档不存在，不能用模拟数据冒充")
    directory = tmp_path / "research/m3" / source.parent.name / source.name
    directory.mkdir(parents=True)
    for name in [*EXPORTS, "manifest.json"]:
        shutil.copyfile(source / name, directory / name)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("读取真实存档不得重调模型"))
    reports, problems = scan_reports(tmp_path)
    assert len(reports) == 1 and not problems
    assert reports[0].report["model_run"]["call_count"] == 1
    assert len(reports[0].report["analysis"]["accepted_claims"]) == 3
    assert reports[0].report["market"]["counts"]["stock_count"] == 100
    at = app(tmp_path)
    assert not at.exception
    at.radio(key="legacy_report_view").set_value("候选与核验").run()
    assert "正式量价预候选为 0" in texts(at)
    at.radio(key="legacy_report_view").set_value("导出").run()
    assert not at.exception and len(at.get("download_button")) == 6
