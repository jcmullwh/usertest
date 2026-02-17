[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$UsePythonPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

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
    if (Get-Command pdm -ErrorAction SilentlyContinue) {
        Invoke-Step -Name 'Scaffold doctor' -Command {
            python tools/scaffold/scaffold.py doctor
        }
    }
    else {
        Write-Host "==> Scaffold doctor skipped (pdm not found on PATH)"
    }

    if (-not $SkipInstall) {
        Invoke-Step -Name 'Install base Python deps' -Command {
            python -m pip install -r requirements-dev.txt
        }

        if ($UsePythonPath) {
            Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
            . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
        }
        else {
            # --no-deps avoids duplicate direct-reference resolver conflicts between local packages.
            Invoke-Step -Name 'Install monorepo packages (editable, no deps)' -Command {
                python -m pip install --no-deps -e packages/normalized_events -e packages/agent_adapters -e packages/reporter -e packages/sandbox_runner -e packages/runner_core -e packages/triage_engine -e packages/backlog_core -e packages/backlog_miner -e packages/backlog_repo -e apps/usertest -e apps/usertest_backlog
            }
        }
    }
    elseif ($UsePythonPath) {
        Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
        . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
    }

    Invoke-Step -Name 'CLI help smoke' -Command {
        python -m usertest.cli --help
    }

    Invoke-Step -Name 'Backlog CLI help smoke' -Command {
        python -m usertest_backlog.cli --help
    }

    Invoke-Step -Name 'Pytest smoke suite' -Command {
        python -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py apps/usertest_backlog/tests/test_smoke.py
    }
}
finally {
    Pop-Location
}
