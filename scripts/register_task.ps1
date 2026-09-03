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
  [switch]$NoUpload,    # capture locally only; do not send to Drive
  # 'board' composites every screen plus the room into ONE situation board.
  # Besides reading better, it is a single upload per wake instead of four,
  # which is what makes a faster cadence affordable against Apps Script quota.
  [ValidateSet('board', 'frames')]
  [string]$Layout = 'board',
  [switch]$NoRoom       # board from screens only, omitting the room camera
)

$ErrorActionPreference = 'Stop'
$TaskName = 'SFDC24 Glasses Intake'

if ($Remove) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'SFDC24 Glasses Intake.lnk'
  if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force }
  # Kill any loop still running from this session.
  Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*glasses_capture.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Write-Output "removed: scheduled task, startup shortcut, and any running capture loop"
  return
}

# Resolve the interpreter and script by looking, not by assuming a path.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python not found on PATH" }
$script = Join-Path $PSScriptRoot 'glasses_capture.py'
if (-not (Test-Path -LiteralPath $script)) { throw "capture script not found: $script" }

# pythonw.exe is python without a console window. A capture loop that parks a
# black console on the desktop would also park it in every screenshot it takes.
$runner = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $runner)) { $runner = $python }

$argList = @(
  "`"$script`""
  '--loop'
  '--interval'; "$Interval"
  '--layout'; $Layout
  '--keep'; '0'
  '--max-age-min'; "$MaxAgeMin"
)
if ($NoRoom) { $argList += '--no-room' }
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

# A logon-triggered task needs administrator rights; a non-elevated shell gets
# "Access is denied" (0x80070005). Rather than demand an admin prompt, fall back
# to a Startup-folder shortcut, which achieves the same thing -- start at logon,
# run in the user's own session -- with no elevation at all. The scheduled task
# is still preferred when available: it restarts on failure and survives battery
# transitions, which a shortcut cannot.
$mechanism = $null
try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
  $mechanism = 'scheduled-task'
} catch {
  Write-Output "Task Scheduler refused ($($_.Exception.Message.Trim())) -- falling back to a Startup shortcut."
  $startup = [Environment]::GetFolderPath('Startup')
  $lnkPath = Join-Path $startup 'SFDC24 Glasses Intake.lnk'
  $shell = New-Object -ComObject WScript.Shell
  $lnk = $shell.CreateShortcut($lnkPath)
  # pythonw runs without a console window; plain python would leave one open.
  $lnk.TargetPath = $runner
  $lnk.Arguments = ($argList -join ' ')
  $lnk.WorkingDirectory = (Split-Path -Parent $PSScriptRoot)
  $lnk.WindowStyle = 7   # minimised
  $lnk.Description = 'SFDC24 Glasses Intake capture loop'
  $lnk.Save()
  $mechanism = "startup-shortcut ($lnkPath)"
}

Write-Output "registered via: $mechanism"
Write-Output ("  runs   : {0} {1}" -f $runner, ($argList -join ' '))
Write-Output  "  trigger: at logon"

if ($Start) {
  # Kill any loop already running before starting another. Without this, a
  # second -Start silently leaves TWO loops uploading, doubling the frame rate,
  # the storage and the quota burn -- and the duplicates look exactly like
  # correct output, so nothing would flag it.
  $existing = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
                Where-Object { $_.CommandLine -like '*glasses_capture.py*--loop*' })
  foreach ($e in $existing) {
    Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output ("  stopped existing loop, pid {0}" -f $e.ProcessId)
  }

  # Start it now regardless of mechanism -- neither trigger fires until the next
  # logon, and waiting for that to find out whether it works is a bad trade.
  $proc = Start-Process -FilePath $runner -ArgumentList $argList `
          -WorkingDirectory (Split-Path -Parent $PSScriptRoot) `
          -WindowStyle Hidden -PassThru
  Start-Sleep -Seconds 3
  if ($proc.HasExited) {
    Write-Output ("  STARTED BUT EXITED immediately, code {0} -- run the capture once by hand to see why:" -f $proc.ExitCode)
    Write-Output ("  {0} `"{1}`" --once --layout {2}" -f $python, $script, $Layout)
  } else {
    Write-Output ("  state  : running, pid {0}" -f $proc.Id)
    Write-Output "Frames should appear in data\glasses_intake within ~$Interval seconds."
    Write-Output "Verify by READ-BACK, not by this message: refresh the Drive folder."
    Write-Output ("To stop it:  Stop-Process -Id {0}" -f $proc.Id)
  }
} else {
  Write-Output "Not started. Re-run with -Start."
}
