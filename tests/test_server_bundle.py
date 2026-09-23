"""Offline bundle tests with a real tiny SQLite fixture, never the user's .env."""

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from ashare_daily.operations.backup import create_backup, verify_backup


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ashare_server_bundle_script", ROOT / "scripts/build_server_bundle.py")
bundle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bundle
spec.loader.exec_module(bundle)


@pytest.fixture
def inputs(tmp_path):
    project = tmp_path / "project-input"
    (project / "src/demo").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "scripts").mkdir()
    (project / "docs").mkdir()
    (project / "config").mkdir()
    (project / ".streamlit").mkdir()
    (project / "data/research").mkdir(parents=True)
    (project / "outputs/research").mkdir(parents=True)
    files = {
        "src/demo/core.py": "# OFFLINE TEST\nvalue = 1\n", "tests/test_demo.py": "# OFFLINE TEST\n",
        "scripts/install_server.sh": "#!/bin/sh\n# OFFLINE TEST\n", "docs/25_SERVER.md": "# 离线部署文档\n",
        "config/m4.json": '{"model_name":"offline-test","key_present":false}',
        ".streamlit/config.toml": '[server]\naddress="127.0.0.1"\n',
        "pyproject.toml": '[project]\nname="offline-bundle-test"\n', "streamlit_app.py": "# OFFLINE TEST\n",
        "requirements.in": "# OFFLINE TEST\n", "requirements.lock": "# OFFLINE TEST\n", "requirements-linux.lock": "# OFFLINE LINUX TEST\n",
        "README.md": "# OFFLINE TEST\n", "QUICKSTART_SERVER.zh-CN.md": "# 离线说明\n",
        "AGENTS.md": "# OFFLINE TEST\n", ".gitignore": ".env\n.venv/\n", ".env.example": "MODEL_API_KEY=\nMODEL_NAME=offline-test\n",
        "outputs/research/report.json": '{"verification_kind":"offline_test"}',
    }
    for relative, content in files.items():
        (project / relative).write_text(content, encoding="utf-8")
    with sqlite3.connect(project / "data/research/market.sqlite3") as db:
        db.execute("CREATE TABLE offline_fixture (kind TEXT)")
        db.execute("INSERT INTO offline_fixture VALUES ('OFFLINE_TEST_ONLY')")
    backup = tmp_path / "existing-backup"
    create_backup(project, backup)
    return project, backup, tmp_path / "deliveries" / "server.tar.gz"


def read_archive(path):
    with tarfile.open(path, "r:gz") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}


def test_bundle_contains_exact_roots_and_verified_backup_bytes(inputs):
    project, backup, output = inputs
    original_backup = {path.relative_to(backup).as_posix(): path.read_bytes() for path in backup.rglob("*") if path.is_file()}
    result = bundle.build_bundle(backup, output, project_root=project)
    contents = read_archive(output)
    assert {name.split("/", 1)[0] for name in contents} == {"project", "backup"}
    assert contents["project/requirements.lock"] == (project / "requirements.lock").read_bytes()
    assert contents["project/requirements-linux.lock"] == (project / "requirements-linux.lock").read_bytes()
    assert contents["project/.gitignore"] == (project / ".gitignore").read_bytes()
    assert contents["project/.env.example"] == (project / ".env.example").read_bytes()
    assert contents["backup/manifest.json"] == original_backup["manifest.json"]
    manifest = verify_backup(backup)
    for item in manifest["files"]:
        assert contents["backup/files/" + item["path"]] == original_backup["files/" + item["path"]]
    assert result["backup_verified"] and result["secrets_included"] is False
    assert original_backup == {path.relative_to(backup).as_posix(): path.read_bytes() for path in backup.rglob("*") if path.is_file()}


def test_manifest_and_compressed_sha256_match_every_member(inputs):
    project, backup, output = inputs
    result = bundle.build_bundle(backup, output, project_root=project)
    manifest = json.loads(Path(result["manifest_file"]).read_text(encoding="utf-8"))
    contents = read_archive(output)
    assert len(contents) == manifest["member_count"]
    assert manifest["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest() == result["sha256"]
    assert Path(result["checksum_file"]).read_text() == result["sha256"] + "  server.tar.gz\n"
    assert set(contents) == {item["path"] for item in manifest["files"]}
    for item in manifest["files"]:
        assert item["size"] == len(contents[item["path"]])
        assert item["sha256"] == hashlib.sha256(contents[item["path"]]).hexdigest()
    with tarfile.open(output, "r:gz") as archive:
        assert all(member.isfile() and not member.issym() and not member.islnk() for member in archive.getmembers())
    assert output.stat().st_nlink == 1


def test_never_reads_env_and_excludes_runtime_trees_and_unregistered_backup_files(inputs, monkeypatch):
    project, backup, output = inputs
    forbidden = [".env", ".venv/secret.txt", ".tools/runtime.txt", "outputs/private.txt", "data/private.txt", "backups/old.txt",
                 "restore_checks/old.txt", "cache/tmp.txt", ".git/config", "src/demo/__pycache__/bad.pyc", "config/.env"]
    for name in forbidden:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("OFFLINE private marker", encoding="utf-8")
    (backup / ".env").write_text("OFFLINE private marker", encoding="utf-8")
    (backup / "unregistered.json").write_text('{"password":"never-copy"}', encoding="utf-8")
    original_open = Path.open
    def forbid_env(path, *args, **kwargs):
        if path.name == ".env":
            pytest.fail("bundler must not read .env, including offline fixtures")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", forbid_env)
    bundle.build_bundle(backup, output, project_root=project)
    names = read_archive(output)
    assert not any("project/" + name in names for name in forbidden)
    assert "backup/.env" not in names and "backup/unregistered.json" not in names
    assert not any("OFFLINE private marker".encode() in body for body in names.values())


@pytest.mark.parametrize("relative, content", [
    ("config/key.json", '{"MODEL_API_KEY":"offline-nonempty-credential"}'),
    ("config/key.json", '{"nested":{"authorization":"offline-nonempty-credential"}}'),
    ("config/key.json", '{"api_key":{"secret":"offline-nonempty-credential"}}'),
    ("config/key.json", '{"base_url":"https://offline-user:offline-password@example.invalid/v1"}'),
    ("config/key.json", '{"source_url":"https://example.invalid/items?api_key=offline-nonempty-credential"}'),
    ("config/key.toml", 'password="offline-nonempty-credential"'),
    (".env.example", "MODEL_API_KEY=offline-nonempty-credential\n"),
    ("src/demo/key.py", 'key="sk-abcdefghijklmnopqrstuvwx123456"'),
])
def test_configuration_credentials_or_obvious_source_key_refuse_before_publication(inputs, relative, content):
    project, backup, output = inputs
    (project / relative).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="凭据"):
        bundle.build_bundle(backup, output, project_root=project)
    assert not output.exists() and not output.parent.exists()


def test_explicit_offline_source_test_literal_can_be_packaged(inputs):
    project, backup, output = inputs
    (project / "tests/test_credentials.py").write_text('token = "sk-OFFLINE-TEST-FAKE-TOKEN"\n', encoding="utf-8")
    bundle.build_bundle(backup, output, project_root=project)
    assert "project/tests/test_credentials.py" in read_archive(output)


def test_bad_backup_hash_prevents_any_package(inputs):
    project, backup, output = inputs
    (backup / "files/outputs/research/report.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        bundle.build_bundle(backup, output, project_root=project)
    assert not output.exists()


def test_bad_manifest_traversal_rejected(inputs):
    project, backup, output = inputs
    path = backup / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../../outside"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="路径穿越"):
        bundle.build_bundle(backup, output, project_root=project)
    assert not output.exists()


@pytest.mark.parametrize("target", ["src", "docs", "backup"])
def test_output_never_inside_any_input_tree(inputs, target):
    project, backup, _ = inputs
    output = (backup if target == "backup" else project / target) / "self.tar.gz"
    with pytest.raises(ValueError, match="输入目录"):
        bundle.build_bundle(backup, output, project_root=project)
    assert not output.exists()


@pytest.mark.parametrize("existing", ["archive", "manifest", "checksum"])
def test_existing_output_or_sidecar_never_overwritten(inputs, existing):
    project, backup, output = inputs
    path = {"archive": output, "manifest": output.with_name(output.name + ".manifest.json"), "checksum": output.with_name(output.name + ".sha256")}[existing]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"OFFLINE previous artifact")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        bundle.build_bundle(backup, output, project_root=project)
    assert path.read_bytes() == b"OFFLINE previous artifact"


def test_links_cannot_be_archived(inputs):
    project, backup, output = inputs
    source = project / "README.md"
    alias = project / "docs/alias.md"
    try:
        os.link(source, alias)
    except OSError:
        pytest.skip("test filesystem does not support hard links")
    with pytest.raises(ValueError, match="链接"):
        bundle.build_bundle(backup, output, project_root=project)
    assert not output.exists()


def test_symbolic_link_refused_when_supported(inputs):
    project, backup, output = inputs
    try:
        (project / "docs/symlink.md").symlink_to(project / "README.md")
    except OSError:
        pytest.skip("Windows account cannot create symbolic links; hard-link test still runs")
    with pytest.raises(ValueError, match="链接"):
        bundle.build_bundle(backup, output, project_root=project)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction-specific check")
def test_windows_junction_refused(inputs, tmp_path):
    project, backup, output = inputs
    target = tmp_path / "outside-test-docs"
    target.mkdir()
    (target / "private.md").write_text("OFFLINE TEST outside scope", encoding="utf-8")
    link = project / "docs/junction"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell unavailable for creating fixture junction")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                             "New-Item -ItemType Junction -Path " + quote(link) + " -Target " + quote(target) + " | Out-Null"],
                            capture_output=True, timeout=20)
    if result.returncode:
        pytest.skip("This Windows account cannot create a fixture junction")
    with pytest.raises(ValueError, match="junction"):
        bundle.build_bundle(backup, output, project_root=project)
    assert not output.exists()


def test_source_mutation_during_archive_is_not_published(inputs, monkeypatch):
    project, backup, output = inputs
    archive = bundle._archive
    def changed(stage, members):
        (project / "README.md").write_text("OFFLINE input changed", encoding="utf-8")
        return archive(stage, members)
    monkeypatch.setattr(bundle, "_archive", changed)
    with pytest.raises(ValueError, match="变化"):
        bundle.build_bundle(backup, output, project_root=project)
    assert not output.exists()
    assert not list(output.parent.glob("*.building-*"))


def test_same_inputs_produce_identical_archive_bytes(inputs):
    project, backup, first = inputs
    second = first.with_name("second.tar.gz")
    one = bundle.build_bundle(backup, first, project_root=project)
    two = bundle.build_bundle(backup, second, project_root=project)
    assert one["sha256"] == two["sha256"] and first.read_bytes() == second.read_bytes()


def test_main_failure_output_never_echoes_exception_secret(inputs, monkeypatch, capsys):
    _, backup, output = inputs
    def fail(*args, **kwargs):
        raise ValueError("sk-THIS_IS_AN_OFFLINE_FAKE_SECRET")
    monkeypatch.setattr(bundle, "build_bundle", fail)
    assert bundle.main(["--backup", str(backup), "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert "sk-" not in captured.out + captured.err
    assert json.loads(captured.out)["status"] == "failed"
