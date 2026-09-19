#Requires -Version 5.1
<#
Scheduled entrypoint for an API-only agent's doorbell.

  .\agent_waker_once.ps1 -Agent foundry
  .\agent_waker_once.ps1 -Agent gemini

--max 3 is the spend control: each answered row is one model call, so a pass
costs at most three. At a 15 minute repeat that is a ceiling of 12 calls an hour
per agent even if the board floods, and the dedupe by BCB id means six re-asks
of one question still only cost one.

15 minutes matches SFDC24-GrokWaInbox on purpose: a WhatsApp beginning with the
agent's name lands on the board through that poller, and this one answers it on
the next tick. That pairing is what Mr Salam asked for on 2026-09-19 - one short
prefix that gets an API-driven response at any hour without messaging anyone
personally.
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('foundry', 'gemini')]
  [string]$Agent,

  [int]$Max = 3
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& C:\Python314\python.exe (Join-Path $PSScriptRoot 'agent_waker.py') --agent $Agent --max $Max
exit $LASTEXITCODE
