[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Path
)

$ErrorActionPreference = 'Stop'

$hadErrors = $false
foreach ($p in $Path) {
    $resolved = Resolve-Path -LiteralPath $p -ErrorAction Stop
    $text = [System.IO.File]::ReadAllText($resolved.Path)

    # Normalize newlines so PSParser line/column reporting is consistent across environments.
    $text = $text -replace "`r?`n", "`r`n"

    $errs = @()
    [System.Management.Automation.PSParser]::Tokenize($text, [ref]$errs) > $null

    if ($errs.Count -gt 0) {
        $hadErrors = $true
        [Console]::Error.WriteLine("PowerShell parse FAILED: {0}" -f $resolved.Path)
        foreach ($e in $errs) {
            [Console]::Error.WriteLine("  line {0}, col {1}: {2}" -f $e.StartLine, $e.StartColumn, $e.Message)
        }
    }
}

if ($hadErrors) {
    exit 1
}

Write-Host 'PowerShell parse OK'
