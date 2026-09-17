#Requires -Version 5.1
<#
Speak through THIS machine's speakers, in a chosen agent persona.
claude-code-cli, 2026-09-12. Asked for directly: "keep refining and fine tuning
with multiple voice options; may be more than one agent in the meeting" and
"voice agent persona's such as one business analyst and one solution architect".

WHY THIS EXISTS SEPARATELY FROM wa_voice_reply.ps1
  wa_voice_reply.ps1 delivers a voice NOTE to WhatsApp. This delivers a voice
  into the ROOM: when this laptop is joined to a Zoom meeting, whatever plays
  through its output device is what the meeting hears. No Linux SDK, no raw
  audio injection, no new credentials — the laptop's own audio path is the
  transport. It deliberately does NOT touch wa_notify.ps1, so the two delivery
  paths cannot break each other (origin has a slimmed, media-less wa_notify;
  this script must survive that merge untouched).

PERSONAS, NOT JUST VOICES
  scripts/voices.json is the single registry: instance voices (who is talking)
  and role personas (what hat they wear — 'ba' the Business Analyst, 'sa' the
  Solution Architect). A persona carries voice + delivery direction together,
  so two agents in one meeting stay tellable apart by ear. Resolution order:
  explicit -Voice/-Direction beats -Persona, which beats the claude default.

WHY WAV AND NOT OPUS HERE
  System.Media.SoundPlayer is in-box on Windows PowerShell 5.1 and plays PCM
  WAV synchronously — no codecs, no installs, blocks until the line finishes,
  which is exactly what sequential dialogue needs. OpenAI's 'wav' response
  format returns PCM16 that SoundPlayer accepts.

DIALOGUE MODE — the two-consultant demo
  -DialogueFile takes a JSON array: [{"persona":"ba","text":"..."}, ...].
  ALL lines are synthesized BEFORE the first one plays, so the meeting hears a
  conversation with natural hand-offs, not API latency between speakers.

DOCTRINE
  D-18: OPENAI_API_KEY comes from ..\.env at run time, never argv, never logs.
  -DryRun synthesizes NOTHING and plays nothing.
  Cost: speech is billed per character (see wa_voice_reply.ps1); the same
  -MaxChars bound applies per line.

USAGE
  & .\scripts\speak_local.ps1 -Persona ba -Text "Walk me through what happens after the lead comes in."
  & .\scripts\speak_local.ps1 -Persona sa -Text "Two objects and one flow. Let me show you the tradeoff." -KeepFile
  & .\scripts\speak_local.ps1 -DialogueFile demo.json
  & .\scripts\speak_local.ps1 -Persona ba -Voice coral -Text "..."   # explicit override wins
#>
param(
  [string]$Text,
  [string]$TextFile,
  [string]$DialogueFile,
  [string]$Persona,
  [ValidateSet('', 'alloy', 'ash', 'ballad', 'coral', 'echo', 'fable',
               'nova', 'onyx', 'sage', 'shimmer', 'verse')]
  [string]$Voice = '',
  [ValidateSet('tts-1', 'tts-1-hd', 'gpt-4o-mini-tts')]
  [string]$Model = 'gpt-4o-mini-tts',
  [string]$Direction = '',
  [ValidateRange(1, 3500)]
  [int]$MaxChars = 1200,
  [switch]$KeepFile,
  [switch]$DryRun,
  [string]$EnvFile,
  [string]$RegistryFile
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- the registry -------------------------------------------------------------
if (-not $RegistryFile) { $RegistryFile = Join-Path $PSScriptRoot 'voices.json' }
$registry = $null
if (Test-Path -LiteralPath $RegistryFile) {
  $registry = Get-Content -LiteralPath $RegistryFile -Raw | ConvertFrom-Json
}

function Resolve-PersonaVoice {
  param([string]$Key)
  if (-not $registry) { throw "persona '$Key' requested but registry not found: $RegistryFile" }
  $entry = $null
  if ($registry.personas -and $registry.personas.PSObject.Properties[$Key]) {
    $entry = $registry.personas.$Key
  } elseif ($registry.instances -and $registry.instances.PSObject.Properties[$Key]) {
    $entry = $registry.instances.$Key
  }
  if (-not $entry) { throw "persona '$Key' not in $RegistryFile (personas: $(@($registry.personas.PSObject.Properties.Name) -join ', '); instances: $(@($registry.instances.PSObject.Properties.Name) -join ', '))" }
  return $entry
}

# ---- collect the lines to speak ------------------------------------------------
$lines = @()
if ($DialogueFile) {
  if (-not (Test-Path -LiteralPath $DialogueFile)) { throw "dialogue file not found: $DialogueFile" }
  $dialogue = Get-Content -LiteralPath $DialogueFile -Raw | ConvertFrom-Json
  foreach ($turn in $dialogue) {
    if (-not $turn.text) { throw "dialogue turn without text" }
    $lines += , @{ persona = [string]$turn.persona; text = ([string]$turn.text).Trim() }
  }
  if (-not $lines.Count) { throw "dialogue file has no turns" }
} else {
  if ($TextFile) {
    if (-not (Test-Path -LiteralPath $TextFile)) { throw "text file not found: $TextFile" }
    $Text = Get-Content -LiteralPath $TextFile -Raw
  }
  if (-not $Text -or -not $Text.Trim()) { throw "nothing to say: pass -Text, -TextFile or -DialogueFile" }
  $lines = , @{ persona = $Persona; text = $Text.Trim() }
}

# ---- resolve voice + direction per line, and bound cost BEFORE spending --------
$plan = @()
foreach ($line in $lines) {
  $v = $Voice; $d = $Direction
  if ($line.persona) {
    $entry = Resolve-PersonaVoice -Key $line.persona
    if (-not $v) { $v = [string]$entry.voice }
    if (-not $d) { $d = [string]$entry.direction }
  }
  if (-not $v) { $v = 'nova' }   # the claude-code-cli default, chosen by ear 2026-09-08
  if ($line.text.Length -gt $MaxChars) {
    throw ("refusing to speak " + $line.text.Length + " characters in one line; the bound is " + $MaxChars)
  }
  $plan += , @{ persona = $line.persona; voice = $v; direction = $d; text = $line.text }
}

if ($DryRun) {
  Write-Output "DRY RUN - no speech synthesised, nothing played"
  foreach ($p in $plan) {
    $who = if ($p.persona) { $p.persona } else { '(default)' }
    Write-Output ("  [" + $who + " / " + $p.voice + "] " + $p.text.Length + " chars: " + $p.text)
  }
  return
}

# ---- credentials, from the env file only (D-18) --------------------------------
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "env file not found: $EnvFile" }
$cfg = @{}
foreach ($envLine in (Get-Content -LiteralPath $EnvFile)) {
  if ($envLine -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
if (-not $cfg.OPENAI_API_KEY) { throw "OPENAI_API_KEY missing in $EnvFile" }

function Invoke-Tts {
  param($Item, [string]$OutFile)
  $payload = @{
    model           = $Model
    voice           = $Item.voice
    input           = $Item.text
    response_format = 'wav'
  }
  if ($Model -eq 'gpt-4o-mini-tts' -and $Item.direction) { $payload['instructions'] = $Item.direction }
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
    $detail = ''
    if ($_.Exception.Response) {
      $r = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
      $detail = $r.ReadToEnd(); $r.Dispose()
    }
    throw ("speech synthesis failed: " + $_.Exception.Message + " " + $detail)
  }
  $in = $resp.GetResponseStream()
  $fs = [IO.File]::Create($OutFile)
  try { $in.CopyTo($fs) } finally { $fs.Dispose(); $in.Dispose(); $resp.Dispose() }
  $size = (Get-Item -LiteralPath $OutFile).Length
  if ($size -lt 512) { throw "synthesis returned $size bytes, which is not a recording" }
}

# ---- synthesize EVERYTHING first, so playback has no API gaps -------------------
$stamp = [guid]::NewGuid().ToString('N')
$files = @()
try {
  $i = 0
  foreach ($p in $plan) {
    $i++
    $f = Join-Path ([IO.Path]::GetTempPath()) ('speak_' + $stamp + '_' + $i + '.wav')
    Invoke-Tts -Item $p -OutFile $f
    $files += , @{ file = $f; item = $p }
    $who = if ($p.persona) { $p.persona } else { $p.voice }
    Write-Output ("synthesised turn " + $i + "/" + $plan.Count + " [" + $who + " / " + $p.voice + "] " + [math]::Round((Get-Item -LiteralPath $f).Length / 1KB, 1) + " KB")
  }

  # ---- play, in order, blocking per line ---------------------------------------
  foreach ($entry in $files) {
    $player = New-Object System.Media.SoundPlayer($entry.file)
    $player.PlaySync()
    $player.Dispose()
  }
  Write-Output ("spoke " + $files.Count + " line(s) through the local output device")
} finally {
  if (-not $KeepFile) {
    foreach ($entry in $files) { Remove-Item -LiteralPath $entry.file -Force -ErrorAction SilentlyContinue }
  } else {
    foreach ($entry in $files) { Write-Output ("kept: " + $entry.file) }
  }
}
