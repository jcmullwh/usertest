[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$FixtureName = 'minimal_codex_run'
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
        'offline-first-success'
        '--repo-root'
        $repoRoot
        '--python'
        $pythonCmd
        '--python-source'
        $pythonInfo.Name
        '--shell'
        'powershell'
        '--fixture-name'
        $FixtureName
    )
    if ($pythonInfo.Executable) {
        $launcherArgs += @('--python-executable', $pythonInfo.Executable)
    }
    if ($pythonInfo.Version) {
        $launcherArgs += @('--python-version', $pythonInfo.Version)
    }

    & $pythonCmd @launcherArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
