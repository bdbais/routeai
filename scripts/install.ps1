<#
.SYNOPSIS
    Install, update or remove the routeai plugin in Claude Code from this folder, and verify it works.

.DESCRIPTION
    1. Checks Python 3.11+ and the Claude Code CLI (not the desktop app executable).
    2. Validates the plugin manifests and runs the offline test suite.
    3. Creates ~/.routeai/fleet.toml if missing, probing the nodes passed with -Node.
    4. Shows the fleet status (node health, routing per category).
    5. Registers this folder as the local marketplace "bais" and installs routeai@bais,
       or refreshes both when they are already installed.
    6. Confirms Claude Code lists the plugin and that its MCP server starts.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Node "gpu=http://192.168.1.13:11434","laptop=http://localhost:11434"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string[]]$Node = @(),
    [switch]$SkipTests,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Continue'  # native tools report failures through exit codes
$Root = Split-Path -Parent $PSScriptRoot
$RunPy = Join-Path $Root 'run.py'
$Marketplace = 'bais'
$PluginId = 'routeai@bais'
$FleetHome = if ($env:ROUTEAI_HOME) { $env:ROUTEAI_HOME } else { Join-Path $env:USERPROFILE '.routeai' }
$ConfigFile = if ($env:ROUTEAI_CONFIG) { $env:ROUTEAI_CONFIG } else { Join-Path $FleetHome 'fleet.toml' }

function Step([string]$Message) { Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }
function Ok([string]$Message) { Write-Host "    [ok] $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "    [!!] $Message" -ForegroundColor Yellow }
function Die([string]$Message) { Write-Host "    [xx] $Message" -ForegroundColor Red; exit 1 }

function Quote([string]$Arg) {
    if ($Arg -match '[\s"]') { return '"' + ($Arg -replace '"', '\"') + '"' }
    return $Arg
}

# Runs a native program and returns its exit code and combined output. Output goes through
# temp files so Windows PowerShell 5.1 never wraps stderr lines into error records.
function Run([string]$Exe, [string[]]$ArgList, [string]$StdinFile = $null) {
    $outFile = [IO.Path]::GetTempFileName()
    $errFile = [IO.Path]::GetTempFileName()
    $params = @{
        FilePath = $Exe; ArgumentList = (($ArgList | ForEach-Object { Quote $_ }) -join ' ')
        WorkingDirectory = $Root; NoNewWindow = $true; PassThru = $true
        RedirectStandardOutput = $outFile; RedirectStandardError = $errFile
    }
    if ($StdinFile) { $params.RedirectStandardInput = $StdinFile }
    $proc = Start-Process @params
    $null = $proc.Handle  # keeps ExitCode available after WaitForExit on PowerShell 5.1
    $proc.WaitForExit()
    $text = "$(Get-Content $outFile -Raw -Encoding UTF8)`n$(Get-Content $errFile -Raw -Encoding UTF8)".Trim()
    Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
    return [pscustomobject]@{ Code = $proc.ExitCode; Out = $text }
}

function Find-ClaudeCli {
    # "claude" on PATH is often the desktop app (AnthropicClaude\claude.exe), which is not the CLI.
    $candidates = New-Object System.Collections.Generic.List[string]
    $candidates.Add((Join-Path $env:USERPROFILE '.local\bin\claude.exe'))
    $bundled = Join-Path $env:APPDATA 'Claude\claude-code'
    if (Test-Path $bundled) {
        Get-ChildItem $bundled -Directory |
            Sort-Object { try { [version]$_.Name } catch { [version]'0.0' } } -Descending |
            ForEach-Object { $candidates.Add((Join-Path $_.FullName 'claude.exe')) }
    }
    Get-Command claude -All -ErrorAction SilentlyContinue | ForEach-Object { $candidates.Add($_.Source) }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path $candidate) -or $candidate -like '*\AnthropicClaude\*') { continue }
        $version = Run $candidate @('--version')
        if ($version.Out -match 'Claude Code') { return $candidate }
    }
    return $null
}

# -- prerequisites ------------------------------------------------------------------

Step 'Checking prerequisites'
# Same order as the plugin's launcher (bin\routeai.cmd): the py launcher first, then python.
$PyExe = $null
$PyPre = @()
$probeCode = 'import sys; print(int(sys.version_info >= (3, 11)), sys.version.split()[0], sys.executable)'
foreach ($candidate in @(@('py', '-3'), @('python'))) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
    $pre = @($candidate | Select-Object -Skip 1)
    $probe = Run $candidate[0] ($pre + @('-c', $probeCode))
    if ($probe.Code -eq 0 -and $probe.Out -match '^1 ') { $PyExe = $candidate[0]; $PyPre = $pre; break }
}
if (-not $PyExe) { Die 'Python 3.11+ not found (tried "py -3" and "python"). Install it from python.org and run this script again.' }
$pyInfo = $probe.Out.Split(' ', 3)
Ok "Python $($pyInfo[1]) ($($pyInfo[2].Trim()))"

$ClaudeCli = Find-ClaudeCli
if (-not $ClaudeCli) { Die 'Claude Code CLI not found. Install Claude Code (https://code.claude.com/docs) and run this script again.' }
Ok "Claude Code CLI: $ClaudeCli ($((Run $ClaudeCli @('--version')).Out))"

# -- uninstall ----------------------------------------------------------------------

if ($Uninstall) {
    Step "Removing $PluginId"
    $r = Run $ClaudeCli @('plugin', 'uninstall', $PluginId)
    if ($r.Code -eq 0) { Ok 'plugin uninstalled' } else { Warn $r.Out }
    $r = Run $ClaudeCli @('plugin', 'marketplace', 'remove', $Marketplace)
    if ($r.Code -eq 0) { Ok "marketplace '$Marketplace' removed" } else { Warn $r.Out }
    Write-Host ''
    Write-Host "Your fleet config and learned stats in $FleetHome were kept."
    exit 0
}

# -- validate and test ----------------------------------------------------------------

Step 'Validating the plugin'
foreach ($target in @($Root, (Join-Path $Root '.claude-plugin\plugin.json'))) {
    $r = Run $ClaudeCli @('plugin', 'validate', $target)
    if ($r.Code -ne 0) { Die "validation failed for $target`n$($r.Out)" }
}
Ok 'marketplace and plugin manifests are valid'

if (-not $SkipTests) {
    Step 'Running the offline test suite'
    $r = Run $PyExe ($PyPre + @('-m', 'unittest', 'discover', '-s', 'tests'))
    if ($r.Code -ne 0) { Write-Host $r.Out; Die 'tests failed' }
    $summary = ($r.Out -split "`r?`n" | Where-Object { $_ -match '^(Ran |OK)' }) -join ' - '
    Ok $summary
}

# -- fleet configuration ----------------------------------------------------------------

Step 'Fleet configuration'
if (Test-Path $ConfigFile) {
    Ok "using $ConfigFile"
    if ($Node.Count) { Warn '-Node ignored because a config already exists (edit it, or delete it to regenerate).' }
} else {
    $initArgs = @($RunPy, 'init')
    $specs = if ($Node.Count) { $Node } else { @('local=http://localhost:11434') }
    foreach ($spec in $specs) { $initArgs += @('--node', $spec) }
    $r = Run $PyExe ($PyPre + $initArgs)
    Write-Host $r.Out
    if ($r.Code -ne 0) { Die 'could not write the fleet config' }
    Ok "created $ConfigFile - adjust the models per machine, then run the benchmark"
}

Step 'Fleet status'
$status = Run $PyExe ($PyPre + @($RunPy, 'status'))
$status.Out -split "`r?`n" | ForEach-Object { Write-Host "    $_" }
if ($status.Out -notmatch '\[OK \]') { Warn 'no Ollama node is reachable right now; the plugin installs anyway.' }
elseif ($status.Out -match '\[DOWN\]') { Warn 'some nodes are down; they are skipped until they come back.' }

# -- install into Claude Code -------------------------------------------------------------

Step "Installing $PluginId into Claude Code"
$markets = Run $ClaudeCli @('plugin', 'marketplace', 'list')
if ($markets.Out -match "(?m)^\W*$Marketplace\s*$") {
    $r = Run $ClaudeCli @('plugin', 'marketplace', 'update', $Marketplace)
    if ($r.Code -ne 0) { Die "marketplace update failed:`n$($r.Out)" }
    Ok "marketplace '$Marketplace' refreshed from $Root"
} else {
    $r = Run $ClaudeCli @('plugin', 'marketplace', 'add', $Root)
    if ($r.Code -ne 0) { Die "marketplace add failed:`n$($r.Out)" }
    Ok "marketplace '$Marketplace' added ($Root)"
}

$plugins = Run $ClaudeCli @('plugin', 'list')
if ($plugins.Out -match 'routeai') {
    $r = Run $ClaudeCli @('plugin', 'update', $PluginId)
    if ($r.Code -ne 0) { Die "plugin update failed:`n$($r.Out)" }
    Ok 'plugin updated'
} else {
    $r = Run $ClaudeCli @('plugin', 'install', $PluginId, '--scope', 'user')
    if ($r.Code -ne 0) { Die "plugin install failed:`n$($r.Out)" }
    Ok 'plugin installed for your user (available in every project)'
}

# -- verify -------------------------------------------------------------------------------

Step 'Verifying'
$plugins = Run $ClaudeCli @('plugin', 'list')
if ($plugins.Out -notmatch 'routeai') { Die "Claude Code does not list the plugin:`n$($plugins.Out)" }
Ok 'Claude Code lists routeai'

$requests = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllLines($requests, [string[]]@(
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install.ps1","version":"1"}}}',
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
))
$handshake = Run (Join-Path $Root 'bin\routeai.cmd') @('serve') $requests  # the launcher Claude Code uses
Remove-Item $requests -ErrorAction SilentlyContinue
$toolCount = ([regex]::Matches($handshake.Out, '"name": ?"fleet_')).Count
if ($toolCount -lt 11) { Die "the MCP server did not answer as expected:`n$($handshake.Out)" }
Ok "MCP server starts and exposes $toolCount tools"

Write-Host ''
Write-Host 'Done. Open a NEW Claude Code session (plugins load when a session starts), then try:' -ForegroundColor Green
Write-Host '    /routeai:status     fleet health, routing and token savings'
Write-Host '    /routeai:bench      run the self-learning benchmark'
Write-Host '    or ask: "use the local fleet to write tests for <file>"'
Write-Host ''
Write-Host "Config: $ConfigFile"
Write-Host "Stats, task log and reports: $FleetHome"
Write-Host 'After changing the plugin code, run this script again. To remove it: -Uninstall'
