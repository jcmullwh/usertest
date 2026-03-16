# Shared PowerShell helpers for resolving a usable Python interpreter on Windows.
#
# Goals:
# - Prefer repo-local .venv when present
# - Avoid WindowsApps launcher shims (App Execution Alias)
# - Detect "Access is denied" / blocked launch failures
# - Fail fast (default: within ~5s total) with actionable remediation

$ErrorActionPreference = 'Stop'

function Write-PreflightErr {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Message
    )

    [Console]::Error.WriteLine($Message)
}

function _Quote-ProcessArg {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '"', '\\"') + '"'
}

function _Invoke-ProcessWithTimeout {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $false)]
        [double]$TimeoutSeconds = 5.0
    )

    $timeoutMs = [Math]::Max(100, [int]([double]$TimeoutSeconds * 1000))
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = (($Arguments | ForEach-Object { _Quote-ProcessArg -Value $_ }) -join ' ')
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    $null = $proc.Start()
    if (-not $proc.WaitForExit($timeoutMs)) {
        try { $proc.Kill($true) } catch { try { $proc.Kill() } catch { } }
        return @{
            TimedOut = $true
            ExitCode = $null
            Stdout   = ""
            Stderr   = ""
        }
    }

    return @{
        TimedOut = $false
        ExitCode = $proc.ExitCode
        Stdout   = $proc.StandardOutput.ReadToEnd()
        Stderr   = $proc.StandardError.ReadToEnd()
    }
}

function _Is-WindowsAppsAliasPath {
    param([Parameter(Mandatory = $true)][string]$PathText)
    return $PathText.Replace('/', '\').ToLower().Contains('\windowsapps\')
}

function _Get-VenvPythonPath {
    param([Parameter(Mandatory = $true)][string]$VenvRoot)
    return Join-Path $VenvRoot 'Scripts\python.exe'
}

function _Get-WindowsWhereAll {
    param([Parameter(Mandatory = $true)][string]$CommandName)

    try {
        $proc = _Invoke-ProcessWithTimeout -FilePath 'where.exe' -Arguments @($CommandName) -TimeoutSeconds 2.0
    }
    catch {
        return @()
    }

    if ($proc.TimedOut -or ($proc.ExitCode -as [int]) -ne 0) {
        return @()
    }

    $out = @()
    foreach ($line in ([string]$proc.Stdout -split "`r?`n")) {
        $candidate = $line.Trim()
        if ($candidate) {
            $out += $candidate
        }
    }
    return $out
}

function _Get-WindowsPy0pInterpreters {
    try {
        $proc = _Invoke-ProcessWithTimeout -FilePath 'py' -Arguments @('-0p') -TimeoutSeconds 2.0
    }
    catch {
        return @()
    }

    if ($proc.TimedOut -or ($proc.ExitCode -as [int]) -ne 0) {
        return @()
    }

    $out = @()
    foreach ($line in ([string]$proc.Stdout -split "`r?`n")) {
        if (-not $line.Trim()) {
            continue
        }
        $match = [regex]::Match($line, '([A-Za-z]:\\.*)$')
        if ($match.Success) {
            $out += $match.Groups[1].Value.Trim()
        }
    }
    return $out
}

function _Get-CommonWindowsPythonCandidatePaths {
    [CmdletBinding()]
    param()

    $candidates = @()

    foreach ($ver in @("313", "312", "311")) {
        $candidates += "C:\\Python$ver\\python.exe"
    }

    $localAppData = $env:LOCALAPPDATA
    if ($localAppData) {
        foreach ($ver in @("313", "312", "311")) {
            $candidates += (Join-Path $localAppData "Programs\\Python\\Python$ver\\python.exe")
        }
    }

    $programFiles = $env:ProgramFiles
    if ($programFiles) {
        foreach ($ver in @("313", "312", "311")) {
            $candidates += (Join-Path $programFiles "Python$ver\\python.exe")
        }
    }

    $programFilesX86 = ${env:ProgramFiles(x86)}
    if ($programFilesX86) {
        foreach ($ver in @("313", "312", "311")) {
            $candidates += (Join-Path $programFilesX86 "Python$ver\\python.exe")
        }
    }

    $existing = @()
    foreach ($path in $candidates) {
        if (-not $path) { continue }
        if (Test-Path -LiteralPath $path) {
            $existing += $path
        }
    }

    return @($existing | Select-Object -Unique)
}

function _Summarize-MultilineText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Text,
        [Parameter(Mandatory = $false)]
        [int]$MaxLines = 6,
        [Parameter(Mandatory = $false)]
        [int]$MaxChars = 700
    )

    $t = ($Text -replace "`r`n?", "`n").Trim()
    if (-not $t) { return "" }

    $lines = $t -split "`n"
    if ($lines.Count -gt $MaxLines) {
        $lines = @($lines[0..($MaxLines - 1)] + "...[truncated]...")
    }
    $out = ($lines -join "`n").Trim()
    if ($out.Length -gt $MaxChars) {
        $out = $out.Substring(0, $MaxChars).TrimEnd() + "...[truncated]..."
    }
    return $out
}

function Test-PythonInterpreter {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandPath,
        [Parameter(Mandatory = $false)]
        [double]$TimeoutSeconds = 5.0
    )

    $probeCode = "import encodings, json, sys; print(json.dumps({'executable': sys.executable, 'version': sys.version.split()[0]}))"
    $result = _Invoke-ProcessWithTimeout -FilePath $CommandPath -Arguments @('-c', $probeCode) -TimeoutSeconds $TimeoutSeconds

    if ($result.TimedOut) {
        return @{
            Usable     = $false
            ReasonCode = "timeout"
            Reason     = "Interpreter health probe timed out (likely a shim or broken runtime)."
            Version    = $null
            Executable = $null
        }
    }

    $stdout = ([string]$result.Stdout).Trim()
    $stderr = ([string]$result.Stderr).Trim()
    $merged = (@($stderr, $stdout) | Where-Object { $_ } | ForEach-Object { $_.Trim() }) -join "`n"
    $lowered = $merged.ToLower()

    if (($result.ExitCode -as [int]) -ne 0) {
        if (
            $lowered.Contains("access is denied") -or
            $lowered.Contains("permission denied") -or
            $lowered.Contains("cannot be accessed by the system")
        ) {
            return @{
                Usable     = $false
                ReasonCode = "access_denied"
                Reason     = $merged
                Version    = $null
                Executable = $null
            }
        }
        if ($lowered.Contains("encodings") -and (
                $lowered.Contains("modulenotfounderror") -or $lowered.Contains("no module named")
            )) {
            return @{
                Usable     = $false
                ReasonCode = "missing_stdlib"
                Reason     = $merged
                Version    = $null
                Executable = $null
            }
        }
        return @{
            Usable     = $false
            ReasonCode = "runtime_probe_failed"
            Reason     = $merged
            Version    = $null
            Executable = $null
        }
    }

    $payloadLine = ($stdout -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
    try {
        $payload = $payloadLine | ConvertFrom-Json
    }
    catch {
        return @{
            Usable     = $false
            ReasonCode = "runtime_probe_failed"
            Reason     = "Interpreter probe did not emit parseable JSON payload."
            Version    = $null
            Executable = $null
        }
    }

    $exe = $payload.executable
    $ver = $payload.version
    return @{
        Usable     = $true
        ReasonCode = $null
        Reason     = $null
        Version    = ($ver | ForEach-Object { "$_" }).Trim()
        Executable = ($exe | ForEach-Object { "$_" }).Trim()
    }
}

function Resolve-UsablePython {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,
        [Parameter(Mandatory = $false)]
        [string]$VenvDirName = ".venv",
        [Parameter(Mandatory = $false)]
        [double]$TimeoutSeconds = 5.0
    )

    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(0.1, [double]$TimeoutSeconds))
    $rejections = @()

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $candidates = New-Object System.Collections.Generic.List[object]

    function Add-Candidate {
        param(
            [Parameter(Mandatory = $true)][string]$Name,
            [Parameter(Mandatory = $false)][string]$CommandPath
        )
        if (-not $CommandPath) {
            return
        }
        $trimmed = $CommandPath.Trim()
        if (-not $trimmed) {
            return
        }
        if (-not $seen.Add($trimmed)) {
            return
        }
        $null = $candidates.Add(@{ Name = $Name; CommandPath = $trimmed })
    }

    $sandboxPython = [string]$env:USERTEST_PYTHON
    if ($sandboxPython.Trim()) {
        Add-Candidate -Name 'sandbox_env' -CommandPath $sandboxPython
    }

    Add-Candidate -Name 'workspace_venv' -CommandPath (_Get-VenvPythonPath -VenvRoot (Join-Path $RepoRoot $VenvDirName))

    $virtualEnv = [string]$env:VIRTUAL_ENV
    if ($virtualEnv.Trim()) {
        Add-Candidate -Name 'virtual_env' -CommandPath (_Get-VenvPythonPath -VenvRoot $virtualEnv)
    }

    foreach ($entry in (_Get-WindowsPy0pInterpreters)) {
        Add-Candidate -Name 'py_0p' -CommandPath $entry
    }

    foreach ($entry in (_Get-WindowsWhereAll -CommandName 'python')) {
        if (-not (_Is-WindowsAppsAliasPath -PathText $entry)) {
            Add-Candidate -Name 'where_python' -CommandPath $entry
        }
    }

    foreach ($entry in (_Get-WindowsWhereAll -CommandName 'python3')) {
        if (-not (_Is-WindowsAppsAliasPath -PathText $entry)) {
            Add-Candidate -Name 'where_python3' -CommandPath $entry
        }
    }

    foreach ($commandName in @('py', 'python', 'python3')) {
        $cmd = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($cmd) {
            $resolved = $cmd.Source
            if (-not $resolved) { $resolved = $cmd.Path }
            Add-Candidate -Name ("command_" + $commandName) -CommandPath $resolved
        }
    }

    if ($env:OS -eq "Windows_NT") {
        foreach ($path in (_Get-CommonWindowsPythonCandidatePaths)) {
            Add-Candidate -Name 'common_python' -CommandPath $path
        }
    }

    foreach ($candidate in $candidates) {
        $remaining = ($deadline - [DateTime]::UtcNow).TotalSeconds
        if ($remaining -le 0) {
            break
        }

        $name = $candidate.Name
        $resolved = $candidate.CommandPath

        if (-not $resolved) {
            $rejections += "[$name] could not resolve command path"
            continue
        }

        if (_Is-WindowsAppsAliasPath -PathText $resolved) {
            $rejections += "[$name] rejected WindowsApps alias: $resolved"
            continue
        }

        try {
            $probe = Test-PythonInterpreter -CommandPath $resolved -TimeoutSeconds $remaining
        }
        catch {
            $rejections += "[$name] interpreter probe failed: $($_.Exception.Message) ($resolved)"
            continue
        }

        if (-not $probe.Usable) {
            $reasonCode = $probe.ReasonCode
            $reason = $probe.Reason
            if ($reason) {
                $reasonShort = _Summarize-MultilineText -Text $reason
                $rejections += "[$name] rejected ($reasonCode): $resolved`n    $reasonShort"
            }
            else {
                $rejections += "[$name] rejected ($reasonCode): $resolved"
            }
            continue
        }

        return @{
            Name        = $name
            CommandPath = $resolved
            Executable  = $probe.Executable
            Version     = $probe.Version
        }
    }

    $lines = @()
    $lines += "No usable Python interpreter found (within ~$TimeoutSeconds seconds)."
    $lines += ""
    $lines += "Tried:"
    if ($rejections.Count -eq 0) {
        $lines += "  - No candidates were discovered."
    }
    else {
        foreach ($line in $rejections) { $lines += "  - $line" }
    }
    $lines += ""
    $lines += "Fix options:"
    $lines += "  1) Install CPython (python.org) or via winget: winget install -e --id Python.Python.3.13"
    $lines += "  2) Disable App Execution Alias shims: Settings -> Apps -> Advanced app settings -> App execution aliases -> turn off python.exe/python3.exe"
    $lines += "  3) Use a portable/vendored Python and put its folder first on PATH"

    foreach ($line in $lines) {
        Write-PreflightErr $line
    }
    return $null
}
