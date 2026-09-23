"""Project Node runtime availability and integrity, without downloads or HTTP."""
import hashlib
import json
from pathlib import Path

import pytest

from ashare_daily.providers import sina_history as sina


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setattr(sina, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sina.sys, "platform", "linux")
    monkeypatch.setattr(sina.shutil, "which", lambda name: None)
    sina._verified_node_binary.cache_clear()
    executable = tmp_path / ".tools/node/bin/node"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test executable identity; never executed")
    executable.chmod(0o755)
    config = {"schema_version": "node-runtime-v1", "version": "v24.21.0", "platform": "linux-x64",
        "executable": ".tools/node/bin/node", "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "archive_sha256": "a" * 64, "archive_url": "https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz"}
    manifest = tmp_path / "config/node_runtime.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(config), encoding="utf-8")
    yield executable, manifest, config
    sina._verified_node_binary.cache_clear()


def test_project_runtime_works_with_empty_path(installed):
    executable, _, _ = installed
    assert sina._node_runtime() == str(executable)


def test_unchanged_binary_is_hashed_once_and_replacement_is_rechecked(installed, monkeypatch):
    executable, _, _ = installed
    original = sina.hashlib.sha256
    calls = []
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(sina.hashlib, "sha256", counted)
    assert sina._node_runtime() == sina._node_runtime() == str(executable)
    assert len(calls) == 1
    executable.write_bytes(b"changed installed executable")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        sina._node_runtime()
    assert len(calls) == 2


def test_hash_mismatch_does_not_fall_back_to_unpinned_path(installed, monkeypatch):
    executable, _, _ = installed
    executable.write_bytes(b"unexpected content")
    monkeypatch.setattr(sina.shutil, "which", lambda name: "/usr/bin/node")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        sina._node_runtime()


@pytest.mark.parametrize("field,value", [("executable", "../../node"), ("platform", "linux-arm64"),
    ("schema_version", "other"), ("executable_sha256", "invalid")])
def test_invalid_manifest_is_rejected(installed, field, value):
    _, manifest, config = installed
    config[field] = value
    manifest.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest contract mismatch"):
        sina._node_runtime()


def test_configured_executable_cannot_be_a_directory(installed):
    executable, _, _ = installed
    executable.unlink()
    executable.mkdir()
    with pytest.raises(ValueError, match="regular executable"):
        sina._node_runtime()


@pytest.mark.parametrize("target", ["binary", "parent", "manifest"])
def test_project_runtime_rejects_linked_path_components(installed, monkeypatch, target):
    executable, manifest, _ = installed
    linked = executable if target == "binary" else executable.parent if target == "parent" else manifest
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == linked or original(path))
    with pytest.raises(ValueError, match="symlink"):
        sina._node_runtime()


def test_windows_uses_existing_path_even_with_linux_manifest(installed, monkeypatch):
    monkeypatch.setattr(sina.sys, "platform", "win32")
    monkeypatch.setattr(sina.shutil, "which", lambda name: "C:/Node/node.exe")
    assert sina._node_runtime() == "C:/Node/node.exe"


def test_missing_project_binary_can_use_existing_path(installed, monkeypatch):
    executable, _, _ = installed
    executable.unlink()
    monkeypatch.setattr(sina.shutil, "which", lambda name: "/usr/bin/node")
    assert sina._node_runtime() == "/usr/bin/node"


def test_absent_runtime_is_reported_as_local_dependency(installed):
    executable, _, _ = installed
    executable.unlink()
    with pytest.raises(ValueError, match="local Node runtime unavailable.*source access did not fail"):
        sina._node_runtime()


def test_decoder_uses_resolved_runtime_without_changing_execution_bounds(installed, monkeypatch):
    executable, _, _ = installed
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return type("Completed", (), {"returncode": 0, "stdout": "[]"})()
    monkeypatch.setattr(sina.subprocess, "run", run)
    assert sina.decode_pinned("AAAA") == []
    command, options = calls[0]
    assert command[0] == str(executable)
    assert command[1] == "--max-old-space-size=96" and options["timeout"] == 5
    assert json.loads(options["input"])["encoded"] == "AAAA"
