[CmdletBinding()]
param(
    [switch]$SkipToolChecks
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

. (Join-Path $PSScriptRoot 'python_preflight.ps1')

$exitCode = 0
Push-Location $repoRoot
try {
    $pythonInfo = Resolve-UsablePython -RepoRoot $repoRoot
    $pythonCmd = $pythonInfo.CommandPath
    Write-Host "==> Using Python: $($pythonInfo.Name) -> $pythonCmd"
    if ($pythonInfo.Executable) {
        Write-Host "==> Python executable: $($pythonInfo.Executable)"
    }
    if ($pythonInfo.Version) {
        Write-Host "==> Python version: $($pythonInfo.Version)"
    }

    if ($SkipToolChecks) {
        Write-Host '==> Scaffold doctor (tool checks skipped)'
        & $pythonCmd tools/scaffold/scaffold.py doctor --skip-tool-checks
    }
    else {
        Write-Host '==> Scaffold doctor'
        & $pythonCmd tools/scaffold/scaffold.py doctor
    }
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode

