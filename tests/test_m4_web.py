"""Windows helper validation. Never start or stop a real service in these tests."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/m4-web.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or POWERSHELL is None,
                                reason="Windows process helper requires Windows PowerShell; Linux service is tested separately")


def ps(command, *, success=True):
    env = dict(os.environ)
    env["MODEL_API_KEY"] = "sk-OFFLINE-FAKE-MUST-NOT-PRINT"
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=env)
    if success:
        assert result.returncode == 0, result.stderr
    assert "sk-OFFLINE-FAKE" not in result.stdout + result.stderr
    return result


def functions(command):
    return f". '{SCRIPT}'; {command}"


def test_web_preview_no_process_or_model_action():
    result = ps(functions("function Start-Process { throw 'MUST_NOT_START' }; $Action='preview'; Invoke-M4Web"))
    plan = json.loads(result.stdout)
    assert plan["executable"] == str(ROOT / ".venv/Scripts/python.exe")
    assert plan["working_directory"] == str(ROOT)
    assert plan["url"] == "http://127.0.0.1:8501"
    assert plan["window_style"] == "Hidden"
    assert plan["model_calls"] == 0 and plan["read_only"]
    assert "run-daily" not in plan["arguments"] and "127.0.0.1" in plan["arguments"]


def valid_identity():
    return f"$p=Get-M4WebPlan '{ROOT}'; $created=[DateTime]::Parse('2026-09-09T01:00:00Z').ToUniversalTime(); " \
           "$proc=[pscustomobject]@{ExecutablePath=$p.executable; CommandLine=$p.command; CreationDate=$created}; " \
           "$s=[pscustomobject]@{creation_time_utc=$created.ToString('o')}; "


def test_exact_process_identity_is_accepted_offline():
    result = ps(functions(valid_identity() + "Assert-M4OwnedWebProcess $proc $s $p; 'VALID_OFFLINE_IDENTITY'"))
    assert "VALID_OFFLINE_IDENTITY" in result.stdout


def test_windows_venv_runtime_child_identity_is_accepted():
    command = valid_identity() + (
        "$proc.ExecutablePath=$p.runtime_executable; "
        "$proc.CommandLine='\"' + $p.runtime_executable + '\" ' + $p.arguments; "
        "$proc | Add-Member -NotePropertyName ParentProcessId -NotePropertyValue 12345; "
        "$s | Add-Member -NotePropertyName role -NotePropertyValue runtime; "
        "$s | Add-Member -NotePropertyName parent_process_id -NotePropertyValue 12345; "
        "Assert-M4OwnedWebProcess $proc $s $p; 'VALID_OFFLINE_RUNTIME'"
    )
    result = ps(functions(command))
    assert "VALID_OFFLINE_RUNTIME" in result.stdout


def test_windows_runtime_wrong_parent_refused():
    command = valid_identity() + (
        "$proc.ExecutablePath=$p.runtime_executable; "
        "$proc.CommandLine='\"' + $p.runtime_executable + '\" ' + $p.arguments; "
        "$proc | Add-Member -NotePropertyName ParentProcessId -NotePropertyValue 99999; "
        "$s | Add-Member -NotePropertyName role -NotePropertyValue runtime; "
        "$s | Add-Member -NotePropertyName parent_process_id -NotePropertyValue 12345; "
        "Assert-M4OwnedWebProcess $proc $s $p"
    )
    result = ps(functions(command), success=False)
    assert result.returncode != 0 and "Runtime parent" in result.stderr


def test_hidden_console_child_never_adopted_as_runtime():
    command = f"$p=Get-M4WebPlan '{ROOT}'; $script:offlineRuntimePath=$p.runtime_executable; " + (
        "function Get-M4WebChildren { param([int]$ParentProcessId); @(" 
        "[pscustomobject]@{ProcessId=24680; ParentProcessId=$ParentProcessId; ExecutablePath=$script:offlineRuntimePath}, "
        "[pscustomobject]@{ProcessId=24681; ParentProcessId=$ParentProcessId; ExecutablePath='C:\\Windows\\System32\\conhost.exe'}) }; "
        "@(Get-M4WebRuntimeChildren 12345 $p) | ConvertTo-Json"
    )
    runtime = json.loads(ps(functions(command)).stdout)
    assert runtime["ProcessId"] == 24680 and runtime["ParentProcessId"] == 12345


@pytest.mark.parametrize("change", [
    "$proc.ExecutablePath='C:\\other\\python.exe';",
    "$proc.CommandLine=$p.command + ' --server.port 9999';",
    "$proc.CommandLine='python.exe -m streamlit run other.py';",
    "$proc.CreationDate=$created.AddSeconds(1);",
])
def test_foreign_or_reused_process_refused(change):
    result = ps(functions(valid_identity() + change + "Assert-M4OwnedWebProcess $proc $s $p"), success=False)
    assert result.returncode != 0 and "Refusing" in result.stderr


def test_web_script_is_utf8_bom_and_never_reads_env():
    data = SCRIPT.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert "-WindowStyle Hidden" in text
    assert "-WorkingDirectory $plan.working_directory" in text
    assert "Get-Content -LiteralPath $Plan.state_file" in text
    assert "Get-Content .env" not in text and "MODEL_API_KEY" not in text
