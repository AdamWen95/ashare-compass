"""Isolated real SQLite/file backup exercises; all market values are fixtures."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from ashare_daily.operations.backup import (SCHEMA, _io, create_backup, verify_backup,
    restore_backup, resolve_restored_reference)
from ashare_daily.operations.paths import resolve_archived_path, resolve_input_path


DATA = "data/engineering_validation/f3s"
OUTPUT = "outputs/engineering_validation/f3s"
SELECTION = "validation-sector-2026-09-11-" + "a" * 20


def write(root, relative, value):
    path = root / relative
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf8")
    _io(path).write_bytes(body)
    return path


def db_rows(path, query):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(query).fetchall()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    for relative in ("data/research/market.sqlite3", "data/operations/runtime.sqlite3", DATA + "/market.sqlite3"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE frozen_facts (id TEXT PRIMARY KEY, value TEXT)")
            rows = [("OFFLINE-" + str(i), str(i)) for i in range(7 if relative.startswith(DATA) else 1)]
            db.executemany("INSERT INTO frozen_facts VALUES (?,?)", rows)
    base = OUTPUT + "/" + SELECTION
    body = write(root, base + "/history/responses/one/body.bin", b"OFFLINE original HTTP body")
    evidence = write(root, "outputs/verification/f3s/fixture/calendar.json", {"verification_kind": "offline_test"})
    response = write(root, base + "/history/responses/one/response.json", {
        "purpose": "engineering_validation", "production_eligible": False, "verification_kind": "offline_test",
        "http": {"body_path": str(body), "body_sha256": hashlib.sha256(body.read_bytes()).hexdigest()}})
    write(root, base + "/sector_selection.json", {"selection_id": SELECTION,
        "purpose": "engineering_validation", "production_eligible": False,
        "members": [{"security_id": "OFFLINE-" + str(i)} for i in range(7)], "evidence": str(evidence)})
    write(root, base + "/history/history_fetch_plan.json", {"selection_id": SELECTION,
        "tasks": [{"security_id": "OFFLINE-" + str(i)} for i in range(7)], "response_path": str(response)})
    write(root, base + "/screening/f3s-offline/screening_inputs.json", {"selection_id": SELECTION,
        "purpose": "engineering_validation", "production_eligible": False,
        "file_refs": [{"path": str(response), "sha256": hashlib.sha256(response.read_bytes()).hexdigest()}]})
    report = write(root, base + "/screening/f3s-offline/technical_evaluation.json", {
        "purpose": "engineering_validation", "production_eligible": False,
        "selection_id": SELECTION, "evaluations": [{"security_id": "OFFLINE-" + str(i)} for i in range(7)]})
    write(root, base + "/screening/f3s-offline/manifest.json", {"schema_version": "f3s-technical-manifest-v1",
        "purpose": "engineering_validation", "production_eligible": False,
        "files": {report.name: hashlib.sha256(report.read_bytes()).hexdigest()}})
    write(root, "config/sector_validation.json", {"purpose": "engineering_validation", "production_eligible": False,
        "database": DATA + "/market.sqlite3", "output_directory": OUTPUT})
    return root


def test_exact_engineering_scopes_restore_sqlite_seven_members_inputs_and_original_http(project, tmp_path):
    before = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    result = create_backup(project, backup)
    assert result["status"] == "ok" and result["budget_ledger_included"]
    manifest = verify_backup(backup)
    assert manifest["schema_version"] == SCHEMA == "m4-backup-v1"
    assert set(before) == {row["path"] for row in manifest["files"]}
    assert all(row["kind"] == "sqlite_backup" for row in manifest["files"] if row["path"].endswith("sqlite3"))
    assert restore_backup(backup, restored)["status"] == "ok"
    assert db_rows(restored / (DATA + "/market.sqlite3"), "SELECT * FROM frozen_facts ORDER BY id") == [
        ("OFFLINE-" + str(i), str(i)) for i in range(7)]
    assert db_rows(restored / "data/operations/runtime.sqlite3", "SELECT * FROM frozen_facts") == [
        ("OFFLINE-0", "0")]
    for relative, original in before.items():
        original_path = project / relative
        mapped = resolve_restored_reference(str(original_path), restored)
        assert mapped == restored / relative
        assert resolve_archived_path(str(original_path), anchor=restored) == mapped
        assert resolve_archived_path(mapped, anchor=restored) == mapped
        if not relative.endswith("sqlite3"):
            assert mapped.read_bytes() == original
        assert original_path.read_bytes() == original
    saved = json.loads((restored / (OUTPUT + "/" + SELECTION + "/screening/f3s-offline/screening_inputs.json")).read_text("utf8"))
    reference = saved["file_refs"][0]
    body = resolve_archived_path(reference["path"], anchor=restored).read_bytes()
    assert hashlib.sha256(body).hexdigest() == reference["sha256"]
    assert reference["path"].startswith(str(project))  # no frozen locator rewrite


def test_committed_engineering_wal_is_included_without_wal_or_lock_copy(project, tmp_path):
    database = project / (DATA + "/market.sqlite3")
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO frozen_facts VALUES ('OFFLINE-WAL','committed')")
        connection.commit()
        for relative in (DATA + "/history.lock", OUTPUT + "/private.log", OUTPUT + "/.env.local",
                         DATA + "/credentials.json", DATA + "/private.key"):
            write(project, relative, b"OFFLINE excluded secret sentinel")
        create_backup(project, tmp_path / "backup")
        manifest = verify_backup(tmp_path / "backup")
        paths = {row["path"] for row in manifest["files"]}
        assert all(not path.endswith(("-wal", "-shm", ".lock", ".log", ".key")) for path in paths)
        assert all(".env" not in path and "credentials" not in path for path in paths)
        restore_backup(tmp_path / "backup", tmp_path / "restored")
        assert db_rows(tmp_path / "restored" / DATA / "market.sqlite3", "SELECT count(*) FROM frozen_facts") == [(8,)]
    finally:
        connection.close()


@pytest.mark.parametrize("scope", ["data/engineering_validation", "outputs/engineering_validation"])
@pytest.mark.parametrize("suffix", ["f3s-extra", "f2s1", "unrelated"])
def test_neighboring_engineering_scopes_are_not_automatically_copied_or_restorable(project, tmp_path, scope, suffix):
    relative = scope + "/" + suffix + "/outside.json"
    write(project, relative, b"OFFLINE outside allowed scope")
    backup = tmp_path / "backup"
    create_backup(project, backup)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf8"))
    assert relative not in {row["path"] for row in manifest["files"]}
    body = b"OFFLINE forged manifest scope"
    write(backup, "files/" + relative, body)
    manifest["files"].append({"path": relative, "sha256": hashlib.sha256(body).hexdigest(),
                              "size": len(body), "kind": "immutable_file"})
    manifest["file_count"] += 1
    manifest["total_bytes"] += len(body)
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(ValueError, match="禁止"):
        restore_backup(backup, tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("base", [DATA, OUTPUT])
def test_backup_target_cannot_be_inside_the_new_input_scopes(project, base):
    with pytest.raises(ValueError, match="输入范围"):
        create_backup(project, project / base / "recursive-backup")


@pytest.mark.parametrize("base", [DATA, OUTPUT])
def test_restored_native_engineering_reference_still_checks_original_hash(project, tmp_path, base):
    relative = base + "/immutable.json"
    original = write(project, relative, {"purpose": "engineering_validation", "production_eligible": False})
    create_backup(project, tmp_path / "backup")
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    target = resolve_archived_path(str(original), anchor=tmp_path / "restored")
    target.write_text('{"tampered":true}', encoding="utf8")
    for locator in (str(original), target, relative):
        with pytest.raises(ValueError, match="SHA256"):
            resolve_archived_path(locator, anchor=tmp_path / "restored")


def test_new_engineering_json_uses_existing_secret_gate_without_exposing_value(project, tmp_path):
    write(project, OUTPUT + "/source.json", {"api_key": "OFFLINE-MUST-NOT-BE-PRINTED"})
    with pytest.raises(ValueError) as caught:
        create_backup(project, tmp_path / "backup")
    assert "OFFLINE-MUST-NOT-BE-PRINTED" not in str(caught.value)
    assert not (tmp_path / "backup/manifest.json").exists()


def test_legacy_backup_without_engineering_scopes_remains_valid(tmp_path):
    root = tmp_path / "legacy"
    (root / "data/research").mkdir(parents=True)
    with sqlite3.connect(root / "data/research/market.sqlite3") as db:
        db.execute("CREATE TABLE legacy (value TEXT)")
        db.execute("INSERT INTO legacy VALUES ('OFFLINE legacy')")
    result = create_backup(root, tmp_path / "backup")
    assert result["file_count"] == 1
    assert verify_backup(tmp_path / "backup")["schema_version"] == "m4-backup-v1"
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert db_rows(tmp_path / "restored/data/research/market.sqlite3", "SELECT value FROM legacy") == [("OFFLINE legacy",)]


def test_exact_restored_engineering_database_accepts_new_versions_without_changing_original(project, tmp_path, monkeypatch):
    relative = DATA + "/market.sqlite3"
    original = project / relative
    original_bytes = original.read_bytes()
    create_backup(project, tmp_path / "backup")
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "backup", restored)
    database = resolve_archived_path(str(original), anchor=restored)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO frozen_facts VALUES ('OFFLINE-new-version','new')")
    assert database.read_bytes() != original_bytes
    for locator in (str(original), relative, database):
        assert resolve_archived_path(locator, anchor=restored) == database
    monkeypatch.chdir(restored)
    assert resolve_input_path(str(original)) == database
    assert original.read_bytes() == original_bytes
    assert db_rows(database, "SELECT count(*) FROM frozen_facts") == [(8,)]
    # A byte-for-byte backup audit is still strict. Only the runtime resolver
    # receives this one explicitly registered mutable database exception.
    with pytest.raises(ValueError, match="SHA256"):
        resolve_restored_reference(str(original), restored)


@pytest.mark.parametrize("relative", [DATA + "/other.sqlite3", DATA + "/nested/market.sqlite3", OUTPUT + "/market.sqlite3"])
def test_other_registered_engineering_sqlite_files_remain_byte_frozen(project, tmp_path, relative):
    database = project / relative
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE fixture(value TEXT)")
    create_backup(project, tmp_path / "backup")
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "backup", restored)
    target = restored / relative
    with sqlite3.connect(target) as db:
        db.execute("INSERT INTO fixture VALUES ('OFFLINE mutation')")
    for locator in (str(database), relative, target):
        with pytest.raises(ValueError, match="SHA256"):
            resolve_archived_path(locator, anchor=restored)


@pytest.mark.parametrize("mutation", ["unregistered", "wrong_kind", "not_sqlite"])
def test_mutable_database_exception_requires_exact_valid_registration(project, tmp_path, mutation):
    relative = DATA + "/market.sqlite3"
    create_backup(project, tmp_path / "backup")
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "backup", restored)
    manifest_path, mapping_path = restored / "backup-manifest.json", restored / "restore-path-map.json"
    manifest = json.loads(manifest_path.read_text("utf8"))
    mapping = json.loads(mapping_path.read_text("utf8"))
    if mutation == "unregistered":
        manifest["files"] = [row for row in manifest["files"] if row["path"] != relative]
        mapping["paths"].remove(relative)
    elif mutation == "wrong_kind":
        next(row for row in manifest["files"] if row["path"] == relative)["kind"] = "immutable_file"
    else:
        (restored / relative).write_bytes(b"OFFLINE not SQLite")
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    mapping_path.write_text(json.dumps(mapping), encoding="utf8")
    with pytest.raises(ValueError):
        resolve_archived_path(str(project / relative), anchor=restored)


def test_repeated_reference_validates_large_metadata_once_but_hashes_each_target(tmp_path, monkeypatch):
    from ashare_daily.operations import backup
    from test_archive_paths import archive_fixture, old
    relative = "outputs/engineering_validation/f3s/offline/body.raw"
    root = archive_fixture(tmp_path / "restore", {relative: b"OFFLINE original bytes"})
    manifest_path, mapping_path = root / "backup-manifest.json", root / "restore-path-map.json"
    manifest, mapping = json.loads(manifest_path.read_text("utf8")), json.loads(mapping_path.read_text("utf8"))
    # This is a resolver metadata fixture, not a claim that these unused files
    # have been backed up. Only the requested registered target must be read.
    for number in range(10009):
        name = "outputs/engineering_validation/f3s/offline/unused-" + str(number) + ".raw"
        manifest["files"].append({"path": name, "sha256": "0" * 64, "size": 0, "kind": "immutable_file"})
        mapping["paths"].append(name)
    manifest["file_count"] = len(manifest["files"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    mapping_path.write_text(json.dumps(mapping), encoding="utf8")
    backup._validated_restore_index.cache_clear()
    hashes = []
    original_hash = backup._hash
    def counting_hash(path):
        hashes.append(path)
        return original_hash(path)
    monkeypatch.setattr(backup, "_hash", counting_hash)
    for _ in range(25):
        assert resolve_restored_reference(old(relative), root) == root / relative
    info = backup._validated_restore_index.cache_info()
    assert info.misses == 1 and info.hits == 24 and info.maxsize == 4
    assert hashes == [root / relative] * 25
    (root / relative).write_bytes(b"OFFLINE changed bytes!")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_restored_reference(old(relative), root)


@pytest.mark.parametrize("name", ["restore-path-map.json", "backup-manifest.json"])
def test_same_size_same_timestamp_metadata_change_cannot_hit_old_index(tmp_path, name):
    from ashare_daily.operations import backup
    from test_archive_paths import archive_fixture, old
    relative = "outputs/engineering_validation/f3s/offline/a.raw"
    root = archive_fixture(tmp_path / "restore", {relative: b"OFFLINE-A"})
    backup._validated_restore_index.cache_clear()
    assert resolve_restored_reference(old(relative), root) == root / relative
    path = root / name
    previous = path.stat()
    body = path.read_bytes()
    changed = body.replace(b"a.raw", b"b.raw")
    assert changed != body and len(changed) == len(body)
    path.write_bytes(changed)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert path.stat().st_size == previous.st_size and path.stat().st_mtime_ns == previous.st_mtime_ns
    with pytest.raises(ValueError, match="清单不一致"):
        resolve_restored_reference(old(relative), root)
    assert backup._validated_restore_index.cache_info().misses == 2


def test_replacing_both_metadata_files_switches_identity_and_keeps_index_immutable(tmp_path):
    from ashare_daily.operations import backup
    from test_archive_paths import archive_fixture, old
    first = "outputs/engineering_validation/f3s/offline/a.raw"
    second = "outputs/engineering_validation/f3s/offline/b.raw"
    root = archive_fixture(tmp_path / "restore", {first: b"OFFLINE-A"})
    (root / second).write_bytes(b"OFFLINE-A")
    backup._validated_restore_index.cache_clear()
    assert resolve_restored_reference(old(first), root) == root / first
    for name in ("restore-path-map.json", "backup-manifest.json"):
        path = root / name
        previous = path.stat()
        body = path.read_bytes()
        path.write_bytes(body.replace(b"a.raw", b"b.raw"))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    with pytest.raises(ValueError, match="未登记"):
        resolve_restored_reference(old(first), root)
    assert resolve_restored_reference(old(second), root) == root / second
    index = backup._read_restore_index(root)[2]
    with pytest.raises(TypeError):
        index[first] = index[second]
    assert backup._validated_restore_index.cache_info().misses == 2


def test_warm_cache_still_checks_metadata_link_boundary(tmp_path, monkeypatch):
    from ashare_daily.operations import backup
    from test_archive_paths import archive_fixture, old
    relative = "outputs/engineering_validation/f3s/offline/body.raw"
    root = archive_fixture(tmp_path / "restore", {relative: b"OFFLINE"})
    resolve_restored_reference(old(relative), root)
    original = backup._no_links
    def moved_to_link(path):
        if path == root / "restore-path-map.json":
            raise ValueError("OFFLINE changed to forbidden link")
        return original(path)
    monkeypatch.setattr(backup, "_no_links", moved_to_link)
    with pytest.raises(ValueError, match="forbidden link"):
        resolve_restored_reference(old(relative), root)
