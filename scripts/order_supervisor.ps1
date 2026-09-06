#Requires -Version 5.1
<#
One-shot SFDC24 ORDER supervisor. Observe is the default and has no board-write
or Claude path. Execute is explicit. The cursor is (UTC timestamp, Row_ID);
vseq is intentionally never read or persisted.
#>
[CmdletBinding()]
param(
    [ValidateSet('Observe', 'Execute')][string]$Mode = 'Observe',
    [string]$AllowedSourcesCsv = 'chat-mobile,codex',
    [string]$RequiredAuthorityToken = 'operator-direct',
    [string]$StatePath,
    [string]$LogPath,
    [string]$EnvFile,
    [string]$UserProfilePath,
    [string]$WorkspacePath,
    [string]$BoardFixturePath,
    [string]$ClaudeCommand = 'claude',
    [ValidateRange(0.01, 20.0)][double]$MaxBudgetUsd = 2.0,
    [ValidateRange(30, 840)][int]$WallTimeoutSeconds = 300,
    [ValidateRange(1, 10080)][int]$MaxOrderAgeMinutes = 60,
    [switch]$ReplayHistorical
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Import-Module (Join-Path $PSScriptRoot 'OrderSupervisor.psm1') -Force

$BoardTitle = 'Blackboard - Alpha DB'
$BusScript = Join-Path $PSScriptRoot 'bus.ps1'
$ClaudeAdapter = Join-Path $PSScriptRoot 'invoke_order_claude.ps1'
$ClaudeSchema = Join-Path $PSScriptRoot 'order_supervisor_result.schema.json'
$RunId = [Guid]::NewGuid().ToString('N')
$state = $null
$mutex = $null
$mutexHeld = $false
$stateLock = $null
$preflightError = ''
$currentWorkId = ''
$currentRowId = ''
$Mode = if ($Mode -ieq 'Execute') { 'Execute' } else { 'Observe' }
$AllowedSources = @($AllowedSourcesCsv -split ',' | ForEach-Object { $_.Trim() })
$releaseRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$workspacePathWasExplicit = -not [string]::IsNullOrWhiteSpace($WorkspacePath)

if (-not $workspacePathWasExplicit) {
    if ($Mode -ceq 'Execute') {
        $preflightError = 'execute_requires_explicit_workspace_path'
    } else {
        # Observe has no Claude or board-write path, so the historic release-root
        # default remains safe for existing read-only invocations.
        $WorkspacePath = $releaseRoot
    }
} elseif (-not [IO.Path]::IsPathRooted($WorkspacePath)) {
    $preflightError = 'workspace_path_must_be_absolute'
} elseif (-not (Test-Path -LiteralPath $WorkspacePath -PathType Container)) {
    $preflightError = 'workspace_path_missing'
} else {
    $WorkspacePath = [IO.Path]::GetFullPath($WorkspacePath)
    if ($Mode -ceq 'Execute' -and
        -not (Test-Path -LiteralPath (Join-Path $WorkspacePath '.git') -PathType Container)) {
        $preflightError = 'execute_workspace_git_directory_missing'
    }
}

if ($UserProfilePath) {
    if (-not [IO.Path]::IsPathRooted($UserProfilePath)) {
        $preflightError = 'user_profile_path_must_be_absolute'
    } elseif (-not (Test-Path -LiteralPath $UserProfilePath -PathType Container)) {
        $preflightError = 'user_profile_path_missing'
    } else {
        $env:USERPROFILE = [IO.Path]::GetFullPath($UserProfilePath)
        $profileRoot = [IO.Path]::GetPathRoot($env:USERPROFILE)
        $env:HOMEDRIVE = $profileRoot.TrimEnd('\')
        $env:HOMEPATH = $env:USERPROFILE.Substring($profileRoot.Length - 1)
    }
}
if (-not $StatePath) {
    $localRoot = if ($env:ProgramData) { $env:ProgramData } else { Split-Path -Parent $PSScriptRoot }
    $StatePath = Join-Path $localRoot 'SFDC24\OrderSupervisor\state.json'
}
if (-not $LogPath) { $LogPath = Join-Path (Split-Path -Parent $StatePath) 'events.jsonl' }
if (-not $EnvFile) {
    $envRoot = if ([string]::IsNullOrWhiteSpace($WorkspacePath)) { $releaseRoot } else { $WorkspacePath }
    $EnvFile = Join-Path $envRoot '.env'
}
if (-not [IO.Path]::IsPathRooted($EnvFile)) {
    if ([string]::IsNullOrWhiteSpace($preflightError)) {
        $preflightError = 'env_file_path_must_be_absolute'
    }
} else {
    $EnvFile = [IO.Path]::GetFullPath($EnvFile)
    if ([string]::IsNullOrWhiteSpace($preflightError) -and
        $Mode -ceq 'Execute' -and
        -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        $preflightError = 'execute_env_file_missing'
    }
}

function Read-Board {
    if ($BoardFixturePath) {
        if (-not (Test-Path -LiteralPath $BoardFixturePath -PathType Leaf)) { throw 'board_fixture_missing' }
        $raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $BoardFixturePath).Path, [Text.Encoding]::UTF8)
        return @(Get-BoardRowsFromJson -Json $raw)
    }
    if (-not (Test-Path -LiteralPath $BusScript -PathType Leaf)) { throw 'v1_bus_script_missing' }
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw 'v1_bus_env_missing' }
    $temporary = [IO.Path]::GetTempFileName()
    try {
        $busArgs = @{
            Action = 'read'
            Title = $BoardTitle
            OutFile = $temporary
            EnvFile = $EnvFile
        }
        & $BusScript @busArgs | Out-Null
        $raw = [IO.File]::ReadAllText($temporary, [Text.Encoding]::UTF8)
        return @(Get-BoardRowsFromJson -Json $raw)
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Append-Board {
    param([Parameter(Mandatory = $true)][object[]]$Cells)
    if ($BoardFixturePath) { throw 'fixture_append_forbidden' }
    if (@($Cells).Count -ne 10) { throw 'append_requires_exactly_10_cells' }
    $busArgs = @{
        Action = 'append'
        Title = $BoardTitle
        SheetRowJson = (ConvertTo-Json -InputObject @($Cells) -Compress)
        EnvFile = $EnvFile
    }
    & $BusScript @busArgs | Out-Null
}

function Set-LocalError {
    param([string]$Code, [string]$Message, [string]$WorkId = '', [string]$RowId = '')
    $safe = Protect-LogText -Text $Message -MaximumLength 500
    if ($state) {
        $state.error = [pscustomobject][ordered]@{
            at = Get-UtcStamp
            code = $Code
            message = $safe
            work_id = $WorkId
            row_id = $RowId
        }
        $state.counts.errors = [int]$state.counts.errors + 1
        if ($state.last_poll) { $state.last_poll.status = 'error' }
        try { Save-OrderState -Path $StatePath -State $state } catch {}
    } else {
        try {
            $state = New-OrderState -Mode $Mode
            $state.last_poll = [pscustomobject][ordered]@{
                at = Get-UtcStamp
                run_id = $RunId
                status = 'error'
            }
            $state.error = [pscustomobject][ordered]@{
                at = Get-UtcStamp
                code = $Code
                message = $safe
                work_id = $WorkId
                row_id = $RowId
            }
            $state.counts.errors = 1
            Save-OrderState -Path $StatePath -State $state
        } catch {}
    }
    try {
        $logArgs = @{
            Path = $LogPath
            Event = 'run_error'
            Level = 'error'
            RunId = $RunId
            WorkId = $WorkId
            RowId = $RowId
            Code = $Code
            Message = $safe
        }
        Write-OrderLog @logArgs
    } catch {}
}

function Set-Work {
    param([string]$InputRowId, [string]$WorkId, [string]$Status, [string]$ResultStatus = '', [string]$Digest = '')
    $record = @($state.work | Where-Object {
        $_.input_row_id -ceq $InputRowId -and $_.work_id -ceq $WorkId
    } | Select-Object -First 1)
    if ($record.Count -eq 0) {
        $item = [pscustomobject][ordered]@{
            input_row_id = $InputRowId
            work_id = $WorkId
            status = ''
            result_status = ''
            output_sha256 = ''
            updated_at = ''
        }
        $state.work = @($state.work) + @($item)
    } else {
        $item = $record[0]
    }
    $item.status = $Status
    $item.result_status = $ResultStatus
    $item.output_sha256 = $Digest
    $item.updated_at = Get-UtcStamp
    if (@($state.work).Count -gt 500) { $state.work = @($state.work | Select-Object -Last 500) }
}

function Write-Phase {
    param(
        $InputRow,
        [string]$WorkId,
        [ValidateSet('CLAIM', 'RECEIPT', 'RESULT')][string]$Phase,
        [string]$Status,
        [string]$Summary = '',
        [string]$ErrorCode = '',
        [string]$Digest = ''
    )
    $phaseArgs = @{
        InputRow = $InputRow
        WorkId = $WorkId
        Phase = $Phase
        RunId = $RunId
        Status = $Status
        Summary = $Summary
        ErrorCode = $ErrorCode
        OutputSha256 = $Digest
    }
    $cells = @(New-OrderPhaseRow @phaseArgs)
    $readOperation = { Read-Board }
    $appendOperation = {
        param($row)
        $candidate = @($row)
        if ($candidate.Count -eq 1 -and
            $candidate[0] -is [Collections.IEnumerable] -and
            $candidate[0] -isnot [string]) {
            $candidate = @($candidate[0])
        }
        Append-Board -Cells $candidate
    }
    return Invoke-IdempotentBoardAppend -Row $cells -ReadBoard $readOperation -AppendBoard $appendOperation
}

function Quote-ProcessArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value.Contains('"')) { throw 'process_argument_contains_quote' }
    return '"' + $Value + '"'
}

function Invoke-ClaudeWorker {
    param(
        [Parameter(Mandatory = $true)][string]$WorkId,
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Task
    )
    if (-not (Test-Path -LiteralPath $ClaudeAdapter -PathType Leaf)) { throw 'claude_adapter_missing' }
    if (-not (Test-Path -LiteralPath $ClaudeSchema -PathType Leaf)) { throw 'claude_schema_missing' }
    $tempRoot = Join-Path (Split-Path -Parent $StatePath) ('run-' + $RunId)
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    $promptPath = Join-Path $tempRoot 'prompt.txt'
    $stdoutPath = Join-Path $tempRoot 'stdout.json'
    $stderrPath = Join-Path $tempRoot 'stderr.txt'
    try {
        $prompt = New-ClaudeWorkerPrompt `
            -WorkId $WorkId `
            -Source $Source `
            -Task $Task
        Write-Utf8NoBom -Path $promptPath -Text $prompt
        $engine = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if (-not (Test-Path -LiteralPath $engine -PathType Leaf)) { throw 'windows_powershell_5_1_missing' }
        $parts = @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', (Quote-ProcessArgument $ClaudeAdapter),
            '-PromptPath', (Quote-ProcessArgument $promptPath),
            '-SchemaPath', (Quote-ProcessArgument $ClaudeSchema),
            '-StdoutPath', (Quote-ProcessArgument $stdoutPath),
            '-StderrPath', (Quote-ProcessArgument $stderrPath),
            '-EnvFile', (Quote-ProcessArgument $EnvFile),
            '-ClaudeCommand', (Quote-ProcessArgument $ClaudeCommand),
            '-WorkspacePath', (Quote-ProcessArgument $WorkspacePath),
            '-MaxBudgetUsd', ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:0.00}', $MaxBudgetUsd))
        )
        $startArgs = @{
            FilePath = $engine
            ArgumentList = ($parts -join ' ')
            WorkingDirectory = $WorkspacePath
            WindowStyle = 'Hidden'
            PassThru = $true
        }
        $process = Start-Process @startArgs
        if (-not $process.WaitForExit($WallTimeoutSeconds * 1000)) {
            & taskkill.exe /PID $process.Id /T /F 1>$null 2>$null
            throw 'claude_wall_timeout'
        }
        if ($process.ExitCode -ne 0) {
            $diagnostic = ''
            if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
                $diagnostic = (Get-StringSha256 -Text ([IO.File]::ReadAllText($stderrPath))).Substring(0, 16)
            }
            throw ('claude_exit_' + $process.ExitCode + '_stderr_sha256_' + $diagnostic)
        }
        if (-not (Test-Path -LiteralPath $stdoutPath -PathType Leaf)) { throw 'claude_output_missing' }
        $outputInfo = Get-Item -LiteralPath $stdoutPath
        if ($outputInfo.Length -gt 1048576) { throw 'claude_output_too_large' }
        $jsonText = [IO.File]::ReadAllText($outputInfo.FullName, [Text.Encoding]::UTF8)
        return ConvertFrom-ClaudeResultEnvelope -JsonText $jsonText -ExpectedWorkId $WorkId
    } finally {
        if (Test-Path -LiteralPath $tempRoot -PathType Container) {
            Get-ChildItem -LiteralPath $tempRoot -Force | Remove-Item -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $tempRoot -Force -ErrorAction SilentlyContinue
        }
    }
}

try {
    if ($preflightError) { throw $preflightError }
    if ($AllowedSources.Count -eq 0 -or @($AllowedSources | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -gt 0) { throw 'allowed_sources_invalid' }
    if ([string]::IsNullOrWhiteSpace($RequiredAuthorityToken)) { throw 'authority_token_invalid' }
    if ($Mode -ceq 'Execute' -and $BoardFixturePath) { throw 'fixture_execute_forbidden' }
    $created = $false
    $mutex = New-Object Threading.Mutex($false, 'Local\SFDC24-Blackboard-OrderSupervisor', [ref]$created)
    try { $mutexHeld = $mutex.WaitOne(0, $false) } catch [Threading.AbandonedMutexException] { $mutexHeld = $true }
    if (-not $mutexHeld) {
        Write-OrderLog -Path $LogPath -Event 'overlap_suppressed' -Level warning -RunId $RunId -Code 'LOCAL_MUTEX_BUSY'
        [pscustomobject]@{ ok = $true; status = 'overlap_suppressed'; run_id = $RunId } | ConvertTo-Json -Compress
        exit 0
    }
    $stateDirectory = Split-Path -Parent $StatePath
    if (-not (Test-Path -LiteralPath $stateDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    }
    try {
        $stateLock = [IO.File]::Open(
            ($StatePath + '.lock'),
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    } catch [IO.IOException] {
        Write-OrderLog -Path $LogPath -Event 'overlap_suppressed' -Level warning -RunId $RunId -Code 'STATE_FILE_LOCK_BUSY'
        [pscustomobject]@{ ok = $true; status = 'overlap_suppressed'; run_id = $RunId } | ConvertTo-Json -Compress
        exit 0
    }
    $state = Read-OrderState -Path $StatePath -Mode $Mode
    $state.mode = $Mode
    $state.counts.polls = [int]$state.counts.polls + 1
    $state.last_poll = [pscustomobject][ordered]@{
        at = Get-UtcStamp
        run_id = $RunId
        status = 'reading'
        identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        user_profile = $env:USERPROFILE
    }
    Save-OrderState -Path $StatePath -State $state
    Write-OrderLog -Path $LogPath -Event 'poll_started' -RunId $RunId -Details @{ mode = $Mode }

    $rows = @(Read-Board)
    $selection = Get-OrderSelection -Rows $rows -Cursor $state.cursor -AllowedSources $AllowedSources -RequiredAuthorityToken $RequiredAuthorityToken
    if ($selection.newest_seen) {
        $state.seen = [pscustomobject][ordered]@{
            at = Get-UtcStamp
            timestamp = $selection.newest_seen.timestamp
            row_id = $selection.newest_seen.row_id
        }
    }
    $state.counts.seen = [int]$state.counts.seen + [int]$selection.after_cursor_count

    if (-not [bool]$state.initialized -and -not $ReplayHistorical) {
        if ($selection.newest_seen) { $state.cursor = $selection.newest_seen }
        $state.initialized = $true
        $state.last_poll.status = 'tail_seeded'
        $state.success = [pscustomobject][ordered]@{ at = Get-UtcStamp; event = 'tail_seeded'; work_id = ''; row_id = '' }
        Save-OrderState -Path $StatePath -State $state
        Write-OrderLog -Path $LogPath -Event 'tail_seeded' -RunId $RunId -Message 'First run seeded the tuple cursor at the current board tail; no historical order was executed.'
        [pscustomobject]@{ ok = $true; status = 'tail_seeded'; run_id = $RunId; mode = $Mode } | ConvertTo-Json -Compress
        exit 0
    }
    $state.initialized = $true

    foreach ($ignored in @($selection.diagnostics)) {
        $state.counts.ignored = [int]$state.counts.ignored + 1
        Write-OrderLog -Path $LogPath -Event 'row_ignored' -RunId $RunId -RowId $ignored.row_id -Code $ignored.reason -Message 'Row failed deterministic ORDER admission.'
    }
    if (-not $selection.selected) {
        if ($selection.advance_cursor) { $state.cursor = $selection.advance_cursor }
        $state.last_poll.status = 'no_eligible_order'
        $state.success = [pscustomobject][ordered]@{ at = Get-UtcStamp; event = 'poll_complete'; work_id = ''; row_id = '' }
        Save-OrderState -Path $StatePath -State $state
        Write-OrderLog -Path $LogPath -Event 'poll_complete' -RunId $RunId -Details @{ status = 'no_eligible_order'; malformed = $selection.malformed_count }
        [pscustomobject]@{ ok = $true; status = 'no_eligible_order'; run_id = $RunId; mode = $Mode } | ConvertTo-Json -Compress
        exit 0
    }

    $selected = $selection.selected
    $inputRow = $selected.row
    $workId = [string]$selected.assessment.work_id
    $currentWorkId = $workId
    $currentRowId = [string]$inputRow.row_id
    $state.counts.selected = [int]$state.counts.selected + 1
    $orderStamp = [DateTimeOffset]::ParseExact($inputRow.timestamp, 'yyyy-MM-ddTHH:mm:ss.fffffffZ', [Globalization.CultureInfo]::InvariantCulture)
    $ageMinutes = ([DateTimeOffset]::UtcNow - $orderStamp).TotalMinutes
    if (-not $ReplayHistorical -and $ageMinutes -gt $MaxOrderAgeMinutes) {
        $state.cursor = $selection.advance_cursor
        $state.last_poll.status = 'stale_order_ignored'
        $state.counts.ignored = [int]$state.counts.ignored + 1
        Save-OrderState -Path $StatePath -State $state
        Write-OrderLog -Path $LogPath -Event 'row_ignored' -Level warning -RunId $RunId -WorkId $workId -RowId $inputRow.row_id -Code 'STALE_ORDER' -Message 'Eligible order exceeded the configured age and was not executed.'
        [pscustomobject]@{ ok = $true; status = 'stale_order_ignored'; run_id = $RunId; work_id = $workId } | ConvertTo-Json -Compress
        exit 0
    }

    if ($Mode -ceq 'Observe') {
        $state.last_poll.status = 'candidate_observed'
        $state.success = [pscustomobject][ordered]@{ at = Get-UtcStamp; event = 'candidate_observed'; work_id = $workId; row_id = $inputRow.row_id }
        Save-OrderState -Path $StatePath -State $state
        Write-OrderLog -Path $LogPath -Event 'candidate_observed' -RunId $RunId -WorkId $workId -RowId $inputRow.row_id -Details @{ mode = 'Observe'; source = $inputRow.source }
        [pscustomobject]@{ ok = $true; status = 'candidate_observed'; run_id = $RunId; work_id = $workId; row_id = $inputRow.row_id; mode = $Mode } | ConvertTo-Json -Compress
        exit 0
    }

    $current = @(Read-Board)
    $resultId = Get-DeterministicPhaseRowId -InputRowId $inputRow.row_id -WorkId $workId -Phase RESULT
    $receiptId = Get-DeterministicPhaseRowId -InputRowId $inputRow.row_id -WorkId $workId -Phase RECEIPT
    $priorResults = @(Find-BoardRowById -Rows $current -RowId $resultId)
    if ($priorResults.Count -gt 1) { throw 'phase_row_id_duplicate' }
    if ($priorResults.Count -eq 1) {
        Test-PhaseRowIdentity -Row $priorResults[0] -InputRow $inputRow -WorkId $workId -Phase RESULT | Out-Null
        $state.cursor = $selection.advance_cursor
        Set-Work -InputRowId $inputRow.row_id -WorkId $workId -Status 'result_already_present'
        $state.last_poll.status = 'duplicate_suppressed'
        Save-OrderState -Path $StatePath -State $state
        Write-OrderLog -Path $LogPath -Event 'duplicate_suppressed' -RunId $RunId -WorkId $workId -Code 'RESULT_ALREADY_PRESENT'
        [pscustomobject]@{ ok = $true; status = 'duplicate_suppressed'; run_id = $RunId; work_id = $workId } | ConvertTo-Json -Compress
        exit 0
    }
    $priorReceipts = @(Find-BoardRowById -Rows $current -RowId $receiptId)
    if ($priorReceipts.Count -gt 1) { throw 'phase_row_id_duplicate' }
    if ($priorReceipts.Count -eq 1) {
        Test-PhaseRowIdentity -Row $priorReceipts[0] -InputRow $inputRow -WorkId $workId -Phase RECEIPT | Out-Null
    }
    $alreadyStarted = @($state.work | Where-Object {
        $_.input_row_id -ceq $inputRow.row_id -and $_.work_id -ceq $workId -and $_.status -ceq 'invocation_started'
    }).Count -gt 0
    if ($alreadyStarted -or $priorReceipts.Count -gt 0) {
        $suppressed = Write-Phase -InputRow $inputRow -WorkId $workId -Phase RESULT -Status 'failed' -Summary 'A prior invocation has no confirmed result; duplicate execution was suppressed.' -ErrorCode 'DUPLICATE_INVOCATION_SUPPRESSED'
        if (-not $suppressed.confirmed) { throw 'duplicate_suppression_result_unconfirmed' }
        $state.cursor = $selection.advance_cursor
        Set-Work -InputRowId $inputRow.row_id -WorkId $workId -Status 'duplicate_suppressed' -ResultStatus 'failed'
        $state.last_poll.status = 'duplicate_suppressed'
        $state.error = [pscustomobject][ordered]@{
            at = Get-UtcStamp
            code = 'DUPLICATE_INVOCATION_SUPPRESSED'
            message = 'A prior receipt/start existed without a result; Claude was not invoked again.'
            work_id = $workId
            row_id = $inputRow.row_id
        }
        $state.counts.errors = [int]$state.counts.errors + 1
        Save-OrderState -Path $StatePath -State $state
        Write-OrderLog -Path $LogPath -Event 'duplicate_suppressed' -Level error -RunId $RunId -WorkId $workId -RowId $inputRow.row_id -Code 'DUPLICATE_INVOCATION_SUPPRESSED'
        [pscustomobject]@{ ok = $false; status = 'duplicate_suppressed'; run_id = $RunId; work_id = $workId } | ConvertTo-Json -Compress
        exit 30
    }
    $claim = Write-Phase -InputRow $inputRow -WorkId $workId -Phase CLAIM -Status 'claimed'
    if (-not $claim.confirmed) { throw 'claim_append_unconfirmed' }
    $receipt = Write-Phase -InputRow $inputRow -WorkId $workId -Phase RECEIPT -Status 'invocation_starting'
    if (-not $receipt.confirmed) { throw 'receipt_append_unconfirmed' }
    Set-Work -InputRowId $inputRow.row_id -WorkId $workId -Status 'invocation_started'
    $state.last_poll.status = 'invocation_started'
    Save-OrderState -Path $StatePath -State $state

    try {
        $claudeResult = Invoke-ClaudeWorker `
            -WorkId $workId `
            -Source ([string]$inputRow.source) `
            -Task ([string]$selected.assessment.parsed.fields['task'])
    } catch {
        $failureCode = Protect-LogText -Text $_.Exception.Message -MaximumLength 80
        $claudeResult = [pscustomobject][ordered]@{
            schema = 'order_supervisor_result.v1'
            work_id = $workId
            status = 'failed'
            summary = 'The bounded Claude invocation failed; consult the local structured error code.'
            evidence = @()
            error_code = $failureCode
        }
    }
    $canonical = $claudeResult | ConvertTo-Json -Depth 8 -Compress
    $digest = Get-StringSha256 -Text $canonical
    $result = Write-Phase -InputRow $inputRow -WorkId $workId -Phase RESULT -Status ([string]$claudeResult.status) -Summary ([string]$claudeResult.summary) -ErrorCode ([string]$claudeResult.error_code) -Digest $digest
    if (-not $result.confirmed) { throw 'result_append_unconfirmed' }
    $state.cursor = $selection.advance_cursor
    Set-Work -InputRowId $inputRow.row_id -WorkId $workId -Status 'result_confirmed' -ResultStatus ([string]$claudeResult.status) -Digest $digest
    $state.last_poll.status = 'result_confirmed'
    $state.success = [pscustomobject][ordered]@{ at = Get-UtcStamp; event = 'result_confirmed'; work_id = $workId; row_id = $inputRow.row_id }
    if ([string]$claudeResult.status -ceq 'failed') {
        $state.error = [pscustomobject][ordered]@{
            at = Get-UtcStamp
            code = [string]$claudeResult.error_code
            message = 'Claude returned failed status; the failure RESULT was read back.'
            work_id = $workId
            row_id = $inputRow.row_id
        }
        $state.counts.errors = [int]$state.counts.errors + 1
    } else {
        $state.counts.succeeded = [int]$state.counts.succeeded + 1
    }
    Save-OrderState -Path $StatePath -State $state
    Write-OrderLog -Path $LogPath -Event 'result_confirmed' -RunId $RunId -WorkId $workId -RowId $inputRow.row_id -Details @{ status = $claudeResult.status; output_sha256 = $digest }
    [pscustomobject]@{ ok = ([string]$claudeResult.status -cne 'failed'); status = 'result_confirmed'; result_status = $claudeResult.status; run_id = $RunId; work_id = $workId; output_sha256 = $digest } | ConvertTo-Json -Compress
    if ([string]$claudeResult.status -ceq 'failed') { exit 31 }
    exit 0
} catch {
    $message = Protect-LogText -Text $_.Exception.Message -MaximumLength 500
    $code = ($message -replace '[^A-Za-z0-9_.-]', '_').ToUpperInvariant()
    if ($code.Length -gt 80) { $code = $code.Substring(0, 80) }
    if (-not $code) { $code = 'ORDER_SUPERVISOR_ERROR' }
    Set-LocalError -Code $code -Message $message -WorkId $currentWorkId -RowId $currentRowId
    [pscustomobject]@{ ok = $false; status = 'error'; run_id = $RunId; error_code = $code } | ConvertTo-Json -Compress
    exit 20
} finally {
    if ($stateLock) { $stateLock.Dispose() }
    if ($mutexHeld -and $mutex) { try { $mutex.ReleaseMutex() } catch {} }
    if ($mutex) { $mutex.Dispose() }
}
