#Requires -Version 5.1
# Free scheduled entrypoint — no Grok Bot wake, no LLM.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
# --ack is NOT optional here. Without it this task read Mr Salam's WhatsApp
# every 15 minutes, wrote "acked": false to the log, and sent nothing back -
# which is what "no one is responding to my whatsapp messages" was. The ack is
# a free-form Meta send with no model call, so it costs nothing to leave on.
& C:\Python314\python.exe (Join-Path $PSScriptRoot 'grok_wa_inbox.py') once --ack @args
exit $LASTEXITCODE
