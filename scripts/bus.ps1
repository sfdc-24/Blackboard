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
if ($ReadMetadataOutFile -and $Action -ne 'read') {
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

function Get-BusIwrResponseBytes {
  param([Parameter(Mandatory = $true)]$Response)

  $streamProperty = $Response.PSObject.Properties['RawContentStream']
  if ($null -eq $streamProperty -or $null -eq $streamProperty.Value) {
    throw [InvalidOperationException]::new('BUS_IWR_RAW_RESPONSE_UNAVAILABLE')
  }

  $stream = $streamProperty.Value
  if ($stream -is [IO.MemoryStream]) {
    return ,([byte[]]$stream.ToArray())
  }
  if (-not $stream.CanRead -or -not $stream.CanSeek) {
    throw [InvalidOperationException]::new('BUS_IWR_RAW_RESPONSE_UNAVAILABLE')
  }

  $savedPosition = $stream.Position
  $copy = New-Object IO.MemoryStream
  try {
    $stream.Position = 0
    $stream.CopyTo($copy)
    return ,([byte[]]$copy.ToArray())
  } finally {
    $stream.Position = $savedPosition
    $copy.Dispose()
  }
}

function ConvertFrom-BusStrictJsonStringToken {
  param([Parameter(Mandatory = $true)][string]$Token)

  # The strict lexer has already proved this token is a complete RFC JSON
  # string. Decode only root property names so escaped spellings participate in
  # the same collision set without copying multi-megabyte response values.
  $builder = New-Object Text.StringBuilder
  for ($index = 1; $index -lt $Token.Length - 1; $index++) {
    $character = $Token[$index]
    if ($character -cne '\') {
      [void]$builder.Append($character)
      continue
    }

    $index++
    $escape = $Token[$index]
    switch -CaseSensitive ($escape) {
      '"' { [void]$builder.Append('"') }
      '\' { [void]$builder.Append('\') }
      '/'  { [void]$builder.Append('/') }
      'b'  { [void]$builder.Append([char]0x08) }
      'f'  { [void]$builder.Append([char]0x0C) }
      'n'  { [void]$builder.Append([char]0x0A) }
      'r'  { [void]$builder.Append([char]0x0D) }
      't'  { [void]$builder.Append([char]0x09) }
      'u'  {
        $hex = $Token.Substring($index + 1, 4)
        [void]$builder.Append([char][Convert]::ToUInt16($hex, 16))
        $index += 4
      }
    }
  }
  return $builder.ToString()
}

function Assert-BusStrictJsonRootObject {
  param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content)

  # ConvertFrom-Json accepts different JavaScript extensions on Desktop and Core.
  # Tokenize every character with the RFC 8259 lexical grammar first; a gap is a
  # forbidden comment, quote, identifier, number form, whitespace character, or
  # other extension. An iterative state machine owns the container grammar and
  # decodes only depth-one property names for collision tracking. Platform JSON
  # readers are intentionally not trusted here: ConvertFrom-Json is permissive,
  # while JsonReaderWriterFactory admits missing separators and hides __type as
  # an XML attribute. This avoids parser/scanner disagreement and a PowerShell
  # loop over every byte of large response values.
  $tokenPattern = '(?<string>"(?:\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4})|[^"\\\x00-\x1F])*")|(?<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?(?![0-9A-Za-z_.+-]))|(?<literal>true(?![A-Za-z0-9_])|false(?![A-Za-z0-9_])|null(?![A-Za-z0-9_]))|(?<punct>[{}\[\],:])|(?<ws>[ \t\r\n]+)'
  $tokenRegex = New-Object Text.RegularExpressions.Regex(
    $tokenPattern,
    [Text.RegularExpressions.RegexOptions]::CultureInvariant,
    [TimeSpan]::FromSeconds(10)
  )
  $cursor = 0
  $depth = 0
  $rootComplete = $false
  # Object states: 0 key-or-end, 1 colon, 2 value, 3 comma-or-end,
  # 4 key-after-comma. Array states: 0 value-or-end, 2 value-after-comma,
  # 3 comma-or-end. A parent becomes complete-as-a-value when a child opens;
  # the child still has to close before another parent token can be consumed.
  $containerTypes = New-Object 'string[]' 128
  $containerStates = New-Object 'int[]' 128
  $rootPropertyNames = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
  )
  foreach ($match in $tokenRegex.Matches($Content)) {
    if ($match.Index -ne $cursor) {
      throw [FormatException]::new('invalid strict JSON token')
    }
    $cursor += $match.Length
    if ($match.Groups['ws'].Success) { continue }

    $token = $match.Value
    if ($rootComplete) {
      throw [FormatException]::new('strict JSON trailing content')
    }
    if ($depth -eq 0) {
      if ($token -cne '{') {
        throw [FormatException]::new('strict JSON root object expected')
      }
      $containerTypes[0] = '{'
      $containerStates[0] = 0
      $depth = 1
      continue
    }

    $isScalar = $match.Groups['string'].Success -or
      $match.Groups['number'].Success -or
      $match.Groups['literal'].Success
    $top = $depth - 1
    $state = $containerStates[$top]
    if ($containerTypes[$top] -ceq '{') {
      if ($state -eq 0 -or $state -eq 4) {
        if ($match.Groups['string'].Success) {
          if ($depth -eq 1) {
            $propertyName = ConvertFrom-BusStrictJsonStringToken -Token $token
            if (-not $rootPropertyNames.Add($propertyName)) {
              throw [FormatException]::new('colliding strict JSON root property')
            }
          }
          $containerStates[$top] = 1
        } elseif ($state -eq 0 -and $token -ceq '}') {
          $depth--
          if ($depth -eq 0) { $rootComplete = $true }
        } else {
          throw [FormatException]::new('strict JSON object property expected')
        }
      } elseif ($state -eq 1) {
        if ($token -cne ':') {
          throw [FormatException]::new('strict JSON property colon expected')
        }
        $containerStates[$top] = 2
      } elseif ($state -eq 2) {
        if ($isScalar) {
          $containerStates[$top] = 3
        } elseif ($token -ceq '{' -or $token -ceq '[') {
          $containerStates[$top] = 3
          if ($depth -ge $containerTypes.Length) {
            throw [FormatException]::new('strict JSON nesting limit')
          }
          $containerTypes[$depth] = $token
          $containerStates[$depth] = 0
          $depth++
        } else {
          throw [FormatException]::new('strict JSON property value expected')
        }
      } elseif ($state -eq 3) {
        if ($token -ceq ',') {
          $containerStates[$top] = 4
        } elseif ($token -ceq '}') {
          $depth--
          if ($depth -eq 0) { $rootComplete = $true }
        } else {
          throw [FormatException]::new('strict JSON object separator expected')
        }
      } else {
        throw [FormatException]::new('invalid strict JSON object state')
      }
    } else {
      if ($state -eq 0 -or $state -eq 2) {
        if ($state -eq 0 -and $token -ceq ']') {
          $depth--
          if ($depth -eq 0) { $rootComplete = $true }
        } elseif ($isScalar) {
          $containerStates[$top] = 3
        } elseif ($token -ceq '{' -or $token -ceq '[') {
          $containerStates[$top] = 3
          if ($depth -ge $containerTypes.Length) {
            throw [FormatException]::new('strict JSON nesting limit')
          }
          $containerTypes[$depth] = $token
          $containerStates[$depth] = 0
          $depth++
        } else {
          throw [FormatException]::new('strict JSON array value expected')
        }
      } elseif ($state -eq 3) {
        if ($token -ceq ',') {
          $containerStates[$top] = 2
        } elseif ($token -ceq ']') {
          $depth--
          if ($depth -eq 0) { $rootComplete = $true }
        } else {
          throw [FormatException]::new('strict JSON array separator expected')
        }
      } else {
        throw [FormatException]::new('invalid strict JSON array state')
      }
    }
  }
  if ($cursor -ne $Content.Length -or -not $rootComplete -or $depth -ne 0) {
    throw [FormatException]::new('incomplete strict JSON root object')
  }
}

function Get-BusExactJsonProperty {
  param(
    [Parameter(Mandatory = $true)]$InputObject,
    [Parameter(Mandatory = $true)][string]$Name
  )

  foreach ($property in @($InputObject.PSObject.Properties)) {
    if ([string]::Equals([string]$property.Name, $Name, [StringComparison]::Ordinal)) {
      return $property
    }
  }
  return $null
}

function Test-BusJsonHttpStatus {
  param([AllowNull()]$Value)

  if ($null -eq $Value -or $Value -is [bool]) { return $false }
  $typeCode = [Type]::GetTypeCode($Value.GetType())
  if (@(
      [TypeCode]::Byte,
      [TypeCode]::SByte,
      [TypeCode]::Int16,
      [TypeCode]::UInt16,
      [TypeCode]::Int32,
      [TypeCode]::UInt32,
      [TypeCode]::Int64,
      [TypeCode]::UInt64,
      [TypeCode]::Single,
      [TypeCode]::Double,
      [TypeCode]::Decimal
    ) -notcontains $typeCode) {
    return $false
  }

  try { $number = [double]$Value } catch { return $false }
  return -not [double]::IsNaN($number) -and
    -not [double]::IsInfinity($number) -and
    $number -eq [Math]::Truncate($number) -and
    $number -ge 200 -and
    $number -lt 300
}

# READ responses fail closed before either stdout or OutFile. Apps Script returns
# logical failures inside HTTP 200, and its bare GET health object is JSON with
# ok=true, so transport success and a leading "{" are not read success. Keep this
# gate generic: identity plus exactly one typed sheet/doc payload is the portable
# contract shared by the Apps Script and self-hosted buses. Never add board headers,
# row widths, timestamps, or other Blackboard-specific admission rules here.
function Assert-BusReadResponseContract {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content,
    [AllowNull()][string]$RequestedTitle,
    [AllowNull()][string]$RequestedFileId
  )

  $invalidMessage = 'BUS_READ_RESPONSE_INVALID: read response failed the generic success, identity, or payload contract.'
  try {
    # ConvertFrom-Json is neither a strict JSON grammar nor a root-shape oracle
    # across editions. Validate one complete RFC-JSON root object and its unique
    # decoded root-property names before the edition-specific deserializer runs.
    Assert-BusStrictJsonRootObject -Content $Content
    # PowerShell 7 otherwise turns ISO-8601-looking JSON strings into DateTime.
    # That changes legitimate document text and identity values before the type
    # and ordinal checks below. DateKind arrived in 7.5; Desktop 5.1 already
    # preserves strings. Read validation is therefore supported on Desktop 5.1
    # and Core 7.5+, while older Core editions deliberately fail closed.
    $jsonCommand = Get-Command ConvertFrom-Json
    if ($jsonCommand.Parameters.ContainsKey('DateKind')) {
      $response = ConvertFrom-Json -InputObject $Content -DateKind String -ErrorAction Stop
    } elseif ($PSVersionTable.PSEdition -ceq 'Desktop') {
      $response = ConvertFrom-Json -InputObject $Content -ErrorAction Stop
    } else {
      throw [NotSupportedException]::new('BUS_JSON_DATE_COERCION_UNSAFE')
    }
    if ($null -eq $response -or
        $response -isnot [System.Management.Automation.PSCustomObject]) {
      throw [InvalidOperationException]::new($invalidMessage)
    }

    $okProperty = Get-BusExactJsonProperty -InputObject $response -Name 'ok'
    if ($null -eq $okProperty -or
        $okProperty.Value -isnot [bool] -or
        $okProperty.Value -ne $true) {
      throw [InvalidOperationException]::new($invalidMessage)
    }

    $logicalStatusProperty = Get-BusExactJsonProperty -InputObject $response -Name '_httpStatus'
    if ($null -ne $logicalStatusProperty -and
        -not (Test-BusJsonHttpStatus -Value $logicalStatusProperty.Value)) {
      throw [InvalidOperationException]::new($invalidMessage)
    }

    $fileIdProperty = Get-BusExactJsonProperty -InputObject $response -Name 'fileId'
    $titleProperty = Get-BusExactJsonProperty -InputObject $response -Name 'title'
    if ($null -eq $fileIdProperty -or
        $fileIdProperty.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$fileIdProperty.Value) -or
        $null -eq $titleProperty -or
        $titleProperty.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$titleProperty.Value)) {
      throw [InvalidOperationException]::new($invalidMessage)
    }

    if ($RequestedTitle -and
        -not [string]::Equals(
          [string]$titleProperty.Value,
          $RequestedTitle,
          [StringComparison]::Ordinal
        )) {
      throw [InvalidOperationException]::new($invalidMessage)
    }
    if ($RequestedFileId -and
        -not [string]::Equals(
          [string]$fileIdProperty.Value,
          $RequestedFileId,
          [StringComparison]::Ordinal
        )) {
      throw [InvalidOperationException]::new($invalidMessage)
    }

    $rowsProperty = Get-BusExactJsonProperty -InputObject $response -Name 'rows'
    $textProperty = Get-BusExactJsonProperty -InputObject $response -Name 'text'
    $bodyProperty = Get-BusExactJsonProperty -InputObject $response -Name 'body'
    $payloadProperties = @(
      @($rowsProperty, $textProperty, $bodyProperty) |
        Where-Object { $null -ne $_ }
    )
    if ($payloadProperties.Count -ne 1) {
      throw [InvalidOperationException]::new($invalidMessage)
    }

    if ($null -ne $rowsProperty) {
      if ($rowsProperty.Value -isnot [Array]) {
        throw [InvalidOperationException]::new($invalidMessage)
      }
      foreach ($row in @($rowsProperty.Value)) {
        if ($row -isnot [Array]) {
          throw [InvalidOperationException]::new($invalidMessage)
        }
      }
    } elseif ($null -ne $textProperty) {
      if ($textProperty.Value -isnot [string]) {
        throw [InvalidOperationException]::new($invalidMessage)
      }
    } elseif ($bodyProperty.Value -isnot [string]) {
      throw [InvalidOperationException]::new($invalidMessage)
    }
  } catch {
    throw [InvalidOperationException]::new($invalidMessage)
  }
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

# ---- fail-closed read preflight (no credential load, request, or file write) --
if ($Action -eq 'read') {
  if ([string]::IsNullOrWhiteSpace($Title) -and
      [string]::IsNullOrWhiteSpace($FileId)) {
    throw [InvalidOperationException]::new(
      'BUS_READ_SELECTOR_REQUIRED: Action read requires Title or FileId.'
    )
  }
  if ($OutFile -and $ReadMetadataOutFile) {
    $resolvedOutFile = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($OutFile)) {
      $OutFile
    } else {
      Join-Path (Get-Location).Path $OutFile
    }))
    $resolvedMetadataFile = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($ReadMetadataOutFile)) {
      $ReadMetadataOutFile
    } else {
      Join-Path (Get-Location).Path $ReadMetadataOutFile
    }))
    $pathComparison = if ([IO.Path]::DirectorySeparatorChar -ceq '\') {
      [StringComparison]::OrdinalIgnoreCase
    } else {
      [StringComparison]::Ordinal
    }
    if ([string]::Equals($resolvedOutFile, $resolvedMetadataFile, $pathComparison)) {
      throw [InvalidOperationException]::new(
        'BUS_READ_OUTPUT_PATH_CONFLICT: OutFile and ReadMetadataOutFile must identify different files.'
      )
    }
  }
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
$wireAction = if ($Action -eq 'read') { 'read' } else { $Action }
$payload = @{ action = $wireAction; secret = $cfg.BUS_SECRET }
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
$contentBytes = $null
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
$isWindowsHost = $PSVersionTable.PSEdition -ceq 'Desktop'
if (-not $isWindowsHost) {
  $isWindowsVariable = Get-Variable -Name IsWindows -ErrorAction SilentlyContinue
  if ($isWindowsVariable) { $isWindowsHost = [bool]$isWindowsVariable.Value }
}
# WSL inherits Windows PATH entries and can resolve curl.exe even though that
# process cannot open Linux /tmp response/request files. Prefer the native name
# for the current host; retain the other name only as a compatibility fallback.
$curlCandidates = if ($isWindowsHost) { @('curl.exe', 'curl') } else { @('curl', 'curl.exe') }
foreach ($candidate in $curlCandidates) {
    $found = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($found) { $curl = $found; break }
}
$readTransportExit = $null
$readHttpStatus = $null
$readContentTypeClass = $null
$readIwrResponse = $null

if ($curl) {
  # ONE request only. -D dumps headers to a file; reads also put the response body
  # in a file so its bytes survive validation and OutFile unchanged. Other actions
  # retain the stdout behavior. An earlier draft of
  # this fix used "-D - -o NUL" and then re-fired to capture the body: on the
  # no-redirect branch that would have APPENDED TWICE, and on hop 2 it burned the
  # one-shot key before reading it. Never call this endpoint twice for one payload.
  $tmpBody = [IO.Path]::GetTempFileName()
  $tmpHead = [IO.Path]::GetTempFileName()
  $tmpReadResponse = $null
  try {
    [IO.File]::WriteAllBytes($tmpBody, $bytes)
    if ($Action -eq 'read') {
      # Native stdout is line-oriented in Windows PowerShell and loses a final
      # newline. Capture read bodies to a file so an accepted response can be
      # written to OutFile byte-for-byte, without reserialization or newline loss.
      $tmpReadResponse = [IO.Path]::GetTempFileName()
      # WinPS 5.1 promotes a native stderr record to a terminating error under
      # the script-wide Stop preference before LASTEXITCODE can be inspected.
      # A failed read must still write its sanitized metadata sidecar, so suppress
      # only this native diagnostic and capture the numeric exit explicitly.
      $savedErrorActionPreference = $ErrorActionPreference
      try {
        $ErrorActionPreference = 'Continue'
        $out = & $curl.Source -s -S -D $tmpHead -o $tmpReadResponse --max-time 120 `
                 -X POST $cfg.BUS_URL -H 'Content-Type: application/json; charset=utf-8' `
                 --data-binary "@$tmpBody" 2>$null
        $hop1Exit = [int]$LASTEXITCODE
      } finally {
        $ErrorActionPreference = $savedErrorActionPreference
      }
    } else {
      $out = & $curl.Source -s -S -D $tmpHead --max-time 120 -X POST $cfg.BUS_URL `
               -H 'Content-Type: application/json; charset=utf-8' --data-binary "@$tmpBody"
      $hop1Exit = [int]$LASTEXITCODE
    }
    $readTransportExit = $hop1Exit
    $head = Get-Content -LiteralPath $tmpHead -ErrorAction SilentlyContinue
    $hop1Metadata = Get-BusHeaderMetadata -HeaderLines @($head)
    $readHttpStatus = $hop1Metadata.http_status
    $readContentTypeClass = $hop1Metadata.content_type_class
    $loc  = @($head | Where-Object { $_ -match '^\s*[Ll]ocation:' }) | Select-Object -First 1
    if ($loc) {
      $location = ($loc -replace '^\s*[Ll]ocation:\s*', '').Trim()
    } elseif ($Action -eq 'read') {
      $contentBytes = [IO.File]::ReadAllBytes($tmpReadResponse)
    } else {
      $content = ($out -join "`n")
    }
  } finally {
    Remove-Item -LiteralPath $tmpBody -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmpHead -Force -ErrorAction SilentlyContinue
    if ($tmpReadResponse) {
      Remove-Item -LiteralPath $tmpReadResponse -Force -ErrorAction SilentlyContinue
    }
  }
} else {
  # THE 302 IS NOT AN EXCEPTION ON 5.1 UNLESS WE MAKE IT ONE.
  #
  # Measured on Windows PowerShell 5.1.26100.9444 against a local HttpListener
  # returning a 302, which is the same shape the bus returns:
  #
  #   -MaximumRedirection 0 emits a NON-TERMINATING InvalidOperationException
  #   ("The maximum redirection count has been exceeded") AND STILL RETURNS the
  #   response, StatusCode 302 with Location intact.
  #
  # $ErrorActionPreference = 'Stop' at the top of this script is what turned that
  # into a terminating error and threw the response away. The exception it raises
  # is InvalidOperationException, which has no .Response property at all -- exactly
  # what the 2026-08-28 regression note ~50 lines above records. So no catch block
  # can recover the Location on 5.1: by the time control reaches one, the only
  # object that ever held the header is gone.
  #
  # -ErrorAction SilentlyContinue keeps the error non-terminating for THIS call
  # only, so the response survives and the ordinary success path below reads the
  # 302 the way the curl branch does. -ErrorVariable keeps the error inspectable
  # rather than discarded.
  #
  # This is why the fallback was dead on Windows and why making it a
  # two-edition catch could not have revived it.
  # The catch is KEPT for editions that raise a genuine terminating error for the
  # same 302 -- PowerShell 7 is reported to raise HttpResponseException carrying an
  # HttpResponseMessage. -ErrorAction SilentlyContinue only downgrades NON-terminating
  # errors, so a truly terminating one still arrives here. This surface has no pwsh
  # installed, so the 7.x branch below is written defensively and is NOT verified
  # here; the 5.1 path above is measured.
  $iwrError = $null
  try {
    $r1 = Invoke-WebRequest -Uri $cfg.BUS_URL -Method Post -Body $bytes `
          -ContentType 'application/json; charset=utf-8' -MaximumRedirection 0 `
          -UseBasicParsing -TimeoutSec 120 `
          -ErrorAction SilentlyContinue -ErrorVariable iwrError

    if ($null -eq $r1) {
      # No response object at all: a real transport failure, not a suppressed 302.
      if ($iwrError -and @($iwrError).Count -gt 0) { throw @($iwrError)[0] }
      throw "BUS_IWR_NO_RESPONSE: Invoke-WebRequest returned nothing for hop 1."
    }

    $readHttpStatus = [int]$r1.StatusCode
    $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType ([string]$r1.Headers['Content-Type'])
    if ([int]$r1.StatusCode -ge 300 -and [int]$r1.StatusCode -lt 400) {
      $location = $r1.Headers['Location']
      if (-not $location -and $Action -ne 'read') {
        throw "BUS_IWR_REDIRECT_NO_LOCATION: hop 1 returned $([int]$r1.StatusCode) with no Location header."
      }
    }
    else {
      if ($Action -eq 'read') { $readIwrResponse = $r1 }
      else { $content = $r1.Content }
    }
  } catch {
    $resp = $null
    try { $resp = $_.Exception.Response } catch { $resp = $null }
    if (-not $resp) {
      if ($Action -eq 'read') {
        # No response means this is a transport exception, not an invalid read
        # response. Preserve its type/inner chain so the supervisor can classify
        # it as retryable; the sidecar remains an exact empty sanitized object.
        Write-BusReadMetadata `
          -Path $ReadMetadataOutFile `
          -TransportExit $readTransportExit `
          -HttpStatus $readHttpStatus `
          -ContentTypeClass $readContentTypeClass
      }
      throw
    } else {
      $status = 0
      try { $status = [int]$resp.StatusCode } catch { $status = 0 }

      $contentType = ''
      $locationValue = $null
      # DUCK-TYPE, never a type literal. `-is [System.Net.Http.Headers.HttpResponseHeaders]`
      # cannot be evaluated on Windows PowerShell 5.1 at all: System.Net.Http is not
      # loaded at startup and this script does not Add-Type it, so the literal raises
      # "Unable to find type" and takes the whole branch with it. Comparing the type
      # NAME needs no assembly to be loaded and behaves the same on both editions.
      try {
        $headerTypeName = ''
        if ($null -ne $resp.Headers) { $headerTypeName = $resp.Headers.GetType().FullName }
        if ($headerTypeName -eq 'System.Net.Http.Headers.HttpResponseHeaders') {
          if ($resp.Headers.Location) { $locationValue = [string]$resp.Headers.Location }
          if ($resp.Content -and $resp.Content.Headers -and $resp.Content.Headers.ContentType) {
            $contentType = [string]$resp.Content.Headers.ContentType
          }
        } elseif ($headerTypeName) {
          # HttpWebResponse and the 5.1 dictionary shapes: indexed access.
          $locationValue = [string]$resp.Headers['Location']
          $contentType = [string]$resp.Headers['Content-Type']
        }
      } catch {
        # Do NOT swallow silently. An unreadable header collection is a fact hop 2
        # needs, and the previous empty catch turned it into a wrong answer.
        Write-Warning "BUS_IWR_HEADER_READ_FAILED: $($_.Exception.Message)"
      }

      if ($status) { $readHttpStatus = $status }
      if ($contentType) { $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType $contentType }
      if ($status -ge 300 -and $status -lt 400 -and $locationValue) {
        $location = $locationValue
      } elseif ($status -ge 300 -and $status -lt 400) {
        if ($Action -ne 'read') {
          # Same contract failure as the non-terminating path above, so it gets the
          # same name. Rethrowing the original transport exception here would report
          # a redirect-with-no-usable-Location as whatever the edition happened to
          # raise, which is the one thing a caller cannot act on.
          throw "BUS_IWR_REDIRECT_NO_LOCATION: hop 1 returned $status with no readable Location header."
        }
      } elseif ($Action -ne 'read') { throw }
    }
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
    $tmpReadResponse2 = $null
    try {
      # Keep both the server-supplied hop-2 URL and every redirect HTTPS-only.
      # The leading '=' replaces curl's default protocol set; without it, 'https'
      # would be added to (rather than replace) the protocols curl already allows.
      if ($Action -eq 'read') {
        $tmpReadResponse2 = [IO.Path]::GetTempFileName()
        $savedErrorActionPreference = $ErrorActionPreference
        try {
          $ErrorActionPreference = 'Continue'
          $out2 = & $curl.Source -s -S -L --max-redirs $hop2MaxRedirects `
                    --proto '=https' --proto-redir '=https' `
                    -D $tmpHead2 -o $tmpReadResponse2 --max-time 120 $location 2>$null
          $hop2Exit = [int]$LASTEXITCODE
        } finally {
          $ErrorActionPreference = $savedErrorActionPreference
        }
      } else {
        $out2 = & $curl.Source -s -S -L --max-redirs $hop2MaxRedirects `
                  --proto '=https' --proto-redir '=https' `
                  -D $tmpHead2 --max-time 120 $location
        $hop2Exit = [int]$LASTEXITCODE
      }
      if ($null -eq $readTransportExit -or $hop2Exit -ne 0) { $readTransportExit = $hop2Exit }
      $head2 = Get-Content -LiteralPath $tmpHead2 -ErrorAction SilentlyContinue
      $hop2Metadata = Get-BusHeaderMetadata -HeaderLines @($head2)
      $readHttpStatus = $hop2Metadata.http_status
      $readContentTypeClass = $hop2Metadata.content_type_class
      if ($Action -ne 'read' -and
          ($hop2Exit -ne 0 -or $null -eq $readHttpStatus -or
          $readHttpStatus -lt 200 -or $readHttpStatus -ge 300)) {
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
      if ($Action -eq 'read') {
        $contentBytes = [IO.File]::ReadAllBytes($tmpReadResponse2)
      } else {
        $content = ($out2 -join "`n")
      }
    } finally {
      Remove-Item -LiteralPath $tmpHead2 -Force -ErrorAction SilentlyContinue
      if ($tmpReadResponse2) {
        Remove-Item -LiteralPath $tmpReadResponse2 -Force -ErrorAction SilentlyContinue
      }
    }
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
      if ($Action -eq 'read') {
        # A response-bearing failure is normalized by the common read gate below.
        # A response-less WebException must retain its network type for the
        # supervisor's one-retry classification.
        if (-not $resp) { throw }
      } else {
        if ($resp -and [int]$resp.StatusCode -ge 300 -and [int]$resp.StatusCode -lt 400) {
          throw [System.Net.WebException]::new(
            'hop 2 returned another redirect, which the IWR fallback refuses to follow. The write, if any, may still have landed: READ BACK before deciding anything.'
          )
        }
        throw [System.Net.WebException]::new(
          'hop 2 did not reach a successful final response. The write, if any, may still have landed: READ BACK before deciding anything.'
        )
      }
    } catch {
      $resp = $null
      try { $resp = $_.Exception.Response } catch { $resp = $null }
      if ($resp) {
        try { $readHttpStatus = [int]$resp.StatusCode } catch { $readHttpStatus = $null }
        $responseContentType = ''
        try {
          $headerTypeName = ''
          if ($null -ne $resp.Headers) { $headerTypeName = $resp.Headers.GetType().FullName }
          if ($headerTypeName -eq 'System.Net.Http.Headers.HttpResponseHeaders') {
            if ($resp.Content -and $resp.Content.Headers -and $resp.Content.Headers.ContentType) {
              $responseContentType = [string]$resp.Content.Headers.ContentType
            }
          } elseif ($headerTypeName) {
            $responseContentType = [string]$resp.Headers['Content-Type']
          }
        } catch {
          Write-Warning "BUS_IWR_HEADER_READ_FAILED: $($_.Exception.Message)"
        }
        if ($responseContentType) {
          $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType $responseContentType
        }
      }
      Write-BusReadMetadata `
        -Path $ReadMetadataOutFile `
        -TransportExit $readTransportExit `
        -HttpStatus $readHttpStatus `
        -ContentTypeClass $readContentTypeClass
      if ($Action -eq 'read') {
        if (-not $resp) { throw }
      } else {
        throw [System.Net.WebException]::new(
          'hop 2 did not reach a successful final response. The write, if any, may still have landed: READ BACK before deciding anything.'
        )
      }
    }
    if ($null -ne $r2) {
      $readHttpStatus = [int]$r2.StatusCode
      $readContentTypeClass = ConvertTo-BusContentTypeClass -ContentType ([string]$r2.Headers['Content-Type'])
      if ($Action -ne 'read' -and ($readHttpStatus -lt 200 -or $readHttpStatus -ge 300)) {
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
      if ($Action -eq 'read') { $readIwrResponse = $r2 }
      else { $content = $r2.Content }
    }
  }
}

Write-BusReadMetadata `
  -Path $ReadMetadataOutFile `
  -TransportExit $readTransportExit `
  -HttpStatus $readHttpStatus `
  -ContentTypeClass $readContentTypeClass

if ($Action -eq 'read') {
  $readInvalidMessage = 'BUS_READ_RESPONSE_INVALID: read response failed the generic success, identity, or payload contract.'
  $transportExitAccepted = $null -eq $readTransportExit -or [int]$readTransportExit -eq 0
  if (-not $transportExitAccepted -or
      $null -eq $readHttpStatus -or
      [int]$readHttpStatus -lt 200 -or
      [int]$readHttpStatus -ge 300 -or
      $readContentTypeClass -cne 'json') {
    throw [InvalidOperationException]::new($readInvalidMessage)
  }

  if ($null -eq $contentBytes -and $null -ne $readIwrResponse) {
    try {
      $contentBytes = Get-BusIwrResponseBytes -Response $readIwrResponse
    } catch {
      throw [InvalidOperationException]::new($readInvalidMessage)
    }
  }

  if ($null -ne $contentBytes) {
    try {
      $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
      $content = $strictUtf8.GetString($contentBytes)
    } catch {
      throw [InvalidOperationException]::new($readInvalidMessage)
    }
  }
}

if ($content -is [byte[]]) { $content = [Text.Encoding]::UTF8.GetString($content) }
# [string]$null is $null in WinPS 5.1, not '' -- coalesce before calling a method on it.
if ($null -eq $content) { $content = '' }
if ($Action -eq 'read') {
  Assert-BusReadResponseContract `
    -Content ([string]$content) `
    -RequestedTitle $Title `
    -RequestedFileId $FileId
} else {
  $trimmed = ([string]$content).TrimStart()
  if (-not $trimmed.StartsWith('{')) {
    Write-Warning "response is not JSON (redirect artifact?). Per D-4 the write may still have landed -- read back before trusting or retrying."
  }
}

if ($OutFile) {
  $path = if ([IO.Path]::IsPathRooted($OutFile)) { $OutFile } else { Join-Path (Get-Location).Path $OutFile }
  if ($Action -eq 'read' -and $null -ne $contentBytes) {
    [IO.File]::WriteAllBytes($path, $contentBytes)
  } else {
    [IO.File]::WriteAllText($path, [string]$content, (New-Object Text.UTF8Encoding($false)))
  }
  Write-Output ("saved {0} chars to {1}" -f ([string]$content).Length, $path)
} else {
  Write-Output $content
}
