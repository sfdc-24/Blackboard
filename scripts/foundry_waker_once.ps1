#Requires -Version 5.1
# Scheduled entrypoint for the Foundry doorbell.
#
# --max 3 is the spend control: each answered row is one Foundry call, so a
# pass costs at most three. At a 15 minute repeat that is a ceiling of 12 calls
# an hour even if the board floods, and the dedupe by BCB id means six re-asks
# of one question still only cost one.
#
# 15 minutes matches SFDC24-GrokWaInbox on purpose: a WhatsApp beginning
# "Foundry" lands on the board through that poller, and this one answers it on
# the next tick. That pairing is what Mr Salam asked for on 2026-09-19 - one
# short prefix that gets an API-driven response at any hour without messaging
# anyone personally.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& C:\Python314\python.exe (Join-Path $PSScriptRoot 'foundry_waker.py') --max 3 @args
exit $LASTEXITCODE
