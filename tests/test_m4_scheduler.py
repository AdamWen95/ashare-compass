"""Read-only scheduler preview and offline XML safeguards, never task registration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "m4-task.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or POWERSHELL is None,
                                reason="Windows Task Scheduler helper requires Windows PowerShell; Linux scheduler is tested separately")


def ps(command: str, *, success: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MODEL_API_KEY"] = "sk-OFFLINE-TEST-MUST-NOT-APPEAR"
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=env,
    )
    if success:
        assert result.returncode == 0, result.stderr
    assert "sk-OFFLINE-TEST" not in result.stdout + result.stderr
    return result


def functions(command: str) -> str:
    return f". '{SCRIPT}'; {command}"


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    path = tmp_path_factory.mktemp("scheduler") / "preview.json"
    ps(f"& '{SCRIPT}' -Action preview -OutputPath '{path}'")
    return json.loads(path.read_text(encoding="utf-8"))


def test_preview_contains_real_absolute_paths_and_no_external_run(plan):
    assert plan["operation"] == "preview_only"
    assert plan["scheduling_verified"] is False
    assert plan["executable"] == str(ROOT / ".venv" / "Scripts" / "python.exe")
    assert plan["working_directory"] == str(ROOT)
    assert plan["arguments"] == "-m ashare_daily run-daily --scheduled"
    assert plan["environment_file"] == str(ROOT / ".env")
    assert len(plan["external_calls"]) == 3
    assert "sk-OFFLINE-TEST" not in json.dumps(plan)


def test_task_xml_fixed_offset_daily_and_no_registration_trigger(plan):
    xml = ET.fromstring(plan["task_xml"])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    get = lambda path: xml.find(path, ns).text
    assert get("t:Triggers/t:CalendarTrigger/t:StartBoundary").endswith("21:00:00+08:00")
    assert get("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval") == "1"
    assert len(xml.find("t:Triggers", ns)) == 1
    assert get("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert get("t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert get("t:Settings/t:WakeToRun") == "false"
    assert get("t:Settings/t:StartWhenAvailable") == "true"
    assert get("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert get("t:Settings/t:ExecutionTimeLimit") == "PT2H"
    assert xml.find("t:Settings/t:RestartOnFailure", ns) is None
    assert get("t:Actions/t:Exec/t:Command") == plan["executable"]
    assert get("t:Actions/t:Exec/t:WorkingDirectory") == str(ROOT)


@pytest.mark.parametrize(
    "now, zone, next_beijing, next_local",
    [
        ("2026-09-09T12:59:59Z", "China Standard Time", "2026-09-09T21:00:00+08:00", "2026-09-09T21:00:00+08:00"),
        ("2026-09-09T13:00:00Z", "China Standard Time", "2026-09-10T21:00:00+08:00", "2026-09-10T21:00:00+08:00"),
        ("2026-07-01T08:00:00Z", "Eastern Standard Time", "2026-07-01T21:00:00+08:00", "2026-07-01T09:00:00-04:00"),
        ("2026-01-01T08:00:00Z", "Eastern Standard Time", "2026-01-01T21:00:00+08:00", "2026-01-01T08:00:00-05:00"),
        ("2026-12-31T16:00:00Z", "UTC", "2027-01-01T21:00:00+08:00", "2027-01-01T13:00:00+00:00"),
    ],
)
def test_next_beijing_trigger_is_independent_of_local_dst(now, zone, next_beijing, next_local):
    result = ps(functions(f"Get-M4TriggerTimes -Now ([DateTimeOffset]::Parse('{now}')) -LocalZone ([TimeZoneInfo]::FindSystemTimeZoneById('{zone}')) | ConvertTo-Json"))
    times = json.loads(result.stdout)
    assert times["next_beijing"] == next_beijing
    assert times["next_local"] == next_local
    assert times["start_boundary"] == next_beijing


def test_install_requires_confirmation_before_connecting_to_scheduler():
    command = functions("function Open-M4Scheduler { throw 'MUST_NOT_CONTACT_SCHEDULER' }; $Action='install'; $ConfirmInstall=$false; Invoke-M4Scheduler")
    result = ps(command, success=False)
    assert result.returncode != 0
    assert "Installation is not authorized" in result.stderr
    assert "MUST_NOT_CONTACT_SCHEDULER" not in result.stderr


@pytest.mark.parametrize("field,replacement", [
    ("Description", "foreign-project"),
    ("Command", "C:\\other\\python.exe"),
    ("Arguments", "-m unrelated"),
    ("WorkingDirectory", "C:\\other"),
])
def test_foreign_task_cannot_be_started_or_changed(field, replacement):
    node = "$x.Task.RegistrationInfo" if field == "Description" else "$x.Task.Actions.Exec"
    command = functions(
        f"$p=Get-M4Plan -Root '{ROOT}'; [xml]$x=New-M4TaskXml $p; "
        f"{node}.{field}='{replacement}'; "
        "Assert-M4OwnedTask ([pscustomobject]@{Xml=$x.OuterXml}) $p"
    )
    result = ps(command, success=False)
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr


def test_repeat_install_comparison_preserves_existing_start_date():
    command = functions(
        f"$p=Get-M4Plan -Root '{ROOT}'; $expected=New-M4TaskXml $p; [xml]$x=$expected; "
        "$x.Task.Triggers.CalendarTrigger.StartBoundary='2020-01-01T21:00:00+08:00'; "
        "Test-M4SameTask ([pscustomobject]@{Xml=$x.OuterXml}) $expected"
    )
    assert ps(command).stdout.strip() == "True"


def test_task_xml_escapes_project_and_account_text():
    command = functions(
        f"$p=Get-M4Plan -Root '{ROOT}'; $p.account='User<&>'; $p.working_directory='D:\\Example & Name'; "
        "[xml]$x=New-M4TaskXml $p; $x.Task.RegistrationInfo.Author; $x.Task.Actions.Exec.WorkingDirectory"
    )
    assert ps(command).stdout.splitlines() == ["User<&>", "D:\\Example & Name"]


def test_windows_omitted_default_xml_nodes_still_compare_unchanged():
    command = functions(
        f"$p=Get-M4Plan -Root '{ROOT}'; $expected=New-M4TaskXml $p; [xml]$x=$expected; "
        "foreach($name in @('WakeToRun','RunLevel','Priority','Hidden','AllowStartOnDemand','RunOnlyIfIdle')) { "
        "$node=$x.SelectSingleNode(\"//*[local-name()='$name']\"); $null=$node.ParentNode.RemoveChild($node) }; "
        "Test-M4SameTask ([pscustomobject]@{Xml=$x.OuterXml}) $expected"
    )
    assert ps(command).stdout.strip() == "True"


@pytest.mark.parametrize("boundary", ["2026-09-09T20:00:00+08:00", "2026-09-09T21:00:00+07:00", "2026-09-09T21:00:00"])
def test_repeat_install_detects_time_or_explicit_offset_change(boundary):
    command = functions(
        f"$p=Get-M4Plan -Root '{ROOT}'; $expected=New-M4TaskXml $p; [xml]$x=$expected; "
        f"$x.Task.Triggers.CalendarTrigger.StartBoundary='{boundary}'; "
        "Test-M4SameTask ([pscustomobject]@{Xml=$x.OuterXml}) $expected"
    )
    assert ps(command).stdout.strip() == "False"


def test_status_reads_effective_settings_when_exported_xml_omits_wake_to_run():
    command = functions(
        f"$p=Get-M4Plan -Root '{ROOT}'; [xml]$x=New-M4TaskXml $p; "
        "$node=$x.SelectSingleNode(\"//*[local-name()='WakeToRun']\"); $null=$node.ParentNode.RemoveChild($node); "
        "$task=[pscustomobject]@{ Xml=$x.OuterXml; State=3; Enabled=$true; "
        "NextRunTime=[datetime]'2026-09-09T21:00:00'; LastRunTime=[datetime]'2026-09-09T17:00:00'; "
        "LastTaskResult=0; NumberOfMissedRuns=0; Definition=[pscustomobject]@{ "
        "Principal=[pscustomobject]@{LogonType=3}; Settings=[pscustomobject]@{StartWhenAvailable=$true; WakeToRun=$false} } }; "
        "Get-M4ActualState $task | ConvertTo-Json"
    )
    state = json.loads(ps(command).stdout)
    assert state["registered"] is True
    assert state["actual_wake_to_run"] is False
    assert state["actual_start_when_available"] is True
    assert state["actual_logon_type"] == "InteractiveToken"
    assert state["last_task_result_hex"] == "0x00000000"
