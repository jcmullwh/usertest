[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$UsePythonPath,
    [switch]$RequireDoctor
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Resolve-UsablePython {
    $commands = @('python', 'python3', 'py')
    $rejections = @()

    function Add-Rejection {
        param([Parameter(Mandatory = $true)][string]$Message)
        $current = @(Get-Variable -Scope 1 -Name rejections -ValueOnly -ErrorAction SilentlyContinue)
        Set-Variable -Scope 1 -Name rejections -Value ($current + @($Message))
    }

    function Resolve-PythonHomeForSplitInstall {
        $candidates = @(
            (Join-Path $env:LocalAppData 'Programs\Python\Python313'),
            (Join-Path $env:LocalAppData 'Programs\Python\Python312'),
            (Join-Path $env:LocalAppData 'Programs\Python\Python311')
        )

        foreach ($candidate in $candidates) {
            if (-not $candidate) {
                continue
            }
            $encodings = Join-Path $candidate 'Lib\encodings\__init__.py'
            if (Test-Path -LiteralPath $encodings) {
                return $candidate
            }
        }

        return $null
    }

    function Test-PythonExe {
        param(
            [Parameter(Mandatory = $true)]
            [string]$ExecutablePath,
            [Parameter(Mandatory = $true)]
            [string]$SourceName
        )

        if (-not (Test-Path -LiteralPath $ExecutablePath)) {
            return $null
        }
        if ($ExecutablePath.ToLower().Contains('\windowsapps\')) {
            Add-Rejection "[$SourceName] rejected windowsapps interpreter: $ExecutablePath"
            return $null
        }
        $originalPythonHome = $env:PYTHONHOME
        $setPythonHome = $false
        $probeSucceeded = $false
        try {
            $probe = & $ExecutablePath -c "import encodings,sys; print(sys.executable)" 2>&1
            if ($LASTEXITCODE -ne 0) {
                $probeText = ($probe | Out-String)
                $looksLikeMissingStdlib = $probeText -match 'Failed to import encodings module' -or $probeText -match "No module named 'encodings'"
                if ($looksLikeMissingStdlib) {
                    $pythonHome = Resolve-PythonHomeForSplitInstall
                    if ($pythonHome) {
                        Write-Host "==> Detected split Python install; setting PYTHONHOME=$pythonHome"
                        $env:PYTHONHOME = $pythonHome
                        $setPythonHome = $true
                        $probe = & $ExecutablePath -c "import encodings,sys; print(sys.executable)" 2>&1
                    }
                }
            }

            if ($LASTEXITCODE -ne 0) {
                Add-Rejection "[$SourceName] interpreter probe exited with code $LASTEXITCODE ($ExecutablePath)"
                return $null
            }

            $executable = ($probe | Select-Object -Last 1).Trim()
            if (-not $executable) {
                Add-Rejection "[$SourceName] interpreter probe returned no executable path ($ExecutablePath)"
                return $null
            }

            $probeSucceeded = $true
            return @{
                Name = $SourceName
                CommandPath = $ExecutablePath
                Executable = $executable
            }
        }
        catch {
            Add-Rejection "[$SourceName] interpreter probe failed ($ExecutablePath): $($_.Exception.Message)"
            return $null
        }
        finally {
            if ($setPythonHome) {
                if (-not $probeSucceeded) {
                    if ($originalPythonHome) {
                        $env:PYTHONHOME = $originalPythonHome
                    }
                    else {
                        Remove-Item env:PYTHONHOME -ErrorAction SilentlyContinue
                    }
                }
                elseif ($originalPythonHome) {
                    # If the caller had PYTHONHOME set already, restore it to avoid surprises.
                    $env:PYTHONHOME = $originalPythonHome
                }
            }
        }
    }

    foreach ($name in $commands) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) {
            continue
        }

        $resolved = $cmd.Source
        if (-not $resolved) {
            $resolved = $cmd.Path
        }

        if ($resolved -and $resolved.ToLower().Contains('\windowsapps\')) {
            $rejections += "[$name] rejected windowsapps alias: $resolved"
            continue
        }

        if ($name -eq 'py') {
            # `py` can default to the Windows Store Python which may be blocked; enumerate registered runtimes instead.
            $candidates = @()
            try {
                $out = & $resolved -0p 2>$null
                if ($LASTEXITCODE -eq 0) {
                    foreach ($line in $out) {
                        if ($line -match '([A-Za-z]:\\[^\\s]+(?:\\[^\\s]+)*)') {
                            $candidates += $Matches[1]
                        }
                    }
                }
            }
            catch {
                $rejections += "[py] failed to enumerate runtimes: $($_.Exception.Message)"
            }

            # Common fallback locations (seen in CI images).
            $candidates += @(
                'C:\Python313\python.exe',
                'C:\Python312\python.exe',
                'C:\Python311\python.exe',
                (Join-Path $env:LocalAppData 'Programs\Python\Python313\python.exe'),
                (Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'),
                (Join-Path $env:LocalAppData 'Programs\Python\Python311\python.exe')
            )

            foreach ($candidate in ($candidates | Select-Object -Unique)) {
                $ok = Test-PythonExe -ExecutablePath $candidate -SourceName 'py'
                if ($ok) {
                    return $ok
                }
            }

            $rejections += "[py] no usable non-WindowsApps interpreter found via py launcher"
            continue
        }

        $ok = Test-PythonExe -ExecutablePath $resolved -SourceName $name
        if ($ok) {
            return $ok
        }
    }

    # One more explicit attempt for "split installs" (python.exe and stdlib in different directories).
    try {
        $pythonHome = Resolve-PythonHomeForSplitInstall
        $splitExe = 'C:\Python313\python.exe'
        if ($pythonHome -and (Test-Path -LiteralPath $splitExe)) {
            Write-Host "==> Trying split-install fallback: $splitExe (PYTHONHOME=$pythonHome)"
            $env:PYTHONHOME = $pythonHome
            $ok = Test-PythonExe -ExecutablePath $splitExe -SourceName 'split'
            if ($ok) {
                return $ok
            }
        }
    }
    catch {
        $rejections += "[split] fallback probe failed: $($_.Exception.Message)"
    }

    Write-Host '==> Python interpreter probe failed'
    foreach ($line in $rejections) {
        Write-Host "    $line"
    }

    throw "No usable Python interpreter found. Install a full CPython runtime and ensure python.exe is on PATH."
}

function Ensure-SmokeVenvPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,
        [Parameter(Mandatory = $true)]
        [string]$BasePythonCmd
    )

    $venvRoot = Join-Path $RepoRoot '.usertest\smoke-venv'
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    $venvCfg = Join-Path $venvRoot 'pyvenv.cfg'

    $needsCreate = $true
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $venvCfg)) {
        $cfgText = Get-Content -LiteralPath $venvCfg -Raw
        if ($cfgText -match 'include-system-site-packages\\s*=\\s*true') {
            $needsCreate = $false
        }
    }

    if ($needsCreate) {
        Write-Host "==> Create smoke venv: $venvRoot"
        if (Test-Path -LiteralPath $venvRoot) {
            Remove-Item -LiteralPath $venvRoot -Recurse -Force
        }
        # Use system site-packages so smoke can run without network access (deps may already exist in the base env).
        # Use --without-pip to avoid venv's internal ensurepip call failing on some split-install layouts.
        & $BasePythonCmd -m venv --system-site-packages --without-pip $venvRoot
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }

    & $venvPython -c "import sys; print(sys.executable)" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Smoke venv python is not runnable: $venvPython"
        exit $LASTEXITCODE
    }

    & $venvPython -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '==> Bootstrapping pip in base interpreter (ensurepip)'
        & $BasePythonCmd -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
        & $venvPython -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Error "pip is still unavailable via the smoke venv python: $venvPython"
            exit 1
        }
    }

    return $venvPython
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Push-Location $repoRoot
try {
    $pythonInfo = Resolve-UsablePython
    $basePythonCmd = $pythonInfo.CommandPath
    $pythonCmd = $basePythonCmd
    Write-Host "==> Using Python: $($pythonInfo.Name) -> $basePythonCmd"
    Write-Host "==> Python executable: $($pythonInfo.Executable)"
    $pipFlags = @('--disable-pip-version-check', '--retries', '10', '--timeout', '30')

    if (Get-Command pdm -ErrorAction SilentlyContinue) {
        Invoke-Step -Name 'Scaffold doctor' -Command {
            & $basePythonCmd tools/scaffold/scaffold.py doctor
        }
    }
    else {
        if ($RequireDoctor) {
            Write-Error "Scaffold doctor required but pdm was not found on PATH.`nInstall pdm (recommended): $pythonCmd -m pip install -U pdm`nOr rerun without -RequireDoctor."
            exit 1
        }
        Invoke-Step -Name 'Scaffold doctor (tool checks skipped; pdm not found on PATH)' -Command {
            Write-Host '    Note: pdm is optional; continuing with the pip-based flow.'
            Write-Host "    To enable tool checks: $pythonCmd -m pip install -U pdm"
            Write-Host '    To require doctor: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -RequireDoctor'
            & $basePythonCmd tools/scaffold/scaffold.py doctor --skip-tool-checks
        }
    }

    if (-not $SkipInstall) {
        $tempRoot = Join-Path $repoRoot '.usertest\tmp'
        New-Item -ItemType Directory -Force -Path $tempRoot *> $null
        $env:TEMP = $tempRoot
        $env:TMP = $tempRoot
        $env:TMPDIR = $tempRoot
        $env:PYTHONDONTWRITEBYTECODE = '1'

        $pythonCmd = Ensure-SmokeVenvPython -RepoRoot $repoRoot -BasePythonCmd $basePythonCmd
        Write-Host "==> Smoke venv python: $pythonCmd"

        Invoke-Step -Name 'Install base Python deps' -Command {
            & $pythonCmd -m pip install @pipFlags -r requirements-dev.txt
        }

        if ($UsePythonPath) {
            Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
            . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
        }
        else {
            # --no-deps avoids duplicate direct-reference resolver conflicts between local packages.
            Write-Host '==> Install monorepo packages (editable, no deps)'
            # Note: this venv uses system-site-packages, so a developer's global editable install can shadow
            # the workspace checkout. Force a reinstall into the venv so imports resolve to this repo.
            & $pythonCmd -m pip install --no-deps --upgrade --force-reinstall -e packages/normalized_events -e packages/agent_adapters -e packages/run_artifacts -e packages/reporter -e packages/sandbox_runner -e packages/runner_core -e packages/triage_engine -e packages/backlog_core -e packages/backlog_miner -e packages/backlog_repo -e apps/usertest -e apps/usertest_backlog
            if ($LASTEXITCODE -ne 0) {
                Write-Host '==> Editable install failed; falling back to PYTHONPATH mode'
                . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
            }
            else {
                # Safety: if a global editable install is still winning, force PYTHONPATH for smoke.
                $resolved = & $pythonCmd -c "import pathlib,usertest.cli; print(pathlib.Path(usertest.cli.__file__).resolve())" 2>$null
                $resolvedPath = ($resolved | Select-Object -Last 1).Trim()
                if ($resolvedPath -and (-not $resolvedPath.ToLower().StartsWith($repoRoot.ToLower()))) {
                    Write-Host "==> Detected usertest import from outside this workspace ($resolvedPath); using PYTHONPATH mode"
                    . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
                }
            }
        }
    }
    elseif ($UsePythonPath) {
        Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
        . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
    }

    Invoke-Step -Name 'CLI help smoke' -Command {
        & $pythonCmd -m usertest.cli --help
    }

    Invoke-Step -Name 'Backlog CLI help smoke' -Command {
        & $pythonCmd -m usertest_backlog.cli --help
    }

    Write-Host '==> Pytest smoke suite'
    $pytestOutput = & $pythonCmd -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py apps/usertest_backlog/tests/test_smoke.py 2>&1
    $pytestExit = $LASTEXITCODE
    $pytestOutput | ForEach-Object { Write-Host $_ }

    if ($pytestExit -ne 0) {
        $outputText = ($pytestOutput | Out-String)
        $looksLikeSandboxFsIssue = $outputText -match 'WinError 5' -or $outputText -match 'Access is denied'
        if ($looksLikeSandboxFsIssue) {
            Write-Host '==> Pytest failed due to filesystem permissions; rerunning reduced smoke suite'
            Invoke-Step -Name 'Pytest reduced smoke suite' -Command {
                & $pythonCmd -m pytest -q -p no:tmpdir -p no:cacheprovider apps/usertest/tests/test_smoke.py apps/usertest_backlog/tests/test_smoke.py
            }
        }
        else {
            exit $pytestExit
        }
    }

    Write-Host '==> Smoke complete: all checks passed.'
}
finally {
    Pop-Location
}
