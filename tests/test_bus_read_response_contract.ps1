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
                    $head = "HTTP/1.1 200 OK`r`nContent-Type: application/json; charset=utf-8`r`nContent-Length: $($responseBytes.Length)`r`nConnection: close`r`n`r`n"
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
        [AllowEmptyString()][string]$LeakToken = ''
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
        $arguments += @('-ReadMetadataOutFile', $metadataPath)
    } else {
        $arguments += @('-Text', 'append compatibility payload')
    }

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $ChildShell @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    $requestPath = Join-Path $script:TestRoot ('request-' + $Index + '.json')
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
    (New-ReadCase -Name 'self-hosted-doc-empty-body' -Valid $true -Title 'Hosted Doc' -Body '{"ok":true,"fileId":"doc-hosted","title":"Hosted Doc","kind":"doc","revision":8,"body":""}'),
    (New-ReadCase -Name 'file-id-selector' -Valid $true -Title '' -FileId 'selected-file' -Body '{"ok":true,"fileId":"selected-file","title":"Any Nonempty Title","rows":[]}'),
    (New-ReadCase -Name 'both-selectors' -Valid $true -Title 'Both Selectors' -FileId 'both-file' -Body '{"ok":true,"fileId":"both-file","title":"Both Selectors","rows":[]}'),
    (New-ReadCase -Name 'uppercase-read' -Valid $true -Action 'READ' -Title 'Uppercase Action' -Body '{"ok":true,"fileId":"upper-file","title":"Uppercase Action","rows":[]}'),
    (New-ReadCase -Name 'unicode-raw-preservation' -Valid $true -Title 'Unicode Sheet' -Body $unicodeRawBody),
    (New-ReadCase -Name 'valid-stdout-preservation' -Valid $true -UseOutFile $false -Title 'Stdout Sheet' -Body $unicodeStdoutBody),
    (New-ReadCase -Name 'no-selector-generic-identity' -Valid $true -Title '' -Body '{"ok":true,"fileId":"generic-file","title":"Generic Title","rows":[]}'),

    # Regression and mutation control: the observed HTTP-200 health object must
    # fail without changing a preseeded output file or leaking its body to stdout.
    (New-ReadCase -Name 'health-object-preseed' -Body '{"ok":true,"service":"blackboard-bus","canary":"HEALTH_BODY_CANARY_PRESEED"}' -LeakToken 'HEALTH_BODY_CANARY_PRESEED'),
    (New-ReadCase -Name 'health-object-stdout' -UseOutFile $false -Body '{"ok":true,"service":"blackboard-bus","canary":"HEALTH_BODY_CANARY_STDOUT"}' -LeakToken 'HEALTH_BODY_CANARY_STDOUT'),
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
    (New-ReadCase -Name 'whitespace-title-selector' -Title '   ' -Body '{"ok":true,"fileId":"bad","title":"Different Title","rows":[],"canary":"WHITESPACE_TITLE_SELECTOR_CANARY"}' -LeakToken 'WHITESPACE_TITLE_SELECTOR_CANARY'),
    (New-ReadCase -Name 'file-id-mismatch' -Title '' -FileId 'requested-file' -Body '{"ok":true,"fileId":"different-file","title":"Any Title","rows":[],"canary":"FILE_MISMATCH_CANARY"}' -LeakToken 'FILE_MISMATCH_CANARY'),
    (New-ReadCase -Name 'file-id-case-mismatch' -Title '' -FileId 'Requested-File' -Body '{"ok":true,"fileId":"requested-file","title":"Any Title","rows":[],"canary":"FILE_CASE_CANARY"}' -LeakToken 'FILE_CASE_CANARY'),
    (New-ReadCase -Name 'whitespace-file-id-selector' -Title '' -FileId '   ' -Body '{"ok":true,"fileId":"different-file","title":"Any Title","rows":[],"canary":"WHITESPACE_FILE_SELECTOR_CANARY"}' -LeakToken 'WHITESPACE_FILE_SELECTOR_CANARY'),
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
}
$cases += $appendCase

New-Item -ItemType Directory -Path $script:TestRoot -Force | Out-Null
$serverJob = $null
try {
    $port = Get-TestPort
    $envPath = Join-Path $script:TestRoot 'test.env'
    $readyPath = Join-Path $script:TestRoot 'ready.txt'
    $serverErrorPath = Join-Path $script:TestRoot 'server-error.txt'
    Write-TestUtf8 -Path $envPath -Text ("BUS_URL=http://127.0.0.1:$port/`nBUS_SECRET=$($script:FakeSecret)`n")
    for ($index = 0; $index -lt $cases.Count; $index++) {
        Write-TestUtf8 `
            -Path (Join-Path $script:TestRoot ('response-' + ($index + 1) + '.json')) `
            -Text ([string]$cases[$index].Body)
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
        -ResponseCount $cases.Count `
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

        Assert-True ($prefix + ' reached the real bus entrypoint and loopback transport') (
            Test-Path -LiteralPath $result.RequestPath -PathType Leaf
        )
        Assert-True ($prefix + ' never exposes the bus secret') (
            -not $result.Rendered.Contains($script:FakeSecret)
        )

        $request = if ($result.RequestText) { $result.RequestText | ConvertFrom-Json } else { $null }
        $expectedWireAction = if ($case.IsRead) { 'read' } else { [string]$case.Action }
        Assert-True ($prefix + ' sends the expected wire action') (
            $null -ne $request -and [string]$request.action -ceq $expectedWireAction
        ) $(if ($null -eq $request) { 'no captured JSON request' } else { 'action=' + [string]$request.action })
        if ($prefix -ceq 'whitespace-title-selector') {
            Assert-True ($prefix + ' sends the nonempty whitespace Title selector') (
                $null -ne $request -and
                $null -ne $request.PSObject.Properties['title'] -and
                [string]$request.title -ceq '   '
            )
        }
        if ($prefix -ceq 'whitespace-file-id-selector') {
            Assert-True ($prefix + ' sends the nonempty whitespace FileId selector') (
                $null -ne $request -and
                $null -ne $request.PSObject.Properties['fileId'] -and
                [string]$request.fileId -ceq '   '
            )
        }

        if ($case.IsRead) {
            $metadata = if ($result.MetadataText) { $result.MetadataText | ConvertFrom-Json } else { $null }
            Assert-True ($prefix + ' preserves sanitized transport metadata') (
                $null -ne $metadata -and
                $metadata.transport_exit -isnot [string] -and
                [int]$metadata.transport_exit -eq 0 -and
                $metadata.http_status -isnot [string] -and
                [int]$metadata.http_status -eq 200 -and
                [string]$metadata.content_type_class -ceq 'json'
            ) $result.MetadataText
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
                $result.Rendered.Contains('BUS_READ_RESPONSE_INVALID')
            )
            Assert-True ($prefix + ' keeps diagnostics bounded') (
                $result.Rendered.Length -le 2048
            ) ('length=' + $result.Rendered.Length)
            if (-not [string]::IsNullOrWhiteSpace([string]$case.LeakToken)) {
                Assert-True ($prefix + ' never emits the response-body canary') (
                    -not $result.Rendered.Contains([string]$case.LeakToken)
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
