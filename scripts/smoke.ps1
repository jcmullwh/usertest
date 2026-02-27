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

function Write-Err {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Message
    )
    [Console]::Error.WriteLine($Message)
}

function Write-SetupHint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonCmd
    )

    Write-Host '==> Setup hint'
    Write-Host '    Choose a setup mode:'
    Write-Host '      - Default (recommended): installs deps + editable installs'
    Write-Host '          powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1'
    Write-Host '      - From-source: installs deps + sets PYTHONPATH (no editables)'
    Write-Host '          powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -UsePythonPath'
    Write-Host '      - No-install: assumes deps + local packages are already importable'
    Write-Host '          powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipInstall  # (often combined with -UsePythonPath)'

    if (-not $env:VIRTUAL_ENV -and -not $env:CI) {
        Write-Host '    Recommended venv:'
        Write-Host "      $PythonCmd -m venv .venv"
        Write-Host '      . .\.venv\Scripts\Activate.ps1'
    }
}

function Invoke-SmokeImportPreflight {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonCmd
    )

    $preflightCode = @'
import importlib

mods = [
    "usertest",
    "usertest.cli",
    "usertest_backlog",
    "usertest_backlog.cli",
    "usertest_implement",
    "usertest_implement.cli",
    "agent_adapters",
    "backlog_core",
    "backlog_miner",
    "backlog_repo",
    "normalized_events",
    "reporter",
    "run_artifacts",
    "runner_core",
    "sandbox_runner",
    "triage_engine",
]

errors = []
for mod in mods:
    try:
        importlib.import_module(mod)
    except Exception as e:
        errors.append((mod, f"{type(e).__name__}: {e}"))

if errors:
    for mod, msg in errors:
        print(f"{mod}: {msg}")
    raise SystemExit(1)
'@

    $preflightOutput = & $PythonCmd -c $preflightCode 2>&1
    $preflightRc = $LASTEXITCODE
    if ($preflightRc -ne 0) {
        Write-Err '==> Smoke preflight failed: required imports are not available in this Python environment.'
        if ($preflightOutput) {
            foreach ($line in $preflightOutput) {
                if ($line) { Write-Err "    - $line" }
            }
        }
        Write-Err ''
        Write-Err '    You passed -SkipInstall, so this script will not run any installs.'
        Write-Err '    That means it will NOT install requirements-dev.txt and it will NOT install local monorepo packages.'
        Write-Err ''
        Write-Err '    Choose one setup mode:'
        Write-Err '      - Default (recommended for dev):'
        Write-Err '          powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1'
        Write-Err '      - From-source (no editables, but installs deps):'
        Write-Err '          powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -UsePythonPath'
        Write-Err '      - No-install (deps already provisioned):'
        Write-Err '          powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipInstall -UsePythonPath'
        Write-Err ''
        Write-Err '    Tip: prefer a virtualenv to avoid global/user-site installs:'
        Write-Err "      $PythonCmd -m venv .venv ; . .\\.venv\\Scripts\\Activate.ps1"
        exit 1
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

    Write-SetupHint -PythonCmd $pythonCmd

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
                & $pythonCmd -m pip install --no-deps -e packages/normalized_events -e packages/agent_adapters -e packages/run_artifacts -e packages/reporter -e packages/sandbox_runner -e packages/runner_core -e packages/triage_engine -e packages/backlog_core -e packages/backlog_miner -e packages/backlog_repo -e apps/usertest -e apps/usertest_backlog -e apps/usertest_implement
            }
        }
    }
    elseif ($UsePythonPath) {
        Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
        . (Join-Path $PSScriptRoot 'set_pythonpath.ps1')
    }

    Write-Host '==> Import-origin guard smoke'
    & $pythonCmd tools/smoke_import_guard.py --repo-root $repoRoot
    $guardRc = $LASTEXITCODE
    if ($guardRc -ne 0) {
        if (-not $UsePythonPath) {
            Write-Host "==> WARNING: 'usertest' did not import from this workspace; switching to PYTHONPATH mode."
            Write-Host '    (This commonly happens when another checkout is installed editable in the same interpreter.)'
            Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
            $UsePythonPath = $true
            . (Join-Path $PSScriptRoot 'set_pythonpath.ps1') -RepoRoot $repoRoot
            & $pythonCmd tools/smoke_import_guard.py --repo-root $repoRoot
            if ($LASTEXITCODE -ne 0) {
                exit $LASTEXITCODE
            }
        }
        else {
            exit $guardRc
        }
    }

    if ($SkipInstall) {
        Invoke-SmokeImportPreflight -PythonCmd $pythonCmd
    }

    Invoke-Step -Name 'CLI help smoke' -Command {
        & $pythonCmd -m usertest.cli --help
    }

    Invoke-Step -Name 'Backlog CLI help smoke' -Command {
        & $pythonCmd -m usertest_backlog.cli --help
    }

    Invoke-Step -Name 'Implement CLI help smoke' -Command {
        & $pythonCmd -m usertest_implement.cli --help
    }

    Invoke-Step -Name 'Pytest smoke suite' -Command {
        & $pythonCmd -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py apps/usertest_backlog/tests/test_smoke.py apps/usertest_implement/tests/test_smoke.py
    }

    Write-Host '==> Smoke complete: all checks passed.'
}
finally {
    Pop-Location
}
