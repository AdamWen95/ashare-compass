"""Offline deployment configuration and self-contained upgrade package checks."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "/home/researcher/apps/ashare-daily-research"


def script_module(name):
    spec = importlib.util.spec_from_file_location("offline_settings_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


@pytest.mark.parametrize("kind", ["observation", "research"])
@pytest.mark.parametrize("explicit", [False, True])
def test_installer_resolves_config_target_and_cli_override(tmp_path, monkeypatch, capsys, kind, explicit):
    installer = script_module("apply_" + kind + "_upgrade")
    config = tmp_path / "deployment.json"
    config.write_text(json.dumps({"project_root": "/home/configured/apps/research"}), encoding="utf-8")
    observed = []

    def inspect(project, package):
        observed.append(project)
        return ({}, []) if kind == "observation" else []

    monkeypatch.setattr(installer, "inspect_upgrade", inspect)
    arguments = ["installer", "--deployment-config", str(config)]
    if explicit:
        arguments += ["--project", PROJECT]
    monkeypatch.setattr(sys, "argv", arguments)
    assert installer.main() == 0
    expected = PROJECT if explicit else "/home/configured/apps/research"
    assert observed == [Path(expected).absolute()]
    assert json.loads(capsys.readouterr().out)["project"] == str(Path(expected).absolute())


@pytest.mark.parametrize("kind", ["observation", "research"])
def test_installer_without_target_fails_before_inspection(tmp_path, monkeypatch, kind):
    installer = script_module("apply_" + kind + "_upgrade")
    config = tmp_path / "empty.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["installer", "--deployment-config", str(config)])
    inspected = []
    monkeypatch.setattr(installer, "inspect_upgrade", lambda *args: inspected.append(args))
    with pytest.raises(ValueError, match="project_root"):
        installer.main()
    assert not inspected


def package_project(tmp_path, kind, builder):
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "src/ashare_daily").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config/m4.json").write_text("{}", encoding="utf-8")
    for name in ("deployment_settings.py", "apply_observation_upgrade.py", "apply_research_upgrade.py",
                 "apply_deployment_fix.py", "linux_services.py"):
        shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
    (root / ".local").mkdir()
    config = root / ".local/deployment.json"
    config.write_text(json.dumps({"project_root": PROJECT, "account": "researcher", "ssh_host": "research-host"}), encoding="utf-8")
    (root / ".local/private-marker").write_text("LOCAL_ONLY_MARKER", encoding="utf-8")
    (root / "deploy").mkdir()
    archives = builder.KNOWN_ARCHIVES if kind == "observation" else (
        "ashare-debian-20260910.tar.gz", "ashare-debian-fix-20260910.tar.gz")
    for name in archives:
        with tarfile.open(root / "deploy" / name, "w:gz"):
            pass
    if kind == "observation":
        (root / "config/sector_observation_daily.json").write_text("{}", encoding="utf-8")
        (root / "streamlit_app.py").write_text("# offline fixture\n", encoding="utf-8")
        (root / "pyproject.toml").write_text('[project]\ndependencies = ["example==1.0"]\n[project.optional-dependencies]\ntest = []\n', encoding="utf-8")
        baseline = root / "baseline"
        baseline.mkdir()
        with zipfile.ZipFile(baseline / "source.zip", "w"):
            pass
        (baseline / "manifest.json").write_text("{}", encoding="utf-8")
        return root, {"root": root, "baseline": baseline}
    for name in builder.FILES:
        target = root / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n" if name.endswith(".json") else "# offline fixture\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/31_RESEARCH_COMPLETION.md").write_text("# Offline package\n", encoding="utf-8")
    (root / "outputs/verification/enhancement").mkdir(parents=True)
    report = root / "outputs/research/m4/reports/2026-09-10/20260911T113313925244-121992df"
    report.mkdir(parents=True)
    for name in ("daily_brief.html", "daily_brief.md"):
        (report / name).write_text("OFFLINE DEMO", encoding="utf-8")
    return root, {"root": root}


@pytest.mark.parametrize("kind", ["observation", "research"])
def test_built_upgrade_contains_standalone_settings_and_resolved_instructions(tmp_path, kind):
    builder = script_module("build_" + kind + "_upgrade")
    root, arguments = package_project(tmp_path, kind, builder)
    result = builder.build(**arguments)
    directory = Path(result["directory"])
    manifest = json.loads((directory / "upgrade.json").read_text(encoding="utf-8"))
    assert "scripts/deployment_settings.py" in {entry["path"] for entry in manifest["files"]}
    installer = script_module("apply_" + kind + "_upgrade")
    checked = installer.inspect_upgrade(root, directory)
    entries = checked[1] if kind == "observation" else checked
    assert "scripts/deployment_settings.py" in {entry["path"] for entry in entries}
    install = (directory / "INSTALL.txt").read_text(encoding="utf-8")
    assert PROJECT + "/.venv/bin/python" in install
    assert "--project " + PROJECT in install
    assert "researcher" in install
    assert "research-host:~/" in (directory / "UPLOAD.ps1").read_text(encoding="utf-8")
    with tarfile.open(result["archive"], "r:gz") as archive:
        assert all("/.local/" not in name for name in archive.getnames())
        assert all(b"LOCAL_ONLY_MARKER" not in archive.extractfile(item).read()
                   for item in archive.getmembers() if item.isfile())
    run = subprocess.run([sys.executable, "-X", "utf8", str(directory / ("apply_" + kind + "_upgrade.py")), "--help"],
                         cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False)
    assert run.returncode == 0, run.stderr
    assert "--deployment-config" in run.stdout


@pytest.mark.parametrize("kind", ["observation", "research"])
def test_builder_explicit_target_overrides_local_settings(tmp_path, kind):
    builder = script_module("build_" + kind + "_upgrade")
    _, arguments = package_project(tmp_path, kind, builder)
    result = builder.build(**arguments, project="/home/override/apps/research", account="override", ssh_host="other-host")
    directory = Path(result["directory"])
    assert "--project /home/override/apps/research" in (directory / "INSTALL.txt").read_text(encoding="utf-8")
    assert "other-host:~/" in (directory / "UPLOAD.ps1").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["observation", "research"])
def test_builder_without_target_does_not_create_partial_package(tmp_path, kind):
    builder = script_module("build_" + kind + "_upgrade")
    root, arguments = package_project(tmp_path, kind, builder)
    config = tmp_path / "empty.json"
    config.write_text("{}", encoding="utf-8")
    before = set((root / "deploy").iterdir())
    with pytest.raises(ValueError, match="project_root"):
        builder.build(**arguments, deployment_config=config)
    assert set((root / "deploy").iterdir()) == before


@pytest.mark.parametrize("kind", ["observation", "research"])
def test_explicit_profile_inside_source_tree_is_not_packaged(tmp_path, kind):
    builder = script_module("build_" + kind + "_upgrade")
    root, arguments = package_project(tmp_path, kind, builder)
    config = root / "config/deployment.private.json"
    profile = json.dumps({"project_root": PROJECT, "account": "researcher", "ssh_host": "research-host",
                          "server_ip": "10.20.30.40", "listen_port": 18765})
    config.write_text(profile, encoding="utf-8")
    public_example = root / "config/deployment.example.json"
    public_example.write_text('{"account": "example"}', encoding="utf-8")
    result = builder.build(**arguments, deployment_config=config)
    directory = Path(result["directory"])
    manifest = json.loads((directory / "upgrade.json").read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in manifest["files"]}
    assert "config/deployment.private.json" not in paths
    assert not (directory / "files/config/deployment.private.json").exists()
    if kind == "observation":
        assert "config/deployment.example.json" in paths
    with tarfile.open(result["archive"], "r:gz") as archive:
        assert not any(name.endswith("/config/deployment.private.json") for name in archive.getnames())
        assert all(b"10.20.30.40" not in archive.extractfile(item).read()
                   for item in archive.getmembers() if item.isfile())
    assert config.read_text(encoding="utf-8") == profile


@pytest.mark.parametrize("kind,relative", [("observation", "config/sector_observation_daily.json"),
                                           ("research", "config/m4.json")])
def test_profile_cannot_replace_required_upgrade_file(tmp_path, kind, relative):
    builder = script_module("build_" + kind + "_upgrade")
    root, arguments = package_project(tmp_path, kind, builder)
    profile = root / relative
    profile.write_text(json.dumps({"project_root": PROJECT, "account": "researcher", "ssh_host": "research-host"}), encoding="utf-8")
    before = set((root / "deploy").iterdir())
    with pytest.raises(ValueError):
        builder.build(**arguments, deployment_config=profile)
    assert set((root / "deploy").iterdir()) == before
