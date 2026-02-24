[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$UsePythonPath,
    [switch]$RequireDoctor
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

. (Join-Path $PSScriptRoot 'python_preflight.ps1')

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
    $pythonInfo = Resolve-UsablePython -RepoRoot $repoRoot
    $pythonCmd = $pythonInfo.CommandPath
    Write-Host "==> Using Python: $($pythonInfo.Name) -> $pythonCmd"
    Write-Host "==> Python executable: $($pythonInfo.Executable)"
    if ($pythonInfo.Version) {
        Write-Host "==> Python version: $($pythonInfo.Version)"
    }
    $pipFlags = @('--disable-pip-version-check', '--retries', '10', '--timeout', '30')

    if ($RequireDoctor) {
        if (-not (Get-Command pdm -ErrorAction SilentlyContinue)) {
            Write-Error "Scaffold doctor required but pdm was not found on PATH.`nInstall pdm (recommended): $pythonCmd -m pip install -U pdm`nOr rerun without -RequireDoctor."
            exit 1
        }
        Invoke-Step -Name 'Scaffold doctor' -Command {
            & $pythonCmd tools/scaffold/scaffold.py doctor
        }
    }
    else {
        Invoke-Step -Name 'Scaffold doctor (tool checks skipped; pdm optional)' -Command {
            Write-Host '    Note: pdm is optional; continuing with the pip-based flow.'
            Write-Host "    To enable tool checks: $pythonCmd -m pip install -U pdm"
            Write-Host '    To require doctor: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -RequireDoctor'
            & $pythonCmd tools/scaffold/scaffold.py doctor --skip-tool-checks
        }
    }

    if (-not $SkipInstall) {
        $pipProbeOk = $false
        & $pythonCmd -m pip --version
        if ($LASTEXITCODE -eq 0) {
            $pipProbeOk = $true
        }

        if (-not $pipProbeOk) {
            $venvPython = Join-Path (Join-Path $repoRoot '.venv') 'Scripts\python.exe'
            if (-not (Test-Path -LiteralPath $venvPython)) {
                Invoke-Step -Name 'Create .venv (pip bootstrap)' -Command {
                    & $pythonCmd -m venv .venv
                }
            }
            if (Test-Path -LiteralPath $venvPython) {
                $pythonCmd = $venvPython
                Write-Host "==> Using Python: venv -> $pythonCmd"
            }

            Invoke-Step -Name 'Bootstrap pip (ensurepip)' -Command {
                & $pythonCmd -m ensurepip --upgrade
            }

            & $pythonCmd -m pip --version
            if ($LASTEXITCODE -ne 0) {
                Write-Error "pip is required for smoke installs, but is not available after ensurepip.`nTry installing a full CPython (with ensurepip), then re-run smoke."
                exit $LASTEXITCODE
            }
        }

        Invoke-Step -Name 'Install base Python deps' -Command {
            & $pythonCmd -m pip install @pipFlags -r requirements-dev.txt
        }

        if ($UsePythonPath) {
            Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
            . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
        }
        else {
            # --no-deps avoids duplicate direct-reference resolver conflicts between local packages.
            Invoke-Step -Name 'Install monorepo packages (editable, no deps)' -Command {
                & $pythonCmd -m pip install --no-deps -e packages/normalized_events -e packages/agent_adapters -e packages/run_artifacts -e packages/reporter -e packages/sandbox_runner -e packages/runner_core -e packages/triage_engine -e packages/backlog_core -e packages/backlog_miner -e packages/backlog_repo -e apps/usertest -e apps/usertest_backlog
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

    Invoke-Step -Name 'Pytest smoke suite' -Command {
        & $pythonCmd -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py apps/usertest_backlog/tests/test_smoke.py
    }

    Write-Host '==> Smoke complete: all checks passed.'
}
finally {
    Pop-Location
}
