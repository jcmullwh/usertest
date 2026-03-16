[CmdletBinding()]
param(
    [switch]$SkipToolChecks,
    [switch]$RequirePip
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

. (Join-Path $PSScriptRoot 'python_preflight.ps1')

$exitCode = 0
Push-Location $repoRoot
try {
    try {
        $pythonInfo = Resolve-UsablePython -RepoRoot $repoRoot
    }
    catch {
        Write-Host $_.Exception.Message
        exit 1
    }
    $pythonCmd = $pythonInfo.CommandPath
    $env:USERTEST_PYTHON = $pythonCmd
    Write-Host "==> Using Python: $($pythonInfo.Name) -> $pythonCmd"
    if ($pythonInfo.Executable) {
        Write-Host "==> Python executable: $($pythonInfo.Executable)"
    }
    if ($pythonInfo.Version) {
        Write-Host "==> Python version: $($pythonInfo.Version)"
    }
    $toolchainArgs = @(
        'resolve',
        '--repo-root', $repoRoot,
        '--python-exe', $pythonCmd,
        '--workflow', 'doctor',
        '--emit', 'powershell'
    )
    if (-not $SkipToolChecks) {
        $toolchainArgs += '--require-pdm'
    }
    if ($RequirePip) {
        $toolchainArgs += @('--require-pip', '--bootstrap-pip')
    }
    $toolchainEnv = & $pythonCmd tools/python_toolchain.py @toolchainArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    Invoke-Expression ($toolchainEnv -join [Environment]::NewLine)
    $pythonCmd = $env:USERTEST_TOOLCHAIN_PYTHON_EXE

    if ($SkipToolChecks) {
        Write-Host '==> Scaffold doctor (tool checks skipped)'
        $doctorArgs = @('doctor', '--skip-tool-checks')
    }
    else {
        Write-Host '==> Scaffold doctor'
        $doctorArgs = @('doctor')
    }
    if ($RequirePip) {
        $doctorArgs += '--require-pip'
    }
    & $pythonCmd tools/scaffold/scaffold.py @doctorArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
