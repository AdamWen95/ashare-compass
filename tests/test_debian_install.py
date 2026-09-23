"""Offline Debian installer review: synthetic SQLite archives, fake process runner."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import tarfile
import zipfile

import pytest

from ashare_daily.operations.backup import create_backup


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


installer = load("offline_debian_installer", ROOT / "scripts/install_debian.py")
bundler = load("offline_debian_bundler", ROOT / "scripts/build_server_bundle.py")


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "source"
    for relative in ["src/ashare_daily/operations", "config", "scripts", "data/research", "data/operations", "outputs/research"]:
        (source / relative).mkdir(parents=True)
    values = {
        "pyproject.toml": '[project]\nname="offline-install-test"\n', "requirements-linux.lock": "# OFFLINE TEST\n",
        "streamlit_app.py": "# OFFLINE TEST\n", "README.md": "# OFFLINE TEST\n", ".gitignore": ".env\n",
        ".env.example": "MODEL_API_KEY=\n", "config/m4.json": '{"verification_kind":"offline_test"}',
        "outputs/research/offline.json": '{"verification_kind":"offline_test","value":"original"}',
        "src/ashare_daily/__init__.py": '"""Offline package fixture."""\n',
        "src/ashare_daily/operations/__init__.py": '"""Offline package fixture."""\n',
    }
    for relative, text in values.items():
        (source / relative).write_text(text, encoding="utf-8")
    shutil.copyfile(ROOT / "scripts/install_debian.py", source / "scripts/install_debian.py")
    shutil.copyfile(ROOT / "src/ashare_daily/operations/backup.py", source / "src/ashare_daily/operations/backup.py")
    for relative, table in [("data/research/market.sqlite3", "offline_prices"), ("data/operations/runtime.sqlite3", "offline_budget")]:
        with sqlite3.connect(source / relative) as database:
            database.execute(f"CREATE TABLE {table} (value INTEGER)")
            database.execute(f"INSERT INTO {table} VALUES (7)")
    backup = tmp_path / "backup"
    create_backup(source, backup)
    archive = tmp_path / "transfer.tar.gz"
    result = bundler.build_bundle(backup, archive, project_root=source)
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(unpacked, filter="data")
    shutil.copyfile(result["manifest_file"], unpacked / "bundle-manifest.json")
    return unpacked, tmp_path / "new-project"


def no_network(*args, **kwargs):
    pytest.fail("offline installer test attempted network access")


def test_preview_validates_without_network_or_destination(package, monkeypatch, capsys):
    unpacked, destination = package
    monkeypatch.setattr(installer.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(installer, "install_runtime", no_network)
    result = installer.main(["--bundle-root", str(unpacked), "--destination", str(destination)])
    assert result == 0 and not destination.exists()
    plan = json.loads(capsys.readouterr().out)
    assert plan["operation"] == "preview" and plan["external_calls"] is False
    assert plan["python_version"] == "3.12.14"
    assert plan["system_python_unchanged"] and plan["services_enabled"] is False


def test_first_prepare_preserves_code_data_and_budget_bytes(package):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    manifest = json.loads((unpacked / "bundle-manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.parts[0] == "project":
            assert (destination / Path(*relative.parts[1:])).read_bytes() == (unpacked / relative).read_bytes()
        elif relative.parts[:2] == ("backup", "files"):
            assert (destination / Path(*relative.parts[2:])).read_bytes() == (unpacked / relative).read_bytes()
    with sqlite3.connect(destination / "data/operations/runtime.sqlite3") as database:
        assert database.execute("SELECT value FROM offline_budget").fetchone() == (7,)
    assert (destination / "restore-path-map.json").is_file()
    assert not (destination / ".env").exists()


def test_repeat_refused_resume_same_bundle_preserves_user_env(package):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    with pytest.raises(ValueError, match="已经存在"):
        installer.prepare(unpacked, destination)
    key_file = destination / ".env"
    key_file.write_bytes(b"MODEL_API_KEY=sk-OFFLINE-FAKE-NOT-A-REAL-KEY\n")
    before = key_file.read_bytes()
    installer.prepare(unpacked, destination, resume=True)
    assert key_file.read_bytes() == before


def test_installed_environment_cannot_be_resumed(package):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    marker = destination / installer.MARKER
    state = json.loads(marker.read_text(encoding="utf-8"))
    state["operation"] = "installed"
    marker.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="安装已完成"):
        installer.prepare(unpacked, destination, resume=True)


def test_source_hash_damage_refused_before_target_creation(package):
    unpacked, destination = package
    (unpacked / "project/streamlit_app.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        installer.prepare(unpacked, destination)
    assert not destination.exists()


@pytest.mark.parametrize("relative", ["streamlit_app.py", "data/research/market.sqlite3", "data/operations/runtime.sqlite3", "outputs/research/offline.json"])
def test_target_damage_refused_on_resume(package, relative):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    damaged = destination / relative
    damaged.write_bytes(b"OFFLINE damaged restored input")
    with pytest.raises(ValueError):
        installer.prepare(unpacked, destination, resume=True)
    assert damaged.read_bytes() == b"OFFLINE damaged restored input"


def test_resume_other_manifest_or_destination_refused(package):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    marker = destination / installer.MARKER
    state = json.loads(marker.read_text(encoding="utf-8"))
    state["manifest_sha256"] = "0" * 64
    marker.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="标识不匹配"):
        installer.prepare(unpacked, destination, resume=True)


@pytest.mark.parametrize("relative", ["../escape", "project/../../escape", "/etc/passwd", "project\\secret", "project/.env"])
def test_manifest_path_escape_or_secrets_refused(package, relative):
    unpacked, destination = package
    path = unpacked / "bundle-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = relative
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        installer.prepare(unpacked, destination)
    assert not destination.exists()


def make_directory_link(link: Path, target: Path):
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        pass
    powershell = shutil.which("powershell.exe")
    if os.name != "nt" or not powershell:
        pytest.skip("No directory-link creation support")
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                             "New-Item -ItemType Junction -Path " + quote(link) + " -Target " + quote(target) + " | Out-Null"],
                            capture_output=True, timeout=20)
    if result.returncode:
        pytest.skip("No junction creation permission")


def test_linked_destination_refused_before_writing(package, tmp_path):
    unpacked, destination = package
    outside = tmp_path / "outside"
    outside.mkdir()
    make_directory_link(destination, outside)
    with pytest.raises(ValueError, match="链接"):
        installer.prepare(unpacked, destination)
    assert not list(outside.iterdir())


def test_uv_tools_link_refused_before_creating_outside_bootstrap(package, tmp_path, monkeypatch):
    _, destination = package
    destination.mkdir()
    outside = tmp_path / "outside-tools"
    outside.mkdir()
    make_directory_link(destination / ".tools", outside)
    monkeypatch.setattr(installer.urllib.request, "urlopen", no_network)
    with pytest.raises(ValueError, match="链接"):
        installer.install_uv(destination)
    assert not list(outside.iterdir())


def test_fixed_uv_download_url_timeout_and_bad_sha_are_enforced(tmp_path, monkeypatch):
    calls = []
    class Response(io.BytesIO):
        status = 200
    def urlopen(url, timeout):
        calls.append((url, timeout))
        return Response(b"OFFLINE incorrect wheel bytes")
    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="SHA256"):
        installer.install_uv(tmp_path)
    assert calls == [(installer.UV_URL, 45)]
    assert installer.UV_URL.startswith("https://files.pythonhosted.org/packages/")
    assert installer.UV_VERSION in installer.UV_URL and "x86_64" in installer.UV_URL
    assert not (tmp_path / ".tools/bootstrap/uv").exists()
    assert not (tmp_path / ".tools/bootstrap/uv.download").exists()


def cached_wheel(destination: Path, body: bytes) -> Path:
    directory = destination / ".tools/bootstrap"
    directory.mkdir(parents=True)
    path = directory / "uv.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(installer.UV_MEMBER, body)
        archive.writestr("../../never-extracted", "OFFLINE traversal decoy")
    return path


def test_cached_uv_wheel_hash_and_binary_hash_both_required(tmp_path, monkeypatch):
    wheel = cached_wheel(tmp_path, b"OFFLINE fake binary")
    monkeypatch.setattr(installer.urllib.request, "urlopen", no_network)
    with pytest.raises(ValueError, match="缓存SHA256"):
        installer.install_uv(tmp_path)
    monkeypatch.setattr(installer, "UV_SHA256", installer.sha(wheel))
    with pytest.raises(ValueError, match="可执行文件SHA256"):
        installer.install_uv(tmp_path)
    assert not (tmp_path / ".tools/bootstrap/uv").exists()


def test_uv_extracts_only_exact_verified_member_without_network(tmp_path, monkeypatch):
    body = b"OFFLINE fake uv binary for extraction test only"
    wheel = cached_wheel(tmp_path, body)
    monkeypatch.setattr(installer, "UV_SHA256", installer.sha(wheel))
    monkeypatch.setattr(installer, "UV_BINARY_SHA256", hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(installer.urllib.request, "urlopen", no_network)
    binary = installer.install_uv(tmp_path)
    assert binary.read_bytes() == body
    assert not (tmp_path / "never-extracted").exists()


def test_runtime_commands_pin_independent_python_and_no_services(tmp_path):
    uv = tmp_path / ".tools/bootstrap/uv"
    commands = installer.runtime_commands(tmp_path, uv)
    flat = [token for command in commands for token in command]
    assert commands[0][-4:] == ["python", "install", "3.12.14", "--no-bin"]
    assert "--managed-python" in commands[1] and commands[1][-1] == str(tmp_path / ".venv")
    assert "--require-hashes" in commands[2] and "requirements-linux.lock" in commands[2]
    assert "https://pypi.org/simple" in commands[2] and "--no-build" in commands[2]
    assert "--offline" in commands[3] and "--no-deps" in commands[3]
    assert commands[5][-2:] == ["doctor", "--offline"]
    assert commands[6][-2:] == ["run-daily", "--dry-run"]
    assert not {"--system", "sudo", "apt", "apt-get", "systemctl", "crontab"}.intersection(flat)


def test_fake_runtime_runner_never_inherits_model_or_index_credentials(package, monkeypatch):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    monkeypatch.setattr(installer, "install_uv", lambda dest: dest / ".tools/bootstrap/uv")
    monkeypatch.setattr(installer.urllib.request, "urlopen", no_network)
    monkeypatch.setenv("MODEL_API_KEY", "sk-OFFLINE-FAKE-SHOULD-NOT-LEAK")
    monkeypatch.setenv("PIP_INDEX_URL", "https://offline-user:offline-password@example.invalid/")
    monkeypatch.setenv("VIRTUAL_ENV", "OFFLINE other virtualenv")
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["cwd"] == destination
        assert not {"MODEL_API_KEY", "PIP_INDEX_URL", "VIRTUAL_ENV"}.intersection(kwargs["env"])
        assert kwargs["env"]["UV_PYTHON_INSTALL_DIR"] == str(destination / ".tools/python")
        return SimpleNamespace(returncode=0, stdout="OFFLINE TEST", stderr="")
    installer.install_runtime(destination, runner=runner)
    assert len(calls) == 7
    assert json.loads((destination / installer.MARKER).read_text(encoding="utf-8"))["operation"] == "installed"
    logs = list((destination / "outputs/verification/linux/install").glob("step-*.json"))
    assert len(logs) == 7 and all("SHOULD-NOT-LEAK" not in path.read_text(encoding="utf-8") for path in logs)


@pytest.mark.parametrize("relative", ["outputs/verification/linux/install", ".venv", ".tools/python", ".tools/uv-cache"])
def test_runtime_write_and_execution_paths_refuse_links_before_runner(package, tmp_path, monkeypatch, relative):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    outside = tmp_path / "outside-runtime-path"
    outside.mkdir()
    link = destination / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    make_directory_link(link, outside)
    monkeypatch.setattr(installer, "install_uv", lambda dest: dest / ".tools/bootstrap/uv")
    with pytest.raises(ValueError, match="链接"):
        installer.install_runtime(destination, runner=no_network)
    assert not list(outside.iterdir())


def test_runtime_failure_stops_later_steps_and_keeps_resumable_marker(package, monkeypatch):
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    monkeypatch.setattr(installer, "install_uv", lambda dest: dest / ".tools/bootstrap/uv")
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="OFFLINE", stderr="OFFLINE simulated download failure")
    with pytest.raises(ValueError, match="步骤1失败"):
        installer.install_runtime(destination, runner=runner)
    assert len(calls) == 1
    assert json.loads((destination / installer.MARKER).read_text(encoding="utf-8"))["operation"] == "preview"


@pytest.mark.parametrize("within_managed_runtime", [True, False])
def test_linux_interpreter_symlink_policy_with_offline_path_simulation(package, tmp_path, monkeypatch, within_managed_runtime):
    """Exercise Linux symlink resolution policy without requiring Windows privileges."""
    unpacked, destination = package
    installer.prepare(unpacked, destination)
    interpreter = destination / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"OFFLINE interpreter placeholder; never executed")
    target = (destination / ".tools/python/cpython-3.12.14/bin/python") if within_managed_runtime else (tmp_path / "unrelated-python")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"OFFLINE runtime placeholder; never executed")
    real_is_symlink, real_resolve = Path.is_symlink, Path.resolve
    monkeypatch.setattr(Path, "is_symlink", lambda path: True if path == interpreter else real_is_symlink(path))
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: target if path == interpreter else real_resolve(path, *args, **kwargs))
    monkeypatch.setattr(installer, "install_uv", lambda dest: dest / ".tools/bootstrap/uv")
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="OFFLINE TEST", stderr="")
    if within_managed_runtime:
        installer.install_runtime(destination, runner=runner)
        assert any(command[0] == str(interpreter) and "-c" in command for command in calls)
        assert not any("venv" in command for command in calls)
    else:
        with pytest.raises(ValueError, match="不属于"):
            installer.install_runtime(destination, runner=runner)
        assert not calls


def test_non_linux_actual_install_refused_without_prepare(package, monkeypatch, capsys):
    unpacked, destination = package
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "prepare", no_network)
    monkeypatch.setattr(installer, "install_runtime", no_network)
    assert installer.main(["--bundle-root", str(unpacked), "--destination", str(destination), "--install"]) == 2
    assert not destination.exists()
    assert "Linux x86_64" in capsys.readouterr().out
