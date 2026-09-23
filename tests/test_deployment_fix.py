"""Offline code-fix checks: tiny synthetic project, no server or credentials."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offline_deployment_fix", ROOT / "scripts/apply_deployment_fix.py")
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project, package = tmp_path / "project", tmp_path / "fix-package"
    (project / "src/ashare_daily").mkdir(parents=True)
    (project / "tests").mkdir()
    package.mkdir()
    files = [("src/ashare_daily/one.py", b"# OFFLINE old one\r\n", b"# OFFLINE new one\n"),
             ("tests/test_two.py", b"# OFFLINE old two\n", b"# OFFLINE new two\n"),
             ("src/ashare_daily/new.py", None, b"# OFFLINE new module\n")]
    entries = []
    for name, old, new in files:
        target = package / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new)
        if old is not None:
            (project / name).write_bytes(old)
        entries.append({"path": name, "old_sha256": hashlib.sha256(old).hexdigest() if old is not None else None,
                        "new_sha256": hashlib.sha256(new).hexdigest()})
    manifest = {"fix_id": "offline-fix-v1", "files": entries}
    (package / "deployment-fix.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(fix.os, "geteuid", lambda: 1000, raising=False)
    return project, package, manifest, files


def save_manifest(package, manifest):
    (package / "deployment-fix.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_preview_checks_all_files_without_writes(setup):
    project, package, _, files = setup
    result = fix.apply_fix(project, package)
    assert result["status"] == "preview" and result["files_checked"] == 3
    assert result["files_to_change"] == [name for name, _, _ in files]
    assert not result["external_calls"] and not result["data_or_credentials_modified"]
    assert not (project / "outputs").exists()
    for name, old, _ in files:
        assert (project / name).read_bytes() == old if old is not None else not (project / name).exists()


def test_apply_keeps_original_bytes_backup_and_is_idempotent(setup):
    project, package, _, files = setup
    result = fix.apply_fix(project, package, apply=True)
    assert result["status"] == "applied"
    backup = Path(result["backup_directory"])
    assert backup.is_relative_to(project / "outputs/verification/linux/deployment-fixes/offline-fix-v1")
    for name, old, new in files:
        assert (project / name).read_bytes() == new
        if old is not None:
            assert (backup / "before" / name).read_bytes() == old
        else:
            assert not (backup / "before" / name).exists()
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert fix.apply_fix(project, package, apply=True)["status"] == "already_applied"
    assert before == {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_apply_does_not_read_or_write_env_data_config_or_reports(setup, monkeypatch):
    project, package, _, _ = setup
    protected = [".env", "data/research/market.sqlite3", "config/m4.json", "outputs/research/report.json"]
    for name in protected:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"OFFLINE protected original bytes")
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        if path in [project / name for name in protected]:
            pytest.fail("code-only fix touched a protected file")
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(Path, "open", guarded)
        fix.apply_fix(project, package, apply=True)
    assert all((project / name).read_bytes() == b"OFFLINE protected original bytes" for name in protected)


@pytest.mark.parametrize("damage", ["source", "target", "missing_target", "new_exists"])
def test_all_hashes_checked_before_any_project_write(setup, damage):
    project, package, _, files = setup
    if damage == "source":
        (package / "files" / files[-1][0]).write_bytes(b"OFFLINE package tamper")
    elif damage == "target":
        (project / files[1][0]).write_bytes(b"OFFLINE user-edited code")
    elif damage == "missing_target":
        (project / files[1][0]).unlink()
    else:
        (project / files[-1][0]).write_bytes(b"OFFLINE unrelated new file")
    with pytest.raises(ValueError, match="SHA256"):
        fix.apply_fix(project, package, apply=True)
    assert (project / files[0][0]).read_bytes() == files[0][1]
    assert not (project / "outputs").exists()


@pytest.mark.parametrize("name", ["../escape.py", "src/ashare_daily/../../bad.py", "src\\ashare_daily\\one.py", ".env",
                                 "data/file.py", "config/file.py", "outputs/file.py", "tests/nested/test_bad.py"])
def test_scope_and_path_traversal_rejected(setup, name):
    project, package, manifest, _ = setup
    manifest["files"][0]["path"] = name
    save_manifest(package, manifest)
    with pytest.raises(ValueError, match="只允许"):
        fix.apply_fix(project, package, apply=True)
    assert not (project / "outputs").exists()


@pytest.mark.parametrize("change", ["duplicate", "invalid_id", "bad_sha", "missing_old"])
def test_invalid_manifest_rejected(setup, change):
    project, package, manifest, _ = setup
    if change == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif change == "invalid_id":
        manifest["fix_id"] = "../../escape"
    elif change == "bad_sha":
        manifest["files"][0]["new_sha256"] = "invalid"
    else:
        del manifest["files"][0]["old_sha256"]
    save_manifest(package, manifest)
    with pytest.raises(ValueError):
        fix.apply_fix(project, package, apply=True)
    assert not (project / "outputs").exists()


def directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        shell = shutil.which("powershell.exe")
        if os.name != "nt" or shell is None:
            pytest.skip("Directory links unavailable")
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command",
                                 "New-Item -ItemType Junction -Path " + quote(link) + " -Target " + quote(target) + " | Out-Null"],
                                capture_output=True, timeout=20)
        if result.returncode:
            pytest.skip("Cannot create offline directory junction")


@pytest.mark.parametrize("location", ["package", "target", "backup"])
def test_linked_paths_refused_without_outside_writes(setup, tmp_path, location):
    project, package, _, files = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    if location == "package":
        link = tmp_path / "linked-package"
        directory_link(link, package)
        package = link
    elif location == "target":
        code = project / "src/ashare_daily"
        code.rename(outside / "code")
        directory_link(code, outside / "code")
    else:
        directory_link(project / "outputs", outside)
    before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="链接|junction"):
        fix.apply_fix(project, package, apply=True)
    assert before == {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}


def test_hardlinked_target_refused(setup):
    project, package, _, files = setup
    target = project / files[0][0]
    target.unlink()
    os.link(package / "files" / files[0][0], target)
    with pytest.raises(ValueError, match="硬链接"):
        fix.apply_fix(project, package, apply=True)
    assert not (project / "outputs").exists()


def test_partial_known_application_only_backs_up_remaining_files(setup):
    project, package, _, files = setup
    (project / files[0][0]).write_bytes(files[0][2])
    result = fix.apply_fix(project, package, apply=True)
    assert files[0][0] not in result["files_to_change"]
    assert not (Path(result["backup_directory"]) / "before" / files[0][0]).exists()


def test_atomic_write_failure_restores_old_files_and_removes_new_files(setup, monkeypatch):
    project, package, manifest, files = setup
    manifest["files"] = [manifest["files"][2], manifest["files"][0], manifest["files"][1]]
    save_manifest(package, manifest)
    original_replace = fix.os.replace
    def fail_last(source, target):
        if target == project / files[1][0]:
            raise OSError("OFFLINE simulated atomic replacement failure")
        return original_replace(source, target)
    monkeypatch.setattr(fix.os, "replace", fail_last)
    with pytest.raises(ValueError, match="已回滚"):
        fix.apply_fix(project, package, apply=True)
    for name, old, _ in files:
        assert (project / name).read_bytes() == old if old is not None else not (project / name).exists()
    records = list((project / "outputs").rglob("result.json"))
    assert len(records) == 1 and json.loads(records[0].read_text())["status"] == "rolled_back"
    assert not list(project.rglob(".deployment-fix-*"))


def test_change_between_precheck_and_write_is_not_overwritten(setup, monkeypatch):
    project, package, _, files = setup
    original_replace = fix.replace_file
    def concurrent_edit(path, body, mode):
        original_replace(path, body, mode)
        if path == project / files[0][0] and body == files[0][2]:
            (project / files[1][0]).write_bytes(b"OFFLINE concurrent user edit")
    monkeypatch.setattr(fix, "replace_file", concurrent_edit)
    with pytest.raises(ValueError, match="已回滚"):
        fix.apply_fix(project, package, apply=True)
    assert (project / files[0][0]).read_bytes() == files[0][1]
    assert (project / files[1][0]).read_bytes() == b"OFFLINE concurrent user edit"


def test_linux_root_apply_refused_but_preview_is_read_only(setup, monkeypatch):
    project, package, _, _ = setup
    monkeypatch.setattr(fix.sys, "platform", "linux")
    monkeypatch.setattr(fix.os, "geteuid", lambda: 0)
    assert fix.apply_fix(project, package)["status"] == "preview"
    with pytest.raises(ValueError, match="不要 sudo 或 root"):
        fix.apply_fix(project, package, apply=True)
    assert not (project / "outputs").exists()


def test_cli_defaults_to_script_adjacent_package_preview(setup, monkeypatch, capsys):
    project, package, _, _ = setup
    monkeypatch.setattr(fix, "__file__", str(package / "apply_deployment_fix.py"))
    assert fix.main(["--project", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "preview"
    assert not (project / "outputs").exists()
