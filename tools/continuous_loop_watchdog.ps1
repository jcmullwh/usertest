$ErrorActionPreference = "Stop"

function Write-WatchdogLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $timestamp = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -LiteralPath $Path -Value "$timestamp $Message"
}

function Start-ContinuousLoop {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,
        [Parameter(Mandatory = $true)]
        [string]$LogPath
    )

    $stdout = Join-Path $RepoRoot "runs\_continuous_loop\launcher.stdout.txt"
    $stderr = Join-Path $RepoRoot "runs\_continuous_loop\launcher.stderr.txt"
    $python = Join-Path $RepoRoot "apps\usertest_implement\.venv\Scripts\python.exe"
    $script = Join-Path $RepoRoot "tools\continuous_implement_loop.py"
    $args = @(
        $script,
        "--repo-root", $RepoRoot,
        "--owner-root", $RepoRoot,
        "--runs-dir", "runs\usertest_implement",
        "--target", "usertest",
        "--repo-input", $RepoRoot,
        "--backlog-agent", "codex",
        "--backlog-model", "gpt-5.4",
        "--implementation-agent", "codex",
        "--review-agent", "claude",
        "--sleep-seconds", "60"
    )
    $proc = Start-Process -FilePath $python -ArgumentList $args -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-WatchdogLog -Path $LogPath -Message "started loop pid=$($proc.Id)"
}

function Get-LoopRelatedProcesses {
    $patterns = @(
        "*continuous_implement_loop.py*",
        "*usertest_backlog.cli reports backlog*",
        "*@openai\\codex*"
    )
    Get-CimInstance Win32_Process | Where-Object {
        $cmd = [string]$_.CommandLine
        if (-not $cmd) {
            return $false
        }
        foreach ($pattern in $patterns) {
            if ($cmd -like $pattern) {
                return $true
            }
        }
        return $false
    }
}

function Get-ProcessFamilyIds {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RootPid
    )

    $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId
    $childrenByParent = @{}
    foreach ($proc in $all) {
        $parentId = [int]$proc.ParentProcessId
        if (-not $childrenByParent.ContainsKey($parentId)) {
            $childrenByParent[$parentId] = New-Object System.Collections.Generic.List[int]
        }
        $childrenByParent[$parentId].Add([int]$proc.ProcessId)
    }

    $family = New-Object System.Collections.Generic.HashSet[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if (-not $family.Add($current)) {
            continue
        }
        if ($childrenByParent.ContainsKey($current)) {
            foreach ($childId in $childrenByParent[$current]) {
                $queue.Enqueue($childId)
            }
        }
    }

    $currentId = $RootPid
    while ($true) {
        $currentProc = $all | Where-Object { [int]$_.ProcessId -eq $currentId } | Select-Object -First 1
        if (-not $currentProc) {
            break
        }
        $parentId = [int]$currentProc.ParentProcessId
        if ($parentId -le 0) {
            break
        }
        if (-not $family.Add($parentId)) {
            break
        }
        $currentId = $parentId
    }

    return $family
}

function Remove-StaleLoopProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [int]$CurrentLoopPid,
        [Parameter(Mandatory = $true)]
        [string]$LogPath
    )

    $preserve = if ($CurrentLoopPid -gt 0) {
        Get-ProcessFamilyIds -RootPid $CurrentLoopPid
    } else {
        New-Object System.Collections.Generic.HashSet[int]
    }

    $stale = Get-LoopRelatedProcesses | Where-Object { -not $preserve.Contains([int]$_.ProcessId) }
    foreach ($proc in $stale | Sort-Object ProcessId -Descending) {
        try {
            Stop-Process -Id ([int]$proc.ProcessId) -Force -ErrorAction Stop
            Write-WatchdogLog -Path $LogPath -Message "killed stale process pid=$($proc.ProcessId) parent=$($proc.ParentProcessId) name=$($proc.Name)"
        } catch {
            Write-WatchdogLog -Path $LogPath -Message "failed to kill stale process pid=$($proc.ProcessId): $($_.Exception.Message)"
        }
    }
}

$repoRoot = "I:\code\usertest"
$watchdogDir = Join-Path $repoRoot "runs\_continuous_loop"
$watchdogLog = Join-Path $watchdogDir "watchdog.log"
$loopPidPath = Join-Path $watchdogDir "loop.pid"
$loopLogPath = Join-Path $watchdogDir "continuous_loop.log"
New-Item -ItemType Directory -Force -Path $watchdogDir | Out-Null
Write-WatchdogLog -Path $watchdogLog -Message "watchdog starting"

while ($true) {
    try {
        $loopAlive = $false
        if (Test-Path -LiteralPath $loopPidPath) {
            $rawPid = (Get-Content -LiteralPath $loopPidPath | Select-Object -First 1).Trim()
            if ($rawPid) {
                $loopProc = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
                if ($loopProc) {
                    $loopAlive = $true
                }
            }
        }

        if (-not $loopAlive) {
            Remove-StaleLoopProcesses -CurrentLoopPid 0 -LogPath $watchdogLog
            Write-WatchdogLog -Path $watchdogLog -Message "loop not running; restarting"
            Start-ContinuousLoop -RepoRoot $repoRoot -LogPath $watchdogLog
        } elseif (Test-Path -LiteralPath $loopLogPath) {
            Remove-StaleLoopProcesses -CurrentLoopPid ([int]$rawPid) -LogPath $watchdogLog
            $ageSeconds = ((Get-Date) - (Get-Item -LiteralPath $loopLogPath).LastWriteTime).TotalSeconds
            if ($ageSeconds -gt 1800) {
                Write-WatchdogLog -Path $watchdogLog -Message "loop log stale for ${ageSeconds}s"
            }
        }
    } catch {
        Write-WatchdogLog -Path $watchdogLog -Message "watchdog error: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds 60
}
