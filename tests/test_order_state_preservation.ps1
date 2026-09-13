#Requires -Version 5.1
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ModulePath = Join-Path $RepoRoot 'scripts\OrderSupervisor.psm1'
$RunnerPath = Join-Path $RepoRoot 'scripts\order_supervisor.ps1'
$SchemaPath = Join-Path $RepoRoot 'scripts\order_supervisor_result.schema.json'
Import-Module $ModulePath -Force

$script:Passed = 0
$script:Failed = 0

# WHICH SHELL THE CHILD RUNS IN.
#
# Every case runs the isolated ORDER runner in a CHILD process, hardcoded to
# powershell.exe. On Linux the launch fails silently from this suite's point of view
# and the failure surfaces 160 lines later as "Could not find file events.jsonl" -
# the runner never started, so it never wrote its log. Measured: 1 assertion reached.
#
# That distance is worth keeping in mind when reading any of these ports: the line
# that throws is rarely the line that is wrong.
#
# ORDER_TEST_CHILD_SHELL overrides the child. Unset, this behaves exactly as before.
$script:ChildShell = $env:ORDER_TEST_CHILD_SHELL
if ([string]::IsNullOrWhiteSpace($script:ChildShell)) { $script:ChildShell = 'powershell.exe' }
$script:ResolvedChildShell = (Get-Command $script:ChildShell -CommandType Application -ErrorAction Stop |
                              Select-Object -First 1).Source
Write-Output ("CHILD_SHELL " + $script:ResolvedChildShell)

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

function Get-FileBase64 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [Convert]::ToBase64String([IO.File]::ReadAllBytes($Path))
}

function Read-Events {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    return @(
        [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { [string]$_ | ConvertFrom-Json }
    )
}

function Get-CaseActions {
    param([Parameter(Mandatory = $true)][string]$CaseRoot)
    $path = Join-Path $CaseRoot 'bus-actions.txt'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return @() }
    return @([IO.File]::ReadAllLines($path, [Text.Encoding]::UTF8))
}

function Invoke-StateCase {
    param(
        [Parameter(Mandatory = $true)][string]$CaseRoot,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$LogPath,
        [string]$AllowedSourcesCsv = 'codex'
    )

    $previousCaseRoot = [Environment]::GetEnvironmentVariable('ORDER_STATE_TEST_CASE_ROOT', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('ORDER_STATE_TEST_CASE_ROOT', $CaseRoot, 'Process')
        $arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $script:IsolatedRunner,
            '-Mode', 'Execute',
            '-AllowedSourcesCsv', $AllowedSourcesCsv,
            '-StatePath', $StatePath,
            '-LogPath', $LogPath,
            '-EnvFile', $script:EnvPath,
            '-WorkspacePath', $script:WorkspacePath,
            '-ClaudeCommand', 'unused-test-command'
        )
        $output = @(& $script:ResolvedChildShell @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('ORDER_STATE_TEST_CASE_ROOT', $previousCaseRoot, 'Process')
    }

    $jsonLine = @(
        $output |
            ForEach-Object { [string]$_ } |
            Where-Object { $_.TrimStart().StartsWith('{') } |
            Select-Object -Last 1
    )
    $result = if ($jsonLine.Count -eq 1) { $jsonLine[0] | ConvertFrom-Json } else { $null }
    return [pscustomobject][ordered]@{
        exit_code = $exitCode
        output = @($output)
        output_text = @($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        result = $result
    }
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
$root = [string]$env:ORDER_STATE_TEST_CASE_ROOT
if ([string]::IsNullOrWhiteSpace($root)) { throw 'state_test_case_root_missing' }
[IO.File]::AppendAllText(
    (Join-Path $root 'bus-actions.txt'),
    ([string]$Action + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
)
if ($Action -cne 'read') { throw 'unexpected_board_write' }
$response = [IO.File]::ReadAllText((Join-Path $root 'board.json'), [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($OutFile, $response, [Text.UTF8Encoding]::new($false))
if ($ReadMetadataOutFile) {
    [IO.File]::WriteAllText(
        $ReadMetadataOutFile,
        '{"transport_exit":0,"http_status":200,"content_type_class":"json"}',
        [Text.UTF8Encoding]::new($false)
    )
}
'@

$fakeAdapterSource = @'
param(
    [string]$PromptPath,
    [string]$SchemaPath,
    [string]$StdoutPath,
    [string]$StderrPath,
    [string]$EnvFile,
    [string]$ClaudeCommand,
    [string]$WorkspacePath,
    [double]$MaxBudgetUsd
)
$root = [string]$env:ORDER_STATE_TEST_CASE_ROOT
[IO.File]::WriteAllText(
    (Join-Path $root 'claude-called.txt'),
    'called',
    [Text.UTF8Encoding]::new($false)
)
exit 99
'@

function New-TestBoardJson {
    $header = @(
        'Row_ID', 'Timestamp', 'Source_Tag', 'Target_Surface', 'Action_Type',
        'Payload', 'Category', 'Project Tag', 'Gist', 'Sub-Gist'
    )
    $order = @(
        'state-test-order',
        [DateTime]::UtcNow.AddMinutes(-1).ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture),
        'codex',
        'vm-order-worker',
        'APPEND',
        'BCB|v=1|id=ORDER-STATE-TEST|phase=DISPATCH|class=BUILD|from=codex|to=vm-order-worker|authority=operator-direct|task=offline state preservation test',
        'OPEN',
        'ORDER-SUPERVISOR',
        '',
        ''
    )
    return ([ordered]@{ ok = $true; rows = @($header, $order) } | ConvertTo-Json -Depth 8 -Compress)
}

$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-state-preservation-' + [Guid]::NewGuid().ToString('N'))
$releaseScripts = Join-Path $script:TestRoot 'release\scripts'
$script:WorkspacePath = Join-Path $script:TestRoot 'workspace'
$script:EnvPath = Join-Path $script:TestRoot 'test.env'
$script:IsolatedRunner = Join-Path $releaseScripts 'order_supervisor.ps1'
New-Item -ItemType Directory -Path $releaseScripts -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $script:WorkspacePath '.git') -Force | Out-Null
Copy-Item -LiteralPath $RunnerPath -Destination $script:IsolatedRunner
Copy-Item -LiteralPath $ModulePath -Destination (Join-Path $releaseScripts 'OrderSupervisor.psm1')
Copy-Item -LiteralPath $SchemaPath -Destination (Join-Path $releaseScripts 'order_supervisor_result.schema.json')
Write-TestUtf8 -Path (Join-Path $releaseScripts 'bus.ps1') -Text $fakeBusSource
Write-TestUtf8 -Path (Join-Path $releaseScripts 'invoke_order_claude.ps1') -Text $fakeAdapterSource
Write-TestUtf8 -Path $script:EnvPath -Text 'TEST_ONLY=1'

try {
    $createOnlyRoot = Join-Path $script:TestRoot 'create-new-only-collision'
    New-Item -ItemType Directory -Path $createOnlyRoot | Out-Null
    $createOnlyPath = Join-Path $createOnlyRoot 'state.json'
    Write-TestUtf8 -Path $createOnlyPath -Text 'CREATE_NEW_COLLISION_CANARY'
    $createOnlyBytes = Get-FileBase64 -Path $createOnlyPath
    $createOnlyThrew = $false
    try {
        Save-OrderState -Path $createOnlyPath -State (New-OrderState -Mode Execute) -CreateNewOnly
    } catch [IO.IOException] {
        $createOnlyThrew = $true
    }
    Assert-True 'create-new-only persistence refuses an existing destination byte exact' (
        $createOnlyThrew -and
        (Get-FileBase64 -Path $createOnlyPath) -ceq $createOnlyBytes -and
        @(Get-ChildItem -LiteralPath $createOnlyRoot -Filter '.state.json.*.tmp' -File).Count -eq 0
    )

    $malformedRoot = Join-Path $script:TestRoot 'malformed-existing-state'
    New-Item -ItemType Directory -Path $malformedRoot | Out-Null
    Write-TestUtf8 -Path (Join-Path $malformedRoot 'board.json') -Text (New-TestBoardJson)
    $malformedStatePath = Join-Path $malformedRoot 'state.json'
    $malformedLogPath = Join-Path $malformedRoot 'events.jsonl'
    $priorState = New-OrderState -Mode Execute
    $priorState.initialized = $true
    $priorState.cursor.timestamp = '2026-09-07T08:00:00.0000000Z'
    $priorState.cursor.row_id = 'cursor-before-unseen-order'
    $priorState.counts.polls = 17
    $priorState.work = @([pscustomobject][ordered]@{
        input_row_id = 'STATE_CONTENT_CANARY'
        work_id = 'ORDER-PRIOR'
        status = 'result_confirmed'
        result_status = 'completed'
        output_sha256 = ('a' * 64)
        updated_at = '2026-09-07T07:00:00.000Z'
    })
    $priorJson = $priorState | ConvertTo-Json -Depth 12
    $malformedJson = $priorJson.Substring(0, $priorJson.Length - 1)
    Write-TestUtf8 -Path $malformedStatePath -Text $malformedJson
    $originalStateBytes = Get-FileBase64 -Path $malformedStatePath

    $malformedFirst = Invoke-StateCase `
        -CaseRoot $malformedRoot `
        -StatePath $malformedStatePath `
        -LogPath $malformedLogPath
    $afterFirstBytes = Get-FileBase64 -Path $malformedStatePath
    $malformedSecond = Invoke-StateCase `
        -CaseRoot $malformedRoot `
        -StatePath $malformedStatePath `
        -LogPath $malformedLogPath
    $afterSecondBytes = Get-FileBase64 -Path $malformedStatePath
    $malformedEvents = @(Read-Events -Path $malformedLogPath)
    $malformedRunErrors = @($malformedEvents | Where-Object event -ceq 'run_error')
    $malformedLogText = [IO.File]::ReadAllText($malformedLogPath, [Text.Encoding]::UTF8)
    $malformedActions = @(Get-CaseActions -CaseRoot $malformedRoot)

    Assert-True 'malformed existing state fails twice with fixed code' (
        $malformedFirst.exit_code -eq 20 -and
        $malformedSecond.exit_code -eq 20 -and
        $malformedFirst.result -and
        $malformedSecond.result -and
        $malformedFirst.result.error_code -ceq 'STATE_JSON_INVALID' -and
        $malformedSecond.result.error_code -ceq 'STATE_JSON_INVALID'
    )
    Assert-True 'malformed existing state remains byte exact across repeated failures' (
        $afterFirstBytes -ceq $originalStateBytes -and
        $afterSecondBytes -ceq $originalStateBytes
    )
    Assert-True 'malformed existing state emits only fixed run errors' (
        $malformedRunErrors.Count -eq 2 -and
        @($malformedRunErrors | Where-Object code -cne 'STATE_JSON_INVALID').Count -eq 0 -and
        @($malformedEvents | Where-Object event -ceq 'poll_started').Count -eq 0 -and
        @($malformedEvents | Where-Object event -ceq 'tail_seeded').Count -eq 0
    )
    Assert-True 'malformed state content never reaches output or logs' (
        -not $malformedFirst.output_text.Contains('STATE_CONTENT_CANARY') -and
        -not $malformedSecond.output_text.Contains('STATE_CONTENT_CANARY') -and
        -not $malformedLogText.Contains('STATE_CONTENT_CANARY')
    )
    Assert-True 'malformed existing state performs no board action or Claude invocation' (
        $malformedActions.Count -eq 0 -and
        -not (Test-Path -LiteralPath (Join-Path $malformedRoot 'claude-called.txt') -PathType Leaf)
    )

    $preflightRoot = Join-Path $script:TestRoot 'valid-state-preflight-failure'
    New-Item -ItemType Directory -Path $preflightRoot | Out-Null
    Write-TestUtf8 -Path (Join-Path $preflightRoot 'board.json') -Text (New-TestBoardJson)
    $preflightStatePath = Join-Path $preflightRoot 'state.json'
    $preflightLogPath = Join-Path $preflightRoot 'events.jsonl'
    $validState = New-OrderState -Mode Execute
    $validState.initialized = $true
    $validState.cursor.timestamp = '2026-09-07T08:15:00.0000000Z'
    $validState.cursor.row_id = 'preserved-cursor'
    $validState.counts.polls = 23
    $validState.work = @([pscustomobject][ordered]@{
        input_row_id = 'preserved-row'
        work_id = 'ORDER-PRESERVED'
        status = 'result_confirmed'
        result_status = 'completed'
        output_sha256 = ('b' * 64)
        updated_at = '2026-09-07T08:16:00.000Z'
    })
    Save-OrderState -Path $preflightStatePath -State $validState
    $validStateBytes = Get-FileBase64 -Path $preflightStatePath
    $preflightFailure = Invoke-StateCase `
        -CaseRoot $preflightRoot `
        -StatePath $preflightStatePath `
        -LogPath $preflightLogPath `
        -AllowedSourcesCsv ',codex'
    $preflightSaved = Read-OrderState -Path $preflightStatePath -Mode Execute
    $preflightEvents = @(Read-Events -Path $preflightLogPath)

    Assert-True 'pre-state preflight failure returns fixed nonzero result' (
        $preflightFailure.exit_code -eq 20 -and
        $preflightFailure.result -and
        $preflightFailure.result.error_code -ceq 'ALLOWED_SOURCES_INVALID'
    )
    Assert-True 'pre-state preflight failure preserves valid state byte exact' (
        (Get-FileBase64 -Path $preflightStatePath) -ceq $validStateBytes -and
        [bool]$preflightSaved.initialized -and
        $preflightSaved.cursor.row_id -ceq 'preserved-cursor' -and
        [int]$preflightSaved.counts.polls -eq 23 -and
        @($preflightSaved.work).Count -eq 1
    )
    Assert-True 'pre-state preflight failure logs without board or Claude activity' (
        @($preflightEvents | Where-Object {
            $_.event -ceq 'run_error' -and $_.code -ceq 'ALLOWED_SOURCES_INVALID'
        }).Count -eq 1 -and
        @(Get-CaseActions -CaseRoot $preflightRoot).Count -eq 0 -and
        -not (Test-Path -LiteralPath (Join-Path $preflightRoot 'claude-called.txt') -PathType Leaf)
    )

    $missingRoot = Join-Path $script:TestRoot 'missing-state-preflight-failure'
    New-Item -ItemType Directory -Path $missingRoot | Out-Null
    Write-TestUtf8 -Path (Join-Path $missingRoot 'board.json') -Text (New-TestBoardJson)
    $missingStatePath = Join-Path $missingRoot 'state.json'
    $missingLogPath = Join-Path $missingRoot 'events.jsonl'
    $missingFailure = Invoke-StateCase `
        -CaseRoot $missingRoot `
        -StatePath $missingStatePath `
        -LogPath $missingLogPath `
        -AllowedSourcesCsv ',codex'
    $missingState = Read-OrderState -Path $missingStatePath -Mode Execute
    $missingEvents = @(Read-Events -Path $missingLogPath)

    Assert-True 'missing state still receives a structured error state' (
        $missingFailure.exit_code -eq 20 -and
        $missingFailure.result -and
        $missingFailure.result.error_code -ceq 'ALLOWED_SOURCES_INVALID' -and
        -not [bool]$missingState.initialized -and
        $missingState.cursor.row_id -ceq '' -and
        $missingState.last_poll.status -ceq 'error' -and
        $missingState.error.code -ceq 'ALLOWED_SOURCES_INVALID' -and
        [int]$missingState.counts.errors -eq 1
    )
    Assert-True 'missing-state error logs without board or Claude activity' (
        @($missingEvents | Where-Object {
            $_.event -ceq 'run_error' -and $_.code -ceq 'ALLOWED_SOURCES_INVALID'
        }).Count -eq 1 -and
        @(Get-CaseActions -CaseRoot $missingRoot).Count -eq 0 -and
        -not (Test-Path -LiteralPath (Join-Path $missingRoot 'claude-called.txt') -PathType Leaf)
    )
} finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $script:TestRoot -PathType Container)) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
