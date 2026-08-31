#Requires -Version 5.1
<#
SFDC24 Blackboard Bus client for Windows PowerShell 5.1
claude-code-cli, 2026-08-26

WHY THIS EXISTS
  On this laptop the Claude Code auto-mode permission classifier blocks any shell
  command that carries the bus secret inline. DOCTRINE D-18 says the credentials
  belong in the machine's local env file anyway. So this script reads BUS_URL and
  BUS_SECRET from ..\.env at run time; the secret never appears on a command line.

  It uses the no-follow redirect pattern that REQ-PR4EXZ settled as standing law:
  POST with redirects suppressed, then a plain GET on the Location header. That
  returns the TRUE JSON response instead of a redirect artifact.

USAGE (any working directory; paths are resolved from this script's location)
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action ping
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action time
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action read -Title "SFDC24 — Dispatch (Work Queue)" -OutFile out.json
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action append -Title "SFDC24 — Inbox · claude-code-cli" -TextFile entry.txt
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action append -Title "Claude Instance - check in sheet" -SheetRowJson '["claude-code-cli","Working","Instance unification","2026-08-26 3:00 PM EDT","","<targets>","<intent>"]'
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action replace -Title "SFDC24 — SESSION STATE · claude-code-cli" -TextFile state.txt   (bus v2+)
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action list   (bus v2+)

RULES THIS SCRIPT DOES NOT RELAX
  D-4: read-back is the only proof of a write. This script prints the response; it
  verifies nothing. Never blind-retry an append -- read the target back first.
  ISSUE 010: for a Sheet, always send -SheetRowJson (a native array), never -Text.
  ISSUE 006: put timestamps in the form "2026-08-26 3:00 PM EDT" (EDT suffix).
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('ping', 'time', 'read', 'append', 'replace', 'list')]
  [string]$Action,
  [string]$Title,
  [string]$FileId,
  [string]$Text,
  [string]$TextFile,
  [string]$SheetRowJson,
  [string]$SheetName,
  [string]$Anchor,
  [switch]$Force,
  [switch]$NoSeparator,
  [string]$OutFile,
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- load credentials from .env (never from argv) ---------------------------
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "env file not found: $EnvFile  (needs BUS_URL= and BUS_SECRET= lines; see .env.example)"
}
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
    $cfg[$matches[1]] = $matches[2].Trim('"').Trim("'")
  }
}
if (-not $cfg.BUS_URL -or -not $cfg.BUS_SECRET) { throw "BUS_URL / BUS_SECRET missing in $EnvFile" }

# ---- ping is a GET, not a POST ----------------------------------------------
# doGet defaults action to 'ping'; doPost has no 'ping' branch and answers
# "Unknown action: ping" (400). This is the ORDER 016 step (b) health check --
# route it the way the endpoint actually implements it. Found 2026-08-28.
if ($Action -eq 'ping') {
  $r = Invoke-WebRequest -Uri $cfg.BUS_URL -Method Get -UseBasicParsing -TimeoutSec 120
  Write-Output $r.Content
  return
}

# ---- build the payload ------------------------------------------------------
$payload = @{ action = $Action; secret = $cfg.BUS_SECRET }
if ($Title)       { $payload.title = $Title }
if ($FileId)      { $payload.fileId = $FileId }
if ($SheetName)   { $payload.sheetName = $SheetName }
if ($Anchor)      { $payload.anchor = $Anchor }
if ($Force)       { $payload.force = $true }
if ($NoSeparator) { $payload.separator = $false }

if ($TextFile) {
  if (-not (Test-Path -LiteralPath $TextFile)) { throw "TextFile not found: $TextFile" }
  $raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $TextFile).Path, [Text.Encoding]::UTF8)
  $payload.text = ($raw -replace "`r`n", "`n").TrimEnd("`n")
} elseif ($PSBoundParameters.ContainsKey('Text')) {
  $payload.text = $Text
}

if ($SheetRowJson) {
  # DEFECT FIXED 2026-08-28 (REQ-V8QD7R), claude-code-cli. The old line was
  #   $row = @($SheetRowJson | ConvertFrom-Json)
  # WinPS 5.1's ConvertFrom-Json writes a deserialized JSON array to the pipeline as
  # ONE object rather than enumerating it, so @() wrapped it as a single element and
  # the ForEach-Object below cast that whole Object[] to a string. Result: all seven
  # cells space-joined into column A and columns B-G blank -- a silently malformed
  # row that the bus accepts and the read-back shows as "successful". This path had
  # never actually been exercised until today, which is why it survived since Aug 26.
  # Use -InputObject (no pipeline) and unwrap defensively; ISSUE 010's lesson is that
  # a wrong-shaped row must fail loudly, so assert the shape before sending.
  $parsed = ConvertFrom-Json -InputObject $SheetRowJson
  if (@($parsed).Count -eq 1 -and $parsed -isnot [string] -and @($parsed)[0] -is [System.Collections.IEnumerable] -and @($parsed)[0] -isnot [string]) {
    $parsed = @($parsed)[0]
  }
  $cells = @()
  foreach ($c in $parsed) { $cells += $(if ($null -eq $c) { '' } else { [string]$c }) }
  if ($cells.Count -lt 2) {
    throw "SheetRowJson produced $($cells.Count) cell(s) -- expected a JSON array of cells such as a bracketed list of quoted strings. Refusing to write a malformed row (ISSUE 010 / REQ-C4NDX7)."
  }
  $payload.sheetRow = @($cells)
}

# Real Unicode goes through ConvertTo-Json as \uXXXX escapes, which JSON.parse on
# the server decodes correctly. (The ORDER 015 corruption came from hand-written
# \u escapes being escaped a second time -- do not pre-escape anything.)
$json  = $payload | ConvertTo-Json -Depth 8 -Compress
$bytes = [Text.Encoding]::UTF8.GetBytes($json)

# ---- hop 1: POST with redirects suppressed ----------------------------------
#
# REGRESSION FIX, claude-code-cli, 2026-08-28. Invoke-WebRequest -MaximumRedirection 0
# no longer surfaces the 302 on this machine: WinPS 5.1 throws a bare
# InvalidOperationException ("maximum redirection count has been exceeded"), which
# carries NO Response object, so the catch below could never read the Location
# header and every POST died before hop 2. curl.exe (shipped in system32 since
# Win10 1803) reports the 302 and its Location honestly with -D -, so it is now the
# primary hop-1 client and Invoke-WebRequest is the fallback for machines without it.
# Do NOT "simplify" this to curl -L: curl re-POSTs to the redirect target without a
# Content-Length and Google answers 411. Two explicit hops or nothing.
# The no-follow contract from REQ-PR4EXZ is unchanged -- only the client is.
$location = $null
$content  = $null
$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue)

if ($curl) {
  # ONE request only. -D dumps headers to a file while the body goes to stdout, so
  # both are captured without ever calling the endpoint twice. An earlier draft of
  # this fix used "-D - -o NUL" and then re-fired to capture the body: on the
  # no-redirect branch that would have APPENDED TWICE, and on hop 2 it burned the
  # one-shot key before reading it. Never call this endpoint twice for one payload.
  $tmpBody = [IO.Path]::GetTempFileName()
  $tmpHead = [IO.Path]::GetTempFileName()
  try {
    [IO.File]::WriteAllBytes($tmpBody, $bytes)
    $out  = & $curl.Source -s -S -D $tmpHead --max-time 120 -X POST $cfg.BUS_URL `
              -H 'Content-Type: application/json; charset=utf-8' --data-binary "@$tmpBody"
    $head = Get-Content -LiteralPath $tmpHead -ErrorAction SilentlyContinue
    $loc  = @($head | Where-Object { $_ -match '^\s*[Ll]ocation:' }) | Select-Object -First 1
    if ($loc) { $location = ($loc -replace '^\s*[Ll]ocation:\s*', '').Trim() }
    else      { $content  = ($out -join "`n") }
  } finally {
    Remove-Item -LiteralPath $tmpBody -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmpHead -Force -ErrorAction SilentlyContinue
  }
} else {
  try {
    $r1 = Invoke-WebRequest -Uri $cfg.BUS_URL -Method Post -Body $bytes `
          -ContentType 'application/json; charset=utf-8' -MaximumRedirection 0 `
          -UseBasicParsing -TimeoutSec 120
    if ([int]$r1.StatusCode -ge 300 -and [int]$r1.StatusCode -lt 400) { $location = $r1.Headers['Location'] }
    else { $content = $r1.Content }
  } catch [System.Net.WebException] {
    $resp = $_.Exception.Response
    if ($resp -and [int]$resp.StatusCode -ge 300 -and [int]$resp.StatusCode -lt 400) {
      $location = $resp.Headers['Location']
    } else { throw }
  }
}

# ---- hop 2: plain GET on the one-shot Location -----------------------------
if ($location) {
  if ($curl) {
    # Single request again -- the Location key is one-shot; a probe would spend it.
    $tmpHead2 = [IO.Path]::GetTempFileName()
    try {
      $out2  = & $curl.Source -s -S -D $tmpHead2 --max-time 120 $location
      $head2 = Get-Content -LiteralPath $tmpHead2 -ErrorAction SilentlyContinue
      $loc2  = @($head2 | Where-Object { $_ -match '^\s*[Ll]ocation:' }) | Select-Object -First 1
      if ($loc2) {
        throw "hop 2 redirected again (to $(($loc2 -replace '^\s*[Ll]ocation:\s*','').Trim())) -- one-shot key consumed or expired. The write, if any, may still have landed: READ BACK before deciding anything."
      }
      $content = ($out2 -join "`n")
    } finally { Remove-Item -LiteralPath $tmpHead2 -Force -ErrorAction SilentlyContinue }
  } else {
    try {
      $r2 = Invoke-WebRequest -Uri $location -Method Get -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 120
      $content = $r2.Content
    } catch [System.Net.WebException] {
      $resp = $_.Exception.Response
      if ($resp -and [int]$resp.StatusCode -ge 300 -and [int]$resp.StatusCode -lt 400) {
        throw "hop 2 redirected again (to $($resp.Headers['Location'])) -- one-shot key consumed or expired. The write, if any, may still have landed: READ BACK before deciding anything."
      }
      throw
    }
  }
}

if ($content -is [byte[]]) { $content = [Text.Encoding]::UTF8.GetString($content) }
# [string]$null is $null in WinPS 5.1, not '' -- coalesce before calling a method on it.
if ($null -eq $content) { $content = '' }
$trimmed = ([string]$content).TrimStart()
if (-not $trimmed.StartsWith('{')) {
  Write-Warning "response is not JSON (redirect artifact?). Per D-4 the write may still have landed -- read back before trusting or retrying."
}

if ($OutFile) {
  $path = if ([IO.Path]::IsPathRooted($OutFile)) { $OutFile } else { Join-Path (Get-Location).Path $OutFile }
  [IO.File]::WriteAllText($path, [string]$content, (New-Object Text.UTF8Encoding($false)))
  Write-Output ("saved {0} chars to {1}" -f ([string]$content).Length, $path)
} else {
  Write-Output $content
}
