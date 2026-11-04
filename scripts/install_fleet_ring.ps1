<#
Registers the ringing doorbell (scripts/fleet_ring.py) as a Windows scheduled task that runs every 2 minutes, hidden.

  Codex, on the laptop that runs the Codex desktop app (its session wakes on `codex queue`):
    powershell -File scripts\install_fleet_ring.ps1 -Agent codex -EnvFile C:\path\.env -RingArgs '--codex-thread','<uuid>'
  Grok, on Grok's own computer (any command that starts a Grok turn; {message} or {file} is filled in):
    powershell -File scripts\install_fleet_ring.ps1 -Agent grok -EnvFile C:\path\.env -RingArgs '--','grok','-p','{message}'
  Remove:
    powershell -File scripts\install_fleet_ring.ps1 -Agent codex -Remove

The task runs pythonw (no console window) and appends one line per event to ~\.fleet-ring-<agent>.log.
The first run only primes the bookmark; rows after that ring. No secret or thread id is stored in the repository.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('codex', 'grok', 'gemini')][string]$Agent,
  [string]$EnvFile,
  [string[]]$RingArgs = @(),
  [string]$ScriptDir = $PSScriptRoot,
  [string]$Python = '',
  [int]$EveryMinutes = 2,
  [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$name = "SFDC24-FleetRing-$Agent"
if ($Remove) {
  Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output "removed $name"
  return
}
if (-not $EnvFile -or -not (Test-Path $EnvFile)) { throw "-EnvFile must name the board .env this agent already uses" }
if (-not $RingArgs) { throw "-RingArgs is required: '--codex-thread','<uuid>' or '--','<command>','{message}'" }
if (-not $Python) {
  $Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
  if (-not $Python) { throw "pythonw.exe not found; pass -Python" }
}
$script = Join-Path $ScriptDir 'fleet_ring.py'
if (-not (Test-Path $script)) { throw "missing $script" }
$log = Join-Path $HOME ".fleet-ring-$Agent.log"

function Quote([string]$s) { if ($s -match '[\s"]') { '"' + ($s -replace '"', '\"') + '"' } else { $s } }
$before = @('-B', $script, '--agent', $Agent, '--env', $EnvFile, '--log', $log)
$all = @($before) + @($RingArgs)
$argLine = ($all | ForEach-Object { Quote $_ }) -join ' '

$action = New-ScheduledTaskAction -Execute $Python -Argument $argLine -WorkingDirectory $ScriptDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
  -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
  -Description "Rings $Agent when Blackboard rows arrive for it (scripts/fleet_ring.py)" -Force | Out-Null
Write-Output "registered $name every $EveryMinutes min; log $log"
Write-Output "$Python $argLine"
