#Requires -Version 5.1
<#
Blackboard - Alpha DB (V2 POC gateway) client for Windows PowerShell 5.1
claude-code-cli, 2026-08-28

WHY THIS EXISTS
  Same reasons as scripts/bus.ps1: DOCTRINE D-18 keeps credentials in this
  machine's local .env, never on a command line, never in Drive. This reads
  ALPHA_URL and ALPHA_SECRET from ..\.env at run time.

  The v1 bus stays the working system and the fallback (ORDER 017). This talks
  to the SEPARATE V2 gateway in front of the Google Sheet "Blackboard - Alpha DB".
  Nothing here touches the v1 bus or any Doc.

REDIRECT HANDLING -- the two shapes differ and it matters
  READ  is a GET with no body, so plain -L is safe and correct.
  WRITE is a POST with a body: curl -L re-issues the POST to the redirect target
        without a Content-Length and Google answers 411. So writes use the
        no-follow two-hop pattern REQ-PR4EXZ settled as standing law -- POST with
        redirects suppressed, then a single GET on the Location header.
        The Location key is ONE-SHOT: never probe it before reading it.

RULES THIS SCRIPT DOES NOT RELAX
  D-4: read-back is the only proof of a write. -Action append prints the response;
       it verifies nothing. Verify with -Action read and match on row_id.
       Never blind-retry an append.

USAGE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\alpha.ps1 -Action read
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\alpha.ps1 -Action append -SourceTag claude-code-cli -TargetSurface "V2 Sandbox" -Payload "text"
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\alpha.ps1 -Action raw -BodyFile probe.json   (adversarial probes: sends the file verbatim)
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('read', 'append', 'raw')]
  [string]$Action,
  [string]$SourceTag,
  [string]$TargetSurface,
  [string]$ActionType = 'APPEND',
  [string]$Payload,
  [string]$BodyFile,
  [switch]$NoSecret,
  [int]$Retries = 4
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
if (-not (Test-Path -LiteralPath $envFile)) { throw "env file not found: $envFile" }
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $envFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
if (-not $cfg.ALPHA_URL -or -not $cfg.ALPHA_SECRET) { throw "ALPHA_URL / ALPHA_SECRET missing in $envFile" }

$curlPath = (Get-Command curl.exe -ErrorAction Stop).Source

# ---- READ: GET, body-less, so following redirects is safe -------------------
if ($Action -eq 'read') {
  $url = $cfg.ALPHA_URL + '?secret=' + $cfg.ALPHA_SECRET
  for ($n = 1; $n -le $Retries; $n++) {
    $raw = (& $curlPath -s -S -L --max-time 90 $url) -join "`n"
    if ($raw.TrimStart().StartsWith('{')) { Write-Output $raw; return }
  }
  Write-Warning "read returned non-JSON $Retries times (redirect artifact). Last response follows."
  Write-Output $raw
  return
}

# ---- WRITE: build the body -------------------------------------------------
if ($Action -eq 'raw') {
  if (-not $BodyFile) { throw "-Action raw needs -BodyFile pointing at a JSON file to send verbatim." }
  $json = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $BodyFile).Path, [Text.Encoding]::UTF8)
} else {
  $body = @{
    source_tag     = $SourceTag
    target_surface = $TargetSurface
    action_type    = $ActionType
    payload        = $Payload
  }
  if (-not $NoSecret) { $body.secret = $cfg.ALPHA_SECRET }
  $json = $body | ConvertTo-Json -Depth 8 -Compress
}

$tmpBody = [IO.Path]::GetTempFileName()
$tmpHead = [IO.Path]::GetTempFileName()
$content = $null
try {
  [IO.File]::WriteAllText($tmpBody, $json, (New-Object Text.UTF8Encoding($false)))
  $out = & $curlPath -s -S -D $tmpHead --max-time 90 -X POST $cfg.ALPHA_URL `
           -H 'Content-Type: application/json' --data-binary "@$tmpBody"
  $head = Get-Content -LiteralPath $tmpHead -ErrorAction SilentlyContinue
  $locLine = @($head | Where-Object { $_ -like 'Location:*' -or $_ -like 'location:*' }) | Select-Object -First 1
  if ($locLine) {
    $target = $locLine.Substring($locLine.IndexOf(':') + 1).Trim()
    $content = (& $curlPath -s -S --max-time 90 $target) -join "`n"
  } else {
    $content = ($out) -join "`n"
  }
} finally {
  if (Test-Path -LiteralPath $tmpBody) { Remove-Item -LiteralPath $tmpBody -Force }
  if (Test-Path -LiteralPath $tmpHead) { Remove-Item -LiteralPath $tmpHead -Force }
}

if ($null -eq $content) { $content = '' }
if (-not $content.TrimStart().StartsWith('{')) {
  Write-Warning "response is not JSON. Per D-4 the write may still have landed -- READ BACK before trusting or retrying."
}
Write-Output $content
