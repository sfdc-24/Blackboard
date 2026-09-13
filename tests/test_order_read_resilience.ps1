#Requires -Version 5.1
param(
    [switch]$KeepArtifacts,
    [switch]$SimulateJsonDateCoercion,
    [ValidateSet('Full', 'IwrOnly')][string]$Scope = 'Full'
)

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

# WHICH SHELL THE CHILD RUNS IN.
#
# Every case here launches scripts/bus.ps1 in a CHILD process, and that child was
# hardcoded to powershell.exe in ten places. So the whole suite only ever exercised
# bus.ps1 under Windows PowerShell 5.1 -- even when the suite itself was started
# from pwsh. bus.ps1's hop-1 error handling differs by EDITION, which meant the
# PowerShell 7 path had no coverage anywhere in CI, and adding this file to the
# pwsh job would not have changed that.
#
# ORDER_TEST_CHILD_SHELL overrides the child. Unset, this behaves exactly as before.
$script:ChildShell = $env:ORDER_TEST_CHILD_SHELL
if ([string]::IsNullOrWhiteSpace($script:ChildShell)) { $script:ChildShell = 'powershell.exe' }
$script:ResolvedChildShell = (Get-Command $script:ChildShell -CommandType Application -ErrorAction Stop |
                              Select-Object -First 1).Source
Write-Output ("CHILD_SHELL " + $script:ResolvedChildShell)

# HOW THIS HARNESS READS JSON, AND WHY NOT WITH A BARE ConvertFrom-Json.
#
# PowerShell 7 turns any ISO-8601-shaped string into [DateTime] on the way in. The
# PRODUCT already refuses to read a board that way -- OrderSupervisor.psm1 has
# ConvertFrom-JsonPreserveStrings for exactly this, after two hosts disagreed about
# admission over it -- but this HARNESS was still calling the bare cmdlet. So under
# pwsh the cursor it read back from the state file was a [DateTime], four exact
# -ceq assertions failed, and the code under test had done nothing wrong: the
# harness was measuring its own parser. It also loses precision, which is the part
# that would not have looked like a parser bug at all -- a round trip turns
# 2026-09-07T08:01:00.0000000Z into 2026-09-07T08:01:00Z.
#
# Splatting the parameter onto the same cmdlet, rather than wrapping it in a helper,
# keeps pipeline and array-unrolling behaviour byte-identical to what every call
# site here already relied on. 5.1 does not coerce and needs no parameter.
#
# -DateKind arrived in PowerShell 7.5. An edition that coerces and cannot be told
# not to is REFUSED here, as the product refuses it, rather than running a suite
# whose failures would describe the harness instead of the bus client.
$script:JsonDateArgs = @{}
if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
    $script:JsonDateArgs = @{ DateKind = 'String' }
} elseif ($PSVersionTable.PSEdition -cne 'Desktop') {
    throw ('This harness cannot read board JSON on ' + $PSVersionTable.PSEdition + ' ' +
           $PSVersionTable.PSVersion + ' without coercing ISO-8601 cells to [DateTime]. ' +
           'PowerShell 7.5 or later provides ConvertFrom-Json -DateKind String. Refusing ' +
           'rather than reporting assertions that measured the harness.')
}

function ConvertTo-FlowedText {
    # PowerShell wraps error records to the CONSOLE WIDTH before they reach the
    # pipeline, so an assertion that matches a long phrase in captured error
    # output is really asserting on the width of whoever ran it. Measured on one
    # machine, one commit, real 5.1: at a 74-column console the hop-2 phrase check
    # below FAILS; at 200 columns the identical run passes. Collapsing every run of
    # whitespace to one space removes the wrap and leaves the phrase intact.
    #
    # Width is not the only thing that gets between an error message and a match.
    # PowerShell 7 renders errors in a BOX, and its continuation lines carry a
    # gutter -- optional line number, a vertical bar, then the text. Collapsing
    # whitespace leaves that bar embedded, so the fixed hop-2 message arrives as
    # '...following up to | 5 redirects' and a Contains check fails against a
    # product that did nothing wrong. Measured with the child on pwsh 7.6.6: one
    # assertion failed on both parent editions, with no canary leaked.
    #
    # The gutter is stripped only at the START of a line, and only here. This
    # helper feeds POSITIVE phrase assertions; every canary-ABSENCE check runs on
    # ConvertTo-SquashedText, which stays strict. Loosening this one cannot make a
    # leak check pass, and that separation is the point of having two functions.
    param([AllowNull()][object[]]$Lines)
    $text = (@($Lines | ForEach-Object { [string]$_ }) -join "`n")
    $text = $text -replace '(?m)^[ \t]*\d*[ \t]*\|[ \t]?', ''
    return ($text -replace '\s+', ' ')
}

function ConvertTo-SquashedText {
    # For canary-ABSENCE checks, collapsing to a space is not enough: a wrap can
    # land INSIDE a long token, and `-not $text.Contains('SOME_LONG_CANARY')` then
    # passes because the canary was split across two lines. A negative assertion
    # that a leaked secret is absent must not be satisfiable by console width.
    # Removing whitespace entirely rejoins any hard-wrapped token before matching.
    param([AllowNull()][object[]]$Lines)
    return ((@($Lines | ForEach-Object { [string]$_ }) -join "`n") -replace '\s+', '')
}

function Test-ExactByteSequence {
    param(
        [AllowNull()][byte[]]$Actual,
        [AllowNull()][byte[]]$Expected
    )

    if ($null -eq $Actual -or $null -eq $Expected -or $Actual.Count -ne $Expected.Count) {
        return $false
    }
    for ($index = 0; $index -lt $Actual.Count; $index++) {
        if ($Actual[$index] -ne $Expected[$index]) { return $false }
    }
    return $true
}

function Test-SanitizedIwrNetworkFailure {
    param(
        [Parameter(Mandatory = $true)]$Run,
        [Parameter(Mandatory = $true)][int]$ExpectedCallCount
    )

    $outputText = ConvertTo-SquashedText -Lines $Run.output
    # Strip presentation markup only for positive message matching. The raw
    # squashed output below remains the input to all canary-absence checks.
    $plainOutputLines = @($Run.output | ForEach-Object {
        [string]$_ -replace '\x1b\[[0-?]*[ -/]*[@-~]', ''
    })
    $positiveOutputText = (ConvertTo-FlowedText -Lines $plainOutputLines) -replace '\s+', ''
    if ($Run.exit_code -eq 0 -or
        @($Run.trace.calls).Count -ne $ExpectedCallCount -or
        $Run.trace.exception_type -cne 'System.Net.WebException' -or
        $Run.trace.exception_message -cne 'BUS_READ_NETWORK_ERROR: read transport failed before an HTTP response was received.' -or
        $null -ne $Run.trace.inner_exception_type -or
        $null -ne $Run.trace.inner_exception_message -or
        $Run.metadata_text -cne '{}' -or
        -not (Test-ExactByteSequence -Actual $Run.metadata_bytes -Expected ([byte[]](0x7b, 0x7d))) -or
        @($Run.metadata.PSObject.Properties).Count -ne 0 -or
        $Run.out_text -cne $Run.sentinel -or
        -not (Test-ExactByteSequence -Actual $Run.out_bytes -Expected $Run.sentinel_bytes) -or
        -not $positiveOutputText.Contains('BUS_READ_NETWORK_ERROR:readtransportfailedbeforeanHTTPresponsewasreceived.')) {
        return $false
    }

    foreach ($canary in @(
        'RAW_IWR_',
        'BUS_URL_IWR_CANARY',
        'BUS_SECRET_IWR_CANARY',
        'ONE_SHOT_IWR_CANARY'
    )) {
        if ($outputText.Contains($canary) -or
            ([string]$Run.metadata_text).Contains($canary) -or
            ([string]$Run.trace_text).Contains($canary)) {
            return $false
        }
    }
    return $true
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
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return [int]$listener.LocalEndpoint.Port
    } finally {
        $listener.Stop()
    }
}

# HOW THE FAKE curl.exe GETS BUILT, AND WHY NOT WITH Add-Type.
#
# The two actual-bus cases need a REAL executable named curl.exe ahead of the system
# one on PATH. It has to be that exact name: bus.ps1 asks for 'curl.exe' before it
# asks for 'curl', so a .cmd or .ps1 shim loses the lookup to C:\Windows\System32\
# curl.exe and the case would silently exercise the real curl instead of the fake.
#
# This used to be Add-Type -OutputType ConsoleApplication. PowerShell 7 removed that
# -- it refuses both ConsoleApplication and WindowsApplication -- so this file died
# 0.2 seconds in under pwsh, on the edition the ORDER worker is being migrated to.
# The suite that guards the bus client had therefore never run on the target edition.
#
# The C# source is unchanged and now goes to the .NET Framework C# compiler, which is
# what Add-Type was driving underneath on 5.1 anyway. csc.exe ships with .NET
# Framework 4 and is present on every supported Windows and on the hosted windows
# runners. Both editions now take THE SAME build path: no edition gets a skip, and no
# edition gets a second implementation of the fake that could drift from the first.
#
# This does NOT make the suite runnable on Linux. There curl has no .exe in its name
# and there is no csc.exe; that needs its own change and its own evidence.
function New-FakeCurlExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $csc = @(
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1

    if (-not $csc) {
        # Fail loudly. A skip here would report a green suite that never built the
        # fake and therefore never tested the bus client's curl path at all.
        throw ('No .NET Framework csc.exe found under ' + $env:WINDIR +
               '\Microsoft.NET. The fake curl.exe cannot be built, so the actual-bus ' +
               'cases cannot run and must not be reported as passing.')
    }

    $sourcePath = [IO.Path]::ChangeExtension($Path, '.cs')
    Write-TestUtf8 -Path $sourcePath -Text $Source

    $output = & $csc /nologo /target:exe ('/out:' + $Path) $sourcePath 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ('csc.exe exited ' + $code + ' building the fake curl: ' +
               (ConvertTo-FlowedText -Lines @($output)))
    }
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
    # This runs in the CHILD process and cannot see the parent's variables, so it
    # works out the same date-coercion guard for itself.
    $childJsonDateArgs = @{}
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
        $childJsonDateArgs = @{ DateKind = 'String' }
    }
    $spec = [IO.File]::ReadAllText($failurePath, [Text.Encoding]::UTF8) | ConvertFrom-Json @childJsonDateArgs
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
        [hashtable]$FailureByAttempt = @{},
        [object[]]$InitialWork = @()
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
    $state.work = @($InitialWork)
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
        $output = @(& $script:ResolvedChildShell @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_ROOT', $previousRoot, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_CLAUDE_MARKER', $previousMarker, 'Process')
    }

    $jsonLine = @($output | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    $result = if ($jsonLine.Count -eq 1) { $jsonLine[0] | ConvertFrom-Json @script:JsonDateArgs } else { $null }
    $savedState = [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8) | ConvertFrom-Json @script:JsonDateArgs
    $events = if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        @([IO.File]::ReadAllLines($logPath, [Text.Encoding]::UTF8) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { [string]$_ | ConvertFrom-Json @script:JsonDateArgs })
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

function Invoke-ActualBusClosedPortSupervisorCase {
    $caseRoot = Join-Path $script:TestRoot 'actual-bus-iwr-closed-port-supervisor'
    $releaseRoot = Join-Path $caseRoot 'release'
    $scriptsRoot = Join-Path $releaseRoot 'scripts'
    $workspace = Join-Path $caseRoot 'workspace'
    $emptyPath = Join-Path $caseRoot 'empty-path'
    $statePath = Join-Path $caseRoot 'state.json'
    $logPath = Join-Path $caseRoot 'events.jsonl'
    $envPath = Join-Path $caseRoot 'test.env'
    $markerPath = Join-Path $caseRoot 'claude-called.txt'
    $fakeClaudePath = Join-Path $caseRoot 'fake-claude.ps1'

    New-Item -ItemType Directory -Path $scriptsRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $workspace '.git') -Force | Out-Null
    New-Item -ItemType Directory -Path $emptyPath -Force | Out-Null
    Copy-Item -LiteralPath $RunnerPath -Destination (Join-Path $scriptsRoot 'order_supervisor.ps1')
    Copy-Item -LiteralPath $ModulePath -Destination (Join-Path $scriptsRoot 'OrderSupervisor.psm1')
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'scripts\bus.ps1') -Destination (Join-Path $scriptsRoot 'bus.ps1')
    Write-TestUtf8 -Path $fakeClaudePath -Text $fakeClaudeSource

    $closedPort = Get-TestPort
    $rawExceptionCanary = 'RAW_IWR_EXCEPTION_CANARY'
    $oneShotCanary = 'ONE_SHOT_IWR_CANARY'
    $busUrl = 'http://127.0.0.1:' + $closedPort + '/' +
        $rawExceptionCanary + '/' + $oneShotCanary
    $secretCanary = 'CLOSED_PORT_SECRET_CANARY'
    Write-TestUtf8 -Path $envPath -Text ("BUS_URL=$busUrl`nBUS_SECRET=$secretCanary`n")

    $state = New-OrderState -Mode Execute
    $state.initialized = $true
    $state.cursor.timestamp = '2026-09-07T08:00:00.0000000Z'
    $state.cursor.row_id = 'cursor-before-closed-port'
    Save-OrderState -Path $statePath -State $state

    $previousPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    $previousMarker = [Environment]::GetEnvironmentVariable('ORDER_READ_TEST_CLAUDE_MARKER', 'Process')
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # The resolved child executable is invoked by absolute path, while an
        # empty child PATH forces the unmodified bus client through genuine IWR.
        [Environment]::SetEnvironmentVariable('PATH', $emptyPath, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_CLAUDE_MARKER', $markerPath, 'Process')
        $ErrorActionPreference = 'Continue'
        $output = @(& $script:ResolvedChildShell `
            -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $scriptsRoot 'order_supervisor.ps1') `
            -Mode Execute `
            -StatePath $statePath `
            -LogPath $logPath `
            -EnvFile $envPath `
            -WorkspacePath $workspace `
            -ClaudeCommand $fakeClaudePath 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        [Environment]::SetEnvironmentVariable('PATH', $previousPath, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_TEST_CLAUDE_MARKER', $previousMarker, 'Process')
    }

    $jsonLine = @($output | ForEach-Object { [string]$_ } |
        Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    $result = if ($jsonLine.Count -eq 1) {
        $jsonLine[0] | ConvertFrom-Json @script:JsonDateArgs
    } else { $null }
    $events = if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        @([IO.File]::ReadAllLines($logPath, [Text.Encoding]::UTF8) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { [string]$_ | ConvertFrom-Json @script:JsonDateArgs })
    } else { @() }
    $savedState = [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8) |
        ConvertFrom-Json @script:JsonDateArgs

    return [pscustomobject][ordered]@{
        exit_code = $exitCode
        output = @($output)
        result = $result
        events = @($events)
        state = $savedState
        log_text = $(if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            [IO.File]::ReadAllText($logPath, [Text.Encoding]::UTF8)
        } else { '' })
        bus_url = $busUrl
        raw_exception_canary = $rawExceptionCanary
        one_shot_canary = $oneShotCanary
        secret_canary = $secretCanary
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
        string outputPath = null;
        for (int i = 0; i + 1 < args.Length; i++) {
            if (args[i] == "-D") { headerPath = args[i + 1]; }
            if (args[i] == "-o") { outputPath = args[i + 1]; }
        }
        if (!String.IsNullOrEmpty(headerPath)) {
            File.WriteAllText(
                headerPath,
                Environment.GetEnvironmentVariable("ORDER_READ_FAKE_CURL_HEADERS") ?? String.Empty,
                new UTF8Encoding(false)
            );
        }
        string body = Environment.GetEnvironmentVariable("ORDER_READ_FAKE_CURL_BODY") ?? String.Empty;
        if (!String.IsNullOrEmpty(outputPath)) {
            File.WriteAllText(outputPath, body, new UTF8Encoding(false));
        } else {
            Console.OutputEncoding = new UTF8Encoding(false);
            Console.Write(body);
        }
        int exitCode;
        return Int32.TryParse(Environment.GetEnvironmentVariable("ORDER_READ_FAKE_CURL_EXIT"), out exitCode)
            ? exitCode
            : 0;
    }
}
'@
    New-FakeCurlExecutable -Source $fakeCurlSource -Path $fakeCurlPath
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
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
                -File (Join-Path $RepoRoot 'scripts\bus.ps1') `
                -Action read `
                -Title 'test board' `
                -OutFile $outPath `
                -EnvFile $envPath `
                -ReadMetadataOutFile $metadataPath 2>&1)
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
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
        metadata = $metadataText | ConvertFrom-Json @script:JsonDateArgs
    }
}

function Invoke-ActualBusSecondRedirectCase {
    $caseRoot = Join-Path $script:TestRoot 'actual-bus-second-redirect'
    $fakeBin = Join-Path $caseRoot 'bin'
    $fakeCurlPath = Join-Path $fakeBin 'curl.exe'
    $envPath = Join-Path $caseRoot 'test.env'
    $outPath = Join-Path $caseRoot 'response.txt'
    $metadataPath = Join-Path $caseRoot 'metadata.json'
    $expectedBody = '{"ok":true,"fileId":"test-board-id","title":"test board","rows":[]}'
    New-Item -ItemType Directory -Path $fakeBin -Force | Out-Null

    $fakeCurlSource = @'
using System;
using System.IO;
using System.Text;

public static class FakeSecondRedirectCurl {
    public static int Main(string[] args) {
        string root = Environment.GetEnvironmentVariable("ORDER_READ_REDIRECT_ROOT");
        if (String.IsNullOrEmpty(root)) { return 90; }

        string countPath = Path.Combine(root, "curl-count.txt");
        int count = File.Exists(countPath) ? Int32.Parse(File.ReadAllText(countPath, Encoding.UTF8)) : 0;
        count++;
        File.WriteAllText(countPath, count.ToString(), new UTF8Encoding(false));
        File.AppendAllText(
            Path.Combine(root, "curl-arguments.txt"),
            String.Join("\u001f", args) + Environment.NewLine,
            new UTF8Encoding(false)
        );

        string headerPath = null;
        string outputPath = null;
        for (int i = 0; i + 1 < args.Length; i++) {
            if (args[i] == "-D") { headerPath = args[i + 1]; }
            if (args[i] == "-o") { outputPath = args[i + 1]; }
        }
        if (String.IsNullOrEmpty(headerPath)) { return 91; }

        if (count == 1) {
            File.WriteAllText(
                headerPath,
                "HTTP/1.1 302 Found\r\nLocation: https://ONE_SHOT_REDIRECT_CANARY.invalid/one-shot\r\n\r\n",
                new UTF8Encoding(false)
            );
            return 0;
        }
        if (count == 2) {
            string finalStatus = Environment.GetEnvironmentVariable("ORDER_READ_REDIRECT_FINAL_STATUS") ?? "200";
            string finalHeader;
            string finalBody;
            if (finalStatus == "503") {
                finalHeader = "HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/html\r\n\r\n";
                finalBody = "<html>FINAL_REDIRECT_BODY_CANARY</html>";
            } else {
                finalHeader = "HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n\r\n";
                finalBody = "{\"ok\":true,\"fileId\":\"test-board-id\",\"title\":\"test board\",\"rows\":[]}";
            }
            File.WriteAllText(
                headerPath,
                "HTTP/1.1 302 Found\r\nLocation: https://CANONICAL_REDIRECT_CANARY.invalid/exec\r\n\r\n" + finalHeader,
                new UTF8Encoding(false)
            );
            if (!String.IsNullOrEmpty(outputPath)) {
                File.WriteAllText(outputPath, finalBody, new UTF8Encoding(false));
            } else {
                Console.OutputEncoding = new UTF8Encoding(false);
                Console.Write(finalBody);
            }
            return 0;
        }
        return 92;
    }
}
'@
    New-FakeCurlExecutable -Source $fakeCurlSource -Path $fakeCurlPath
    Write-TestUtf8 -Path $envPath -Text @'
BUS_URL=https://BUS_URL_REDIRECT_CANARY.invalid/private
BUS_SECRET=BUS_SECRET_REDIRECT_CANARY
'@

    $failureRoot = Join-Path $script:TestRoot 'actual-bus-second-redirect-failure'
    $failureOutPath = Join-Path $failureRoot 'response.txt'
    $failureMetadataPath = Join-Path $failureRoot 'metadata.json'
    New-Item -ItemType Directory -Path $failureRoot -Force | Out-Null

    $variables = @('PATH', 'ORDER_READ_REDIRECT_ROOT', 'ORDER_READ_REDIRECT_FINAL_STATUS')
    $previous = @{}
    foreach ($name in $variables) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        [Environment]::SetEnvironmentVariable('PATH', ($fakeBin + [IO.Path]::PathSeparator + $previous.PATH), 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_REDIRECT_ROOT', $caseRoot, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_REDIRECT_FINAL_STATUS', '200', 'Process')
        $output = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $RepoRoot 'scripts\bus.ps1') `
            -Action read `
            -Title 'test board' `
            -OutFile $outPath `
            -EnvFile $envPath `
            -ReadMetadataOutFile $metadataPath 2>&1)
        $exitCode = $LASTEXITCODE

        [Environment]::SetEnvironmentVariable('ORDER_READ_REDIRECT_ROOT', $failureRoot, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_READ_REDIRECT_FINAL_STATUS', '503', 'Process')
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $failureOutput = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
                -File (Join-Path $RepoRoot 'scripts\bus.ps1') `
                -Action read `
                -Title 'test board' `
                -OutFile $failureOutPath `
                -EnvFile $envPath `
                -ReadMetadataOutFile $failureMetadataPath 2>&1)
            $failureExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    } finally {
        foreach ($name in $variables) {
            [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
        }
    }

    $argumentPath = Join-Path $caseRoot 'curl-arguments.txt'
    $countPath = Join-Path $caseRoot 'curl-count.txt'
    $failureCountPath = Join-Path $failureRoot 'curl-count.txt'
    $argumentLines = $(if (Test-Path -LiteralPath $argumentPath) {
        @([IO.File]::ReadAllLines($argumentPath, [Text.Encoding]::UTF8))
    } else { @() })
    $firstCall = @()
    $secondCall = @()
    if ($argumentLines.Count -ge 1) { $firstCall = @([string]$argumentLines[0] -split ([char]0x1f)) }
    if ($argumentLines.Count -ge 2) { $secondCall = @([string]$argumentLines[1] -split ([char]0x1f)) }
    $callCount = $(if (Test-Path -LiteralPath $countPath) {
        [int][IO.File]::ReadAllText($countPath, [Text.Encoding]::UTF8)
    } else { 0 })
    $failureCallCount = $(if (Test-Path -LiteralPath $failureCountPath) {
        [int][IO.File]::ReadAllText($failureCountPath, [Text.Encoding]::UTF8)
    } else { 0 })
    $metadataText = [IO.File]::ReadAllText($metadataPath, [Text.Encoding]::UTF8)
    $failureMetadataText = [IO.File]::ReadAllText($failureMetadataPath, [Text.Encoding]::UTF8)
    return [pscustomobject][ordered]@{
        exit_code = $exitCode
        output = @($output)
        call_count = $callCount
        first_call = $firstCall
        second_call = $secondCall
        body = [IO.File]::ReadAllText($outPath, [Text.Encoding]::UTF8)
        expected_body = $expectedBody
        metadata_text = $metadataText
        metadata = $metadataText | ConvertFrom-Json @script:JsonDateArgs
        failure_exit_code = $failureExitCode
        failure_output = @($failureOutput)
        failure_call_count = $failureCallCount
        failure_metadata_text = $failureMetadataText
        failure_metadata = $failureMetadataText | ConvertFrom-Json @script:JsonDateArgs
    }
}

function Invoke-ActualBusIwrFallbackCase {
    $caseRoot = Join-Path $script:TestRoot 'actual-bus-iwr-fallback'
    $emptyPath = Join-Path $caseRoot 'empty-path'
    $wrapperPath = Join-Path $caseRoot 'invoke-iwr-fallback.ps1'
    $envPath = Join-Path $caseRoot 'test.env'
    New-Item -ItemType Directory -Path $emptyPath -Force | Out-Null

    $wrapperSource = @'
param(
    [Parameter(Mandatory = $true)][string]$BusPath,
    [Parameter(Mandatory = $true)][string]$EnvPath,
    [Parameter(Mandatory = $true)][string]$OutPath,
    [Parameter(Mandatory = $true)][string]$MetadataPath,
    [Parameter(Mandatory = $true)][string]$TracePath,
    [Parameter(Mandatory = $true)][string]$EmptyPath,
    [Parameter(Mandatory = $true)][ValidateSet(
        'success',
        'initial-network-failure',
        'initial-errorvariable-no-response',
        'network-failure',
        'generic-network-failure',
        'second-redirect',
        'insecure-location',
        'empty-location',
        'realistic-5-1-redirect'
    )][string]$Scenario
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$global:OrderReadIwrFallbackCalls = New-Object System.Collections.Generic.List[object]

function Invoke-WebRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Method,
        [AllowNull()]$Body,
        [AllowNull()][string]$ContentType,
        [int]$MaximumRedirection,
        [switch]$UseBasicParsing,
        [int]$TimeoutSec
    )

    $global:OrderReadIwrFallbackCalls.Add([pscustomobject][ordered]@{
        method = $Method
        maximum_redirection = $MaximumRedirection
        uri_class = $(if ($Uri -like 'https://BUS_URL_IWR_CANARY.invalid/*') { 'bus' } else { 'redirect' })
        body_present = ($null -ne $Body)
        error_action = [string]$PSBoundParameters['ErrorAction']
        error_variable = [string]$PSBoundParameters['ErrorVariable']
    })
    if ($Method -ceq 'Post') {
        if ($Scenario -ceq 'initial-network-failure') {
            throw [System.Net.WebException]::new(
                ('RAW_IWR_HOP1_THROW_CANARY ' + $Uri + ' BUS_SECRET_IWR_CANARY'),
                [System.InvalidOperationException]::new(
                    'RAW_IWR_HOP1_THROW_INNER_CANARY https://ONE_SHOT_IWR_CANARY.invalid/hop1-inner'
                )
            )
        }
        if ($Scenario -ceq 'initial-errorvariable-no-response') {
            # Simulate an advanced IWR implementation that reports a response-less
            # failure through -ErrorVariable while returning no response object.
            # bus.ps1's explicit null path rethrows that record into its sanitizer.
            Write-Error -Exception ([System.Net.WebException]::new(
                ('RAW_IWR_HOP1_ERRORVARIABLE_CANARY ' + $Uri + ' BUS_SECRET_IWR_CANARY'),
                [System.InvalidOperationException]::new(
                    'RAW_IWR_HOP1_ERRORVARIABLE_INNER_CANARY https://ONE_SHOT_IWR_CANARY.invalid/hop1-errorvariable-inner'
                )
            )) -Category ConnectionError
            return
        }
        if ($Scenario -ceq 'realistic-5-1-redirect') {
            # WHAT THE REAL CMDLET DOES, which every other scenario here skips.
            # Measured on Windows PowerShell 5.1.26100.9444 against a local
            # HttpListener: -MaximumRedirection 0 on a 302 emits a NON-TERMINATING
            # InvalidOperationException and STILL RETURNS the response. The other
            # scenarios return the 302 silently, so they exercise the success path
            # and never reach hop 1's error handling at all -- which is why a
            # handler that cannot work on 5.1 passed this suite.
            #
            # Write-Error honours the CALLER's -ErrorAction through CmdletBinding.
            # bus.ps1 passes -ErrorAction SilentlyContinue, so this stays
            # non-terminating and the response below is used. Remove that
            # parameter from bus.ps1 and $ErrorActionPreference='Stop' promotes
            # this to terminating, the response is discarded, and the read dies --
            # which is exactly the regression this case exists to catch.
            Write-Error -Exception ([System.InvalidOperationException]::new(
                'The maximum redirection count has been exceeded. To increase the number of redirections allowed, supply a higher value to the -MaximumRedirection parameter.'
            )) -Category InvalidOperation
        }
        return [pscustomobject]@{
            StatusCode = 302
            Headers = @{
                Location = $(if ($Scenario -ceq 'insecure-location') {
                    'http://INSECURE_IWR_LOCATION_CANARY.invalid/one-shot'
                } elseif ($Scenario -ceq 'empty-location') {
                    '   '
                } else {
                    'https://ONE_SHOT_IWR_CANARY.invalid/one-shot'
                })
                'Content-Type' = 'text/html'
            }
            Content = ''
        }
    }
    if ($Method -cne 'Get') { throw 'IWR_TEST_METHOD_INVALID' }
    if ($Scenario -ceq 'network-failure') {
        throw [System.Net.WebException]::new(
            ('RAW_IWR_EXCEPTION_CANARY ' + $Uri + ' BUS_SECRET_IWR_CANARY'),
            [System.InvalidOperationException]::new(
                'RAW_IWR_WEB_INNER_CANARY https://ONE_SHOT_IWR_CANARY.invalid/web-inner'
            )
        )
    }
    if ($Scenario -ceq 'generic-network-failure') {
        throw [System.TimeoutException]::new(
            ('RAW_IWR_GENERIC_EXCEPTION_CANARY ' + $Uri + ' BUS_SECRET_IWR_CANARY'),
            [System.InvalidOperationException]::new(
                'RAW_IWR_GENERIC_INNER_CANARY https://ONE_SHOT_IWR_CANARY.invalid/generic-inner'
            )
        )
    }
    if ($Scenario -ceq 'second-redirect') {
        return [pscustomobject]@{
            StatusCode = 302
            Headers = @{
                Location = 'http://IWR_DOWNGRADE_REDIRECT_CANARY.invalid/exec'
                'Content-Type' = 'text/html'
            }
            Content = '<html>IWR_REDIRECT_BODY_CANARY</html>'
        }
    }
    return [pscustomobject]@{
        StatusCode = 200
        Headers = @{ 'Content-Type' = 'application/json; charset=utf-8' }
        Content = '{"ok":true,"fileId":"test-board-id","title":"test board","rows":[]}'
        RawContentStream = [IO.MemoryStream]::new(
            [Text.Encoding]::UTF8.GetBytes('{"ok":true,"fileId":"test-board-id","title":"test board","rows":[]}')
        )
    }
}

$previousPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
$caught = $null
try {
    [Environment]::SetEnvironmentVariable('PATH', $EmptyPath, 'Process')
    & $BusPath `
        -Action read `
        -Title 'test board' `
        -OutFile $OutPath `
        -EnvFile $EnvPath `
        -ReadMetadataOutFile $MetadataPath
} catch {
    $caught = $_
    throw
} finally {
    $trace = [ordered]@{
        calls = $global:OrderReadIwrFallbackCalls.ToArray()
        exception_type = $(if ($null -eq $caught) { $null } else { $caught.Exception.GetType().FullName })
        exception_message = $(if ($null -eq $caught) { $null } else { [string]$caught.Exception.Message })
        inner_exception_type = $(if ($null -eq $caught -or $null -eq $caught.Exception.InnerException) {
            $null
        } else { $caught.Exception.InnerException.GetType().FullName })
        inner_exception_message = $(if ($null -eq $caught -or $null -eq $caught.Exception.InnerException) {
            $null
        } else { [string]$caught.Exception.InnerException.Message })
    } | ConvertTo-Json -Depth 6 -Compress
    [IO.File]::WriteAllText($TracePath, $trace, [Text.UTF8Encoding]::new($false))
    Remove-Variable -Name OrderReadIwrFallbackCalls -Scope Global -Force -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable('PATH', $previousPath, 'Process')
}
'@
    Write-TestUtf8 -Path $wrapperPath -Text $wrapperSource
    Write-TestUtf8 -Path $envPath -Text @'
BUS_URL=https://BUS_URL_IWR_CANARY.invalid/private
BUS_SECRET=BUS_SECRET_IWR_CANARY
'@

    $successRoot = Join-Path $caseRoot 'success'
    New-Item -ItemType Directory -Path $successRoot -Force | Out-Null
    $successOutPath = Join-Path $successRoot 'response.txt'
    $successMetadataPath = Join-Path $successRoot 'metadata.json'
    $successTracePath = Join-Path $successRoot 'trace.json'
    $successOutput = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File $wrapperPath `
        -BusPath (Join-Path $RepoRoot 'scripts\bus.ps1') `
        -EnvPath $envPath `
        -OutPath $successOutPath `
        -MetadataPath $successMetadataPath `
        -TracePath $successTracePath `
        -EmptyPath $emptyPath `
        -Scenario success 2>&1)
    $successExitCode = $LASTEXITCODE

    function Invoke-IwrReadFailureScenario {
        param(
            [Parameter(Mandatory = $true)][string]$Name,
            [Parameter(Mandatory = $true)][string]$Scenario,
            [Parameter(Mandatory = $true)][string]$Sentinel
        )

        $scenarioRoot = Join-Path $caseRoot $Name
        New-Item -ItemType Directory -Path $scenarioRoot -Force | Out-Null
        $scenarioOutPath = Join-Path $scenarioRoot 'response.txt'
        $scenarioMetadataPath = Join-Path $scenarioRoot 'metadata.json'
        $scenarioTracePath = Join-Path $scenarioRoot 'trace.json'
        Write-TestUtf8 -Path $scenarioOutPath -Text $Sentinel

        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $scenarioOutput = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
                -File $wrapperPath `
                -BusPath (Join-Path $RepoRoot 'scripts\bus.ps1') `
                -EnvPath $envPath `
                -OutPath $scenarioOutPath `
                -MetadataPath $scenarioMetadataPath `
                -TracePath $scenarioTracePath `
                -EmptyPath $emptyPath `
                -Scenario $Scenario 2>&1)
            $scenarioExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }

        $scenarioMetadataText = [IO.File]::ReadAllText($scenarioMetadataPath, [Text.Encoding]::UTF8)
        $scenarioTraceText = [IO.File]::ReadAllText($scenarioTracePath, [Text.Encoding]::UTF8)
        return [pscustomobject][ordered]@{
            exit_code = $scenarioExitCode
            output = @($scenarioOutput)
            metadata_text = $scenarioMetadataText
            metadata_bytes = [byte[]][IO.File]::ReadAllBytes($scenarioMetadataPath)
            metadata = $scenarioMetadataText | ConvertFrom-Json @script:JsonDateArgs
            trace_text = $scenarioTraceText
            trace = $scenarioTraceText | ConvertFrom-Json @script:JsonDateArgs
            out_text = [IO.File]::ReadAllText($scenarioOutPath, [Text.Encoding]::UTF8)
            out_bytes = [byte[]][IO.File]::ReadAllBytes($scenarioOutPath)
            sentinel = $Sentinel
            sentinel_bytes = [byte[]](New-Object Text.UTF8Encoding($false)).GetBytes($Sentinel)
        }
    }

    $initialFailure = Invoke-IwrReadFailureScenario `
        -Name 'initial-network-failure' `
        -Scenario 'initial-network-failure' `
        -Sentinel 'IWR_HOP1_THROW_OUTFILE_SENTINEL'
    $initialErrorVariableFailure = Invoke-IwrReadFailureScenario `
        -Name 'initial-errorvariable-no-response' `
        -Scenario 'initial-errorvariable-no-response' `
        -Sentinel 'IWR_HOP1_ERRORVARIABLE_OUTFILE_SENTINEL'
    $failure = Invoke-IwrReadFailureScenario `
        -Name 'network-failure' `
        -Scenario 'network-failure' `
        -Sentinel 'IWR_HOP2_WEB_OUTFILE_SENTINEL'
    $genericFailure = Invoke-IwrReadFailureScenario `
        -Name 'generic-network-failure' `
        -Scenario 'generic-network-failure' `
        -Sentinel 'IWR_HOP2_GENERIC_OUTFILE_SENTINEL'

    $redirectRoot = Join-Path $caseRoot 'second-redirect'
    New-Item -ItemType Directory -Path $redirectRoot -Force | Out-Null
    $redirectOutPath = Join-Path $redirectRoot 'response.txt'
    $redirectMetadataPath = Join-Path $redirectRoot 'metadata.json'
    $redirectTracePath = Join-Path $redirectRoot 'trace.json'
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $redirectOutput = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File $wrapperPath `
            -BusPath (Join-Path $RepoRoot 'scripts\bus.ps1') `
            -EnvPath $envPath `
            -OutPath $redirectOutPath `
            -MetadataPath $redirectMetadataPath `
            -TracePath $redirectTracePath `
            -EmptyPath $emptyPath `
            -Scenario second-redirect 2>&1)
        $redirectExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $insecureRoot = Join-Path $caseRoot 'insecure-location'
    New-Item -ItemType Directory -Path $insecureRoot -Force | Out-Null
    $insecureOutPath = Join-Path $insecureRoot 'response.txt'
    $insecureMetadataPath = Join-Path $insecureRoot 'metadata.json'
    $insecureTracePath = Join-Path $insecureRoot 'trace.json'
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $insecureOutput = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File $wrapperPath `
            -BusPath (Join-Path $RepoRoot 'scripts\bus.ps1') `
            -EnvPath $envPath `
            -OutPath $insecureOutPath `
            -MetadataPath $insecureMetadataPath `
            -TracePath $insecureTracePath `
            -EmptyPath $emptyPath `
            -Scenario insecure-location 2>&1)
        $insecureExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $emptyLocationRoot = Join-Path $caseRoot 'empty-location'
    New-Item -ItemType Directory -Path $emptyLocationRoot -Force | Out-Null
    $emptyLocationOutPath = Join-Path $emptyLocationRoot 'response.txt'
    $emptyLocationMetadataPath = Join-Path $emptyLocationRoot 'metadata.json'
    $emptyLocationTracePath = Join-Path $emptyLocationRoot 'trace.json'
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $emptyLocationOutput = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File $wrapperPath `
            -BusPath (Join-Path $RepoRoot 'scripts\bus.ps1') `
            -EnvPath $envPath `
            -OutPath $emptyLocationOutPath `
            -MetadataPath $emptyLocationMetadataPath `
            -TracePath $emptyLocationTracePath `
            -EmptyPath $emptyPath `
            -Scenario empty-location 2>&1)
        $emptyLocationExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $realisticRoot = Join-Path $caseRoot 'realistic-5-1-redirect'
    New-Item -ItemType Directory -Path $realisticRoot -Force | Out-Null
    $realisticOutPath = Join-Path $realisticRoot 'response.txt'
    $realisticMetadataPath = Join-Path $realisticRoot 'metadata.json'
    $realisticTracePath = Join-Path $realisticRoot 'trace.json'
    # $ErrorActionPreference='Continue' around the call, as every other
    # failure-capable scenario here does. Without it a regression makes the
    # child's stderr a terminating NativeCommandError and the whole SUITE
    # aborts, so the guard reads as a crash instead of a named FAIL.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $realisticOutput = @(& $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File $wrapperPath `
            -BusPath (Join-Path $RepoRoot 'scripts\bus.ps1') `
            -EnvPath $envPath `
            -OutPath $realisticOutPath `
            -MetadataPath $realisticMetadataPath `
            -TracePath $realisticTracePath `
            -EmptyPath $emptyPath `
            -Scenario realistic-5-1-redirect 2>&1)
        $realisticExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $successMetadataText = [IO.File]::ReadAllText($successMetadataPath, [Text.Encoding]::UTF8)
    $redirectMetadataText = [IO.File]::ReadAllText($redirectMetadataPath, [Text.Encoding]::UTF8)
    $insecureMetadataText = [IO.File]::ReadAllText($insecureMetadataPath, [Text.Encoding]::UTF8)
    $emptyLocationMetadataText = [IO.File]::ReadAllText($emptyLocationMetadataPath, [Text.Encoding]::UTF8)
    return [pscustomobject][ordered]@{
        success_exit_code = $successExitCode
        success_output = @($successOutput)
        success_body = [IO.File]::ReadAllText($successOutPath, [Text.Encoding]::UTF8)
        success_metadata = $successMetadataText | ConvertFrom-Json @script:JsonDateArgs
        success_trace = [IO.File]::ReadAllText($successTracePath, [Text.Encoding]::UTF8) | ConvertFrom-Json @script:JsonDateArgs
        initial_failure = $initialFailure
        initial_errorvariable_failure = $initialErrorVariableFailure
        failure = $failure
        failure_exit_code = $failure.exit_code
        failure_output = @($failure.output)
        failure_metadata_text = $failure.metadata_text
        failure_metadata = $failure.metadata
        failure_trace_text = $failure.trace_text
        failure_trace = $failure.trace
        failure_out_text = $failure.out_text
        failure_sentinel = $failure.sentinel
        generic_failure = $genericFailure
        redirect_exit_code = $redirectExitCode
        redirect_output = @($redirectOutput)
        redirect_metadata_text = $redirectMetadataText
        redirect_metadata = $redirectMetadataText | ConvertFrom-Json @script:JsonDateArgs
        redirect_trace = [IO.File]::ReadAllText($redirectTracePath, [Text.Encoding]::UTF8) | ConvertFrom-Json @script:JsonDateArgs
        insecure_exit_code = $insecureExitCode
        insecure_output = @($insecureOutput)
        insecure_metadata_text = $insecureMetadataText
        insecure_metadata = $insecureMetadataText | ConvertFrom-Json @script:JsonDateArgs
        insecure_trace = [IO.File]::ReadAllText($insecureTracePath, [Text.Encoding]::UTF8) | ConvertFrom-Json @script:JsonDateArgs
        empty_location_exit_code = $emptyLocationExitCode
        empty_location_output = @($emptyLocationOutput)
        empty_location_metadata_text = $emptyLocationMetadataText
        empty_location_metadata = $emptyLocationMetadataText | ConvertFrom-Json @script:JsonDateArgs
        empty_location_trace = [IO.File]::ReadAllText($emptyLocationTracePath, [Text.Encoding]::UTF8) | ConvertFrom-Json @script:JsonDateArgs
        realistic_exit_code = $realisticExitCode
        realistic_output = @($realisticOutput)
        realistic_body = $(if (Test-Path -LiteralPath $realisticOutPath) { [IO.File]::ReadAllText($realisticOutPath, [Text.Encoding]::UTF8) } else { '' })
        realistic_metadata = $(if (Test-Path -LiteralPath $realisticMetadataPath) { [IO.File]::ReadAllText($realisticMetadataPath, [Text.Encoding]::UTF8) | ConvertFrom-Json @script:JsonDateArgs } else { $null })
        realistic_trace = $(if (Test-Path -LiteralPath $realisticTracePath) { [IO.File]::ReadAllText($realisticTracePath, [Text.Encoding]::UTF8) | ConvertFrom-Json @script:JsonDateArgs } else { $null })
    }
}

function Invoke-IwrSecurityAssertions {
    param([switch]$IncludeRenderRegression)

    $iwrRun = Invoke-ActualBusIwrFallbackCase
    Assert-True 'IWR fallback keeps hop 1 POST no-follow and hop 2 GET no-follow' (
        $iwrRun.success_exit_code -eq 0 -and
        @($iwrRun.success_trace.calls).Count -eq 2 -and
        $iwrRun.success_trace.calls[0].method -ceq 'Post' -and
        [int]$iwrRun.success_trace.calls[0].maximum_redirection -eq 0 -and
        [bool]$iwrRun.success_trace.calls[0].body_present -and
        $iwrRun.success_trace.calls[1].method -ceq 'Get' -and
        [int]$iwrRun.success_trace.calls[1].maximum_redirection -eq 0 -and
        -not [bool]$iwrRun.success_trace.calls[1].body_present
    )
    Assert-True 'IWR fallback retains the final successful body and metadata' (
        $iwrRun.success_body -ceq '{"ok":true,"fileId":"test-board-id","title":"test board","rows":[]}' -and
        $null -ne $iwrRun.success_metadata.PSObject.Properties['http_status'] -and
        $iwrRun.success_metadata.http_status -isnot [string] -and
        [int]$iwrRun.success_metadata.http_status -eq 200 -and
        $iwrRun.success_metadata.content_type_class -ceq 'json' -and
        $null -eq $iwrRun.success_trace.exception_type
    )
    # The 302 exactly as Windows PowerShell 5.1 really delivers it: a
    # non-terminating InvalidOperationException alongside the response. Every
    # other IWR scenario above returns the 302 silently, so none of them reach
    # hop 1's error handling; this is the only case that does.
    Assert-True 'realistic 5.1 non-terminating 302 still completes the read' (
        $iwrRun.realistic_exit_code -eq 0 -and
        @($iwrRun.realistic_trace.calls).Count -eq 2 -and
        $iwrRun.realistic_trace.calls[0].method -ceq 'Post' -and
        [int]$iwrRun.realistic_trace.calls[0].maximum_redirection -eq 0 -and
        $iwrRun.realistic_trace.calls[1].method -ceq 'Get' -and
        [int]$iwrRun.realistic_trace.calls[1].maximum_redirection -eq 0 -and
        $null -eq $iwrRun.realistic_trace.exception_type
    )
    Assert-True 'realistic 5.1 302 reaches hop 2 and keeps its body and metadata' (
        $iwrRun.realistic_body.Contains('"ok":true') -and
        [int]$iwrRun.realistic_metadata.http_status -eq 200 -and
        $iwrRun.realistic_metadata.content_type_class -ceq 'json'
    )
    Assert-True 'realistic 5.1 302 leaks no secret or one-shot URL' (
        -not (ConvertTo-SquashedText -Lines $iwrRun.realistic_output).Contains('BUS_SECRET_IWR_CANARY') -and
        -not (ConvertTo-SquashedText -Lines $iwrRun.realistic_output).Contains('ONE_SHOT_IWR_CANARY')
    )

    Assert-True 'IWR hop 1 terminating response-less failure exposes only the fixed inner-free network surface' (
        Test-SanitizedIwrNetworkFailure -Run $iwrRun.initial_failure -ExpectedCallCount 1
    )
    Assert-True 'IWR hop 1 null plus ErrorVariable response-less failure exposes only the fixed inner-free network surface' (
        Test-SanitizedIwrNetworkFailure -Run $iwrRun.initial_errorvariable_failure -ExpectedCallCount 1
    )
    Assert-True 'IWR hop 1 null plus ErrorVariable fixture reaches the explicit captured-error ingress' (
        $iwrRun.initial_errorvariable_failure.trace.calls[0].method -ceq 'Post' -and
        $iwrRun.initial_errorvariable_failure.trace.calls[0].error_action -ceq 'SilentlyContinue' -and
        $iwrRun.initial_errorvariable_failure.trace.calls[0].error_variable -ceq 'iwrError'
    )

    Assert-True 'IWR hop 2 WebException clears stale hop 1 metadata and retains its network type' (
        $iwrRun.failure_exit_code -ne 0 -and
        @($iwrRun.failure_trace.calls).Count -eq 2 -and
        @($iwrRun.failure_metadata.PSObject.Properties).Count -eq 0 -and
        $iwrRun.failure_metadata_text -ceq '{}' -and
        $iwrRun.failure_trace.exception_type -ceq 'System.Net.WebException' -and
        $null -eq $iwrRun.failure_trace.inner_exception_type -and
        $null -eq $iwrRun.failure_trace.inner_exception_message
    )
    Assert-True 'IWR hop 2 WebException exposes only the fixed inner-free network surface' (
        Test-SanitizedIwrNetworkFailure -Run $iwrRun.failure -ExpectedCallCount 2
    )
    Assert-True 'IWR hop 2 generic response-less failure exposes only the fixed inner-free network surface' (
        Test-SanitizedIwrNetworkFailure -Run $iwrRun.generic_failure -ExpectedCallCount 2
    )

    $iwrRedirectOutputText = @($iwrRun.redirect_output | ForEach-Object { [string]$_ }) -join "`n"
    Assert-True 'IWR fallback refuses a second redirect without accepting its body' (
        $iwrRun.redirect_exit_code -ne 0 -and
        @($iwrRun.redirect_trace.calls).Count -eq 2 -and
        $iwrRun.redirect_trace.calls[1].method -ceq 'Get' -and
        [int]$iwrRun.redirect_trace.calls[1].maximum_redirection -eq 0 -and
        $null -ne $iwrRun.redirect_metadata.PSObject.Properties['http_status'] -and
        [int]$iwrRun.redirect_metadata.http_status -eq 302 -and
        $iwrRun.redirect_metadata.content_type_class -ceq 'html' -and
        $iwrRun.redirect_trace.exception_type -ceq 'System.InvalidOperationException'
    )
    Assert-True 'IWR second-redirect failure exposes only a fixed safe error' (
        $iwrRun.redirect_trace.exception_message -ceq 'BUS_READ_RESPONSE_INVALID: read response failed the generic success, identity, or payload contract.' -and
        -not $iwrRedirectOutputText.Contains('IWR_DOWNGRADE_REDIRECT_CANARY') -and
        -not $iwrRedirectOutputText.Contains('IWR_REDIRECT_BODY_CANARY') -and
        -not $iwrRedirectOutputText.Contains('BUS_SECRET_IWR_CANARY') -and
        -not $iwrRun.redirect_metadata_text.Contains('IWR_DOWNGRADE_REDIRECT_CANARY') -and
        -not $iwrRun.redirect_metadata_text.Contains('IWR_REDIRECT_BODY_CANARY')
    )
    $iwrInsecureOutputText = @($iwrRun.insecure_output | ForEach-Object { [string]$_ }) -join "`n"
    Assert-True 'IWR fallback rejects an insecure initial hop 2 Location before transfer' (
        $iwrRun.insecure_exit_code -ne 0 -and
        @($iwrRun.insecure_trace.calls).Count -eq 1 -and
        $iwrRun.insecure_trace.calls[0].method -ceq 'Post' -and
        $null -ne $iwrRun.insecure_metadata.PSObject.Properties['http_status'] -and
        [int]$iwrRun.insecure_metadata.http_status -eq 302 -and
        $iwrRun.insecure_trace.exception_type -ceq 'System.InvalidOperationException'
    )
    Assert-True 'IWR insecure-Location failure exposes only a fixed safe error' (
        $iwrRun.insecure_trace.exception_message -ceq 'hop 2 Location must resolve to HTTPS. Refusing transfer; the write, if any, may still have landed: READ BACK before deciding anything.' -and
        -not $iwrInsecureOutputText.Contains('INSECURE_IWR_LOCATION_CANARY') -and
        -not $iwrInsecureOutputText.Contains('BUS_SECRET_IWR_CANARY') -and
        -not $iwrRun.insecure_metadata_text.Contains('INSECURE_IWR_LOCATION_CANARY')
    )
    $iwrEmptyLocationOutputText = @($iwrRun.empty_location_output | ForEach-Object { [string]$_ }) -join "`n"
    Assert-True 'IWR fallback rejects an empty hop 2 Location before transfer' (
        $iwrRun.empty_location_exit_code -ne 0 -and
        @($iwrRun.empty_location_trace.calls).Count -eq 1 -and
        $iwrRun.empty_location_trace.calls[0].method -ceq 'Post' -and
        $null -ne $iwrRun.empty_location_metadata.PSObject.Properties['http_status'] -and
        [int]$iwrRun.empty_location_metadata.http_status -eq 302 -and
        $iwrRun.empty_location_trace.exception_type -ceq 'System.InvalidOperationException'
    )
    Assert-True 'IWR empty-Location failure exposes only a fixed safe error' (
        $iwrRun.empty_location_trace.exception_message -ceq 'hop 2 Location or BUS_URL is empty. Refusing transfer; the write, if any, may still have landed: READ BACK before deciding anything.' -and
        -not $iwrEmptyLocationOutputText.Contains('BUS_URL_IWR_CANARY') -and
        -not $iwrEmptyLocationOutputText.Contains('BUS_SECRET_IWR_CANARY')
    )
    $actualClosedPort = Invoke-ActualBusClosedPortSupervisorCase
    $actualClosedPortRetries = @($actualClosedPort.events |
        Where-Object event -ceq 'board_read_retry')
    Assert-True 'genuine forced-IWR closed-port failure remains retry-classifiable end to end' (
        $actualClosedPort.exit_code -eq 20 -and
        $actualClosedPort.result -and
        $actualClosedPort.result.error_code -ceq 'BOARD_READ_TRANSPORT_ERROR' -and
        $actualClosedPortRetries.Count -eq 1 -and
        $actualClosedPortRetries[0].code -ceq 'BOARD_READ_TRANSPORT_ERROR' -and
        $actualClosedPortRetries[0].details.attempt -ceq '1' -and
        $actualClosedPortRetries[0].details.code -ceq 'BOARD_READ_TRANSPORT_ERROR'
    )
    $actualClosedPortOutputText = ConvertTo-SquashedText -Lines $actualClosedPort.output
    Assert-True 'genuine forced-IWR retry failure preserves state and the no-side-effect boundary' (
        $actualClosedPort.state.cursor.timestamp -ceq '2026-09-07T08:00:00.0000000Z' -and
        $actualClosedPort.state.cursor.row_id -ceq 'cursor-before-closed-port' -and
        @($actualClosedPort.state.work).Count -eq 0 -and
        -not $actualClosedPort.claude_called -and
        -not $actualClosedPortOutputText.Contains($actualClosedPort.raw_exception_canary) -and
        -not $actualClosedPort.log_text.Contains($actualClosedPort.raw_exception_canary) -and
        -not $actualClosedPortOutputText.Contains($actualClosedPort.one_shot_canary) -and
        -not $actualClosedPort.log_text.Contains($actualClosedPort.one_shot_canary) -and
        -not $actualClosedPortOutputText.Contains($actualClosedPort.secret_canary) -and
        -not $actualClosedPort.log_text.Contains($actualClosedPort.secret_canary) -and
        -not $actualClosedPortOutputText.Contains($actualClosedPort.bus_url) -and
        -not $actualClosedPort.log_text.Contains($actualClosedPort.bus_url)
    )

    if ($IncludeRenderRegression) {
        $escape = [char]27
        $renderProbe = [pscustomobject][ordered]@{
            exit_code = $iwrRun.initial_failure.exit_code
            output = @(
                ($escape + '[31;1mBUS_READ_NETWORK_ERROR: read' + $escape + '[0m'),
                ($escape + '[31;1m  12 | transport failed before an HTTP response was' + $escape + '[0m'),
                ($escape + '[31;1m     | received.' + $escape + '[0m')
            )
            trace = $iwrRun.initial_failure.trace
            metadata_text = $iwrRun.initial_failure.metadata_text
            metadata_bytes = $iwrRun.initial_failure.metadata_bytes
            metadata = $iwrRun.initial_failure.metadata
            out_text = $iwrRun.initial_failure.out_text
            out_bytes = $iwrRun.initial_failure.out_bytes
            sentinel = $iwrRun.initial_failure.sentinel
            sentinel_bytes = $iwrRun.initial_failure.sentinel_bytes
            trace_text = $iwrRun.initial_failure.trace_text
        }
        Assert-True 'ANSI and Core error gutters preserve the fixed-message positive guard' (
            Test-SanitizedIwrNetworkFailure -Run $renderProbe -ExpectedCallCount 1
        )

        $rawCanaryProbe = $renderProbe | Select-Object *
        $rawCanaryProbe.output = @($renderProbe.output) + @('RAW_IWR_', 'CANARY')
        Assert-True 'render normalization cannot hide a split raw-exception canary' (
            -not (Test-SanitizedIwrNetworkFailure -Run $rawCanaryProbe -ExpectedCallCount 1)
        )

        $busUrlCanaryProbe = $renderProbe | Select-Object *
        $busUrlCanaryProbe.output = @($renderProbe.output) + 'https://BUS_URL_IWR_CANARY.invalid/private'
        Assert-True 'render normalization cannot hide a BUS URL canary' (
            -not (Test-SanitizedIwrNetworkFailure -Run $busUrlCanaryProbe -ExpectedCallCount 1)
        )

        $secretCanaryProbe = $renderProbe | Select-Object *
        $secretCanaryProbe.trace_text = ([string]$renderProbe.trace_text) + ' BUS_SECRET_IWR_CANARY'
        Assert-True 'render normalization cannot hide a BUS secret canary' (
            -not (Test-SanitizedIwrNetworkFailure -Run $secretCanaryProbe -ExpectedCallCount 1)
        )

        $oneShotCanaryProbe = $renderProbe | Select-Object *
        $oneShotCanaryProbe.trace_text = ([string]$renderProbe.trace_text) + ' https://ONE_SHOT_IWR_CANARY.invalid/private'
        Assert-True 'render normalization cannot hide a one-shot URL canary' (
            -not (Test-SanitizedIwrNetworkFailure -Run $oneShotCanaryProbe -ExpectedCallCount 1)
        )
    }
}

function Invoke-DateReaderNegativeControl {
    param([Parameter(Mandatory = $true)][ValidateSet('Full', 'IwrOnly')][string]$CurrentScope)

    if ($SimulateJsonDateCoercion) { return }

    # Re-run only the prerequisite under this same engine. The child exits at
    # the reader gate, and this explicit guard prevents recursive self-tests.
    $currentShell = (Get-Process -Id $PID -ErrorAction Stop).Path
    $dateReaderFailureOutput = @(& $currentShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File $PSCommandPath -Scope $CurrentScope -SimulateJsonDateCoercion 2>&1)
    $dateReaderFailureExitCode = $LASTEXITCODE
    $dateReaderFailureLines = @($dateReaderFailureOutput | ForEach-Object { [string]$_ })
    $dateReaderFailurePassLines = @($dateReaderFailureLines | Where-Object { $_ -like 'PASS *' })
    $dateReaderFailureFailLines = @($dateReaderFailureLines | Where-Object { $_ -like 'FAIL *' })
    $dateReaderFailureResultLines = @($dateReaderFailureLines | Where-Object { $_ -like 'RESULT *' })
    Assert-True 'date-reader negative control stops at the harness boundary' (
        $dateReaderFailureExitCode -eq 1 -and
        $dateReaderFailurePassLines.Count -eq 0 -and
        $dateReaderFailureFailLines.Count -eq 1 -and
        $dateReaderFailureFailLines[0] -ceq
            'FAIL harness reads ISO-8601 board cells as strings, on this edition' -and
        $dateReaderFailureResultLines.Count -eq 1 -and
        $dateReaderFailureResultLines[0] -ceq 'RESULT passed=0 failed=1'
    ) ('exit=' + $dateReaderFailureExitCode +
       ' pass_lines=' + $dateReaderFailurePassLines.Count +
       ' fail_lines=' + $dateReaderFailureFailLines.Count +
       ' result_lines=' + $dateReaderFailureResultLines.Count)
}

$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-read-resilience-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
try {
    # The harness checks its own reader before it checks anything else. Four
    # assertions below compare a cursor to an exact timestamp string, and if this
    # reader ever goes back to coercing, those four fail in a way that reads like a
    # product bug. This one fails in a way that reads like what it is.
    $script:DateProbe = if ($SimulateJsonDateCoercion) {
        # 5.1 does not naturally coerce this JSON, so inject the bad type directly
        # to keep the failure oracle deterministic on every supported engine.
        [pscustomobject]@{ t = [datetime]'2026-09-07T08:00:00Z' }
    } else {
        '{"t":"2026-09-07T08:00:00.0000000Z"}' | ConvertFrom-Json @script:JsonDateArgs
    }
    $dateReaderSafe = (
        $script:DateProbe.t -is [string] -and
        $script:DateProbe.t -ceq '2026-09-07T08:00:00.0000000Z'
    )
    Assert-True 'harness reads ISO-8601 board cells as strings, on this edition' $dateReaderSafe
    if (-not $dateReaderSafe) {
        Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
        exit 1
    }

    if ($Scope -ceq 'IwrOnly') {
        $isLinuxVariable = Get-Variable -Name IsLinux -ErrorAction SilentlyContinue
        if (-not $isLinuxVariable -or -not [bool]$isLinuxVariable.Value -or
            $PSVersionTable.PSEdition -cne 'Core' -or
            $PSVersionTable.PSVersion -lt [version]'7.5' -or
            -not [IO.Path]::IsPathRooted($script:ResolvedChildShell) -or
            [IO.Path]::GetFileName($script:ResolvedChildShell) -cne 'pwsh') {
            throw ('IwrOnly requires Linux PowerShell Core 7.5+ and an absolute pwsh child; host=' +
                   $PSVersionTable.PSEdition + ' ' + $PSVersionTable.PSVersion +
                   '; child=' + $script:ResolvedChildShell)
        }

        $busSourcePath = Join-Path $RepoRoot 'scripts/bus.ps1'
        $busSourceSha256 = (Get-FileHash -LiteralPath $busSourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
        $suiteSourceSha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Output 'SCOPE IwrOnly'
        Write-Output ('ENGINE Core ' + $PSVersionTable.PSVersion + ' child=' + $script:ResolvedChildShell)
        Write-Output ('SOURCE bus.ps1 sha256=' + $busSourceSha256)
        Write-Output ('SOURCE test_order_read_resilience.ps1 sha256=' + $suiteSourceSha256)

        Invoke-IwrSecurityAssertions -IncludeRenderRegression
        Invoke-DateReaderNegativeControl -CurrentScope $Scope

        Write-Output ('CASES ' + ($script:Passed + $script:Failed))
        Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
        if ($script:Failed -gt 0) { exit 1 }
        exit 0
    }

    $validEmpty = New-BoardJson
    $eligible = New-BoardJson -DataRows (, (New-EligibleOrderRow))

    $actualBusSidecar = Invoke-ActualBusMetadataCase
    Assert-True 'actual bus captures native curl exit and HTTP status as numbers' (
        $null -ne $actualBusSidecar.metadata.PSObject.Properties['transport_exit'] -and
        $actualBusSidecar.metadata.transport_exit -isnot [string] -and
        [int]$actualBusSidecar.metadata.transport_exit -eq 7 -and
        $null -ne $actualBusSidecar.metadata.PSObject.Properties['http_status'] -and
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

    $actualBusSecondRedirect = Invoke-ActualBusSecondRedirectCase
    $hop2ProtoPair = $false
    $hop2ProtoRedirectPair = $false
    for ($argumentIndex = 0; $argumentIndex -lt ($actualBusSecondRedirect.second_call.Count - 1); $argumentIndex++) {
        if ($actualBusSecondRedirect.second_call[$argumentIndex] -ceq '--proto' -and
            $actualBusSecondRedirect.second_call[$argumentIndex + 1] -ceq '=https') {
            $hop2ProtoPair = $true
        }
        if ($actualBusSecondRedirect.second_call[$argumentIndex] -ceq '--proto-redir' -and
            $actualBusSecondRedirect.second_call[$argumentIndex + 1] -ceq '=https') {
            $hop2ProtoRedirectPair = $true
        }
    }
    Assert-True 'actual bus follows a bounded redirect only on the bodyless hop 2 GET' (
        $actualBusSecondRedirect.exit_code -eq 0 -and
        $actualBusSecondRedirect.call_count -eq 2 -and
        @($actualBusSecondRedirect.first_call | Where-Object { $_ -ceq '-X' }).Count -eq 1 -and
        @($actualBusSecondRedirect.first_call | Where-Object { $_ -ceq 'POST' }).Count -eq 1 -and
        @($actualBusSecondRedirect.first_call | Where-Object { $_ -ceq '-L' }).Count -eq 0 -and
        @($actualBusSecondRedirect.second_call | Where-Object { $_ -ceq '-X' -or $_ -ceq 'POST' }).Count -eq 0 -and
        @($actualBusSecondRedirect.second_call | Where-Object { $_ -ceq '-L' }).Count -eq 1 -and
        @($actualBusSecondRedirect.second_call | Where-Object { $_ -ceq '--max-redirs' }).Count -eq 1 -and
        @($actualBusSecondRedirect.second_call | Where-Object { $_ -ceq '5' }).Count -eq 1 -and
        @($actualBusSecondRedirect.second_call | Where-Object { $_ -ceq '--proto' }).Count -eq 1 -and
        @($actualBusSecondRedirect.second_call | Where-Object { $_ -ceq '--proto-redir' }).Count -eq 1 -and
        @($actualBusSecondRedirect.second_call | Where-Object { $_ -ceq '=https' }).Count -eq 2 -and
        $hop2ProtoPair -and
        $hop2ProtoRedirectPair
    )
    Assert-True 'actual bus retains only the final hop 2 response body and metadata' (
        $actualBusSecondRedirect.body -ceq $actualBusSecondRedirect.expected_body -and
        $null -ne $actualBusSecondRedirect.metadata.PSObject.Properties['transport_exit'] -and
        $actualBusSecondRedirect.metadata.transport_exit -isnot [string] -and
        [int]$actualBusSecondRedirect.metadata.transport_exit -eq 0 -and
        $null -ne $actualBusSecondRedirect.metadata.PSObject.Properties['http_status'] -and
        $actualBusSecondRedirect.metadata.http_status -isnot [string] -and
        [int]$actualBusSecondRedirect.metadata.http_status -eq 200 -and
        $actualBusSecondRedirect.metadata.content_type_class -ceq 'json'
    )
    Assert-True 'successful second redirect emits one bounded safe status line' (
        @($actualBusSecondRedirect.output).Count -eq 1 -and
        ([string]$actualBusSecondRedirect.output[0]).StartsWith('saved ') -and
        -not ([string]$actualBusSecondRedirect.output[0]).Contains('ONE_SHOT_REDIRECT_CANARY') -and
        -not ([string]$actualBusSecondRedirect.output[0]).Contains('CANONICAL_REDIRECT_CANARY') -and
        -not ([string]$actualBusSecondRedirect.output[0]).Contains('BUS_SECRET_REDIRECT_CANARY')
    )
    $secondRedirectFailureText = ConvertTo-FlowedText -Lines $actualBusSecondRedirect.failure_output
    $secondRedirectFailureSquashed = ConvertTo-SquashedText -Lines $actualBusSecondRedirect.failure_output
    Assert-True 'failed final hop 2 preserves sanitized metadata before returning nonzero' (
        $actualBusSecondRedirect.failure_exit_code -ne 0 -and
        $actualBusSecondRedirect.failure_call_count -eq 2 -and
        $null -ne $actualBusSecondRedirect.failure_metadata.PSObject.Properties['transport_exit'] -and
        $actualBusSecondRedirect.failure_metadata.transport_exit -isnot [string] -and
        [int]$actualBusSecondRedirect.failure_metadata.transport_exit -eq 0 -and
        $null -ne $actualBusSecondRedirect.failure_metadata.PSObject.Properties['http_status'] -and
        $actualBusSecondRedirect.failure_metadata.http_status -isnot [string] -and
        [int]$actualBusSecondRedirect.failure_metadata.http_status -eq 503 -and
        $actualBusSecondRedirect.failure_metadata.content_type_class -ceq 'html'
    )
    Assert-True 'failed final hop 2 exposes only the fixed bounded error' (
        $secondRedirectFailureText.Contains('BUS_READ_RESPONSE_INVALID: read response failed the generic success, identity, or payload contract.') -and
        -not $secondRedirectFailureSquashed.Contains('ONE_SHOT_REDIRECT_CANARY') -and
        -not $secondRedirectFailureSquashed.Contains('CANONICAL_REDIRECT_CANARY') -and
        -not $secondRedirectFailureSquashed.Contains('FINAL_REDIRECT_BODY_CANARY') -and
        -not $secondRedirectFailureSquashed.Contains('BUS_SECRET_REDIRECT_CANARY') -and
        -not $actualBusSecondRedirect.failure_metadata_text.Contains('FINAL_REDIRECT_BODY_CANARY')
    )

    Invoke-IwrSecurityAssertions
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

    $notFoundThenValidCanary = '<html>NOT_FOUND_THEN_VALID_BODY_CANARY</html>'
    $notFoundThenValid = Invoke-ReadCase `
        -Name 'http-404-then-valid' `
        -Responses @($notFoundThenValidCanary, $validEmpty) `
        -FailureByAttempt @{ 1 = [ordered]@{
            transport_exit = 0
            http_status = 404
            content_type_class = 'html'
        } }
    $notFoundRetry = @($notFoundThenValid.events | Where-Object event -ceq 'board_read_retry')
    $notFoundRetryDetailNames = @($notFoundRetry[0].details.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $expectedNotFoundRetryDetailNames = @(
        'attempt', 'code', 'content_length', 'content_sha256', 'content_type_class',
        'elapsed_ms', 'http_status', 'transport_exit'
    )
    Assert-True 'HTTP 404 retries one whole read then completes normally with no eligible order' (
        $notFoundThenValid.exit_code -eq 0 -and
        $notFoundThenValid.result -and
        $notFoundThenValid.result.status -ceq 'no_eligible_order' -and
        $notFoundThenValid.state.last_poll.status -ceq 'no_eligible_order' -and
        $notFoundThenValid.read_count -eq 2 -and
        @($notFoundThenValid.actions).Count -eq 2 -and
        @($notFoundThenValid.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        -not $notFoundThenValid.claude_called
    )
    Assert-True 'HTTP 404 recovery emits one safe retry warning' (
        $notFoundRetry.Count -eq 1 -and
        $notFoundRetry[0].level -ceq 'warning' -and
        $notFoundRetry[0].code -ceq 'BOARD_READ_HTTP_ERROR' -and
        $notFoundRetry[0].work_id -ceq '' -and
        $notFoundRetry[0].row_id -ceq '' -and
        $notFoundRetry[0].details.attempt -ceq '1' -and
        $notFoundRetry[0].details.code -ceq 'BOARD_READ_HTTP_ERROR' -and
        $notFoundRetry[0].details.transport_exit -ceq '0' -and
        $notFoundRetry[0].details.http_status -ceq '404' -and
        $notFoundRetry[0].details.content_type_class -ceq 'html' -and
        [int]$notFoundRetry[0].details.content_length -gt 0 -and
        [string]$notFoundRetry[0].details.content_sha256 -cmatch '^[0-9a-f]{64}$' -and
        -not [string]::IsNullOrWhiteSpace([string]$notFoundRetry[0].details.elapsed_ms) -and
        $notFoundRetryDetailNames.Count -eq $expectedNotFoundRetryDetailNames.Count -and
        @($expectedNotFoundRetryDetailNames | Where-Object { $notFoundRetryDetailNames -cnotcontains $_ }).Count -eq 0 -and
        -not $notFoundThenValid.log_text.Contains('NOT_FOUND_THEN_VALID_BODY_CANARY')
    )

    $preservedWork = [pscustomobject][ordered]@{
        input_row_id = 'preserved-input-row'
        work_id = 'PRESERVED-WORK'
        status = 'claim_confirmed'
        result_status = ''
        output_sha256 = ''
        updated_at = '2026-09-07T08:01:00.0000000Z'
    }
    $preservedWorkJson = $preservedWork | ConvertTo-Json -Compress
    $notFoundTwiceCanaryOne = '<html>NOT_FOUND_TWICE_FIRST_BODY_CANARY</html>'
    $notFoundTwiceCanaryTwo = '<html>NOT_FOUND_TWICE_SECOND_BODY_CANARY</html>'
    $notFoundTwice = Invoke-ReadCase `
        -Name 'http-404-twice' `
        -Responses @($notFoundTwiceCanaryOne, $notFoundTwiceCanaryTwo) `
        -FailureByAttempt @{
            1 = [ordered]@{ transport_exit = 0; http_status = 404; content_type_class = 'html' }
            2 = [ordered]@{ transport_exit = 0; http_status = 404; content_type_class = 'html' }
        } `
        -InitialWork (, $preservedWork)
    $notFoundTwiceRetries = @($notFoundTwice.events | Where-Object event -ceq 'board_read_retry')
    $notFoundTwiceRunErrors = @($notFoundTwice.events | Where-Object event -ceq 'run_error')
    Assert-True 'two HTTP 404 responses stop after the existing one-retry bound' (
        $notFoundTwice.exit_code -eq 20 -and
        $notFoundTwice.result -and
        $notFoundTwice.result.status -ceq 'error' -and
        $notFoundTwice.result.error_code -ceq 'BOARD_READ_HTTP_ERROR' -and
        $notFoundTwice.state.error.code -ceq 'BOARD_READ_HTTP_ERROR' -and
        $notFoundTwice.read_count -eq 2 -and
        $notFoundTwiceRetries.Count -eq 1 -and
        $notFoundTwiceRunErrors.Count -eq 1 -and
        $notFoundTwiceRunErrors[0].code -ceq 'BOARD_READ_HTTP_ERROR' -and
        $notFoundTwiceRunErrors[0].details.attempt -ceq '2' -and
        $notFoundTwiceRunErrors[0].details.http_status -ceq '404'
    )
    Assert-True 'two HTTP 404 responses preserve cursor and work with no write or inference' (
        [bool]$notFoundTwice.state.initialized -and
        $notFoundTwice.state.cursor.timestamp -ceq '2026-09-07T08:00:00.0000000Z' -and
        $notFoundTwice.state.cursor.row_id -ceq 'cursor-before-read' -and
        @($notFoundTwice.state.work).Count -eq 1 -and
        (@($notFoundTwice.state.work)[0] | ConvertTo-Json -Compress) -ceq $preservedWorkJson -and
        @($notFoundTwice.actions).Count -eq 2 -and
        @($notFoundTwice.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        -not $notFoundTwice.claude_called -and
        -not $notFoundTwice.log_text.Contains('NOT_FOUND_TWICE_FIRST_BODY_CANARY') -and
        -not $notFoundTwice.log_text.Contains('NOT_FOUND_TWICE_SECOND_BODY_CANARY')
    )

    $missingRowsJson = '{"ok":true}'
    $missingRowsThenEmpty = Invoke-ReadCase `
        -Name 'missing-rows-then-empty' `
        -Responses @($missingRowsJson, $validEmpty)
    $missingRowsEmptyRetries = @($missingRowsThenEmpty.events | Where-Object event -ceq 'board_read_retry')
    $missingRowsEmptyPropertyNames = @($missingRowsEmptyRetries[0].PSObject.Properties | ForEach-Object { [string]$_.Name })
    $missingRowsEmptyDetailNames = @($missingRowsEmptyRetries[0].details.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $expectedRetryPropertyNames = @(
        'at', 'code', 'details', 'event', 'level', 'message', 'row_id', 'run_id', 'work_id'
    )
    $expectedMissingRowsDetailNames = @(
        'attempt', 'code', 'content_type_class', 'elapsed_ms', 'http_status', 'transport_exit'
    )
    Assert-True 'missing rows retries one whole read then completes normally with no eligible order' (
        $missingRowsThenEmpty.exit_code -eq 0 -and
        $missingRowsThenEmpty.result -and
        $missingRowsThenEmpty.result.status -ceq 'no_eligible_order' -and
        $missingRowsThenEmpty.state.last_poll.status -ceq 'no_eligible_order' -and
        $missingRowsThenEmpty.read_count -eq 2 -and
        @($missingRowsThenEmpty.actions).Count -eq 2
    )
    Assert-True 'missing rows recovery emits one exact lowercase safe retry warning' (
        $missingRowsEmptyRetries.Count -eq 1 -and
        $missingRowsEmptyRetries[0].level -ceq 'warning' -and
        $missingRowsEmptyRetries[0].code -ceq 'board_rows_missing' -and
        $missingRowsEmptyRetries[0].message -ceq 'A transient pre-admission board read failed; retrying once.' -and
        $missingRowsEmptyRetries[0].work_id -ceq '' -and
        $missingRowsEmptyRetries[0].row_id -ceq '' -and
        $missingRowsEmptyRetries[0].details.attempt -ceq '1' -and
        $missingRowsEmptyRetries[0].details.code -ceq 'board_rows_missing' -and
        $missingRowsEmptyRetries[0].details.transport_exit -ceq '0' -and
        $missingRowsEmptyRetries[0].details.http_status -ceq '200' -and
        $missingRowsEmptyRetries[0].details.content_type_class -ceq 'json' -and
        -not [string]::IsNullOrWhiteSpace([string]$missingRowsEmptyRetries[0].details.elapsed_ms) -and
        $missingRowsEmptyPropertyNames.Count -eq $expectedRetryPropertyNames.Count -and
        @($expectedRetryPropertyNames | Where-Object { $missingRowsEmptyPropertyNames -cnotcontains $_ }).Count -eq 0 -and
        $missingRowsEmptyDetailNames.Count -eq $expectedMissingRowsDetailNames.Count -and
        @($expectedMissingRowsDetailNames | Where-Object { $missingRowsEmptyDetailNames -cnotcontains $_ }).Count -eq 0
    )
    Assert-True 'missing rows recovery logs no raw response and has no write or inference side effect' (
        -not $missingRowsThenEmpty.log_text.Contains($missingRowsJson) -and
        @($missingRowsThenEmpty.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        -not $missingRowsThenEmpty.claude_called
    )

    $missingRowsThenEligible = Invoke-ReadCase `
        -Name 'missing-rows-then-eligible-observe' `
        -Responses @($missingRowsJson, $eligible) `
        -Mode Observe
    $missingRowsEligibleRetries = @($missingRowsThenEligible.events | Where-Object event -ceq 'board_read_retry')
    $missingRowsCandidates = @($missingRowsThenEligible.events | Where-Object event -ceq 'candidate_observed')
    Assert-True 'missing rows then eligible Observe admits the candidate exactly once' (
        $missingRowsThenEligible.exit_code -eq 0 -and
        $missingRowsThenEligible.result -and
        $missingRowsThenEligible.result.status -ceq 'candidate_observed' -and
        $missingRowsThenEligible.result.work_id -ceq 'ORDER-READ-RETRY' -and
        $missingRowsThenEligible.state.last_poll.status -ceq 'candidate_observed' -and
        [int]$missingRowsThenEligible.state.counts.selected -eq 1 -and
        $missingRowsThenEligible.state.cursor.timestamp -ceq '2026-09-07T08:00:00.0000000Z' -and
        $missingRowsThenEligible.state.cursor.row_id -ceq 'cursor-before-read' -and
        $missingRowsThenEligible.read_count -eq 2 -and
        $missingRowsEligibleRetries.Count -eq 1 -and
        $missingRowsCandidates.Count -eq 1
    )
    Assert-True 'missing rows then eligible Observe performs no synthesis, write, or inference' (
        $missingRowsEligibleRetries[0].code -ceq 'board_rows_missing' -and
        @($missingRowsThenEligible.state.work).Count -eq 0 -and
        @($missingRowsThenEligible.actions).Count -eq 2 -and
        @($missingRowsThenEligible.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        @($missingRowsThenEligible.events | Where-Object {
            @('claim_confirmed', 'receipt_confirmed', 'invocation_started', 'result_confirmed') -ccontains [string]$_.event
        }).Count -eq 0 -and
        -not $missingRowsThenEligible.log_text.Contains($missingRowsJson) -and
        -not $missingRowsThenEligible.claude_called
    )

    $missingRowsBodyCanary = 'ROWS_MISSING_BODY_CANARY'
    $missingRowsCanaryJson = '{"ok":true,"diagnostic":"' + $missingRowsBodyCanary + '"}'
    $missingRowsTwice = Invoke-ReadCase `
        -Name 'missing-rows-twice' `
        -Responses @($missingRowsCanaryJson, $missingRowsJson) `
        -InitialWork (, $preservedWork)
    $missingRowsTwiceRetries = @($missingRowsTwice.events | Where-Object event -ceq 'board_read_retry')
    $missingRowsTwiceRunErrors = @($missingRowsTwice.events | Where-Object event -ceq 'run_error')
    $missingRowsTwiceRetryDetailNames = @($missingRowsTwiceRetries[0].details.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $missingRowsTwiceRunErrorDetailNames = @($missingRowsTwiceRunErrors[0].details.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $expectedMissingRowsRunErrorDetailNames = @(
        'attempt', 'content_type_class', 'elapsed_ms', 'http_status', 'transport_exit'
    )
    Assert-True 'two missing rows envelopes stop after the existing one-retry bound with the public code' (
        $missingRowsTwice.exit_code -eq 20 -and
        $missingRowsTwice.result -and
        $missingRowsTwice.result.status -ceq 'error' -and
        $missingRowsTwice.result.error_code -ceq 'BOARD_ROWS_MISSING' -and
        $missingRowsTwice.state.error.code -ceq 'BOARD_ROWS_MISSING' -and
        $missingRowsTwice.read_count -eq 2 -and
        $missingRowsTwiceRetries.Count -eq 1 -and
        $missingRowsTwiceRunErrors.Count -eq 1
    )
    Assert-True 'missing rows retry stays lowercase while terminal run_error stays public uppercase' (
        $missingRowsTwiceRetries[0].code -ceq 'board_rows_missing' -and
        $missingRowsTwiceRetries[0].details.attempt -ceq '1' -and
        $missingRowsTwiceRetries[0].details.code -ceq 'board_rows_missing' -and
        $missingRowsTwiceRetries[0].message -ceq 'A transient pre-admission board read failed; retrying once.' -and
        $missingRowsTwiceRetryDetailNames.Count -eq $expectedMissingRowsDetailNames.Count -and
        @($expectedMissingRowsDetailNames | Where-Object { $missingRowsTwiceRetryDetailNames -cnotcontains $_ }).Count -eq 0 -and
        $missingRowsTwiceRunErrors[0].code -ceq 'BOARD_ROWS_MISSING' -and
        $missingRowsTwiceRunErrors[0].message -ceq 'board_rows_missing' -and
        $missingRowsTwiceRunErrors[0].details.attempt -ceq '2' -and
        $missingRowsTwiceRunErrors[0].details.http_status -ceq '200' -and
        $missingRowsTwiceRunErrors[0].details.content_type_class -ceq 'json' -and
        $missingRowsTwiceRunErrorDetailNames.Count -eq $expectedMissingRowsRunErrorDetailNames.Count -and
        @($expectedMissingRowsRunErrorDetailNames | Where-Object {
            $missingRowsTwiceRunErrorDetailNames -cnotcontains $_
        }).Count -eq 0
    )
    Assert-True 'two missing rows envelopes preserve the exact cursor and work record' (
        [bool]$missingRowsTwice.state.initialized -and
        $missingRowsTwice.state.cursor.timestamp -ceq '2026-09-07T08:00:00.0000000Z' -and
        $missingRowsTwice.state.cursor.row_id -ceq 'cursor-before-read' -and
        [int]$missingRowsTwice.state.counts.selected -eq 0 -and
        @($missingRowsTwice.state.work).Count -eq 1 -and
        (@($missingRowsTwice.state.work)[0] | ConvertTo-Json -Compress) -ceq $preservedWorkJson
    )
    Assert-True 'two missing rows envelopes expose no raw canary and cause no write or inference' (
        @($missingRowsTwice.actions).Count -eq 2 -and
        @($missingRowsTwice.actions | Where-Object { $_ -cne 'read' }).Count -eq 0 -and
        @($missingRowsTwice.events | Where-Object {
            @('candidate_observed', 'poll_complete') -ccontains [string]$_.event
        }).Count -eq 0 -and
        -not $missingRowsTwice.log_text.Contains($missingRowsBodyCanary) -and
        -not $missingRowsTwice.log_text.Contains($missingRowsJson) -and
        -not $missingRowsTwice.claude_called
    )

    foreach ($permanentStatus in @(401, 403, 418)) {
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
        [pscustomobject]@{ name = 'header-missing'; response = '{"ok":true,"rows":[]}'; code = 'BOARD_HEADER_MISSING' },
        [pscustomobject]@{ name = 'header-invalid'; response = '{"ok":true,"rows":[["bad"]]}'; code = 'BOARD_HEADER_INVALID' }
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
    $runnerTokens = $null
    $runnerParseErrors = $null
    $runnerAst = [Management.Automation.Language.Parser]::ParseFile($RunnerPath, [ref]$runnerTokens, [ref]$runnerParseErrors)
    $classifierDefinitions = @($runnerAst.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -ceq 'Test-TransientBoardReadFailure'
    }, $true))
    if ($classifierDefinitions.Count -eq 1) { Invoke-Expression $classifierDefinitions[0].Extent.Text }
    $lowercaseRowsMissingError = try { throw 'board_rows_missing' } catch { $_ }
    $uppercaseRowsMissingError = try { throw 'BOARD_ROWS_MISSING' } catch { $_ }
    $refusedError = try { throw 'board_read_refused' } catch { $_ }
    $headerMissingError = try { throw 'board_header_missing' } catch { $_ }
    $headerInvalidError = try { throw 'board_header_invalid' } catch { $_ }
    Assert-True 'transient classifier adds only the exact lowercase real-parser missing-rows message' (
        @($runnerParseErrors).Count -eq 0 -and
        $classifierDefinitions.Count -eq 1 -and
        [bool](Test-TransientBoardReadFailure -ErrorRecord $lowercaseRowsMissingError) -and
        -not [bool](Test-TransientBoardReadFailure -ErrorRecord $uppercaseRowsMissingError) -and
        -not [bool](Test-TransientBoardReadFailure -ErrorRecord $refusedError) -and
        -not [bool](Test-TransientBoardReadFailure -ErrorRecord $headerMissingError) -and
        -not [bool](Test-TransientBoardReadFailure -ErrorRecord $headerInvalidError)
    )

    Invoke-DateReaderNegativeControl -CurrentScope $Scope
} finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $script:TestRoot -PathType Container)) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
