# One-time sign-in of worker B (the second Claude account) for TaskDelegator.
#
# Run in YOUR OWN PowerShell window (not through Claude):
#   powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\worker_login.ps1
#
# Claude Code opens with a separate config folder. Type /login, sign in with the SECOND
# account in the browser, then type /exit. Nothing here touches your main Claude login.

$configDir = if ($env:TASK_DELEGATOR_WORKER_CONFIG_DIR) { $env:TASK_DELEGATOR_WORKER_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude-worker-b' }
New-Item -ItemType Directory -Force $configDir | Out-Null

$claude = Get-ChildItem "$env:LOCALAPPDATA\Claude-Data\claude_*\claude-code\*\*\claude.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime | Select-Object -Last 1 -ExpandProperty FullName
if (-not $claude) { $claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe' }
if (-not (Test-Path $claude)) { Write-Host "claude.exe not found"; exit 1 }

# Do not inherit the lead account's session or tokens.
foreach ($v in 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN') {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}
$env:CLAUDE_CONFIG_DIR = $configDir

Write-Host "Worker B config folder: $configDir"
Write-Host "Claude Code: $claude"
Write-Host ""
Write-Host "Type /login, sign in with the SECOND Claude account, then /exit."
Write-Host ""
Set-Location $configDir
& $claude

Write-Host ""
Write-Host "Check:"
& $claude auth status
