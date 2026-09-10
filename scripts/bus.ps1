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
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action append -Title "Claude Instance - check in sheet" -SheetRowJson '["claude-code-cli","Working","Instance unification","2026-08-26T19:00:00Z","","<targets>","<intent>"]'
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action replace -Title "SFDC24 — SESSION STATE · claude-code-cli" -TextFile state.txt   (bus v2+)
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bus.ps1 -Action list   (bus v2+)

RULES THIS SCRIPT DOES NOT RELAX
  D-4: read-back is the only proof of a write. This script prints the response; it
  verifies nothing. Never blind-retry an append -- read the target back first.
  ISSUE 010: for a Sheet, always send -SheetRowJson (a native array), never -Text.
  ISSUE 006: put timestamps in invariant UTC ISO-8601 form ending in Z.
#>
param(
  # 'upload' here is a DEPLOY PROBE, not a working uploader -- this client cannot
  # carry file bytes. Run `bus.ps1 -Action upload` with no other arguments after
  # redeploying Code.gs: "Unknown action: upload" means the live URL is still on
  # the old version (DEPLOY.md section 5 -- saving the editor does NOT redeploy,
  # and "New deployment" mints a DIFFERENT URL). Once the action is live the same
  # probe answers "base64 content required", which is the deploy landing.
  # The real uploader is scripts/glasses_capture.py --upload bus.
  [Parameter(Mandatory = $true)]
  [ValidateSet('ping', 'time', 'read', 'append', 'replace', 'list', 'upload')]
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
  [string]$EnvFile,
  [string]$ReadMetadataOutFile
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($ReadMetadataOutFile -and $Action -cne 'read') {
  throw 'ReadMetadataOutFile is available only for Action read'
}

function ConvertTo-BusContentTypeClass {
  param([AllowNull()][string]$ContentType)

  if ([string]::IsNullOrWhiteSpace($ContentType)) { return $null }
  $mediaType = @($ContentType -split ';', 2)[0].Trim().ToLowerInvariant()
  return $(switch -Regex ($mediaType) {
    '^application/(?:[a-z0-9.+-]+\+)?json$' { 'json'; break }
    '^text/html$' { 'html'; break }
    '^text/' { 'text'; break }
    '^application/octet-stream$' { 'binary'; break }
    default { 'other' }
  })
}

function Get-BusHeaderMetadata {
  param([AllowNull()][object[]]$HeaderLines)

  $status = $null
  $contentTypeClass = $null
  foreach ($line in @($HeaderLines)) {
    $text = [string]$line
    if ($text -match '^\s*HTTP/\S+\s+([1-5]\d{2})(?:\s|$)') {
      $status = [int]$matches[1]
      $contentTypeClass = $null
      continue
    }
    if ($text -match '^\s*[Cc]ontent-[Tt]ype\s*:\s*(.+)$') {
      $contentTypeClass = ConvertTo-BusContentTypeClass -ContentType ([string]$matches[1])
    }
  }
  return [pscustomobject][ordered]@{
    http_status = $status
    content_type_class = $contentTypeClass
  }
}

function Write-BusReadMetadata {
  param(
    [AllowNull()][string]$Path,
    [AllowNull()]$TransportExit,
    [AllowNull()]$HttpStatus,
    [AllowNull()][string]$ContentTypeClass
  )

  if ([string]::IsNullOrWhiteSpace($Path)) { return }
  $metadata = [ordered]@{}
  if ($null -ne $TransportExit -and [string]$TransportExit -cmatch '^-?\d{1,10}$') {
    $metadata.transport_exit = [int]$TransportExit
  }
  if ($null -ne $HttpStatus -and [string]$HttpStatus -cmatch '^[1-5]\d{2}$') {
    $metadata.http_status = [int]$HttpStatus
  }
  if (@('json', 'html', 'text', 'binary', 'other') -ccontains $ContentTypeClass) {
    $metadata.content_type_class = $ContentTypeClass
  }
  $metadataPath = if ([IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path (Get-Location).Path $Path }
  $metadataJson = $metadata | ConvertTo-Json -Compress
  [IO.File]::WriteAllText($metadataPath, $metadataJson, (New-Object Text.UTF8Encoding($false)))
}

function Resolve-BusHttpsLocation {
  param(
    [Parameter(Mandatory = $true)][string]$Location,
    [Parameter(Mandatory = $true)][string]$BaseUrl
  )

  if ([string]::IsNullOrWhiteSpace($Location) -or [string]::IsNullOrWhiteSpace($BaseUrl)) {
    throw [System.InvalidOperationException]::new(
      'hop 2 Location or BUS_URL is empty. Refusing transfer; the write, if any, may still have landed: READ BACK before deciding anything.'
    )
  }
  try {
    $baseUri = [Uri]::new($BaseUrl)
    $resolvedUri = [Uri]::new($baseUri, $Location)
  } catch {
    throw [System.InvalidOperationException]::new(
      'hop 2 Location or BUS_URL is invalid. Refusing transfer; the write, if any, may still have landed: READ BACK before deciding anything.'
    )
  }
  if (-not $resolvedUri.IsAbsoluteUri -or $resolvedUri.Scheme -cne [Uri]::UriSchemeHttps) {
    throw [System.InvalidOperationException]::new(
      'hop 2 Location must resolve to HTTPS. Refusing transfer; the write, if any, may still have landed: READ BACK before deciding anything.'
    )
  }
  return $resolvedUri.AbsoluteUri
}

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
  # Parse the array without pipeline enumeration and unwrap defensively; ISSUE
  # 010's lesson is that a wrong-shaped row must fail loudly, so assert the shape
  # before sending.
  # ConvertFrom-Json in newer PowerShell versions eagerly turns ISO-8601 strings
  # into DateTime values. Casting those values back to strings below drops the
  # trailing Z and Google Sheets then interprets them in the spreadsheet's local
  # timezone. The resulting cell is shifted when handleRead_ serializes it again
  # (for example 06:53Z came back as 10:53Z). The sheetRow contract is a flat JSON
  # array, so deserialize it explicitly as object[] to preserve date-looking cells
  # as strings on both Windows PowerShell 5.1 and PowerShell 7.
  Add-Type -AssemblyName System.Runtime.Serialization
  $jsonBytes = [Text.Encoding]::UTF8.GetBytes($SheetRowJson)
  $jsonStream = New-Object IO.MemoryStream(,$jsonBytes)
  try {
    $jsonReader = [System.Runtime.Serialization.Json.DataContractJsonSerializer]::new([object[]])
    $parsed = $jsonReader.ReadObject($jsonStream)
  } finally {
    $jsonStream.Dispose()
  }
  if (@($parsed).Count -eq 1 -and $parsed -isnot [string] -and @($parsed)[0] -is [System.Collections.IEnumerable] -and @($parsed)[0] -isnot [string]) {
    $parsed = @($parsed)[0]
  }
  $cells = @()
  foreach ($c in $parsed) { $cells += $(if ($null -eq $c) { '' } else { [string]$c }) }
  if ($cells.Count -lt 2) {
    throw "SheetRowJson produced $($cells.Count) cell(s) -- expected a JSON array of cells such as a bracketed list of quoted strings. Refusing to write a malformed row (ISSUE 010 / REQ-C4NDX7)."
  }

  # A canonical Blackboard control-bus full row is exactly A:J, with its timestamp
  # in cell B and a BCB envelope in cell F. Read-side blank K:L padding, truncated
  # rows, and shifted envelopes are not write shapes. Reject them before
  # serialization or transport instead of allowing a different width to bypass
  # this guard. The self-hosted server supports an eight-content-cell shorthand,
  # but Apps Script v1 appends those eight cells verbatim; this portable client
  # therefore cannot opt into that backend-specific behavior implicitly.
  # Non-BCB rows sent to other sheets keep their existing compatibility behavior.
  $isAlphaBoardTitle = [string]::Equals(
    [string]$Title,
    'Blackboard - Alpha DB',
    [StringComparison]::Ordinal
  )
  $hasBcbEnvelope = $false
  foreach ($cell in $cells) {
    if (([string]$cell).StartsWith('BCB|', [StringComparison]::Ordinal)) {
      $hasBcbEnvelope = $true
      break
    }
  }
  $isFullBcbRow = $cells.Count -gt 5 -and
    ([string]$cells[5]).StartsWith('BCB|', [StringComparison]::Ordinal)
  $isCanonicalBcbRow = $cells.Count -eq 10 -and $isFullBcbRow
  if (($isAlphaBoardTitle -and $cells.Count -ne 10) -or
      ($hasBcbEnvelope -and -not $isCanonicalBcbRow)) {
    throw [InvalidOperationException]::new(
      'BOARD_ROW_SHAPE_INVALID: Blackboard BCB writes require exactly 10 logical A:J cells with the envelope in cell F (index 5). Read-side padding, truncated rows, shifted rows, and backend-specific shorthand are not portable write shapes. Refusing transport.'
    )
  }
  if ($isCanonicalBcbRow) {
    $boardTimestamp = [string]$cells[1]
    $boardTimestampPattern = '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,7})?Z$'
    if ($boardTimestamp -cnotmatch $boardTimestampPattern) {
      throw [InvalidOperationException]::new(
        'BOARD_TIMESTAMP_INVALID: canonical 10-cell BCB rows require invariant UTC ISO-8601 in cell B (index 1), ending in uppercase Z with zero to seven fractional digits. Refusing transport.'
      )
    }

    $boardTimestampFormats = [string[]]@(
      "yyyy-MM-dd'T'HH:mm:ss'Z'",
      "yyyy-MM-dd'T'HH:mm:ss.f'Z'",
      "yyyy-MM-dd'T'HH:mm:ss.ff'Z'",
      "yyyy-MM-dd'T'HH:mm:ss.fff'Z'",
      "yyyy-MM-dd'T'HH:mm:ss.ffff'Z'",
      "yyyy-MM-dd'T'HH:mm:ss.fffff'Z'",
      "yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'",
      "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'"
    )
    $parsedBoardTimestamp = [DateTimeOffset]::MinValue
    $boardTimestampStyles = [Globalization.DateTimeStyles]::AssumeUniversal -bor
      [Globalization.DateTimeStyles]::AdjustToUniversal
    if (-not [DateTimeOffset]::TryParseExact(
      $boardTimestamp,
      $boardTimestampFormats,
      [Globalization.CultureInfo]::InvariantCulture,
      $boardTimestampStyles,
      [ref]$parsedBoardTimestamp
    )) {
      throw [InvalidOperationException]::new(
        'BOARD_TIMESTAMP_INVALID: canonical 10-cell BCB rows require a real invariant UTC calendar timestamp in cell B (index 1). Refusing transport.'
      )
    }
    if ($parsedBoardTimestamp -gt [DateTimeOffset]::UtcNow.AddMinutes(2)) {
      throw [InvalidOperationException]::new(
        'BOARD_TIMESTAMP_FUTURE: canonical 10-cell BCB row timestamp is more than two minutes ahead of the local UTC clock. Refusing transport.'
      )
    }
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
# curl.exe on Windows, curl on Linux and macOS. Looking only for curl.exe meant
# the PREFERRED, well-tested two-hop path was silently unavailable off Windows,
# and every read fell through to the Invoke-WebRequest fallback - which then
# failed on the 302 for a different reason entirely (see the catch below).
#
# -CommandType Application is load-bearing, not tidiness. In Windows PowerShell
# 5.1 `curl` is an ALIAS FOR Invoke-WebRequest, so a bare `Get-Command curl`
# resolves to the very cmdlet this branch exists to avoid, and the "curl path"
# would quietly be the fallback path wearing its name.
$curl = $null
foreach ($candidate in @('curl.exe', 'curl')) {
    $found = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($found) { $curl = $found; break }
}
$readTransportExit = $null
$readHttpStatus = $null
$readContentTypeClass = $null

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
    $readTransportExit = [int]$LASTEXITCODE
    $head = Get-Content -LiteralPath $tmpHead -ErrorAction SilentlyContinue
    $hop1Metadata = Get-BusHeaderMetadata -HeaderLines @($head)
    $readHttpStatus = $hop1Metadata.http_status
    $readContentTypeClass = $hop1Metadata.content_type_class
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
    $readHttpStatus = [int]$r1.StatusCode
    $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType ([string]$r1.Headers['Content-Type'])
    if ([int]$r1.StatusCode -ge 300 -and [int]$r1.StatusCode -lt 400) { $location = $r1.Headers['Location'] }
    else { $content = $r1.Content }
  } catch {
    # TWO EDITIONS THROW TWO DIFFERENT TYPES for the same 302.
    #
    # Windows PowerShell 5.1 raises System.Net.WebException carrying an
    # HttpWebResponse, whose headers are indexed like a dictionary. PowerShell 7
    # raises Microsoft.PowerShell.Commands.HttpResponseException carrying an
    # HttpResponseMessage, whose headers are a typed collection and whose
    # StatusCode is an enum. Catching only WebException meant that on
    # PowerShell 7 the 302 this contract DEPENDS ON escaped as an unhandled
    # error and every read died before hop 2 - the same failure mode the 2026-08-28
    # regression fix above describes, arriving by a different route.
    $resp = $_.Exception.Response
    if (-not $resp) { throw }

    $status = 0
    try { $status = [int]$resp.StatusCode } catch { $status = 0 }

    $contentType = ''
    $locationValue = $null
    if ($resp.PSObject.Properties['Headers'] -and $resp.Headers) {
      try {
        # HttpResponseMessage: typed headers.
        if ($resp.Headers -is [System.Net.Http.Headers.HttpResponseHeaders]) {
          if ($resp.Headers.Location) { $locationValue = [string]$resp.Headers.Location }
          if ($resp.Content -and $resp.Content.Headers -and $resp.Content.Headers.ContentType) {
            $contentType = [string]$resp.Content.Headers.ContentType
          }
        } else {
          # HttpWebResponse: dictionary-style indexing.
          $locationValue = [string]$resp.Headers['Location']
          $contentType = [string]$resp.Headers['Content-Type']
        }
      } catch { }
    }

    if ($status) { $readHttpStatus = $status }
    if ($contentType) { $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType $contentType }
    if ($status -ge 300 -and $status -lt 400 -and $locationValue) {
      $location = $locationValue
    } else { throw }
  }
}

# ---- hop 2: plain GET on the one-shot Location -----------------------------
if ($location) {
  try {
    # Validate before choosing a transport so the IWR fallback cannot issue its
    # initial hop-2 request to a plaintext or non-web scheme.
    $location = Resolve-BusHttpsLocation -Location ([string]$location) -BaseUrl ([string]$cfg.BUS_URL)
  } catch {
    Write-BusReadMetadata `
      -Path $ReadMetadataOutFile `
      -TransportExit $readTransportExit `
      -HttpStatus $readHttpStatus `
      -ContentTypeClass $readContentTypeClass
    throw
  }
  if ($curl) {
    # Single request again -- the Location key is one-shot; a probe would spend it.
    # This leg is a bodyless GET, so redirects are safe to follow. Google may add
    # another redirect from the one-shot URL to the canonical /exec endpoint; a
    # Location header on this leg is therefore not evidence that the key expired.
    $hop2MaxRedirects = 5
    $tmpHead2 = [IO.Path]::GetTempFileName()
    try {
      # Keep both the server-supplied hop-2 URL and every redirect HTTPS-only.
      # The leading '=' replaces curl's default protocol set; without it, 'https'
      # would be added to (rather than replace) the protocols curl already allows.
      $out2  = & $curl.Source -s -S -L --max-redirs $hop2MaxRedirects `
                --proto '=https' --proto-redir '=https' `
                -D $tmpHead2 --max-time 120 $location
      $hop2Exit = [int]$LASTEXITCODE
      if ($null -eq $readTransportExit -or $hop2Exit -ne 0) { $readTransportExit = $hop2Exit }
      $head2 = Get-Content -LiteralPath $tmpHead2 -ErrorAction SilentlyContinue
      $hop2Metadata = Get-BusHeaderMetadata -HeaderLines @($head2)
      $readHttpStatus = $hop2Metadata.http_status
      $readContentTypeClass = $hop2Metadata.content_type_class
      if ($hop2Exit -ne 0 -or $null -eq $readHttpStatus -or
          $readHttpStatus -lt 200 -or $readHttpStatus -ge 300) {
        # Preserve the sanitized sidecar even though the standalone client fails.
        # The supervisor uses it to distinguish a retryable transport/HTTP fault
        # from a deterministic local-client error without logging response data.
        Write-BusReadMetadata `
          -Path $ReadMetadataOutFile `
          -TransportExit $readTransportExit `
          -HttpStatus $readHttpStatus `
          -ContentTypeClass $readContentTypeClass
        throw "hop 2 did not reach a successful final response after following up to $hop2MaxRedirects redirects. The write, if any, may still have landed: READ BACK before deciding anything."
      }
      $content = ($out2 -join "`n")
    } finally { Remove-Item -LiteralPath $tmpHead2 -Force -ErrorAction SilentlyContinue }
  } else {
    # Do not carry hop 1's 302 metadata into a hop 2 failure that produced no
    # response. A response-less WebException must remain a transport failure.
    # WinPS 5.1/.NET Framework has no per-hop scheme policy for automatic
    # redirects, so this fallback fails closed on any further redirect. The
    # normal curl path above is the only path that follows the bounded HTTPS
    # Google chain.
    $readHttpStatus = $null
    $readContentTypeClass = $null
    $r2 = $null
    try {
      $r2 = Invoke-WebRequest -Uri $location -Method Get -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 120
    } catch [System.Net.WebException] {
      $resp = $_.Exception.Response
      if ($resp) {
        $readHttpStatus = [int]$resp.StatusCode
        $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType ([string]$resp.Headers['Content-Type'])
      }
      Write-BusReadMetadata `
        -Path $ReadMetadataOutFile `
        -TransportExit $readTransportExit `
        -HttpStatus $readHttpStatus `
        -ContentTypeClass $readContentTypeClass
      if ($resp -and [int]$resp.StatusCode -ge 300 -and [int]$resp.StatusCode -lt 400) {
        throw [System.Net.WebException]::new(
          'hop 2 returned another redirect, which the IWR fallback refuses to follow. The write, if any, may still have landed: READ BACK before deciding anything.'
        )
      }
      throw [System.Net.WebException]::new(
        'hop 2 did not reach a successful final response. The write, if any, may still have landed: READ BACK before deciding anything.'
      )
    } catch {
      Write-BusReadMetadata `
        -Path $ReadMetadataOutFile `
        -TransportExit $readTransportExit `
        -HttpStatus $readHttpStatus `
        -ContentTypeClass $readContentTypeClass
      throw [System.Net.WebException]::new(
        'hop 2 did not reach a successful final response. The write, if any, may still have landed: READ BACK before deciding anything.'
      )
    }
    $readHttpStatus = [int]$r2.StatusCode
    $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType ([string]$r2.Headers['Content-Type'])
    if ($readHttpStatus -lt 200 -or $readHttpStatus -ge 300) {
      Write-BusReadMetadata `
        -Path $ReadMetadataOutFile `
        -TransportExit $readTransportExit `
        -HttpStatus $readHttpStatus `
        -ContentTypeClass $readContentTypeClass
      if ($readHttpStatus -ge 300 -and $readHttpStatus -lt 400) {
        throw [System.Net.WebException]::new(
          'hop 2 returned another redirect, which the IWR fallback refuses to follow. The write, if any, may still have landed: READ BACK before deciding anything.'
        )
      }
      throw [System.Net.WebException]::new(
        'hop 2 did not reach a successful final response. The write, if any, may still have landed: READ BACK before deciding anything.'
      )
    }
    $content = $r2.Content
  }
}

Write-BusReadMetadata `
  -Path $ReadMetadataOutFile `
  -TransportExit $readTransportExit `
  -HttpStatus $readHttpStatus `
  -ContentTypeClass $readContentTypeClass

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
