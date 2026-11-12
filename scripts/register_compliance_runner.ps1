# Registers the Windows scheduled task "SFDC24-ComplianceRunner": scripts/compliance_runner.py every 15 minutes,
# as the current user, only while that user is logged on (no stored password).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_compliance_runner.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_compliance_runner.ps1 -Python C:\Python314\python.exe
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_compliance_runner.ps1 -Unregister
#
# The runner reads the bus credentials the way scripts/bus.py does (<repo>\.env, or BLACKBOARD_ENV), so register
# it from the checkout that holds the .env. Its state and log live in %USERPROFILE%\.claude\compliance-runner.
param(
    [string]$Python = "C:\Python314\python.exe",
    [string]$Script = (Join-Path $PSScriptRoot "compliance_runner.py"),
    [string]$TaskName = "SFDC24-ComplianceRunner",
    [int]$EveryMinutes = 15,
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "UNREGISTERED $TaskName"
    return
}
if (-not (Test-Path $Python)) { throw "python not found: $Python" }
if (-not (Test-Path $Script)) { throw "runner not found: $Script" }

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$workDir = Split-Path -Parent $Script
$action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}"' -f $Script) -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 14)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "SFDC24 fleet compliance runner: rings rule gaps on TH-COMPLIANCE every $EveryMinutes min" `
    -Force | Out-Null
Write-Output "REGISTERED $TaskName as $user every $EveryMinutes min: $Python $Script"
