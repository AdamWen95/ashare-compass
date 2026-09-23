"""Offline deployment transactions; never contact SSH, systemd, sources or models."""
import importlib.util
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("offline_observation_upgrade", ROOT / "scripts/apply_observation_upgrade.py")
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)
build_spec = importlib.util.spec_from_file_location("offline_observation_builder", ROOT / "scripts/build_observation_upgrade.py")
builder = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(builder)
sys.path.pop(0)


@pytest.fixture
def package(tmp_path):
    project, package = tmp_path / "project", tmp_path / "package"
    (project / "src/ashare_daily").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "config/m4.json").write_text("{}", encoding="utf-8")
    entries = []
    payloads = {"src/ashare_daily/one.py": b"# new\n", upgrade.DAILY_CONFIG: b"{}\n",
                upgrade.TEST_SUPPORT: b"# OFFLINE support\n"}
    for name, body in payloads.items():
        before = b"# old\n" if name.endswith("one.py") else None
        if before:
            (project / name).write_bytes(before)
        target = package / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        entries.append({"path": name, "sha256": upgrade.digest(body),
                        "accepted_previous_sha256": [upgrade.digest(before) if before else None]})
    manifest = {"schema_version": upgrade.SCHEMA, "daily_config": upgrade.DAILY_CONFIG, "files": entries}
    (package / "upgrade.json").write_text(json.dumps(manifest), encoding="utf-8")
    return project, package


def test_manifest_includes_required_config_and_exact_test_helper(package):
    project, source = package
    _, entries = upgrade.inspect_upgrade(project, source)
    assert {entry["path"] for entry in entries} == {"src/ashare_daily/one.py", upgrade.DAILY_CONFIG, upgrade.TEST_SUPPORT}


@pytest.mark.parametrize("path", [".env", "data/research/market.sqlite3", "outputs/verification/other.py",
                                  "outputs/research/report.json", "../escaped.py", "config/../escape.json", "/absolute.py"])
def test_forbids_data_secrets_and_unlisted_output_paths(package, path):
    project, source = package
    manifest = json.loads((source / "upgrade.json").read_text())
    manifest["files"][0]["path"] = path
    (source / "upgrade.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="升级路径"):
        upgrade.inspect_upgrade(project, source)


@pytest.mark.parametrize("mode", ["tampered_payload", "unknown_server_edit", "missing_helper"])
def test_unverified_payload_or_server_edit_never_installed(package, mode):
    project, source = package
    if mode == "tampered_payload":
        (source / "files/src/ashare_daily/one.py").write_bytes(b"# changed\n")
    elif mode == "unknown_server_edit":
        (project / "src/ashare_daily/one.py").write_bytes(b"# user-owned edit\n")
    else:
        manifest = json.loads((source / "upgrade.json").read_text())
        manifest["files"] = manifest["files"][:-1]
        (source / "upgrade.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        upgrade.inspect_upgrade(project, source)


def test_existing_timer_definition_unchanged_and_explicit_daily_config():
    project = PurePosixPath("/home/researcher/apps/ashare-daily-research")
    original = upgrade.build_units(str(project))
    desired = upgrade.daily_unit(project)
    assert desired.replace(" --config " + upgrade.DAILY_CONFIG, "") == original[upgrade.DAILY]
    assert "21:00:00 Asia/Shanghai" in original[upgrade.TIMER]
    assert "Persistent=true" in original[upgrade.TIMER]


@pytest.fixture
def transaction(package, tmp_path, monkeypatch):
    project, source = package
    _, entries = upgrade.inspect_upgrade(project, source)
    directory = tmp_path / "user-units"
    directory.mkdir()
    definitions = {name: "# OFFLINE owned unit\n" + (
        "ExecStart=python -m ashare_daily run-daily --scheduled\n" if name == upgrade.DAILY else "OFFLINE " + name + "\n")
        for name in (upgrade.DAILY, upgrade.TIMER, upgrade.WEB)}
    monkeypatch.setattr(upgrade, "build_units", lambda _: dict(definitions))
    for name, body in definitions.items():
        (directory / name).write_text(body, encoding="utf-8")
    units = upgrade.inspect_units(project, directory)
    states = {name: {"ActiveState": "inactive" if name == upgrade.DAILY else "active",
                    "UnitFileState": "static" if name == upgrade.DAILY else "enabled",
                    "FragmentPath": str(directory / name)} for name in definitions}
    calls = []

    def fake_systemd(*arguments, check=True):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(upgrade, "systemd", fake_systemd)
    monkeypatch.setattr(upgrade, "service_state", lambda name: dict(states[name]))
    monkeypatch.setattr(upgrade, "verify_unit_file", lambda *_: None)
    monkeypatch.setattr(upgrade, "check_web", lambda: None)
    return project, entries, directory, units, tmp_path / "backup", Path(sys.executable), calls, states


@pytest.mark.parametrize("failure", [None, "tests", "unit_validation", "web"])
def test_transaction_preserves_timer_and_rolls_back_every_target(transaction, monkeypatch, failure):
    project, entries, directory, units, backup, python, calls, _ = transaction
    before = {name: (directory / name).read_bytes() for name in units}

    def fail(*_):
        raise RuntimeError("OFFLINE deliberate failure")

    if failure == "unit_validation":
        monkeypatch.setattr(upgrade, "verify_unit_file", fail)
    if failure == "web":
        monkeypatch.setattr(upgrade, "check_web", fail)
    regression = lambda *_: subprocess.CompletedProcess([], 1 if failure == "tests" else 0)
    if failure:
        with pytest.raises(RuntimeError):
            upgrade.apply_changes(project, entries, directory, units, backup, python, regression=regression)
        assert (project / "src/ashare_daily/one.py").read_bytes() == b"# old\n"
        assert not (project / upgrade.DAILY_CONFIG).exists()
        assert (directory / upgrade.DAILY).read_bytes() == before[upgrade.DAILY]
        assert json.loads((backup / "result.json").read_text())["status"] == "rolled_back"
    else:
        result = upgrade.apply_changes(project, entries, directory, units, backup, python, regression=regression)
        assert result["status"] == "applied_and_verified"
        assert upgrade.DAILY_CONFIG in (directory / upgrade.DAILY).read_text()
        assert (project / "src/ashare_daily/one.py").read_bytes() == b"# new\n"
    assert (directory / upgrade.TIMER).read_bytes() == before[upgrade.TIMER]
    assert (directory / upgrade.WEB).read_bytes() == before[upgrade.WEB]
    assert not any(upgrade.TIMER in call or "enable" in call or "run-daily" in call for call in calls)
    assert (backup / "daily.service.before").read_bytes() == before[upgrade.DAILY]


@pytest.mark.parametrize("mutation", ["active", "fragment", "dropin", "unknown_unit"])
def test_refuses_running_task_or_modified_units_before_changes(transaction, mutation):
    project, entries, directory, units, backup, python, calls, states = transaction
    if mutation == "active":
        states[upgrade.DAILY]["ActiveState"] = "activating"
    elif mutation == "fragment":
        states[upgrade.DAILY]["FragmentPath"] = "/some/other/user/service"
    elif mutation == "dropin":
        (directory / (upgrade.DAILY + ".d")).mkdir()
    else:
        (directory / upgrade.DAILY).write_text("# User customized\n")
    with pytest.raises(ValueError):
        checked = upgrade.inspect_units(project, directory)
        upgrade.apply_changes(project, entries, directory, checked, backup, python)
    assert (project / "src/ashare_daily/one.py").read_bytes() == b"# old\n"
    assert not backup.exists()
    assert not calls


def test_concurrent_edit_during_failed_validation_is_preserved_and_reported(transaction):
    project, entries, directory, units, backup, python, _, _ = transaction

    def regression(*_):
        (project / "src/ashare_daily/one.py").write_bytes(b"# user edit during deployment\n")
        return subprocess.CompletedProcess([], 1)

    with pytest.raises(RuntimeError, match="回归未通过"):
        upgrade.apply_changes(project, entries, directory, units, backup, python, regression=regression)
    assert (project / "src/ashare_daily/one.py").read_bytes() == b"# user edit during deployment\n"
    result = json.loads((backup / "result.json").read_text())
    assert result["status"] == "rollback_failed" and result["rollback_errors"]


def test_source_inventory_contains_only_allowed_code_and_test_support(monkeypatch):
    original_read = builder.read_file
    def checked_read(path, *args, **kwargs):
        assert Path(path) != ROOT / upgrade.TEST_SUPPORT  # Ignored artifacts are not a build prerequisite.
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(builder, "read_file", checked_read)
    files = builder.source_files(ROOT)
    assert upgrade.DAILY_CONFIG in files and upgrade.TEST_SUPPORT in files
    assert b"scripts/record_sector_command.py" in files[upgrade.TEST_SUPPORT]
    assert "scripts/record_sector_command.py" in files
    assert "scripts/apply_observation_upgrade.py" in files
    assert "tests/test_observation_upgrade.py" in files
    assert all(upgrade.allowed_path(name) for name in files)
    assert not any(name.endswith(".sqlite3") or name.startswith("data/") or ".env" in name for name in files)


def test_node_preflight_checks_hash_before_executing(tmp_path, monkeypatch):
    node = tmp_path / ".tools/node/bin/node"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"OFFLINE_NODE")
    pin = {"schema_version":"node-runtime-v1", "platform":"linux-x64", "executable":".tools/node/bin/node",
        "version":"v24.21.0", "executable_sha256":upgrade.digest(node.read_bytes())}
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(arguments, 0, "v24.21.0\n", "")
    monkeypatch.setattr(upgrade.subprocess, "run", run)
    upgrade.check_node_runtime(tmp_path, pin)
    assert len(calls) == 1
    node.write_bytes(b"MODIFIED_NODE")
    with pytest.raises(ValueError, match="哈希"):
        upgrade.check_node_runtime(tmp_path, pin)
    assert len(calls) == 1
