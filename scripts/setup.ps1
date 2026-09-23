# Run from PowerShell. All installed project dependencies stay in .venv.
param([string]$PythonExe = "")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $managedPython = Join-Path $projectRoot '.tools\python\cpython-3.12.14-windows-x86_64-none\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (Test-Path -LiteralPath (Join-Path $projectRoot '.venv')) {
            throw 'Existing .venv is incomplete. Preserve or rename it, then rerun setup.'
        }
        if ($PythonExe) {
            $selectedPython = $PythonExe
        } elseif (Test-Path -LiteralPath $managedPython) {
            $selectedPython = $managedPython
        } else {
            $selectedPython = $null
            if (Get-Command py -ErrorAction SilentlyContinue) {
                try {
                    $foundPython = & py -3.12 -c 'import sys; print(sys.executable)' 2>$null
                    if ($LASTEXITCODE -eq 0) { $selectedPython = $foundPython }
                } catch {
                    # Windows PowerShell 5.1 can promote native stderr to an error.
                    $selectedPython = $null
                }
            }
            if (-not $selectedPython) {
                # Bootstrap via the installed system Python; no global pip installation.
                if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
                    throw 'Install Python 3.12 first, or rerun with -PythonExe C:\path\python.exe.'
                }
                $uvExe = Join-Path $projectRoot '.tools\bootstrap\bin\uv.exe'
                if (-not (Test-Path -LiteralPath $uvExe)) {
                    & python -m pip --isolated install --disable-pip-version-check --retries 1 --target .tools/bootstrap 'uv==0.12.11'
                    if ($LASTEXITCODE -ne 0) { throw 'uv download failed. Check access to PyPI.' }
                }
                & $uvExe python install 3.12.14 --install-dir .tools/python --no-bin --no-registry --cache-dir .tools/uv-cache
                if ($LASTEXITCODE -ne 0) { throw 'Python download failed. Check access to GitHub.' }
                $selectedPython = $managedPython
            }
        }
        & $selectedPython -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'
        if ($LASTEXITCODE -ne 0) { throw 'The selected Python must be version 3.12.' }
        & $selectedPython -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
    }
    & $venvPython -c 'import sys; assert sys.version_info[:2] == (3, 12), "Existing .venv must use Python 3.12"'
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv uses another Python. It has not been replaced.' }
    & $venvPython -m pip --isolated install --disable-pip-version-check --retries 1 --require-hashes -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $venvPython -m pip --isolated install --disable-pip-version-check --no-index --no-deps --no-build-isolation -e .
    if ($LASTEXITCODE -ne 0) { throw 'Local project installation failed.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
    Write-Host 'M0/M1/M2 setup complete. Run: .\.venv\Scripts\python.exe -m ashare_daily doctor --offline'
} finally {
    Pop-Location
}
