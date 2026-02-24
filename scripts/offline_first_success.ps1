# One-command "from source" verification for offline-safe workflows.
#
# What it does:
# - Creates/uses a local `.venv`
# - Installs minimal deps from `requirements-dev.txt`
# - Sets `PYTHONPATH` for monorepo source execution
# - Copies a golden fixture run dir to a temp location
# - Re-renders `report.md` + recomputes metrics
#
# Usage (PowerShell, from repo root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$FixtureName = "minimal_codex_run"
)

$ErrorActionPreference = 'Stop'

Write-Host "NOTE: This script does NOT execute any agents. It copies a golden fixture run and rerenders artifacts." -ForegroundColor Yellow
Write-Host "      It is a smoke check for offline-safe/report rendering workflows." -ForegroundColor Yellow
Write-Host ""
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
    $venvDir = Join-Path $repoRoot '.venv'
    # Windows-specific: venv uses Scripts\python.exe
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'

    if (Test-Path -LiteralPath $venvPython) {
        $venvProbe = Test-PythonInterpreter -CommandPath $venvPython -TimeoutSeconds 2.0
        if (-not $venvProbe.Usable) {
            Write-Host "==> Existing .venv looks unhealthy ($($venvProbe.ReasonCode)); recreating it." -ForegroundColor Yellow
            try { Remove-Item -Recurse -Force -LiteralPath $venvDir } catch { }
        }
    }

    $pythonInfo = Resolve-UsablePython -RepoRoot $repoRoot
    $pythonCmd = $pythonInfo.CommandPath
    Write-Host "==> Using Python: $($pythonInfo.Name) -> $pythonCmd"
    Write-Host "==> Python executable: $($pythonInfo.Executable)"
    if ($pythonInfo.Version) {
        Write-Host "==> Python version: $($pythonInfo.Version)"
    }
    $pipFlags = @('--disable-pip-version-check', '--retries', '10', '--timeout', '30')

    if (-not (Test-Path $venvPython)) {
        Invoke-Step -Name 'Create venv (.venv)' -Command {
            & $pythonCmd -m venv $venvDir
        }
    }
    if (-not (Test-Path $venvPython)) {
        throw "Failed to create venv at $venvDir"
    }

    Invoke-Step -Name 'Install minimal deps (requirements-dev.txt)' -Command {
        & $venvPython -m pip install @pipFlags -r requirements-dev.txt
    }

    Write-Host '==> Configure PYTHONPATH via scripts/set_pythonpath.ps1'
    . (Join-Path $PSScriptRoot 'set_pythonpath.ps1') -RepoRoot $repoRoot

    if (-not (Test-Path (Join-Path $repoRoot 'examples/golden_runs'))) {
        throw "Missing examples/golden_runs in repo root: $repoRoot"
    }

    $runDir = & $venvPython -c @'
import pathlib
import shutil
import sys
import tempfile

fixture_name = sys.argv[1] if len(sys.argv) > 1 else 'minimal_codex_run'
src = pathlib.Path('examples/golden_runs') / fixture_name
if not src.exists():
    raise SystemExit(f'Missing fixture dir: {src}')
dst_root = pathlib.Path(tempfile.mkdtemp(prefix='usertest_fixture_'))
dst = dst_root / fixture_name
shutil.copytree(src, dst)
print(dst)
'@ $FixtureName
    $runDir = ($runDir | Select-Object -Last 1).Trim()
    if (-not $runDir) {
        throw 'Failed to create temp fixture copy.'
    }

    Invoke-Step -Name 'Re-render report from fixture copy' -Command {
        & $venvPython -m usertest.cli report --repo-root $repoRoot --run-dir $runDir --recompute-metrics
    }

    Write-Host "==> Success. Scratch run dir: $runDir"
}
finally {
    Pop-Location
}
