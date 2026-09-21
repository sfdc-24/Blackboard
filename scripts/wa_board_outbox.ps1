#Requires -Version 5.1
<#
SFDC24 — laptop entrypoint for the Blackboard → WhatsApp outbox.

Agents post phase=WA_SEND (or WA_OUT|) on the board. This task, running on
the host that holds META_TOKEN, is what actually calls wa_notify.ps1.
Credentials stay in the laptop .env. They are never argv, never Drive,
never Apps Script (doctrine D-18). Recipient stays WA_TO.

  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\wa_board_outbox.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\wa_board_outbox.ps1 --dry-run
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$candidates = @(
  (Join-Path $root '.venv\Scripts\python.exe'),
  'C:\Python314\python.exe',
  'C:\Python313\python.exe',
  'python.exe',
  'python3'
)
$py = $null
foreach ($c in $candidates) {
  try {
    $null = & $c -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
  } catch { }
}
if (-not $py) { throw "python not found — install it on the laptop or put it on PATH" }

& $py (Join-Path $PSScriptRoot 'wa_board_outbox.py') once @args
exit $LASTEXITCODE
