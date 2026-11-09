<#
Registers the Redis ring (scripts/redis_ring.py) as a Windows scheduled task: it starts at logon, runs hidden for as
long as the session lasts, and is restarted by Task Scheduler if it fails. It waits on the agent's own Redis lane and
runs the agent's wake on each new entry. See docs/REDIS-RING.md for the wake table.

  Codex, on the laptop that runs the Codex desktop app:
    powershell -File scripts\install_redis_ring.ps1 -Agent codex -WakeArgs '--codex-thread','<uuid>' `
      -BridgeUrl https://<bridge>.run.app -Impersonate fleet-codex@<project>.iam.gserviceaccount.com -DryRun
  Gemini, on its own computer (drops into ~\.gemini\binder\inbox\):
    powershell -File scripts\install_redis_ring.ps1 -Agent gemini -BridgeUrl ... -Impersonate fleet-gemini@... -DryRun
  Any agent with a command that starts its turn ({message} or {file} is filled in):
    powershell -File scripts\install_redis_ring.ps1 -Agent grok -WakeArgs '--','<command>','{file}' -BridgeUrl ... -DryRun
  Remove:
    powershell -File scripts\install_redis_ring.ps1 -Agent codex -Remove

-DryRun prints the task it would register and asks redis_ring.py --dry-run whether the bridge is set up for this
agent; it registers nothing. Drop -DryRun only on the agent's own machine, with the owner's go.

No secret belongs here or in the task: the bridge URL and the service-account email are not secrets, and the token is
minted per request by impersonating that account (keyless). The login on this machine must hold token-creator on it.
claude-code-cli does not use this task: its doorbell is redis_doorbell.py run in the background by the session.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('codex', 'grok', 'gemini')][string]$Agent,
  [string[]]$WakeArgs = @(),
  [string]$BridgeUrl = '',
  [string]$Impersonate = '',
  [int]$MinGap = 120,
  [string]$ScriptDir = '',
  [string]$Python = '',
  [switch]$DryRun,
  [switch]$Remove
)
$ErrorActionPreference = 'Stop'
# Not a param default: under `powershell -File` on Windows PowerShell 5.1 $PSScriptRoot is empty there.
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$name = "SFDC24-RedisRing-$Agent"
if ($Remove) {
  if ($DryRun) { Write-Output "DRY RUN: would remove $name"; return }
  Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output "removed $name"
  return
}
if ($Agent -ne 'gemini' -and -not $WakeArgs) {
  throw "-WakeArgs is required for ${Agent}: '--codex-thread','<uuid>' or '--','<command>','{file}'"
}
foreach ($arg in @($WakeArgs) + @($BridgeUrl, $Impersonate)) {
  if ($arg -match '(?i)(key|token|secret|password)=' -or $arg -match '(?i)^(sk-|xai-|ghp_|AIza)') {
    throw "an argument looks like a secret; the task must carry none (the wake command reads its key from its own env)"
  }
}
if (-not $Python) {
  $Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
  if (-not $Python) { throw "pythonw.exe not found; pass -Python" }
}
$script = Join-Path $ScriptDir 'redis_ring.py'
if (-not (Test-Path $script)) { throw "missing $script" }
$log = Join-Path $HOME ".redis-ring-$Agent.log"

function Quote([string]$s) { if ($s -match '[\s"]') { '"' + ($s -replace '"', '\"') + '"' } else { $s } }
$head = @('-B', $script, '--agent', $Agent, '--min-gap', "$MinGap", '--log', $log)
if ($BridgeUrl) { $head += @('--bridge-url', $BridgeUrl) }
if ($Impersonate) { $head += @('--impersonate', $Impersonate) }
# --dry-run goes before the wake args: after '--' it would be part of the wake command.
$wakeHead = @(); $wakeTail = @()
if ($WakeArgs -and $WakeArgs[0] -eq '--') { $wakeTail = $WakeArgs } else { $wakeHead = $WakeArgs }
$argLine = ((@($head) + @($wakeHead) + @($wakeTail)) | ForEach-Object { Quote $_ }) -join ' '

if ($DryRun) {
  Write-Output "DRY RUN: would register $name"
  Write-Output "  trigger : at logon of $env:USERNAME"
  Write-Output "  settings: hidden, no time limit, one instance, restart every 5 min on failure (up to 999 times)"
  Write-Output "  action  : $Python $argLine"
  $console = $Python -replace 'pythonw\.exe$', 'python.exe'
  $check = @($head | Where-Object { $_ -ne '--log' -and $_ -ne $log }) + @($wakeHead) + @('--dry-run') + @($wakeTail)
  & $console @check
  Write-Output "  bridge check exit: $LASTEXITCODE (0 ready; 3 the bridge env is missing or not bound to this agent)"
  return
}

$action = New-ScheduledTaskAction -Execute $Python -Argument $argLine -WorkingDirectory $ScriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
  -Description "Wakes $Agent when an entry lands in its Redis lane (scripts/redis_ring.py)" -Force | Out-Null
Write-Output "registered $name at logon; log $log"
Write-Output "$Python $argLine"
