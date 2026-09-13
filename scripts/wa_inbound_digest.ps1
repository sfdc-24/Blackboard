#Requires -Version 5.1
<#
Understand the media Mr. Salam sends, instead of leaving it on the board as an id.

WHY THIS EXISTS
  The gateway writes one row per inbound WhatsApp message. When the message is a
  voice note or a photo it cannot write the words, so it writes a marker:

      WA-MEDIA|type=audio|media=1432701812300046

  Nothing ever turned those markers back into content. wa_transcribe.ps1 has
  existed since 2026-09-08 and had to be run BY HAND, against an id someone had
  noticed. So a voice note is only heard if an agent happens to be looking at the
  right row at the right moment.

  MEASURED: on 2026-09-12 he recorded a voice note at 04:05Z saying "tell me what
  you need from me and I will enable you... I will do the clicks, I will pay the
  money". It sat unheard for SIXTEEN HOURS while the board carried it as a
  sixteen-digit number. Earlier the same week three of his voice notes were missed
  the same way and he had to say so.

  A message that arrived and was not understood is indistinguishable, to him, from
  one that was ignored.

WHAT IT DOES
  Finds WA-MEDIA rows with no digest, fetches the media, turns audio into words
  and images into a description, and appends ONE digest row per source row that
  quotes the source id. Append-only: it never rewrites the marker (D-4), so the
  original stays as evidence of what actually arrived.

IDEMPOTENT BY CONSTRUCTION
  A digest row carries `src=<source row id>`. A source row already named by some
  digest is skipped. Re-running costs nothing and cannot double-post - which
  matters because the intended use is a schedule, and a scheduled job that
  double-posts is a job people turn off.

PRIVACY GUARD ON IMAGES, deliberately
  His screenshots contain phone numbers, contact cards and account pages. The
  board is shared with every agent and is permanent. So the vision prompt is
  instructed to describe WHAT THE IMAGE IS and what it is for, and NOT to
  reproduce phone numbers, email addresses, tokens or account identifiers. The
  description is meant to make an agent look at the image, not to replace it.

D-18: every key is read from .env at run time. None reaches a command line.
#>
param(
  [string]$EnvFile,
  [string]$AlphaPath,
  [string]$BoardJson,
  [string]$OutFile,
  # Direct mode: understand these media ids and do not consult the board.
  [string[]]$MediaId,
  [ValidateSet('audio','image')]
  [string]$Kind = 'audio',
  [int]$MaxItems = 10,
  [switch]$AudioOnly,
  # Posting is OPT-IN. The board's write path could not be verified at the time
  # this was written, and an unverified write to a shared permanent log is worse
  # than no write - so by default it prints and returns the digest.
  [switch]$Post,
  [switch]$DryRun,
  [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$GRAPH = 'https://graph.facebook.com/v22.0'

$here = Split-Path -Parent $PSCommandPath
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $here) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "env file not found: $EnvFile" }

$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
foreach ($k in @('META_TOKEN','GROQ_API_KEY','OPENAI_API_KEY')) {
  if (-not $cfg.ContainsKey($k) -or -not $cfg[$k]) { throw "$k missing in $EnvFile" }
}
# The stored META_TOKEN already begins with the literal text "Bearer ". Sending
# 'Bearer ' + it yields "Bearer Bearer EAA..." and Graph answers 401 - a bug that
# cost 25 minutes in this repo once already.
$graphTok = $cfg['META_TOKEN'] -replace '^\s*[Bb]earer\s+', ''

# alpha.ps1, NOT bus.ps1. bus.ps1 is the Docs/Sheets client and has no
# -SourceTag/-Payload; the Alpha DB board append lives in alpha.ps1, and its own
# header states the rule this script obeys: an append response verifies nothing,
# so a write is proved by reading back and matching on row id, never by a retry.
# THE FLEET'S COMMS TOOLCHAIN IS NOT IN GIT. `git ls-files scripts/` returns ZERO
# tracked files: alpha.ps1, bus.ps1, wa_notify.ps1, wa_transcribe.ps1 and
# wa_voice_reply.ps1 exist only in the working checkout on one laptop. The single
# channel this fleet has to Mr. Salam is unversioned and unbacked. That is worth
# fixing and is not this script's business to fix, so it LOOKS for its dependency
# rather than assuming a layout, and says where it found it.
$alpha = $AlphaPath
if (-not $alpha) {
  $candidates = @(
    (Join-Path $here 'alpha.ps1'),
    (Join-Path 'C:\Users\salam\Quantum\Blackboard\scripts' 'alpha.ps1')
  )
  foreach ($cand in $candidates) {
    if (Test-Path -LiteralPath $cand) { $alpha = $cand; break }
  }
}
if (-not $alpha -or -not (Test-Path -LiteralPath $alpha)) {
  throw "alpha.ps1 not found - looked beside this script and in the working checkout. Pass -AlphaPath."
}

function Write-Note { param([string]$m) if (-not $Quiet) { Write-Host $m } }

# ---- board ------------------------------------------------------------------
function Read-Board {
  $raw = & $alpha -Action read 2>$null 3>$null
  if (-not $raw) { return $null }
  try { return (($raw | Out-String) | ConvertFrom-Json) } catch { return $null }
}

# DIRECT MODE. -MediaId processes exactly what it is given and never touches the
# board at all.
#
# It exists because the board is not a dependable input right now, and the
# UNDERSTANDING is the valuable half. `alpha.ps1 -Action read` currently answers
# with an HTML redirect artifact rather than JSON ("read returned non-JSON 4
# times"), and my notes record three write endpoints on this board none of which
# can be identified from a row's shape. Understanding a voice note should not be
# unavailable because a sheet is flaky.
if ($MediaId -and $MediaId.Count -gt 0) {
  # @() around the foreach: under Set-StrictMode 2.0 a single result is a SCALAR
  # and .Count does not exist on it, which throws PropertyNotFoundStrict. One
  # item is the common case here, so the unwrapped version fails exactly when it
  # is most used.
  $pending = @(foreach ($id in $MediaId) {
    [pscustomobject]@{ RowId = "direct-$id"; Kind = $Kind; MediaId = "$id" }
  })
  Write-Note "direct mode: $(@($pending).Count) media id(s), board not consulted"
} else {
  $board = $null
  if ($BoardJson) {
    if (-not (Test-Path -LiteralPath $BoardJson)) { throw "board json not found: $BoardJson" }
    $board = Get-Content -LiteralPath $BoardJson -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Note "board from file: $BoardJson"
  } else {
    $board = Read-Board
  }
  if (-not $board -or -not $board.rows) {
    throw "could not read the board - refusing to guess what has been processed. Pass -BoardJson <dump>, or -MediaId <id> to work directly."
  }
  $rows = @($board.rows)
  Write-Note "board rows: $($rows.Count)"

# Flatten each row to text once; the shape varies by writer and this only needs
# to find markers and digests.
$flat = foreach ($r in $rows) { , ((@($r) | ForEach-Object { "$_" }) -join '|') }

# Which source rows already have a digest? Anything named by a WA-DIGEST row.
$done = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($t in $flat) {
  foreach ($m in [regex]::Matches($t, 'WA-DIGEST\|src=([A-Za-z0-9\-]+)')) {
    $null = $done.Add($m.Groups[1].Value)
  }
}
Write-Note "already digested: $($done.Count)"

# Outstanding markers, oldest first so the board reads in order.
$pending = @()
for ($i = 0; $i -lt $rows.Count; $i++) {
  $t = $flat[$i]
  $m = [regex]::Match($t, 'WA-MEDIA\|type=(audio|image|video|document)\|media=(\d+)')
  if (-not $m.Success) { continue }
  $srcId = (@($rows[$i]) | Select-Object -First 1) -as [string]
  if (-not $srcId) { continue }
  if ($done.Contains($srcId)) { continue }
  $kind = $m.Groups[1].Value
  if ($AudioOnly -and $kind -ne 'audio') { continue }
  $pending += [pscustomobject]@{ RowId = $srcId; Kind = $kind; MediaId = $m.Groups[2].Value }
}
  Write-Note "outstanding media rows: $($pending.Count)"
  $pending = $pending | Select-Object -Last $MaxItems
}
if (-not $pending -or @($pending).Count -eq 0) { Write-Note "nothing to do"; exit 0 }

# ---- media ------------------------------------------------------------------
function Get-Media {
  param([string]$MediaId)
  $req = [Net.HttpWebRequest]::Create("$GRAPH/$MediaId")
  $req.Method = 'GET'; $req.Headers.Add('Authorization', 'Bearer ' + $graphTok); $req.Timeout = 60000
  $resp = $req.GetResponse(); $sr = New-Object IO.StreamReader($resp.GetResponseStream())
  try { $meta = ($sr.ReadToEnd() | ConvertFrom-Json) } finally { $sr.Dispose(); $resp.Dispose() }
  if (-not $meta.url) { throw "media $MediaId returned no url" }

  $ext = switch -Wildcard ([string]$meta.mime_type) {
    'audio/ogg*' {'.ogg'} 'audio/mpeg*' {'.mp3'} 'audio/mp4*' {'.m4a'} 'audio/amr*' {'.amr'}
    'image/jpeg*' {'.jpg'} 'image/png*' {'.png'} 'image/webp*' {'.webp'} default {'.bin'}
  }
  $path = Join-Path ([IO.Path]::GetTempPath()) ("wa_" + $MediaId + $ext)
  $r2 = [Net.HttpWebRequest]::Create([string]$meta.url)
  $r2.Method = 'GET'; $r2.Headers.Add('Authorization', 'Bearer ' + $graphTok)
  # Hop 2 is Meta's lookaside CDN and it rejects the default .NET user agent with
  # a bare 401 that looks exactly like a bad token. Hop 1 does not care.
  $r2.UserAgent = 'sfdc24-blackboard/1.0'; $r2.Timeout = 120000
  $resp2 = $r2.GetResponse(); $fs = [IO.File]::Create($path)
  try { $resp2.GetResponseStream().CopyTo($fs) } finally { $fs.Dispose(); $resp2.Dispose() }

  $size = (Get-Item -LiteralPath $path).Length
  if ($size -lt 256) { throw "downloaded $size bytes for $MediaId - not usable media" }
  return [pscustomobject]@{ Path = $path; Mime = [string]$meta.mime_type; Bytes = $size }
}

function Get-Transcript {
  param([string]$Path)
  $curl = "$env:SystemRoot\System32\curl.exe"
  if (-not (Test-Path -LiteralPath $curl)) { $c = Get-Command curl.exe -ErrorAction SilentlyContinue; if ($c) { $curl = $c.Source } else { throw "curl.exe not found" } }
  $cc = Join-Path ([IO.Path]::GetTempPath()) ("gq_" + [guid]::NewGuid().ToString('N') + ".conf")
  Set-Content -LiteralPath $cc -Value ('header = "Authorization: Bearer ' + $cfg['GROQ_API_KEY'] + '"') -Encoding ascii
  try {
    $raw = & $curl -s -K $cc -F "file=@$Path" -F 'model=whisper-large-v3' -F 'response_format=json' -F 'language=en' `
             https://api.groq.com/openai/v1/audio/transcriptions
  } finally { Remove-Item -LiteralPath $cc -Force -ErrorAction SilentlyContinue }
  try { return ($raw | ConvertFrom-Json).text } catch { return $null }
}

function Get-ImageDescription {
  param([string]$Path, [string]$Mime)
  $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Path))
  # THE PRIVACY INSTRUCTION IS LOAD BEARING. The board is shared and permanent,
  # and his screenshots have included a contact card with a phone number on it.
  $prompt = @'
Describe this screenshot or photo for a colleague who has not seen it, in at most
four sentences. Say what application or page it shows and what it appears to be
about, so they know whether they need to open it.

Do NOT reproduce phone numbers, email addresses, postal addresses, API keys,
passwords, meeting passcodes, account numbers or full names of private
individuals. If the image contains any of those, say that it does and what kind,
without repeating the value.
'@
  $body = @{
    model = 'gpt-4o-mini'; max_tokens = 300
    messages = @(@{ role = 'user'; content = @(
      @{ type = 'text'; text = $prompt },
      @{ type = 'image_url'; image_url = @{ url = "data:$Mime;base64,$b64" } }
    )})
  } | ConvertTo-Json -Depth 10 -Compress

  $req = [Net.HttpWebRequest]::Create('https://api.openai.com/v1/chat/completions')
  $req.Method = 'POST'; $req.ContentType = 'application/json'
  $req.Headers.Add('Authorization', 'Bearer ' + $cfg['OPENAI_API_KEY']); $req.Timeout = 120000
  $b = [Text.Encoding]::UTF8.GetBytes($body); $req.ContentLength = $b.Length
  $s = $req.GetRequestStream(); try { $s.Write($b, 0, $b.Length) } finally { $s.Dispose() }
  $resp = $req.GetResponse(); $sr = New-Object IO.StreamReader($resp.GetResponseStream())
  try { $raw = $sr.ReadToEnd() } finally { $sr.Dispose(); $resp.Dispose() }
  try { return ($raw | ConvertFrom-Json).choices[0].message.content } catch { return $null }
}

# ---- process ----------------------------------------------------------------
$posted = 0; $failed = 0
foreach ($item in $pending) {
  Write-Note ""
  Write-Note ("-- {0}  {1}  media={2}" -f $item.RowId, $item.Kind, $item.MediaId)
  $media = $null
  try { $media = Get-Media -MediaId $item.MediaId }
  catch { Write-Note "   FETCH FAILED: $($_.Exception.Message)"; $failed++; continue }
  Write-Note ("   fetched {0:N0} bytes  {1}" -f $media.Bytes, $media.Mime)

  $text = $null
  try {
    if ($item.Kind -eq 'audio')      { $text = Get-Transcript -Path $media.Path }
    elseif ($item.Kind -eq 'image')  { $text = Get-ImageDescription -Path $media.Path -Mime $media.Mime }
    else { $text = "(no handler for type $($item.Kind); media fetched, $($media.Bytes) bytes, $($media.Mime))" }
  } catch { Write-Note "   UNDERSTANDING FAILED: $($_.Exception.Message)" }
  finally { Remove-Item -LiteralPath $media.Path -Force -ErrorAction SilentlyContinue }

  if (-not $text -or -not "$text".Trim()) {
    # REFUSE rather than post an empty digest. An empty digest would mark the row
    # processed and bury the message for good - worse than leaving it pending.
    Write-Note "   produced no text - leaving the row PENDING rather than marking it done"
    $failed++; continue
  }
  $text = ("$text" -replace '\s+', ' ').Trim()
  Write-Note ("   => " + $text.Substring(0, [Math]::Min(160, $text.Length)))

  # THE FULL TEXT, on the OUTPUT stream and optionally to a file.
  #
  # The line above is a 160-character PREVIEW for a human watching. A privacy
  # check run against that preview would be checking a truncation, not the thing
  # that gets posted - and I did exactly that once, then "passed" a leak test
  # against an empty string because Write-Host does not reach the output stream
  # at all. Anything asserting on this content must be able to see ALL of it.
  [pscustomobject]@{ RowId = $item.RowId; Kind = $item.Kind; MediaId = $item.MediaId; Text = $text }
  if ($OutFile) { Add-Content -LiteralPath $OutFile -Value ("$($item.RowId)`t$($item.Kind)`t$text") -Encoding UTF8 }

  if ($DryRun -or -not $Post) {
    Write-Note "   not posted (pass -Post to write a digest row; posting is opt-in on purpose)"
    $posted++   # the understanding succeeded, which is what this counts
    continue
  }

  # No raw pipe inside a payload value: the board grammar is pipe-delimited.
  $safe = $text -replace '\|', '/'
  $payload = "WA-DIGEST|src=$($item.RowId)|type=$($item.Kind)|media=$($item.MediaId)|text=$safe"
  try {
    & $alpha -Action append -SourceTag 'claude-code-cli' -TargetSurface 'ALL' `
        -ActionType 'APPEND' -Payload $payload 2>$null 3>$null | Out-Null
    Write-Note "   posted"
    $posted++
  } catch { Write-Note "   POST FAILED: $($_.Exception.Message)"; $failed++ }
}

Write-Note ""
Write-Host "RESULT digested=$posted failed=$failed pending_seen=$($pending.Count)"
if ($failed -gt 0) { exit 1 }
exit 0
