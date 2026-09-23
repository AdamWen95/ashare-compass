"""Save independently versioned DEMO artifacts and a small SQLite archive."""

from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ashare_daily.schemas import DailyReport
from ashare_daily.reports.render import render_html, render_markdown


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def publish_report(report: DailyReport, output_root: Path) -> Path:
    # Revalidate mutable nested objects and model_copy updates before any writes.
    report = DailyReport.model_validate(report.model_dump())
    document = report.model_dump(mode="json")
    fixture_text = files("ashare_daily").joinpath("fixtures/demo.json").read_text(encoding="utf-8")
    if any(source.raw_hash != sha256(fixture_text.encode("utf-8")).hexdigest() for source in report.source_health):
        raise ValueError("合成原始资料已改变，请重新生成报告；禁止把新资料配给旧快照")
    contents = {
        "daily_brief.json": json_text(document),
        "daily_brief.md": render_markdown(report),
        "daily_brief.html": render_html(report),
        "snapshot.json": json_text({
            "mode": "demo",
            "notice": "DEMO 人工合成资料冻结副本；不是真实历史市场快照。",
            "snapshot_id": report.snapshot_id,
            "frozen_report": document,
            "raw_fixture": json.loads(fixture_text),
            "raw_fixture_text": fixture_text,
        }),
    }
    run_id = f"{report.actual_generated_at:%Y%m%dT%H%M%S%f}-{uuid4().hex[:8]}"
    demo_root = Path(output_root).resolve() / "demo"
    date_root = demo_root / report.scenario_date.isoformat()
    date_root.mkdir(parents=True, exist_ok=True)
    directory = date_root / run_id
    staging = date_root / f".pending-{run_id}"
    staging.mkdir(exist_ok=False)
    for filename, content in contents.items():
        (staging / filename).write_text(content, encoding="utf-8", newline="\n")
    manifest = {
        "mode": "demo", "run_id": run_id, "snapshot_id": report.snapshot_id,
        "files": {name: sha256((staging / name).read_bytes()).hexdigest() for name in contents},
    }
    (staging / "manifest.json").write_text(json_text(manifest), encoding="utf-8", newline="\n")
    # Each run gets a new path. A failed publication never advances latest.json.
    with sqlite3.connect(demo_root / "demo.sqlite3") as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS demo_reports (
                run_id TEXT PRIMARY KEY,
                scenario_date TEXT NOT NULL,
                actual_generated_at TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                report_json TEXT NOT NULL,
                report_directory TEXT NOT NULL
            )
        """)
        connection.execute(
            "INSERT INTO demo_reports VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, report.scenario_date.isoformat(), report.actual_generated_at.isoformat(),
             report.snapshot_id, contents["daily_brief.json"], str(directory)),
        )
        staging.rename(directory)
    latest = {
        "mode": "demo", "run_id": run_id,
        "report_directory": str(directory), "html": str(directory / "daily_brief.html"),
    }
    latest_temp = demo_root / f".latest-{run_id}.tmp"
    latest_temp.write_text(json_text(latest), encoding="utf-8", newline="\n")
    latest_temp.replace(demo_root / "latest.json")
    return directory
