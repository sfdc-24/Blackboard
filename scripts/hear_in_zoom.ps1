#Requires -Version 5.1
<#
Listen to the meeting and turn it into text.

WHY
  The rig has been able to speak and share for two days and has never been able
  to hear. Mr Salam sat in a Zoom call with it, spoke, got no response, and had
  to type on WhatsApp: "Why do I have to message in whatsapp while being on a
  zoom call with you?" Because nothing was listening. This is the missing half.

THE PATH
  far end -> Zoom -> sink 'zspk' -> zspk.monitor -> parec on the box
          -> WAV pulled here -> Groq whisper-large-v3 -> text

  zspk exists already: Zoom's speaker was put on its own sink so Zoom would not
  hear itself through the virtual microphone. Everything the far end says has
  been landing there the whole time.

WHY THE TRANSCRIPTION HAPPENS HERE
  Same reason the speech synthesis does. The box is disposable and gets
  destroyed; a key does not belong on it. Audio is not a credential.

-SelfTest PROVES THE PATH WITHOUT A HUMAN
  It speaks a known sentence into Zoom's SPEAKER sink (not the microphone),
  records the monitor, transcribes it, and checks the words come back. A hearing
  test that needs someone to volunteer to talk is a test that never gets run.

D-18: GROQ_API_KEY is read from .env and passed to curl through a config file,
never on a command line where it would sit in a process list.
#>
param(
  [string]$Ip,
  [ValidateRange(3, 120)][int]$Seconds = 5,
  [string]$SshKey = 'C:\Users\salam\.ssh\google_compute_engine',
  [string]$EnvFile = 'C:\Users\salam\Quantum\Blackboard\.env',
  [string]$Model = 'whisper-large-v3',
  [switch]$SelfTest,
  [switch]$KeepWav
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $Ip) { throw "pass -Ip" }

# ssh and scp must come from ONE directory - scp shells out to its sibling, and
# this machine's System32 OpenSSH has scp without ssh.
$pair = $null
foreach ($d in @("C:\Program Files\Git\usr\bin", "C:\Program Files\Git\bin", "C:\Windows\System32\OpenSSH")) {
  if ((Test-Path -LiteralPath (Join-Path $d 'ssh.exe')) -and (Test-Path -LiteralPath (Join-Path $d 'scp.exe'))) { $pair = $d; break }
}
if (-not $pair) { throw "no directory holds both ssh.exe and scp.exe" }
$SSH = Join-Path $pair 'ssh.exe'
$SCP = Join-Path $pair 'scp.exe'

$curl = "$env:SystemRoot\System32\curl.exe"
if (-not (Test-Path -LiteralPath $curl)) { $c = Get-Command curl.exe -ErrorAction SilentlyContinue; if ($c) { $curl = $c.Source } else { throw "curl.exe not found" } }

$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'") }
}
if (-not $cfg.ContainsKey('GROQ_API_KEY') -or -not $cfg['GROQ_API_KEY']) { throw "GROQ_API_KEY missing in $EnvFile" }

$PHRASE = 'the quick brown fox jumps over the lazy dog'

# ---- optional: speak into the SPEAKER sink so there is something to hear ------
if ($SelfTest) {
  Write-Host "SELF TEST - speaking a known sentence into Zoom's speaker sink, then listening to it"
  $say = Join-Path (Split-Path -Parent $PSCommandPath) 'say_in_zoom.ps1'
  if (-not (Test-Path -LiteralPath $say)) { throw "say_in_zoom.ps1 not found next to this script" }
  $tmpWav = Join-Path ([IO.Path]::GetTempPath()) ("selftest_" + [guid]::NewGuid().ToString('N') + ".wav")
  & $say -Text $PHRASE -KeepLocal $tmpWav -NoPlay | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "could not synthesise the self-test phrase" }
  $remoteWav = "/tmp/selftest.wav"
  & $SCP -q -i $SshKey -o StrictHostKeyChecking=accept-new $tmpWav "user@${Ip}:$remoteWav"
  if ($LASTEXITCODE -ne 0) { throw "scp of the self-test phrase failed" }
  Remove-Item -LiteralPath $tmpWav -Force -ErrorAction SilentlyContinue
  # Deliberately into zspk, NOT vmic: we are pretending to be the far end.
  Start-Job -Name selfspeak -ArgumentList $SSH, $SshKey, $Ip, $remoteWav -ScriptBlock {
    param($ssh, $key, $ip, $w)
    & $ssh -i $key "user@$ip" "sleep 2; paplay --device=zspk $w >/dev/null 2>&1"
  } | Out-Null
}

# ---- record ------------------------------------------------------------------
Write-Host "listening for ${Seconds}s ..."
# In self test the far end is paplay, not Zoom, so the "is Zoom feeding the
# speaker" gate must be waived - explicitly, and only here.
$req = if ($SelfTest) { "PRESENTER_REQUIRE_ZOOM=0 " } else { "" }
$listen = & $SSH -i $SshKey -o StrictHostKeyChecking=accept-new "user@$Ip" "${req}bash /home/user/presenter_listen.sh $Seconds /tmp/hear.wav"
$listenRc = $LASTEXITCODE
$listen | ForEach-Object { Write-Host "  $_" }

if ($SelfTest) { Get-Job -Name selfspeak -ErrorAction SilentlyContinue | Remove-Job -Force -ErrorAction SilentlyContinue }

if ($listenRc -eq 3) { Write-Host "NOT LISTENING: Zoom is not feeding the speaker sink. Fix the plumbing before trusting silence."; exit 3 }
if ($listenRc -ne 0) { Write-Host "recording failed"; exit $listenRc }

$verdict = ($listen | Where-Object { $_ -match '^VERDICT' }) -join ''
if ($verdict -match 'silence') {
  Write-Host "HEARD: nothing. The path is working; nobody spoke."
  if ($SelfTest) { Write-Host "SELF TEST FAILED - a phrase was played and the monitor heard silence."; exit 1 }
  exit 0
}

# ---- pull and transcribe -----------------------------------------------------
$local = Join-Path ([IO.Path]::GetTempPath()) ("hear_" + [guid]::NewGuid().ToString('N') + ".wav")
& $SCP -q -i $SshKey "user@${Ip}:/tmp/hear.wav" $local
if ($LASTEXITCODE -ne 0) { throw "could not pull the recording" }

# curl config file, so the key is never an argument in a process list.
$cc = Join-Path ([IO.Path]::GetTempPath()) ("gq_" + [guid]::NewGuid().ToString('N') + ".conf")
Set-Content -LiteralPath $cc -Value ('header = "Authorization: Bearer ' + $cfg['GROQ_API_KEY'] + '"') -Encoding ascii
try {
  $raw = & $curl -s -K $cc `
    -F "file=@$local;type=audio/wav" `
    -F "model=$Model" `
    -F "response_format=json" `
    -F "language=en" `
    https://api.groq.com/openai/v1/audio/transcriptions
} finally {
  Remove-Item -LiteralPath $cc -Force -ErrorAction SilentlyContinue
}
if (-not $KeepWav) { Remove-Item -LiteralPath $local -Force -ErrorAction SilentlyContinue }

$text = $null
try { $text = ($raw | ConvertFrom-Json).text } catch { }
if (-not $text) { Write-Host "transcription returned nothing usable:"; Write-Host $raw; exit 1 }
$text = $text.Trim()

Write-Host ""
Write-Host "HEARD: $text"

if ($SelfTest) {
  # Compare on words, not on an exact string: Whisper punctuates and capitalises.
  $norm = ($text.ToLower() -replace '[^a-z ]', ' ') -replace '\s+', ' '
  $hits = 0
  foreach ($w in @('quick','brown','fox','jumps','lazy','dog')) { if ($norm -match "\b$w\b") { $hits++ } }
  Write-Host ""
  Write-Host "SELF TEST: $hits of 6 key words came back"
  if ($hits -ge 5) { Write-Host "SELF TEST PASSED - the rig can hear."; exit 0 }
  Write-Host "SELF TEST FAILED - audio was present but the words did not survive."
  exit 1
}
exit 0
