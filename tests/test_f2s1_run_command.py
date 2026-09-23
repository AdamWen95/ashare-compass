"""Actual CLI subprocess regression; dry run reads real config and writes no data."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/record_sector_command.py"


def wrapper_module():
    spec = importlib.util.spec_from_file_location("recorded_sector_command", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_fixture(tmp_path, monkeypatch):
    module = wrapper_module()
    environment = tmp_path / ".venv"
    directory = environment / ("Scripts" if os.name == "nt" else "bin")
    directory.mkdir(parents=True)
    interpreter = directory / ("python.exe" if os.name == "nt" else "python")
    interpreter.write_bytes(b"interpreter identity fixture; never executed")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr(sys, "executable", str(interpreter))
    return module, environment, interpreter


def test_interpreter_link_keeps_virtual_environment_path(tmp_path, monkeypatch):
    module, _, interpreter = _runtime_fixture(tmp_path, monkeypatch)
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == interpreter or original_is_symlink(path))
    # A link is forbidden for ordinary project/data paths but is normal at the
    # executable leaf of a verified Linux venv. No link privileges needed here.
    with pytest.raises(ValueError, match="symlink"):
        module.safe_project_path(interpreter.relative_to(tmp_path))
    accepted = module.project_interpreter()
    assert accepted == interpreter
    assert module.child_command(accepted, ["--help"])[0] == str(interpreter)


@pytest.mark.parametrize("mismatch", ["prefix", "executable"])
def test_wrapper_rejects_another_python_environment(tmp_path, monkeypatch, mismatch):
    module, _, _ = _runtime_fixture(tmp_path, monkeypatch)
    if mismatch == "prefix":
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "another-environment"))
    else:
        other = tmp_path / "other-python"
        other.write_bytes(b"other executable")
        monkeypatch.setattr(sys, "executable", str(other))
    with pytest.raises(ValueError, match="this project's virtual environment"):
        module.project_interpreter()


@pytest.mark.parametrize("linked_parent", [".venv", "runtime_bin", "data/research"])
def test_runtime_exception_does_not_allow_linked_parent_or_data(tmp_path, monkeypatch, linked_parent):
    module, environment, interpreter = _runtime_fixture(tmp_path, monkeypatch)
    linked = interpreter.parent if linked_parent == "runtime_bin" else tmp_path / linked_parent
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == linked or original_is_symlink(path))
    with pytest.raises(ValueError, match="symlink"):
        if linked_parent == "data/research":
            module.safe_project_path("data/research/market.sqlite3")
        else:
            module.project_interpreter()


def test_wrapper_targets_executing_package_entrypoint():
    module = wrapper_module()
    command = module.child_command(sys.executable, ["--help"])
    assert command[command.index("-m") + 1] == "ashare_daily"
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0
    assert "sector" in result.stdout and "run-daily" in result.stdout


def test_real_wrapper_dry_run_executes_cli_and_records_nonempty_json():
    # Existing real archived IDs are only labels here: --dry-run exits before
    # snapshot/network/database work. Never create a research fixture or token.
    directory = ROOT / "outputs/research/sse_szse_a/sector_first"
    selections = sorted(path.name for path in directory.glob("sector-*") if (path / "sector_selection.json").is_file())
    if not selections:
        pytest.skip("real archived selection absent; do not fabricate research data")
    command = [sys.executable, "-B", str(SCRIPT), "--", "sector", "prepare", "--selection", selections[0], "--dry-run"]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert process.returncode == 0, process.stderr
    wrapper_lines = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    record_directory = Path(wrapper_lines[-1]["record_directory"])
    result = json.loads((record_directory / "result.json").read_text(encoding="utf-8"))
    actual_output = json.loads(Path(result["stdout_path"]).read_text(encoding="utf-8"))
    assert result["command"][result["command"].index("-m") + 1] == "ashare_daily"
    assert result["child_exit_code"] == result["wrapper_exit_code"] == 0
    assert result["stdout_bytes"] > 0 and result["cli_output_present"]
    assert result["invalid_empty_success"] is False and result["child_running_after_return"] is False
    assert actual_output["status"] == "dry_run" and actual_output["operation"] == "prepare"
    assert actual_output["network_requests"] == actual_output["model_calls"] == 0
    assert result["database_sizes_before"] == result["database_sizes_after"]
