"""Frozen local artifacts, SQLite persistence, and safe independent reruns."""

import hashlib
import json
import sqlite3

import pytest

from ashare_daily.demo import build_demo_report
from ashare_daily.reports.publish import publish_report
from ashare_daily.schemas import DailyReport


def file_hashes(directory):
    return {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in directory.iterdir()
        if item.is_file()
    }


def test_publish_saves_valid_reports_and_sqlite(demo_report, tmp_path):
    directory = publish_report(demo_report, tmp_path)
    assert directory.parent == tmp_path / "demo" / "2026-09-09"
    assert {"daily_brief.json", "daily_brief.md", "daily_brief.html", "snapshot.json", "manifest.json"} <= set(file_hashes(directory))
    disk_report = DailyReport.model_validate_json((directory / "daily_brief.json").read_text(encoding="utf-8"))
    assert disk_report == demo_report
    for filename in ("snapshot.json", "manifest.json"):
        assert isinstance(json.loads((directory / filename).read_text(encoding="utf-8")), dict)
    snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    raw_fixture_hash = hashlib.sha256(snapshot["raw_fixture_text"].encode("utf-8")).hexdigest()
    assert raw_fixture_hash == disk_report.source_health[0].raw_hash
    assert json.loads(snapshot["raw_fixture_text"]) == snapshot["raw_fixture"]
    for evidence in disk_report.evidence:
        filename, pointer = evidence.locator.split("#", 1)
        assert filename == "snapshot.json"
        located = snapshot
        for key in pointer.lstrip("/").split("/"):
            located = located[int(key)] if isinstance(located, list) else located[key]
        assert located["evidence_id"] == evidence.evidence_id
        assert located["raw_hash"] == evidence.raw_hash
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for filename, expected_hash in manifest["files"].items():
        assert hashlib.sha256((directory / filename).read_bytes()).hexdigest() == expected_hash

    with sqlite3.connect(tmp_path / "demo" / "demo.sqlite3") as connection:
        row = connection.execute("SELECT scenario_date, report_json, report_directory FROM demo_reports").fetchone()
        tables = {entry[0] for entry in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert row is not None
    assert row[0] == "2026-09-09"
    assert DailyReport.model_validate_json(row[1]) == demo_report
    assert str(directory) == row[2]
    assert not tables.intersection({"portfolio", "orders", "fills", "cash_accounts", "positions"})
    assert (tmp_path / "demo" / "latest.json").is_file()


def test_rerun_preserves_existing_report_versions(scenario_date, generated_at, tmp_path):
    first = build_demo_report(scenario_date, generated_at=generated_at)
    first_directory = publish_report(first, tmp_path)
    frozen_hashes = file_hashes(first_directory)
    second = build_demo_report(scenario_date, generated_at=generated_at)
    second_directory = publish_report(second, tmp_path)
    assert first_directory != second_directory
    assert file_hashes(first_directory) == frozen_hashes
    with sqlite3.connect(tmp_path / "demo" / "demo.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM demo_reports").fetchone()[0] == 2


def test_changed_source_hash_cannot_publish_with_current_fixture(demo_report, tmp_path):
    demo_report.source_health[0].raw_hash = "0" * 64
    with pytest.raises(ValueError, match="原始资料已改变"):
        publish_report(demo_report, tmp_path)
    assert not list(tmp_path.iterdir())
