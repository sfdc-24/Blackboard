# Run a script on the presenter box WITHOUT inlining it into an ssh argument.
#
# WHY THIS EXISTS
#   On 2026-09-16 eight separate commands died in transit to that box, every one
#   of them a quoting failure rather than a logic error:
#
#     case "$n" in ...        -> bash: syntax error near unexpected token `('
#     grep -E "a|b"           -> the | became a shell pipe; bash ran `passcode`
#     printf "  %-12s "       -> lost its format string, ran as a command
#     tr "\n" " "             -> tr: missing operand after 'n'
#     cut -d" " -f5           -> cut: the delimiter must be a single character
#     set -- $p (from "x y")  -> xdotool: expected 2 coordinates, got 1
#     curl -w "%{http_code}"  -> printed the literal %http_code
#     \"escaped quotes\"      -> killed the whole script
#
#   Each cost a round trip, and two of them printed cheerful success lines while
#   doing nothing, which is worse than failing loudly.
#
#   The cause is always the same: the command is a STRING that passes through
#   PowerShell, then ssh, then the remote shell, and each layer eats a quote.
#   Shipping a FILE has no such layers. The bytes that arrive are the bytes that
#   were written.
#
# USAGE
#   . .\rx.ps1
#   rx 'echo hello; xdotool getmouselocation'          # returns stdout as text
#   rx -Script $body -Raw                              # unfiltered output
#
# Every quirk below is one of the failures above, made impossible.

$script:RX_IP  = '34.74.36.174'
$script:RX_KEY = 'C:\Users\salam\.ssh\google_compute_engine'
$script:RX_SSH = 'C:\Program Files\Git\usr\bin\ssh.exe'
$script:RX_SCP = 'C:\Program Files\Git\usr\bin\scp.exe'

function rx {
  param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Script,
    [switch]$Raw,
    [int]$TimeoutSec = 120
  )

  $id = [Guid]::NewGuid().ToString('N').Substring(0, 12)
  $local = Join-Path $env:TEMP ("rx_$id.sh")

  # The display exports every remote command needs. Put them in the FILE so no
  # caller has to remember them and no caller has to quote them.
  $preamble = @'
#!/bin/bash
export DISPLAY=:99
export XDG_RUNTIME_DIR=/run/user/1001
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1001/bus"
'@

  # LF line endings, no BOM. A CRLF script fails on bash with an unreadable
  # "\r: command not found", and a BOM once made Zoom silently ignore a URL.
  $body = ($preamble + "`n" + $Script + "`n") -replace "`r`n", "`n"
  [IO.File]::WriteAllText($local, $body, (New-Object Text.UTF8Encoding($false)))

  $remote = "/tmp/rx_$id.sh"
  & $script:RX_SCP -q -i $script:RX_KEY -o StrictHostKeyChecking=accept-new $local ("user@" + $script:RX_IP + ":" + $remote) 2>&1 | Out-Null

  $out = & $script:RX_SSH -i $script:RX_KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 ("user@" + $script:RX_IP) "bash $remote" 2>&1 | Out-String

  if ($Raw) { return $out }

  # ssh writes its own noise to stderr and PowerShell renders native stderr as
  # error records, which buried real output all night. Strip only that.
  $keep = @()
  foreach ($line in ($out -split "`r?`n")) {
    if (-not $line.Trim()) { continue }
    if ($line -match 'Warning: Permanently added') { continue }
    if ($line -match 'CategoryInfo|FullyQualifiedErrorId|NativeCommandError') { continue }
    if ($line -match '^\s*\+ ') { continue }
    if ($line -match '^At line:\d+ char:\d+') { continue }
    $keep += $line.TrimEnd()
  }
  return ($keep -join "`n")
}

# Move the pointer in small steps so a viewer can follow it, instead of the
# teleport xdotool does by default. He said: "I should be able to see you're
# moving your mouse."
function rx-glide {
  param([int]$ToX, [int]$ToY, [int]$Steps = 24, [double]$PauseMs = 16)
  $s = @"
read -r sx sy < <(xdotool getmouselocation --shell | awk -F= '/^X=/{x=`$2} /^Y=/{y=`$2} END{print x, y}')
steps=$Steps
for i in `$(seq 1 `$steps); do
  nx=`$(( sx + (($ToX - sx) * i) / steps ))
  ny=`$(( sy + (($ToY - sy) * i) / steps ))
  xdotool mousemove `$nx `$ny
  sleep $([Math]::Round($PauseMs / 1000, 3))
done
xdotool getmouselocation
"@
  rx $s
}
