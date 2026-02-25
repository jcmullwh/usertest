[CmdletBinding(DefaultParameterSetName = 'write')]
param(
    [Parameter(ParameterSetName = 'write')]
    [string]$Out = 'repo_snapshot.zip',

    [Parameter(ParameterSetName = 'write')]
    [switch]$Overwrite,

    [Parameter(ParameterSetName = 'write')]
    [switch]$IncludeGitignoreFiles,

    [Parameter(ParameterSetName = 'write')]
    [switch]$TrackedOnly,

    [Parameter(ParameterSetName = 'write')]
    [switch]$IncludeIgnored,

    [Parameter(ParameterSetName = 'write')]
    [switch]$NoVerify,

    [Parameter(ParameterSetName = 'dry', Mandatory = $true)]
    [switch]$DryRun,

    [Parameter(ParameterSetName = 'list_included', Mandatory = $true)]
    [switch]$ListIncluded,

    [Parameter(ParameterSetName = 'list_excluded', Mandatory = $true)]
    [switch]$ListExcluded,

    [Parameter(ParameterSetName = 'list_excluded')]
    [int]$ListLimit = 0,

    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

. (Join-Path $PSScriptRoot 'python_preflight.ps1')

$toolArgs = @()
if ($RepoRoot) {
    $toolArgs += @('--repo-root', $RepoRoot)
}

if ($DryRun) {
    $toolArgs += @('--dry-run')
}
elseif ($ListIncluded) {
    $toolArgs += @('--list-included')
}
elseif ($ListExcluded) {
    $toolArgs += @('--list-excluded')
    if ($ListLimit -gt 0) {
        $toolArgs += @('--list-limit', "$ListLimit")
    }
}
else {
    $toolArgs += @('--out', $Out)
    if ($Overwrite) { $toolArgs += @('--overwrite') }
    if ($IncludeGitignoreFiles) { $toolArgs += @('--include-gitignore-files') }
    if ($TrackedOnly) { $toolArgs += @('--tracked-only') }
    if ($IncludeIgnored) { $toolArgs += @('--include-ignored') }
    if ($NoVerify) { $toolArgs += @('--no-verify') }
}

$exitCode = 0
Push-Location $repoRoot
try {
    $pythonInfo = Resolve-UsablePython -RepoRoot $repoRoot
    $pythonCmd = $pythonInfo.CommandPath
    Write-Host "==> Using Python: $($pythonInfo.Name) -> $pythonCmd"

    Write-Host '==> snapshot_repo'
    & $pythonCmd tools/snapshot_repo.py @toolArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
