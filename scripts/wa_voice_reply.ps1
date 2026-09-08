#Requires -Version 5.1
<#
Speak a reply to Mr. Salam on WhatsApp, as a playable voice note.
claude-code-cli, 2026-09-08. Asked for directly: "build voice and also respond in voice".

WHY THIS EXISTS SEPARATELY FROM wa_notify.ps1
  wa_notify.ps1 already knows how to upload media and send it, and its own header
  records the thing that makes this work at all: a recording sent `-As audio`
  arrives as a PLAYABLE VOICE NOTE rather than a file attachment. So this script
  does not re-implement any of that. It does exactly one new thing -- turn text
  into an Ogg Opus recording -- and then hands off.

WHY OGG OPUS AND NOT MP3
  WhatsApp will accept audio/mpeg, but Ogg Opus is the format its own voice notes
  use, so it renders with the waveform and inline player rather than as a generic
  audio file. OpenAI's `opus` response_format returns exactly that container.

DOCTRINE
  D-18: OPENAI_API_KEY is read from ..\.env at run time. It never appears on a
  command line, in a log line, or in the transcript. -DryRun does NOT call the
  provider and does NOT upload -- wa_notify learned that lesson the expensive way
  (its header records an upload that ran during a dry run and left a media object
  behind while reporting that nothing was sent), so the same rule is honoured here.

COST
  Speech is billed per character, unlike text. A 600-character reply is a fraction
  of a cent, but a habit of speaking long messages is not free. Keep spoken replies
  short; that is also better listening.

USAGE
  & .\scripts\wa_voice_reply.ps1 -Text "the intake page is fixed"
  & .\scripts\wa_voice_reply.ps1 -TextFile reply.txt -DryRun
  & .\scripts\wa_voice_reply.ps1 -Text "..." -AlsoText      # voice AND the words
#>
param(
  [string]$Text,
  [string]$TextFile,
  # He asked on 2026-09-08 for "female and one that sounds airy and smart like in
  # claude mobile", heard all six of the shortlist, and chose NOVA. The newer model
  # carries voices the original six do not -- sage and coral are the warm,
  # articulate ones -- so both models are selectable rather than the voice list
  # being silently limited by a model choice nobody stated.
  [ValidateSet('alloy', 'ash', 'ballad', 'coral', 'echo', 'fable',
               'nova', 'onyx', 'sage', 'shimmer', 'verse')]
  [string]$Voice = 'nova',
  [ValidateSet('tts-1', 'tts-1-hd', 'gpt-4o-mini-tts')]
  [string]$Model = 'gpt-4o-mini-tts',
  # Only gpt-4o-mini-tts honours this. It steers delivery, not words.
  [string]$Direction = 'Calm, warm and articulate. Unhurried. Never salesy.',
  [ValidateRange(1, 3500)]
  [int]$MaxChars = 1200,
  [switch]$AlsoText,
  [switch]$KeepFile,
  [switch]$DryRun,
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- the words ---------------------------------------------------------------
if ($TextFile) {
  if (-not (Test-Path -LiteralPath $TextFile)) { throw "text file not found: $TextFile" }
  $Text = Get-Content -LiteralPath $TextFile -Raw
}
if (-not $Text -or -not $Text.Trim()) { throw "nothing to say: pass -Text or -TextFile" }
$Text = $Text.Trim()

# Bound it before spending anything. Speech is billed per character, so an
# accidental paste of a whole document is a real cost, not a slow request.
if ($Text.Length -gt $MaxChars) {
  throw ("refusing to speak " + $Text.Length + " characters; the bound is " + $MaxChars +
         ". Shorten it, or raise -MaxChars deliberately. Long spoken messages are also worse to listen to.")
}

# ---- credentials, from the env file only (D-18) -------------------------------
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "env file not found: $EnvFile" }
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
if (-not $cfg.OPENAI_API_KEY) { throw "OPENAI_API_KEY missing in $EnvFile" }

$outFile = Join-Path ([IO.Path]::GetTempPath()) ('wa_voice_' + [guid]::NewGuid().ToString('N') + '.ogg')

if ($DryRun) {
  Write-Output "DRY RUN - no speech synthesised, no upload, no message sent"
  Write-Output ("voice     : " + $Voice + "  model: " + $Model)
  Write-Output ("chars     : " + $Text.Length + " of " + $MaxChars)
  Write-Output ("would say : " + $Text)
  return
}

# ---- synthesise --------------------------------------------------------------
# The key goes in a header on a request object, never into an argument list that
# could be logged by a shell or captured by the permission classifier.
$payload = @{
  model           = $Model
  voice           = $Voice
  input           = $Text
  response_format = 'opus'
}
if ($Model -eq 'gpt-4o-mini-tts' -and $Direction) { $payload['instructions'] = $Direction }
$body = $payload | ConvertTo-Json -Compress

$req = [Net.HttpWebRequest]::Create('https://api.openai.com/v1/audio/speech')
$req.Method = 'POST'
$req.ContentType = 'application/json'
$req.Headers.Add('Authorization', 'Bearer ' + $cfg.OPENAI_API_KEY)
$req.Timeout = 120000
$bytes = [Text.Encoding]::UTF8.GetBytes($body)
$req.ContentLength = $bytes.Length
$s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Dispose()

try {
  $resp = $req.GetResponse()
} catch [Net.WebException] {
  # Report the provider's reason, never the key. A bare "400" teaches nothing.
  $detail = ''
  if ($_.Exception.Response) {
    $r = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
    $detail = $r.ReadToEnd(); $r.Dispose()
  }
  throw ("speech synthesis failed: " + $_.Exception.Message + " " + $detail)
}

$in = $resp.GetResponseStream()
$fs = [IO.File]::Create($outFile)
try { $in.CopyTo($fs) } finally { $fs.Dispose(); $in.Dispose(); $resp.Dispose() }

$size = (Get-Item -LiteralPath $outFile).Length
if ($size -lt 512) { throw "synthesis returned $size bytes, which is not a recording" }
Write-Output ("synthesised " + [math]::Round($size / 1KB, 1) + " KB of Ogg Opus (" + $Text.Length + " chars, voice " + $Voice + ", model " + $Model + ")")

# ---- hand off to the sender that already works -------------------------------
try {
  $notify = Join-Path $PSScriptRoot 'wa_notify.ps1'
  if (-not (Test-Path -LiteralPath $notify)) { throw "wa_notify.ps1 not found beside this script" }

  # -Raw so the recording is not prefixed with a caption line; the spoken words
  # ARE the message. Identity still travels: -Tag names the instance, and the
  # optional -AlsoText below carries the same words in writing.
  & $notify -File $outFile -As audio -Raw -Text ' ' -Tag 'claude-code-cli'

  if ($AlsoText) {
    & $notify -Text $Text -Kind STATUS -Tag 'claude-code-cli'
  }
} finally {
  if (-not $KeepFile) { Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue }
  else { Write-Output ("kept: " + $outFile) }
}
