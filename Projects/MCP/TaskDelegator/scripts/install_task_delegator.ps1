<#
Registers the task-delegator MCP server in Claude Code (user scope) with the paths of THIS clone,
removes the pre-2026-11-18 registration "aitools" if it is still there, then checks the result.

Run in your own PowerShell window:
  powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\install_task_delegator.ps1

Options:
  -Force             re-register when task-delegator is already registered (e.g. after moving the clone);
                     the server environment registered before is kept unless -ServerEnv is given
  -ServerEnv "K=V;K2=V2"
                     server environment variables, e.g.
                     "TASK_DELEGATOR_CODEX_USAGE_SCRIPT=D:\Tools\codex_usage.py;TASK_DELEGATOR_WORKER_MODEL=sonnet"
                     (a ';' not followed by NAME= stays inside the value, so
                     "TASK_DELEGATOR_WORKER_DENY=Bash(git push:*);Bash(avrdude:*)" is one variable)
  -Claude <path>     claude.exe to use (default: found automatically)
  -DryRun            print what would be done, change nothing

The server is registered for the Claude account of CLAUDE_CONFIG_DIR (default: ~\.claude).
Its tools appear in Claude sessions started after the registration.
#>
param(
    [switch]$Force,
    [string[]]$ServerEnv = @(),
    [string]$Claude,
    [switch]$DryRun
)

# Not 'Stop': in Windows PowerShell 5.1 a native command writing to stderr would then throw.
$ErrorActionPreference = 'Continue'
$Name = 'task-delegator'
$OldName = 'aitools'

$project = Split-Path -Parent $PSScriptRoot
$clone = (Resolve-Path (Join-Path $project '..\..\..')).Path
$python = Join-Path $clone 'Utils\Bin\Windows\Python3\python.exe'
$server = Join-Path $project 'server.py'

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Find-ClaudeExe {
    if ($Claude) { return $Claude }
    if ($env:TASK_DELEGATOR_CLAUDE_EXE) { return $env:TASK_DELEGATOR_CLAUDE_EXE }
    $patterns = @(
        "$env:LOCALAPPDATA\Claude-Data\claude_*\claude-code\*\*\claude.exe",                    # desktop app bundle
        "$env:LOCALAPPDATA\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\*\claude.exe" # MSIX install
    )
    foreach ($p in $patterns) {
        $found = Get-ChildItem $p -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime | Select-Object -Last 1 -ExpandProperty FullName
        if ($found) { return $found }
    }
    $native = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path $native) { return $native }
    $cmd = Get-Command claude.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-Registered([string]$ServerName) {
    & $script:claudeExe mcp get $ServerName *> $null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-Claude([string[]]$Arguments) {
    Write-Host ("> claude " + ($Arguments -join ' ')) -ForegroundColor Cyan
    if ($DryRun) { return 0 }
    & $script:claudeExe @Arguments | Out-Host
    return $LASTEXITCODE
}

function Get-RegisteredEnv {
    # User-scope MCP servers live in <config dir>\.claude.json (CLAUDE_CONFIG_DIR) or ~\.claude.json.
    $dir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { $env:USERPROFILE }
    $file = Join-Path $dir '.claude.json'
    if (-not (Test-Path $file)) { return @() }
    try {
        $cfg = Get-Content $file -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Host "WARNING: could not read $file, the old server environment is not carried over" -ForegroundColor Yellow
        return @()
    }
    $srv = $cfg.mcpServers.$Name
    if (-not $srv -or -not $srv.env) { return @() }
    return @($srv.env.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" })
}

# --- prerequisites ---------------------------------------------------------------------------
if (-not (Test-Path $python)) { Fail "portable Python not found: $python. Run $clone\python_setup.bat first." }
if (-not (Test-Path $server)) { Fail "server.py not found: $server" }
& $python -c "import mcp" *> $null
if ($LASTEXITCODE -ne 0) { Fail "the 'mcp' package is missing in $python. Run $clone\python_setup.bat -Force." }

$script:claudeExe = Find-ClaudeExe
if (-not $script:claudeExe -or -not (Test-Path $script:claudeExe)) {
    Fail "claude.exe not found. Pass -Claude <path> (see Projects\UsageGuard\README.md, 'Find claude.exe')."
}

# Split only where the next item starts with NAME=: a value may itself contain ';'
# (TASK_DELEGATOR_WORKER_DENY=Bash(git push:*);Bash(avrdude:*)).
$envPairs = @($ServerEnv | ForEach-Object { $_ -split ';(?=\s*[A-Za-z_][A-Za-z0-9_]*=)' } |
    ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($kv in $envPairs) {
    if ($kv -notmatch '^[A-Za-z_][A-Za-z0-9_]*=') { Fail "bad -ServerEnv entry '$kv' (expected NAME=value)" }
}

Write-Host "Clone:       $clone"
Write-Host "Python:      $python"
Write-Host "Server:      $server"
Write-Host "Claude Code: $script:claudeExe"
if ($env:CLAUDE_CONFIG_DIR) { Write-Host "Account:     CLAUDE_CONFIG_DIR=$env:CLAUDE_CONFIG_DIR" }
if ($DryRun) { Write-Host "Dry run: nothing will be changed." -ForegroundColor Yellow }
Write-Host ""

# --- migration from AITools ------------------------------------------------------------------
if (Test-Registered $OldName) {
    Write-Host "Found the old registration '$OldName' (renamed to '$Name' on 2026-11-18); removing it."
    $rc = Invoke-Claude @('mcp', 'remove', $OldName, '-s', 'user')
    if ($rc -ne 0) { Write-Host "WARNING: remove it by hand: claude mcp remove $OldName (add -s <scope>)" -ForegroundColor Yellow }
}
$oldState = Join-Path $env:LOCALAPPDATA 'AITools'
if (Test-Path $oldState) {
    Write-Host "Old job history found: $oldState. It is not used any more: delete it, or move its jobs\*"
    Write-Host "into $env:LOCALAPPDATA\TaskDelegator\jobs to keep them visible in codex_status."
}

# --- registration ----------------------------------------------------------------------------
$register = $true
if (Test-Registered $Name) {
    if (-not $Force) {
        Write-Host "'$Name' is already registered; left as is (pass -Force to re-register with this clone's paths)."
        $register = $false
    } else {
        if ($envPairs.Count -eq 0) {
            $envPairs = Get-RegisteredEnv
            if ($envPairs.Count -gt 0) { Write-Host "Keeping the registered environment: $($envPairs -join '; ')" }
        }
        $rc = Invoke-Claude @('mcp', 'remove', $Name, '-s', 'user')
        if ($rc -ne 0) { Fail "could not remove the existing '$Name' (registered in another scope? see: claude mcp get $Name)" }
    }
}
if ($register) {
    $addArgs = @('mcp', 'add', '--scope', 'user', $Name)
    foreach ($kv in $envPairs) { $addArgs += @('-e', $kv) }
    $addArgs += @('--', $python, $server)
    $rc = Invoke-Claude $addArgs
    if ($rc -ne 0) { Fail "claude mcp add failed (exit code $rc)" }
}
if ($DryRun) { exit 0 }

# --- check -----------------------------------------------------------------------------------
Write-Host ""
Write-Host "Check: claude mcp get $Name" -ForegroundColor Cyan
& $script:claudeExe mcp get $Name | Out-Host
Write-Host "Check: delegator_doctor (no quota spent)" -ForegroundColor Cyan
foreach ($kv in $envPairs) {
    $k, $v = $kv -split '=', 2
    Set-Item -Path "Env:$k" -Value $v
}
& $python (Join-Path $PSScriptRoot 'doctor.py') | Out-Host
$rc = $LASTEXITCODE
Write-Host ""
Write-Host "Done. Start a new Claude session to get the mcp__${Name}__* tools."
Write-Host "Lines marked !! above need attention; worker B sign-in: scripts\worker_login.ps1."
exit $rc
