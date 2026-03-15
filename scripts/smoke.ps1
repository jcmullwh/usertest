[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$UsePythonPath,
    [switch]$RequireDoctor
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

. (Join-Path $PSScriptRoot 'python_preflight.ps1')

Push-Location $repoRoot
try {
    try {
        $pythonInfo = Resolve-UsablePython -RepoRoot $repoRoot
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }

    $pythonCmd = $pythonInfo.CommandPath
    $env:USERTEST_PYTHON = $pythonCmd
    $launcherArgs = @(
        'tools/first_run_launcher.py'
        'smoke'
        '--repo-root'
        $repoRoot
        '--python'
        $pythonCmd
        '--python-source'
        $pythonInfo.Name
        '--shell'
        'powershell'
    )
    if ($pythonInfo.Executable) {
        $launcherArgs += @('--python-executable', $pythonInfo.Executable)
    }
    if ($pythonInfo.Version) {
        $launcherArgs += @('--python-version', $pythonInfo.Version)
    }
    if ($SkipInstall) {
        $launcherArgs += '--skip-install'
    }
    if ($UsePythonPath) {
        $launcherArgs += '--use-pythonpath'
    }
    if ($RequireDoctor) {
        $launcherArgs += '--require-doctor'
    }

    & $pythonCmd @launcherArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
