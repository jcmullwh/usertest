[CmdletBinding()]
param(
    [switch]$SkipToolChecks,
    [switch]$AllowMissingPip
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
        $doctorArgs = @('doctor', '--skip-tool-checks')
    }
    else {
        Write-Host '==> Scaffold doctor'
        $doctorArgs = @('doctor')
    }
    if ($AllowMissingPip) {
        $doctorArgs += '--allow-missing-pip'
    }
    & $pythonCmd tools/scaffold/scaffold.py @doctorArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
