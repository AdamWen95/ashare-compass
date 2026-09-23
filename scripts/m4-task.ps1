[CmdletBinding()]
param(
    [ValidateSet('preview', 'install', 'status', 'start', 'disable', 'uninstall')]
    [string]$Action = 'preview',
    [string]$ProjectRoot = '',
    [string]$OutputPath = '',
    [switch]$ConfirmInstall
)

# No credentials are loaded by this script. The application reads its project .env.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }

function Get-M4TriggerTimes {
    param(
        [DateTimeOffset]$Now = [DateTimeOffset]::UtcNow,
        [TimeZoneInfo]$LocalZone = [TimeZoneInfo]::Local
    )
    $beijingNow = $Now.ToOffset([TimeSpan]::FromHours(8))
    $next = [DateTimeOffset]::new($beijingNow.Year, $beijingNow.Month, $beijingNow.Day, 21, 0, 0, [TimeSpan]::FromHours(8))
    if ($next -le $beijingNow) { $next = $next.AddDays(1) }
    return [ordered]@{
        timezone = 'Asia/Shanghai'
        local_timezone_id = $LocalZone.Id
        local_supports_daylight_saving = $LocalZone.SupportsDaylightSavingTime
        current_beijing = $beijingNow.ToString('o')
        current_local = ([TimeZoneInfo]::ConvertTime($Now, $LocalZone)).ToString('o')
        next_beijing = $next.ToString('yyyy-MM-ddTHH:mm:sszzz')
        next_local = ([TimeZoneInfo]::ConvertTime($next, $LocalZone)).ToString('yyyy-MM-ddTHH:mm:sszzz')
        start_boundary = $next.ToString('yyyy-MM-ddTHH:mm:sszzz')
        interval_days = 1
        note = '21:00 is the trigger time, not a promised report completion time.'
    }
}

function ConvertTo-M4XmlText {
    param([string]$Value)
    return [Security.SecurityElement]::Escape($Value)
}

function Get-M4Plan {
    param([string]$Root)
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    $python = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $owner = 'ashare-daily-research/M4/v1|' + $resolvedRoot.ToLowerInvariant()
    $times = Get-M4TriggerTimes
    $arguments = '-m ashare_daily run-daily --scheduled'
    return [ordered]@{
        schema_version = 'm4-scheduler-preview-v1'
        operation = 'preview_only'
        task_name = 'ashare-daily-research-M4-Daily'
        task_path = '\'
        owner_marker = $owner
        account = $identity.Name
        account_sid = $identity.User.Value
        is_sandbox_account = ($identity.Name -match 'CodexSandbox')
        executable = $python
        executable_exists = (Test-Path -LiteralPath $python -PathType Leaf)
        arguments = $arguments
        command = ('"{0}" {1}' -f $python, $arguments)
        working_directory = $resolvedRoot
        environment_file = (Join-Path $resolvedRoot '.env')
        schedule = $times
        settings = [ordered]@{
            logon_type = 'InteractiveToken'
            require_user_logged_on = $true
            run_level = 'LeastPrivilege'
            stores_windows_password = $false
            start_when_available = $true
            wake_to_run = $false
            run_only_if_idle = $false
            disallow_start_on_batteries = $false
            stop_on_batteries = $false
            multiple_instances = 'IgnoreNew'
            execution_time_limit = 'PT2H'
            restart_on_failure_count = 0
            enabled_after_install = $true
        }
        catch_up_policy = 'At most one current Beijing-day application identity; before 21:00 defer. No automatic multi-day historical model backlog.'
        external_calls = @('BaoStock trading calendar and configured market sample only', 'Currently enabled M3 material sources only', 'Configured Modex model within application persistent daily budget; no model on non-trading days')
        approval = 'Not registered or enabled by preview. Install requires explicit user confirmation and -ConfirmInstall.'
        scheduling_verified = $false
    }
}

function New-M4TaskXml {
    param($Plan)
    $author = ConvertTo-M4XmlText $Plan.account
    $sid = ConvertTo-M4XmlText $Plan.account_sid
    $owner = ConvertTo-M4XmlText $Plan.owner_marker
    $command = ConvertTo-M4XmlText $Plan.executable
    $arguments = ConvertTo-M4XmlText $Plan.arguments
    $working = ConvertTo-M4XmlText $Plan.working_directory
    $boundary = $Plan.schedule.start_boundary
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>$author</Author><Description>$owner</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT2H</ExecutionTimeLimit><Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>$command</Command><Arguments>$arguments</Arguments><WorkingDirectory>$working</WorkingDirectory></Exec></Actions>
</Task>
"@
}

function Open-M4Scheduler {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    return $service
}

function Find-M4Task {
    param($Folder, [string]$Name)
    try { return $Folder.GetTask($Name) }
    catch {
        # Only a real file/task-not-found response means there is no task.
        $code = $_.Exception.HResult
        if ($_.Exception.InnerException) { $code = $_.Exception.InnerException.HResult }
        if ($code -in @(-2147024894, -2147024893)) { return $null }
        throw
    }
}

function Assert-M4OwnedTask {
    param($Existing, $Plan)
    [xml]$definition = $Existing.Xml
    if ([string]$definition.Task.RegistrationInfo.Description -ne $Plan.owner_marker -or
        [string]$definition.Task.Actions.Exec.Command -ne $Plan.executable -or
        [string]$definition.Task.Actions.Exec.Arguments -ne $Plan.arguments -or
        [string]$definition.Task.Actions.Exec.WorkingDirectory -ne $Plan.working_directory) {
        throw 'Task name belongs to a different project or command; refusing to overwrite, start, disable or remove it.'
    }
}

function Test-M4SameTask {
    param($Existing, [string]$ExpectedXml)
    function Get-Semantics {
        param([string]$Text)
        [xml]$document = $Text
        $defaults = @{
            'Task/Principals/Principal/RunLevel' = 'LeastPrivilege'
            'Task/Settings/AllowHardTerminate' = 'true'
            'Task/Settings/AllowStartOnDemand' = 'true'
            'Task/Settings/Enabled' = 'true'
            'Task/Settings/Hidden' = 'false'
            'Task/Settings/RunOnlyIfIdle' = 'false'
            'Task/Settings/RunOnlyIfNetworkAvailable' = 'false'
            'Task/Settings/WakeToRun' = 'false'
            'Task/Settings/Priority' = '7'
            'Task/Triggers/CalendarTrigger/Enabled' = 'true'
        }
        $values = @()
        foreach ($leaf in $document.SelectNodes('//*[not(*)]')) {
            $names = @($leaf.LocalName)
            $ancestor = $leaf.ParentNode
            while ($ancestor -and $ancestor.NodeType -eq [Xml.XmlNodeType]::Element) {
                $names = @($ancestor.LocalName) + $names
                $ancestor = $ancestor.ParentNode
            }
            $path = $names -join '/'
            if ($path -like 'Task/RegistrationInfo/*') { continue }
            $value = $leaf.InnerText
            if ($defaults.ContainsKey($path) -and $defaults[$path] -eq $value) { continue }
            if ($path -eq 'Task/Triggers/CalendarTrigger/StartBoundary' -and $value -match '^\d{4}-\d{2}-\d{2}T(.+[+-]\d{2}:\d{2})$') {
                # Retain the installed date; time AND explicit UTC offset must match.
                $value = $Matches[1]
            }
            $values += $path + '=' + $value
        }
        return (($values | Sort-Object) -join "`n")
    }
    # Task Scheduler reorders XML and omits nodes equal to schema defaults.
    return (Get-Semantics $Existing.Xml) -eq (Get-Semantics $ExpectedXml)
}

function Get-M4ActualState {
    param($Existing)
    if ($null -eq $Existing) { return [ordered]@{ registered = $false; status = 'not_registered' } }
    [xml]$xml = $Existing.Xml
    $definition = $Existing.Definition
    $settings = $definition.Settings
    $logonTypes = @('None', 'Password', 'S4U', 'InteractiveToken', 'Group', 'ServiceAccount', 'InteractiveTokenOrPassword')
    $next = [datetime]$Existing.NextRunTime
    $last = [datetime]$Existing.LastRunTime
    $nextOffset = [DateTimeOffset]::new($next, [TimeZoneInfo]::Local.GetUtcOffset($next))
    return [ordered]@{
        registered = $true
        state = [int]$Existing.State
        enabled = [bool]$Existing.Enabled
        last_run_local = $last.ToString('o')
        next_run_local = $nextOffset.ToString('o')
        next_run_beijing = $nextOffset.ToOffset([TimeSpan]::FromHours(8)).ToString('o')
        last_task_result_decimal = [long]$Existing.LastTaskResult
        last_task_result_hex = ('0x{0:X8}' -f [long]$Existing.LastTaskResult)
        missed_run_count = [int]$Existing.NumberOfMissedRuns
        actual_start_boundary = [string]$xml.Task.Triggers.CalendarTrigger.StartBoundary
        actual_logon_type = $logonTypes[[int]$definition.Principal.LogonType]
        actual_start_when_available = [bool]$settings.StartWhenAvailable
        actual_wake_to_run = [bool]$settings.WakeToRun
        note = 'Registration, Ready state, and task exit code alone do not establish report completeness; inspect the corresponding application run record.'
    }
}

function Write-M4Result {
    param($Result, [string]$Path)
    $json = $Result | ConvertTo-Json -Depth 12
    if ($Path) {
        $full = [IO.Path]::GetFullPath($Path)
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($full)) | Out-Null
        [IO.File]::WriteAllText($full, $json, [Text.UTF8Encoding]::new($false))
    }
    Write-Output $json
}

function Invoke-M4Scheduler {
    $plan = Get-M4Plan -Root $ProjectRoot
    $xml = New-M4TaskXml -Plan $plan
    if ($Action -eq 'preview') {
        $plan.task_xml = $xml
        try {
            $service = Open-M4Scheduler
            $existing = Find-M4Task -Folder $service.GetFolder('\') -Name $plan.task_name
            $plan.current_task = Get-M4ActualState $existing
        }
        catch { $plan.current_task = @{ registered = $null; status = 'unavailable'; reason = $_.Exception.Message } }
        Write-M4Result $plan $OutputPath
        return
    }
    if ($Action -eq 'install' -and -not $ConfirmInstall) {
        throw 'Installation is not authorized by preview. Review -Action preview, then explicitly pass -ConfirmInstall after user confirmation.'
    }
    if ($Action -eq 'install' -and $plan.is_sandbox_account) {
        throw 'Refusing installation for a Codex sandbox account. Run the reviewed command in your own Windows PowerShell session.'
    }
    if ($Action -eq 'install' -and -not $plan.executable_exists) { throw 'Project .venv Python does not exist.' }
    $service = Open-M4Scheduler
    $folder = $service.GetFolder('\')
    $existing = Find-M4Task -Folder $folder -Name $plan.task_name
    if ($null -ne $existing) { Assert-M4OwnedTask $existing $plan }
    $outcome = $Action
    $trigger = $null
    switch ($Action) {
        'install' {
            if ($null -ne $existing -and (Test-M4SameTask $existing $xml) -and $existing.Enabled) { $outcome = 'unchanged' }
            else {
                # TASK_CREATE_OR_UPDATE=6; TASK_LOGON_INTERACTIVE_TOKEN=3. No password.
                $existing = $folder.RegisterTask($plan.task_name, $xml, 6, $plan.account_sid, $null, 3, $null)
                $outcome = 'installed_or_updated'
            }
        }
        'status' { }
        'start' {
            if ($null -eq $existing) { throw 'Task is not installed.' }
            if (-not $existing.Enabled) { throw 'Task is disabled; review preview and explicitly reinstall to enable.' }
            $running = $existing.Run($null)
            $trigger = [ordered]@{ instance_guid = $running.InstanceGuid; task_engine_pid = $running.EnginePID; requested_at = [DateTimeOffset]::Now.ToString('o') }
            $outcome = 'trigger_requested_not_report_success'
        }
        'disable' {
            if ($null -ne $existing) { $existing.Enabled = $false }
            else { $outcome = 'already_absent' }
        }
        'uninstall' {
            if ($null -ne $existing) { $folder.DeleteTask($plan.task_name, 0); $existing = $null }
            else { $outcome = 'already_absent' }
        }
    }
    Write-M4Result ([ordered]@{ operation = $Action; outcome = $outcome; task_name = $plan.task_name; working_directory = $plan.working_directory; trigger = $trigger; actual = (Get-M4ActualState $existing) }) $OutputPath
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-M4Scheduler }
