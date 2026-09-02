#Requires -Version 5.1
<#
Register the Glasses Intake capture loop as a Windows scheduled task.

claude-code-cli, 2026-09-02. wf=GLASSES-INTAKE sub=WEBCAM-CAPTURE.

WHY A SCRIPT RATHER THAN A PASTED schtasks LINE
  The raw command nests three levels of quoting (cmd around the task's /TR,
  around the interpreter path, around the script path). Getting one wrong
  registers a task that looks fine in the UI and fails silently at run time
  with result 0x1. Register-ScheduledTask takes the arguments as data, so
  there is nothing to escape.

  The task runs the script's OWN loop rather than a per-wake schedule: Task
  Scheduler's minimum repeat is 1 minute, and each wake pays ~1.5s of Python
  and OpenCV import cost. The loop also self-throttles -- a wake that uploads
  three screens takes ~30s, so it can never build a backlog.

USAGE (from anywhere)
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Start
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove

STOPPING IT LATER
  schtasks /End    /TN "SFDC24 Glasses Intake"
  schtasks /Change /TN "SFDC24 Glasses Intake" /DISABLE
#>
param(
  [switch]$Start,       # start it immediately instead of waiting for next logon
  [switch]$Remove,      # unregister and exit
  [int]$Interval = 30,  # seconds between wakes
  [int]$MaxAgeMin = 60, # retention, both local and Drive
  [switch]$NoUpload     # capture locally only; do not send to Drive
)

$ErrorActionPreference = 'Stop'
$TaskName = 'SFDC24 Glasses Intake'

if ($Remove) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output "removed scheduled task: $TaskName"
  return
}

# Resolve the interpreter and script by looking, not by assuming a path.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python not found on PATH" }
$script = Join-Path $PSScriptRoot 'glasses_capture.py'
if (-not (Test-Path -LiteralPath $script)) { throw "capture script not found: $script" }

$argList = @(
  "`"$script`""
  '--loop'
  '--interval'; "$Interval"
  '--keep'; '0'
  '--max-age-min'; "$MaxAgeMin"
)
if (-not $NoUpload) {
  $argList += @('--upload'; 'bus'; '--drive-max-age-min'; "$MaxAgeMin")
}

$action = New-ScheduledTaskAction -Execute $python -Argument ($argList -join ' ') `
          -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Interactive-only by design: it captures the logged-on desktop, so it must run
# in that session. RunLevel stays Limited -- nothing here needs admin.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
             -LogonType Interactive -RunLevel Limited
# Defaults would stop the task on battery and kill it after 3 days. Neither is
# wanted for something meant to run whenever the machine is in use.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

Write-Output "registered: $TaskName"
Write-Output ("  runs   : {0} {1}" -f $python, ($argList -join ' '))
Write-Output  "  trigger: at logon"

if ($Start) {
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 2
  $info = Get-ScheduledTask -TaskName $TaskName
  Write-Output ("  state  : {0}" -f $info.State)
  Write-Output "Frames should appear in data\glasses_intake within ~$Interval seconds."
  Write-Output "Verify by READ-BACK, not by this message: refresh the Drive folder."
} else {
  Write-Output "Not started. Run with -Start, or: schtasks /Run /TN `"$TaskName`""
}
