"""Offline production isolation contracts; no real collection or publication."""
import hashlib
import json
import shutil
import socket

import pytest

from ashare_daily.artifact_purpose import is_production_artifact
from ashare_daily.operations.daily import read_runs, publish_report, valid_report
from ashare_daily.viewer import (ArchiveError, artifact_bytes, read_report, read_sector_report,
    scan_reports, scan_runs, scan_sector_reports, sector_artifact_bytes, unresolved_daily_failure)
from test_m4_viewer import app, texts, offline_report, write_report, write_run
from test_sector_daily_viewer import write_sector


VALIDATION_ID = "validation-sector-2026-09-11-" + "a" * 20
MARKERS = [{"purpose": "engineering_validation"}, {"production_eligible": False},
           {"selection_id": VALIDATION_ID}, {"purpose": "unrecognized_purpose"},
           {"purpose": None}, {"production_eligible": 1}]


def rewrite(directory, name, value):
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    (directory / name).write_bytes(body)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][name] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def publication_fixture(output):
    directory = write_report(output)
    for name in ("input_snapshot.json", "model_responses.json"):
        rewrite(directory, name, {})
    return {"run_directory": str(directory), "trade_date": directory.parent.name}


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("where", ["report", "manifest", "selection"])
def test_engineering_or_unknown_readiness_cannot_be_disguised_in_production_directory(tmp_path, marker, where):
    directory = write_sector(tmp_path, selection="renamed-production-sector")
    if where == "selection":
        # A copied selection retains its independent marker, even though the
        # wrapper and filename now look like legacy production artifacts.
        (directory.parent.parent / "sector_selection.json").write_text(json.dumps(marker), encoding="utf8")
    elif where == "manifest":
        path = directory / "manifest.json"
        value = json.loads(path.read_text(encoding="utf8"))
        value.update(marker)
        path.write_text(json.dumps(value), encoding="utf8")
    else:
        value = json.loads((directory / "sector_data_readiness.json").read_text(encoding="utf8"))
        # Keep wrapper selection identity valid; nested source metadata must
        # still reject a validation ID/purpose when the manifest omits it.
        value.update(purpose="production", production_eligible=True, selection=marker)
        rewrite(directory, "sector_data_readiness.json", value)
    reports, problems = scan_sector_reports(tmp_path)
    assert reports == [] and problems
    with pytest.raises(ArchiveError):
        read_sector_report(directory, tmp_path)


def test_validation_prefix_remains_excluded_when_all_purpose_fields_are_omitted(tmp_path):
    directory = write_sector(tmp_path, selection=VALIDATION_ID, report={})
    assert scan_sector_reports(tmp_path)[0] == []
    with pytest.raises(ArchiveError, match="工程验收"):
        read_sector_report(directory, tmp_path)


def test_copied_and_renamed_selection_validation_schema_is_not_legacy_production(tmp_path):
    original = write_sector(tmp_path / "isolated", selection=VALIDATION_ID, report={})
    (original.parent.parent / "sector_selection.json").write_text(json.dumps({
        "schema_version": "f3s-validation-selection-v1"}), encoding="utf8")
    destination = tmp_path / "research/sse_szse_a/sector_first/renamed"
    shutil.copytree(original.parent.parent, destination)
    manifest_path = destination / "reports/offline-run-1/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    manifest["selection_id"] = "renamed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    reports, problems = scan_sector_reports(tmp_path)
    assert reports == [] and problems


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("where", ["report", "manifest", "input_snapshot", "selection"])
def test_old_m3_shell_does_not_allow_engineering_publication_or_today_display(tmp_path, marker, where):
    source = publication_fixture(tmp_path)
    directory = tmp_path / "research/m3" / source["trade_date"] / "offline-version-1"
    if where == "manifest":
        path = directory / "manifest.json"
        value = json.loads(path.read_text(encoding="utf8"))
        value.update(marker)
        path.write_text(json.dumps(value), encoding="utf8")
    elif where == "selection":
        (directory / "sector_selection.json").write_text(json.dumps(marker), encoding="utf8")
    else:
        name = "daily_brief.json" if where == "report" else "input_snapshot.json"
        value = offline_report() if where == "report" else {}
        value.update(purpose="production", production_eligible=True, selection=marker)
        rewrite(directory, name, value)
    assert not valid_report(source, anchor=tmp_path)
    with pytest.raises(ValueError, match="校验失败"):
        publish_report(source, tmp_path / "publication", {"run_id": "should-not-exist"})
    assert not (tmp_path / "publication").exists()
    assert scan_reports(tmp_path)[0] == []


@pytest.mark.parametrize("sector", [False, True])
def test_export_rechecks_purpose_after_initial_read(tmp_path, sector):
    directory = write_sector(tmp_path) if sector else write_report(tmp_path)
    archive = read_sector_report(directory, tmp_path) if sector else read_report(directory, tmp_path)
    name = "sector_data_readiness.json" if sector else "daily_brief.json"
    value = json.loads((directory / name).read_text(encoding="utf8"))
    value.update(purpose="engineering_validation", production_eligible=False)
    rewrite(directory, name, value)
    with pytest.raises(ArchiveError, match="工程验收"):
        (sector_artifact_bytes if sector else artifact_bytes)(archive,
            "sector_data_readiness.md" if sector else "daily_brief.md")


@pytest.mark.parametrize("marker", MARKERS)
def test_engineering_run_cannot_clear_production_failure_or_enter_default_history(tmp_path, marker):
    day = "2026-09-11"
    production = {"run_id": "production-failed", "target_trade_date": day, "started_at": day + "T21:00:00+08:00",
                  "status": "failed", "exit_code": 2}
    disguised = {"run_id": "renamed-success", "target_trade_date": day, "started_at": day + "T22:00:00+08:00",
                 "status": "f2_complete", "generation_status": "ok", "exit_code": 0, "report": marker}
    for row in (production, disguised):
        write_run(tmp_path, row["run_id"], **{key: value for key, value in row.items() if key != "run_id"})
    rows, issues = scan_runs(tmp_path)
    assert not issues and [row["run_id"] for row in rows] == ["production-failed"]
    assert [row["run_id"] for row in read_runs(tmp_path / "research/m4")] == ["production-failed"]
    assert unresolved_daily_failure([production, disguised], day=day)["run_id"] == "production-failed"


def test_legacy_report_and_explicit_production_are_compatible_but_permission_purpose_is_separate(tmp_path):
    source = publication_fixture(tmp_path)
    assert valid_report(source, anchor=tmp_path)
    assert len(scan_reports(tmp_path)[0]) == 1
    assert is_production_artifact({})
    assert is_production_artifact({"purpose": "production", "production_eligible": True,
        "configuration": {"sina": {"purpose": "personal_noncommercial_local_research"}}})
    assert not is_production_artifact({"purpose": "unknown"})
    assert not is_production_artifact({"configuration": {"sina": {"purpose": "engineering_validation"}}})


def test_disguised_engineering_artifacts_are_excluded_from_ui_readonly(tmp_path, monkeypatch):
    report = offline_report()
    report.update(purpose="engineering_validation", production_eligible=False, title="MUST NOT DISPLAY F3-S VALIDATION")
    write_report(tmp_path, report)
    write_sector(tmp_path, report={"purpose": "engineering_validation", "title": "MUST NOT DISPLAY F3-S VALIDATION"})
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("viewer must remain read-only"))
    result = app(tmp_path)
    assert not result.exception
    assert "MUST NOT DISPLAY F3-S VALIDATION" not in texts(result)
    assert "暂无可阅读报告" in texts(result)
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
