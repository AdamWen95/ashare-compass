"""OFFLINE relocation fixtures; source path syntax is independent of host OS."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from ashare_daily.operations.backup import resolve_restored_reference
from ashare_daily.operations.paths import archive_relative_path, resolve_archived_path
from ashare_daily.screening.snapshots import load_snapshot, read_bundle
from ashare_daily.screening.engine import digest
from ashare_daily.m2_data import canonical_hash, normalize_adjusted_response
from ashare_daily.sample_data import _reuse_adjusted
from ashare_daily.research.preparation import market_baseline
from test_m2_engine import input_snapshot
from test_m21_engine import m21_input
from test_m21_sample_data import FakeClient, DATES

SOURCE = r"Q:\offline-original\ashare"


def archive_fixture(root, entries, source=SOURCE):
    """Construct independently hashed local fixtures; never access source drive."""
    root.mkdir(parents=True, exist_ok=True)
    manifest_entries = []
    for relative, content in entries.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        body = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
        path.write_bytes(body)
        manifest_entries.append({"path": relative, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "kind": "immutable_file"})
    manifest = {"schema_version": "m4-backup-v1", "source_project_root": source,
                "files": manifest_entries, "file_count": len(manifest_entries),
                "total_bytes": sum(item["size"] for item in manifest_entries)}
    mapping = {"schema_version": "m4-restore-map-v1", "source_project_root": source,
               "restored_project_root": str(root), "paths": list(entries)}
    (root / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "restore-path-map.json").write_text(json.dumps(mapping), encoding="utf-8")
    return root


def old(relative):
    return str(PureWindowsPath(SOURCE) / PurePosixPath(relative))


@pytest.mark.parametrize("value", [r"Q:\offline-original\ashare\outputs\research\m3\x.json",
                                  "Q:/offline-original/ashare/outputs/research/m3/x.json"])
def test_windows_locator_produces_posix_relative_parts_on_any_host(value):
    relative = archive_relative_path(value, SOURCE)
    assert type(relative) is PurePosixPath
    assert str(relative) == "outputs/research/m3/x.json"
    assert PurePosixPath("/srv/ashare") / relative == PurePosixPath("/srv/ashare/outputs/research/m3/x.json")


def test_posix_source_and_relative_windows_source_are_understood_independently():
    assert archive_relative_path("/old/project/outputs/research/a.json", "/old/project") == PurePosixPath("outputs/research/a.json")
    assert archive_relative_path(r"outputs\research\a.json", SOURCE) == PurePosixPath("outputs/research/a.json")


@pytest.mark.parametrize("value", [r"Q:\offline-original\other\x.json", r"R:\offline-original\ashare\x.json",
                                  r"Q:outputs\x.json", r"Q:\offline-original\ashare\..\outside.json",
                                  r"..\outside.json", "/etc/passwd", r"\\other\share\x.json",
                                  r"\\?\Q:\offline-original\ashare\x.json"])
def test_other_roots_traversal_and_device_namespaces_rejected(value):
    with pytest.raises(ValueError):
        archive_relative_path(value, SOURCE)


def test_registered_windows_locator_maps_to_local_file_and_preserves_bytes(tmp_path):
    relative = "outputs/research/m3/version/evidence.json"
    root = archive_fixture(tmp_path / "restored", {relative: {"raw_locator": old("outputs/verification/m3/body.raw"), "test": "OFFLINE"}})
    original = (root / relative).read_bytes()
    result = resolve_restored_reference(old(relative).upper(), root)
    assert result == root / relative
    assert result.read_bytes() == original
    assert resolve_archived_path(old(relative), anchor=root / "outputs/research/m21/snapshots") == result
    assert (root / relative).read_bytes() == original


def test_posix_source_resolves_even_when_test_runs_on_windows(tmp_path):
    relative = "outputs/research/m3/evidence.json"
    root = archive_fixture(tmp_path / "restore", {relative: {"test": "OFFLINE"}}, source="/old/ashare")
    assert resolve_restored_reference("/old/ashare/" + relative, root) == root / relative


def test_tamper_same_size_is_rejected_by_backup_hash(tmp_path):
    relative = "outputs/research/m3/evidence.raw"
    root = archive_fixture(tmp_path / "restore", {relative: b"OFFLINE-A"})
    (root / relative).write_bytes(b"OFFLINE-B")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_archived_path(old(relative), anchor=root)


def test_unregistered_existing_file_not_read_through_old_locator(tmp_path):
    root = archive_fixture(tmp_path / "restore", {"outputs/research/m3/a.raw": b"OFFLINE"})
    unregistered = root / "outputs/research/m3/unregistered.raw"
    unregistered.write_bytes(b"not in backup")
    with pytest.raises(ValueError, match="未登记"):
        resolve_restored_reference(old("outputs/research/m3/unregistered.raw"), root)


def test_map_cannot_add_file_without_backup_manifest_registration(tmp_path):
    relative = "outputs/research/m3/a.raw"
    root = archive_fixture(tmp_path / "restore", {relative: b"OFFLINE"})
    mapping_path = root / "restore-path-map.json"
    mapping = json.loads(mapping_path.read_text())
    mapping["paths"].append("outputs/research/m3/extra.raw")
    mapping_path.write_text(json.dumps(mapping))
    with pytest.raises(ValueError, match="清单不一致"):
        resolve_restored_reference(old(relative), root)


def test_secret_and_out_of_scope_manifest_entries_are_refused(tmp_path):
    for index, forbidden in enumerate([".env", "config/.env", "outside/private.txt"]):
        root = archive_fixture(tmp_path / str(index), {forbidden: b"OFFLINE-NOT-A-SECRET"})
        with pytest.raises(ValueError):
            resolve_restored_reference(old(forbidden), root)


def test_original_host_without_map_keeps_existing_path_behavior(tmp_path):
    path = tmp_path / "file.json"
    path.write_text("{}")
    assert resolve_archived_path(path, anchor=tmp_path) == path
    assert resolve_archived_path(old("outputs/research/absent.json"), anchor=tmp_path) == Path(old("outputs/research/absent.json"))


def test_new_native_files_remain_usable_after_restore_but_path_escape_fails(tmp_path):
    root = archive_fixture(tmp_path / "restore", {"outputs/research/m3/old.raw": b"OFFLINE"})
    new = root / "outputs/research/m3/new.raw"
    new.write_bytes(b"OFFLINE-new-version")
    assert resolve_archived_path(new, anchor=root) == new
    with pytest.raises(ValueError):
        resolve_archived_path(root / ".." / "outside.raw", anchor=root)


def test_registered_native_frozen_artifact_still_checks_original_hash(tmp_path):
    relative = "outputs/research/m3/manifest.json"
    root = archive_fixture(tmp_path / "restore", {relative: b"OFFLINE-original"})
    target = root / relative
    target.write_bytes(b"OFFLINE-modified")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_archived_path(target, anchor=root)


def test_native_config_live_database_and_mutable_index_can_update(tmp_path):
    entries = {"config/m4.json": b"OFFLINE-old-config", "data/research/market.sqlite3": b"OFFLINE-test-not-SQLite",
               "data/operations/runtime.sqlite3": b"OFFLINE-test-not-SQLite", "outputs/research/m3/latest.json": b"OFFLINE-old-index",
               "data/research/m21_adjusted/window-test.json": b"OFFLINE-old-pointer"}
    root = archive_fixture(tmp_path / "restore", entries)
    for relative in entries:
        path = root / relative
        path.write_bytes(b"OFFLINE legitimate runtime change")
        assert resolve_archived_path(path, anchor=root) == path


def test_snapshot_loader_and_default_market_baseline_follow_registered_old_pointer(tmp_path, monkeypatch, m21_input):
    snapshot = deepcopy(m21_input)
    snapshot.pop("snapshot_id")
    snapshot["frozen_at"] = "2025-05-01T10:00:00+08:00"
    snapshot["snapshot_id"] = "m2-" + digest(snapshot)
    relative = "outputs/research/m21/snapshots/" + snapshot["snapshot_id"] + ".json"
    root = archive_fixture(tmp_path / "restored", {relative: snapshot,
        "outputs/research/m21/latest.json": {"snapshot_path": old(relative)}})
    before = (root / relative).read_bytes()
    restored, file = load_snapshot(old(relative), root / "outputs/research/m21")
    assert restored == snapshot and file == root / relative
    monkeypatch.chdir(root)
    frozen, market, location = market_baseline()
    assert frozen["snapshot_id"] == snapshot["snapshot_id"] and location == root / relative
    assert market["snapshot_id"] == snapshot["snapshot_id"]
    assert (root / relative).read_bytes() == before


def test_adjusted_bundle_and_raw_response_reused_with_old_windows_locators(tmp_path):
    symbol = "sh.600000"
    relative_response = "data/research/m21_adjusted/version/response.json"
    relative_manifest = "data/research/m21_adjusted/version/manifest.json"
    response = FakeClient().query("history", code=symbol, security_type="stock", start_date=DATES[0].isoformat(),
                                  end_date=DATES[-1].isoformat(), adjustment_mode="forward_adjusted")
    series = normalize_adjusted_response(response, symbol=symbol, security_type="stock", trading_dates=DATES)
    series["response_path"] = old(relative_response)
    bundle = {"schema_version": "m2-adjusted-bundle-v1", "verification_kind": "offline_test", "series": {symbol: series},
              "trading_dates": [day.isoformat() for day in DATES], "symbol_types": {symbol: "stock"},
              "run_directory": old("data/research/m21_adjusted/version")}
    bundle["manifest_hash"] = canonical_hash(bundle)
    root = archive_fixture(tmp_path / "restore", {relative_manifest: bundle,
        relative_response: {"verification_kind": "offline_test", "result": response}})
    before = (root / relative_manifest).read_bytes()
    assert read_bundle(old(relative_manifest), anchor=root) == bundle
    reused, references = _reuse_adjusted([root / "data/research/m21_adjusted"], {symbol: "stock"}, DATES, live=False)
    assert list(reused) == [symbol]
    assert reused[symbol]["response_path"] == old(relative_response)
    assert references[0]["manifest_hash"] == bundle["manifest_hash"]
    assert (root / relative_manifest).read_bytes() == before
