#Requires -Version 5.1
<#
Registers (or repairs) the scheduled doorbells for the API-only agents.

  .\register_agent_wakers.ps1            # register/repair foundry + gemini
  .\register_agent_wakers.ps1 -WhatIf    # show what would change

WHY THIS FILE EXISTS
The first registration on 2026-09-19 was done by hand with a ONE-TIME trigger
(MSFT_TaskTimeTrigger) carrying a 15-minute repetition of PT23H50M. A one-time
trigger is spent once its repetition window closes, so both wakers fired for
23h50m and then stopped dead: last Foundry run 2026-09-19 23:50, last Gemini run
2026-09-19 23:57, NextRunTime empty on both, and the agents were silent again by
morning - the same silence the doorbells were built to end.

A DAILY trigger re-arms every day. That is the shape already proven on this box
by "SFDC24 Blackboard Waker" (daily at 00:07, repeat PT23M for PT23H50M), and it
is the shape this script enforces. Running it again is safe: it overwrites the
triggers of an existing task rather than deleting and recreating it.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string[]]$Agent = @('foundry', 'gemini', 'grok', 'claude-api'),
  [int]$IntervalMinutes = 15
)
$ErrorActionPreference = 'Stop'

$runner = Join-Path $PSScriptRoot 'agent_waker_once.ps1'
if (-not (Test-Path $runner)) { throw "runner not found: $runner" }

# Stagger the two agents so they do not hit the board in the same second.
$startAt = @{ foundry = '00:05'; gemini = '00:12'; grok = '00:19'; 'claude-api' = '00:26' }

foreach ($a in $Agent) {
  $name = 'SFDC24-' + ((Get-Culture).TextInfo.ToTitleCase($a)) + 'Waker'
  $at = [datetime]::Today.Add([timespan]::Parse($startAt[$a]))

  # A daily trigger re-arms every day; the repetition covers the day itself.
  $trigger = New-ScheduledTaskTrigger -Daily -At $at
  # PS 5.1 cannot assign Repetition sub-properties directly - borrow the whole
  # object from a throwaway -Once trigger, which is the documented workaround.
  $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $at `
      -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
      -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 50)).Repetition

  $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Agent {1}' -f $runner, $a)
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

  if ($PSCmdlet.ShouldProcess($name, 'register daily doorbell')) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
      Set-ScheduledTask -TaskName $name -Trigger $trigger -Action $action -Settings $settings | Out-Null
      Write-Output "repaired $name"
    }
    else {
      Register-ScheduledTask -TaskName $name -Trigger $trigger -Action $action -Settings $settings `
        -Description "Doorbell for the $a agent: answers board rows addressed to it." | Out-Null
      Write-Output "registered $name"
    }
    $info = Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo
    $tr = (Get-ScheduledTask -TaskName $name).Triggers[0]
    Write-Output ('  {0}  repeat={1}/{2}  next={3}' -f $tr.CimClass.CimClassName, $tr.Repetition.Interval, $tr.Repetition.Duration, $info.NextRunTime)
  }
}
