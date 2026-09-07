#Requires -Version 5.1
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ModulePath = Join-Path $RepoRoot 'scripts\OrderSupervisor.psm1'
$RunnerPath = Join-Path $RepoRoot 'scripts\order_supervisor.ps1'
Import-Module $ModulePath -Force

$script:Passed = 0
$script:Failed = 0
function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) {
        $script:Passed++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Failed++
        Write-Output ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
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

function New-BoardJson {
    param([object[]]$DataRows = @())

    $header = @(
        'Row_ID', 'Timestamp', 'Source_Tag', 'Target_Surface', 'Action_Type',
        'Payload', 'Category', 'Project Tag', 'Gist', 'Sub-Gist'
    )
    $rows = New-Object System.Collections.Generic.List[object]
    $rows.Add($header)
    foreach ($row in @($DataRows)) { $rows.Add(@($row)) }
    return ([ordered]@{ ok = $true; rows = $rows.ToArray() } | ConvertTo-Json -Depth 8 -Compress)
}

function New-EligibleOrderRow {
    param([string]$Id = 'ORDER-READ-RETRY')

    return @(
        $Id,
        [DateTime]::UtcNow.AddMinutes(-1).ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture),
        'codex',
        'vm-order-worker',
        'APPEND',
        ('BCB|v=1|id=' + $Id + '|phase=DISPATCH|class=BUILD|from=codex|to=vm-order-worker|authority=operator-direct|task=read only'),
        'OPEN',
        'ORDER-SUPERVISOR',
        '',
        ''
    )
}

$fakeBusSource = @'
param(
    [string]$Action,
    [string]$Title,
    [string]$OutFile,
    [string]$EnvFile,
    [string]$SheetRowJson,
    [string]$ReadMetadataOutFile
)
$ErrorActionPreference = 'Stop'
$root = [string]$env:ORDER_READ_TEST_ROOT
if ([string]::IsNullOrWhiteSpace($root)) { throw 'test_root_missing' }
$countPath = Join-Path $root 'read-count.txt'
$actionsPath = Join-Path $root 'actions.txt'
$count = 0
if (Test-Path -LiteralPath $countPath -PathType Leaf) {
    $count = [int][IO.File]::ReadAllText($countPath, [Text.Encoding]::UTF8)
}
$count++
[IO.File]::WriteAllText($countPath, [string]$count, [Text.UTF8Encoding]::new($false))
[IO.File]::AppendAllText($actionsPath, ([string]$Action + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
if ($Action -cne 'read') { throw 'test_non_read_action' }
$responsePath = Join-Path $root ('response-' + $count + '.txt')
if (-not (Test-Path -LiteralPath $responsePath -PathType Leaf)) { throw 'test_response_missing' }
$response = [IO.File]::ReadAllText($responsePath, [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($OutFile, $response, [Text.UTF8Encoding]::new($false))
$metadata = [ordered]@{
    transport_exit = 0
    http_status = 200
    content_type_class = 'json'
}
$failurePath = Join-Path $root ('failure-' + $count + '.json')
if (Test-Path -LiteralPath $failurePath -PathType Leaf) {
    $spec = [IO.File]::ReadAllText($failurePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $clientErrorProperty = $spec.PSObject.Properties['client_error']
    if ($clientErrorProperty -and [bool]$clientErrorProperty.Value) {
        throw [InvalidOperationException]::new('simulated_local_client_failure')
    }
    foreach ($key in @('transport_exit', 'http_status')) {
        $property = $spec.PSObject.Properties[$key]
        if ($property -and $null -ne $property.Value) { $metadata[$key] = [int]$property.Value }
    }
    $contentTypeProperty = $spec.PSObject.Properties['content_type_class']
    if ($contentTypeProperty -and $null -ne $contentTypeProperty.Value) {
        $metadata.content_type_class = [string]$contentTypeProperty.Value
    }
}
if ($ReadMetadataOutFile) {
    $metadataJson = $metadata | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($ReadMetadataOutFile, $metadataJson, [Text.UTF8Encoding]::new($false))
}
'@

$fakeClaudeSource = @'
[IO.File]::WriteAllText(
    [string]$env:ORDER_READ_TEST_CLAUDE_MARKER,
    'called',
    [Text.UTF8Encoding]::new($false)
)
exit 99
'@

function Invoke-ReadCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string[]]$Responses,
        [ValidateSet('Observe', 'Execute')][string]$Mode = 'Execute',
        [hashtable]$FailureByAttempt = @{}
    )

    $caseRoot = Join-Path $script:TestRoot $Name
    $releaseRoot = Join-Path $caseRoot 'release'
    $scriptsRoot = Join-Path $releaseRoot 'scripts'
    $workspace = Join-Path $caseRoot 'workspace'
    $statePath = Join-Path $caseRoot 'state.json'
    $logPath = Join-Path $caseRoot 'events.jsonl'
    $envPath = Join-Path $caseRoot 'test.env'
    $markerPath = Join-Path $caseRoot 'claude-called.txt'
    $fakeClaudePath = Join-Path $caseRoot 'fake-claude.ps1'

    New-Item -ItemType Directory -Path $scriptsRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $workspace '.git') -Force | Out-Null
    Copy-Item -LiteralPath $RunnerPath -Destination (Join-Path $scriptsRoot 'order_supervisor.ps1')
    Copy-Item -LiteralPath $ModulePath -Destination (Join-Path $scriptsRoot 'OrderSupervisor.psm1')
    Write-TestUtf8 -Path (Join-Path $scriptsRoot 'bus.ps1') -Text $fakeBusSource
    Write-TestUtf8 -Path $fakeClaudePath -Text $fakeClaudeSource
    Write-TestUtf8 -Path $envPath -Text 'TEST_ONLY=1'
    for ($index = 0; $index -lt $Responses.Count; $index++) {
        Write-TestUtf8 -Path (Join-Path $caseRoot ('response-' + ($index + 1) + '.txt')) -Text $Responses[$index]
    }
    foreach ($attempt in @($FailureByAttempt.Keys)) {
        $failureJson = $FailureByAttempt[$attempt] | ConvertTo-Json -Compress
        Write-TestUtf8 -Path (Join-Path $caseRoot ('failure-' + $attempt + '.json')) -Text $failureJson
    }

    $state = New-OrderState -Mode $Mode
    $state.initialized = $true
    $state.cursor.timestamp = '2026-09-07T08:00:00.0000000Z'
    $state.cursor.row_id = 'cursor-before-read'
    Save-OrderState -Path $statePath -State $state

    $previousRoot = [Environment]::GetEnvironmentVariable('ORDER_READ_TEST_ROOT', 'Process')
    $previousMarker = [Environment]::GetEnvironmentVariable('ORDER_READ_TEST_CLAUDE_MARKER', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_ROOT', $caseRoot, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_CLAUDE_MARKER', $markerPath, 'Process')
        $arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', (Join-Path $scriptsRoot 'order_supervisor.ps1'),
            '-Mode', $Mode,
            '-StatePath', $statePath,
            '-LogPath', $logPath,
            '-EnvFile', $envPath,
            '-WorkspacePath', $workspace,
            '-ClaudeCommand', $fakeClaudePath
        )
        $output = @(& powershell.exe @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_ROOT', $previousRoot, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_CLAUDE_MARKER', $previousMarker, 'Process')
    }

    $jsonLine = @($output | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    $result = if ($jsonLine.Count -eq 1) { $jsonLine[0] | ConvertFrom-Json } else { $null }
    $savedState = [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $events = if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        @([IO.File]::ReadAllLines($logPath, [Text.Encoding]::UTF8) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { [string]$_ | ConvertFrom-Json })
    } else { @() }
    $readCountPath = Join-Path $caseRoot 'read-count.txt'
    $readCount = if (Test-Path -LiteralPath $readCountPath -PathType Leaf) {
        [int][IO.File]::ReadAllText($readCountPath, [Text.Encoding]::UTF8)
    } else { 0 }
    $actionsPath = Join-Path $caseRoot 'actions.txt'
    $actions = if (Test-Path -LiteralPath $actionsPath -PathType Leaf) {
        @([IO.File]::ReadAllLines($actionsPath, [Text.Encoding]::UTF8))
    } else { @() }

    return [pscustomobject][ordered]@{
        exit_code = $exitCode
        output = @($output)
        result = $result
        state = $savedState
        events = @($events)
        read_count = $readCount
        actions = @($actions)
        log_text = $(if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            [IO.File]::ReadAllText($logPath, [Text.Encoding]::UTF8)
        } else { '' })
        claude_called = Test-Path -LiteralPath $markerPath -PathType Leaf
    }
}

function Invoke-ActualBusMetadataCase {
    $caseRoot = Join-Path $script:TestRoot 'actual-bus-sidecar'
    $fakeBin = Join-Path $caseRoot 'bin'
    $fakeCurlPath = Join-Path $fakeBin 'curl.exe'
    $envPath = Join-Path $caseRoot 'test.env'
    $outPath = Join-Path $caseRoot 'response.txt'
    $metadataPath = Join-Path $caseRoot 'metadata.json'
    New-Item -ItemType Directory -Path $fakeBin -Force | Out-Null

    $fakeCurlSource = @'
using System;
using System.IO;
using System.Text;

public static class FakeCurl {
    public static int Main(string[] args) {
        string headerPath = null;
        for (int i = 0; i + 1 < args.Length; i++) {
            if (args[i] == "-D") { headerPath = args[i + 1]; break; }
        }
        if (!String.IsNullOrEmpty(headerPath)) {
            File.WriteAllText(
                headerPath,
                Environment.GetEnvironmentVariable("ORDER_READ_FAKE_CURL_HEADERS") ?? String.Empty,
                new UTF8Encoding(false)
            );
        }
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.Write(Environment.GetEnvironmentVariable("ORDER_READ_FAKE_CURL_BODY") ?? String.Empty);
        int exitCode;
        return Int32.TryParse(Environment.GetEnvironmentVariable("ORDER_READ_FAKE_CURL_EXIT"), out exitCode)
            ? exitCode
            : 0;
    }
}
'@
    Add-Type -TypeDefinition $fakeCurlSource -Language CSharp -OutputAssembly $fakeCurlPath -OutputType ConsoleApplication
    Write-TestUtf8 -Path $envPath -Text @'
BUS_URL=https://BUS_URL_CANARY.invalid/private
BUS_SECRET=BUS_SECRET_CANARY
'@

    $variables = @(
        'PATH',
        'ORDER_READ_FAKE_CURL_HEADERS',
        'ORDER_READ_FAKE_CURL_BODY',
        'ORDER_READ_FAKE_CURL_EXIT'
    )
    $previous = @{}
    foreach ($name in $variables) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        [Environment]::SetEnvironmentVariable('PATH', ($fakeBin + [IO.Path]::PathSeparator + $previous.PATH), 'Process')
        [Environment]::SetEnvironmentVariable(
            'ORDER_READ_FAKE_CURL_HEADERS',
            "HTTP/1.1 503 Service Unavailable`r`nContent-Type: text/html; boundary=RAW_HEADER_CANARY`r`n`r`n",
            'Process'
        )
        [Environment]::SetEnvironmentVariable('ORDER_READ_FAKE_CURL_BODY', '<html>ACTUAL_BUS_BODY_CANARY</html>', 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_FAKE_CURL_EXIT', '7', 'Process')
        $output = @(& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $RepoRoot 'scripts\bus.ps1') `
            -Action read `
            -Title 'test board' `
            -OutFile $outPath `
            -EnvFile $envPath `
            -ReadMetadataOutFile $metadataPath 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        foreach ($name in $variables) {
            [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
        }
    }

    $metadataText = [IO.File]::ReadAllText($metadataPath, [Text.Encoding]::UTF8)
    return [pscustomobject][ordered]@{
        exit_code = $exitCode
        output = @($output)
        metadata_text = $metadataText
        metadata = $metadataText | ConvertFrom-Json
    }
}

$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-read-resilience-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
try {
    $validEmpty = New-BoardJson
    $eligible = New-BoardJson -DataRows (, (New-EligibleOrderRow))

    $actualBusSidecar = Invoke-ActualBusMetadataCase
    Assert-True 'actual bus captures native curl exit and HTTP status as numbers' (
        $actualBusSidecar.metadata.transport_exit -isnot [string] -and
        [int]$actualBusSidecar.metadata.transport_exit -eq 7 -and
        $actualBusSidecar.metadata.http_status -isnot [string] -and
        [int]$actualBusSidecar.metadata.http_status -eq 503
    )
    Assert-True 'actual bus reduces raw content type to a safe enum' (
        $actualBusSidecar.metadata.content_type_class -ceq 'html'
    )
    Assert-True 'actual bus sidecar contains only allow-listed sanitized fields' (
        (@($actualBusSidecar.metadata.PSObject.Properties.Name | Sort-Object) -join ',') -ceq
            'content_type_class,http_status,transport_exit' -and
        -not $actualBusSidecar.metadata_text.Contains('RAW_HEADER_CANARY') -and
        -not $actualBusSidecar.metadata_text.Contains('ACTUAL_BUS_BODY_CANARY') -and
        -not $actualBusSidecar.metadata_text.Contains('BUS_URL_CANARY') -and
        -not $actualBusSidecar.metadata_text.Contains('BUS_SECRET_CANARY')
    )

    $validFirst = Invoke-ReadCase -Name 'valid-first' -Responses @($validEmpty, '<unused>')
    Assert-True 'valid-first exits zero' ($validFirst.exit_code -eq 0)
    Assert-True 'valid-first reads exactly once' ($validFirst.read_count -eq 1)
    Assert-True 'valid-first does not emit retry' (@($validFirst.events | Where-Object event -ceq 'board_read_retry').Count -eq 0)
    Assert-True 'valid-first remains read-only before admission' (
        @($validFirst.actions).Count -eq 1 -and @($validFirst.actions | Where-Object { $_ -cne 'read' }).Count -eq 0
    )
    Assert-True 'valid-first never invokes Claude' (-not $validFirst.claude_called)

    $malformedCanary = '<!DOCTYPE html><title>ORDER_READ_BODY_CANARY</title>'
    $malformedThenValid = Invoke-ReadCase `
        -Name 'malformed-then-valid' `
        -Responses @($malformedCanary, $eligible) `
        -Mode Observe
    $retryEvents = @($malformedThenValid.events | Where-Object event -ceq 'board_read_retry')
    Assert-True 'malformed-then-valid exits zero' ($malformedThenValid.exit_code -eq 0)
    Assert-True 'malformed-then-valid reaches admission after retry' (
        $malformedThenValid.result -and $malformedThenValid.result.status -ceq 'candidate_observed'
    )
    Assert-True 'malformed-then-valid performs exactly two reads' ($malformedThenValid.read_count -eq 2)
    Assert-True 'malformed-then-valid emits one fixed retry event' (
        $retryEvents.Count -eq 1 -and
        $retryEvents[0].code -ceq 'BOARD_READ_JSON_INVALID' -and
        $retryEvents[0].details.attempt -ceq '1' -and
        $retryEvents[0].details.code -ceq 'BOARD_READ_JSON_INVALID'
    )
    Assert-True 'retry event carries safe response fingerprint and elapsed time' (
        [int]$retryEvents[0].details.content_length -gt 0 -and
        [string]$retryEvents[0].details.content_sha256 -cmatch '^[0-9a-f]{64}$' -and
        -not [string]::IsNullOrWhiteSpace([string]$retryEvents[0].details.elapsed_ms)
    )
    Assert-True 'retry event never records raw response body' (-not $malformedThenValid.log_text.Contains('ORDER_READ_BODY_CANARY'))
    Assert-True 'no write or Claude occurs before valid parse' (
        @($malformedThenValid.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        -not $malformedThenValid.claude_called
    )

    $transportThenValid = Invoke-ReadCase `
        -Name 'transport-then-valid' `
        -Responses @('<transport failure body>', $validEmpty) `
        -FailureByAttempt @{ 1 = [ordered]@{ transport_exit = 7 } }
    $transportRetry = @($transportThenValid.events | Where-Object event -ceq 'board_read_retry')
    Assert-True 'native transport failure retries once then succeeds' (
        $transportThenValid.exit_code -eq 0 -and $transportThenValid.read_count -eq 2
    )
    Assert-True 'native transport exit is retained safely when available' (
        $transportRetry.Count -eq 1 -and
        $transportRetry[0].code -ceq 'BOARD_READ_TRANSPORT_ERROR' -and
        $transportRetry[0].details.transport_exit -ceq '7'
    )

    $httpThenValid = Invoke-ReadCase `
        -Name 'http-then-valid' `
        -Responses @('<html>HTTP_BODY_CANARY</html>', $validEmpty) `
        -FailureByAttempt @{ 1 = [ordered]@{
            transport_exit = 0
            http_status = 503
            content_type_class = 'html'
        } }
    $httpRetry = @($httpThenValid.events | Where-Object event -ceq 'board_read_retry')
    Assert-True 'HTTP failure retries once then succeeds' (
        $httpThenValid.exit_code -eq 0 -and $httpThenValid.read_count -eq 2
    )
    Assert-True 'HTTP retry exposes only allow-listed metadata' (
        $httpRetry.Count -eq 1 -and
        $httpRetry[0].code -ceq 'BOARD_READ_HTTP_ERROR' -and
        $httpRetry[0].details.transport_exit -ceq '0' -and
        $httpRetry[0].details.http_status -ceq '503' -and
        $httpRetry[0].details.content_type_class -ceq 'html'
    )
    Assert-True 'HTTP retry logs no raw body' (-not $httpThenValid.log_text.Contains('HTTP_BODY_CANARY'))

    foreach ($permanentStatus in @(401, 403)) {
        $permanentHttp = Invoke-ReadCase `
            -Name ('http-' + $permanentStatus + '-no-retry') `
            -Responses @('<html>PERMANENT_HTTP_BODY_CANARY</html>', $validEmpty) `
            -FailureByAttempt @{ 1 = [ordered]@{
                transport_exit = 0
                http_status = $permanentStatus
                content_type_class = 'html'
            } }
        Assert-True ('HTTP ' + $permanentStatus + ' is not retried') (
            $permanentHttp.exit_code -eq 20 -and
            $permanentHttp.read_count -eq 1 -and
            @($permanentHttp.events | Where-Object event -ceq 'board_read_retry').Count -eq 0
        )
        Assert-True ('HTTP ' + $permanentStatus + ' retains fixed HTTP code and side-effect boundary') (
            $permanentHttp.result.error_code -ceq 'BOARD_READ_HTTP_ERROR' -and
            $permanentHttp.state.cursor.row_id -ceq 'cursor-before-read' -and
            @($permanentHttp.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
            -not $permanentHttp.claude_called -and
            -not $permanentHttp.log_text.Contains('PERMANENT_HTTP_BODY_CANARY')
        )
    }

    $clientFailure = Invoke-ReadCase `
        -Name 'deterministic-client-failure' `
        -Responses @($validEmpty, $validEmpty) `
        -FailureByAttempt @{ 1 = [ordered]@{ client_error = $true } }
    Assert-True 'deterministic bus client failure is not retried' (
        $clientFailure.exit_code -eq 20 -and
        $clientFailure.read_count -eq 1 -and
        @($clientFailure.events | Where-Object event -ceq 'board_read_retry').Count -eq 0
    )
    Assert-True 'deterministic bus client failure has fixed safe classification' (
        $clientFailure.result.error_code -ceq 'BOARD_READ_CLIENT_ERROR' -and
        -not $clientFailure.log_text.Contains('simulated_local_client_failure') -and
        @($clientFailure.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        -not $clientFailure.claude_called
    )

    $twiceCanaryOne = '<html>ORDER_READ_FIRST_BODY_CANARY</html>'
    $twiceCanaryTwo = 'undefined ORDER_READ_SECOND_BODY_CANARY'
    $malformedTwice = Invoke-ReadCase `
        -Name 'malformed-twice' `
        -Responses @($twiceCanaryOne, $twiceCanaryTwo)
    $twiceRetries = @($malformedTwice.events | Where-Object event -ceq 'board_read_retry')
    $twiceRunErrors = @($malformedTwice.events | Where-Object event -ceq 'run_error')
    $expectedSecondLength = [Text.Encoding]::UTF8.GetByteCount($twiceCanaryTwo)
    $expectedSecondHash = Get-StringSha256 -Text $twiceCanaryTwo
    Assert-True 'malformed-twice exits fixed decimal 20' (
        $malformedTwice.exit_code -eq 20 -and
        $malformedTwice.result -and
        $malformedTwice.result.error_code -ceq 'BOARD_READ_JSON_INVALID'
    )
    Assert-True 'malformed-twice is bounded to two reads and one retry' (
        $malformedTwice.read_count -eq 2 -and $twiceRetries.Count -eq 1
    )
    Assert-True 'malformed-twice preserves initialized state and cursor' (
        [bool]$malformedTwice.state.initialized -and
        $malformedTwice.state.cursor.timestamp -ceq '2026-09-07T08:00:00.0000000Z' -and
        $malformedTwice.state.cursor.row_id -ceq 'cursor-before-read'
    )
    Assert-True 'malformed-twice persists fixed BOARD error' (
        $malformedTwice.state.error.code -ceq 'BOARD_READ_JSON_INVALID'
    )
    Assert-True 'malformed-twice run_error retains sanitized second-attempt evidence' (
        $twiceRunErrors.Count -eq 1 -and
        $twiceRunErrors[0].code -ceq 'BOARD_READ_JSON_INVALID' -and
        $twiceRunErrors[0].details.attempt -ceq '2' -and
        [int]$twiceRunErrors[0].details.content_length -eq $expectedSecondLength -and
        $twiceRunErrors[0].details.content_sha256 -ceq $expectedSecondHash -and
        -not [string]::IsNullOrWhiteSpace([string]$twiceRunErrors[0].details.elapsed_ms)
    )
    Assert-True 'malformed-twice never writes or invokes Claude' (
        @($malformedTwice.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        -not $malformedTwice.claude_called
    )
    Assert-True 'malformed-twice logs no raw bodies' (
        -not $malformedTwice.log_text.Contains('ORDER_READ_FIRST_BODY_CANARY') -and
        -not $malformedTwice.log_text.Contains('ORDER_READ_SECOND_BODY_CANARY')
    )

    $emptyThenValid = Invoke-ReadCase -Name 'empty-then-valid' -Responses @('', $validEmpty)
    $emptyRetries = @($emptyThenValid.events | Where-Object event -ceq 'board_read_retry')
    Assert-True 'truly empty response retries once then succeeds' (
        $emptyThenValid.exit_code -eq 0 -and
        $emptyThenValid.read_count -eq 2 -and
        $emptyRetries.Count -eq 1 -and
        $emptyRetries[0].code -ceq 'BOARD_READ_RESPONSE_EMPTY'
    )

    foreach ($rootCase in @(
        [pscustomobject]@{ name = 'root-false'; response = 'false'; code = 'BOARD_READ_RESPONSE_SHAPE_INVALID' },
        [pscustomobject]@{ name = 'root-zero'; response = '0'; code = 'BOARD_READ_RESPONSE_SHAPE_INVALID' },
        [pscustomobject]@{ name = 'root-string'; response = '"text"'; code = 'BOARD_READ_RESPONSE_SHAPE_INVALID' },
        [pscustomobject]@{ name = 'root-null'; response = 'null'; code = 'BOARD_READ_RESPONSE_SHAPE_INVALID' },
        [pscustomobject]@{ name = 'root-array'; response = '[]'; code = 'BOARD_READ_RESPONSE_SHAPE_INVALID' },
        [pscustomobject]@{ name = 'root-true'; response = 'true'; code = 'BOARD_READ_RESPONSE_SHAPE_INVALID' },
        [pscustomobject]@{ name = 'root-empty-object'; response = '{}'; code = 'BOARD_READ_REFUSED' }
    )) {
        $rootFailure = Invoke-ReadCase -Name $rootCase.name -Responses @($rootCase.response, $validEmpty)
        Assert-True ($rootCase.name + ' is fixed and not retried') (
            $rootFailure.exit_code -eq 20 -and
            $rootFailure.read_count -eq 1 -and
            $rootFailure.result.error_code -ceq $rootCase.code -and
            @($rootFailure.events | Where-Object event -ceq 'board_read_retry').Count -eq 0
        )
        Assert-True ($rootCase.name + ' preserves pre-admission side-effect boundary') (
            $rootFailure.state.cursor.row_id -ceq 'cursor-before-read' -and
            @($rootFailure.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
            -not $rootFailure.claude_called
        )
    }

    foreach ($logicalCase in @(
        [pscustomobject]@{ name = 'logical-refusal'; response = '{"ok":false,"rows":[]}'; code = 'BOARD_READ_REFUSED' },
        [pscustomobject]@{ name = 'schema-missing-rows'; response = '{"ok":true}'; code = 'BOARD_ROWS_MISSING' }
    )) {
        $logical = Invoke-ReadCase `
            -Name $logicalCase.name `
            -Responses @($logicalCase.response, $validEmpty)
        Assert-True ($logicalCase.name + ' is not retried') (
            $logical.exit_code -eq 20 -and
            $logical.read_count -eq 1 -and
            @($logical.events | Where-Object event -ceq 'board_read_retry').Count -eq 0
        )
        Assert-True ($logicalCase.name + ' retains fixed logical code') (
            $logical.result -and $logical.result.error_code -ceq $logicalCase.code
        )
        Assert-True ($logicalCase.name + ' preserves cursor and side-effect boundary') (
            $logical.state.cursor.row_id -ceq 'cursor-before-read' -and
            @($logical.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
            -not $logical.claude_called
        )
    }

    $runnerSource = [IO.File]::ReadAllText($RunnerPath, [Text.Encoding]::UTF8)
    Assert-True 'retry wrapper is used only for initial pre-admission read' (
        ([regex]::Matches($runnerSource, '\$rows\s*=\s*@\(Read-BoardPreAdmission\)')).Count -eq 1 -and
        $runnerSource.Contains('$current = @(Read-Board)') -and
        $runnerSource.Contains('$readOperation = { Read-Board }')
    )
} finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $script:TestRoot -PathType Container)) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
