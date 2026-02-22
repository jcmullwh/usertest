# Sets PYTHONPATH for running this monorepo from source.
#
# Usage (PowerShell, from repo root):
#   . .\scripts\set_pythonpath.ps1
#
# Notes:
# - Dot-source the script so it updates PYTHONPATH in your current shell.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot
)

$RepoRoot = if ($RepoRoot) { $RepoRoot } else { (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$paths = @(
    (Join-Path $RepoRoot "apps/usertest/src"),
    (Join-Path $RepoRoot "apps/usertest_backlog/src"),
    (Join-Path $RepoRoot "apps/usertest_implement/src"),
    (Join-Path $RepoRoot "packages/runner_core/src"),
    (Join-Path $RepoRoot "packages/agent_adapters/src"),
    (Join-Path $RepoRoot "packages/normalized_events/src"),
    (Join-Path $RepoRoot "packages/reporter/src"),
    (Join-Path $RepoRoot "packages/sandbox_runner/src"),
    (Join-Path $RepoRoot "packages/triage_engine/src"),
    (Join-Path $RepoRoot "packages/backlog_core/src"),
    (Join-Path $RepoRoot "packages/backlog_miner/src"),
    (Join-Path $RepoRoot "packages/backlog_repo/src"),
    (Join-Path $RepoRoot "packages/run_artifacts/src")
)

$env:PYTHONPATH = ($paths -join ";")
Write-Host "PYTHONPATH set."
Write-Host $env:PYTHONPATH
