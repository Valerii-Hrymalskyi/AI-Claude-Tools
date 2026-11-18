# One-time sign-in of another Claude account for TaskDelegator (agent "claude:<name>").
#
# Run in YOUR OWN PowerShell window (not through Claude):
#   powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\claude_login.ps1 -Account <name>
#
# Creates %USERPROFILE%\.claude-accounts\<name> (TASK_DELEGATOR_ACCOUNTS_ROOT changes the root) and
# opens Claude Code with it as the config folder. Type /login, sign in with THAT account in the
# browser, then /exit. Nothing here touches your main Claude login (~\.claude).
#
# -From <folder>  move an existing account folder there first (e.g. the old
#                 %USERPROFILE%\.claude-worker-b); its sign-in moves with it, so no new /login is needed.
param(
    [Parameter(Mandatory = $true)][string]$Account,
    [string]$From
)

if ($Account -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$') {
    Write-Host "Bad account name '$Account': letters, digits, '.', '_', '-'; up to 32 characters."
    exit 1
}
$root = if ($env:TASK_DELEGATOR_ACCOUNTS_ROOT) { $env:TASK_DELEGATOR_ACCOUNTS_ROOT } else { $env:USERPROFILE }
$configDir = Join-Path (Join-Path $root '.claude-accounts') $Account

$claude = Get-ChildItem "$env:LOCALAPPDATA\Claude-Data\claude_*\claude-code\*\*\claude.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime | Select-Object -Last 1 -ExpandProperty FullName
if (-not $claude) { $claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe' }
if ($env:TASK_DELEGATOR_CLAUDE_EXE) { $claude = $env:TASK_DELEGATOR_CLAUDE_EXE }
if (-not (Test-Path $claude)) { Write-Host "claude.exe not found (set TASK_DELEGATOR_CLAUDE_EXE)"; exit 1 }

if ($From) {
    if (-not (Test-Path $From)) { Write-Host "Folder not found: $From"; exit 1 }
    if (Test-Path $configDir) { Write-Host "Already exists, nothing moved: $configDir"; exit 1 }
    New-Item -ItemType Directory -Force (Split-Path $configDir) | Out-Null
    try {
        Move-Item -Path $From -Destination $configDir -ErrorAction Stop
    } catch {
        Write-Host "Could not move $From -> $configDir. Close every Claude session and job of that account and try again."
        Write-Host $_.Exception.Message
        exit 1
    }
    Write-Host "Moved $From -> $configDir"
}
New-Item -ItemType Directory -Force $configDir | Out-Null

# Do not inherit the lead account's session or tokens.
foreach ($v in 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN') {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}
$env:CLAUDE_CONFIG_DIR = $configDir

Write-Host "Account folder: $configDir"
Write-Host "Claude Code:    $claude"

$signedIn = $false
try { $signedIn = ((& $claude auth status | Out-String) | ConvertFrom-Json).loggedIn -eq $true } catch { }
if ($signedIn) {
    Write-Host "Already signed in; nothing to do."
} else {
    Write-Host ""
    Write-Host "Type /login, sign in with the account for 'claude:$Account', then /exit."
    Write-Host ""
    Set-Location $configDir
    & $claude
}

Write-Host ""
Write-Host "Check:"
& $claude auth status
Write-Host ""
Write-Host "Agent id: claude:$Account. list_agents() sees it at once; no re-registration is needed."
Write-Host "UsageGuard hooks for it: run Projects\UsageGuard\install_user_hooks.py again."
