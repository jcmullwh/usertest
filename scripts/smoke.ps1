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

        try {
            $probe = & $resolved -c "import encodings,sys; print(sys.executable)"
            if ($LASTEXITCODE -ne 0) {
                $rejections += "[$name] interpreter probe exited with code $LASTEXITCODE"
                continue
            }
            $executable = ($probe | Select-Object -Last 1).Trim()
            if (-not $executable) {
                $rejections += "[$name] interpreter probe returned no executable path"
                continue
            }
            return @{
                Name = $name
                CommandPath = $resolved
                Executable = $executable
            }
        }
        catch {
            $rejections += "[$name] interpreter probe failed: $($_.Exception.Message)"
        }
    }

    Write-Host '==> Python interpreter probe failed'
    foreach ($line in $rejections) {
        Write-Host "    $line"
    }
    throw "No usable Python interpreter found. Install a full CPython runtime and ensure python.exe is on PATH."
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
    $pythonCmd = $pythonInfo.CommandPath
    Write-Host "==> Using Python: $($pythonInfo.Name) -> $pythonCmd"
    Write-Host "==> Python executable: $($pythonInfo.Executable)"

    if (Get-Command pdm -ErrorAction SilentlyContinue) {
        Invoke-Step -Name 'Scaffold doctor' -Command {
            & $pythonCmd tools/scaffold/scaffold.py doctor
        }
    }
    else {
        if ($RequireDoctor) {
            Write-Error 'Scaffold doctor required but pdm was not found on PATH. Install pdm or rerun without -RequireDoctor.'
            exit 1
        }
        Write-Host "==> Scaffold doctor skipped (pdm not found on PATH; preflight coverage reduced)"
    }

    if (-not $SkipInstall) {
        Invoke-Step -Name 'Install base Python deps' -Command {
            & $pythonCmd -m pip install -r requirements-dev.txt
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
