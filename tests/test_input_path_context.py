"""OFFLINE inputs: fixture cwd must not become an unrelated archive owner."""
import json
import os
from pathlib import Path

import pytest

from ashare_daily.operations.paths import resolve_archived_path, resolve_input_path
from test_archive_paths import SOURCE, archive_fixture, old

RELATIVE = "outputs/research/m3/fixture.json"


def test_explicit_native_input_uses_own_context_but_archive_anchor_stays_strict(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE archived"})
    external = tmp_path / "fixture.json"
    external.write_bytes(b"OFFLINE explicit input")
    monkeypatch.chdir(root)
    assert resolve_input_path(external) == external
    assert resolve_input_path(external).read_bytes() == b"OFFLINE explicit input"
    with pytest.raises(ValueError):
        resolve_archived_path(external, anchor=root)
    nested = root / "outputs/research/m3"
    monkeypatch.chdir(nested)
    assert resolve_input_path(external) == external


def test_native_original_file_still_maps_and_checks_restored_bytes(tmp_path, monkeypatch):
    original_root = tmp_path / "original"
    original = original_root / RELATIVE
    original.parent.mkdir(parents=True)
    original.write_bytes(b"ORIGINAL disk still exists")
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"RESTORED input"}, source=str(original_root))
    monkeypatch.chdir(root)
    assert original.exists()
    assert resolve_input_path(original) == root / RELATIVE
    assert resolve_input_path(original).read_bytes() == b"RESTORED input"
    (root / RELATIVE).write_bytes(b"TAMPERED input")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_input_path(original)
    assert original.read_bytes() == b"ORIGINAL disk still exists"


def test_original_namespace_traversal_does_not_become_explicit_external_input(tmp_path, monkeypatch):
    original_root = tmp_path / "original"
    original_root.mkdir()
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"}, source=str(original_root))
    external = tmp_path / "external.json"
    external.write_bytes(b"OFFLINE")
    monkeypatch.chdir(root)
    with pytest.raises(ValueError):
        resolve_input_path(original_root / ".." / "external.json")


def test_native_restore_nested_under_original_root_keeps_its_own_hash_context(tmp_path, monkeypatch):
    original_root = tmp_path / "original"
    root = archive_fixture(original_root / "restore_checks/restored", {RELATIVE: b"OFFLINE"}, source=str(original_root))
    monkeypatch.chdir(root)
    assert resolve_input_path(root / RELATIVE) == root / RELATIVE
    new = root / "outputs/research/new.json"
    new.write_bytes(b"NEW VERSION")
    assert resolve_input_path(new) == new
    (root / RELATIVE).write_bytes(b"TAMPER!")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_input_path(root / RELATIVE)


def test_windows_original_locator_always_follows_map(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    monkeypatch.chdir(root)
    assert resolve_input_path(old(RELATIVE)) == root / RELATIVE
    (root / RELATIVE).write_bytes(b"TAMPER!")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_input_path(old(RELATIVE))


def test_explicit_file_in_another_restore_validates_its_own_registered_hash(tmp_path, monkeypatch):
    ambient = archive_fixture(tmp_path / "restored-a", {RELATIVE: b"OFFLINE A"})
    owner = archive_fixture(tmp_path / "restored-b", {RELATIVE: b"OFFLINE B"})
    monkeypatch.chdir(ambient)
    assert resolve_input_path(owner / RELATIVE) == owner / RELATIVE
    (owner / RELATIVE).write_bytes(b"TAMPER  B")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_input_path(owner / RELATIVE)


@pytest.mark.parametrize("ambient_has_map", [False, True])
def test_explicit_file_cannot_ignore_malformed_owner_metadata(tmp_path, monkeypatch, ambient_has_map):
    ambient = tmp_path
    if ambient_has_map:
        ambient = archive_fixture(tmp_path / "restored-a", {RELATIVE: b"OFFLINE A"})
    owner = archive_fixture(tmp_path / "restored-b", {RELATIVE: b"OFFLINE B"})
    mapping_path = owner / "restore-path-map.json"
    mapping = json.loads(mapping_path.read_text())
    mapping["schema_version"] = "invalid"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.chdir(ambient)
    with pytest.raises(ValueError, match="格式无效"):
        resolve_input_path(owner / RELATIVE)


def test_explicit_registered_native_file_keeps_hash_and_new_file_remains_readable(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    monkeypatch.chdir(root)
    assert resolve_input_path(root / RELATIVE) == root / RELATIVE
    new = root / "outputs/research/new.json"
    new.write_bytes(b"NEW VERSION")
    assert resolve_input_path(new) == new
    (root / RELATIVE).write_bytes(b"TAMPER!")
    with pytest.raises(ValueError, match="SHA256"):
        resolve_input_path(root / RELATIVE)


def test_relative_traversal_never_uses_explicit_native_escape(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    monkeypatch.chdir(root)
    (tmp_path / "fixture.json").write_bytes(b"OFFLINE")
    with pytest.raises(ValueError):
        resolve_input_path("../fixture.json")


@pytest.mark.skipif(os.name == "nt", reason="Foreign Windows drive is native syntax on Windows")
def test_foreign_drive_is_not_native_absolute_input_on_posix(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    monkeypatch.chdir(root)
    with pytest.raises(ValueError):
        resolve_input_path(r"R:\other-project\fixture.json")


@pytest.mark.parametrize("invalid", [None, [], {"schema_version": "wrong"},
    {"schema_version": "m4-restore-map-v1", "source_project_root": SOURCE, "paths": [1]}])
def test_invalid_ambient_mapping_cannot_be_bypassed_by_external_input(tmp_path, monkeypatch, invalid):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    external = tmp_path / "fixture.json"
    external.write_bytes(b"OFFLINE")
    (root / "restore-path-map.json").write_text(json.dumps(invalid), encoding="utf-8")
    monkeypatch.chdir(root)
    with pytest.raises(ValueError):
        resolve_input_path(external)


def test_invalid_manifest_entry_type_is_rejected_before_context_selection(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    manifest_path = root / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["kind"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(root)
    with pytest.raises(ValueError, match="字段无效"):
        resolve_input_path(tmp_path / "fixture.json")


def test_oversized_map_is_rejected_without_reading_contents(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    with (root / "restore-path-map.json").open("wb") as stream:
        stream.seek(32_000_000)
        stream.write(b" ")
    monkeypatch.chdir(root)
    with pytest.raises(ValueError, match="超限"):
        resolve_input_path(tmp_path / "fixture.json")


def test_mapping_directory_is_invalid_instead_of_absent(tmp_path, monkeypatch):
    root = tmp_path / "restored"
    (root / "restore-path-map.json").mkdir(parents=True)
    monkeypatch.chdir(root)
    with pytest.raises(ValueError, match="缺失"):
        resolve_input_path(tmp_path / "fixture.json")


def test_mapping_symlink_is_rejected(tmp_path, monkeypatch):
    root = archive_fixture(tmp_path / "restored", {RELATIVE: b"OFFLINE"})
    path = root / "restore-path-map.json"
    external = tmp_path / "metadata.json"
    external.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(external)
    except OSError:
        pytest.skip("当前测试账号不能创建符号链接")
    monkeypatch.chdir(root)
    with pytest.raises(ValueError, match="符号链接"):
        resolve_input_path(tmp_path / "fixture.json")


def test_no_restore_context_preserves_existing_native_input_behavior(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_input_path(Path("fixture.json")) == Path("fixture.json")
    assert resolve_input_path(tmp_path / "fixture.json") == tmp_path / "fixture.json"
