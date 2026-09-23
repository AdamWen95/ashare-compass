"""OFFLINE migrated Windows records exercise the actual daily reuse boundary."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path, PureWindowsPath

import pytest

from ashare_daily.operations.daily import DailyConfig, input_state, relocated_report, valid_report
from ashare_daily.research.model_settings import load_model_settings
from test_archive_paths import SOURCE, archive_fixture, old
from test_m4_daily import project, FakeServices, execute, write_json


def independent_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def old_path_values(value, project):
    if isinstance(value, dict):
        return {key: old_path_values(item, project) for key, item in value.items()}
    if isinstance(value, list):
        return [old_path_values(item, project) for item in value]
    if isinstance(value, str) and value.startswith(str(project)):
        return old(Path(value).relative_to(project).as_posix())
    return value


@pytest.fixture
def restored_daily(project):
    # A synthetic first generation creates realistic artifact/record structures.
    # Only test-owned files are converted into a historical Windows fixture.
    first = execute(project)
    assert first["generation_status"] == "ok"
    config = DailyConfig.model_validate_json((project / "config/m4.json").read_text(encoding="utf-8"))
    settings = load_model_settings(project / ".env", include_environment=False).model_copy(update={
        "timeout_seconds": float(config.model_timeout_seconds), "max_retries": config.model_max_retries,
        "max_calls": config.model_max_calls_per_run})
    if config.model_max_output_tokens is not None:
        settings = settings.model_copy(update={'max_output_tokens': config.model_max_output_tokens})
    versions = {str(PureWindowsPath(key)): value for key, value in first["config_versions"].items()}
    identity = {key: first[key] for key in ("target_trade_date", "cutoff_at", "query_start_at")}
    identity.update(config_versions=versions, model=settings.public_dict(), skip_model=False)
    legacy = old_path_values(deepcopy(first), project)
    legacy["config_versions"] = versions
    legacy["task_identity"] = independent_digest(identity)
    report_directory = Path(first["report"]["run_directory"])
    entries = {path.relative_to(project).as_posix(): path.read_bytes() for path in report_directory.iterdir() if path.is_file()}
    record_path = Path(first["run_directory"]) / "result.json"
    entries[record_path.relative_to(project).as_posix()] = legacy
    archive_fixture(project, entries)
    frozen = {relative: (project / relative).read_bytes() for relative in entries}
    return {"project": project, "record": legacy, "native_first": first, "frozen": frozen,
            "canonical_identity": first["task_identity"]}


def assert_original_bytes_unchanged(fixture):
    for relative, original in fixture["frozen"].items():
        assert (fixture["project"] / relative).read_bytes() == original


def test_migrated_windows_report_is_valid_and_resolution_is_only_in_memory(restored_daily):
    fixture = restored_daily
    report = fixture["record"]["report"]
    frozen_dict = deepcopy(report)
    assert valid_report(report, anchor=fixture["project"])
    resolved = relocated_report(report, anchor=fixture["project"])
    assert resolved["json"] == fixture["native_first"]["report"]["json"]
    assert resolved["run_directory"] == str(Path(resolved["json"]).parent)
    assert report == frozen_dict
    assert_original_bytes_unchanged(fixture)


def test_legacy_windows_config_key_identity_reuses_without_any_external_stage(restored_daily):
    fixture = restored_daily
    assert fixture["record"]["task_identity"] != fixture["canonical_identity"]
    assert all("\\" in key for key in fixture["record"]["config_versions"])
    services = FakeServices(fail_stage="calendar")
    result = execute(fixture["project"], services)
    assert result["status"] == "reused"
    assert result["reused_from"] == fixture["record"]["run_id"]
    assert result["task_identity"] == fixture["canonical_identity"]
    assert all("\\" not in key for key in result["config_versions"])
    assert services.calls == [] and services.model_calls == 0
    assert result["model_summary"]["call_count"] == 0
    assert Path(result["report"]["json"]).is_file()
    assert_original_bytes_unchanged(fixture)


def test_modified_cutoff_does_not_match_legacy_identity(restored_daily):
    fixture = restored_daily
    services = FakeServices(fail_stage="calendar")
    result = execute(fixture["project"], services, cutoff="2026-09-08T20:59:59+08:00")
    assert result["status"] == "calendar_unavailable"
    assert services.calls == ["calendar"] and services.model_calls == 0
    assert result["task_identity"] not in {fixture["record"]["task_identity"], fixture["canonical_identity"]}
    assert_original_bytes_unchanged(fixture)


def test_changed_adjusted_input_state_blocks_migrated_reuse(restored_daily):
    fixture = restored_daily
    path = fixture["project"] / "data/research/m21_adjusted/new-version/manifest.json"
    write_json(path, {"end_date": "2026-09-08", "manifest_hash": "OFFLINE changed immutable input version"})
    sample = json.loads((fixture["project"] / "config/samples/m21_100.json").read_text(encoding="utf-8"))
    changed_state = input_state(fixture["project"] / "data/research/market.sqlite3",
        fixture["project"] / "data/research/m21_adjusted", sorted(sample["symbol_types"]),
        date.fromisoformat(fixture["record"]["target_trade_date"]), fixture["record"]["cutoff_at"])
    assert changed_state != fixture["record"]["input_state"]
    services = FakeServices(fail_stage="calendar")
    result = execute(fixture["project"], services)
    assert result["task_identity"] == fixture["canonical_identity"]
    assert result["status"] == "calendar_unavailable" and services.calls == ["calendar"]
    assert_original_bytes_unchanged(fixture)


def test_same_named_config_with_changed_contents_cannot_reuse_legacy(restored_daily):
    fixture = restored_daily
    path = fixture["project"] / "config/m4.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["model_max_calls_per_day"] -= 1
    write_json(path, config)
    services = FakeServices(fail_stage="calendar")
    result = execute(fixture["project"], services)
    assert result["task_identity"] != fixture["canonical_identity"]
    assert result["status"] == "calendar_unavailable" and services.calls == ["calendar"]
    assert_original_bytes_unchanged(fixture)


@pytest.mark.parametrize("filename", ["daily_brief.json", "daily_brief.html", "input_snapshot.json"])
def test_hash_tamper_never_reuses_migrated_report(restored_daily, filename):
    fixture = restored_daily
    path = Path(fixture["native_first"]["report"]["run_directory"]) / filename
    original = path.read_bytes()
    changed = bytes([original[0] ^ 1]) + original[1:]
    path.write_bytes(changed)
    assert not valid_report(fixture["record"]["report"], anchor=fixture["project"])
    services = FakeServices(fail_stage="calendar")
    result = execute(fixture["project"], services)
    assert result["status"] == "calendar_unavailable" and services.calls == ["calendar"]
    assert path.read_bytes() == changed  # no attempt to repair or overwrite old evidence


def test_recomputed_report_manifest_cannot_bypass_restored_file_hash(restored_daily):
    fixture = restored_daily
    directory = Path(fixture["native_first"]["report"]["run_directory"])
    path = directory / "daily_brief.json"
    body = path.read_bytes() + b" "
    path.write_bytes(body)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["daily_brief.json"] = hashlib.sha256(body).hexdigest()
    write_json(manifest_path, manifest)
    assert not valid_report(fixture["record"]["report"], anchor=fixture["project"])
    services = FakeServices(fail_stage="calendar")
    assert execute(fixture["project"], services)["status"] == "calendar_unavailable"
    assert services.calls == ["calendar"]


def test_recomputed_manifest_and_nonpublic_response_still_fail_backup_hash(restored_daily):
    fixture = restored_daily
    directory = Path(fixture["native_first"]["report"]["run_directory"])
    response_path = directory / "model_responses.json"
    changed = response_path.read_bytes() + b" OFFLINE tampered"
    response_path.write_bytes(changed)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["model_responses.json"] = hashlib.sha256(changed).hexdigest()
    write_json(manifest_path, manifest)
    assert not valid_report(fixture["record"]["report"], anchor=fixture["project"])
    services = FakeServices(fail_stage="calendar")
    assert execute(fixture["project"], services)["status"] == "calendar_unavailable"
    assert services.calls == ["calendar"]


def test_native_paths_in_a_reused_record_keep_original_backup_integrity(restored_daily):
    fixture = restored_daily
    second = execute(fixture["project"], FakeServices(fail_stage="calendar"))
    assert second["status"] == "reused"
    directory = Path(second["report"]["run_directory"])
    response_path = directory / "model_responses.json"
    changed = response_path.read_bytes() + b" OFFLINE post-reuse tamper"
    response_path.write_bytes(changed)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["model_responses.json"] = hashlib.sha256(changed).hexdigest()
    write_json(manifest_path, manifest)
    services = FakeServices(fail_stage="calendar")
    third = execute(fixture["project"], services)
    assert third["status"] == "calendar_unavailable"
    assert services.calls == ["calendar"]
