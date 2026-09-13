#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0
$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:BusPath = Join-Path $script:RepoRoot 'scripts\bus.ps1'
$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('bus-read-response-contract-' + [Guid]::NewGuid().ToString('N'))
$script:FakeSecret = 'TEST_BUS_SECRET_DO_NOT_LEAK_0b920fa5'

function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')

    if ($Condition) {
        $script:Passed++
        [Console]::Out.WriteLine('PASS ' + $Name)
    } else {
        $script:Failed++
        [Console]::Out.WriteLine('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
    }
}

function Write-TestUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )

    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-TestPort {
    $probe = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    try {
        $probe.Start()
        return [int]$probe.LocalEndpoint.Port
    } finally {
        $probe.Stop()
    }
}

function Test-ByteArraysEqual {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Left,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Right
    )

    if ($Left.Length -ne $Right.Length) { return $false }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) { return $false }
    }
    return $true
}

function Test-ContainsIgnoringWhitespace {
    param(
        [AllowEmptyString()][string]$Text,
        [AllowEmptyString()][string]$Needle
    )

    if ([string]::IsNullOrWhiteSpace($Needle)) { return $false }
    $compactText = [regex]::Replace([string]$Text, '\s', '')
    $compactNeedle = [regex]::Replace([string]$Needle, '\s', '')
    return $compactText.Contains($compactNeedle)
}

function Start-TestResponseServer {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$ResponseCount,
        [Parameter(Mandatory = $true)][string]$ReadyPath,
        [Parameter(Mandatory = $true)][string]$ErrorPath
    )

    $job = Start-Job -ArgumentList $Port, $Root, $ResponseCount, $ReadyPath, $ErrorPath -ScriptBlock {
        param($ListenPort, $ServerRoot, $ExpectedResponses, $ReadyFile, $FailureFile)
        $ErrorActionPreference = 'Stop'
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, [int]$ListenPort)
        try {
            $listener.Start()
            [IO.File]::WriteAllText($ReadyFile, 'ready', (New-Object Text.UTF8Encoding($false)))
            for ($responseIndex = 1; $responseIndex -le $ExpectedResponses; $responseIndex++) {
                $client = $null
                try {
                    $client = $listener.AcceptTcpClient()
                    $client.ReceiveTimeout = 15000
                    $stream = $client.GetStream()
                    $headerBytes = New-Object 'System.Collections.Generic.List[byte]'
                    while ($true) {
                        $nextByte = $stream.ReadByte()
                        if ($nextByte -lt 0) { throw 'test_request_header_ended_early' }
                        $headerBytes.Add([byte]$nextByte)
                        if ($headerBytes.Count -gt 65536) { throw 'test_request_header_too_large' }
                        if ($headerBytes.Count -ge 4 -and
                            $headerBytes[$headerBytes.Count - 4] -eq 13 -and
                            $headerBytes[$headerBytes.Count - 3] -eq 10 -and
                            $headerBytes[$headerBytes.Count - 2] -eq 13 -and
                            $headerBytes[$headerBytes.Count - 1] -eq 10) {
                            break
                        }
                    }

                    $headerText = [Text.Encoding]::ASCII.GetString($headerBytes.ToArray())
                    $contentLength = 0
                    if ($headerText -match '(?im)^Content-Length:\s*([0-9]+)\s*$') {
                        $contentLength = [int]$matches[1]
                    }
                    $requestBytes = New-Object byte[] $contentLength
                    $received = 0
                    while ($received -lt $contentLength) {
                        $readNow = $stream.Read($requestBytes, $received, $contentLength - $received)
                        if ($readNow -le 0) { throw 'test_request_body_ended_early' }
                        $received += $readNow
                    }
                    [IO.File]::WriteAllBytes(
                        (Join-Path $ServerRoot ('request-' + $responseIndex + '.json')),
                        $requestBytes
                    )

                    $responseBytes = [IO.File]::ReadAllBytes(
                        (Join-Path $ServerRoot ('response-' + $responseIndex + '.json'))
                    )
                    $responseStatus = [int][IO.File]::ReadAllText(
                        (Join-Path $ServerRoot ('status-' + $responseIndex + '.txt')),
                        [Text.Encoding]::UTF8
                    )
                    $responseContentType = [IO.File]::ReadAllText(
                        (Join-Path $ServerRoot ('content-type-' + $responseIndex + '.txt')),
                        [Text.Encoding]::UTF8
                    )
                    $declaredLengthExtra = [int][IO.File]::ReadAllText(
                        (Join-Path $ServerRoot ('length-extra-' + $responseIndex + '.txt')),
                        [Text.Encoding]::UTF8
                    )
                    $declaredLength = $responseBytes.Length + $declaredLengthExtra
                    $head = "HTTP/1.1 $responseStatus Test`r`nContent-Type: $responseContentType`r`nContent-Length: $declaredLength`r`nConnection: close`r`n`r`n"
                    $headBytes = [Text.Encoding]::ASCII.GetBytes($head)
                    $stream.Write($headBytes, 0, $headBytes.Length)
                    $stream.Write($responseBytes, 0, $responseBytes.Length)
                    $stream.Flush()
                } finally {
                    if ($client) { $client.Close() }
                }
            }
        } catch {
            [IO.File]::WriteAllText(
                $FailureFile,
                [string]$_.Exception.Message,
                (New-Object Text.UTF8Encoding($false))
            )
            throw
        } finally {
            $listener.Stop()
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while (-not (Test-Path -LiteralPath $ReadyPath -PathType Leaf) -and
           [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    if (-not (Test-Path -LiteralPath $ReadyPath -PathType Leaf)) {
        $details = (@(Receive-Job -Job $job -Keep -ErrorAction SilentlyContinue) -join "`n")
        Stop-Job -Job $job -ErrorAction SilentlyContinue
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        throw ('test_response_server_start_failed' + $(if ($details) { ': ' + $details } else { '' }))
    }
    return $job
}

function New-ReadCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Body,
        [bool]$Valid = $false,
        [AllowEmptyString()][string]$Title = 'Contract Sheet',
        [AllowEmptyString()][string]$FileId = '',
        [ValidateSet('read', 'READ')][string]$Action = 'read',
        [bool]$UseOutFile = $true,
        [AllowEmptyString()][string]$LeakToken = '',
        [int]$HttpStatus = 200,
        [AllowEmptyString()][string]$ContentType = 'application/json; charset=utf-8',
        [int]$DeclaredLengthExtra = 0,
        [AllowNull()]$ExpectedTransportExit = 0,
        [AllowEmptyString()][string]$ExpectedContentTypeClass = 'json',
        [bool]$RawInvalidUtf8 = $false,
        [bool]$ForceIwr = $false,
        [bool]$ExpectRequest = $true,
        [bool]$ExpectMetadata = $true,
        [AllowEmptyString()][string]$ExpectedErrorCode = 'BUS_READ_RESPONSE_INVALID',
        [bool]$AliasOutputMetadata = $false
    )

    return [pscustomobject][ordered]@{
        Name = $Name
        Body = $Body
        Valid = $Valid
        Title = $Title
        FileId = $FileId
        Action = $Action
        UseOutFile = $UseOutFile
        LeakToken = $LeakToken
        IsRead = $true
        HttpStatus = $HttpStatus
        ContentType = $ContentType
        DeclaredLengthExtra = $DeclaredLengthExtra
        ExpectedTransportExit = $ExpectedTransportExit
        ExpectedContentTypeClass = $ExpectedContentTypeClass
        RawInvalidUtf8 = $RawInvalidUtf8
        ForceIwr = $ForceIwr
        ExpectRequest = $ExpectRequest
        ExpectMetadata = $ExpectMetadata
        ExpectedErrorCode = $ExpectedErrorCode
        AliasOutputMetadata = $AliasOutputMetadata
        ServerIndex = 0
    }
}

function Invoke-TestCase {
    param(
        [Parameter(Mandatory = $true)]$Case,
        [Parameter(Mandatory = $true)][int]$Index,
        [Parameter(Mandatory = $true)][string]$EnvPath,
        [Parameter(Mandatory = $true)][string]$ChildShell
    )

    $caseRoot = Join-Path $script:TestRoot ('case-' + $Index + '-' + $Case.Name)
    New-Item -ItemType Directory -Path $caseRoot -Force | Out-Null
    $outPath = Join-Path $caseRoot 'response.json'
    $metadataPath = Join-Path $caseRoot 'metadata.json'
    $metadataArgument = $metadataPath
    if ($Case.AliasOutputMetadata) {
        $metadataPath = $outPath
        $metadataArgument = Join-Path $caseRoot ('unused' + [IO.Path]::DirectorySeparatorChar + '..' +
            [IO.Path]::DirectorySeparatorChar + 'response.json')
    }
    $sentinel = 'OUTFILE_SENTINEL_' + $Case.Name
    if ($Case.UseOutFile -and -not $Case.Valid) {
        Write-TestUtf8 -Path $outPath -Text $sentinel
    }

    $arguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $script:BusPath,
        '-Action', $Case.Action,
        '-EnvFile', $EnvPath
    )
    if ([string]$Case.Title) {
        $arguments += @('-Title', [string]$Case.Title)
    }
    if ([string]$Case.FileId) {
        $arguments += @('-FileId', [string]$Case.FileId)
    }
    if ($Case.UseOutFile) { $arguments += @('-OutFile', $outPath) }
    if ($Case.IsRead) {
        $arguments += @('-ReadMetadataOutFile', $metadataArgument)
    } else {
        $arguments += @('-Text', 'append compatibility payload')
    }

    $previousPreference = $ErrorActionPreference
    $previousPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    $ErrorActionPreference = 'Continue'
    try {
        if ($Case.ForceIwr) {
            [Environment]::SetEnvironmentVariable('PATH', '', 'Process')
        }
        $output = @(& $ChildShell @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('PATH', $previousPath, 'Process')
        $ErrorActionPreference = $previousPreference
    }

    $requestPath = if ($Case.ExpectRequest) {
        Join-Path $script:TestRoot ('request-' + [int]$Case.ServerIndex + '.json')
    } else {
        Join-Path $script:TestRoot ('request-local-' + $Index + '.json')
    }
    $requestText = if (Test-Path -LiteralPath $requestPath -PathType Leaf) {
        [IO.File]::ReadAllText($requestPath, [Text.Encoding]::UTF8)
    } else { '' }
    $metadataText = if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
        [IO.File]::ReadAllText($metadataPath, [Text.Encoding]::UTF8)
    } else { '' }
    $outText = if (Test-Path -LiteralPath $outPath -PathType Leaf) {
        [IO.File]::ReadAllText($outPath, [Text.Encoding]::UTF8)
    } else { '' }
    $rendered = (@($output | ForEach-Object { [string]$_ }) -join "`n")

    return [pscustomobject][ordered]@{
        ExitCode = $exitCode
        Output = @($output)
        Rendered = $rendered
        OutPath = $outPath
        OutText = $outText
        MetadataPath = $metadataPath
        MetadataText = $metadataText
        RequestPath = $requestPath
        RequestText = $requestText
        Sentinel = $sentinel
    }
}

$unicodeValue = 'caf{0}-{1}{2}-{3}' -f (
    [char]0x00E9,
    [char]0x6F22,
    [char]0x5B57,
    [char]::ConvertFromUtf32(0x1F642)
)
$unicodeRawBody = @'
{
  "ok": true,
  "fileId": "unicode-file",
  "title": "Unicode Sheet",
  "rows": [["UNICODE_VALUE_TOKEN"]],
  "note": "spacing and property order stay untouched"
}
'@
$unicodeRawBody = $unicodeRawBody.Replace('UNICODE_VALUE_TOKEN', $unicodeValue).TrimEnd("`r", "`n") + "`n  "
$unicodeStdoutBody = '{"ok":true,"fileId":"stdout-file","title":"Stdout Sheet","rows":[["' +
    $unicodeValue + '"]]}'

$cases = @(
    (New-ReadCase -Name 'apps-script-sheet' -Valid $true -Title 'Contract Sheet' -Body '{"ok":true,"fileId":"sheet-apps","title":"Contract Sheet","rows":[["a",1],[]]}'),
    (New-ReadCase -Name 'self-hosted-sheet' -Valid $true -Title 'Hosted Sheet' -Body '{"ok":true,"_httpStatus":299,"fileId":"sheet-hosted","title":"Hosted Sheet","kind":"sheet","total_rows":0,"rows":[]}'),
    (New-ReadCase -Name 'apps-script-doc-empty-text' -Valid $true -Title 'Apps Doc' -Body '{"ok":true,"fileId":"doc-apps","title":"Apps Doc","text":""}'),
    (New-ReadCase -Name 'iso-looking-doc-strings' -Valid $true -Title '2026-09-13T00:00:00Z' -FileId '2026-09-14T00:00:00Z' -Body '{"ok":true,"fileId":"2026-09-14T00:00:00Z","title":"2026-09-13T00:00:00Z","text":"2026-09-15T00:00:00Z"}'),
    (New-ReadCase -Name 'self-hosted-doc-empty-body' -Valid $true -Title 'Hosted Doc' -Body '{"ok":true,"fileId":"doc-hosted","title":"Hosted Doc","kind":"doc","revision":8,"body":""}'),
    (New-ReadCase -Name 'file-id-selector' -Valid $true -Title '' -FileId 'selected-file' -Body '{"ok":true,"fileId":"selected-file","title":"Any Nonempty Title","rows":[]}'),
    (New-ReadCase -Name 'both-selectors' -Valid $true -Title 'Both Selectors' -FileId 'both-file' -Body '{"ok":true,"fileId":"both-file","title":"Both Selectors","rows":[]}'),
    (New-ReadCase -Name 'uppercase-read' -Valid $true -Action 'READ' -Title 'Uppercase Action' -Body '{"ok":true,"fileId":"upper-file","title":"Uppercase Action","rows":[]}'),
    (New-ReadCase -Name 'unicode-raw-preservation' -Valid $true -Title 'Unicode Sheet' -Body $unicodeRawBody),
    (New-ReadCase -Name 'valid-stdout-preservation' -Valid $true -UseOutFile $false -Title 'Stdout Sheet' -Body $unicodeStdoutBody),
    (New-ReadCase -Name 'iwr-valid-sheet' -Valid $true -ForceIwr $true -ExpectedTransportExit $null -Body '{"ok":true,"fileId":"iwr-valid","title":"Contract Sheet","rows":[["fallback"]]}'),

    # Regression and mutation control: the observed HTTP-200 health object must
    # fail without changing a preseeded output file or leaking its body to stdout.
    (New-ReadCase -Name 'health-object-preseed' -Body '{"ok":true,"service":"blackboard-bus","canary":"HEALTH_BODY_CANARY_PRESEED"}' -LeakToken 'HEALTH_BODY_CANARY_PRESEED'),
    (New-ReadCase -Name 'health-object-stdout' -UseOutFile $false -Body '{"ok":true,"service":"blackboard-bus","canary":"HEALTH_BODY_CANARY_STDOUT"}' -LeakToken 'HEALTH_BODY_CANARY_STDOUT'),
    (New-ReadCase -Name 'curl-nonzero-valid-body' -Body '{"ok":true,"fileId":"transport-exit","title":"Contract Sheet","rows":[],"canary":"CURL_NONZERO_BODY_CANARY"}' -LeakToken 'CURL_NONZERO_BODY_CANARY' -DeclaredLengthExtra 19 -ExpectedTransportExit 18),
    # The spaced canary proves leak checks remain effective if diagnostics add
    # formatting whitespace between response fragments.
    (New-ReadCase -Name 'http-500-valid-body' -Body '{"ok":true,"fileId":"http-500","title":"Contract Sheet","rows":[],"canary":"HTTP 500 BODY CANARY"}' -LeakToken 'HTTP 500 BODY CANARY' -HttpStatus 500),
    (New-ReadCase -Name 'html-content-type-valid-body' -Body '{"ok":true,"fileId":"html-type","title":"Contract Sheet","rows":[],"canary":"HTML_TYPE_BODY_CANARY"}' -LeakToken 'HTML_TYPE_BODY_CANARY' -ContentType 'text/html; charset=utf-8' -ExpectedContentTypeClass 'html'),
    (New-ReadCase -Name 'redirect-no-location-valid-body' -Body '{"ok":true,"fileId":"redirect-no-location","title":"Contract Sheet","rows":[],"canary":"REDIRECT_NO_LOCATION_BODY_CANARY"}' -LeakToken 'REDIRECT_NO_LOCATION_BODY_CANARY' -HttpStatus 302),
    (New-ReadCase -Name 'iwr-http-500-valid-body' -Body '{"ok":true,"fileId":"iwr-http-500","title":"Contract Sheet","rows":[],"canary":"IWR_HTTP_500_BODY_CANARY"}' -LeakToken 'IWR_HTTP_500_BODY_CANARY' -HttpStatus 500 -ForceIwr $true -ExpectedTransportExit $null),
    (New-ReadCase -Name 'iwr-html-content-type-valid-body' -Body '{"ok":true,"fileId":"iwr-html-type","title":"Contract Sheet","rows":[],"canary":"IWR_HTML_TYPE_BODY_CANARY"}' -LeakToken 'IWR_HTML_TYPE_BODY_CANARY' -ContentType 'text/html; charset=utf-8' -ExpectedContentTypeClass 'html' -ForceIwr $true -ExpectedTransportExit $null),
    (New-ReadCase -Name 'iwr-redirect-no-location-valid-body' -Body '{"ok":true,"fileId":"iwr-redirect-no-location","title":"Contract Sheet","rows":[],"canary":"IWR_REDIRECT_NO_LOCATION_BODY_CANARY"}' -LeakToken 'IWR_REDIRECT_NO_LOCATION_BODY_CANARY' -HttpStatus 302 -ForceIwr $true -ExpectedTransportExit $null),
    (New-ReadCase -Name 'curl-malformed-utf8-valid-shape' -Body 'RAW_BYTES_WRITTEN_BELOW' -LeakToken 'CURL_MALFORMED_UTF8_BODY_CANARY' -RawInvalidUtf8 $true),
    (New-ReadCase -Name 'iwr-malformed-utf8-valid-shape' -Body 'RAW_BYTES_WRITTEN_BELOW' -LeakToken 'IWR_MALFORMED_UTF8_BODY_CANARY' -RawInvalidUtf8 $true -ForceIwr $true -ExpectedTransportExit $null),
    (New-ReadCase -Name 'duplicate-ok' -Body '{"ok":false,"ok":true,"fileId":"duplicate-ok","title":"Contract Sheet","rows":[],"canary":"DUPLICATE_OK_BODY_CANARY"}' -LeakToken 'DUPLICATE_OK_BODY_CANARY'),
    (New-ReadCase -Name 'duplicate-title' -Body '{"ok":true,"fileId":"duplicate-title","title":"Wrong Title","title":"Contract Sheet","rows":[],"canary":"DUPLICATE_TITLE_BODY_CANARY"}' -LeakToken 'DUPLICATE_TITLE_BODY_CANARY'),
    (New-ReadCase -Name 'duplicate-rows' -Body '{"ok":true,"fileId":"duplicate-rows","title":"Contract Sheet","rows":"wrong","rows":[],"canary":"DUPLICATE_ROWS_BODY_CANARY"}' -LeakToken 'DUPLICATE_ROWS_BODY_CANARY'),
    (New-ReadCase -Name 'ok-false' -Body '{"ok":false,"fileId":"bad","title":"Contract Sheet","rows":[],"canary":"OK_FALSE_CANARY"}' -LeakToken 'OK_FALSE_CANARY'),
    (New-ReadCase -Name 'ok-string' -Body '{"ok":"true","fileId":"bad","title":"Contract Sheet","rows":[],"canary":"OK_STRING_CANARY"}' -LeakToken 'OK_STRING_CANARY'),
    (New-ReadCase -Name 'missing-ok' -Body '{"fileId":"bad","title":"Contract Sheet","rows":[],"canary":"MISSING_OK_CANARY"}' -LeakToken 'MISSING_OK_CANARY'),
    (New-ReadCase -Name 'logical-http-refusal' -Body '{"ok":true,"_httpStatus":503,"fileId":"bad","title":"Contract Sheet","rows":[],"canary":"LOGICAL_STATUS_CANARY"}' -LeakToken 'LOGICAL_STATUS_CANARY'),
    (New-ReadCase -Name 'logical-http-string' -Body '{"ok":true,"_httpStatus":"200","fileId":"bad","title":"Contract Sheet","rows":[],"canary":"LOGICAL_STRING_CANARY"}' -LeakToken 'LOGICAL_STRING_CANARY'),
    (New-ReadCase -Name 'logical-http-fraction' -Body '{"ok":true,"_httpStatus":200.5,"fileId":"bad","title":"Contract Sheet","rows":[],"canary":"LOGICAL_FRACTION_CANARY"}' -LeakToken 'LOGICAL_FRACTION_CANARY'),
    (New-ReadCase -Name 'malformed-json' -Body '{"ok":true,"canary":"MALFORMED_JSON_CANARY"' -LeakToken 'MALFORMED_JSON_CANARY'),
    (New-ReadCase -Name 'concatenated-objects' -Body '{"canary":"CONCAT_OBJECT_CANARY"}{"ok":true}' -LeakToken 'CONCAT_OBJECT_CANARY'),
    (New-ReadCase -Name 'root-scalar' -Body '"ROOT_SCALAR_CANARY"' -LeakToken 'ROOT_SCALAR_CANARY'),
    (New-ReadCase -Name 'root-array' -Body '["ROOT_ARRAY_CANARY"]' -LeakToken 'ROOT_ARRAY_CANARY'),
    (New-ReadCase -Name 'root-valid-object-array' -Body '[{"ok":true,"fileId":"array-file","title":"Contract Sheet","rows":[],"canary":"ROOT_VALID_OBJECT_ARRAY_CANARY"}]' -LeakToken 'ROOT_VALID_OBJECT_ARRAY_CANARY'),
    (New-ReadCase -Name 'missing-file-id' -Body '{"ok":true,"title":"Contract Sheet","rows":[],"canary":"MISSING_FILE_CANARY"}' -LeakToken 'MISSING_FILE_CANARY'),
    (New-ReadCase -Name 'blank-file-id' -Body '{"ok":true,"fileId":"   ","title":"Contract Sheet","rows":[],"canary":"BLANK_FILE_CANARY"}' -LeakToken 'BLANK_FILE_CANARY'),
    (New-ReadCase -Name 'numeric-file-id' -Body '{"ok":true,"fileId":7,"title":"Contract Sheet","rows":[],"canary":"NUMERIC_FILE_CANARY"}' -LeakToken 'NUMERIC_FILE_CANARY'),
    (New-ReadCase -Name 'missing-title' -Body '{"ok":true,"fileId":"bad","rows":[],"canary":"MISSING_TITLE_CANARY"}' -LeakToken 'MISSING_TITLE_CANARY'),
    (New-ReadCase -Name 'blank-title' -Body '{"ok":true,"fileId":"bad","title":"   ","rows":[],"canary":"BLANK_TITLE_CANARY"}' -LeakToken 'BLANK_TITLE_CANARY'),
    (New-ReadCase -Name 'missing-payload' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","canary":"MISSING_PAYLOAD_CANARY"}' -LeakToken 'MISSING_PAYLOAD_CANARY'),
    (New-ReadCase -Name 'multiple-payloads' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","rows":[],"text":"","canary":"MULTIPLE_PAYLOAD_CANARY"}' -LeakToken 'MULTIPLE_PAYLOAD_CANARY'),
    (New-ReadCase -Name 'rows-null' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","rows":null,"canary":"ROWS_NULL_CANARY"}' -LeakToken 'ROWS_NULL_CANARY'),
    (New-ReadCase -Name 'rows-string' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","rows":"not-an-array","canary":"ROWS_STRING_CANARY"}' -LeakToken 'ROWS_STRING_CANARY'),
    (New-ReadCase -Name 'rows-object' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","rows":{"not":"an array"},"canary":"ROWS_OBJECT_CANARY"}' -LeakToken 'ROWS_OBJECT_CANARY'),
    (New-ReadCase -Name 'row-member-scalar' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","rows":[["valid"],"invalid"],"canary":"ROW_MEMBER_CANARY"}' -LeakToken 'ROW_MEMBER_CANARY'),
    (New-ReadCase -Name 'text-null' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","text":null,"canary":"TEXT_NULL_CANARY"}' -LeakToken 'TEXT_NULL_CANARY'),
    (New-ReadCase -Name 'body-number' -Body '{"ok":true,"fileId":"bad","title":"Contract Sheet","body":4,"canary":"BODY_NUMBER_CANARY"}' -LeakToken 'BODY_NUMBER_CANARY'),
    (New-ReadCase -Name 'title-mismatch' -Title 'Requested Title' -Body '{"ok":true,"fileId":"bad","title":"Different Title","rows":[],"canary":"TITLE_MISMATCH_CANARY"}' -LeakToken 'TITLE_MISMATCH_CANARY'),
    (New-ReadCase -Name 'title-case-mismatch' -Title 'Requested Title' -Body '{"ok":true,"fileId":"bad","title":"requested title","rows":[],"canary":"TITLE_CASE_CANARY"}' -LeakToken 'TITLE_CASE_CANARY'),
    (New-ReadCase -Name 'whitespace-title-selector' -Title '   ' -Body '{"ok":true,"fileId":"bad","title":"Different Title","rows":[],"canary":"WHITESPACE_TITLE_SELECTOR_CANARY"}' -LeakToken 'WHITESPACE_TITLE_SELECTOR_CANARY' -ExpectRequest $false -ExpectMetadata $false -ExpectedErrorCode 'BUS_READ_SELECTOR_REQUIRED'),
    (New-ReadCase -Name 'file-id-mismatch' -Title '' -FileId 'requested-file' -Body '{"ok":true,"fileId":"different-file","title":"Any Title","rows":[],"canary":"FILE_MISMATCH_CANARY"}' -LeakToken 'FILE_MISMATCH_CANARY'),
    (New-ReadCase -Name 'file-id-case-mismatch' -Title '' -FileId 'Requested-File' -Body '{"ok":true,"fileId":"requested-file","title":"Any Title","rows":[],"canary":"FILE_CASE_CANARY"}' -LeakToken 'FILE_CASE_CANARY'),
    (New-ReadCase -Name 'whitespace-file-id-selector' -Title '' -FileId '   ' -Body '{"ok":true,"fileId":"different-file","title":"Any Title","rows":[],"canary":"WHITESPACE_FILE_SELECTOR_CANARY"}' -LeakToken 'WHITESPACE_FILE_SELECTOR_CANARY' -ExpectRequest $false -ExpectMetadata $false -ExpectedErrorCode 'BUS_READ_SELECTOR_REQUIRED'),
    (New-ReadCase -Name 'wrong-property-case' -Body '{"OK":true,"fileId":"bad","title":"Contract Sheet","rows":[],"canary":"PROPERTY_CASE_CANARY"}' -LeakToken 'PROPERTY_CASE_CANARY')
)

$appendCase = [pscustomobject][ordered]@{
    Name = 'append-minimal-response-unchanged'
    Body = '{"ok":true}'
    Valid = $true
    Title = 'General Test Document'
    FileId = ''
    Action = 'append'
    UseOutFile = $true
    LeakToken = ''
    IsRead = $false
    HttpStatus = 200
    ContentType = 'application/json; charset=utf-8'
    DeclaredLengthExtra = 0
    ExpectedTransportExit = 0
    ExpectedContentTypeClass = 'json'
    RawInvalidUtf8 = $false
    ForceIwr = $false
    ExpectRequest = $true
    ExpectMetadata = $false
    ExpectedErrorCode = ''
    AliasOutputMetadata = $false
    ServerIndex = 0
}
$cases += $appendCase
$cases += @(
    (New-ReadCase -Name 'selector-required-preflight' -Title '' -Body '{"ok":true,"fileId":"unserved-selector","title":"Unserved Selector","rows":[],"canary":"NO_SELECTOR_BODY_CANARY"}' -LeakToken 'NO_SELECTOR_BODY_CANARY' -ExpectRequest $false -ExpectMetadata $false -ExpectedErrorCode 'BUS_READ_SELECTOR_REQUIRED'),
    (New-ReadCase -Name 'output-metadata-path-conflict' -Body '{"ok":true,"fileId":"unserved-alias","title":"Contract Sheet","rows":[],"canary":"PATH_ALIAS_BODY_CANARY"}' -LeakToken 'PATH_ALIAS_BODY_CANARY' -ExpectRequest $false -ExpectMetadata $false -ExpectedErrorCode 'BUS_READ_OUTPUT_PATH_CONFLICT' -AliasOutputMetadata $true)
)

New-Item -ItemType Directory -Path $script:TestRoot -Force | Out-Null
$serverJob = $null
try {
    $port = Get-TestPort
    $envPath = Join-Path $script:TestRoot 'test.env'
    $readyPath = Join-Path $script:TestRoot 'ready.txt'
    $serverErrorPath = Join-Path $script:TestRoot 'server-error.txt'
    $script:BusUrl = "http://127.0.0.1:$port/"
    Write-TestUtf8 -Path $envPath -Text ("BUS_URL=$($script:BusUrl)`nBUS_SECRET=$($script:FakeSecret)`n")
    $responseIndex = 0
    foreach ($case in $cases) {
        if (-not $case.ExpectRequest) { continue }
        $responseIndex++
        $case.ServerIndex = $responseIndex
        $responsePath = Join-Path $script:TestRoot ('response-' + $responseIndex + '.json')
        if ($case.RawInvalidUtf8) {
            $prefixBytes = [Text.Encoding]::UTF8.GetBytes(
                '{"ok":true,"fileId":"raw-utf8","title":"Contract Sheet","rows":[],"raw":"'
            )
            $invalidBytes = [byte[]]@(0xC3, 0x28)
            $suffixBytes = [Text.Encoding]::UTF8.GetBytes(
                '","canary":"' + [string]$case.LeakToken + '"}'
            )
            $rawBytes = New-Object byte[] ($prefixBytes.Length + $invalidBytes.Length + $suffixBytes.Length)
            [Array]::Copy($prefixBytes, 0, $rawBytes, 0, $prefixBytes.Length)
            [Array]::Copy($invalidBytes, 0, $rawBytes, $prefixBytes.Length, $invalidBytes.Length)
            [Array]::Copy(
                $suffixBytes,
                0,
                $rawBytes,
                $prefixBytes.Length + $invalidBytes.Length,
                $suffixBytes.Length
            )
            [IO.File]::WriteAllBytes($responsePath, $rawBytes)
        } else {
            Write-TestUtf8 -Path $responsePath -Text ([string]$case.Body)
        }
        Write-TestUtf8 `
            -Path (Join-Path $script:TestRoot ('status-' + $responseIndex + '.txt')) `
            -Text ([string]$case.HttpStatus)
        Write-TestUtf8 `
            -Path (Join-Path $script:TestRoot ('content-type-' + $responseIndex + '.txt')) `
            -Text ([string]$case.ContentType)
        Write-TestUtf8 `
            -Path (Join-Path $script:TestRoot ('length-extra-' + $responseIndex + '.txt')) `
            -Text ([string]$case.DeclaredLengthExtra)
    }

    $childShell = if ($PSVersionTable.PSEdition -ceq 'Desktop') {
        Join-Path $PSHOME 'powershell.exe'
    } elseif ($IsWindows) {
        Join-Path $PSHOME 'pwsh.exe'
    } else {
        Join-Path $PSHOME 'pwsh'
    }
    if (-not (Test-Path -LiteralPath $childShell -PathType Leaf)) {
        throw ('test_child_shell_missing: ' + $childShell)
    }
    [Console]::Out.WriteLine('ENGINE ' + $PSVersionTable.PSEdition + ' ' + $PSVersionTable.PSVersion + ' child=' + $childShell)
    [Console]::Out.WriteLine('CASES ' + $cases.Count)

    $serverJob = Start-TestResponseServer `
        -Port $port `
        -Root $script:TestRoot `
        -ResponseCount $responseIndex `
        -ReadyPath $readyPath `
        -ErrorPath $serverErrorPath

    for ($index = 0; $index -lt $cases.Count; $index++) {
        $case = $cases[$index]
        $result = Invoke-TestCase `
            -Case $case `
            -Index ($index + 1) `
            -EnvPath $envPath `
            -ChildShell $childShell
        $prefix = [string]$case.Name

        if ($case.ExpectRequest) {
            Assert-True ($prefix + ' reached the real bus entrypoint and loopback transport') (
                Test-Path -LiteralPath $result.RequestPath -PathType Leaf
            )
        } else {
            Assert-True ($prefix + ' fails before making a transport request') (
                -not (Test-Path -LiteralPath $result.RequestPath)
            )
        }
        Assert-True ($prefix + ' never exposes the bus secret') (
            -not (Test-ContainsIgnoringWhitespace -Text $result.Rendered -Needle $script:FakeSecret)
        )

        $request = if ($result.RequestText) { $result.RequestText | ConvertFrom-Json } else { $null }
        $expectedWireAction = if ($case.IsRead) { 'read' } else { [string]$case.Action }
        if ($case.ExpectRequest) {
            Assert-True ($prefix + ' sends the expected wire action') (
                $null -ne $request -and [string]$request.action -ceq $expectedWireAction
            ) $(if ($null -eq $request) { 'no captured JSON request' } else { 'action=' + [string]$request.action })
        }
        if ($case.IsRead -and $case.ExpectMetadata) {
            $expectedMetadata = [ordered]@{}
            if ($null -ne $case.ExpectedTransportExit) {
                $expectedMetadata.transport_exit = [int]$case.ExpectedTransportExit
            }
            $expectedMetadata.http_status = [int]$case.HttpStatus
            $expectedMetadata.content_type_class = [string]$case.ExpectedContentTypeClass
            $expectedMetadataText = $expectedMetadata | ConvertTo-Json -Compress
            Assert-True ($prefix + ' preserves sanitized transport metadata') (
                [string]$result.MetadataText -ceq [string]$expectedMetadataText -and
                -not (Test-ContainsIgnoringWhitespace -Text $result.MetadataText -Needle $script:FakeSecret) -and
                -not (Test-ContainsIgnoringWhitespace -Text $result.MetadataText -Needle $script:BusUrl) -and
                -not (Test-ContainsIgnoringWhitespace -Text $result.MetadataText -Needle ([string]$case.LeakToken))
            ) ('expected=' + $expectedMetadataText + '; actual=' + $result.MetadataText)
        } elseif ($case.IsRead) {
            $localMetadataUntouched = if ($case.AliasOutputMetadata) {
                [string]$result.MetadataText -ceq [string]$result.Sentinel
            } else {
                -not (Test-Path -LiteralPath $result.MetadataPath)
            }
            Assert-True ($prefix + ' makes no metadata write during local preflight') (
                $localMetadataUntouched
            )
        }

        if ($case.Valid) {
            Assert-True ($prefix + ' succeeds') ($result.ExitCode -eq 0) ('exit=' + $result.ExitCode)
            if ($case.UseOutFile) {
                Assert-True ($prefix + ' creates or replaces the requested output file') (
                    Test-Path -LiteralPath $result.OutPath -PathType Leaf
                )
                Assert-True ($prefix + ' preserves the original response text without reserialization') (
                    [string]$result.OutText -ceq [string]$case.Body
                )
                $expectedBytes = [Text.Encoding]::UTF8.GetBytes([string]$case.Body)
                $actualBytes = if (Test-Path -LiteralPath $result.OutPath -PathType Leaf) {
                    [IO.File]::ReadAllBytes($result.OutPath)
                } else { [byte[]]@() }
                Assert-True ($prefix + ' preserves the original UTF-8 response bytes') (
                    Test-ByteArraysEqual -Left $expectedBytes -Right $actualBytes
                ) ('expected=' + $expectedBytes.Length + ' actual=' + $actualBytes.Length)
            } else {
                Assert-True ($prefix + ' creates no implicit output file') (
                    -not (Test-Path -LiteralPath $result.OutPath)
                )
                Assert-True ($prefix + ' emits the original response text without reserialization') (
                    [string]$result.Rendered -ceq [string]$case.Body
                )
                Assert-True ($prefix + ' emits exactly one response record') (
                    @($result.Output).Count -eq 1
                ) ('records=' + @($result.Output).Count)
            }
            Assert-True ($prefix + ' emits no semantic-gate error') (
                -not $result.Rendered.Contains('BUS_READ_RESPONSE_INVALID')
            )
        } else {
            Assert-True ($prefix + ' fails closed') ($result.ExitCode -ne 0) ('exit=' + $result.ExitCode)
            Assert-True ($prefix + ' reports the stable semantic code') (
                $result.Rendered.Contains([string]$case.ExpectedErrorCode)
            )
            Assert-True ($prefix + ' keeps diagnostics bounded') (
                $result.Rendered.Length -le 2048
            ) ('length=' + $result.Rendered.Length)
            if (-not [string]::IsNullOrWhiteSpace([string]$case.LeakToken)) {
                Assert-True ($prefix + ' never emits the response-body canary') (
                    -not (Test-ContainsIgnoringWhitespace -Text $result.Rendered -Needle ([string]$case.LeakToken))
                )
            }
            if ($case.UseOutFile) {
                Assert-True ($prefix + ' leaves a pre-existing output file byte-for-byte unchanged') (
                    [string]$result.OutText -ceq [string]$result.Sentinel
                )
            } else {
                Assert-True ($prefix + ' creates no implicit output file') (
                    -not (Test-Path -LiteralPath $result.OutPath)
                )
            }
        }
    }

    Wait-Job -Job $serverJob -Timeout 15 | Out-Null
    Assert-True 'loopback response server handled every case without an internal failure' (
        $serverJob.State -ceq 'Completed' -and
        -not (Test-Path -LiteralPath $serverErrorPath -PathType Leaf)
    ) $(if (Test-Path -LiteralPath $serverErrorPath -PathType Leaf) {
        [IO.File]::ReadAllText($serverErrorPath, [Text.Encoding]::UTF8)
    } else { 'state=' + $serverJob.State })
} finally {
    if ($serverJob) {
        Stop-Job -Job $serverJob -ErrorAction SilentlyContinue
        Remove-Job -Job $serverJob -Force -ErrorAction SilentlyContinue
    }
    if ($KeepArtifacts) {
        [Console]::Out.WriteLine('Artifacts: ' + $script:TestRoot)
    } elseif (Test-Path -LiteralPath $script:TestRoot -PathType Container) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

[Console]::Out.WriteLine('')
[Console]::Out.WriteLine(('RESULT passed={0} failed={1}' -f $script:Passed, $script:Failed))
if ($script:Failed -gt 0) { exit 1 }
