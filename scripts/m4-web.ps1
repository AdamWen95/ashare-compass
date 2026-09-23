[CmdletBinding()]
param(
    [ValidateSet('preview', 'start', 'status', 'stop')]
    [string]$Action = 'preview',
    [string]$ProjectRoot = ''
)

# This helper never reads .env and never starts a research job.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }

function Get-M4WebPlan {
    param([string]$Root)
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    $python = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
    $app = Join-Path $resolvedRoot 'streamlit_app.py'
    $venvConfig = Join-Path $resolvedRoot '.venv\pyvenv.cfg'
    $runtime = $python
    if (Test-Path -LiteralPath $venvConfig -PathType Leaf) {
        $homeLines = @(Get-Content -LiteralPath $venvConfig -Encoding UTF8 | Where-Object { $_ -match '^home\s*=\s*(.+)$' })
        if ($homeLines.Count -eq 1 -and $homeLines[0] -match '^home\s*=\s*(.+)$') {
            $runtime = Join-Path $Matches[1].Trim() 'python.exe'
        }
    }
    $directory = Join-Path $resolvedRoot 'outputs\research\m4\viewer'
    $arguments = '-m streamlit run "' + $app + '" --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false --server.fileWatcherType none'
    return [ordered]@{
        schema_version = 'm4-web-process-v1'
        owner_marker = 'ashare-daily-research/M4/web/v1|' + $resolvedRoot.ToLowerInvariant()
        operation = 'preview'
        executable = $python
        runtime_executable = $runtime
        executable_exists = (Test-Path -LiteralPath $python -PathType Leaf)
        app_file = $app
        app_exists = (Test-Path -LiteralPath $app -PathType Leaf)
        arguments = $arguments
        command = ('"{0}" {1}' -f $python, $arguments)
        working_directory = $resolvedRoot
        url = 'http://127.0.0.1:8501'
        bind_address = '127.0.0.1'
        port = 8501
        window_style = 'Hidden'
        read_only = $true
        model_calls = 0
        state_file = (Join-Path $directory 'web-process.json')
        log_directory = $directory
        note = 'Read-only viewer only. It does not collect data, run research, or install any scheduled task.'
    }
}

function Read-M4WebState {
    param($Plan)
    if (-not (Test-Path -LiteralPath $Plan.state_file -PathType Leaf)) { return $null }
    if ((Get-Item -LiteralPath $Plan.state_file).Length -gt 100000) { throw 'Viewer state file exceeds its size limit.' }
    try { $state = Get-Content -LiteralPath $Plan.state_file -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'Viewer state file is damaged. It was retained; no process has been stopped.' }
    foreach ($field in @('owner_marker', 'executable', 'runtime_executable', 'arguments', 'working_directory')) {
        if (-not $state.PSObject.Properties[$field] -or $state.$field -cne $Plan.$field) {
            throw 'Viewer state does not belong to this exact project configuration. No process has been stopped.'
        }
    }
    if (-not $state.PSObject.Properties['process_id'] -or -not $state.PSObject.Properties['creation_time_utc']) {
        throw 'Viewer state lacks process identity. No process has been stopped.'
    }
    if (-not $state.PSObject.Properties['processes'] -or @($state.processes).Count -lt 1 -or @($state.processes).Count -gt 2) {
        throw 'Viewer state lacks the exact launcher/runtime process identities. No process has been stopped.'
    }
    return $state
}

function Get-M4WebCimProcess {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { throw 'Invalid recorded process id.' }
    return Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = ' + $ProcessId) -ErrorAction Stop
}

function Get-M4WebCreationTime {
    param($Process)
    if ($null -eq $Process.CreationDate) { throw 'Cannot verify the process creation time.' }
    return ([DateTime]$Process.CreationDate).ToUniversalTime().ToString('o')
}

function Assert-M4OwnedWebProcess {
    param($Process, $State, $Plan)
    if ($null -eq $Process) { throw 'Recorded viewer process no longer exists.' }
    $expectedExecutable = $Plan.executable
    if ($State.PSObject.Properties['role'] -and $State.role -eq 'runtime') {
        $expectedExecutable = $Plan.runtime_executable
        if ([int]$Process.ParentProcessId -ne [int]$State.parent_process_id) {
            throw 'Runtime parent process id does not match the recorded launcher. Refusing to stop this process.'
        }
    }
    if (-not [String]::Equals([string]$Process.ExecutablePath, [string]$expectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Executable path does not match the project virtual environment. Refusing to stop this process.'
    }
    $match = [regex]::Match([string]$Process.CommandLine, '^\s*(?:"([^"]+)"|(\S+))\s+(.+?)\s*$')
    if (-not $match.Success) { throw 'Cannot verify the viewer command line. Refusing to stop this process.' }
    $command = $match.Groups[1].Value
    if (-not $command) { $command = $match.Groups[2].Value }
    if (-not [String]::Equals($command, [string]$expectedExecutable, [StringComparison]::OrdinalIgnoreCase) -or $match.Groups[3].Value -cne $Plan.arguments) {
        throw 'Command line does not match this exact viewer. Refusing to stop this process.'
    }
    if ((Get-M4WebCreationTime $Process) -cne [string]$State.creation_time_utc) {
        throw 'The process id has been reused. Refusing to stop this process.'
    }
}

function Get-M4WebIdentity {
    param($Process, [string]$Role)
    return [pscustomobject]@{ role = $Role; process_id = [int]$Process.ProcessId; parent_process_id = [int]$Process.ParentProcessId;
        creation_time_utc = (Get-M4WebCreationTime $Process) }
}

function Get-M4WebChildren {
    param([int]$ParentProcessId)
    return @(Get-CimInstance -ClassName Win32_Process -Filter ('ParentProcessId = ' + $ParentProcessId) -ErrorAction Stop)
}

function Get-M4WebRuntimeChildren {
    param([int]$ParentProcessId, $Plan)
    return @(Get-M4WebChildren $ParentProcessId | Where-Object {
        [String]::Equals([string]$_.ExecutablePath, [string]$Plan.runtime_executable, [StringComparison]::OrdinalIgnoreCase)
    })
}

function Get-M4WebLiveProcesses {
    param($State, $Plan)
    if ($null -eq $State) { return @() }
    $live = @()
    foreach ($identity in @($State.processes)) {
        if ($identity.role -notin @('launcher', 'runtime')) { throw 'Unknown recorded process role. No process has been stopped.' }
        if ($identity.role -eq 'launcher' -and [int]$identity.process_id -ne [int]$State.process_id) {
            throw 'Launcher identity does not match the recorded process id.'
        }
        if ($identity.role -eq 'runtime' -and [int]$identity.parent_process_id -ne [int]$State.process_id) {
            throw 'Runtime identity is not a direct child of the recorded launcher.'
        }
        $process = Get-M4WebCimProcess ([int]$identity.process_id)
        if ($null -ne $process) {
            Assert-M4OwnedWebProcess $process $identity $Plan
            $live += [pscustomobject]@{ process = $process; identity = $identity }
        }
    }
    return $live
}

function Test-M4WebPort {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect('127.0.0.1', 8501, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($attempt)
        return $true
    }
    catch { return $false }
    finally { $client.Close() }
}

function Get-M4WebHealth {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8501/_stcore/health' -UseBasicParsing -TimeoutSec 2
        return [ordered]@{ status = 'responding'; http_status = [int]$response.StatusCode; report_verified = $false }
    }
    catch { return [ordered]@{ status = 'not_ready'; http_status = $null; report_verified = $false } }
}

function Save-M4WebState {
    param($Plan, $State)
    $root = [IO.Path]::GetFullPath($Plan.working_directory).TrimEnd('\') + '\'
    $target = [IO.Path]::GetFullPath($Plan.state_file)
    if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Viewer state path escaped the project.' }
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
    $temporary = $target + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $target -Force
}

function Invoke-M4Web {
    $plan = Get-M4WebPlan $ProjectRoot
    if ($Action -eq 'preview') { $plan | ConvertTo-Json -Depth 8; return }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($plan.owner_marker)))).Replace('-', '') }
    finally { $sha.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\ashare-m4-web-' + $hash))
    $locked = $false
    try {
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { throw 'Another viewer start/stop action is in progress. Try status again shortly.' }
        $state = Read-M4WebState $plan
        $live = @(Get-M4WebLiveProcesses $state $plan)
        if ($Action -eq 'status') {
            [ordered]@{ operation = 'status'; status = $(if ($live.Count -gt 0) { 'running_owned_viewer' } else { 'no_owned_process' });
                url = $plan.url; process_id = $(if ($live.Count -gt 0) { $state.process_id } else { $null });
                owned_processes = @($live | ForEach-Object { $_.identity });
                state_file = $plan.state_file; read_only = $true; port_8501_in_use = (Test-M4WebPort);
                note = 'A service started outside this helper is not owned and will not be stopped.';
                health = $(if ($live.Count -gt 0) { Get-M4WebHealth } else { $null }) } | ConvertTo-Json -Depth 8
            return
        }
        if ($Action -eq 'stop') {
            if ($live.Count -eq 0) {
                [ordered]@{ operation = 'stop'; status = 'already_stopped'; state_file = $plan.state_file } | ConvertTo-Json
                return
            }
            # Pin an OS handle, then recheck CIM identity before terminating only
            # the process represented by that handle. No recursive process kill.
            # Windows venv python.exe is a launcher. Stop its exact verified
            # runtime child first, then the launcher if it has not already exited.
            $stopped = @()
            foreach ($entry in @($live | Sort-Object { if ($_.identity.role -eq 'runtime') { 0 } else { 1 } })) {
                $identity = $entry.identity
                $current = Get-M4WebCimProcess ([int]$identity.process_id)
                if ($null -eq $current) { continue }
                Assert-M4OwnedWebProcess $current $identity $plan
                $native = Get-Process -Id ([int]$identity.process_id) -ErrorAction Stop
                $null = $native.Handle
                Assert-M4OwnedWebProcess (Get-M4WebCimProcess ([int]$identity.process_id)) $identity $plan
                $native.Kill()
                $null = $native.WaitForExit(5000)
                $stopped += [int]$identity.process_id
            }
            $state | Add-Member -NotePropertyName stopped_at -NotePropertyValue ([DateTimeOffset]::Now.ToString('o')) -Force
            Save-M4WebState $plan $state
            [ordered]@{ operation = 'stop'; status = 'stopped'; process_id = $state.process_id; stopped_process_ids = $stopped;
                port_8501_in_use = (Test-M4WebPort); reports_preserved = $true; state_file = $plan.state_file } | ConvertTo-Json
            return
        }
        if ($live.Count -gt 0) {
            [ordered]@{ operation = 'start'; status = 'already_running'; process_id = $state.process_id;
                owned_processes = @($live | ForEach-Object { $_.identity }); url = $plan.url; health = (Get-M4WebHealth) } | ConvertTo-Json -Depth 8
            return
        }
        if (-not $plan.executable_exists -or -not $plan.app_exists) { throw 'Project .venv Python or streamlit_app.py does not exist.' }
        if (Test-M4WebPort) { throw 'Port 8501 is already in use by an unregistered service. It was not stopped or replaced. Close its own terminal/service first.' }
        [IO.Directory]::CreateDirectory($plan.log_directory) | Out-Null
        $version = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
        $stdout = Join-Path $plan.log_directory ($version + '-stdout.log')
        $stderr = Join-Path $plan.log_directory ($version + '-stderr.log')
        $started = Start-Process -FilePath $plan.executable -ArgumentList $plan.arguments -WorkingDirectory $plan.working_directory -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $cim = Get-M4WebCimProcess $started.Id
        $identities = @((Get-M4WebIdentity $cim 'launcher'))
        if (-not [String]::Equals($plan.executable, $plan.runtime_executable, [StringComparison]::OrdinalIgnoreCase)) {
            $children = @()
            for ($attempt = 0; $attempt -lt 10 -and $children.Count -eq 0; $attempt++) {
                # Hidden Windows console launchers can also own conhost.exe.
                # It is not a Python runtime and is never adopted or killed.
                $children = @(Get-M4WebRuntimeChildren $started.Id $plan)
                if ($children.Count -eq 0) { Start-Sleep -Milliseconds 200 }
            }
            if ($children.Count -ne 1) { throw 'Could not identify exactly one Windows venv runtime child. No unknown process was stopped.' }
            $runtimeIdentity = Get-M4WebIdentity $children[0] 'runtime'
            Assert-M4OwnedWebProcess $children[0] $runtimeIdentity $plan
            $identities += $runtimeIdentity
        }
        $newState = [ordered]@{ schema_version = $plan.schema_version; owner_marker = $plan.owner_marker; executable = $plan.executable;
            runtime_executable = $plan.runtime_executable; processes = $identities;
            arguments = $plan.arguments; working_directory = $plan.working_directory; process_id = $started.Id;
            creation_time_utc = (Get-M4WebCreationTime $cim); started_at = [DateTimeOffset]::Now.ToString('o');
            stdout_log = $stdout; stderr_log = $stderr; url = $plan.url; read_only = $true }
        Assert-M4OwnedWebProcess $cim ([pscustomobject]$newState) $plan
        if (Test-Path -LiteralPath $plan.state_file -PathType Leaf) {
            Copy-Item -LiteralPath $plan.state_file -Destination (Join-Path $plan.log_directory ($version + '-previous-state.json'))
        }
        Save-M4WebState $plan $newState
        $health = Get-M4WebHealth
        for ($attempt = 0; $attempt -lt 5 -and $health.status -ne 'responding'; $attempt++) {
            Start-Sleep -Milliseconds 500
            $health = Get-M4WebHealth
        }
        [ordered]@{ operation = 'start'; status = $(if ($health.status -eq 'responding') { 'started' } else { 'started_not_ready' });
            process_id = $started.Id; url = $plan.url; state_file = $plan.state_file; stdout_log = $stdout; stderr_log = $stderr;
            owned_processes = $identities;
            health = $health; read_only = $true; model_calls = 0 } | ConvertTo-Json -Depth 8
    }
    finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-M4Web }
