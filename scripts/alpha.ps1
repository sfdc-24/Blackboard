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
  [Parameter(Mandatory = $true, ParameterSetName = 'Run')]
  [ValidateSet('read', 'append', 'raw')]
  [string]$Action,
  [string]$SourceTag,
  [string]$TargetSurface,
  [string]$ActionType = 'APPEND',
  [string]$Payload,
  [string]$BodyFile,
  [switch]$NoSecret,
  [int]$Retries = 4,
  # Exercise the secret-handling helper with fixed inputs, no .env and no
  # network. A separate parameter set so -Action stays mandatory for a real run
  # without -SelfTest having to supply it.
  [Parameter(Mandatory = $true, ParameterSetName = 'SelfTest')]
  [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- keeping the read secret off the command line ---------------------------
# THE DEFECT THIS REPLACES, and it was in this file for a fortnight. The header
# of this script says, in its own words, that D-18 keeps credentials "never on a
# command line". The read path then built
#
#     $url = $cfg.ALPHA_URL + '?secret=' + $cfg.ALPHA_SECRET
#
# and passed $url as an ARGUMENT to curl.exe, where any local user can read it
# out of the process table while the transfer is in flight. The WRITE path in
# the same file already did the right thing - the secret goes in a JSON body
# written to a temp file and handed over as --data-binary @file - so the file
# contained both the rule, the correct pattern, and the violation at once.
#
# scripts/hear_in_zoom.ps1 and scripts/wa_inbound_digest.ps1 already solved this
# for header credentials with a curl config file and -K. This is the same fix
# for a query-string credential, so the fleet now applies one pattern
# everywhere rather than two thirds of the time.
#
# PERCENT-ENCODING IS NOT COSMETIC HERE. The old concatenation dropped the
# secret into a query string raw, so a '&' would have started a new parameter,
# a '#' would have truncated the URL at the fragment, and a '+' would have been
# read by the server as a space. That is a correctness bug independent of the
# exposure, and it fails as a mangled request rather than an obvious error.
function New-CurlSecretConfig {
  <#
    Write a curl config naming the full URL, and return its path. The caller
    passes it as `-K <path>` so the URL - and the credential inside it - is
    never an argv element. Delete it in a finally block.
  #>
  param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Secret
  )
  $url = $BaseUrl + '?secret=' + [uri]::EscapeDataString($Secret)
  $path = Join-Path ([IO.Path]::GetTempPath()) ("alpha_" + [guid]::NewGuid().ToString('N') + ".conf")
  # curl reads an unquoted value to end-of-line, which tolerates every character
  # a percent-encoded URL can contain. A quoted value would re-interpret
  # backslash escapes, so it is deliberately NOT quoted.
  [IO.File]::WriteAllText($path, "url = $url`n", (New-Object Text.UTF8Encoding($false)))
  return $path
}

function Get-AlphaReadCurlArgs {
  <#
    The exact argument vector the read uses. It exists as a function so the
    self-test exercises the SAME construction a real run does, rather than a
    copy of it that can drift.

    Reverting the old `$url` concatenation would have to change this function to
    pass a URL positionally, and the assertion below - that no element carries
    the credential - is what fails when someone does.
  #>
  param([Parameter(Mandatory = $true)][string]$ConfigPath)
  return @('-s', '-S', '-L', '--max-time', '90', '-K', $ConfigPath)
}

if ($SelfTest) {
  $pass = 0; $fail = 0
  function Check { param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { $script:pass++; Write-Host "  ok   $Name" }
    else     { $script:fail++; Write-Host "  FAIL $Name $Detail" }
  }

  # A secret containing every character that breaks a raw query string.
  $canary = 'S3CR3T&with#hash+plus=eq/slash?q'
  $base   = 'https://example.invalid/exec'
  $cfgPath = New-CurlSecretConfig -BaseUrl $base -Secret $canary
  try {
    $body = [IO.File]::ReadAllText($cfgPath)

    Check 'the config names the base URL' ($body -like "*$base*")
    # The assertion this whole change exists for, run against the SAME argv
    # builder the read path calls - not a copy of it written here.
    $argv = Get-AlphaReadCurlArgs -ConfigPath $cfgPath
    # BOTH FORMS, and the second is the one that matters. Mutation-testing this
    # suite showed the raw-substring check PASSING against the pre-fix defect,
    # because by then the secret in the URL is percent-encoded and the raw
    # canary no longer appears anywhere in it. A search for the plaintext is
    # blind to exactly the leak it was written to catch.
    $encoded = [uri]::EscapeDataString($canary)
    Check 'the secret is absent from every argv element, raw' (
      -not (@($argv | Where-Object { $_ -like "*$canary*" }).Count)) "argv=$($argv -join ' ')"
    Check 'the secret is absent from every argv element, percent-encoded' (
      -not (@($argv | Where-Object { $_ -like "*$encoded*" }).Count)) "argv=$($argv -join ' ')"
    Check 'the argv passes a config file rather than a URL' (
      $argv -contains '-K' -and $argv[-1] -eq $cfgPath) "argv=$($argv -join ' ')"
    Check 'no argv element looks like a URL at all' (
      -not (@($argv | Where-Object { $_ -like 'http*://*' }).Count)) "argv=$($argv -join ' ')"
    Check 'the raw secret never appears verbatim in the config either' ($body -notlike "*$canary*")
    Check 'the secret IS present, percent-encoded, so the request still works' (
      $body -like ("*" + [uri]::EscapeDataString($canary) + "*"))

    # Each character that would have corrupted the old concatenated URL.
    Check 'an ampersand is encoded and cannot start a new parameter' ($body -notlike '*&with*')
    Check 'a hash is encoded and cannot truncate the URL' ($body -notlike '*#hash*')
    Check 'the config is a single url line curl can read' (
      ($body -split "`n" | Where-Object { $_.Trim() } | Measure-Object).Count -eq 1)
    Check 'the config carries no quoting for curl to re-escape' ($body -notlike '*"*')
  } finally {
    Remove-Item -LiteralPath $cfgPath -Force -ErrorAction SilentlyContinue
  }
  Check 'the config file is removed after use' (-not (Test-Path -LiteralPath $cfgPath))

  # Two different calls must not collide, or a concurrent run reads or deletes
  # the other's credential.
  $p1 = New-CurlSecretConfig -BaseUrl $base -Secret 'a'
  $p2 = New-CurlSecretConfig -BaseUrl $base -Secret 'b'
  try { Check 'each call gets its own config path' ($p1 -ne $p2) }
  finally { Remove-Item -LiteralPath $p1, $p2 -Force -ErrorAction SilentlyContinue }

  Write-Host ""
  Write-Host "RESULT passed=$pass failed=$fail"
  if ($fail -gt 0) { exit 1 }
  if ($pass -eq 0) { Write-Host 'no assertions ran, which is not a pass'; exit 1 }
  exit 0
}

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
  # The URL - and the secret in it - goes in a curl config, never in argv.
  $cc = New-CurlSecretConfig -BaseUrl $cfg.ALPHA_URL -Secret $cfg.ALPHA_SECRET
  try {
    for ($n = 1; $n -le $Retries; $n++) {
      $raw = (& $curlPath @(Get-AlphaReadCurlArgs -ConfigPath $cc)) -join "`n"
      if ($raw.TrimStart().StartsWith('{')) { Write-Output $raw; return }
    }
  } finally {
    # ALWAYS, including on the early `return` above and on a throw. This file
    # holds a live credential; it must not outlive the transfer.
    Remove-Item -LiteralPath $cc -Force -ErrorAction SilentlyContinue
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
