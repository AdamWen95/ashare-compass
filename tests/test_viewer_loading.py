"""Offline loading regressions: avoid repeated large snapshot reads, never stale checks."""

from collections import Counter
import hashlib
import json

import pytest

from ashare_daily import viewer
from test_m4_viewer import app, write_report, write_run


def register_snapshot(directory, payload=None):
    """Register a synthetic frozen input; no model or real market data is used."""
    data = json.dumps(payload if payload is not None else {
        "purpose": "production", "production_eligible": True,
        "schema_version": "OFFLINE-loading-test",
        "rows": [{"symbol": "OFFLINE", "observations": list(range(500))} for _ in range(30)],
    }).encode()
    (directory / "input_snapshot.json").write_bytes(data)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["input_snapshot.json"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_archive_scan_reads_each_frozen_input_once_but_rechecks_next_scan(tmp_path, monkeypatch):
    directories = [write_report(tmp_path, version=f"OFFLINE-{i}") for i in range(3)]
    for directory in directories:
        register_snapshot(directory)
    original = viewer._bytes
    reads = Counter()

    def counted(path, root, limit):
        if path.name == "input_snapshot.json":
            reads[path] += 1
        return original(path, root, limit)

    monkeypatch.setattr(viewer, "_bytes", counted)
    archives, problems = viewer.scan_reports(tmp_path)
    assert len(archives) == 3 and not problems
    assert reads == Counter({directory / "input_snapshot.json": 1 for directory in directories})
    archives, problems = viewer.scan_reports(tmp_path)
    assert len(archives) == 3 and not problems
    assert reads == Counter({directory / "input_snapshot.json": 2 for directory in directories})


@pytest.mark.parametrize("rehashed", [False, True])
def test_rescan_does_not_reuse_trust_after_snapshot_changes(tmp_path, rehashed):
    directory = write_report(tmp_path)
    register_snapshot(directory)
    assert len(viewer.scan_reports(tmp_path)[0]) == 1
    if rehashed:
        register_snapshot(directory, {"purpose": "engineering_validation"})
    else:
        (directory / "input_snapshot.json").write_text("changed after scan", encoding="utf-8")
    archives, problems = viewer.scan_reports(tmp_path)
    assert not archives and len(problems) == 1
    assert ("工程" if rehashed else "哈希") in problems[0]["问题"]


@pytest.mark.parametrize("rehashed", [False, True])
def test_export_rechecks_snapshot_despite_previous_successful_scan(tmp_path, rehashed):
    directory = write_report(tmp_path)
    register_snapshot(directory)
    archive = viewer.scan_reports(tmp_path)[0][0]
    if rehashed:
        register_snapshot(directory, {"purpose": "engineering_validation"})
    else:
        (directory / "input_snapshot.json").write_text("changed after scan", encoding="utf-8")
    with pytest.raises(viewer.ArchiveError, match="工程" if rehashed else "哈希"):
        viewer.artifact_bytes(archive, "daily_brief.md")


@pytest.mark.parametrize("name", list(viewer.EXPORTS))
def test_scan_still_checks_every_public_artifact_with_snapshot(tmp_path, name):
    directory = write_report(tmp_path)
    register_snapshot(directory)
    (directory / name).write_text("OFFLINE damaged public artifact", encoding="utf-8")
    archives, problems = viewer.scan_reports(tmp_path)
    assert not archives and len(problems) == 1
    assert "哈希" in problems[0]["问题"]


def test_each_standalone_export_rechecks_current_snapshot(tmp_path, monkeypatch):
    directory = write_report(tmp_path)
    register_snapshot(directory)
    archive = viewer.scan_reports(tmp_path)[0][0]
    original = viewer._bytes
    reads = []

    def counted(path, root, limit):
        if path.name == "input_snapshot.json":
            reads.append(path)
        return original(path, root, limit)

    monkeypatch.setattr(viewer, "_bytes", counted)
    for name in viewer.EXPORTS:
        assert viewer.artifact_bytes(archive, name)
    assert len(reads) == len(viewer.EXPORTS)


def test_current_observation_does_not_scan_or_render_unselected_archive_families(tmp_path, monkeypatch):
    from test_observation_daily import publish
    publish(tmp_path, [("pass", "pass")])
    output = tmp_path / "outputs"
    write_report(output)

    def unused(*args, **kwargs):
        pytest.fail("unselected archive families and diagnostics must remain unloaded")

    for name in ("scan_reports", "scan_sector_reports", "scan_sector_research_reports", "_run_details", "artifact_bytes"):
        monkeypatch.setattr(viewer, name, unused)
    at = app(output)
    assert not at.exception
    assert len(at.get("download_button")) == 0
    at.button(key="refresh_display").click().run()
    assert not at.exception


def test_route_reads_only_the_explicitly_selected_archive_family(tmp_path, monkeypatch):
    from test_observation_daily import publish
    from test_sector_daily_viewer import write_sector
    from test_sector_research_viewer import fixture
    publish(tmp_path)
    output = tmp_path / "outputs"
    write_report(output)
    write_sector(output)
    fixture(output)
    fixture(output, engineering=True)
    calls = []
    for name in ("scan_observation_reports", "scan_reports", "scan_sector_reports", "scan_sector_research_reports"):
        original = getattr(viewer, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls.append((_name, kwargs.get("purpose", "production")))
            return _original(*args, **kwargs)

        monkeypatch.setattr(viewer, name, counted)
    at = app(output)
    assert not at.exception and calls == [("scan_observation_reports", "production")]
    for section, expected in (("行业研究历史", [("scan_sector_research_reports", "production")]),
                              ("行业就绪历史", [("scan_sector_reports", "production")]),
                              ("历史样本日报", [("scan_reports", "production")]),
                              ("工程验收（非生产）", [("scan_sector_research_reports", "engineering_validation")]),
                              ("运行记录", [])):
        calls.clear()
        at.selectbox(key="archive_section").select(section).run()
        assert not at.exception and calls == expected


def test_legacy_overview_prepares_no_hidden_views_or_downloads(tmp_path, monkeypatch):
    write_report(tmp_path)
    original = viewer.artifact_bytes
    exports = []

    def exported(archive, name):
        exports.append(name)
        return original(archive, name)

    def unused(*args, **kwargs):
        pytest.fail("inactive historical report views must not execute")

    monkeypatch.setattr(viewer, "artifact_bytes", exported)
    for name in ("_candidates", "_evidence", "_run_details"):
        monkeypatch.setattr(viewer, name, unused)
    at = app(tmp_path)
    assert not at.exception and not exports
    assert len(at.get("download_button")) == 0
    at.radio(key="legacy_report_view").set_value("导出").run()
    assert not at.exception and exports == list(viewer.EXPORTS)
    assert len(at.get("download_button")) == 6


def test_large_run_details_are_one_selected_component_not_thousands_of_texts(tmp_path):
    # Reproduce a broad market diagnostic without network or real market data.
    rows = [{"security_id": f"OFFLINE-{i}", "missing_dates": ["2026-09-01"] * 8,
             "status": "pending", "api_key": "OFFLINE-secret"} for i in range(2000)]
    for number in range(2):
        write_run(tmp_path, f"run-{number}", status="partial", started_at=f"2026-09-0{number + 1}T21:00:00+08:00",
                  target_trade_date=f"2026-09-0{number + 1}", f2_result={"rows": rows})
    at = app(tmp_path)
    assert not at.exception and not at.json
    at.selectbox(key="archive_section").select("运行记录").run()
    assert not at.exception and len(at.json) == 1 and len(at.text) < 20
    assert "OFFLINE-secret" not in at.json[0].value
    assert json.loads(at.json[0].value)["f2_result"]["rows"][0]["api_key"] == "[已隐藏]"
    at.selectbox(key="run_detail_version").select(1).run()
    assert not at.exception and len(at.json) == 1


def test_nested_readiness_diagnostics_do_not_expand_to_one_widget_per_leaf(tmp_path):
    from test_sector_daily_viewer import write_sector
    report = {"history": {"securities": [{"symbol": f"OFFLINE-{i}", "missing_dates": list(range(50))}
                                       for i in range(500)]}}
    write_sector(tmp_path, report=report)
    at = app(tmp_path)
    assert not at.exception and len(at.json) == 1 and len(at.text) < 20
    assert len(at.get("download_button")) == 0
