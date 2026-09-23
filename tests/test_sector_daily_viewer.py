"""F2-S1 route and read-only UI contracts; every artifact is an offline fixture."""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ashare_daily.operations.daily import DailyConfig, read_runs, resolve_times, run_daily
from ashare_daily.viewer import (
    ArchiveError, SECTOR_EXPORTS, read_sector_report, scan_sector_reports, sector_artifact_bytes,
)
from test_m4_viewer import app, texts, write_report


DAY = "2026-09-11"
NOW = datetime.fromisoformat("2026-09-12T22:00:00+08:00")


def daily_config(root, **updates):
    config = {"workflow_version": "f2s1-offline-route", "scope_mode": "sse_szse_a",
              "research_mode": "sector_first", "sector_config": "sector.json",
              "f2_config": "must-not-run-full-universe.json", "trigger_time": "21:00", **updates}
    (root / "daily.json").write_text(json.dumps(config), encoding="utf8")
    return config


def sector_stub(monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"status": "dry_run" if kwargs["dry_run"] else "pending", "exit_code": 0,
                "research_mode": "sector_first", "model_summary": {"call_count": 0}}

    monkeypatch.setitem(sys.modules, "ashare_daily.sector_pipeline", SimpleNamespace(run_sector_daily=run))
    return calls


def test_old_daily_config_default_is_unchanged_full_market():
    config = DailyConfig()
    assert config.scope_mode == "sample" and config.research_mode == "full_market"
    assert config.sector_config is None and config.trigger_time == "21:00"


@pytest.mark.parametrize("updates", [
    {"scope_mode": "all_a"}, {"scope_mode": "sample"}, {"sector_config": None},
    {"sector_config": "  "}, {"trigger_time": "20:00"}, {"research_mode": "full_market"},
])
def test_sector_mode_requires_explicit_config_scope_and_existing_cutoff(tmp_path, updates):
    with pytest.raises(ValidationError):
        DailyConfig.model_validate(daily_config(tmp_path, **updates))


def test_sector_daily_routes_before_all_universe_and_does_not_read_model_settings(tmp_path, monkeypatch):
    import ashare_daily.operations.full_market as full_market
    import ashare_daily.operations.daily as daily
    import ashare_daily.market_pipeline as market
    calls = sector_stub(monkeypatch)
    for module, name in ((full_market, "run_full_market_daily"), (market, "run_market"), (daily, "load_model_settings")):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: pytest.fail("sector route must not enter legacy full-market/model path"))
    daily_config(tmp_path)
    result = run_daily(project=tmp_path, config_path="daily.json", target=date.fromisoformat(DAY),
                       cutoff=DAY + "T21:00:00+08:00", now=NOW, dry_run=True, f2_max_seconds=123)
    assert result["research_mode"] == "sector_first" and len(calls) == 1
    assert calls[0]["max_seconds"] == 123 and calls[0]["target"] == date.fromisoformat(DAY)
    assert calls[0]["now"] == NOW and calls[0]["scheduled"] is False
    assert calls[0]["cutoff"] == DAY + "T21:00:00+08:00"
    assert not (tmp_path / "data").exists()


def test_sector_runtime_limit_does_not_require_the_obsolete_full_market_config(tmp_path, monkeypatch):
    calls = sector_stub(monkeypatch)
    daily_config(tmp_path, f2_config=None)
    run_daily(project=tmp_path, config_path="daily.json", now=NOW, f2_max_seconds=60)
    assert len(calls) == 1 and calls[0]["max_seconds"] == 60


@pytest.mark.parametrize("value", [0, -1, 14401, True, float("nan")])
def test_sector_runtime_limit_stays_bounded_before_dispatch(tmp_path, monkeypatch, value):
    calls = sector_stub(monkeypatch)
    daily_config(tmp_path)
    result = run_daily(project=tmp_path, config_path="daily.json", now=NOW, f2_max_seconds=value)
    assert result["status"] == "configuration_failed" and result["exit_code"] == 2
    assert calls == [] and result["model_summary"]["call_count"] == 0


@pytest.mark.parametrize("flags", [{"scheduled": True}, {"scheduled": True, "dry_run": True}, {"sample_mode": True}, {"services": object()}])
def test_sector_route_rejects_scheduling_and_old_sample_services(tmp_path, monkeypatch, flags):
    calls = sector_stub(monkeypatch)
    daily_config(tmp_path)
    result = run_daily(project=tmp_path, config_path="daily.json", now=NOW, **flags)
    assert result["status"] == "configuration_failed" and not result["external_calls_allowed"]
    assert calls == []


def test_existing_cutoff_marks_historical_supplement_without_changing_target(tmp_path):
    config = DailyConfig.model_validate(daily_config(tmp_path))
    times = resolve_times(date.fromisoformat(DAY), None, None, NOW, config, [])
    assert times["cutoff_at"] == DAY + "T21:00:00+08:00" and times["historical_run"] is True
    assert times["target_trade_date"] == DAY and times["timezone"] == "Asia/Shanghai"


def test_daily_history_mode_filter_preserves_legacy_and_isolates_sector_records(tmp_path):
    for name, mode in (("legacy", None), ("full", "full_market"), ("sector", "sector_first")):
        directory = tmp_path / "runs" / name
        directory.mkdir(parents=True)
        row = {"run_id": name, "started_at": NOW.isoformat(), "scope": "sse_szse_a", "status": "running"}
        if mode:
            row["research_mode"] = mode
        (directory / "result.json").write_text(json.dumps(row), encoding="utf8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    assert {r["run_id"] for r in read_runs(tmp_path, scope="sse_szse_a", research_mode="full_market")} == {"legacy", "full"}
    assert {r["run_id"] for r in read_runs(tmp_path, scope="sse_szse_a", research_mode="sector_first")} == {"sector"}
    assert len(read_runs(tmp_path)) == 3
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*.json")}


def write_sector(output, *, selection="offline-f2s1-selection", version="offline-run-1", report=None, markdown=None):
    directory = output / "research/sse_szse_a/sector_first" / selection / "reports" / version
    directory.mkdir(parents=True)
    report = {"title": "OFFLINE TEST 行业就绪资料", "target_date": DAY, "selection_id": selection,
              "market_scope": "sse_szse_a", "research_mode": "sector_first", "status": "pending",
              "generated_at": NOW.isoformat(), "mode": "offline_test", "model_calls": 0,
              "counts": {"catalog": 20, "preselected": 6, "selected": 2, "selected_securities": 124,
                         "history_ready": 0, "adjustment_ready": 0, "risk_unknown": 124, "pending": 124},
              "limitations": ["OFFLINE TEST：不是真实板块覆盖"], "not_run": ["F3", "F4", "deployment"]} if report is None else report
    files = {"sector_data_readiness.json": json.dumps(report, ensure_ascii=False).encode("utf8"),
             "sector_data_readiness.md": (markdown or "# OFFLINE TEST\n行业成分历史与资格仍待核验。\n").encode("utf8")}
    for name, data in files.items():
        (directory / name).write_bytes(data)
    manifest = {"schema_version": "f2s1-readiness-manifest-v1", "selection_id": selection,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    return directory


def test_sector_archive_is_separate_from_old_reports_and_keeps_124_members(tmp_path):
    directory = write_sector(tmp_path)
    old = write_report(tmp_path)
    archives, errors = scan_sector_reports(tmp_path)
    assert not errors and len(archives) == 1
    assert archives[0].report["counts"]["selected_securities"] == 124
    assert archives[0].selection_id == "offline-f2s1-selection"
    assert directory != old
    for name in SECTOR_EXPORTS:
        assert sector_artifact_bytes(archives[0], name) == (directory / name).read_bytes()


@pytest.mark.parametrize("name", list(SECTOR_EXPORTS))
def test_sector_archive_tampering_blocks_display_and_download(tmp_path, name):
    directory = write_sector(tmp_path)
    archive = read_sector_report(directory, tmp_path)
    (directory / name).write_text("tampered", encoding="utf8")
    with pytest.raises(ArchiveError, match="哈希"):
        sector_artifact_bytes(archive, name)
    reports, errors = scan_sector_reports(tmp_path)
    assert reports == [] and errors


def test_sector_missing_counts_are_pending_and_never_inferred_zero(tmp_path):
    write_sector(tmp_path, report={})
    result = app(tmp_path)
    assert not result.exception
    assert "待核验" in texts(result)
    frame = result.dataframe[0].value
    assert all(frame["数量"] == "待核验")
    assert "模型调用记录待核验" in texts(result)


def test_sector_latest_pointer_cannot_follow_paths_outside_registered_archive(tmp_path):
    directory = write_sector(tmp_path)
    (directory.parent.parent / "latest_readiness.json").write_text(json.dumps({
        "report_directory": "../../../../../../.env", "manifest_sha256": "0" * 64}), encoding="utf8")
    archives, problems = scan_sector_reports(tmp_path)
    assert len(archives) == 1 and problems == []
    with pytest.raises(ArchiveError, match="白名单"):
        sector_artifact_bytes(archives[0], "../../../../.env")


@pytest.mark.parametrize("mutation", [
    {"market_scope": "all_a"}, {"research_mode": "full_market"}, {"selection_id": "other-selection"},
    {"target_date": "2026-99-99"}, {"generated_at": "2026-09-11T21:00:00"},
    {"api_key": "fake secret value"},
])
def test_sector_invalid_identity_date_or_secret_never_becomes_a_readable_report(tmp_path, mutation):
    write_sector(tmp_path, report=mutation)
    archives, errors = scan_sector_reports(tmp_path)
    assert not archives and errors


def test_sector_ui_refresh_and_selection_preserve_old_files_and_never_connect(tmp_path, monkeypatch):
    write_report(tmp_path)
    write_sector(tmp_path)
    write_sector(tmp_path, version="offline-run-2", markdown="![external](https://example.invalid/x) <script>bad()</script>")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("viewer must not connect"))
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}
    result = app(tmp_path)
    assert not result.exception and "沪深 A 股·板块精选研究" in texts(result)
    assert "隔离测试资料" in texts(result) and "个股筛选、公告核查及模型研究尚未执行" in texts(result)
    result.selectbox(key="sector_readiness_version").select(1).run()
    result.button(key="refresh_display").click().run()
    assert not result.exception
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}
