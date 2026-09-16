<#
.SYNOPSIS
    Development helper for Claude Code's Stop hook: reinstall and verify the plugin with install.ps1,
    but only when plugin files changed since the last successful run.

.DESCRIPTION
    Exit 0: nothing changed, or the reinstall succeeded (a JSON systemMessage tells the user).
    Exit 2: the reinstall failed; stderr carries the log so Claude sees it and fixes the problem.
    A failure is reported once per set of files, so a problem Claude cannot fix never loops.
#>
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $Root '.claude'
$OkStamp = Join-Path $StateDir 'install-ok.stamp'
$FailStamp = Join-Path $StateDir 'install-failed.stamp'
$Watched = @('.claude-plugin', 'src', 'skills', 'tests', 'run.py', 'scripts\install.ps1')

$files = foreach ($item in $Watched) {
    $path = Join-Path $Root $item
    if (Test-Path $path -PathType Leaf) { Get-Item $path }
    elseif (Test-Path $path) { Get-ChildItem $path -Recurse -File | Where-Object { $_.FullName -notmatch '\\__pycache__\\' } }
}
$lines = $files | Sort-Object FullName | ForEach-Object { "$($_.FullName)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }
$sha = [Security.Cryptography.SHA256]::Create()
$fingerprint = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($lines -join "`n")))).Replace('-', '')

function Read-Stamp([string]$Path) {
    if (Test-Path $Path) { return (Get-Content $Path -Raw).Trim() }
    return ''
}

if ($fingerprint -eq (Read-Stamp $OkStamp)) { exit 0 }
if ($fingerprint -eq (Read-Stamp $FailStamp)) {
    Write-Output (ConvertTo-Json @{ systemMessage = 'routeai: install.ps1 is still failing for the current files (not re-run). Run scripts\install.ps1 to see the error.' } -Compress)
    exit 0
}

$log = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install.ps1') 2>&1 | Out-String
$code = $LASTEXITCODE
New-Item -ItemType Directory -Force $StateDir | Out-Null

if ($code -eq 0) {
    Set-Content -Path $OkStamp -Value $fingerprint -Encoding ASCII
    Remove-Item $FailStamp -ErrorAction SilentlyContinue
    $tests = ($log -split "`r?`n" | Where-Object { $_ -match 'Ran \d+ tests' } | Select-Object -First 1)
    $summary = if ($tests) { ($tests -replace '^\s*\[ok\]\s*', '').Trim() } else { 'tests skipped' }
    Write-Output (ConvertTo-Json @{ systemMessage = "routeai: plugin reinstalled and verified ($summary). New sessions load the new version." } -Compress)
    exit 0
}

Set-Content -Path $FailStamp -Value $fingerprint -Encoding ASCII
[Console]::Error.WriteLine("scripts\install.ps1 failed after changes to the routeai plugin. Fix the problem below; the Stop hook re-runs it once the files change again.`n`n$log")
exit 2
