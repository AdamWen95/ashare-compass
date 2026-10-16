"""Immutable morning archives with actual publication times and hash checks."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from ..reports.m3_render import render_sections_html, render_sections_markdown
from .contracts import READABLE_SCHEMAS, aware
from .report import report_sections, validate_brief

RELATIVE_ROOT = Path("research/sse_szse_a/premarket_reports")
FILES = {"premarket_brief.json", "premarket_brief.md", "premarket_brief.html"}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _hash(body):
    return hashlib.sha256(body).hexdigest()


def _safe(path: Path, root: Path):
    if not path.is_relative_to(root):
        raise ValueError("premarket_archive_outside_root")
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("premarket_archive_link_rejected")
        if parent == root:
            break


def publish_brief(project: Path, report: dict, *, now) -> dict:
    report, now = deepcopy(report), aware(now)
    validate_brief(report)
    if now < aware(report["generated_at"]):
        raise ValueError("premarket_generation_in_future")
    output = Path(project).absolute() / "outputs"
    draft = now < aware(report["publication_due_at"])
    if not draft:
        # The schedule has second precision; retain the actual subsecond time
        # without treating normal wakeup latency within that second as late.
        on_time = now.replace(microsecond=0) <= aware(report["publication_due_at"]).replace(microsecond=0)
        report.update(published_at=now.isoformat(), publication_status="on_time" if on_time else "late")
    identity = "brief-" + _hash(encoded(report))[:24]
    report["report_id"] = identity
    base = output / RELATIVE_ROOT
    directory = base / ("drafts" if draft else report["report_date"]) / identity
    _safe(directory, output)
    if directory.exists():
        _read(output, directory, draft=draft)
        return {"status": "staged" if draft else "published", "directory": str(directory), "report_id": identity, "reused": True}
    stage = directory.with_name(".stage-" + uuid4().hex)
    stage.mkdir(parents=True, exist_ok=False)
    sections = report_sections(report)
    contents = {"premarket_brief.json": encoded(report),
        "premarket_brief.md": render_sections_markdown(report["title"], report["notice"], sections).encode(),
        "premarket_brief.html": render_sections_html(report["title"], report["report_date"], report["notice"], sections,
            eyebrow="盘前研究 · 3–10个交易日 · 数据与风险可追溯").encode()}
    for name, body in contents.items():
        (stage / name).write_bytes(body)
    (stage / "manifest.json").write_bytes(encoded({"schema_version": report["schema_version"], "report_id": identity,
        "files": {name: _hash(body) for name, body in contents.items()}}))
    stage.rename(directory)
    _read(output, directory, draft=draft)
    return {"status": "staged" if draft else "published", "directory": str(directory), "report_id": identity, "reused": False}


def _read(root: Path, directory: Path, *, draft=False) -> dict:
    _safe(directory, root)
    if directory.parent.parent != root / RELATIVE_ROOT or not re.fullmatch(r"brief-[a-f0-9]{24}", directory.name):
        raise ValueError("premarket_archive_location_invalid")
    def read(name, limit=25_000_000):
        path = directory / name
        _safe(path, root)
        if not path.is_file() or path.stat().st_size > limit:
            raise ValueError("premarket_archive_file_invalid")
        return path.read_bytes()
    manifest = json.loads(read("manifest.json", 100_000))
    if manifest.get("schema_version") not in READABLE_SCHEMAS or manifest.get("report_id") != directory.name or set(manifest.get("files", {})) != FILES:
        raise ValueError("premarket_manifest_invalid")
    contents = {name: read(name) for name in FILES}
    if any(_hash(body) != manifest["files"][name] for name, body in contents.items()):
        raise ValueError("premarket_artifact_hash_mismatch")
    report = json.loads(contents["premarket_brief.json"])
    if report.get("schema_version") != manifest["schema_version"]:
        raise ValueError("premarket_manifest_schema_mismatch")
    validate_brief(report)
    if report.get("report_id") != directory.name or "brief-" + _hash(encoded({k:v for k,v in report.items() if k != "report_id"}))[:24] != directory.name:
        raise ValueError("premarket_identity_mismatch")
    if draft:
        if directory.parent.name != "drafts" or report["published_at"] is not None:
            raise ValueError("premarket_draft_invalid")
    elif (directory.parent.name != report["report_date"] or not report["published_at"]
            or aware(report["published_at"]) < max(aware(report["publication_due_at"]), aware(report["generated_at"]))):
        raise ValueError("premarket_publication_invalid")
    return {"report": report, "directory": directory, "files": contents}


def scan_briefs(output_root: Path, *, now) -> tuple[list, list]:
    root, now = Path(output_root).absolute(), aware(now)
    reports, problems = [], []
    base = root / RELATIVE_ROOT
    try:
        _safe(base, root)
    except ValueError:
        return [], [{"problem": "premarket_archive_link_rejected"}]
    for day in sorted(base.glob("????-??-??"), reverse=True):
        try:
            _safe(day, root)
        except ValueError:
            problems.append({"problem": "premarket_archive_link_rejected"})
            continue
        for directory in day.glob("brief-*"):
            try:
                item = _read(root, directory)
                if aware(item["report"]["published_at"]) <= now:
                    reports.append(item)
            except (ValueError, TypeError, KeyError, OSError):
                problems.append({"report_id": directory.name, "problem": "archive_validation_failed"})
    reports.sort(key=lambda a: (a["report"]["report_date"], a["report"]["published_at"]), reverse=True)
    return reports, problems
