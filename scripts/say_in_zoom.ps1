#Requires -Version 5.1
<#
Speak into the live Zoom meeting in a NEURAL voice, synthesised on the laptop.

WHY THIS EXISTS
  presenter_say.sh used espeak-ng, a formant synthesiser. It is intelligible, it
  needs no network and no key, and it sounds like a machine. Mr Salam, hearing it
  live in a meeting on 2026-09-12: "robotic and very unpleasant". In front of a
  prospect that is not a rough edge, it is the entire impression.

  The good voice already existed and had already been chosen. wa_voice_reply.ps1
  has been speaking to him on WhatsApp in OpenAI's "nova" since 2026-09-08,
  picked by ear from a shortlist of six. The presenter box simply never got it.

WHY THE SYNTHESIS HAPPENS HERE AND NOT ON THE BOX
  Doing it there means putting an OpenAI key on a disposable machine that gets
  destroyed and rebuilt, whose Zoom client writes its own launch URL into a log,
  and which exists to be thrown away. A WAV is not a credential. An API key is.
  The laptop makes the sound; the box only plays it.

WHAT IS DELIBERATELY UNCHANGED
  Everything that made presenter_say.sh trustworthy. It still refuses when no
  ZOOM capture stream is bound to vmic_src, still reports bytes rather than
  success, and still states that reaching Zoom's input is not proof a human heard
  anything. Only the source of the audio moved.

D-18: OPENAI_API_KEY is read from .env at run time. It never reaches a command
line, a log, or the box.

USAGE
  .\say_in_zoom.ps1 -Ip 34.73.38.85 -Text "hello"
  .\say_in_zoom.ps1 -Ip 34.73.38.85 -TextFile line.txt
  .\say_in_zoom.ps1 -Text "sample" -KeepLocal out.wav -NoPlay      # no box needed
#>
param(
  [string]$Ip,
  [string]$Text,
  [string]$TextFile,
  # Chosen by ear on 2026-09-08 from all six of the original shortlist. Do not
  # change it on a whim: he picked it, and one consistent voice is part of what
  # makes this feel like a colleague rather than a tool.
  [ValidateSet('alloy','ash','ballad','coral','echo','fable','nova','onyx','sage','shimmer','verse')]
  [string]$Voice = 'nova',
  [ValidateSet('tts-1','tts-1-hd','gpt-4o-mini-tts')]
  [string]$Model = 'gpt-4o-mini-tts',
  # Only gpt-4o-mini-tts honours this. It steers delivery, not words.
  [string]$Direction = 'Energetic, engaged colleague in a live debate. Clear conviction, natural emphasis, vary pace. Sound interested and human - never flat, never monotone, never robotic.',
  [ValidateRange(1, 3500)][int]$MaxChars = 1200,
  [string]$SshKey = 'C:\Users\salam\.ssh\google_compute_engine',
  [string]$EnvFile = 'C:\Users\salam\Quantum\Blackboard\.env',
  [string]$KeepLocal,
  [switch]$NoPlay
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- find ssh and scp, and say so when they are not there --------------------
# THIS BOX'S WINDOWS OpenSSH INSTALL HAS NO ssh.exe. C:\Windows\System32\OpenSSH
# contains scp.exe, sshd.exe, ssh-keygen.exe and friends, but not the client - so
# `ssh` is not a command in PowerShell, and scp fails with
#   "CreateProcessW failed error:2 / posix_spawn: No such file or directory"
# which reads like a missing FILE and is actually scp failing to spawn its own
# transport. Git Bash works only because Git ships its own at usr\bin.
# RESOLVE THEM AS A PAIR, from one directory. scp shells out to its SIBLING ssh,
# so a scp found on PATH next to a missing ssh is worse than no scp at all: it
# resolves, it runs, and it dies with a message about a file not existing. The
# first version of this preferred Get-Command, found System32's scp, and failed
# exactly that way AFTER paying for the synthesis.
$pair = $null
$dirs = @("C:\Program Files\Git\usr\bin", "C:\Program Files\Git\bin", "C:\Windows\System32\OpenSSH")
$fromPath = (Get-Command ssh -ErrorAction SilentlyContinue)
if ($fromPath) { $dirs = @((Split-Path -Parent $fromPath.Source)) + $dirs }
foreach ($d in $dirs) {
  if ((Test-Path -LiteralPath (Join-Path $d 'ssh.exe')) -and (Test-Path -LiteralPath (Join-Path $d 'scp.exe'))) {
    $pair = $d; break
  }
}
if (-not $pair) {
  throw "no directory holds BOTH ssh.exe and scp.exe. Checked: $($dirs -join '; '). Windows' OpenSSH here ships scp without the client."
}
$SSH = Join-Path $pair 'ssh.exe'
$SCP = Join-Path $pair 'scp.exe'
Write-Host "ssh/scp from $pair"

if (-not $Text -and -not $TextFile) { throw "pass -Text or -TextFile" }
if (-not $NoPlay -and -not $Ip)     { throw "pass -Ip, or -NoPlay to only synthesise" }
if ($TextFile) {
  if (-not (Test-Path -LiteralPath $TextFile)) { throw "text file not found: $TextFile" }
  $Text = (Get-Content -LiteralPath $TextFile -Raw)
}
$Text = $Text.Trim()
if (-not $Text) { throw "refusing to speak an empty string" }
if ($Text.Length -gt $MaxChars) {
  throw "text is $($Text.Length) chars, over the $MaxChars bound - say it in shorter pieces; it also listens better that way"
}

$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
if (-not $cfg.ContainsKey('OPENAI_API_KEY') -or -not $cfg['OPENAI_API_KEY']) {
  throw "OPENAI_API_KEY missing in $EnvFile"
}

# ---- synthesise --------------------------------------------------------------
# WAV on purpose: paplay reads it natively, nothing on the box has to decode, and
# a truncated file is detectable from its header instead of by playing silence.
$payload = @{ model = $Model; voice = $Voice; input = $Text; response_format = 'wav' }
if ($Model -eq 'gpt-4o-mini-tts' -and $Direction) { $payload['instructions'] = $Direction }
$body = $payload | ConvertTo-Json -Depth 6 -Compress

$wav = if ($KeepLocal) { $KeepLocal }
       else { Join-Path ([IO.Path]::GetTempPath()) ("say_" + [guid]::NewGuid().ToString('N') + ".wav") }

$req = [Net.HttpWebRequest]::Create('https://api.openai.com/v1/audio/speech')
$req.Method = 'POST'
$req.ContentType = 'application/json'
$req.Headers.Add('Authorization', 'Bearer ' + $cfg['OPENAI_API_KEY'])
$req.Timeout = 120000
$bytes = [Text.Encoding]::UTF8.GetBytes($body)
$req.ContentLength = $bytes.Length
$s = $req.GetRequestStream(); try { $s.Write($bytes, 0, $bytes.Length) } finally { $s.Dispose() }
try {
  $resp = $req.GetResponse()
  $fs = [IO.File]::Create($wav)
  try { $resp.GetResponseStream().CopyTo($fs) } finally { $fs.Dispose(); $resp.Dispose() }
} catch [Net.WebException] {
  $er = $_.Exception.Response
  if ($er) {
    $sr = New-Object IO.StreamReader($er.GetResponseStream())
    try { $eb = $sr.ReadToEnd() } finally { $sr.Dispose() }
    throw "OpenAI refused: HTTP $([int]$er.StatusCode) $eb"
  }
  throw
}

# ASSERT the recording rather than trusting the 200. An error body written to a
# .wav would otherwise ship happily and play as silence, and paplay would return
# 0 - a silent success, the exact failure shape this rig keeps meeting.
$size = (Get-Item -LiteralPath $wav).Length
$head = [IO.File]::ReadAllBytes($wav)
if ($head.Length -lt 4) { throw "synthesis returned $size bytes" }
$first4 = [Text.Encoding]::ASCII.GetString($head, 0, 4)
if ($first4 -ne 'RIFF') { throw "synthesis returned $size bytes not starting with RIFF - that is not a WAV" }
if ($size -lt 2000)     { throw "synthesis returned only $size bytes - refusing to ship that" }
Write-Host ("synthesised {0:N0} bytes of WAV   voice={1}  model={2}" -f $size, $Voice, $Model)

if ($NoPlay) { Write-Host "NoPlay - nothing shipped, nothing played. File: $wav"; exit 0 }

# ---- ship and play -----------------------------------------------------------
$remote = "/tmp/say_$([guid]::NewGuid().ToString('N')).wav"
& $SCP -q -i $SshKey -o StrictHostKeyChecking=accept-new $wav "user@${Ip}:$remote"
if ($LASTEXITCODE -ne 0) { throw "scp failed with exit $LASTEXITCODE" }

# Read the size back off the far end. A half-shipped WAV still has a RIFF header,
# so the header check above cannot catch truncation; only the byte count can.
$remoteSize = (& $SSH -i $SshKey -o StrictHostKeyChecking=accept-new "user@$Ip" "stat -c %s $remote 2>/dev/null || echo 0" | Select-Object -First 1)
if ("$remoteSize".Trim() -ne "$size") {
  & $SSH -i $SshKey "user@$Ip" "rm -f $remote" 2>$null | Out-Null
  throw "shipped $size bytes but the box reports $remoteSize - truncated, refusing to play it"
}
Write-Host "shipped $remoteSize bytes, byte-identical on the box"

# presenter_play.sh owns the Zoom binding check, the mute guard, and the refusal
# to claim it spoke. This only hands it a better recording.
#
# IT USED TO SAY presenter_say.sh, AND THAT NAME IS NOW GONE ON PURPOSE.
# presenter_say.sh held two jobs: espeak synthesis, and playback with all the
# guards. It was deleted on 2026-09-18 to remove the robotic voice, and the
# voice went down with it - this line failed with exit 127 into a live meeting.
# The guards moved to presenter_play.sh verbatim; only the synthesiser was left
# behind. A file that does one thing cannot be half-deleted.
& $SSH -i $SshKey -o StrictHostKeyChecking=accept-new "user@$Ip" "PRESENTER_WAV=$remote bash /home/user/presenter_play.sh; rc=`$?; rm -f $remote; exit `$rc"
$rc = $LASTEXITCODE
if (-not $KeepLocal) { Remove-Item -LiteralPath $wav -Force -ErrorAction SilentlyContinue }
if ($rc -ne 0) { Write-Host "presenter_play.sh returned $rc - it refused, or Zoom is not bound to the virtual mic"; exit $rc }
exit 0
