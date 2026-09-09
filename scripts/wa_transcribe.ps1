#Requires -Version 5.1
<#
Turn a WhatsApp voice note into text.
claude-code-cli, 2026-09-08. Asked for directly after four voice notes from
Mr. Salam arrived on the board with an empty payload.

WHY THE WORK IS HERE AND NOT IN THE GATEWAY
  The obvious place is the Pipedream step that already receives the webhook. It is
  the wrong place. That single step is also what writes his messages to the board,
  so every line added to it is a line that can stop the fleet hearing him -- and a
  media fetch plus a transcription call is a lot of new failure surface to put in
  front of the one channel that must not break.

  So the gateway gains ONE thing: when a message carries no text, it records WHAT
  arrived (type and media id) instead of writing an empty row. Everything else --
  downloading the audio, transcribing it, publishing the words -- happens here,
  where it can be tested, retried and fixed without touching the channel. If this
  script is broken or not running, a voice note still lands as a legible marker
  rather than as silence.

DOCTRINE
  D-18: META_TOKEN and GROQ_API_KEY are read from ..\.env at run time. Neither
  reaches a command line or a log.
  D-4: this appends a NEW row carrying the transcript and quoting the source
  Row_ID. It never rewrites the original. The board is append-only and the
  original marker stays as evidence of what actually arrived.

USAGE
  & .\scripts\wa_transcribe.ps1 -MediaId 1234567890 -DryRun
  & .\scripts\wa_transcribe.ps1 -File C:\path\sample.ogg          # local file, no Graph call
  & .\scripts\wa_transcribe.ps1 -MediaId 1234567890 -SourceRowId WRK-abc123
#>
param(
  [string]$MediaId,
  [string]$File,
  [string]$SourceRowId,
  [string]$Model = 'whisper-large-v3',
  [string]$Language = 'en',
  [ValidateRange(1, 25)]
  [int]$MaxMB = 20,
  [switch]$Publish,
  [switch]$DryRun,
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$GRAPH = 'https://graph.facebook.com/v22.0'

if (-not $MediaId -and -not $File) { throw "pass -MediaId (fetch from WhatsApp) or -File (a local recording)" }

if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "env file not found: $EnvFile" }
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
if (-not $cfg.GROQ_API_KEY) { throw "GROQ_API_KEY missing in $EnvFile" }
if ($MediaId -and -not $cfg.META_TOKEN) { throw "META_TOKEN missing in $EnvFile" }

# GOTCHA, already documented in wa_notify.ps1 and rediscovered the hard way on
# 2026-09-09: the STORED META_TOKEN begins with the literal text "Bearer ".
# Sending 'Bearer ' + token yields "Bearer Bearer EAA..." and Graph answers 401
# with code 190. wa_notify.ps1 has always stripped it; this script did not, so
# every -MediaId fetch here would have failed. It was never caught because the
# script was only ever exercised with -File, which does not touch Graph at all.
$GraphToken = $cfg.META_TOKEN -replace '^\s*[Bb]earer\s+', ''
# NOT named $META. PowerShell variable names are CASE-INSENSITIVE, and line ~92
# assigns the media metadata to $meta -- which would silently overwrite a token
# held in $META and send the header 'Bearer @{url=...; mime_type=...}'. Graph
# answers 401, identical to a bad token, and hop 1 still works because it runs
# first. Cost 25 minutes on 2026-09-09 chasing a token bug that was a name bug.

function Invoke-GraphJson {
  param([string]$Url)
  $req = [Net.HttpWebRequest]::Create($Url)
  $req.Method = 'GET'
  $req.Headers.Add('Authorization', 'Bearer ' + $GraphToken)
  $req.Timeout = 60000
  $resp = $req.GetResponse()
  $sr = New-Object IO.StreamReader($resp.GetResponseStream())
  try { return ($sr.ReadToEnd() | ConvertFrom-Json) } finally { $sr.Dispose(); $resp.Dispose() }
}

# ---- get the recording -------------------------------------------------------
$audioPath = $null
$temp = $false
if ($File) {
  if (-not (Test-Path -LiteralPath $File)) { throw "file not found: $File" }
  $audioPath = (Resolve-Path -LiteralPath $File).Path
} else {
  # Two hops, both authenticated: the id resolves to a short-lived URL, then the
  # URL serves the bytes. The second hop needs the token too -- a plain GET on it
  # returns 401, which is the mistake worth writing down rather than rediscovering.
  $meta = Invoke-GraphJson -Url ("$GRAPH/$MediaId")
  if (-not $meta.url) { throw "media $MediaId returned no url" }
  $bytesExpected = [int]$meta.file_size
  if ($bytesExpected -gt ($MaxMB * 1MB)) {
    throw ("recording is " + [math]::Round($bytesExpected/1MB,1) + " MB, over the " + $MaxMB + " MB bound")
  }
  $ext = switch -Wildcard ([string]$meta.mime_type) {
    'audio/ogg*'  { '.ogg' } 'audio/mpeg*' { '.mp3' } 'audio/mp4*' { '.m4a' }
    'audio/amr*'  { '.amr' } 'audio/aac*'  { '.aac' } default { '.ogg' }
  }
  $audioPath = Join-Path ([IO.Path]::GetTempPath()) ('wa_in_' + [guid]::NewGuid().ToString('N') + $ext)
  $temp = $true
  $req = [Net.HttpWebRequest]::Create([string]$meta.url)
  $req.Method = 'GET'
  $req.Headers.Add('Authorization', 'Bearer ' + $GraphToken)
  # Hop 2 is served by Meta's lookaside CDN, not graph.facebook.com, and it
  # rejects the default .NET user agent with a bare 401 that looks identical to
  # a bad token. Hop 1 does not care. Found 2026-09-09 by fixing the token and
  # watching the failure move from line 75 to here rather than disappear.
  $req.UserAgent = 'sfdc24-blackboard/1.0'
  $req.Timeout = 120000
  $resp = $req.GetResponse()
  $fs = [IO.File]::Create($audioPath)
  try { $resp.GetResponseStream().CopyTo($fs) } finally { $fs.Dispose(); $resp.Dispose() }
  Write-Host ("fetched " + [math]::Round((Get-Item $audioPath).Length/1KB,1) + " KB  mime=" + $meta.mime_type)
}

try {
  $size = (Get-Item -LiteralPath $audioPath).Length
  if ($size -lt 256) { throw "recording is $size bytes, which is not audio" }
  if ($size -gt ($MaxMB * 1MB)) { throw ("recording is " + [math]::Round($size/1MB,1) + " MB, over the bound") }

  if ($DryRun) {
    Write-Host "DRY RUN - nothing transcribed, nothing published"
    Write-Host ("would transcribe: " + $audioPath + "  (" + [math]::Round($size/1KB,1) + " KB, model " + $Model + ")")
    return
  }

  # ---- transcribe ------------------------------------------------------------
  # curl does the multipart body; PowerShell 5.1 has no native multipart and
  # hand-rolling the boundary is a well-known way to corrupt binary parts.
  # The key goes in a HEADER FILE, never in the argument list (D-18) -- argv is
  # visible to other processes on this machine.
  $curl = Get-Command curl.exe -ErrorAction Stop
  $hdr = Join-Path ([IO.Path]::GetTempPath()) ('gq_' + [guid]::NewGuid().ToString('N') + '.hdr')
  Set-Content -LiteralPath $hdr -Value ('Authorization: Bearer ' + $cfg.GROQ_API_KEY) -Encoding ASCII
  try {
    $out = & $curl.Source -s -S --max-time 180 `
      -H "@$hdr" `
      -F ("file=@" + $audioPath) `
      -F ("model=" + $Model) `
      -F ("language=" + $Language) `
      -F 'response_format=json' `
      'https://api.groq.com/openai/v1/audio/transcriptions'
  } finally { Remove-Item -LiteralPath $hdr -Force -ErrorAction SilentlyContinue }

  $raw = ($out -join "`n").Trim()
  if (-not $raw) { throw "transcription returned an empty body" }
  try { $j = $raw | ConvertFrom-Json } catch { throw ("transcription returned non-JSON: " + $raw.Substring(0, [Math]::Min(300, $raw.Length))) }
  if ($j.error) { throw ("transcription failed: " + ($j.error.message)) }
  $textOut = ([string]$j.text).Trim()
  if (-not $textOut) { throw "transcription produced no words" }

  # Framing goes to the HOST, the transcript goes to the SUCCESS stream, and the
  # transcript is emitted exactly once. Doing both -- printing it and returning it
  # -- handed callers the words twice, which the first round-trip test caught by
  # producing a duplicated sentence.
  Write-Host "--- transcript ---"
  Write-Host $textOut
  Write-Host "------------------"

  # ---- publish, as a NEW row that quotes the original (D-4) ------------------
  if ($Publish) {
    $flat = ($textOut -replace '\s+', ' ')
    if ($flat.Length -gt 3000) { $flat = $flat.Substring(0, 3000) + ' [truncated]' }
    $payload = 'WA-VOICE|from=whatsapp|source_row=' + $(if ($SourceRowId) { $SourceRowId } else { 'unknown' }) +
               '|media=' + $(if ($MediaId) { $MediaId } else { 'local-file' }) +
               '|model=' + $Model + '|text=' + $flat
    $alpha = Join-Path $PSScriptRoot 'alpha.ps1'
    & $alpha -Action append -SourceTag 'whatsapp' -TargetSurface 'ALL' -Payload $payload
    Write-Host "published; VERIFY BY READ-BACK before treating it as delivered (D-4)"
  }
  return $textOut
} finally {
  if ($temp -and -not $DryRun) { Remove-Item -LiteralPath $audioPath -Force -ErrorAction SilentlyContinue }
}
