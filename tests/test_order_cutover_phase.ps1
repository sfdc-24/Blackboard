#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Condition,
        [AllowEmptyString()][string]$Detail = ''
    )
    if ($Condition) {
        $script:Passed++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Failed++
        Write-Output ('FAIL ' + $Name + $(if ($Detail) { ' :: ' + $Detail } else { '' }))
    }
}

function Assert-ThrowsCode {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Body,
        [Parameter(Mandatory = $true)][string]$Code
    )
    $actual = ''
    try { & $Body; $actual = 'NO_ERROR' }
    catch { $actual = [string]$_.Exception.Message }
    Assert-True -Name $Name -Condition ($actual -ceq $Code) -Detail ('actual=' + $actual)
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$driverPath = Join-Path $repoRoot 'infra\azure\order_cutover_phase.ps1'
. $driverPath

$mockNames = @(
    'Invoke-CutoverEscrow',
    'Invoke-CutoverChildScript',
    'Get-CutoverExactInstallerStatus',
    'Get-CutoverExactInstallerStatusAnyState',
    'Get-CutoverTaskRuntime',
    'Get-CutoverFileCheckpoint',
    'Get-CutoverLogCheckpoint',
    'Get-CutoverTrailingLogRun',
    'Assert-CutoverFileCheckpointUnchanged',
    'Export-CutoverTaskXml',
    'Disable-CutoverTask',
    'Assert-CutoverTriggerWindow',
    'Invoke-CutoverInstaller',
    'Assert-CutoverInstallerStatus',
    'Assert-CutoverBackupMatchesExpectedXml',
    'Invoke-CutoverDrainObserve',
    'Assert-CutoverCurrentTerminalRun',
    'Get-CutoverState',
    'Assert-CutoverPinnedExecutables',
    'Get-CutoverProtectedSnapshot',
    'Invoke-CutoverGitRead',
    'Assert-CutoverPinnedFile',
    'Disable-CutoverTaskAfterFailure',
    'Invoke-CutoverOneRun',
    'Start-CutoverTask',
    'Wait-CutoverTaskRun',
    'Get-CutoverLogDelta'
)
$script:OriginalFunctions = @{}
foreach ($name in $mockNames) {
    $script:OriginalFunctions[$name] = (Get-Item -LiteralPath ('Function:' + $name)).ScriptBlock
}

function Set-TestMock {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][scriptblock]$Body)
    Set-Item -LiteralPath ('Function:script:' + $Name) -Value $Body
}

function Reset-TestMocks {
    foreach ($name in $script:OriginalFunctions.Keys) {
        Set-Item -LiteralPath ('Function:script:' + $name) -Value $script:OriginalFunctions[$name]
    }
}

function New-TestContext {
    return [pscustomobject][ordered]@{
        operation_id = '0123456789abcdef0123456789abcdef'
        escrow_id = 'cutover-20260907'
        escrow_path = 'C:\ProgramData\SFDC24\OrderSupervisor\acceptance\cutover-20260907'
        escrow_release_id = ('1' * 40)
        escrow_tool_path = 'C:\ProgramData\SFDC24\OrderSupervisor\tools\escrow.ps1'
        expected_escrow_tool_sha256 = ('5' * 64)
        installer_path = 'C:\ProgramData\SFDC24\OrderSupervisor\releases\candidate\scripts\install_order_supervisor.ps1'
        expected_installer_sha256 = ('6' * 64)
        restored_installer_path = 'C:\ProgramData\SFDC24\OrderSupervisor\releases\old\scripts\install_order_supervisor.ps1'
        expected_restored_installer_sha256 = ('7' * 64)
        release_id = ('2' * 40)
        mode = 'Observe'
        user_profile_path = 'C:\Users\akatiawam'
        workspace_path = 'C:\Users\akatiawam\Blackboard'
        env_file = 'C:\Users\akatiawam\Blackboard\.env'
        state_path = 'C:\ProgramData\SFDC24\OrderSupervisor\state.json'
        log_path = 'C:\ProgramData\SFDC24\OrderSupervisor\events.jsonl'
        claude_command = 'C:\Users\akatiawam\.local\bin\claude.exe'
        wall_timeout_seconds = 720
        max_runs = 4
        timeout_seconds = 600
        poll_milliseconds = 100
        natural_trigger_margin_seconds = 60
        expected_terminal_status = ''
        expected_work_id = ''
        expected_row_id = ''
        expected_result_status = ''
        expected_current_task_result = 0
        expected_current_failure_code = ''
        expected_current_run_id = ''
        expected_disabled_xml_sha256 = ''
        git_path = 'C:\Program Files\Git\cmd\git.exe'
        expected_git_sha256 = ('3' * 64)
        expected_claude_sha256 = ('4' * 64)
        metadata_root = 'C:\ProgramData\SFDC24\OrderSupervisor'
    }
}

function New-TestExactStatus {
    param(
        [DateTime]$Last = [DateTime]'2026-09-07T00:00:00Z',
        [DateTime]$Next = [DateTime]'2099-01-01T00:00:00Z',
        [string]$State = 'Ready',
        [int64]$LastTaskResult = 0
    )
    return [pscustomobject][ordered]@{
        raw = [pscustomobject]@{ state = $State; last_task_result = $LastTaskResult }
        last_run_utc = $Last.ToUniversalTime()
        next_run_utc = $Next.ToUniversalTime()
    }
}

function New-TestInstallerStatusReceipt {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [string]$Mode = 'Execute',
        [string]$State = 'Ready',
        [int64]$LastTaskResult = 0
    )
    $statusText = switch ($State) {
        'Ready' { 'READY' }
        'Disabled' { 'DISABLED' }
        'Running' { 'RUNNING' }
        default { 'NOT_READY' }
    }
    return [pscustomobject][ordered]@{
        status = $statusText
        task_name = 'SFDC24 Blackboard Order Worker'
        task_path = '\'
        state = $State
        mode = $Mode
        user_profile = $Context.user_profile_path
        workspace_path = $Context.workspace_path
        state_path = $Context.state_path
        log_path = $Context.log_path
        wall_timeout_seconds = [int]$Context.wall_timeout_seconds
        last_task_result = [int64]$LastTaskResult
        runner_exists = $true
        env_file_exists = $true
        drift = @()
        wall_timeout_readback = [pscustomobject]@{ confirmed = $true }
        workspace_git_directory_exists = $true
        last_run_time = [DateTime]'2026-09-07T00:00:00Z'
        next_run_time = [DateTime]'2099-01-01T00:00:00Z'
    }
}

function New-TestEscrowReceipt {
    param([string]$Action = 'Validate', [string]$XmlSha256 = ('a' * 64), [bool]$TaskStopped = $false)
    return [pscustomobject][ordered]@{
        schema = 'blackboard.order-task-escrow-receipt.v1'
        ok = $true
        action = $Action
        status = $(if ($Action -ceq 'Validate') { 'VALID' } else { 'RESTORED' })
        escrow_id = 'cutover-20260907'
        escrow_path = 'C:\ProgramData\SFDC24\OrderSupervisor\acceptance\cutover-20260907'
        task_name = 'SFDC24 Blackboard Order Worker'
        task_path = '\'
        expected_release_id = ('1' * 40)
        task_xml_sha256 = $XmlSha256
        release_file_count = 6
        task_stopped = $TaskStopped
    }
}

function New-TestState {
    param(
        [string]$Mode = 'Observe',
        [string]$RunId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        [string]$Status = 'no_eligible_order',
        [string]$WorkId = '',
        [string]$RowId = '',
        [string]$ResultStatus = '',
        [string]$PollAt = '2026-09-07T00:00:00.000Z',
        [string]$ErrorCode = '',
        [string]$ErrorMessage = '',
        [string]$ErrorAt = '2026-09-07T00:00:01.000Z'
    )
    $work = @()
    $successEvent = 'poll_complete'
    if ($Status -ceq 'result_confirmed') {
        $successEvent = 'result_confirmed'
        $work = @([pscustomobject][ordered]@{
            input_row_id = $RowId
            work_id = $WorkId
            status = 'result_confirmed'
            result_status = $ResultStatus
            output_sha256 = ('8' * 64)
            updated_at = '2026-09-07T00:00:00.000Z'
        })
    } elseif ($Status -ceq 'candidate_observed') {
        $successEvent = 'candidate_observed'
    }
    $errorEvidence = $null
    if ($Status -ceq 'error') {
        if ([string]::IsNullOrEmpty($ErrorMessage)) {
            $ErrorMessage = if ($ErrorCode -ceq 'BOARD_HEADER_INVALID') { 'board_header_invalid' } else { $ErrorCode }
        }
        $errorEvidence = [pscustomobject][ordered]@{
            at = $ErrorAt
            code = $ErrorCode
            message = $ErrorMessage
            work_id = $WorkId
            row_id = $RowId
        }
    }
    return [pscustomobject][ordered]@{
        schema = 'order_supervisor_state.v1'
        source_tag = 'vm-order-worker'
        mode = $Mode
        initialized = $true
        cursor = [pscustomobject]@{
            timestamp = '2026-09-07T00:00:00.0000000Z'
            row_id = $(if ($RowId) { $RowId } else { '11111111-1111-1111-1111-111111111111' })
        }
        last_poll = [pscustomobject][ordered]@{
            at = $PollAt
            run_id = $RunId
            status = $Status
            identity = 'NT AUTHORITY\SYSTEM'
            user_profile = 'C:\Users\akatiawam'
        }
        seen = $null
        success = [pscustomobject][ordered]@{
            at = '2026-09-07T00:00:00.000Z'
            event = $successEvent
            work_id = $WorkId
            row_id = $RowId
        }
        error = $errorEvidence
        counts = [pscustomobject]@{
            polls = 1; seen = 1; selected = $(if ($WorkId) { 1 } else { 0 })
            succeeded = $(if ($Status -ceq 'result_confirmed' -and $ResultStatus -cne 'failed') { 1 } else { 0 })
            errors = $(if ($Status -ceq 'error') { 1 } else { 0 }); ignored = 0
        }
        work = $work
    }
}

function New-TestLogEntry {
    param(
        [Parameter(Mandatory = $true)][string]$Event,
        [Parameter(Mandatory = $true)][string]$RunId,
        [string]$WorkId = '',
        [string]$RowId = '',
        [string]$Code = '',
        [string]$Message = '',
        [ValidateSet('debug', 'info', 'warning', 'error')][string]$Level = 'info',
        [string]$At = '2026-09-07T00:00:01.000Z',
        $Details = $null
    )
    $entry = [ordered]@{
        at = $At
        level = $Level
        event = $Event
        run_id = $RunId
        work_id = $WorkId
        row_id = $RowId
        code = $Code
        message = $Message
    }
    if ($null -ne $Details) { $entry.details = $Details }
    return [pscustomobject]$entry
}

function New-TestBoardHeaderDetails {
    return [pscustomobject][ordered]@{
        attempt = '1'
        transport_exit = '0'
        http_status = '200'
        content_type_class = 'json'
        elapsed_ms = '3874.97'
    }
}

function Reset-TestFailedExecuteScenario {
    $script:IncidentRunId = '062af07187da47b29a208dfe4067c573'
    $script:IncidentStart = [DateTime]::UtcNow.AddSeconds(-15)
    $script:IncidentErrorAt = $script:IncidentStart.AddSeconds(4)
    $script:IncidentTerminalAt = $script:IncidentStart.AddSeconds(5)
    $startText = $script:IncidentStart.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $errorText = $script:IncidentErrorAt.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $terminalText = $script:IncidentTerminalAt.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $script:IncidentState = New-TestState `
        -Mode Execute `
        -RunId $script:IncidentRunId `
        -Status error `
        -PollAt $startText `
        -ErrorAt $errorText `
        -ErrorCode BOARD_HEADER_INVALID
    $script:IncidentEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $script:IncidentRunId -At $startText -Details ([pscustomobject]@{ mode = 'Execute' })),
        (New-TestLogEntry -Event run_error -RunId $script:IncidentRunId -Code BOARD_HEADER_INVALID -Message board_header_invalid -Level error -At $terminalText -Details (New-TestBoardHeaderDetails))
    )
    $script:IncidentInitialResult = [int64]20
    $script:IncidentSecondResult = [int64]20
    $script:IncidentDisabledResult = [int64]20
    $script:IncidentSecondLast = $script:IncidentStart
    $script:IncidentDisabledLast = $script:IncidentStart
    $script:IncidentInitialNext = [DateTime]'2099-01-01T00:00:00Z'
    $script:IncidentSecondNext = $script:IncidentInitialNext
    $script:IncidentStatusCall = 0
    $script:IncidentXmlCall = 0
    $script:IncidentDisableCalls = 0
    $script:IncidentInstallCalls = 0
    $script:IncidentCandidateInstalled = $false
    $script:IncidentEscrowAction = ''
    $script:IncidentInstallPath = ''
    $script:IncidentInstallAction = ''
    $script:IncidentInstallMode = ''
    $script:IncidentExpectedCurrentTaskXmlSha256 = ''
    $script:IncidentDrainInstaller = ''
    $script:IncidentDrainInherited = ''
    $script:IncidentAssertInherited = ''
    $script:IncidentBackupUtf8Sha256 = ''
    $script:IncidentBackupUtf16LeBomSha256 = ''
    $script:IncidentDrainCalls = 0
    $script:IncidentCleanupCalls = 0
    $script:IncidentStartCalls = 0
    $script:IncidentBackupCalls = 0
    $script:IncidentCheckpointCalls = 0
    $script:IncidentCheckpointFailureCode = ''
    $script:IncidentTriggerFailureCode = ''
    $script:IncidentInstallFailureCode = ''
    $script:IncidentInstallReceiptFailureCode = ''
    $script:IncidentInstallReceiptStatus = 'READY'
    $script:IncidentCandidateStatusFailureCode = ''
    $script:IncidentBackupFailureCode = ''
    $script:IncidentDrainFailureCode = ''
    $script:IncidentEscrowHash = ''
    $script:IncidentPreDisableXml = ''
    $script:IncidentDisabledXml = ''
    $script:IncidentDefinitionUnknown = $false
    $script:IncidentCleanupInitialState = 'Disabled'
    $script:IncidentCleanupObservedInitialState = ''
    $script:IncidentCleanupActiveStatus = ''
    $script:IncidentCleanupTaskStopped = $false
    $script:IncidentCleanupSequence = New-Object 'System.Collections.Generic.List[string]'
    $script:IncidentStatusSequence = New-Object 'System.Collections.Generic.List[string]'
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('blackboard-cutover-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryRoot -ErrorAction Stop | Out-Null

try {
    # The executable contract must catch declared invalid values itself: no
    # parameter-binding error, no stderr, and exactly one bounded JSON receipt.
    $stdoutPath = Join-Path $temporaryRoot 'invalid.stdout'
    $stderrPath = Join-Path $temporaryRoot 'invalid.stderr'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $process = Start-Process -FilePath $powershell -ArgumentList @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $driverPath, '-Action', 'Invalid',
        '-OperationId', '0123456789abcdef0123456789abcdef',
        '-MaxRuns', 'not-a-number'
    ) -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $stdoutLines = @([IO.File]::ReadAllLines($stdoutPath) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $stderrText = [IO.File]::ReadAllText($stderrPath)
    $invalidReceipt = $stdoutLines[0] | ConvertFrom-Json
    Assert-True 'invalid invocation exits one' ($process.ExitCode -eq 1)
    Assert-True 'invalid invocation emits exactly one JSON line' ($stdoutLines.Count -eq 1 -and $invalidReceipt.code -ceq 'ACTION_INVALID')
    Assert-True 'invalid invocation emits no native stderr' ([string]::IsNullOrEmpty($stderrText))
    Assert-True 'invalid invocation receipt is Azure-bounded' ([Text.Encoding]::UTF8.GetByteCount($stdoutLines[0]) -le 3072)

    Assert-ThrowsCode 'bounded receipt rejects oversized content' {
        ConvertTo-CutoverBoundedReceipt -Receipt ([pscustomobject]@{ data = ('x' * 4000) }) | Out-Null
    } 'RECEIPT_TOO_LARGE'
    $smallReceipt = ConvertTo-CutoverBoundedReceipt -Receipt ([pscustomobject]@{ ok = $true; operation_id = ('0' * 32) })
    Assert-True 'bounded receipt accepts compact content' ([Text.Encoding]::UTF8.GetByteCount($smallReceipt) -lt 3072)

    $originalFailure = $null
    try { Throw-Cutover -Code 'ORIGINAL_ACCEPTANCE_FAILURE' }
    catch { $originalFailure = $_ }
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        [pscustomobject]@{
            mode = 'Execute'; future_triggers_disabled = $true
            definition_preserved_except_enabled = $true; task_stopped = $false
            initial_task_state = 'Running'; final_task_state = 'Disabled'; disable_performed = $true
        }
    }
    $cleanedFailure = $null
    try {
        Throw-CutoverAfterCleanup `
            -FailureRecord $originalFailure `
            -Context (New-TestContext) `
            -Installer 'C:\pinned\installer.ps1' `
            -ExpectedModes @('Execute')
    } catch { $cleanedFailure = $_ }
    $cleanedReceipt = New-CutoverFailureReceipt `
        -ActionValue StartAndAwait `
        -OperationIdValue ('0' * 32) `
        -FailureRecord $cleanedFailure
    $cleanedJson = ConvertTo-CutoverBoundedReceipt -Receipt $cleanedReceipt
    Assert-True 'failure receipt exposes verified disable and original code' (
        $cleanedReceipt.code -ceq 'ORIGINAL_ACCEPTANCE_FAILURE' -and
        $cleanedReceipt.original_code -ceq 'ORIGINAL_ACCEPTANCE_FAILURE' -and
        $cleanedReceipt.cleanup_status -ceq 'DISABLED_VERIFIED' -and
        $cleanedReceipt.cleanup_mode -ceq 'Execute' -and
        $cleanedReceipt.cleanup_initial_task_state -ceq 'Running' -and
        $cleanedReceipt.cleanup_final_task_state -ceq 'Disabled' -and
        $cleanedReceipt.cleanup_disable_performed -and -not $cleanedReceipt.cleanup_task_stopped
    )
    Assert-True 'verified-cleanup failure receipt remains one line and bounded' (
        $cleanedJson.IndexOf("`n", [StringComparison]::Ordinal) -lt 0 -and
        [Text.Encoding]::UTF8.GetByteCount($cleanedJson) -le 3072
    )

    $originalFailure = $null
    try { Throw-Cutover -Code 'ORIGINAL_ACCEPTANCE_FAILURE' }
    catch { $originalFailure = $_ }
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $exception = New-Object InvalidOperationException('FAILURE_CLEANUP_WAIT_TIMEOUT')
        $exception.Data['future_triggers_disabled'] = $true
        $exception.Data['cleanup_definition_preserved'] = $true
        $exception.Data['cleanup_task_stopped'] = $false
        $exception.Data['active_instance_status'] = 'PERSISTED'
        throw $exception
    }
    $failedCleanup = $null
    try {
        Throw-CutoverAfterCleanup `
            -FailureRecord $originalFailure `
            -Context (New-TestContext) `
            -Installer 'C:\pinned\installer.ps1' `
            -ExpectedModes @('Execute')
    } catch { $failedCleanup = $_ }
    $failedCleanupReceipt = New-CutoverFailureReceipt `
        -ActionValue StartAndAwait `
        -OperationIdValue ('0' * 32) `
        -FailureRecord $failedCleanup
    $failedCleanupJson = ConvertTo-CutoverBoundedReceipt -Receipt $failedCleanupReceipt
    Assert-True 'cleanup-failed receipt preserves both failure codes' (
        $failedCleanupReceipt.code -ceq 'CUTOVER_FAILURE_CLEANUP_FAILED' -and
        $failedCleanupReceipt.original_code -ceq 'ORIGINAL_ACCEPTANCE_FAILURE' -and
        $failedCleanupReceipt.cleanup_status -ceq 'FAILED' -and
        $failedCleanupReceipt.cleanup_code -ceq 'FAILURE_CLEANUP_WAIT_TIMEOUT' -and
        $failedCleanupReceipt.future_triggers_disabled -and
        $failedCleanupReceipt.active_instance_status -ceq 'PERSISTED' -and
        [Text.Encoding]::UTF8.GetByteCount($failedCleanupJson) -le 3072
    )
    Reset-TestMocks

    # Emergency cleanup disables scheduling before it waits for an in-flight
    # instance, never stops that instance, and authenticates the final disabled
    # definition.  A timeout still carries positive future-trigger evidence.
    $cleanupContext = New-TestContext
    $script:CleanupXmlEnabled = '<?xml version="1.0"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings><Enabled>true</Enabled></Settings></Task>'
    $script:CleanupXmlDisabled = $script:CleanupXmlEnabled.Replace('<Enabled>true</Enabled>', '<Enabled>false</Enabled>')
    $script:RuntimeStates = @('Running', 'Running', 'Disabled')
    $script:RuntimeIndex = 0
    Set-TestMock 'Get-CutoverTaskRuntime' {
        $state = $script:RuntimeStates[[Math]::Min($script:RuntimeIndex, $script:RuntimeStates.Count - 1)]
        $script:RuntimeIndex++
        [pscustomobject]@{ state = $state }
    }
    $script:CleanupXmlIndex = 0
    Set-TestMock 'Export-CutoverTaskXml' {
        $script:CleanupXmlIndex++
        if ($script:CleanupXmlIndex -eq 1) { $script:CleanupXmlEnabled } else { $script:CleanupXmlDisabled }
    }
    $script:CleanupDisableCalls = 0
    Set-TestMock 'Disable-CutoverTask' { $script:CleanupDisableCalls++ }
    Set-TestMock 'Get-CutoverExactInstallerStatus' {
        param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState, $RequireResultZero)
        if ($ExpectedMode -cne 'Execute' -or $ExpectedTaskState -cne 'Disabled') { Throw-Cutover -Code 'INSTALLER_STATUS_MISMATCH' }
        New-TestExactStatus -State Disabled
    }
    $cleanupResult = Disable-CutoverTaskAfterFailure `
        -Context $cleanupContext `
        -Installer $cleanupContext.installer_path `
        -ExpectedModes @('Execute') `
        -CleanupTimeoutSeconds 2
    Assert-True 'failure quarantine disables before natural post-run completion' (
        $script:CleanupDisableCalls -eq 1 -and $cleanupResult.mode -ceq 'Execute' -and
        $cleanupResult.initial_task_state -ceq 'Running' -and
        $cleanupResult.final_task_state -ceq 'Disabled' -and
        $cleanupResult.future_triggers_disabled -and -not $cleanupResult.task_stopped
    )

    $script:RuntimeStates = @('Running')
    $script:RuntimeIndex = 0
    $script:CleanupXmlIndex = 0
    $hungCleanup = $null
    try {
        Disable-CutoverTaskAfterFailure `
            -Context $cleanupContext `
            -Installer $cleanupContext.installer_path `
            -ExpectedModes @('Execute') `
            -CleanupTimeoutSeconds 1 | Out-Null
    } catch { $hungCleanup = $_ }
    Assert-True 'hung active cleanup fails but proves future triggers disabled' (
        [string]$hungCleanup.Exception.Message -ceq 'FAILURE_CLEANUP_WAIT_TIMEOUT' -and
        [bool]$hungCleanup.Exception.Data['future_triggers_disabled'] -and
        [string]$hungCleanup.Exception.Data['active_instance_status'] -ceq 'PERSISTED' -and
        -not [bool]$hungCleanup.Exception.Data['cleanup_task_stopped']
    )

    $script:RuntimeStates = @('Ready')
    $script:RuntimeIndex = 0
    $script:CleanupXmlIndex = 0
    Set-TestMock 'Disable-CutoverTask' { Throw-Cutover -Code 'TASK_DISABLE_FAILED' }
    Assert-ThrowsCode 'failure quarantine fails closed when disable fails' {
        Disable-CutoverTaskAfterFailure `
            -Context $cleanupContext `
            -Installer $cleanupContext.installer_path `
            -ExpectedModes @('Execute') `
            -CleanupTimeoutSeconds 1 | Out-Null
    } 'TASK_DISABLE_FAILED'

    $xmlDisabledDrift = $script:CleanupXmlDisabled.Replace('</Settings>', '<AllowHardTerminate>false</AllowHardTerminate></Settings>')
    $script:RuntimeStates = @('Ready', 'Disabled')
    $script:RuntimeIndex = 0
    $script:CleanupXmlIndex = 0
    Set-TestMock 'Disable-CutoverTask' { $script:CleanupDisableCalls++ }
    Set-TestMock 'Export-CutoverTaskXml' {
        $script:CleanupXmlIndex++
        if ($script:CleanupXmlIndex -eq 1) { $script:CleanupXmlEnabled } else { $script:XmlDisabledDrift }
    }
    $script:XmlDisabledDrift = $xmlDisabledDrift
    $driftCleanup = $null
    try {
        Disable-CutoverTaskAfterFailure `
            -Context $cleanupContext `
            -Installer $cleanupContext.installer_path `
            -ExpectedModes @('Execute') `
            -CleanupTimeoutSeconds 1 | Out-Null
    } catch { $driftCleanup = $_ }
    Assert-True 'failure quarantine detects definition drift after disable' (
        [string]$driftCleanup.Exception.Message -ceq 'FAILURE_CLEANUP_DEFINITION_DRIFT' -and
        [bool]$driftCleanup.Exception.Data['future_triggers_disabled'] -and
        -not [bool]$driftCleanup.Exception.Data['cleanup_definition_preserved']
    )
    Reset-TestMocks

    Assert-ThrowsCode 'integer parser rejects non-canonical number' {
        ConvertTo-CutoverInteger -Value '01' -Minimum 1 -Maximum 20 -Code 'NUMBER_INVALID' | Out-Null
    } 'NUMBER_INVALID'
    Assert-True 'integer parser accepts bounded canonical number' ((ConvertTo-CutoverInteger -Value '20' -Minimum 1 -Maximum 20 -Code 'NUMBER_INVALID') -eq 20)
    Assert-True 'failed Execute gate accepts only the exact known header failure' (
        (Get-CutoverExpectedFailedExecuteCode -Value 'BOARD_HEADER_INVALID') -ceq 'BOARD_HEADER_INVALID'
    )
    foreach ($unapprovedFailureCode in @('', 'board_header_invalid', 'BOARD_READ_HTTP_ERROR', 'BOARD_READ_TRANSPORT_ERROR')) {
        Assert-ThrowsCode ('failed Execute gate rejects unapproved code ' + $(if ($unapprovedFailureCode) { $unapprovedFailureCode } else { 'empty' })) {
            Get-CutoverExpectedFailedExecuteCode -Value $unapprovedFailureCode | Out-Null
        } 'EXPECTED_CURRENT_FAILURE_CODE_INVALID'
    }
    $normalStatusContext = New-TestContext
    $normalNonzeroStatus = New-TestInstallerStatusReceipt -Context $normalStatusContext -Mode Execute -State Ready -LastTaskResult 20
    Assert-ThrowsCode 'normal installer status semantics still reject result 20' {
        Assert-CutoverInstallerStatus -Status $normalNonzeroStatus -Context $normalStatusContext -ExpectedMode Execute -ExpectedTaskState Ready | Out-Null
    } 'INSTALLER_STATUS_MISMATCH'
    $incidentStatusOkay = $true
    try {
        $null = Assert-CutoverInstallerStatus `
            -Status $normalNonzeroStatus `
            -Context $normalStatusContext `
            -ExpectedMode Execute `
            -ExpectedTaskState Ready `
            -RequireResultZero $false
    } catch { $incidentStatusOkay = $false }
    Assert-True 'nonzero status requires an explicit specialist opt-out' $incidentStatusOkay

    # THE RESULT A REPLACED TASK INHERITS IS NOT THE CANDIDATE'S RESULT.
    #
    # Measured on the guest 2026-09-16: OWCutoverPR119 installed the
    # candidate and disabled cleanly, then failed INSTALLER_STATUS_MISMATCH
    # because the replaced task still reported the OLD worker's
    # last_task_result of 20.  InstallFromDisabledNoStop replaces the
    # definition without unregistering it, and Task Scheduler keeps that
    # value until the task runs again - so the first reads after the install
    # describe a task the candidate has not run yet.
    $inheritedContext = New-TestContext
    $inheritedContext.expected_current_task_result = 20
    $inheritedStatus = New-TestInstallerStatusReceipt -Context $inheritedContext -Mode Observe -State Ready -LastTaskResult 20
    Assert-ThrowsCode 'the guest failure reproduces without the inherited-result opt-in' {
        Assert-CutoverInstallerStatus -Status $inheritedStatus -Context $inheritedContext -ExpectedMode Observe -ExpectedTaskState Ready | Out-Null
    } 'INSTALLER_STATUS_MISMATCH'
    $inheritedOkay = $true
    try {
        $null = Assert-CutoverInstallerStatus `
            -Status $inheritedStatus `
            -Context $inheritedContext `
            -ExpectedMode Observe `
            -ExpectedTaskState Ready `
            -AllowInheritedTaskResult $true
    } catch { $inheritedOkay = $false }
    Assert-True 'the exact pinned inherited result is accepted before the first run' $inheritedOkay

    # The tolerance is for ONE exact value, not for 'non-zero'.  A different
    # result means a NEW failure appeared between the disable and the
    # install, which is precisely the drift these reads exist to catch.
    $unexpectedResultStatus = New-TestInstallerStatusReceipt -Context $inheritedContext -Mode Observe -State Ready -LastTaskResult 21
    Assert-ThrowsCode 'a result other than the pinned inherited one still fails closed' {
        Assert-CutoverInstallerStatus -Status $unexpectedResultStatus -Context $inheritedContext -ExpectedMode Observe -ExpectedTaskState Ready -AllowInheritedTaskResult $true | Out-Null
    } 'INSTALLER_STATUS_MISMATCH'
    $inheritedZeroOkay = $true
    try {
        $null = Assert-CutoverInstallerStatus `
            -Status (New-TestInstallerStatusReceipt -Context $inheritedContext -Mode Observe -State Ready -LastTaskResult 0) `
            -Context $inheritedContext `
            -ExpectedMode Observe `
            -ExpectedTaskState Ready `
            -AllowInheritedTaskResult $true
    } catch { $inheritedZeroOkay = $false }
    Assert-True 'zero is still accepted while the inherited value is tolerated' $inheritedZeroOkay

    # And the opt-in must not quietly become a second spelling of
    # RequireResultZero false: against a context that pins zero it tolerates
    # nothing at all.
    $zeroPinnedContext = New-TestContext
    Assert-ThrowsCode 'the opt-in tolerates nothing when the context pins zero' {
        Assert-CutoverInstallerStatus -Status (New-TestInstallerStatusReceipt -Context $zeroPinnedContext -Mode Observe -State Ready -LastTaskResult 20) -Context $zeroPinnedContext -ExpectedMode Observe -ExpectedTaskState Ready -AllowInheritedTaskResult $true | Out-Null
    } 'INSTALLER_STATUS_MISMATCH'

    $quoted = ConvertTo-CutoverCommandLineArgument -Value 'C:\A path\file.ps1'
    Assert-True 'native argument quoting wraps spaces' ($quoted -ceq '"C:\A path\file.ps1"')
    Assert-ThrowsCode 'single JSON parser rejects multiple records' {
        ConvertFrom-CutoverSingleJsonLine -Text "{}`n{}" -Code 'CHILD_JSON_INVALID' | Out-Null
    } 'CHILD_JSON_INVALID'
    Assert-ThrowsCode 'single JSON parser rejects duplicate object keys' {
        ConvertFrom-CutoverSingleJsonLine -Text '{"ok":true,"ok":false}' -Code 'CHILD_JSON_INVALID' | Out-Null
    } 'CHILD_JSON_INVALID'

    # FRAMING, ON THE STRICT SIDE TOO. The line parser dropped every
    # Char.IsWhiteSpace-only line, so a receipt like VT + CRLF + {"ok":true} came
    # back as one line and was accepted. Measured on the base of this branch: VT,
    # FF, NBSP and U+2028 lines were all discarded that way. This parser governs
    # the escrow receipt and every mutating installer action, so the gap mattered
    # most exactly where the contract is strictest. Found by Copilot on PR114.
    $nonJsonWhitespaceLines = [ordered]@{ vertical_tab = 0x0B; form_feed = 0x0C; nbsp = 0x00A0; line_separator = 0x2028; next_line = 0x0085; ideographic_space = 0x3000 }
    foreach ($nonJsonWhitespaceLine in $nonJsonWhitespaceLines.GetEnumerator()) {
        $blankish = [string][char][int]$nonJsonWhitespaceLine.Value
        Assert-ThrowsCode ('single JSON parser rejects a ' + $nonJsonWhitespaceLine.Key + ' line beside the object') {
            ConvertFrom-CutoverSingleJsonLine -Text ($blankish + "`r`n" + '{"ok":true}' + "`r`n" + $blankish) -Code 'CHILD_JSON_INVALID' | Out-Null
        } 'CHILD_JSON_INVALID'
    }
    # Positive control: lines that are blank by JSON's own definition still are.
    # Captured with try/catch rather than Invoke-TestCapture, which is defined
    # further down this file - calling it here is an unrecognised command, and
    # under this file's ErrorActionPreference that ends the run before the RESULT
    # line, which is the very failure the helper exists to prevent.
    $jsonBlankLines = $null
    try { $jsonBlankLines = ConvertFrom-CutoverSingleJsonLine -Text ("   `r`n`t`r`n" + '{"ok":true}' + "`r`n   ") -Code 'CHILD_JSON_INVALID' }
    catch { $jsonBlankLines = [string]$_.Exception.Message }
    Assert-True 'single JSON parser still drops JSON-whitespace-only lines' (
        $null -ne $jsonBlankLines -and $jsonBlankLines -isnot [string] -and [bool]$jsonBlankLines.ok
    ) ('actual=' + $jsonBlankLines)

    # A MISSING TIME FAILS BY NAME, not through parameter binding. The mandatory
    # untyped $Value rejected $null at binding, so the failure carried no code.
    # Measured, the cast refuses all three values on 5.1.19041 and on pwsh 7.5.4,
    # which is why the named code comes back. These cases are also the detector
    # for the fail-open Copilot raised: on a runtime where [datetime]$null
    # returned DateTime.MinValue instead of throwing, the null case would report
    # NO_ERROR and fail here rather than passing quietly.
    foreach ($missingTime in @($null, '', '   ')) {
        $label = if ($null -eq $missingTime) { 'null' } elseif ($missingTime -eq '') { 'empty' } else { 'whitespace' }
        Assert-ThrowsCode ('a ' + $label + ' time is refused by name') {
            ConvertTo-CutoverUtcDateTime -Value $missingTime -Code 'TASK_NEXT_RUN_TIME_INVALID' | Out-Null
        } 'TASK_NEXT_RUN_TIME_INVALID'
    }
    $realTime = $null
    try { $realTime = ConvertTo-CutoverUtcDateTime -Value '2026-09-14T13:00:00Z' -Code 'TASK_NEXT_RUN_TIME_INVALID' }
    catch { $realTime = [string]$_.Exception.Message }
    Assert-True 'a real time still converts to UTC' (
        $null -ne $realTime -and $realTime -isnot [string] -and
        $realTime -eq [DateTime]::new(2026, 9, 14, 13, 0, 0, [DateTimeKind]::Utc)
    ) ('actual=' + $realTime)
    $disabledStatusContext=New-TestContext
    foreach($nextShape in @('absent','null')){
        $disabledStatus=New-TestInstallerStatusReceipt -Context $disabledStatusContext -State Disabled
        if($nextShape -ceq 'absent'){$disabledStatus.PSObject.Properties.Remove('next_run_time')}else{$disabledStatus.next_run_time=$null}
        $disabledReadback=Assert-CutoverInstallerStatus -Status $disabledStatus -Context $disabledStatusContext -ExpectedMode Execute -ExpectedTaskState Disabled
        Assert-True ('disabled installer status accepts '+$nextShape+' next run without using it') ($disabledReadback.next_run_utc -eq [DateTime]::MinValue -and $disabledReadback.last_run_utc -eq ([DateTime]'2026-09-07T00:00:00Z').ToUniversalTime())
    }
    $readyMissingNext=New-TestInstallerStatusReceipt -Context $disabledStatusContext -State Ready
    $readyMissingNext.next_run_time=$null
    Assert-ThrowsCode 'Ready installer status still requires actual next run time' {Assert-CutoverInstallerStatus -Status $readyMissingNext -Context $disabledStatusContext -ExpectedMode Execute -ExpectedTaskState Ready} 'TASK_NEXT_RUN_TIME_INVALID'
    $disabledRuntime=ConvertTo-CutoverTaskRuntimeInfo -State Disabled -Info ([pscustomobject]@{LastTaskResult=0;LastRunTime=[DateTime]'2026-09-07T00:00:00Z'})
    Assert-True 'disabled runtime accepts absent next run and preserves last run/result' ($disabledRuntime.state -ceq 'Disabled' -and $disabledRuntime.last_task_result -eq 0 -and $disabledRuntime.next_run_utc -eq [DateTime]::MinValue)
    Assert-ThrowsCode 'Ready runtime still rejects null next run' {ConvertTo-CutoverTaskRuntimeInfo -State Ready -Info ([pscustomobject]@{LastTaskResult=0;LastRunTime=[DateTime]'2026-09-07T00:00:00Z';NextRunTime=$null})} 'TASK_NEXT_RUN_TIME_INVALID'
    Assert-True 'real native runtime forwards validated task info to disabled-safe converter' ([IO.File]::ReadAllText($driverPath).Contains('return ConvertTo-CutoverTaskRuntimeInfo -State ([string]$matches[0].State) -Info $info'))
    $prettyInstallerJson = "{`r`n  `"ok`": true,`r`n  `"status`": `"READY`"`r`n}`r`n"
    $prettyInstallerObject = ConvertFrom-CutoverSingleJsonDocument -Text $prettyInstallerJson -Code 'CHILD_JSON_INVALID'
    Assert-True 'single JSON document parser accepts one pretty-printed object' (
        [bool]$prettyInstallerObject.ok -and [string]$prettyInstallerObject.status -ceq 'READY'
    )
    $invalidInstallerJsonCases = [ordered]@{
        multiple_documents = "{}`n{}"
        banner = "banner`n{`"ok`":true}"
        trailer = "{`"ok`":true}`ntrailer"
        duplicate_key = '{"ok":true,"ok":false}'
        array_root = '[{"ok":true}]'
        scalar_root = 'true'
        utf8_bom = ([string][char]0xfeff + '{"ok":true}')
        oversized = ('{"value":"' + ('x' * $script:CutoverChildMaximumCharacters) + '"}')
    }
    foreach ($invalidInstallerJsonCase in $invalidInstallerJsonCases.GetEnumerator()) {
        Assert-ThrowsCode ('single JSON document parser rejects ' + $invalidInstallerJsonCase.Key) {
            ConvertFrom-CutoverSingleJsonDocument -Text ([string]$invalidInstallerJsonCase.Value) -Code 'CHILD_JSON_INVALID' | Out-Null
        } 'CHILD_JSON_INVALID'
    }
    # Acceptance cases run through this helper rather than calling the parser
    # directly.  Measured: a direct call turns a regression into an unhandled
    # terminating error, which aborts the run before the RESULT line is ever
    # written - so the suite reports NO failure count at all, and the reader
    # sees a crash in the finally block instead of the assertion that broke.
    function Invoke-TestCapture {
        param([Parameter(Mandatory = $true)][scriptblock]$Body)
        try { return [pscustomobject]@{ ok = $true; value = (& $Body); error = '' } }
        catch { return [pscustomobject]@{ ok = $false; value = $null; error = [string]$_.Exception.Message } }
    }

    $prettyStatusReceipt = Invoke-TestCapture {
        ConvertFrom-CutoverInstallerReceipt -Text $prettyInstallerJson -RequestedAction Status
    }
    Assert-True 'installer receipt parser accepts a pretty Status receipt' (
        $prettyStatusReceipt.ok -and
        [bool]$prettyStatusReceipt.value.ok -and
        [string]$prettyStatusReceipt.value.status -ceq 'READY') -Detail $prettyStatusReceipt.error

    # Status is read-only and is the ONLY action allowed to arrive pretty.
    # Every mutating action still owes exactly one physical line, which stays
    # safe because mutating actions are only ever issued against
    # $Context.installer_path - the candidate release - and that installer
    # emits -Compress.
    foreach ($mutatingInstallerAction in @('Install', 'InstallFromDisabledNoStop', 'Uninstall', 'Rollback')) {
        Assert-ThrowsCode ('installer receipt parser rejects pretty output for ' + $mutatingInstallerAction) {
            ConvertFrom-CutoverInstallerReceipt `
                -Text $prettyInstallerJson `
                -RequestedAction $mutatingInstallerAction | Out-Null
        } 'INSTALLER_RECEIPT_INVALID'
    }

    # A larger hand-built object than the three-line one above.  It is still a
    # hand-built object and is NOT evidence about what the installer writes -
    # the real receipt in both of its shapes is executed further down.  This
    # only covers nesting and an empty array surviving the round trip.
    $fullStatusContext = New-TestContext
    $fullStatusJson = (New-TestInstallerStatusReceipt -Context $fullStatusContext) | ConvertTo-Json -Depth 10
    Assert-True 'the larger fixture is pretty-printed rather than compressed' (
        @($fullStatusJson -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -gt 1)
    $fullStatusReceipt = Invoke-TestCapture {
        ConvertFrom-CutoverInstallerReceipt -Text $fullStatusJson -RequestedAction Status
    }
    Assert-True 'installer receipt parser accepts the full pretty Status shape' (
        $fullStatusReceipt.ok -and
        [string]$fullStatusReceipt.value.task_name -ceq 'SFDC24 Blackboard Order Worker' -and
        [string]$fullStatusReceipt.value.status -ceq 'READY') -Detail $fullStatusReceipt.error

    # Blank child output must come back as the bounded CODE.  Downstream the
    # byte conversion feeds a Mandatory [byte[]], and PowerShell rejects an
    # empty array at BINDING time; whitespace-only gets past that and dies on a
    # null document element.  Measured, by removing the guard: the two leak
    # "Cannot bind argument to parameter 'Bytes'" and "The property 'NodeType'
    # cannot be found on this object" into a receipt instead of a failure code.
    $blankChildOutputCases = [ordered]@{
        empty = ''
        spaces = '   '
        blank_lines = ('   ' + "`r`n" + '  ')
    }
    foreach ($blankChildOutputCase in $blankChildOutputCases.GetEnumerator()) {
        Assert-ThrowsCode ('single JSON document parser rejects blank output ' + $blankChildOutputCase.Key) {
            ConvertFrom-CutoverSingleJsonDocument -Text ([string]$blankChildOutputCase.Value) -Code 'CHILD_JSON_INVALID' | Out-Null
        } 'CHILD_JSON_INVALID'
    }

    # The escrow tool emits -Compress and keeps the stricter one-line contract.
    # The installer repair must not have loosened it on the way past.
    Assert-ThrowsCode 'escrow parser still refuses a multi-line document' {
        ConvertFrom-CutoverSingleJsonLine -Text $fullStatusJson -Code 'CHILD_JSON_INVALID' | Out-Null
    } 'CHILD_JSON_INVALID'
    # FRAMING: only the four characters JSON itself calls whitespace may wrap a
    # receipt.  Argument-less String.Trim() strips everything char.IsWhiteSpace
    # accepts, which is a far larger set.  Measured under 5.1 before the fix:
    # U+000B, U+000C, U+0085, U+00A0, U+2028 and U+3000 were all stripped and
    # the wrapped receipt parsed clean instead of failing closed.
    $jsonWhitespaceWrappers = [ordered]@{ space = 0x20; tab = 0x09; cr = 0x0D; lf = 0x0A }
    foreach ($jsonWhitespaceWrapper in $jsonWhitespaceWrappers.GetEnumerator()) {
        $jsonWrapper = [string][char][int]$jsonWhitespaceWrapper.Value
        $jsonWrapped = Invoke-TestCapture {
            ConvertFrom-CutoverSingleJsonDocument -Text ($jsonWrapper + '{"ok":true}' + $jsonWrapper) -Code 'CHILD_JSON_INVALID'
        }
        Assert-True ('document parser accepts JSON whitespace framing ' + $jsonWhitespaceWrapper.Key) (
            $jsonWrapped.ok -and [bool]$jsonWrapped.value.ok) -Detail $jsonWrapped.error
    }
    $nonJsonWhitespaceWrappers = [ordered]@{
        vertical_tab = 0x0B
        form_feed = 0x0C
        next_line = 0x85
        no_break_space = 0xA0
        line_separator = 0x2028
        ideographic_space = 0x3000
    }
    foreach ($nonJsonWhitespaceWrapper in $nonJsonWhitespaceWrappers.GetEnumerator()) {
        $nonJsonWrapper = [string][char][int]$nonJsonWhitespaceWrapper.Value
        Assert-ThrowsCode ('document parser rejects non-JSON framing ' + $nonJsonWhitespaceWrapper.Key) {
            ConvertFrom-CutoverSingleJsonDocument -Text ($nonJsonWrapper + '{"ok":true}' + $nonJsonWrapper) -Code 'CHILD_JSON_INVALID' | Out-Null
        } 'CHILD_JSON_INVALID'
    }

    # NOT A FIXTURE.  The two receipt shapes this repair is about, produced by
    # the shipped installer itself and read back through the real child pipe.
    #
    # A hand-built object cannot prove this: it carries whatever fields the test
    # author remembered, so it agrees with the parser for the same reason the
    # parser agrees with it.  Get-StatusObject decides the real field set, and
    # the only honest way to know what it writes is to run it.  The pretty shape
    # is the same shipped source with -Compress removed from its five emitters,
    # which is exactly what every release before that change contained.
    #
    # The byte counts measured by hand - 568 characters over 15 non-blank lines
    # pretty, 318 over one compressed - are deliberately NOT asserted: they are
    # this host's ABSENT status with this host's paths in it, so pinning them
    # would fail on any other machine for a reason that has nothing to do with
    # framing.  What is asserted is what actually has to hold anywhere: the
    # compressed form is exactly one line, the pretty form is more than one,
    # both parse, and both carry the same status.
    Reset-TestMocks
    $shippedInstallerPath = Join-Path $repoRoot 'scripts\install_order_supervisor.ps1'
    $shippedInstallerText = [IO.File]::ReadAllText($shippedInstallerPath)
    $prettyInstallerText = $shippedInstallerText.Replace(
        ' | ConvertTo-Json -Depth 10 -Compress', ' | ConvertTo-Json -Depth 10')
    Assert-True 'the shipped installer really does compress its receipts' (
        $prettyInstallerText -cne $shippedInstallerText)
    $prettyInstallerPath = Join-Path $temporaryRoot 'pretty_install_order_supervisor.ps1'
    [IO.File]::WriteAllText($prettyInstallerPath, $prettyInstallerText, (New-Object Text.UTF8Encoding($false)))
    $realStatusArguments = @(
        '-Action', 'Status',
        '-Mode', 'Observe',
        '-UserProfilePath', $env:USERPROFILE,
        '-WorkspacePath', $repoRoot,
        '-EnvFile', (Join-Path $repoRoot '.env'),
        '-StatePath', (Join-Path $temporaryRoot 'state.json'),
        '-LogPath', (Join-Path $temporaryRoot 'events.jsonl'),
        '-WallTimeoutSeconds', '720',
        '-ClaudeCommand', (Join-Path $temporaryRoot 'claude.exe')
    )
    $compressedRun = Invoke-TestCapture {
        Invoke-CutoverChildScript -ScriptPath $shippedInstallerPath -Arguments $realStatusArguments -TimeoutSeconds 90 -FailureCode 'REAL_STATUS_FAILED'
    }
    $prettyRun = Invoke-TestCapture {
        Invoke-CutoverChildScript -ScriptPath $prettyInstallerPath -Arguments $realStatusArguments -TimeoutSeconds 90 -FailureCode 'REAL_STATUS_FAILED'
    }
    # Build the detail BEFORE asserting, and only from what actually exists.
    # -Detail is an ordinary argument, so it is evaluated eagerly - it does not
    # get the short-circuit protection the condition above enjoys.  When a
    # capture fails, Invoke-TestCapture returns value = $null by design, and
    # reaching through it under Set-StrictMode throws from the FAILURE-REPORTING
    # path itself: the suite dies before the RESULT line, which is precisely the
    # abort the capture helper exists to prevent.
    $realRunDetail = 'compressed_error=' + [string]$compressedRun.error +
        ' pretty_error=' + [string]$prettyRun.error
    if ($compressedRun.ok) {
        $realRunDetail += ' compressed_exit=' + [string]$compressedRun.value.exit_code +
            ' compressed_stderr=' + [string]$compressedRun.value.stderr
    }
    if ($prettyRun.ok) {
        $realRunDetail += ' pretty_exit=' + [string]$prettyRun.value.exit_code +
            ' pretty_stderr=' + [string]$prettyRun.value.stderr
    }
    Assert-True 'both real installer Status runs succeed with empty stderr' (
        $compressedRun.ok -and $prettyRun.ok -and
        $compressedRun.value.exit_code -eq 0 -and $prettyRun.value.exit_code -eq 0 -and
        [string]::IsNullOrWhiteSpace($compressedRun.value.stderr) -and
        [string]::IsNullOrWhiteSpace($prettyRun.value.stderr)
    ) -Detail $realRunDetail
    if ($compressedRun.ok -and $prettyRun.ok) {
        $compressedLines = @([string]$compressedRun.value.stdout -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $prettyLines = @([string]$prettyRun.value.stdout -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        Assert-True 'the shipped installer writes exactly one physical line' (
            $compressedLines.Count -eq 1) -Detail ('lines=' + $compressedLines.Count)
        Assert-True 'the pre-compression installer writes a genuinely multi-line receipt' (
            $prettyLines.Count -gt 1) -Detail ('lines=' + $prettyLines.Count)
        $compressedReceipt = Invoke-TestCapture {
            ConvertFrom-CutoverInstallerReceipt -Text ([string]$compressedRun.value.stdout) -RequestedAction Status
        }
        $prettyReceipt = Invoke-TestCapture {
            ConvertFrom-CutoverInstallerReceipt -Text ([string]$prettyRun.value.stdout) -RequestedAction Status
        }
        Assert-True 'the driver parses a real receipt in both shapes and reads the same status' (
            $compressedReceipt.ok -and $prettyReceipt.ok -and
            -not [string]::IsNullOrWhiteSpace([string]$prettyReceipt.value.status) -and
            [string]$prettyReceipt.value.status -ceq [string]$compressedReceipt.value.status
        ) -Detail ($compressedReceipt.error + ' ' + $prettyReceipt.error)
        Assert-ThrowsCode 'the escrow one-line contract still refuses that real pretty receipt' {
            ConvertFrom-CutoverSingleJsonLine -Text ([string]$prettyRun.value.stdout) -Code 'CHILD_JSON_INVALID' | Out-Null
        } 'CHILD_JSON_INVALID'
    }

    # Static surface: the driver itself has no cloud/bus transport and owns no
    # task register/stop path.  Restore and Install remain inside pinned tools.
    $source = [IO.File]::ReadAllText($driverPath)
    foreach ($forbidden in @('Invoke-RestMethod', 'Invoke-WebRequest', 'az rest', 'bus.ps1', 'Register-ScheduledTask', 'Stop-ScheduledTask')) {
        Assert-True ('driver excludes direct surface ' + $forbidden) ($source.IndexOf($forbidden, [StringComparison]::OrdinalIgnoreCase) -lt 0)
    }
    Assert-True 'driver exposes all eight bounded actions' (@(
        'ValidateEscrowAndDisable', 'DrainObserve', 'InstallObserveAndDrain',
        'InstallObserveAndDrainFromFailedExecute', 'InstallExecuteReady', 'RestoreReady', 'StartAndAwait', 'InstallObserveAndDrainFromDisabledExecute' |
            Where-Object { $source.IndexOf($_, [StringComparison]::Ordinal) -ge 0 }
    ).Count -eq 8)
    Assert-True 'receipt byte cap is literal 3072' ($source.Contains('$script:CutoverReceiptMaximumBytes = 3072'))
    Assert-True 'log and protected inputs have finite byte caps' (
        $source.Contains('$script:CutoverLogMaximumBytes = 67108864') -and
        $source.Contains('$script:CutoverProtectedTreeMaximumBytes = 536870912')
    )

    # A clean Git status is the empty string. Hashing it is legitimate SHA-256
    # input, not a missing mandatory value. The pre-fix helper rejected the
    # resulting empty byte array before StartAndAwait could start the task.
    $emptySha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    $emptyBytesDigest = Invoke-TestCapture { Get-CutoverBytesSha256 -Bytes ([byte[]]@()) }
    Assert-True 'byte digest accepts empty input and returns known SHA256' (
        $emptyBytesDigest.ok -and [string]$emptyBytesDigest.value -ceq $emptySha256
    ) -Detail $emptyBytesDigest.error
    $emptyTextDigest = Invoke-TestCapture { Get-CutoverTextSha256 -Text '' }
    Assert-True 'text digest accepts clean-status empty string and returns known SHA256' (
        $emptyTextDigest.ok -and [string]$emptyTextDigest.value -ceq $emptySha256
    ) -Detail $emptyTextDigest.error
    Assert-True 'nonempty text digest retains known SHA256' (
        (Get-CutoverTextSha256 -Text 'abc') -ceq 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    )

    # Path/hash binding uses the exact ProgramData identities for both tools.
    # Exercise the real protected snapshot at the measured guest executable
    # size, not a mocked snapshot that masks the byte-bound contract.
    Reset-TestMocks
    $sizeContext = New-TestContext
    $sizeContext.metadata_root = Join-Path $temporaryRoot 'size-metadata'
    $sizeContext.workspace_path = Join-Path $temporaryRoot 'size-workspace'
    $sizeContext.user_profile_path = Join-Path $temporaryRoot 'size-profile'
    $sizeRelease = Join-Path $temporaryRoot 'size-release'
    foreach ($directory in @($sizeContext.metadata_root, $sizeContext.workspace_path, $sizeContext.user_profile_path, (Join-Path $sizeRelease 'scripts'))) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $sizeContext.installer_path = Join-Path $sizeRelease 'scripts\install.ps1'
    $sizeContext.env_file = Join-Path $temporaryRoot 'size-env'
    $sizeContext.git_path = Join-Path $temporaryRoot 'size-git.exe'
    $sizeContext.claude_command = Join-Path $temporaryRoot 'size-claude.exe'
    [IO.File]::WriteAllBytes($sizeContext.env_file, [byte[]]@(1, 2, 3))
    [IO.File]::WriteAllBytes($sizeContext.git_path, [byte[]]@(4, 5, 6))
    $sizeStream = [IO.File]::Create($sizeContext.claude_command)
    try { $sizeStream.SetLength(337745056) } finally { $sizeStream.Dispose() }
    Assert-ThrowsCode 'unpinned file retains 256MiB bound at measured Claude size' {
        Get-CutoverFileFingerprint -Path $sizeContext.claude_command | Out-Null
    } 'PROTECTED_FILE_TOO_LARGE'
    Set-TestMock 'Invoke-CutoverGitRead' {
        param($Context, $Arguments, $AllowedExitCodes)
        $stdout = if ($Arguments -contains 'rev-parse') { ('a' * 40) + "`n" }
                  elseif ($Arguments -contains 'symbolic-ref') { "refs/heads/test`n" }
                  else { '' }
        [pscustomobject]@{ exit_code = 0; stdout = $stdout; stderr = '' }
    }
    $sizeSnapshot = Invoke-TestCapture { Get-CutoverProtectedSnapshot -Context $sizeContext }
    Assert-True 'real protected snapshot accepts measured 337745056-byte pinned executable' (
        $sizeSnapshot.ok -and [string]$sizeSnapshot.value -cmatch '^[0-9a-f]{64}$'
    ) -Detail $sizeSnapshot.error
    [IO.File]::WriteAllBytes($sizeContext.git_path, [byte[]]@(7, 8, 9))
    $changedSizeSnapshot = Invoke-TestCapture { Get-CutoverProtectedSnapshot -Context $sizeContext }
    Assert-True 'real protected snapshot still detects executable content mutation' (
        $changedSizeSnapshot.ok -and [string]$changedSizeSnapshot.value -cne [string]$sizeSnapshot.value
    ) -Detail $changedSizeSnapshot.error
    $sizeStream = [IO.File]::OpenWrite($sizeContext.claude_command)
    try { $sizeStream.SetLength(536870913) } finally { $sizeStream.Dispose() }
    Assert-ThrowsCode 'pinned executable still rejects above finite 512MiB bound' {
        Get-CutoverFileFingerprint -Path $sizeContext.claude_command -MaximumBytes $script:CutoverPinnedExecutableMaximumBytes | Out-Null
    } 'PROTECTED_FILE_TOO_LARGE'
    Assert-True 'ordinary files and protected tree bounds remain unchanged' (
        $script:CutoverProtectedFileMaximumBytes -eq 268435456 -and
        $script:CutoverProtectedTreeMaximumBytes -eq 536870912
    )
    Reset-TestMocks

    $oldProgramData = $env:ProgramData
    $fakeProgramData = Join-Path $temporaryRoot 'ProgramData'
    $env:ProgramData = $fakeProgramData
    $toolBytes = [Text.Encoding]::UTF8.GetBytes('escrow-tool')
    $toolSha = Get-CutoverBytesSha256 -Bytes $toolBytes
    $toolPath = Join-Path $fakeProgramData ('SFDC24\OrderSupervisor\tools\order_task_escrow.' + $toolSha + '.ps1')
    New-Item -ItemType Directory -Path (Split-Path -Parent $toolPath) -Force | Out-Null
    [IO.File]::WriteAllBytes($toolPath, $toolBytes)
    $resolvedTool = Resolve-CutoverEscrowTool -Path $toolPath -ExpectedSha256 $toolSha
    Assert-True 'escrow tool binds exact digest-qualified absolute path' ($resolvedTool -ceq [IO.Path]::GetFullPath($toolPath))
    Assert-ThrowsCode 'escrow tool rejects wrong digest' {
        Resolve-CutoverEscrowTool -Path $toolPath -ExpectedSha256 ('f' * 64) | Out-Null
    } 'ESCROW_TOOL_PATH_IDENTITY_MISMATCH'

    $releaseId = '1234567890abcdef1234567890abcdef12345678'
    $installerBytes = [Text.Encoding]::UTF8.GetBytes('installer')
    $installerSha = Get-CutoverBytesSha256 -Bytes $installerBytes
    $installerPath = Join-Path $fakeProgramData ('SFDC24\OrderSupervisor\releases\' + $releaseId + '\scripts\install_order_supervisor.ps1')
    New-Item -ItemType Directory -Path (Split-Path -Parent $installerPath) -Force | Out-Null
    [IO.File]::WriteAllBytes($installerPath, $installerBytes)
    $resolvedInstaller = Resolve-CutoverInstaller -Path $installerPath -ExpectedSha256 $installerSha -ReleaseId $releaseId -Prefix 'INSTALLER'
    Assert-True 'installer binds exact immutable release path and hash' ($resolvedInstaller -ceq [IO.Path]::GetFullPath($installerPath))
    Assert-ThrowsCode 'installer rejects release-path substitution' {
        Resolve-CutoverInstaller -Path $toolPath -ExpectedSha256 $toolSha -ReleaseId $releaseId -Prefix 'INSTALLER' | Out-Null
    } 'INSTALLER_PATH_IDENTITY_MISMATCH'

    # Command-surface/context wiring for the incident action requires all three
    # action-only pins and the two distinct immutable releases.
    $oldReleaseId = 'abcdefabcdefabcdefabcdefabcdefabcdefabcd'
    $oldInstallerBytes = [Text.Encoding]::UTF8.GetBytes('old-installer')
    $oldInstallerSha = Get-CutoverBytesSha256 -Bytes $oldInstallerBytes
    $oldInstallerPath = Join-Path $fakeProgramData ('SFDC24\OrderSupervisor\releases\' + $oldReleaseId + '\scripts\install_order_supervisor.ps1')
    New-Item -ItemType Directory -Path (Split-Path -Parent $oldInstallerPath) -Force | Out-Null
    [IO.File]::WriteAllBytes($oldInstallerPath, $oldInstallerBytes)
    $contextUser = Join-Path $temporaryRoot 'incident-user'
    $contextWorkspace = Join-Path $contextUser 'Blackboard'
    New-Item -ItemType Directory -Path $contextWorkspace -Force | Out-Null
    $contextValues = @{
        OperationId = '0123456789abcdef0123456789abcdef'
        EscrowToolPath = $toolPath
        ExpectedEscrowToolSha256 = $toolSha
        EscrowId = 'incident-cutover'
        ExpectedEscrowReleaseId = $oldReleaseId
        InstallerPath = $installerPath
        ExpectedInstallerSha256 = $installerSha
        ExpectedReleaseId = $releaseId
        RestoredInstallerPath = $oldInstallerPath
        ExpectedRestoredInstallerSha256 = $oldInstallerSha
        Mode = 'Observe'
        UserProfilePath = $contextUser
        WorkspacePath = $contextWorkspace
        EnvFile = (Join-Path $contextWorkspace '.env')
        StatePath = (Join-Path $fakeProgramData 'SFDC24\OrderSupervisor\state.json')
        LogPath = (Join-Path $fakeProgramData 'SFDC24\OrderSupervisor\events.jsonl')
        ClaudeCommand = (Join-Path $contextUser 'claude.exe')
        WallTimeoutSeconds = '720'
        MaxRuns = '4'
        TimeoutSeconds = '600'
        PollMilliseconds = '100'
        NaturalTriggerMarginSeconds = '60'
        ExpectedTerminalStatus = ''
        ExpectedWorkId = ''
        ExpectedRowId = ''
        ExpectedResultStatus = ''
        ExpectedCurrentTaskResult = '20'
        ExpectedCurrentFailureCode = 'BOARD_HEADER_INVALID'
        ExpectedCurrentRunId = '062af07187da47b29a208dfe4067c573'
        GitPath = ''
        ExpectedGitSha256 = ''
        ExpectedClaudeSha256 = ''
    }
    $wiredIncidentContext = New-CutoverContext `
        -RequestedAction InstallObserveAndDrainFromFailedExecute `
        -Values $contextValues
    Assert-True 'incident context wires exact result code run and distinct releases' (
        $wiredIncidentContext.expected_current_task_result -eq 20 -and
        $wiredIncidentContext.expected_current_failure_code -ceq 'BOARD_HEADER_INVALID' -and
        $wiredIncidentContext.expected_current_run_id -ceq $contextValues.ExpectedCurrentRunId -and
        $wiredIncidentContext.release_id -cne $wiredIncidentContext.escrow_release_id
    )
    foreach ($badResult in @('', '0', '020', '21')) {
        $badValues = $contextValues.Clone()
        $badValues.ExpectedCurrentTaskResult = $badResult
        Assert-ThrowsCode ('incident context rejects task-result input ' + $(if ($badResult) { $badResult } else { 'empty' })) {
            New-CutoverContext -RequestedAction InstallObserveAndDrainFromFailedExecute -Values $badValues | Out-Null
        } 'EXPECTED_CURRENT_TASK_RESULT_INVALID'
    }
    foreach ($badCode in @('', 'board_header_invalid', 'BOARD_READ_HTTP_ERROR')) {
        $badValues = $contextValues.Clone()
        $badValues.ExpectedCurrentFailureCode = $badCode
        Assert-ThrowsCode ('incident context rejects failure-code input ' + $(if ($badCode) { $badCode } else { 'empty' })) {
            New-CutoverContext -RequestedAction InstallObserveAndDrainFromFailedExecute -Values $badValues | Out-Null
        } 'EXPECTED_CURRENT_FAILURE_CODE_INVALID'
    }
    foreach ($badRunId in @('', ('A' * 32), ('f' * 31), ('f' * 33))) {
        $badValues = $contextValues.Clone()
        $badValues.ExpectedCurrentRunId = $badRunId
        Assert-ThrowsCode ('incident context rejects run-id input length ' + $badRunId.Length) {
            New-CutoverContext -RequestedAction InstallObserveAndDrainFromFailedExecute -Values $badValues | Out-Null
        } 'EXPECTED_CURRENT_RUN_ID_INVALID'
    }
    $sameReleaseValues = $contextValues.Clone()
    $sameReleaseValues.InstallerPath = $oldInstallerPath
    $sameReleaseValues.ExpectedInstallerSha256 = $oldInstallerSha
    $sameReleaseValues.ExpectedReleaseId = $oldReleaseId
    Assert-ThrowsCode 'incident context rejects candidate equal to escrow release' {
        New-CutoverContext -RequestedAction InstallObserveAndDrainFromFailedExecute -Values $sameReleaseValues | Out-Null
    } 'FAILED_EXECUTE_RELEASE_NOT_ADVANCED'
    $wrongModeValues = $contextValues.Clone()
    $wrongModeValues.Mode = 'Execute'
    Assert-ThrowsCode 'incident context requires requested Observe mode' {
        New-CutoverContext -RequestedAction InstallObserveAndDrainFromFailedExecute -Values $wrongModeValues | Out-Null
    } 'ACTION_REQUIRES_OBSERVE_MODE'
    $ordinaryValues = $contextValues.Clone()
    Assert-ThrowsCode 'ordinary action refuses incident-only bypass inputs' {
        New-CutoverContext -RequestedAction DrainObserve -Values $ordinaryValues | Out-Null
    } 'FAILED_EXECUTE_INPUTS_ACTION_MISMATCH'
    $env:ProgramData = $oldProgramData

    $fakeClaudePath = Join-Path $temporaryRoot 'claude.exe'
    $fakeGitPath = Join-Path $temporaryRoot 'git.exe'
    [IO.File]::WriteAllBytes($fakeClaudePath, [Text.Encoding]::UTF8.GetBytes('claude'))
    [IO.File]::WriteAllBytes($fakeGitPath, [Text.Encoding]::UTF8.GetBytes('git'))
    $pinContext = [pscustomobject]@{
        claude_command = $fakeClaudePath
        git_path = $fakeGitPath
        expected_claude_sha256 = (Get-CutoverFileSha256 -Path $fakeClaudePath)
        expected_git_sha256 = (Get-CutoverFileSha256 -Path $fakeGitPath)
    }
    $pinnedOkay = $true
    try { Assert-CutoverPinnedExecutables -Context $pinContext }
    catch { $pinnedOkay = $false }
    Assert-True 'Execute binaries accept exact pinned paths and hashes' $pinnedOkay
    $pinContext.expected_claude_sha256 = ('0' * 64)
    Assert-ThrowsCode 'Execute binary gate rejects wrong Claude digest' {
        Assert-CutoverPinnedExecutables -Context $pinContext
    } 'CLAUDE_SHA256_MISMATCH'
    $pinContext.expected_claude_sha256 = Get-CutoverFileSha256 -Path $fakeClaudePath
    Remove-Item -LiteralPath $fakeGitPath -Force
    Assert-ThrowsCode 'Execute binary gate rejects missing Git executable' {
        Assert-CutoverPinnedExecutables -Context $pinContext
    } 'GIT_MISSING'
    Set-TestMock 'Invoke-CutoverGitRead' {
        param($Context, $Arguments, $AllowedExitCodes)
        $verb = [string]$Arguments[2]
        if ($verb -ceq 'rev-parse') { [pscustomobject]@{ stdout = (('a' * 40) + "`n") } }
        elseif ($verb -ceq 'symbolic-ref') { [pscustomobject]@{ stdout = "refs/heads/main`n" } }
        else { [pscustomobject]@{ stdout = " M scripts/order_supervisor.ps1`n" } }
    }
    Assert-ThrowsCode 'Execute protected baseline rejects a dirty workspace' {
        Get-CutoverGitSnapshot -Context (New-TestContext) | Out-Null
    } 'GIT_WORKSPACE_NOT_CLEAN'
    Set-TestMock 'Invoke-CutoverGitRead' {
        param($Context, $Arguments, $AllowedExitCodes)
        $verb = [string]$Arguments[2]
        if ($verb -ceq 'rev-parse') { [pscustomobject]@{ stdout = (('a' * 40) + "`n") } }
        elseif ($verb -ceq 'symbolic-ref') { [pscustomobject]@{ stdout = "refs/heads/main`n" } }
        else { [pscustomobject]@{ stdout = '' } }
    }
    $cleanGitSnapshot = Invoke-TestCapture { Get-CutoverGitSnapshot -Context (New-TestContext) }
    $expectedCleanGitSnapshot = Get-CutoverTextSha256 -Text ((('a' * 40), 'refs/heads/main', $emptySha256) -join '|')
    Assert-True 'Execute protected baseline hashes a clean Git status through real digest helper' (
        $cleanGitSnapshot.ok -and [string]$cleanGitSnapshot.value -ceq $expectedCleanGitSnapshot
    ) -Detail $cleanGitSnapshot.error

    # Exercise the real escrow child-receipt gate: the tool path is pinned by
    # context and the receipt must also bind the exact acceptance directory.
    Reset-TestMocks
    $escrowContext = New-TestContext
    $escrowContext.escrow_tool_path = $toolPath
    $escrowContext.expected_escrow_tool_sha256 = $toolSha
    $script:ChildStdout = (New-TestEscrowReceipt | ConvertTo-Json -Compress)
    $script:ChildArguments = @()
    $script:ChildTimeout = 0
    Set-TestMock 'Invoke-CutoverChildScript' {
        param($ScriptPath, $Arguments, $TimeoutSeconds, $FailureCode)
        $script:ChildArguments = @($Arguments)
        $script:ChildTimeout = $TimeoutSeconds
        [pscustomobject]@{ exit_code = 0; stdout = $script:ChildStdout; stderr = '' }
    }
    $realEscrowReceipt = Invoke-CutoverEscrow -Context $escrowContext -RequestedAction Validate
    Assert-True 'escrow child receipt binds exact acceptance path' (
        $realEscrowReceipt.escrow_path -ceq $escrowContext.escrow_path
    )
    $wrongEscrowReceipt = New-TestEscrowReceipt
    $wrongEscrowReceipt.escrow_path = 'C:\ProgramData\SFDC24\OrderSupervisor\acceptance\wrong'
    $script:ChildStdout = $wrongEscrowReceipt | ConvertTo-Json -Compress
    Assert-ThrowsCode 'escrow child receipt rejects wrong acceptance path' {
        Invoke-CutoverEscrow -Context $escrowContext -RequestedAction Validate | Out-Null
    } 'ESCROW_RECEIPT_INVALID'
    [IO.File]::WriteAllBytes($toolPath, [Text.Encoding]::UTF8.GetBytes('changed-escrow-tool'))
    Assert-ThrowsCode 'escrow child rechecks pinned digest immediately before execution' {
        Invoke-CutoverEscrow -Context $escrowContext -RequestedAction Validate | Out-Null
    } 'ESCROW_TOOL_SHA256_MISMATCH'

    $installerContext = New-TestContext
    $installerContext.installer_path = $installerPath
    $installerContext.expected_installer_sha256 = $installerSha
    $restoredInstallerPath = Join-Path $temporaryRoot 'restored-installer.ps1'
    [IO.File]::WriteAllBytes($restoredInstallerPath, [Text.Encoding]::UTF8.GetBytes('restored-installer'))
    $restoredInstallerSha = Get-CutoverFileSha256 -Path $restoredInstallerPath
    $installerContext.restored_installer_path = $restoredInstallerPath
    $installerContext.expected_restored_installer_sha256 = $restoredInstallerSha
    $script:ChildStdout = '{"ok":true}'
    $candidateChild = Invoke-CutoverInstaller -Context $installerContext -ScriptPath $installerPath -RequestedAction Status -RequestedMode Observe
    $restoredChild = Invoke-CutoverInstaller -Context $installerContext -ScriptPath $restoredInstallerPath -RequestedAction Status -RequestedMode Execute
    Assert-True 'candidate and restored installer children accept compact receipts and recheck distinct pins' (
        [bool]$candidateChild.ok -and [bool]$restoredChild.ok
    )
    $script:ChildStdout = "{`r`n  `"ok`": true`r`n}`r`n"
    $prettyCandidateStatus = Invoke-TestCapture {
        Invoke-CutoverInstaller -Context $installerContext -ScriptPath $installerPath -RequestedAction Status -RequestedMode Observe
    }
    Assert-True 'candidate installer accepts a pretty Status receipt from any release' (
        $prettyCandidateStatus.ok -and [bool]$prettyCandidateStatus.value.ok) -Detail $prettyCandidateStatus.error
    $script:ChildStdout = '{"ok":true}'
    $noStopExpectedXmlSha256 = ('a' * 64)
    $noStopChild = Invoke-CutoverInstaller `
        -Context $installerContext `
        -ScriptPath $installerPath `
        -RequestedAction InstallFromDisabledNoStop `
        -RequestedMode Observe `
        -ExpectedCurrentTaskXmlSha256 $noStopExpectedXmlSha256
    Assert-True 'incident installer child receives the dedicated no-stop action and install timeout' (
        [bool]$noStopChild.ok -and
        ($script:ChildArguments -join '|').Contains('-Action|InstallFromDisabledNoStop|-Mode|Observe') -and
        ($script:ChildArguments -join '|').Contains('-ExpectedCurrentTaskXmlSha256|' + $noStopExpectedXmlSha256) -and
        $script:ChildTimeout -eq 180
    ) (($script:ChildArguments -join '|') + '; timeout=' + $script:ChildTimeout)
    [IO.File]::WriteAllBytes($installerPath, [Text.Encoding]::UTF8.GetBytes('changed-installer'))
    Assert-ThrowsCode 'candidate installer child rechecks pinned digest immediately before execution' {
        Invoke-CutoverInstaller -Context $installerContext -ScriptPath $installerPath -RequestedAction Status -RequestedMode Observe | Out-Null
    } 'INSTALLER_SHA256_MISMATCH'
    [IO.File]::WriteAllBytes($restoredInstallerPath, [Text.Encoding]::UTF8.GetBytes('changed-restored-installer'))
    Assert-ThrowsCode 'restored installer child rechecks pinned digest immediately before execution' {
        Invoke-CutoverInstaller -Context $installerContext -ScriptPath $restoredInstallerPath -RequestedAction Status -RequestedMode Execute | Out-Null
    } 'RESTORED_INSTALLER_SHA256_MISMATCH'

    # The carveout follows the ACTION, so it reaches both installer roles and
    # needs no release or digest constant to get there.  That is the whole
    # point: a constant that has to match the guest is one more input that can
    # be wrong, and when it is wrong it fails as INSTALLER_RECEIPT_INVALID -
    # indistinguishable from the defect being repaired, and only discoverable
    # after another Managed Run Command round trip.  The real pin checks above
    # prove the immediate pre-execution digest behavior; this mock isolates
    # role routing only.
    Set-TestMock 'Assert-CutoverPinnedFile' {
        param($Path, $ExpectedSha256, $ExpectedPath, $Prefix)
        return $Path
    }
    $anyReleaseContext = New-TestContext
    $script:ChildStdout = "{`r`n  `"ok`": true`r`n}`r`n"
    $primaryPrettyStatus = Invoke-TestCapture {
        Invoke-CutoverInstaller `
            -Context $anyReleaseContext `
            -ScriptPath $anyReleaseContext.installer_path `
            -RequestedAction Status `
            -RequestedMode Execute
    }
    $restoredPrettyStatus = Invoke-TestCapture {
        Invoke-CutoverInstaller `
            -Context $anyReleaseContext `
            -ScriptPath $anyReleaseContext.restored_installer_path `
            -RequestedAction Status `
            -RequestedMode Execute
    }
    Assert-True 'pretty Status compatibility reaches primary and restored roles' (
        $primaryPrettyStatus.ok -and $restoredPrettyStatus.ok
    ) -Detail ($primaryPrettyStatus.error + ' ' + $restoredPrettyStatus.error)
    Assert-ThrowsCode 'pretty compatibility does not broaden to mutation in either role' {
        Invoke-CutoverInstaller `
            -Context $anyReleaseContext `
            -ScriptPath $anyReleaseContext.installer_path `
            -RequestedAction Install `
            -RequestedMode Observe | Out-Null
    } 'INSTALLER_RECEIPT_INVALID'
    Reset-TestMocks

    # XML evidence explicitly separates UTF-8 text hashes from the escrow's
    # UTF-16LE-with-BOM file representation.
    $xmlEnabled = '<?xml version="1.0"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings><Enabled>true</Enabled></Settings></Task>'
    $xmlDisabled = $xmlEnabled.Replace('<Enabled>true</Enabled>', '<Enabled>false</Enabled>')
    $xmlEnabledDefault = $xmlEnabled.Replace('<Enabled>true</Enabled>', '')
    $enabledEvidence = Get-CutoverTaskXmlEvidence -Text $xmlEnabled
    $disabledEvidence = Get-CutoverTaskXmlEvidence -Text $xmlDisabled
    $defaultEnabledEvidence = Get-CutoverTaskXmlEvidence -Text $xmlEnabledDefault
    Assert-True 'task XML detects enabled and disabled states' ($enabledEvidence.enabled -and -not $disabledEvidence.enabled)
    Assert-True 'normalization proves only Enabled changed' ($enabledEvidence.normalized_sha256 -ceq $disabledEvidence.normalized_sha256)
    Assert-True 'omitted Enabled uses the scheduler schema true default' $defaultEnabledEvidence.enabled
    Assert-True 'omitted Enabled normalizes with explicit enabled and disabled forms' (
        $defaultEnabledEvidence.normalized_sha256 -ceq $enabledEvidence.normalized_sha256 -and
        $defaultEnabledEvidence.normalized_sha256 -ceq $disabledEvidence.normalized_sha256
    )
    Assert-ThrowsCode 'duplicate Enabled nodes remain invalid' {
        Get-CutoverTaskXmlEvidence -Text $xmlEnabled.Replace('</Settings>', '<Enabled>true</Enabled></Settings>') | Out-Null
    } 'TASK_XML_ENABLED_INVALID'
    Assert-ThrowsCode 'invalid explicit Enabled text remains invalid' {
        Get-CutoverTaskXmlEvidence -Text $xmlEnabled.Replace('>true<', '>True<') | Out-Null
    } 'TASK_XML_ENABLED_INVALID'
    Assert-True 'UTF8 text and UTF16 BOM XML hashes remain distinct' ($enabledEvidence.utf8_text_sha256 -cne $enabledEvidence.utf16le_bom_sha256)

    # The fixtures above use a Settings element with ONE child, where "remove
    # Enabled and re-append it" and "leave Enabled where it is" are the same
    # operation - there is nowhere else for the node to go. Task Scheduler
    # exports Enabled ninth among its siblings, and the live cutover compares a
    # pre-XML that OMITS Enabled against a post-XML that carries it in schema
    # position. Canonicalizing those to different positions still passes every
    # single-child assertion above and fails only on the guest, as a spurious
    # definition-changed on the exact path this driver exists to repair. So the
    # position has to be pinned against a realistic shape, not a minimal one.
    $realSettingsBody = @(
        '<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
        '<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
        '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>'
        '<AllowHardTerminate>true</AllowHardTerminate>'
        '<StartWhenAvailable>false</StartWhenAvailable>'
        '<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>'
        '<IdleSettings><StopOnIdleEnd>true</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>'
        '<AllowStartOnDemand>true</AllowStartOnDemand>'
        '<Enabled>true</Enabled>'
        '<Hidden>false</Hidden>'
        '<RunOnlyIfIdle>false</RunOnlyIfIdle>'
        '<WakeToRun>false</WakeToRun>'
        '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>'
        '<Priority>7</Priority>'
    ) -join ''
    $realTaskPrefix = '<?xml version="1.0"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings>'
    $realTaskSuffix = '</Settings></Task>'
    $siblingEnabled = $realTaskPrefix + $realSettingsBody + $realTaskSuffix
    $siblingOmitted = $realTaskPrefix + $realSettingsBody.Replace('<Enabled>true</Enabled>', '') + $realTaskSuffix
    $siblingDisabled = $realTaskPrefix + $realSettingsBody.Replace('<Enabled>true</Enabled>', '<Enabled>false</Enabled>') + $realTaskSuffix
    $siblingOtherSetting = $realTaskPrefix + $realSettingsBody.Replace('<Priority>7</Priority>', '<Priority>5</Priority>') + $realTaskSuffix
    $siblingEnabledEvidence = Get-CutoverTaskXmlEvidence -Text $siblingEnabled
    $siblingOmittedEvidence = Get-CutoverTaskXmlEvidence -Text $siblingOmitted
    $siblingDisabledEvidence = Get-CutoverTaskXmlEvidence -Text $siblingDisabled
    $siblingOtherEvidence = Get-CutoverTaskXmlEvidence -Text $siblingOtherSetting
    Assert-True 'omitted Enabled among siblings still reads as enabled' (
        $siblingOmittedEvidence.enabled -and -not $siblingDisabledEvidence.enabled
    )
    Assert-True 'Enabled position does not change the normalized digest' (
        $siblingOmittedEvidence.normalized_sha256 -ceq $siblingEnabledEvidence.normalized_sha256 -and
        $siblingOmittedEvidence.normalized_sha256 -ceq $siblingDisabledEvidence.normalized_sha256
    )
    Assert-True 'a changed sibling setting still moves the normalized digest' (
        $siblingOtherEvidence.normalized_sha256 -cne $siblingEnabledEvidence.normalized_sha256
    )

    # Real backup representation check: manifest stores a UTF-8 text hash while
    # the escrow and backup file bind UTF-16LE bytes including the BOM.
    $backupRoot = Join-Path $temporaryRoot 'backup'
    New-Item -ItemType Directory -Path $backupRoot | Out-Null
    $backupContext = [pscustomobject]@{ metadata_root = $backupRoot }
    [IO.File]::WriteAllText((Join-Path $backupRoot 'previous-task.xml'), $xmlEnabled, [Text.Encoding]::Unicode)
    $backupManifest = [ordered]@{
        schema = 'order_supervisor_task_backup.v1'
        previous_existed = $true
        xml_sha256 = (Get-CutoverTextSha256 -Text $xmlEnabled)
        created_at = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $backupRoot 'previous-task.json'), $backupManifest, (New-Object Text.UTF8Encoding($false)))
    $escrowXmlHash = Get-CutoverUnicodeTextSha256 -Text $xmlEnabled
    $backupOkay = $true
    try {
        Assert-CutoverBackupMatchesExpectedXml `
            -Context $backupContext `
            -ExpectedUtf8TextSha256 (Get-CutoverTextSha256 -Text $xmlEnabled) `
            -ExpectedUtf16LeBomSha256 $escrowXmlHash
    }
    catch { $backupOkay = $false }
    Assert-True 'backup matches escrow across documented representations' $backupOkay
    Assert-ThrowsCode 'backup refuses a different escrow XML digest' {
        Assert-CutoverBackupMatchesExpectedXml `
            -Context $backupContext `
            -ExpectedUtf8TextSha256 (Get-CutoverTextSha256 -Text $xmlEnabled) `
            -ExpectedUtf16LeBomSha256 ('0' * 64)
    } 'BACKUP_EXPECTED_XML_MISMATCH'
    $duplicateManifest = '{"schema":"order_supervisor_task_backup.v1","previous_existed":true,"xml_sha256":"' +
        (Get-CutoverTextSha256 -Text $xmlEnabled) + '","xml_sha256":"' +
        (Get-CutoverTextSha256 -Text $xmlEnabled) + '"}'
    [IO.File]::WriteAllText((Join-Path $backupRoot 'previous-task.json'), $duplicateManifest, (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'backup manifest rejects duplicate object keys' {
        Assert-CutoverBackupMatchesExpectedXml `
            -Context $backupContext `
            -ExpectedUtf8TextSha256 (Get-CutoverTextSha256 -Text $xmlEnabled) `
            -ExpectedUtf16LeBomSha256 $escrowXmlHash
    } 'BACKUP_MANIFEST_INVALID'

    # Append-only log evidence: same run, one start, one exact terminal record.
    $runId = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    $candidateEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
        (New-TestLogEntry -Event candidate_observed -RunId $runId -WorkId WORK-1 -RowId row-1 -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ mode = 'Observe'; source = 'codex' }))
    )
    $logCandidateOkay = $true
    try { Assert-CutoverLogRun -Entries $candidateEntries -RunId $runId -Status 'candidate_observed' | Out-Null }
    catch { $logCandidateOkay = $false }
    Assert-True 'observe log accepts nonempty intermediate identity' $logCandidateOkay
    $staleEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z'),
        (New-TestLogEntry -Event row_ignored -RunId $runId -WorkId WORK-2 -RowId row-2 -Code STALE_ORDER -Level warning -At '2026-09-07T00:00:02.000Z')
    )
    $logStaleOkay = $true
    try { Assert-CutoverLogRun -Entries $staleEntries -RunId $runId -Status 'stale_order_ignored' | Out-Null }
    catch { $logStaleOkay = $false }
    Assert-True 'observe log accepts exact stale terminal evidence' $logStaleOkay
    $noEligibleEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Execute' })),
        (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
    )
    $logNoEligibleOkay = $true
    try {
        Assert-CutoverLogRun -Entries $noEligibleEntries -RunId $runId -Status no_eligible_order -ExpectedMode Execute | Out-Null
    } catch { $logNoEligibleOkay = $false }
    Assert-True 'log accepts exact no-eligible details contract' $logNoEligibleOkay

    # The live board can legitimately produce both bounded, counts-only
    # diagnostics before the no-eligible terminal record.  They are worker
    # contract events, not terminal outcomes and not arbitrary warnings.
    $liveDiagnosticEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
        (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '2'; exact_duplicate_row_count = '3' })),
        (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ row_count = '1' })),
        (New-TestLogEntry -Event row_ignored -RunId $runId -Level warning -Code source_not_allowlisted -Message 'Row failed deterministic ORDER admission.' -At '2026-09-07T00:00:04.000Z'),
        (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:05.000Z' -Details ([pscustomobject]@{ malformed = '25'; status = 'no_eligible_order' }))
    )
    $liveDiagnosticsOkay = $true
    try {
        Assert-CutoverLogRun `
            -Entries $liveDiagnosticEntries `
            -RunId $runId `
            -Status no_eligible_order `
            -ExpectedMode Observe | Out-Null
    } catch { $liveDiagnosticsOkay = $false }
    Assert-True 'log accepts the exact live duplicate and known-schema diagnostics as nonterminal evidence' $liveDiagnosticsOkay

    foreach ($badDiagnosticCase in @(
        [pscustomobject]@{
            name = 'duplicate diagnostic wrong code'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code WRONG -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic impossible count relation'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '2'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'known-schema diagnostic nonexact row count'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ row_count = '2' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic repeated'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:04.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic missing details'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z'),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic wrong level'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level info -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic nonblank work identity'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -WorkId LEAK -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic nonblank row identity'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -RowId LEAK -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic nonblank message'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -Message LEAK -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic extra detail property'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '1'; extra = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic numeric count'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = 1; exact_duplicate_row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'duplicate diagnostic out-of-bound count'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_duplicate_rows_collapsed -RunId $runId -Level warning -Code BOARD_CURSOR_DUPLICATES_COLLAPSED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ exact_duplicate_group_count = '1'; exact_duplicate_row_count = '10000000000' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'known-schema diagnostic missing details'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -At '2026-09-07T00:00:02.000Z'),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'known-schema diagnostic wrong level and code'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level info -Code WRONG -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'known-schema diagnostic nonblank identity and message'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -WorkId LEAK -RowId LEAK -Message LEAK -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'known-schema diagnostic extra detail property'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ row_count = '1'; extra = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'known-schema diagnostic numeric count'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ row_count = 1 })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        },
        [pscustomobject]@{
            name = 'known-schema diagnostic repeated'
            entries = @(
                (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Observe' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ row_count = '1' })),
                (New-TestLogEntry -Event board_schema_incident -RunId $runId -Level warning -Code BOARD_KNOWN_TRAILING_ROW_IGNORED -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ row_count = '1' })),
                (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:04.000Z' -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
            )
        }
    )) {
        Assert-ThrowsCode ('log rejects ' + $badDiagnosticCase.name) {
            Assert-CutoverLogRun `
                -Entries $badDiagnosticCase.entries `
                -RunId $runId `
                -Status no_eligible_order `
                -ExpectedMode Observe | Out-Null
        } 'LOG_DIAGNOSTIC_EVENT_INVALID'
    }
    $resultEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z' -Details ([pscustomobject]@{ mode = 'Execute' })),
        (New-TestLogEntry -Event result_confirmed -RunId $runId -WorkId CANARY-1 -RowId '11111111-1111-1111-1111-111111111111' -At '2026-09-07T00:00:02.000Z' -Details ([pscustomobject]@{ output_sha256 = ('8' * 64); status = 'completed' }))
    )
    $logResultOkay = $true
    try {
        Assert-CutoverLogRun `
            -Entries $resultEntries `
            -RunId $runId `
            -Status result_confirmed `
            -ExpectedWorkId CANARY-1 `
            -ExpectedRowId '11111111-1111-1111-1111-111111111111' `
            -ExpectedResultStatus completed `
            -ExpectedMode Execute | Out-Null
    } catch { $logResultOkay = $false }
    Assert-True 'log accepts exact result digest/details contract' $logResultOkay
    Assert-ThrowsCode 'log rejects a second causal run id' {
        $extra = @($candidateEntries + (New-TestLogEntry -Event poll_started -RunId ('c' * 32) -At '2026-09-07T00:00:03.000Z'))
        Assert-CutoverLogRun -Entries $extra -RunId $runId -Status 'candidate_observed'
    } 'LOG_RUN_ID_MISMATCH'
    Assert-ThrowsCode 'log explicitly rejects tail seed' {
        $tail = @(
            (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z'),
            (New-TestLogEntry -Event tail_seeded -RunId $runId -At '2026-09-07T00:00:02.000Z')
        )
        Assert-CutoverLogRun -Entries $tail -RunId $runId -Status 'no_eligible_order'
    } 'LOG_FORBIDDEN_TERMINAL_STATUS'
    Assert-ThrowsCode 'log explicitly rejects overlap suppression' {
        $overlap = @(
            (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z'),
            (New-TestLogEntry -Event overlap_suppressed -RunId $runId -At '2026-09-07T00:00:02.000Z')
        )
        Assert-CutoverLogRun -Entries $overlap -RunId $runId -Status 'no_eligible_order'
    } 'LOG_FORBIDDEN_TERMINAL_STATUS'
    Assert-ThrowsCode 'log rejects malformed base fields before causal acceptance' {
        $malformed = @($candidateEntries | ForEach-Object { $_ | Select-Object * })
        $malformed[1].PSObject.Properties.Remove('message')
        Assert-CutoverLogRun -Entries $malformed -RunId $runId -Status candidate_observed
    } 'LOG_DELTA_ENTRY_INVALID'
    Assert-ThrowsCode 'log rejects contradictory terminal records' {
        $contradictory = @(
            (New-TestLogEntry -Event poll_started -RunId $runId -At '2026-09-07T00:00:01.000Z'),
            (New-TestLogEntry -Event candidate_observed -RunId $runId -WorkId WORK-1 -RowId row-1 -At '2026-09-07T00:00:02.000Z'),
            (New-TestLogEntry -Event poll_complete -RunId $runId -At '2026-09-07T00:00:03.000Z' -Details ([pscustomobject]@{ status = 'no_eligible_order' }))
        )
        Assert-CutoverLogRun -Entries $contradictory -RunId $runId -Status no_eligible_order
    } 'LOG_TERMINAL_CARDINALITY_INVALID'
    Assert-ThrowsCode 'log rejects a terminal timestamp outside its run window' {
        Assert-CutoverLogRun `
            -Entries $candidateEntries `
            -RunId $runId `
            -Status candidate_observed `
            -RunWindowStartUtc ([DateTime]'2026-09-07T00:01:00Z') `
            -RunWindowEndUtc ([DateTime]'2026-09-07T00:02:00Z')
    } 'LOG_TIMESTAMP_INVALID'

    # The exceptional gate authenticates one exact, causally current failed
    # Execute run.  state.error alone is deliberately insufficient because it
    # persists after later successful polls.
    $failedRunId = '062af07187da47b29a208dfe4067c573'
    $failedStart = [DateTime]::UtcNow.AddSeconds(-15)
    $failedErrorAt = $failedStart.AddSeconds(4)
    $failedTerminalAt = $failedStart.AddSeconds(5)
    $failedStartText = $failedStart.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $failedErrorText = $failedErrorAt.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $failedTerminalText = $failedTerminalAt.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $failedState = New-TestState `
        -Mode Execute `
        -RunId $failedRunId `
        -Status error `
        -PollAt $failedStartText `
        -ErrorAt $failedErrorText `
        -ErrorCode BOARD_HEADER_INVALID
    $failedEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $failedRunId -At $failedStartText -Details ([pscustomobject]@{ mode = 'Execute' })),
        (New-TestLogEntry -Event run_error -RunId $failedRunId -Code BOARD_HEADER_INVALID -Message board_header_invalid -Level error -At $failedTerminalText -Details (New-TestBoardHeaderDetails))
    )
    $failedLogPath = Join-Path $temporaryRoot 'current-failed-execute.jsonl'
    $failedLogLines = @($failedEntries | ForEach-Object { $_ | ConvertTo-Json -Depth 5 -Compress })
    [IO.File]::WriteAllText($failedLogPath, (($failedLogLines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
    $failedContext = New-TestContext
    $failedContext.log_path = $failedLogPath
    $failedExactStatus = New-TestExactStatus -Last $failedStart -LastTaskResult 20
    $failedEvidence = Assert-CutoverCurrentFailedExecuteRun `
        -Context $failedContext `
        -State $failedState `
        -ExactStatus $failedExactStatus `
        -LogCheckpoint (Get-CutoverLogCheckpoint -Path $failedLogPath) `
        -ExpectedTaskResult 20 `
        -ExpectedFailureCode BOARD_HEADER_INVALID `
        -ExpectedRunId $failedRunId
    Assert-True 'failed Execute gate binds result state log run and Scheduler time' (
        $failedEvidence.run_id -ceq $failedRunId -and
        $failedEvidence.failure_code -ceq 'BOARD_HEADER_INVALID' -and
        $failedEvidence.task_result -eq 20
    )
    $delayedStartupEvidence = Assert-CutoverCurrentFailedExecuteRun `
        -Context $failedContext `
        -State $failedState `
        -ExactStatus (New-TestExactStatus -Last $failedStart.AddSeconds(-15) -LastTaskResult 20) `
        -LogCheckpoint (Get-CutoverLogCheckpoint -Path $failedLogPath) `
        -ExpectedTaskResult 20 `
        -ExpectedFailureCode BOARD_HEADER_INVALID `
        -ExpectedRunId $failedRunId
    Assert-True 'failed Execute gate permits bounded 15-second task startup' (
        $delayedStartupEvidence.run_id -ceq $failedRunId
    )

    Reset-TestMocks
    Set-TestMock 'Get-CutoverTrailingLogRun' {
        param($Path, $Checkpoint, $RunId)
        return @($script:FailedExecuteEntries)
    }
    $script:FailedExecuteEntries = $failedEntries
    Assert-ThrowsCode 'failed Execute gate rejects any result other than 20' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus (New-TestExactStatus -Last $failedStart -LastTaskResult 0) -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_TASK_RESULT_MISMATCH'
    Assert-ThrowsCode 'failed Execute gate rejects a non-Ready task' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus (New-TestExactStatus -Last $failedStart -State Disabled -LastTaskResult 20) -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_TASK_STATE_MISMATCH'
    $wrongStatusState = New-TestState -Mode Execute -RunId $failedRunId -Status no_eligible_order -PollAt $failedStartText
    Assert-ThrowsCode 'failed Execute gate rejects stale error after later success' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $wrongStatusState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_STATE_STATUS_MISMATCH'
    $wrongModeState = New-TestState -Mode Observe -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedErrorText -ErrorCode BOARD_HEADER_INVALID
    Assert-ThrowsCode 'failed Execute gate rejects Observe state' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $wrongModeState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'STATE_LAST_POLL_INVALID'
    $wrongIdentityState = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedErrorText -ErrorCode BOARD_HEADER_INVALID
    $wrongIdentityState.last_poll.identity = 'OTHER\user'
    Assert-ThrowsCode 'failed Execute gate rejects non-SYSTEM identity' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $wrongIdentityState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'STATE_LAST_POLL_INVALID'
    $wrongFailedProfileState = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedErrorText -ErrorCode BOARD_HEADER_INVALID
    $wrongFailedProfileState.last_poll.user_profile = 'C:\Users\other'
    Assert-ThrowsCode 'failed Execute gate rejects wrong SYSTEM profile' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $wrongFailedProfileState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'STATE_LAST_POLL_INVALID'
    Assert-ThrowsCode 'failed Execute gate rejects a stale caller-pinned run id' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId ('f' * 32) | Out-Null
    } 'FAILED_EXECUTE_RUN_ID_MISMATCH'
    $wrongCodeState = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedErrorText -ErrorCode BOARD_READ_HTTP_ERROR
    Assert-ThrowsCode 'failed Execute gate rejects a different state error code' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $wrongCodeState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_ERROR_EVIDENCE_INVALID'
    $wrongMessageState = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedErrorText -ErrorCode BOARD_HEADER_INVALID -ErrorMessage different
    Assert-ThrowsCode 'failed Execute gate rejects a different state error message' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $wrongMessageState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_ERROR_EVIDENCE_INVALID'
    $missingErrorState = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedErrorText -ErrorCode BOARD_HEADER_INVALID
    $missingErrorState.error = $null
    Assert-ThrowsCode 'failed Execute gate rejects missing current error evidence' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $missingErrorState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_ERROR_EVIDENCE_INVALID'
    $selectedErrorState = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedErrorText -ErrorCode BOARD_HEADER_INVALID -WorkId WORK-1 -RowId row-1
    Assert-ThrowsCode 'failed header gate rejects post-selection work identity' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $selectedErrorState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_ERROR_EVIDENCE_INVALID'
    $script:FailedExecuteEntries = @($failedEntries[0], (New-TestLogEntry -Event run_error -RunId $failedRunId -Code BOARD_READ_HTTP_ERROR -Message BOARD_READ_HTTP_ERROR -Level error -At $failedTerminalText))
    Assert-ThrowsCode 'failed Execute gate rejects a different trailing log code' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'LOG_TERMINAL_EVIDENCE_INVALID'
    $script:FailedExecuteEntries = @(
        (New-TestLogEntry -Event poll_started -RunId ('e' * 32) -At $failedStartText -Details ([pscustomobject]@{ mode = 'Execute' })),
        (New-TestLogEntry -Event run_error -RunId ('e' * 32) -Code BOARD_HEADER_INVALID -Message board_header_invalid -Level error -At $failedTerminalText -Details (New-TestBoardHeaderDetails))
    )
    Assert-ThrowsCode 'failed Execute gate rejects a different trailing log run id' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'LOG_RUN_ID_MISMATCH'
    $script:FailedExecuteEntries = @($failedEntries[0], (New-TestLogEntry -Event run_error -RunId $failedRunId -Code BOARD_HEADER_INVALID -Message different -Level error -At $failedTerminalText))
    Assert-ThrowsCode 'failed Execute gate rejects a different trailing log message' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_STATE_LOG_MISMATCH'
    $script:FailedExecuteEntries = @(
        (New-TestLogEntry -Event poll_started -RunId $failedRunId -At $failedStartText -Details ([pscustomobject]@{ mode = 'Observe' })),
        $failedEntries[1]
    )
    Assert-ThrowsCode 'failed Execute gate rejects a non-Execute poll log' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'LOG_POLL_MODE_MISMATCH'
    foreach ($badPollCase in @(
        [pscustomobject]@{ name = 'error-level poll'; property = 'level'; value = 'error' },
        [pscustomobject]@{ name = 'poll work identity'; property = 'work_id'; value = 'WORK-1' },
        [pscustomobject]@{ name = 'poll row identity'; property = 'row_id'; value = 'row-1' },
        [pscustomobject]@{ name = 'poll code'; property = 'code'; value = 'BOARD_HEADER_INVALID' },
        [pscustomobject]@{ name = 'poll message'; property = 'message'; value = 'unexpected' }
    )) {
        $badPoll = New-TestLogEntry -Event poll_started -RunId $failedRunId -At $failedStartText -Details ([pscustomobject]@{ mode = 'Execute' })
        $badPoll.PSObject.Properties[$badPollCase.property].Value = $badPollCase.value
        $script:FailedExecuteEntries = @($badPoll, $failedEntries[1])
        Assert-ThrowsCode ('failed Execute gate rejects ' + $badPollCase.name) {
            Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
        } 'FAILED_EXECUTE_POLL_EVIDENCE_INVALID'
    }
    $liveDetailsOkay = $true
    try { $null = Assert-CutoverBoardHeaderFailureDetails -Details (New-TestBoardHeaderDetails) }
    catch { $liveDetailsOkay = $false }
    Assert-True 'header gate accepts exact live successful-sidecar details' $liveDetailsOkay
    $script:FailedExecuteEntries = @(
        $failedEntries[0],
        (New-TestLogEntry -Event run_error -RunId $failedRunId -Code BOARD_HEADER_INVALID -Message board_header_invalid -Level error -At $failedTerminalText)
    )
    Assert-ThrowsCode 'failed Execute gate rejects missing run-error details' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'BOARD_HEADER_FAILURE_DETAILS_INVALID'
    foreach ($badDetailCase in @(
        [pscustomobject]@{ name = 'numeric attempt type'; property = 'attempt'; value = 1; extra = $false },
        [pscustomobject]@{ name = 'retry attempt'; property = 'attempt'; value = '2'; extra = $false },
        [pscustomobject]@{ name = 'failed transport'; property = 'transport_exit'; value = '1'; extra = $false },
        [pscustomobject]@{ name = 'nonexact successful HTTP'; property = 'http_status'; value = '204'; extra = $false },
        [pscustomobject]@{ name = 'non-JSON content'; property = 'content_type_class'; value = 'html'; extra = $false },
        [pscustomobject]@{ name = 'zero elapsed time'; property = 'elapsed_ms'; value = '0'; extra = $false },
        [pscustomobject]@{ name = 'comma decimal elapsed time'; property = 'elapsed_ms'; value = '3874,97'; extra = $false },
        [pscustomobject]@{ name = 'noncanonical elapsed precision'; property = 'elapsed_ms'; value = '3874.970'; extra = $false },
        [pscustomobject]@{ name = 'unknown property'; property = 'unexpected'; value = 'x'; extra = $true }
    )) {
        $badDetails = New-TestBoardHeaderDetails
        if ($badDetailCase.extra) {
            $badDetails | Add-Member -NotePropertyName $badDetailCase.property -NotePropertyValue $badDetailCase.value
        } else {
            $badDetails.PSObject.Properties[$badDetailCase.property].Value = $badDetailCase.value
        }
        $script:FailedExecuteEntries = @(
            $failedEntries[0],
            (New-TestLogEntry -Event run_error -RunId $failedRunId -Code BOARD_HEADER_INVALID -Message board_header_invalid -Level error -At $failedTerminalText -Details $badDetails)
        )
        Assert-ThrowsCode ('failed Execute gate rejects detail ' + $badDetailCase.name) {
            Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
        } 'BOARD_HEADER_FAILURE_DETAILS_INVALID'
    }
    $script:FailedExecuteEntries = @($failedEntries + (New-TestLogEntry -Event run_error -RunId $failedRunId -Code BOARD_HEADER_INVALID -Message board_header_invalid -Level error -At $failedTerminalText -Details (New-TestBoardHeaderDetails)))
    Assert-ThrowsCode 'failed Execute gate rejects any extra current-run record' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_LOG_SHAPE_INVALID'
    $script:FailedExecuteEntries = $failedEntries
    Assert-ThrowsCode 'failed Execute gate rejects excessive task-startup delay' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus (New-TestExactStatus -Last $failedStart.AddSeconds(-31) -LastTaskResult 20) -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_TASK_TIME_MISMATCH'
    Assert-ThrowsCode 'failed Execute gate rejects poll before Scheduler start tolerance' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $failedState -ExactStatus (New-TestExactStatus -Last $failedStart.AddSeconds(3) -LastTaskResult 20) -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'LOG_TIMESTAMP_INVALID'
    $lateErrorState = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedTerminalAt.AddSeconds(10).ToString('yyyy-MM-ddTHH:mm:ss.fffZ') -ErrorCode BOARD_HEADER_INVALID
    Assert-ThrowsCode 'failed Execute gate rejects noncausal state error time' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $lateErrorState -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_STATE_LOG_TIME_MISMATCH'
    $staleStateError = New-TestState -Mode Execute -RunId $failedRunId -Status error -PollAt $failedStartText -ErrorAt $failedStart.AddSeconds(1).ToString('yyyy-MM-ddTHH:mm:ss.fffZ') -ErrorCode BOARD_HEADER_INVALID
    Assert-ThrowsCode 'failed Execute gate rejects terminal error delayed from state error' {
        Assert-CutoverCurrentFailedExecuteRun -Context $failedContext -State $staleStateError -ExactStatus $failedExactStatus -LogCheckpoint ([pscustomobject]@{ length = 1 }) -ExpectedTaskResult 20 -ExpectedFailureCode BOARD_HEADER_INVALID -ExpectedRunId $failedRunId | Out-Null
    } 'FAILED_EXECUTE_STATE_LOG_TIME_MISMATCH'
    Reset-TestMocks

    $resultRow = '11111111-1111-1111-1111-111111111111'
    $resultState = New-TestState -Mode Execute -RunId $runId -Status result_confirmed -WorkId 'CANARY-1' -RowId $resultRow -ResultStatus completed
    $stateResultOkay = $true
    try {
        Assert-CutoverStateTerminal -State $resultState -ExpectedMode Execute -ExpectedStatus result_confirmed -ExpectedWorkId 'CANARY-1' -ExpectedRowId $resultRow -ExpectedResultStatus completed | Out-Null
    } catch { $stateResultOkay = $false }
    Assert-True 'state binds exact Execute result identity and status' $stateResultOkay
    Assert-ThrowsCode 'state rejects wrong result identity' {
        Assert-CutoverStateTerminal -State $resultState -ExpectedMode Execute -ExpectedStatus result_confirmed -ExpectedWorkId 'OTHER' -ExpectedRowId $resultRow -ExpectedResultStatus completed | Out-Null
    } 'STATE_RESULT_IDENTITY_MISMATCH'
    $noEligibleState = New-TestState -Mode Execute -RunId $runId -Status no_eligible_order
    $noEligibleOkay = $true
    try { Assert-CutoverStateTerminal -State $noEligibleState -ExpectedMode Execute -ExpectedStatus no_eligible_order | Out-Null }
    catch { $noEligibleOkay = $false }
    Assert-True 'state accepts exact fresh no-eligible evidence' $noEligibleOkay
    $beforeTransition = New-TestState -Mode Observe -RunId ('a' * 32) -Status no_eligible_order -PollAt '2026-09-07T00:00:00.000Z'
    $afterTransition = New-TestState -Mode Observe -RunId ('b' * 32) -Status no_eligible_order -PollAt '2026-09-07T00:00:02.000Z'
    $afterTransition.counts.polls = 2
    $transitionOkay = $true
    $transitionError = ''
    try {
        Assert-CutoverStateTransition `
            -Before $beforeTransition `
            -After $afterTransition `
            -Status no_eligible_order `
            -SchedulerLastRunUtc ([DateTime]'2026-09-07T00:00:01Z') `
            -RunWindowEndUtc ([DateTime]'2026-09-07T00:00:03Z') `
            -PollStartedAtUtc ([DateTime]'2026-09-07T00:00:01Z')
    } catch { $transitionOkay = $false; $transitionError = [string]$_.Exception.Message }
    Assert-True 'state transition binds a fresh poll timestamp to scheduler run' $transitionOkay $transitionError
    $afterTransition.last_poll.at = $beforeTransition.last_poll.at
    Assert-ThrowsCode 'state transition rejects stale last-poll timestamp' {
        Assert-CutoverStateTransition -Before $beforeTransition -After $afterTransition -Status no_eligible_order
    } 'STATE_LAST_POLL_TIME_NOT_ADVANCED'
    $futureTransition = New-TestState -Mode Observe -RunId ('c' * 32) -Status no_eligible_order -PollAt '2026-09-07T01:00:00.000Z'
    $futureTransition.counts.polls = 2
    Assert-ThrowsCode 'state transition rejects a future same-run poll timestamp' {
        Assert-CutoverStateTransition `
            -Before $beforeTransition `
            -After $futureTransition `
            -Status no_eligible_order `
            -SchedulerLastRunUtc ([DateTime]'2026-09-07T00:00:01Z') `
            -RunWindowEndUtc ([DateTime]'2026-09-07T00:00:03Z') `
            -PollStartedAtUtc ([DateTime]'2026-09-07T00:00:01Z')
    } 'STATE_LAST_POLL_TIME_INVALID'
    $wrongProfileState = New-TestState -Mode Execute -RunId $runId -Status no_eligible_order
    $wrongProfileState.last_poll.user_profile = 'C:\Users\other'
    Assert-ThrowsCode 'state terminal rejects wrong SYSTEM profile' {
        Assert-CutoverStateTerminal `
            -State $wrongProfileState `
            -ExpectedMode Execute `
            -ExpectedStatus no_eligible_order `
            -ExpectedUserProfile 'C:\Users\akatiawam' | Out-Null
    } 'STATE_LAST_POLL_INVALID'
    $badCountState = New-TestState -Mode Execute -RunId $runId -Status no_eligible_order
    $badCountState.counts.polls = -1
    Assert-ThrowsCode 'state terminal rejects negative counters' {
        Assert-CutoverStateTerminal -State $badCountState -ExpectedMode Execute -ExpectedStatus no_eligible_order | Out-Null
    } 'STATE_COUNTS_INVALID'
    $badCursorState = New-TestState -Mode Execute -RunId $runId -Status no_eligible_order
    $badCursorState.cursor.timestamp = '2026-09-07T00:00:00.000Z'
    Assert-ThrowsCode 'state terminal rejects non-canonical cursor timestamp' {
        Assert-CutoverStateTerminal -State $badCursorState -ExpectedMode Execute -ExpectedStatus no_eligible_order | Out-Null
    } 'STATE_CURSOR_INVALID'
    $nullWorkState = New-TestState -Mode Execute -RunId $runId -Status no_eligible_order
    $nullWorkState.work = $null
    Assert-ThrowsCode 'state contract rejects null work instead of an array' {
        Assert-CutoverStateTerminal -State $nullWorkState -ExpectedMode Execute -ExpectedStatus no_eligible_order | Out-Null
    } 'STATE_WORK_INVALID'
    $scalarWorkState = New-TestState -Mode Execute -RunId $runId -Status no_eligible_order
    $scalarWorkState.work = [pscustomobject]@{
        input_row_id = $resultRow; work_id = 'CANARY-1'; status = 'result_confirmed'
        result_status = 'completed'; output_sha256 = ('8' * 64); updated_at = '2026-09-07T00:00:00.000Z'
    }
    Assert-ThrowsCode 'state contract rejects scalar work instead of an array' {
        Assert-CutoverStateTerminal -State $scalarWorkState -ExpectedMode Execute -ExpectedStatus no_eligible_order | Out-Null
    } 'STATE_WORK_INVALID'

    $realLogPath = Join-Path $temporaryRoot 'append-only.jsonl'
    $priorLogLine = ([pscustomobject]@{ event = 'prior'; run_id = ('a' * 32) } | ConvertTo-Json -Compress) + "`r`n"
    [IO.File]::WriteAllText($realLogPath, $priorLogLine, (New-Object Text.UTF8Encoding($false)))
    $realLogBefore = Get-CutoverLogCheckpoint -Path $realLogPath
    $newLogText = @(
        ([pscustomobject]@{ event = 'poll_started'; run_id = ('b' * 32) } | ConvertTo-Json -Compress),
        ([pscustomobject]@{ event = 'poll_complete'; run_id = ('b' * 32); details = [pscustomobject]@{ status = 'no_eligible_order' } } | ConvertTo-Json -Compress)
    ) -join "`r`n"
    [IO.File]::AppendAllText($realLogPath, ($newLogText + "`r`n"), (New-Object Text.UTF8Encoding($false)))
    $realDelta = Get-CutoverLogDelta -Path $realLogPath -Before $realLogBefore
    Assert-True 'real log delta preserves prior-byte prefix' ($realDelta.entries.Count -eq 2 -and $realDelta.appended_bytes -gt 0)

    $unterminatedLogPath = Join-Path $temporaryRoot 'unterminated-prefix.jsonl'
    [IO.File]::WriteAllText($unterminatedLogPath, $priorLogLine.TrimEnd("`r", "`n"), (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'log checkpoint rejects an unterminated prefix record' {
        Get-CutoverLogCheckpoint -Path $unterminatedLogPath | Out-Null
    } 'LOG_PREFIX_BOUNDARY_INVALID'

    $tamperedLogPath = Join-Path $temporaryRoot 'tampered-prefix.jsonl'
    [IO.File]::WriteAllText($tamperedLogPath, $priorLogLine, (New-Object Text.UTF8Encoding($false)))
    $tamperedBefore = Get-CutoverLogCheckpoint -Path $tamperedLogPath
    [byte[]]$tamperedBytes = [IO.File]::ReadAllBytes($tamperedLogPath)
    $tamperedBytes[0] = [byte][char]'X'
    [IO.File]::WriteAllBytes($tamperedLogPath, $tamperedBytes)
    [IO.File]::AppendAllText($tamperedLogPath, ($newLogText + "`r`n"), (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'real log delta rejects changed historical prefix' {
        Get-CutoverLogDelta -Path $tamperedLogPath -Before $tamperedBefore | Out-Null
    } 'LOG_PREFIX_CHANGED'

    $duplicateLogPath = Join-Path $temporaryRoot 'duplicate-key.jsonl'
    [IO.File]::WriteAllText($duplicateLogPath, $priorLogLine, (New-Object Text.UTF8Encoding($false)))
    $duplicateLogBefore = Get-CutoverLogCheckpoint -Path $duplicateLogPath
    $duplicateLogSuffix = '{"event":"poll_started","event":"poll_complete","run_id":"' + ('b' * 32) + '"}' + "`r`n" +
        '{"event":"poll_complete","run_id":"' + ('b' * 32) + '","details":{"status":"no_eligible_order"}}' + "`r`n"
    [IO.File]::AppendAllText($duplicateLogPath, $duplicateLogSuffix, (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'real log delta rejects duplicate JSON keys' {
        Get-CutoverLogDelta -Path $duplicateLogPath -Before $duplicateLogBefore | Out-Null
    } 'LOG_DELTA_JSON_INVALID'

    $currentRunLogPath = Join-Path $temporaryRoot 'current-observe-run.jsonl'
    $currentRunId = 'dddddddddddddddddddddddddddddddd'
    $laterOverlapRunId = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
    $currentRunStart = [DateTime]::UtcNow.AddSeconds(-10)
    $currentRunEnd = $currentRunStart.AddSeconds(1)
    $currentRunStartText = $currentRunStart.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $currentRunEndText = $currentRunEnd.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $currentRunLines = @(
        (New-TestLogEntry -Event poll_started -RunId $currentRunId -At $currentRunStartText -Details ([pscustomobject]@{ mode = 'Observe' })),
        (New-TestLogEntry -Event poll_complete -RunId $currentRunId -At $currentRunEndText -Details ([pscustomobject]@{ malformed = '0'; status = 'no_eligible_order' }))
    ) | ForEach-Object { $_ | ConvertTo-Json -Depth 5 -Compress }
    [IO.File]::WriteAllText($currentRunLogPath, (($currentRunLines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
    $currentRunState = New-TestState -Mode Observe -RunId $currentRunId -Status no_eligible_order -PollAt $currentRunStartText
    $currentRunContext = New-TestContext
    $currentRunContext.log_path = $currentRunLogPath
    $currentRunStatus = New-TestExactStatus -Last $currentRunStart
    $currentRunCheckpoint = Get-CutoverLogCheckpoint -Path $currentRunLogPath
    $currentRunEvidence = Assert-CutoverCurrentTerminalRun `
        -Context $currentRunContext `
        -State $currentRunState `
        -ExactStatus $currentRunStatus `
        -LogCheckpoint $currentRunCheckpoint `
        -ExpectedMode Observe `
        -ExpectedStatus no_eligible_order
    Assert-True 'current Observe terminal gate binds state task time and trailing log run' (
        $currentRunEvidence.run_id -ceq $currentRunId
    )
    $overlapAt = $currentRunStart.AddSeconds(5)
    $overlapLine = New-TestLogEntry -Event overlap_suppressed -RunId $laterOverlapRunId -At $overlapAt.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') -Code LOCAL_MUTEX_BUSY -Level warning
    [IO.File]::AppendAllText($currentRunLogPath, (($overlapLine | ConvertTo-Json -Depth 5 -Compress) + "`n"), (New-Object Text.UTF8Encoding($false)))
    $afterOverlapCheckpoint = Get-CutoverLogCheckpoint -Path $currentRunLogPath
    $afterOverlapStatus = New-TestExactStatus -Last $overlapAt
    Assert-ThrowsCode 'current Observe terminal gate rejects stale no-eligible state after later overlap' {
        Assert-CutoverCurrentTerminalRun `
            -Context $currentRunContext `
            -State $currentRunState `
            -ExactStatus $afterOverlapStatus `
            -LogCheckpoint $afterOverlapCheckpoint `
            -ExpectedMode Observe `
            -ExpectedStatus no_eligible_order | Out-Null
    } 'LOG_CURRENT_RUN_ID_MISMATCH'

    $duplicateStatePath = Join-Path $temporaryRoot 'duplicate-state.json'
    $duplicateState = '{"schema":"order_supervisor_state.v1","schema":"order_supervisor_state.v1",' +
        '"source_tag":"vm-order-worker","mode":"Observe","initialized":true,' +
        '"cursor":{"timestamp":"","row_id":""},"counts":{"polls":0},"last_poll":null}'
    [IO.File]::WriteAllText($duplicateStatePath, $duplicateState, (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'state reader rejects duplicate JSON keys' {
        Get-CutoverState -Path $duplicateStatePath | Out-Null
    } 'STATE_INVALID'
    $checkpointRacePath = Join-Path $temporaryRoot 'checkpoint-race.json'
    [IO.File]::WriteAllText(
        $checkpointRacePath,
        (New-TestState | ConvertTo-Json -Depth 8 -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
    $checkpointRaceLength = (Get-Item -LiteralPath $checkpointRacePath).Length
    Set-TestMock 'Get-CutoverFileCheckpoint' {
        param($Path, $MaximumBytes, $MissingCode, $InvalidCode)
        [pscustomobject]@{ length = $script:CheckpointRaceLength; sha256 = ('0' * 64) }
    }
    $script:CheckpointRaceLength = $checkpointRaceLength
    Assert-ThrowsCode 'state reader rejects checkpoint/content hash split' {
        Get-CutoverState -Path $checkpointRacePath | Out-Null
    } 'STATE_INVALID'
    Reset-TestMocks

    # ValidateEscrowAndDisable: exact enabled escrow XML, Ready-only preflight,
    # Disabled readback, definition-only Enabled delta, and no state/log change.
    Reset-TestMocks
    $context = New-TestContext
    $context.mode = 'Execute'
    $script:XmlCall = 0
    $script:DisableCalled = 0
    $xmlHash = Get-CutoverUnicodeTextSha256 -Text $xmlEnabled
    Set-TestMock 'Invoke-CutoverEscrow' { param($Context, $RequestedAction) New-TestEscrowReceipt -Action Validate -XmlSha256 $script:TestXmlHash }
    $script:TestXmlHash = $xmlHash
    Set-TestMock 'Get-CutoverExactInstallerStatus' { param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState) New-TestExactStatus }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{ value = (New-TestState -Mode Execute); checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
    }
    Set-TestMock 'Get-CutoverFileCheckpoint' { param($Path, $MaximumBytes, $MissingCode, $InvalidCode) [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
    Set-TestMock 'Get-CutoverLogCheckpoint' { param($Path) [pscustomobject]@{ length = 20; sha256 = ('e' * 64) } }
    Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' { param($Before, $Path, $MaximumBytes, $Code) }
    Set-TestMock 'Export-CutoverTaskXml' { $script:XmlCall++; if ($script:XmlCall -eq 1) { $script:XmlEnabled } else { $script:XmlDisabled } }
    $script:XmlEnabled = $xmlEnabled
    $script:XmlDisabled = $xmlDisabled
    Set-TestMock 'Disable-CutoverTask' { $script:DisableCalled++ }
    $disableReceipt = Invoke-CutoverValidateEscrowAndDisable -Context $context
    Assert-True 'ValidateEscrowAndDisable returns exact success status' ($disableReceipt.ok -and $disableReceipt.status -ceq 'TASK_DISABLED')
    Assert-True 'ValidateEscrowAndDisable invokes disable exactly once' ($script:DisableCalled -eq 1)
    Assert-True 'ValidateEscrowAndDisable proves definition delta only' ($disableReceipt.definition_preserved_except_enabled -and $disableReceipt.escrow_xml_sha256 -ceq $xmlHash)

    Reset-TestMocks
    $context = New-TestContext
    $context.mode = 'Execute'
    $script:OrdinaryResultTwentyInstallCalls = 0
    $script:OrdinaryResultTwentyDisableCalls = 0
    $script:OrdinaryResultTwentyDrainCalls = 0
    $script:OrdinaryResultTwentyCleanupCalls = 0
    $script:OrdinaryResultTwentyStartCalls = 0
    Set-TestMock 'Invoke-CutoverEscrow' {
        param($Context, $RequestedAction)
        New-TestEscrowReceipt -Action Validate -XmlSha256 $script:TestXmlHash
    }
    Set-TestMock 'Invoke-CutoverInstaller' {
        param($Context, $ScriptPath, $RequestedAction, $RequestedMode)
        if ($RequestedAction -ceq 'Install') { $script:OrdinaryResultTwentyInstallCalls++ }
        New-TestInstallerStatusReceipt -Context $Context -Mode Execute -State Ready -LastTaskResult 20
    }
    Set-TestMock 'Disable-CutoverTask' { $script:OrdinaryResultTwentyDisableCalls++ }
    Set-TestMock 'Invoke-CutoverDrainObserve' { $script:OrdinaryResultTwentyDrainCalls++ }
    Set-TestMock 'Disable-CutoverTaskAfterFailure' { $script:OrdinaryResultTwentyCleanupCalls++ }
    Set-TestMock 'Start-CutoverTask' { $script:OrdinaryResultTwentyStartCalls++ }
    Assert-ThrowsCode 'ordinary ValidateEscrowAndDisable action rejects result 20' {
        Invoke-CutoverValidateEscrowAndDisable -Context $context | Out-Null
    } 'INSTALLER_STATUS_MISMATCH'
    Assert-True 'ordinary result-20 rejection cannot reach any mutation or cleanup path' (
        $script:OrdinaryResultTwentyDisableCalls -eq 0 -and
        $script:OrdinaryResultTwentyInstallCalls -eq 0 -and
        $script:OrdinaryResultTwentyDrainCalls -eq 0 -and
        $script:OrdinaryResultTwentyCleanupCalls -eq 0 -and
        $script:OrdinaryResultTwentyStartCalls -eq 0
    )

    # RestoreReady accepts either pinned candidate mode in Ready or Disabled.
    # Ready is disabled after a stable reread; Disabled is reread without a
    # redundant mutation.  Both restore only the prevalidated old Execute XML.
    foreach ($candidateMode in @('Observe', 'Execute')) {
        foreach ($candidateState in @('Ready', 'Disabled')) {
            Reset-TestMocks
            $context = New-TestContext
            $context.mode = $candidateMode
            $script:RestoreCandidateState = $candidateState
            $script:RestoreCandidateLastResult = $(if ($candidateState -ceq 'Disabled') { 23 } else { 0 })
            $script:EscrowCalls = 0
            $script:TestXmlHash = $xmlHash
            Set-TestMock 'Invoke-CutoverEscrow' {
                param($Context, $RequestedAction)
                $script:EscrowCalls++
                New-TestEscrowReceipt -Action $RequestedAction -XmlSha256 $script:TestXmlHash -TaskStopped:$false
            }
            Set-TestMock 'Get-CutoverExactInstallerStatusAnyState' {
                param($Context, $ScriptPath, $ExpectedMode, $AllowedStates, $RequireResultZero)
                New-TestExactStatus -State $script:RestoreCandidateState -LastTaskResult $script:RestoreCandidateLastResult
            }
            Set-TestMock 'Get-CutoverExactInstallerStatus' {
                param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState, $RequireResultZero)
                New-TestExactStatus `
                    -State $ExpectedTaskState `
                    -LastTaskResult $(if ($ExpectedTaskState -ceq 'Disabled') { $script:RestoreCandidateLastResult } else { 0 })
            }
            Set-TestMock 'Assert-CutoverTriggerWindow' { param($NextRunUtc, $RequiredSeconds) }
            $script:RestoreStateValidated = 0
            Set-TestMock 'Get-CutoverState' {
                param($Path)
                $script:RestoreStateValidated++
                [pscustomobject]@{ value = (New-TestState -Mode Execute); checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
            }
            Set-TestMock 'Get-CutoverFileCheckpoint' { param($Path, $MaximumBytes, $MissingCode, $InvalidCode) [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
            Set-TestMock 'Get-CutoverLogCheckpoint' { param($Path) [pscustomobject]@{ length = 20; sha256 = ('e' * 64) } }
            Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' { param($Before, $Path, $MaximumBytes, $Code) }
            $script:XmlCall = 0
            Set-TestMock 'Export-CutoverTaskXml' {
                $script:XmlCall++
                if ($script:RestoreCandidateState -ceq 'Ready') {
                    if ($script:XmlCall -eq 2) { $script:XmlDisabled } else { $script:XmlEnabled }
                } else {
                    if ($script:XmlCall -le 2) { $script:XmlDisabled } else { $script:XmlEnabled }
                }
            }
            $script:DisableCalled = 0
            Set-TestMock 'Disable-CutoverTask' { $script:DisableCalled++ }
            $restoreReceipt = Invoke-CutoverRestoreReady -Context $context
            $expectedDisableCalls = if ($candidateState -ceq 'Ready') { 1 } else { 0 }
            Assert-True ("RestoreReady supports $candidateMode $candidateState") (
                $script:EscrowCalls -eq 2 -and
                $script:DisableCalled -eq $expectedDisableCalls -and
                $restoreReceipt.status -ceq 'RESTORED_READY' -and
                $restoreReceipt.candidate_mode -ceq $candidateMode -and
                $restoreReceipt.pre_task_state -ceq $candidateState -and
                $restoreReceipt.candidate_last_task_result -eq $script:RestoreCandidateLastResult -and
                $script:RestoreStateValidated -eq 1 -and
                [bool]$restoreReceipt.candidate_disable_performed -eq ($candidateState -ceq 'Ready') -and
                -not $restoreReceipt.task_stopped -and $restoreReceipt.state_and_log_preserved
            )
        }
    }

    Reset-TestMocks
    $context = New-TestContext
    $script:EscrowCalls = 0
    $script:FailureCleanupCalls = 0
    Set-TestMock 'Invoke-CutoverEscrow' {
        param($Context, $RequestedAction)
        $script:EscrowCalls++
        New-TestEscrowReceipt -Action $RequestedAction -XmlSha256 $script:TestXmlHash
    }
    Set-TestMock 'Get-CutoverExactInstallerStatusAnyState' {
        param($Context, $ScriptPath, $ExpectedMode, $AllowedStates, $RequireResultZero)
        New-TestExactStatus -State Ready
    }
    Set-TestMock 'Get-CutoverState' { param($Path) Throw-Cutover -Code 'STATE_INVALID' }
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $script:FailureCleanupCalls++
        [pscustomobject]@{ mode = 'Observe'; future_triggers_disabled = $true; definition_preserved_except_enabled = $true; task_stopped = $false }
    }
    Assert-ThrowsCode 'RestoreReady rejects malformed current state before Restore' {
        Invoke-CutoverRestoreReady -Context $context | Out-Null
    } 'STATE_INVALID'
    Assert-True 'malformed current state never invokes Restore and quarantines' (
        $script:EscrowCalls -eq 1 -and $script:FailureCleanupCalls -eq 1
    )

    Reset-TestMocks
    $context = New-TestContext
    $script:TestXmlHash = $xmlHash
    Set-TestMock 'Invoke-CutoverEscrow' {
        param($Context, $RequestedAction)
        New-TestEscrowReceipt -Action $RequestedAction -XmlSha256 $script:TestXmlHash -TaskStopped:($RequestedAction -ceq 'Restore')
    }
    Set-TestMock 'Get-CutoverExactInstallerStatusAnyState' { param($Context, $ScriptPath, $ExpectedMode, $AllowedStates, $RequireResultZero) New-TestExactStatus -State Ready }
    Set-TestMock 'Get-CutoverExactInstallerStatus' { param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState, $RequireResultZero) New-TestExactStatus -State $ExpectedTaskState }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{ value = (New-TestState -Mode Execute); checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
    }
    Set-TestMock 'Get-CutoverFileCheckpoint' { param($Path, $MaximumBytes, $MissingCode, $InvalidCode) [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
    Set-TestMock 'Get-CutoverLogCheckpoint' { param($Path) [pscustomobject]@{ length = 20; sha256 = ('e' * 64) } }
    Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' { param($Before, $Path, $MaximumBytes, $Code) }
    $script:XmlCall = 0
    Set-TestMock 'Export-CutoverTaskXml' { $script:XmlCall++; if ($script:XmlCall -eq 2) { $script:XmlDisabled } else { $script:XmlEnabled } }
    Set-TestMock 'Disable-CutoverTask' { }
    $script:FailureCleanupCalls = 0
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $script:FailureCleanupCalls++
        [pscustomobject]@{ mode = 'Execute'; future_triggers_disabled = $true; definition_preserved_except_enabled = $true; task_stopped = $false }
    }
    Assert-ThrowsCode 'RestoreReady rejects escrow emergency-stop semantics and quarantines' {
        Invoke-CutoverRestoreReady -Context $context | Out-Null
    } 'RESTORE_STOPPED_ACTIVE_TASK'
    Assert-True 'failed restored task is left disabled' ($script:FailureCleanupCalls -eq 1)

    Reset-TestMocks
    $context = New-TestContext
    $script:EscrowCalls = 0
    Set-TestMock 'Invoke-CutoverEscrow' { param($Context, $RequestedAction) $script:EscrowCalls++; New-TestEscrowReceipt -Action $RequestedAction -XmlSha256 $script:TestXmlHash }
    Set-TestMock 'Get-CutoverExactInstallerStatusAnyState' { param($Context, $ScriptPath, $ExpectedMode, $AllowedStates, $RequireResultZero) New-TestExactStatus -State Running }
    $script:FailureCleanupCalls = 0
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $script:FailureCleanupCalls++
        [pscustomobject]@{ mode = 'Observe'; future_triggers_disabled = $true; definition_preserved_except_enabled = $true; task_stopped = $false }
    }
    Assert-ThrowsCode 'RestoreReady quarantines an exact active candidate without stopping it' {
        Invoke-CutoverRestoreReady -Context $context | Out-Null
    } 'RESTORE_CANDIDATE_ACTIVE_QUARANTINED'
    Assert-True 'active candidate recovery never invokes Restore' ($script:EscrowCalls -eq 1 -and $script:FailureCleanupCalls -eq 1)

    Reset-TestMocks
    $context = New-TestContext
    $script:UnexpectedDisableCalls = 0
    Set-TestMock 'Invoke-CutoverEscrow' { param($Context, $RequestedAction) New-TestEscrowReceipt -Action $RequestedAction -XmlSha256 $script:TestXmlHash }
    Set-TestMock 'Get-CutoverExactInstallerStatusAnyState' { param($Context, $ScriptPath, $ExpectedMode, $AllowedStates, $RequireResultZero) Throw-Cutover -Code 'INSTALLER_STATUS_MISMATCH' }
    Set-TestMock 'Disable-CutoverTask' { $script:UnexpectedDisableCalls++ }
    Assert-ThrowsCode 'RestoreReady rejects an unauthenticated unexpected task state' {
        Invoke-CutoverRestoreReady -Context $context | Out-Null
    } 'INSTALLER_STATUS_MISMATCH'
    Assert-True 'unknown task authority is never mutated' ($script:UnexpectedDisableCalls -eq 0)

    # Observe drain loops in one resource and accepts only stale intermediates
    # until a causally fresh no-eligible terminal state.  A candidate requires
    # external resolution and quarantines the task after the first such run.
    Reset-TestMocks
    $context = New-TestContext
    Set-TestMock 'Get-CutoverExactInstallerStatus' { param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState) New-TestExactStatus }
    Set-TestMock 'Assert-CutoverTriggerWindow' { param($NextRunUtc, $RequiredSeconds) }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{ value = (New-TestState -Mode Execute -RunId ('a' * 32)); checkpoint = [pscustomobject]@{ length = 1; sha256 = ('a' * 64) } }
    }
    Set-TestMock 'Get-CutoverLogCheckpoint' { param($Path) [pscustomobject]@{ length = 1; sha256 = ('a' * 64) } }
    $script:DrainCleanupCalls = 0
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $script:DrainCleanupCalls++
        [pscustomobject]@{ mode = 'Observe'; future_triggers_disabled = $true; definition_preserved_except_enabled = $true; task_stopped = $false }
    }
    $script:RunSequence = @('stale_order_ignored', 'no_eligible_order')
    $script:RunSequenceIndex = 0
    Set-TestMock 'Invoke-CutoverOneRun' {
        param($Context, $Installer, $ExpectedMode, $DeadlineUtc, $PreviousLastRunUtc, $PreviousRunId, $LogBefore, $ExpectedStatus, $ExpectedWorkId, $ExpectedRowId, $ExpectedResultStatus)
        $status = $script:RunSequence[$script:RunSequenceIndex]
        $script:RunSequenceIndex++
        [pscustomobject]@{
            status = $status
            run_id = ([char](98 + $script:RunSequenceIndex)).ToString() * 32
            last_run_utc = $PreviousLastRunUtc.AddSeconds(2)
            log_checkpoint = [pscustomobject]@{ length = $LogBefore.length + 10; sha256 = ('f' * 64) }
            log_appended_bytes = 10
        }
    }
    $drain = Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path
    Assert-True 'DrainObserve transitions Execute baseline to Observe no-eligible' ($drain.status -ceq 'OBSERVE_DRAINED' -and $drain.runs -eq 2 -and $drain.final_worker_status -ceq 'no_eligible_order')
    Assert-True 'DrainObserve accumulates bounded append evidence' ($drain.log_appended_bytes -eq 20)

    $admission = [pscustomobject]@{run_id=('a'*32);last_run_utc=(New-TestExactStatus).last_run_utc;state_checkpoint=[pscustomobject]@{length=1;sha256=('a'*64)};log_checkpoint=[pscustomobject]@{length=1;sha256=('a'*64)}}
    $script:RunSequenceIndex=0
    $boundDrain=Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path -AdmittedBaseline $admission
    Assert-True 'real Observe drain accepts the exact admitted baseline and still drains stale work' ($boundDrain.runs -eq 2 -and $boundDrain.final_worker_status -ceq 'no_eligible_order')
    $savedDrainStateMock=(Get-Item Function:Get-CutoverState).ScriptBlock
    $savedDrainStatusMock=(Get-Item Function:Get-CutoverExactInstallerStatus).ScriptBlock
    $savedDrainLogMock=(Get-Item Function:Get-CutoverLogCheckpoint).ScriptBlock
    $savedDrainCleanupCalls=$script:DrainCleanupCalls
    foreach($admissionDrift in @('run_id','last_run','state','log')){
        $script:RunSequenceIndex=0;$script:AdmissionDrift=$admissionDrift
        Set-TestMock 'Get-CutoverState' {param($Path)
            $run=if($script:AdmissionDrift -ceq 'run_id'){('b'*32)}else{('a'*32)}
            [pscustomobject]@{value=(New-TestState -Mode Execute -RunId $run);checkpoint=[pscustomobject]@{length=1;sha256=$(if($script:AdmissionDrift -ceq 'state'){('b'*64)}else{('a'*64)})}}
        }
        Set-TestMock 'Get-CutoverExactInstallerStatus' {param($Context,$ScriptPath,$ExpectedMode,$ExpectedTaskState,$AllowInheritedTaskResult)
            if($script:AdmissionDrift -ceq 'last_run'){New-TestExactStatus -Last (([DateTime]'2026-09-07T00:00:00Z').ToUniversalTime().AddSeconds(1))}else{New-TestExactStatus}
        }
        Set-TestMock 'Get-CutoverLogCheckpoint' {param($Path) [pscustomobject]@{length=1;sha256=$(if($script:AdmissionDrift -ceq 'log'){('b'*64)}else{('a'*64)})}}
        $expectedAdmissionCode=switch($admissionDrift){'state'{'OBSERVE_DRAIN_ADMITTED_STATE_CHANGED'};'log'{'OBSERVE_DRAIN_ADMITTED_LOG_CHANGED'};default{'OBSERVE_DRAIN_ADMITTED_RUN_CHANGED'}}
        Assert-ThrowsCode ('real Observe drain rejects intervening admitted '+$admissionDrift) {Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path -AdmittedBaseline $admission} $expectedAdmissionCode
        Assert-True ('admitted '+$admissionDrift+' drift never starts an Observe run') ($script:RunSequenceIndex -eq 0)
    }
    Set-TestMock 'Get-CutoverState' $savedDrainStateMock
    Set-TestMock 'Get-CutoverExactInstallerStatus' $savedDrainStatusMock
    Set-TestMock 'Get-CutoverLogCheckpoint' $savedDrainLogMock
    $script:DrainCleanupCalls=$savedDrainCleanupCalls

    # THE REAL DRAIN, NOT A MOCKED ONE.
    #
    # Asserting that the transition passes True to a MOCKED
    # Invoke-CutoverDrainObserve proves the call site and nothing about the
    # drain itself.  Reproduced before writing this: removing the tolerance
    # from either of the drain's two pre-run reads left the suite green.
    # These two exercise the real drain and record what it forwards.
    $script:DrainForwarded = New-Object 'System.Collections.Generic.List[string]'
    Set-TestMock 'Get-CutoverExactInstallerStatus' {
        param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState, $RequireResultZero, $AllowInheritedTaskResult)
        $script:DrainForwarded.Add($(
            if ($PSBoundParameters.ContainsKey('AllowInheritedTaskResult')) { [string][bool]$AllowInheritedTaskResult } else { 'absent' }))
        New-TestExactStatus
    }
    $script:RunSequence = @('no_eligible_order')
    $script:RunSequenceIndex = 0
    $null = Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path -AllowInheritedTaskResult $true
    Assert-True 'the real drain forwards the tolerance to both pre-run reads' (
        ($script:DrainForwarded.ToArray() -join ',') -ceq 'True,True'
    ) -Detail ($script:DrainForwarded.ToArray() -join ',')
    $script:DrainForwarded = New-Object 'System.Collections.Generic.List[string]'
    $script:RunSequence = @('no_eligible_order')
    $script:RunSequenceIndex = 0
    $null = Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path
    Assert-True 'the real drain defaults both pre-run reads to no tolerance' (
        ($script:DrainForwarded.ToArray() -join ',') -ceq 'False,False'
    ) -Detail ($script:DrainForwarded.ToArray() -join ',')
    Set-TestMock 'Get-CutoverExactInstallerStatus' { param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState) New-TestExactStatus }

    $script:RunSequence = @('overlap_suppressed')
    $script:RunSequenceIndex = 0
    Assert-ThrowsCode 'DrainObserve rejects overlap as an intermediate success' {
        Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path | Out-Null
    } 'OBSERVE_INTERMEDIATE_STATUS_INVALID'

    $script:RunSequence = @('candidate_observed')
    $script:RunSequenceIndex = 0
    Assert-ThrowsCode 'DrainObserve stops at first unresolved candidate' {
        Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path | Out-Null
    } 'OBSERVE_CANDIDATE_REQUIRES_EXTERNAL_RESOLUTION'
    Assert-True 'candidate drain cannot loop or falsely report drained' ($script:RunSequenceIndex -eq 1 -and $script:DrainCleanupCalls -eq 2)

    $context.max_runs = 1
    $script:RunSequence = @('stale_order_ignored')
    $script:RunSequenceIndex = 0
    Assert-ThrowsCode 'DrainObserve fails closed at max-run bound' {
        Invoke-CutoverDrainObserve -Context $context -Installer $context.installer_path | Out-Null
    } 'OBSERVE_DRAIN_BOUND_EXCEEDED'

    # InstallObserveAndDrain invokes the exact installer without Start, proves
    # the existing state/log did not change at installation, authenticates the
    # enabled old XML backup, then enters the shared drain loop.
    Reset-TestMocks
    $context = New-TestContext
    $script:InstallRequestedAction = ''
    $script:InstallRequestedMode = ''
    Set-TestMock 'Invoke-CutoverEscrow' { param($Context, $RequestedAction) New-TestEscrowReceipt -Action Validate -XmlSha256 $script:TestXmlHash }
    $script:ObserveStatusExpectations = New-Object 'System.Collections.Generic.List[string]'
    Set-TestMock 'Get-CutoverExactInstallerStatus' {
        param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState)
        $script:ObserveStatusExpectations.Add($ExpectedMode + ':' + $ExpectedTaskState)
        New-TestExactStatus
    }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{ value = (New-TestState -Mode Execute); checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
    }
    Set-TestMock 'Assert-CutoverTriggerWindow' { param($NextRunUtc, $RequiredSeconds) }
    $script:XmlCall = 0
    Set-TestMock 'Export-CutoverTaskXml' {
        $script:XmlCall++
        if ($script:XmlCall -eq 1) { $script:XmlEnabled } else { $script:XmlDisabled }
    }
    Set-TestMock 'Disable-CutoverTask' { $script:DisableCalled++ }
    $script:DisableCalled = 0
    Set-TestMock 'Get-CutoverFileCheckpoint' { param($Path, $MaximumBytes, $MissingCode, $InvalidCode) [pscustomobject]@{ length = 10; sha256 = ('d' * 64) } }
    Set-TestMock 'Get-CutoverLogCheckpoint' { param($Path) [pscustomobject]@{ length = 20; sha256 = ('e' * 64) } }
    Set-TestMock 'Invoke-CutoverInstaller' {
        param($Context, $ScriptPath, $RequestedAction, $RequestedMode)
        $script:InstallRequestedAction = $RequestedAction
        $script:InstallRequestedMode = $RequestedMode
        [pscustomobject]@{ status = 'READY' }
    }
    Set-TestMock 'Assert-CutoverInstallerStatus' { param($Status, $Context, $ExpectedMode, $ExpectedTaskState) New-TestExactStatus }
    Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' { param($Before, $Path, $MaximumBytes, $Code) }
    Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' { param($Context, $ExpectedUtf8TextSha256, $ExpectedUtf16LeBomSha256) }
    Set-TestMock 'Invoke-CutoverDrainObserve' {
        param($Context, $Installer)
        [pscustomobject]@{ status = 'OBSERVE_DRAINED'; runs = 2; final_run_id = ('b' * 32); final_worker_status = 'no_eligible_order'; final_last_run_utc = [DateTime]::UtcNow.ToString('o'); log_appended_bytes = 20 }
    }
    $installDrainReceipt = Invoke-CutoverInstallObserveAndDrain -Context $context
    Assert-True 'InstallObserveAndDrain installs Observe without Start' ($script:InstallRequestedAction -ceq 'Install' -and $script:InstallRequestedMode -ceq 'Observe')
    Assert-True 'InstallObserveAndDrain reports state not restored' ($installDrainReceipt.state_not_restored -and $installDrainReceipt.backup_matches_post_disable_xml)
    Assert-True 'InstallObserveAndDrain disables Ready old task before install' ($script:DisableCalled -eq 1 -and $installDrainReceipt.post_disable_task_state -ceq 'Disabled' -and -not $installDrainReceipt.task_stopped)
    Assert-True 'InstallObserveAndDrain uses shared bounded drain' ($installDrainReceipt.runs -eq 2 -and $installDrainReceipt.final_worker_status -ceq 'no_eligible_order')
    Assert-True 'InstallObserveAndDrain performs a distinct post-install Status readback' (
        ($script:ObserveStatusExpectations.ToArray() -join ',') -ceq 'Execute:Ready,Execute:Ready,Execute:Disabled,Observe:Ready' -and
        $installDrainReceipt.post_install_status_readback
    )

    # The incident-only transition is the sole path that accepts the exact
    # old Execute Ready/result-20 BOARD_HEADER_INVALID state.  Its complete
    # exceptional proof is read-only and outside the cleanup/mutation region.
    Reset-TestMocks
    Reset-TestFailedExecuteScenario
    $incidentContext = New-TestContext
    $incidentContext.expected_current_task_result = 20
    $incidentContext.expected_current_failure_code = 'BOARD_HEADER_INVALID'
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentEscrowHash = $script:TestXmlHash
    Set-TestMock 'Invoke-CutoverEscrow' {
        param($Context, $RequestedAction)
        $script:IncidentEscrowAction = $RequestedAction
        New-TestEscrowReceipt -Action Validate -XmlSha256 $script:IncidentEscrowHash
    }
    # DECLARING $AllowInheritedTaskResult HERE IS NOT OPTIONAL.
    #
    # Measured under 5.1: a function created from a scriptblock SILENTLY
    # ACCEPTS a named parameter its param() block never declared.  A mock that
    # omits one therefore keeps passing while observing nothing, and a
    # regression that stopped passing this gate would look exactly like a
    # green run.  Record it so the sequence assertion can see it.
    Set-TestMock 'Get-CutoverExactInstallerStatus' {
        param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState, $RequireResultZero, $AllowInheritedTaskResult)
        $explicitResultGate = $PSBoundParameters.ContainsKey('RequireResultZero')
        $explicitInherited = $PSBoundParameters.ContainsKey('AllowInheritedTaskResult')
        $script:IncidentStatusSequence.Add(
            $ExpectedMode + ':' + $ExpectedTaskState + ':' +
            $(if ($explicitResultGate) { [string][bool]$RequireResultZero } else { 'default' }) + ':' +
            $(if ($explicitInherited) { [string][bool]$AllowInheritedTaskResult } else { 'default' })
        )
        if ($ScriptPath -ceq $Context.restored_installer_path -and $ExpectedTaskState -ceq 'Ready') {
            $script:IncidentStatusCall++
            if ($script:IncidentStatusCall -eq 1) {
                return New-TestExactStatus -Last $script:IncidentStart -Next $script:IncidentInitialNext -State Ready -LastTaskResult $script:IncidentInitialResult
            }
            return New-TestExactStatus -Last $script:IncidentSecondLast -Next $script:IncidentSecondNext -State Ready -LastTaskResult $script:IncidentSecondResult
        }
        if ($ScriptPath -ceq $Context.restored_installer_path -and $ExpectedTaskState -ceq 'Disabled') {
            return New-TestExactStatus -Last $script:IncidentDisabledLast -Next $script:IncidentSecondNext -State Disabled -LastTaskResult $script:IncidentDisabledResult
        }
        if ($script:IncidentCandidateStatusFailureCode) {
            Throw-Cutover -Code $script:IncidentCandidateStatusFailureCode
        }
        return New-TestExactStatus -Last $script:IncidentStart -Next $script:IncidentInitialNext -State Ready -LastTaskResult 0
    }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{
            value = $script:IncidentState
            checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) }
        }
    }
    Set-TestMock 'Get-CutoverLogCheckpoint' {
        param($Path)
        [pscustomobject]@{ length = 20; sha256 = ('e' * 64) }
    }
    Set-TestMock 'Get-CutoverTrailingLogRun' {
        param($Path, $Checkpoint, $RunId)
        return @($script:IncidentEntries)
    }
    Set-TestMock 'Assert-CutoverTriggerWindow' {
        param($NextRunUtc, $RequiredSeconds)
        if ($script:IncidentTriggerFailureCode) { Throw-Cutover -Code $script:IncidentTriggerFailureCode }
    }
    Set-TestMock 'Export-CutoverTaskXml' {
        $script:IncidentXmlCall++
        if ($script:IncidentXmlCall -eq 1) { return $script:XmlEnabled }
        if ($script:IncidentXmlCall -eq 2) {
            if ($script:IncidentPreDisableXml) { return $script:IncidentPreDisableXml }
            return $script:XmlEnabled
        }
        if ($script:IncidentDisabledXml) { return $script:IncidentDisabledXml }
        return $script:XmlDisabled
    }
    Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' {
        param($Before, $Path, $MaximumBytes, $Code)
        $script:IncidentCheckpointCalls++
        if ($script:IncidentCheckpointFailureCode -and $Code -ceq $script:IncidentCheckpointFailureCode) {
            Throw-Cutover -Code $Code
        }
    }
    Set-TestMock 'Disable-CutoverTask' { $script:IncidentDisableCalls++ }
    Set-TestMock 'Invoke-CutoverInstaller' {
        param($Context, $ScriptPath, $RequestedAction, $RequestedMode, $ExpectedCurrentTaskXmlSha256)
        $script:IncidentInstallCalls++
        $script:IncidentInstallPath = $ScriptPath
        $script:IncidentInstallAction = $RequestedAction
        $script:IncidentInstallMode = $RequestedMode
        $script:IncidentExpectedCurrentTaskXmlSha256 = $ExpectedCurrentTaskXmlSha256
        if ($script:IncidentInstallFailureCode) { Throw-Cutover -Code $script:IncidentInstallFailureCode }
        $script:IncidentCandidateInstalled = $true
        [pscustomobject]@{ status = $script:IncidentInstallReceiptStatus }
    }
    # Declares $AllowInheritedTaskResult for the same reason the status mock
    # does.  This is the read that actually failed on the guest, and while
    # this mock omitted the parameter, removing the tolerance from the
    # post-install Assert call left the whole suite green.
    Set-TestMock 'Assert-CutoverInstallerStatus' {
        param($Status, $Context, $ExpectedMode, $ExpectedTaskState, $RequireResultZero, $AllowInheritedTaskResult)
        $script:IncidentAssertInherited = $(
            if ($PSBoundParameters.ContainsKey('AllowInheritedTaskResult')) { [string][bool]$AllowInheritedTaskResult } else { 'absent' })
        if ($script:IncidentInstallReceiptFailureCode) {
            Throw-Cutover -Code $script:IncidentInstallReceiptFailureCode
        }
        if ([string]$Status.status -cne 'READY') {
            Throw-Cutover -Code 'INSTALLER_STATUS_MISMATCH'
        }
        New-TestExactStatus -Last $script:IncidentStart -State Ready -LastTaskResult 0
    }
    Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' {
        param($Context, $ExpectedUtf8TextSha256, $ExpectedUtf16LeBomSha256)
        $script:IncidentBackupCalls++
        $script:IncidentBackupUtf8Sha256 = $ExpectedUtf8TextSha256
        $script:IncidentBackupUtf16LeBomSha256 = $ExpectedUtf16LeBomSha256
        if ($script:IncidentBackupFailureCode) { Throw-Cutover -Code $script:IncidentBackupFailureCode }
    }
    Set-TestMock 'Invoke-CutoverDrainObserve' {
        param($Context, $Installer, $AllowInheritedTaskResult)
        $script:IncidentDrainCalls++
        $script:IncidentDrainInstaller = $Installer
        $script:IncidentDrainInherited = $(
            if ($PSBoundParameters.ContainsKey('AllowInheritedTaskResult')) { [string][bool]$AllowInheritedTaskResult } else { 'default' })
        if ($script:IncidentDrainFailureCode) {
            $exception = New-Object InvalidOperationException($script:IncidentDrainFailureCode)
            if ($script:IncidentDrainFailureCode -ceq 'OBSERVE_DRAIN_ALREADY_QUARANTINED') {
                $exception.Data['cleanup_status'] = 'DISABLED_VERIFIED'
            }
            throw $exception
        }
        [pscustomobject]@{
            status = 'OBSERVE_DRAINED'; runs = 1; final_run_id = ('b' * 32)
            final_worker_status = 'no_eligible_order'
            final_last_run_utc = [DateTime]::UtcNow.ToString('o'); log_appended_bytes = 50
        }
    }
    Set-TestMock 'Start-CutoverTask' { $script:IncidentStartCalls++ }
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $script:IncidentCleanupCalls++
        $script:IncidentCleanupSequence.Add($Installer + ':' + (@($ExpectedModes) -join ','))
        if ($script:IncidentDefinitionUnknown) {
            Throw-Cutover -Code 'FAILURE_CLEANUP_DEFINITION_NOT_VERIFIED'
        }
        if ($Installer -ceq $Context.installer_path -and -not $script:IncidentCandidateInstalled) {
            Throw-Cutover -Code 'FAILURE_CLEANUP_DEFINITION_NOT_VERIFIED'
        }
        $script:IncidentCleanupObservedInitialState = $script:IncidentCleanupInitialState
        $script:IncidentCleanupActiveStatus = $(if ($script:IncidentCleanupInitialState -ceq 'Running') { 'FINISHED_NATURALLY' } else { 'NONE' })
        [pscustomobject]@{
            mode = $(if ($Installer -ceq $Context.restored_installer_path) { 'Execute' } else { 'Observe' })
            initial_task_state = $script:IncidentCleanupInitialState; final_task_state = 'Disabled'
            disable_performed = ($script:IncidentCleanupInitialState -cne 'Disabled')
            future_triggers_disabled = $true; definition_preserved_except_enabled = $true
            task_stopped = $script:IncidentCleanupTaskStopped
            active_instance_status = $script:IncidentCleanupActiveStatus
        }
    }

    $expectedDisabledXml = Get-CutoverTaskXmlEvidence -Text $script:XmlDisabled
    $incidentReceipt = Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext
    $incidentReceiptJson = ConvertTo-CutoverBoundedReceipt -Receipt $incidentReceipt
    # The candidate readback now carries the inherited-result tolerance, and
    # the three pre-disable reads deliberately do not: they describe the OLD
    # task, where result 20 is bound by expected_current_task_result instead.
    Assert-True 'failed Execute transition pins old result reads and tolerates only the inherited candidate result' (
        ($script:IncidentStatusSequence.ToArray() -join ',') -ceq
            'Execute:Ready:False:default,Execute:Ready:False:default,Execute:Disabled:False:default,Observe:Ready:default:True'
    )
    Assert-True 'failed Execute transition carries the tolerance into the drain' (
        $script:IncidentDrainInherited -ceq 'True'
    )
    Assert-True 'failed Execute transition tolerates the inherited result on the install receipt' (
        $script:IncidentAssertInherited -ceq 'True'
    ) -Detail $script:IncidentAssertInherited
    Assert-True 'failed Execute transition disables once then installs Observe without Start' (
        $script:IncidentDisableCalls -eq 1 -and $script:IncidentInstallCalls -eq 1 -and
        $script:IncidentStartCalls -eq 0 -and $script:IncidentDrainCalls -eq 1
    )
    Assert-True 'failed Execute transition passes exact governed action arguments' (
        $script:IncidentEscrowAction -ceq 'Validate' -and
        $script:IncidentInstallPath -ceq $incidentContext.installer_path -and
        $script:IncidentInstallAction -ceq 'InstallFromDisabledNoStop' -and
        $script:IncidentInstallMode -ceq 'Observe' -and
        $script:IncidentExpectedCurrentTaskXmlSha256 -ceq $expectedDisabledXml.utf8_text_sha256 -and
        $script:IncidentDrainInstaller -ceq $incidentContext.installer_path -and
        $script:IncidentCandidateInstalled
    )
    Assert-True 'failed Execute transition verifies the exact disabled XML backup hashes' (
        $script:IncidentBackupUtf8Sha256 -ceq $expectedDisabledXml.utf8_text_sha256 -and
        $script:IncidentBackupUtf16LeBomSha256 -ceq $expectedDisabledXml.utf16le_bom_sha256
    )
    Assert-True 'failed Execute transition returns bounded causal recovery evidence' (
        $incidentReceipt.ok -and
        $incidentReceipt.action -ceq 'InstallObserveAndDrainFromFailedExecute' -and
        $incidentReceipt.operation_id -ceq $incidentContext.operation_id -and
        $incidentReceipt.release_id -ceq $incidentContext.release_id -and
        $incidentReceipt.escrow_id -ceq $incidentContext.escrow_id -and
        $incidentReceipt.escrow_release_id -ceq $incidentContext.escrow_release_id -and
        $incidentReceipt.failed_execute_code -ceq 'BOARD_HEADER_INVALID' -and
        $incidentReceipt.failed_execute_task_result -eq 20 -and
        $incidentReceipt.failed_execute_run_id -ceq $script:IncidentRunId -and
        $incidentReceipt.pre_task_state -ceq 'Ready' -and
        $incidentReceipt.post_disable_task_state -ceq 'Disabled' -and
        $incidentReceipt.candidate_task_state -ceq 'Ready' -and
        $incidentReceipt.candidate_install_action -ceq 'InstallFromDisabledNoStop' -and
        $incidentReceipt.rollback_action -ceq 'RestoreReady' -and
        $incidentReceipt.old_definition_preserved_except_enabled -and
        $incidentReceipt.backup_matches_post_disable_xml -and
        $incidentReceipt.post_install_status_readback -and
        $incidentReceipt.state_and_log_preserved_through_candidate_install -and
        -not $incidentReceipt.task_stopped -and
        $script:IncidentBackupCalls -eq 1 -and $script:IncidentCleanupCalls -eq 0 -and
        [Text.Encoding]::UTF8.GetByteCount($incidentReceiptJson) -le 3072
    )

    foreach ($badInitialResult in @(0, 1, 31)) {
        Reset-TestFailedExecuteScenario
        $script:IncidentEscrowHash = $script:TestXmlHash
        $incidentContext.expected_current_run_id = $script:IncidentRunId
        $script:IncidentInitialResult = [int64]$badInitialResult
        Assert-ThrowsCode ('failed Execute preauth rejects initial result ' + $badInitialResult) {
            Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
        } 'FAILED_EXECUTE_TASK_RESULT_MISMATCH'
        Assert-True ('failed Execute initial-result rejection is non-mutating ' + $badInitialResult) (
            $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
            $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
            $script:IncidentStartCalls -eq 0
        )
    }

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentState.error.code = 'BOARD_READ_HTTP_ERROR'
    $script:IncidentState.error.message = 'BOARD_READ_HTTP_ERROR'
    Assert-ThrowsCode 'failed Execute preauth rejects a transient HTTP failure' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'FAILED_EXECUTE_ERROR_EVIDENCE_INVALID'
    Assert-True 'wrong current failure code cannot trigger cleanup or mutation' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = ('f' * 32)
    Assert-ThrowsCode 'failed Execute preauth rejects a superseded caller run pin' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'FAILED_EXECUTE_RUN_ID_MISMATCH'
    Assert-True 'stale run pin cannot disable or invoke cleanup' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = ('0' * 64)
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    Assert-ThrowsCode 'failed Execute preauth rejects escrow XML mismatch' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'FAILED_EXECUTE_ESCROW_XML_MISMATCH'
    Assert-True 'escrow mismatch cannot disable or invoke cleanup' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentSecondLast = $script:IncidentStart.AddSeconds(1)
    Assert-ThrowsCode 'failed Execute preauth rejects an advanced second LastRunTime' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'TASK_CHANGED_BEFORE_DISABLE'
    Assert-True 'LastRunTime race is rejected before disable or cleanup' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentSecondResult = 0
    Assert-ThrowsCode 'failed Execute preauth rejects changed result at second read' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'FAILED_EXECUTE_TASK_RESULT_MISMATCH'
    Assert-True 'result race is rejected before disable or cleanup' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentSecondNext = $script:IncidentInitialNext.AddMinutes(15)
    Assert-ThrowsCode 'failed Execute preauth rejects changed NextRunTime' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'TASK_CHANGED_BEFORE_DISABLE'
    Assert-True 'NextRunTime race is rejected before disable or cleanup' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentTriggerFailureCode = 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'
    Assert-ThrowsCode 'failed Execute preauth rejects an imminent natural trigger' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'
    Assert-True 'trigger-window rejection is non-mutating' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentCheckpointFailureCode = 'STATE_CHANGED_BEFORE_DISABLE'
    Assert-ThrowsCode 'failed Execute preauth rejects state drift before disable' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'STATE_CHANGED_BEFORE_DISABLE'
    Assert-True 'pre-disable state drift cannot invoke cleanup or mutation' (
        $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    foreach ($preauthCase in @(
        [pscustomobject]@{
            name = 'pre-disable task XML drift'; code = 'TASK_CHANGED_BEFORE_DISABLE'
            setup = { $script:IncidentPreDisableXml = $script:XmlEnabled.Replace('</Task>', '<!--drift--></Task>') }
        },
        [pscustomobject]@{
            name = 'pre-disable log drift'; code = 'LOG_CHANGED_BEFORE_DISABLE'
            setup = { $script:IncidentCheckpointFailureCode = 'LOG_CHANGED_BEFORE_DISABLE' }
        }
    )) {
        Reset-TestFailedExecuteScenario
        $script:IncidentEscrowHash = $script:TestXmlHash
        $incidentContext.expected_current_run_id = $script:IncidentRunId
        & $preauthCase.setup
        Assert-ThrowsCode ('failed Execute preauth rejects ' + $preauthCase.name) {
            Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
        } $preauthCase.code
        Assert-True ('failed Execute ' + $preauthCase.name + ' is non-mutating') (
            $script:IncidentDisableCalls -eq 0 -and $script:IncidentInstallCalls -eq 0 -and
            $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 0 -and
            $script:IncidentStartCalls -eq 0
        )
    }

    foreach ($postAuthCase in @(
        [pscustomobject]@{
            name = 'disabled result drift'; code = 'TASK_CHANGED_DURING_DISABLE'
            cleanup_calls = 2
            setup = { $script:IncidentDisabledResult = 0 }
        },
        [pscustomobject]@{
            name = 'disabled LastRunTime drift'; code = 'TASK_CHANGED_DURING_DISABLE'
            cleanup_calls = 2
            setup = { $script:IncidentDisabledLast = $script:IncidentStart.AddSeconds(1) }
        },
        [pscustomobject]@{
            name = 'post-disable state drift'; code = 'STATE_CHANGED_DURING_DISABLE'
            cleanup_calls = 2
            setup = { $script:IncidentCheckpointFailureCode = 'STATE_CHANGED_DURING_DISABLE' }
        },
        [pscustomobject]@{
            name = 'post-disable log drift'; code = 'LOG_CHANGED_DURING_DISABLE'
            cleanup_calls = 2
            setup = { $script:IncidentCheckpointFailureCode = 'LOG_CHANGED_DURING_DISABLE' }
        },
        [pscustomobject]@{
            name = 'invalid candidate Install receipt'; code = 'CANDIDATE_INSTALL_RECEIPT_INVALID'
            cleanup_calls = 1
            setup = { $script:IncidentInstallReceiptFailureCode = 'CANDIDATE_INSTALL_RECEIPT_INVALID' }
        },
        [pscustomobject]@{
            name = 'matching candidate becomes Running after registration'; code = 'INSTALLER_STATUS_MISMATCH'
            cleanup_calls = 1
            setup = {
                $script:IncidentInstallReceiptStatus = 'RUNNING'
                $script:IncidentCleanupInitialState = 'Running'
            }
        },
        [pscustomobject]@{
            name = 'failed distinct candidate Status readback'; code = 'CANDIDATE_STATUS_INVALID'
            cleanup_calls = 1
            setup = { $script:IncidentCandidateStatusFailureCode = 'CANDIDATE_STATUS_INVALID' }
        },
        [pscustomobject]@{
            name = 'post-install state drift'; code = 'STATE_CHANGED_DURING_INSTALL'
            cleanup_calls = 1
            setup = { $script:IncidentCheckpointFailureCode = 'STATE_CHANGED_DURING_INSTALL' }
        },
        [pscustomobject]@{
            name = 'post-install log drift'; code = 'LOG_CHANGED_DURING_INSTALL'
            cleanup_calls = 1
            setup = { $script:IncidentCheckpointFailureCode = 'LOG_CHANGED_DURING_INSTALL' }
        },
        [pscustomobject]@{
            name = 'candidate backup mismatch'; code = 'BACKUP_EXPECTED_XML_MISMATCH'
            cleanup_calls = 1
            setup = { $script:IncidentBackupFailureCode = 'BACKUP_EXPECTED_XML_MISMATCH' }
        }
    )) {
        Reset-TestFailedExecuteScenario
        $script:IncidentEscrowHash = $script:TestXmlHash
        $incidentContext.expected_current_run_id = $script:IncidentRunId
        & $postAuthCase.setup
        Assert-ThrowsCode ('failed Execute transition quarantines after ' + $postAuthCase.name) {
            Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
        } $postAuthCase.code
        $expectedCleanupSequence = if ($postAuthCase.cleanup_calls -eq 2) {
            $incidentContext.installer_path + ':Observe,' + $incidentContext.restored_installer_path + ':Execute'
        } else {
            $incidentContext.installer_path + ':Observe'
        }
        Assert-True ('failed Execute ' + $postAuthCase.name + ' authenticates the surviving definition') (
            $script:IncidentDisableCalls -eq 1 -and
            $script:IncidentCleanupCalls -eq $postAuthCase.cleanup_calls -and
            ($script:IncidentCleanupSequence.ToArray() -join ',') -ceq $expectedCleanupSequence -and
            $script:IncidentCandidateInstalled -eq ($postAuthCase.cleanup_calls -eq 1) -and
            $script:IncidentStartCalls -eq 0 -and
            -not $script:IncidentCleanupTaskStopped
        )
        if ($postAuthCase.name -ceq 'matching candidate becomes Running after registration') {
            Assert-True 'matching post-register Running candidate is quarantined and allowed to finish naturally' (
                $script:IncidentCleanupObservedInitialState -ceq 'Running' -and
                $script:IncidentCleanupActiveStatus -ceq 'FINISHED_NATURALLY' -and
                $script:IncidentCleanupCalls -eq 1 -and
                -not $script:IncidentCleanupTaskStopped -and
                $script:IncidentStartCalls -eq 0
            )
        }
    }

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentDisabledXml = $script:XmlDisabled.Replace('</Task>', '<!--drift--></Task>')
    $script:IncidentDefinitionUnknown = $true
    $unknownDefinitionFailure = $null
    try {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } catch { $unknownDefinitionFailure = $_ }
    $unknownDefinitionReceipt = New-CutoverFailureReceipt `
        -ActionValue InstallObserveAndDrainFromFailedExecute `
        -OperationIdValue $incidentContext.operation_id `
        -FailureRecord $unknownDefinitionFailure
    Assert-True 'failed Execute rejects disabled XML drift when neither definition authenticates' (
        $null -ne $unknownDefinitionFailure -and
        [string]$unknownDefinitionFailure.Exception.Message -ceq 'CUTOVER_FAILURE_CLEANUP_FAILED' -and
        $unknownDefinitionReceipt.code -ceq 'CUTOVER_FAILURE_CLEANUP_FAILED' -and
        $unknownDefinitionReceipt.original_code -ceq 'TASK_DISABLE_DEFINITION_DRIFT' -and
        $unknownDefinitionReceipt.cleanup_status -ceq 'FAILED' -and
        $unknownDefinitionReceipt.cleanup_code -ceq 'FAILURE_CLEANUP_DEFINITION_NOT_VERIFIED'
    )
    Assert-True 'unknown disabled definition is not reported rollback-ready' (
        $script:IncidentDisableCalls -eq 1 -and $script:IncidentInstallCalls -eq 0 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 2 -and
        ($script:IncidentCleanupSequence.ToArray() -join ',') -ceq
            ($incidentContext.installer_path + ':Observe,' + $incidentContext.restored_installer_path + ':Execute') -and
        $null -eq $unknownDefinitionReceipt.PSObject.Properties['cleanup_mode'] -and
        $null -eq $unknownDefinitionReceipt.PSObject.Properties['rollback_action'] -and
        $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentInstallFailureCode = 'CANDIDATE_INSTALL_FAILED'
    Assert-ThrowsCode 'failed Execute transition quarantines after install failure' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'CANDIDATE_INSTALL_FAILED'
    Assert-True 'post-auth install failure rejects candidate then quarantines surviving old definition' (
        $script:IncidentDisableCalls -eq 1 -and $script:IncidentInstallCalls -eq 1 -and
        $script:IncidentDrainCalls -eq 0 -and $script:IncidentCleanupCalls -eq 2 -and
        $script:IncidentCleanupSequence[0] -ceq ($incidentContext.installer_path + ':Observe') -and
        $script:IncidentCleanupSequence[1] -ceq ($incidentContext.restored_installer_path + ':Execute') -and
        -not $script:IncidentCandidateInstalled -and $script:IncidentStartCalls -eq 0
    )

    Reset-TestFailedExecuteScenario
    $script:IncidentEscrowHash = $script:TestXmlHash
    $incidentContext.expected_current_run_id = $script:IncidentRunId
    $script:IncidentDrainFailureCode = 'OBSERVE_DRAIN_ALREADY_QUARANTINED'
    Assert-ThrowsCode 'failed Execute transition preserves shared-drain quarantine' {
        Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $incidentContext | Out-Null
    } 'OBSERVE_DRAIN_ALREADY_QUARANTINED'
    Assert-True 'outer transition does not duplicate shared-drain cleanup' (
        $script:IncidentDisableCalls -eq 1 -and $script:IncidentInstallCalls -eq 1 -and
        $script:IncidentDrainCalls -eq 1 -and $script:IncidentCleanupCalls -eq 0 -and
        $script:IncidentStartCalls -eq 0
    )

    # InstallExecuteReady owns the Observe-to-Execute mutation without starting
    # the task: Ready -> disable/readback -> Install -> exact Execute readback.
    Reset-TestMocks
    $context = New-TestContext
    $context.mode = 'Execute'
    $script:ExecuteSequence = New-Object 'System.Collections.Generic.List[string]'
    Set-TestMock 'Assert-CutoverPinnedExecutables' {
        param($Context)
        $script:ExecuteSequence.Add('pinned')
    }
    $script:StatusExpectations = New-Object 'System.Collections.Generic.List[string]'
    Set-TestMock 'Get-CutoverExactInstallerStatus' {
        param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState)
        $script:StatusExpectations.Add($ExpectedMode + ':' + $ExpectedTaskState)
        $next = if ($ExpectedMode -ceq 'Execute') { $script:ExecuteReadbackNext } else { [DateTime]'2099-01-01T00:00:00Z' }
        New-TestExactStatus -Next $next
    }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{
            value = (New-TestState -Mode Observe -RunId ('a' * 32) -Status no_eligible_order)
            checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) }
        }
    }
    Set-TestMock 'Get-CutoverLogCheckpoint' { param($Path) [pscustomobject]@{ length = 20; sha256 = ('e' * 64) } }
    $script:CurrentObserveRunValidated = 0
    Set-TestMock 'Assert-CutoverCurrentTerminalRun' {
        param($Context, $State, $ExactStatus, $LogCheckpoint, $ExpectedMode, $ExpectedStatus)
        $script:CurrentObserveRunValidated++
        [pscustomobject]@{ run_id = ('a' * 32); last_run_utc = $ExactStatus.last_run_utc; log_checkpoint = $LogCheckpoint }
    }
    $script:XmlCall = 0
    Set-TestMock 'Export-CutoverTaskXml' {
        $script:XmlCall++
        if ($script:XmlCall -eq 2) { $script:XmlDisabled } else { $script:XmlEnabled }
    }
    $script:DisableCalled = 0
    Set-TestMock 'Disable-CutoverTask' { $script:DisableCalled++ }
    $script:ExecuteInstallAction = ''
    $script:ExecuteInstallMode = ''
    Set-TestMock 'Invoke-CutoverInstaller' {
        param($Context, $ScriptPath, $RequestedAction, $RequestedMode)
        $script:ExecuteSequence.Add('install')
        $script:ExecuteInstallAction = $RequestedAction
        $script:ExecuteInstallMode = $RequestedMode
        [pscustomobject]@{ status = 'READY' }
    }
    $script:ExecuteReadbackNext = [DateTime]'2099-01-01T00:00:00Z'
    Set-TestMock 'Assert-CutoverInstallerStatus' {
        param($Status, $Context, $ExpectedMode, $ExpectedTaskState)
        New-TestExactStatus -Next $script:ExecuteReadbackNext
    }
    Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' { param($Before, $Path, $MaximumBytes, $Code) }
    $script:ExecuteBackupChecked = 0
    Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' {
        param($Context, $ExpectedUtf8TextSha256, $ExpectedUtf16LeBomSha256)
        $script:ExecuteBackupChecked++
    }
    $script:UnexpectedStart = 0
    Set-TestMock 'Start-CutoverTask' { $script:UnexpectedStart++ }
    $script:FailureCleanupCalls = 0
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $script:FailureCleanupCalls++
        [pscustomobject]@{ mode = 'Execute'; future_triggers_disabled = $true; definition_preserved_except_enabled = $true; task_stopped = $false }
    }
    $executeReadyReceipt = Invoke-CutoverInstallExecuteReady -Context $context
    Assert-True 'InstallExecuteReady proves Observe Ready then Disabled' (
        ($script:StatusExpectations.ToArray() -join ',') -ceq 'Observe:Ready,Observe:Ready,Observe:Disabled,Execute:Ready' -and
        $script:DisableCalled -eq 1
    )
    Assert-True 'InstallExecuteReady installs Execute without Start' (
        $script:ExecuteInstallAction -ceq 'Install' -and $script:ExecuteInstallMode -ceq 'Execute' -and
        $script:UnexpectedStart -eq 0 -and $executeReadyReceipt.status -ceq 'EXECUTE_READY'
    )
    Assert-True 'InstallExecuteReady pins Claude and Git before install' (
        ($script:ExecuteSequence.ToArray() -join ',') -ceq 'pinned,install' -and
        $executeReadyReceipt.execute_executables_pinned -and
        $executeReadyReceipt.claude_sha256 -ceq $context.expected_claude_sha256 -and
        $executeReadyReceipt.git_sha256 -ceq $context.expected_git_sha256
    )
    Assert-True 'InstallExecuteReady preserves recovery and backup evidence' (
        $script:ExecuteBackupChecked -eq 1 -and
        $script:CurrentObserveRunValidated -eq 1 -and
        $executeReadyReceipt.candidate_observe_recovery_evidence_preserved -and
        $executeReadyReceipt.backup_matches_post_disable_xml -and
        $executeReadyReceipt.post_install_status_readback -and
        $executeReadyReceipt.state_and_log_preserved -and
        -not $executeReadyReceipt.task_stopped
    )
    $script:ExecuteReadbackNext = [DateTime]::UtcNow.AddSeconds(10)
    $script:XmlCall = 0
    Assert-ThrowsCode 'InstallExecuteReady rejects an imminent new Execute trigger' {
        Invoke-CutoverInstallExecuteReady -Context $context | Out-Null
    } 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'
    Assert-True 'InstallExecuteReady disables and verifies after post-install failure' (
        $script:FailureCleanupCalls -eq 1 -and $script:UnexpectedStart -eq 0
    )
    $script:ExecuteReadbackNext = [DateTime]'2099-01-01T00:00:00Z'
    foreach ($forbiddenObserveStatus in @('tail_seeded', 'overlap_suppressed')) {
        $script:ForbiddenObserveStatus = $forbiddenObserveStatus
        Set-TestMock 'Get-CutoverState' {
            param($Path)
            [pscustomobject]@{
                value = (New-TestState -Mode Observe -RunId ('a' * 32) -Status $script:ForbiddenObserveStatus)
                checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) }
            }
        }
        Assert-ThrowsCode ('InstallExecuteReady rejects ' + $forbiddenObserveStatus) {
            Invoke-CutoverInstallExecuteReady -Context $context | Out-Null
        } 'FORBIDDEN_TERMINAL_STATUS'
    }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{
            value = (New-TestState -Mode Observe -RunId ('a' * 32) -Status candidate_observed -WorkId 'PENDING-1' -RowId '22222222-2222-2222-2222-222222222222')
            checkpoint = [pscustomobject]@{ length = 10; sha256 = ('d' * 64) }
        }
    }
    Assert-ThrowsCode 'InstallExecuteReady rejects an undrained observed candidate' {
        Invoke-CutoverInstallExecuteReady -Context $context | Out-Null
    } 'STATE_TERMINAL_STATUS_MISMATCH'

    # StartAndAwait is the only Execute board-effects gateway.  It starts once,
    # binds exact task-local identity/status, proves append-only log evidence,
    # and compares the complete protected snapshot after the worker exits.
    Reset-TestMocks
    $context = New-TestContext
    $context.mode = 'Execute'
    $context.expected_terminal_status = 'result_confirmed'
    $context.expected_work_id = 'CANARY-1'
    $context.expected_row_id = $resultRow
    $context.expected_result_status = 'completed'
    Set-TestMock 'Get-CutoverExactInstallerStatus' { param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState) New-TestExactStatus }
    Set-TestMock 'Assert-CutoverTriggerWindow' { param($NextRunUtc, $RequiredSeconds) }
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{ value = (New-TestState -Mode Observe -RunId ('a' * 32)); checkpoint = [pscustomobject]@{ length = 1; sha256 = ('a' * 64) } }
    }
    Set-TestMock 'Assert-CutoverPinnedExecutables' { param($Context) }
    Set-TestMock 'Get-CutoverProtectedSnapshot' { param($Context) 'protected-same' }
    Set-TestMock 'Get-CutoverLogCheckpoint' { param($Path) [pscustomobject]@{ length = 1; sha256 = ('a' * 64) } }
    $script:StartCleanupCalls = 0
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {
        param($Context, $Installer, $ExpectedModes)
        $script:StartCleanupCalls++
        [pscustomobject]@{ mode = 'Execute'; future_triggers_disabled = $true; definition_preserved_except_enabled = $true; task_stopped = $false }
    }
    $script:StartExpectedStatus = ''
    Set-TestMock 'Invoke-CutoverOneRun' {
        param($Context, $Installer, $ExpectedMode, $DeadlineUtc, $PreviousLastRunUtc, $PreviousRunId, $LogBefore, $ExpectedStatus, $ExpectedWorkId, $ExpectedRowId, $ExpectedResultStatus)
        $script:StartExpectedStatus = $ExpectedStatus
        [pscustomobject]@{ status = 'result_confirmed'; run_id = ('b' * 32); last_run_utc = $PreviousLastRunUtc.AddSeconds(3); log_checkpoint = $LogBefore; log_appended_bytes = 50 }
    }
    $startReceipt = Invoke-CutoverStartAndAwait -Context $context
    Assert-True 'StartAndAwait forwards exact canary terminal identity' ($script:StartExpectedStatus -ceq 'result_confirmed' -and $startReceipt.work_id -ceq 'CANARY-1' -and $startReceipt.row_id -ceq $resultRow)
    Assert-True 'StartAndAwait proves append and protected equality' ($startReceipt.log_prefix_preserved -and $startReceipt.protected_fingerprints_unchanged)

    $script:ProtectedCalls = 0
    Set-TestMock 'Get-CutoverProtectedSnapshot' { param($Context) $script:ProtectedCalls++; if ($script:ProtectedCalls -eq 1) { 'before' } else { 'after' } }
    Assert-ThrowsCode 'StartAndAwait fails on any protected drift' {
        Invoke-CutoverStartAndAwait -Context $context | Out-Null
    } 'PROTECTED_FINGERPRINT_CHANGED'
    Assert-True 'StartAndAwait disables and verifies after acceptance failure' ($script:StartCleanupCalls -eq 1)

    # Invoke-CutoverOneRun independently rejects a non-advancing task result and
    # a reused state run id even when the scheduler reports result zero.
    Reset-TestMocks
    $context = New-TestContext
    $previousLast = [DateTime]::UtcNow.AddMinutes(-2)
    Set-TestMock 'Start-CutoverTask' { }
    Set-TestMock 'Wait-CutoverTaskRun' {
        param($PreviousLastRunUtc, $DeadlineUtc, $PollMilliseconds)
        [pscustomobject]@{ state = 'Ready'; last_run_utc = $PreviousLastRunUtc.AddSeconds(1); last_task_result = 0 }
    }
    Set-TestMock 'Get-CutoverExactInstallerStatus' {
        param($Context, $ScriptPath, $ExpectedMode, $ExpectedTaskState)
        [pscustomobject]@{ raw = $null; last_run_utc = $script:MockExactLastRun; next_run_utc = [DateTime]::UtcNow.AddMinutes(15) }
    }
    $script:MockExactLastRun = $previousLast
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{
            value = (New-TestState -Mode Observe -RunId ('a' * 32) -Status no_eligible_order -PollAt '2026-09-07T00:00:00.000Z')
            checkpoint = [pscustomobject]@{ length = 1; sha256 = ('a' * 64) }
        }
    }
    Assert-ThrowsCode 'one-run gate rejects non-advancing exact LastRunTime' {
        Invoke-CutoverOneRun -Context $context -Installer $context.installer_path -ExpectedMode Observe -DeadlineUtc ([DateTime]::UtcNow.AddMinutes(1)) -PreviousLastRunUtc $previousLast -PreviousRunId ('a' * 32) -LogBefore ([pscustomobject]@{ length = 1; sha256 = ('a' * 64) }) | Out-Null
    } 'TASK_LAST_RUN_NOT_ADVANCED'

    $script:MockExactLastRun = $previousLast.AddSeconds(1)
    Set-TestMock 'Get-CutoverState' {
        param($Path)
        [pscustomobject]@{ value = (New-TestState -Mode Observe -RunId ('a' * 32) -Status no_eligible_order); checkpoint = $null }
    }
    Assert-ThrowsCode 'one-run gate rejects reused worker run id' {
        Invoke-CutoverOneRun -Context $context -Installer $context.installer_path -ExpectedMode Observe -DeadlineUtc ([DateTime]::UtcNow.AddMinutes(1)) -PreviousLastRunUtc $previousLast -PreviousRunId ('a' * 32) -LogBefore ([pscustomobject]@{ length = 1; sha256 = ('a' * 64) }) | Out-Null
    } 'STATE_RUN_ID_NOT_ADVANCED'

    Assert-ThrowsCode 'trigger window rejects a natural-run race' {
        Assert-CutoverTriggerWindow -NextRunUtc ([DateTime]::UtcNow.AddSeconds(10)) -RequiredSeconds 60
    } 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'

    # The former disabled Execute enable/replay action is intentionally absent.
    # A real drain delegates to the REAL OneRun. Drift is injected by its
    # SECOND Ready read, after initial admission hashes already matched.
    Reset-TestMocks
    $firstStartContext=New-TestContext
    $firstStartContext.mode='Observe';$firstStartContext.timeout_seconds=420
    $firstStartContext.state_path=Join-Path $temporaryRoot 'first-start-state.json'
    $firstStartContext.log_path=Join-Path $temporaryRoot 'first-start-events.jsonl'
    $script:FirstStartContext=$firstStartContext
    $script:FirstStartCleanState=New-TestState -Mode Observe -RunId ('a'*32) -Status no_eligible_order
    $script:FirstStartCleanLog=([pscustomobject]@{event='prior';run_id=('a'*32)}|ConvertTo-Json -Compress)+"`n"
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {param($Context,$Installer,$ExpectedModes) [pscustomobject]@{mode='Observe';future_triggers_disabled=$true;definition_preserved_except_enabled=$true;task_stopped=$false}}
    Set-TestMock 'Start-CutoverTask' {$script:FirstStartCalls++;throw 'TEST_FIRST_START_REACHED'}
    Set-TestMock 'Get-CutoverExactInstallerStatus' {param($Context,$ScriptPath,$ExpectedMode,$ExpectedTaskState,$AllowInheritedTaskResult)
        $script:FirstStartReadyReads++
        if($script:FirstStartReadyReads -eq 2){
            if($script:FirstStartDrift -ceq 'state'){
                $s=New-TestState -Mode Observe -RunId ('a'*32) -Status no_eligible_order;$s.counts.polls++
                [IO.File]::WriteAllText($script:FirstStartContext.state_path,($s|ConvertTo-Json -Depth 10),(New-Object Text.UTF8Encoding($false)))
            }elseif($script:FirstStartDrift -ceq 'log'){
                [IO.File]::AppendAllText($script:FirstStartContext.log_path,('{"event":"unexpected","run_id":"'+('b'*32)+'"}'+"`n"),(New-Object Text.UTF8Encoding($false)))
            }
        }
        New-TestExactStatus
    }
    foreach($firstStartDrift in @('none','state','log')){
        $script:FirstStartDrift=$firstStartDrift;$script:FirstStartReadyReads=0;$script:FirstStartCalls=0
        [IO.File]::WriteAllText($firstStartContext.state_path,($script:FirstStartCleanState|ConvertTo-Json -Depth 10),(New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($firstStartContext.log_path,$script:FirstStartCleanLog,(New-Object Text.UTF8Encoding($false)))
        $firstStartAdmission=[pscustomobject]@{run_id=('a'*32);last_run_utc=(New-TestExactStatus).last_run_utc;state_checkpoint=(Get-CutoverState -Path $firstStartContext.state_path).checkpoint;log_checkpoint=(Get-CutoverLogCheckpoint -Path $firstStartContext.log_path)}
        $firstStartCode=switch($firstStartDrift){'state'{'STATE_CHANGED_BEFORE_OBSERVE_FIRST_START'};'log'{'LOG_CHANGED_BEFORE_OBSERVE_FIRST_START'};default{'TEST_FIRST_START_REACHED'}}
        Assert-ThrowsCode ('real drain and OneRun first-start interleaving '+$firstStartDrift) {Invoke-CutoverDrainObserve -Context $firstStartContext -Installer $firstStartContext.installer_path -AdmittedBaseline $firstStartAdmission} $firstStartCode
        Assert-True ('first-start '+$firstStartDrift+' starts only the unchanged positive baseline') ($script:FirstStartCalls -eq $(if($firstStartDrift -ceq 'none'){1}else{0}))
    }
    foreach($boundaryTiming in @('deadline','trigger')){
        $script:FirstStartCalls=0
        $timingAdmission=[pscustomobject]@{run_id=('a'*32);last_run_utc=(New-TestExactStatus).last_run_utc;state_checkpoint=(Get-CutoverState -Path $firstStartContext.state_path).checkpoint;log_checkpoint=(Get-CutoverLogCheckpoint -Path $firstStartContext.log_path);next_run_utc=$(if($boundaryTiming -ceq 'trigger'){[DateTime]::UtcNow.AddSeconds(-1)}else{(New-TestExactStatus).next_run_utc})}
        $timingDeadline=if($boundaryTiming -ceq 'deadline'){[DateTime]::UtcNow.AddSeconds(-1)}else{[DateTime]::UtcNow.AddSeconds(420)}
        $timingCode=if($boundaryTiming -ceq 'deadline'){'OBSERVE_FIRST_START_DEADLINE_EXCEEDED'}else{'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'}
        Assert-ThrowsCode ('real OneRun rechecks '+$boundaryTiming+' after checkpoint reads') {Invoke-CutoverOneRun -Context $firstStartContext -Installer $firstStartContext.installer_path -ExpectedMode Observe -DeadlineUtc $timingDeadline -PreviousLastRunUtc $timingAdmission.last_run_utc -PreviousRunId ('a'*32) -LogBefore $timingAdmission.log_checkpoint -AdmittedBaseline $timingAdmission} $timingCode
        Assert-True ('expired '+$boundaryTiming+' cannot start Observe') ($script:FirstStartCalls -eq 0)
    }

    # The new recovery installs Observe; even fresh unrelated work cannot infer
    # or append worker lifecycle rows. The existing Execute gateway stays single.
    Reset-TestMocks
    Assert-True 'unsafe disabled Execute action is not admitted' ((Get-CutoverSafeAction -Value StartAndAwaitFromDisabled) -ceq 'Invalid')
    Assert-True 'command entry forwards exact disabled XML caller pin' ($source.Contains('ExpectedDisabledXmlSha256 = $ExpectedDisabledXmlSha256'))
    $futureContext = New-TestContext
    $futureContext.timeout_seconds = 420
    $futureXml = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><BootTrigger id="AtBoot"/><TimeTrigger id="Every15Minutes"><StartBoundary>2099-01-01T00:00:00Z</StartBoundary><Repetition><Interval>PT15M</Interval></Repetition></TimeTrigger></Triggers><Settings><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled></Settings></Task>'
    $futureEvidence = Assert-CutoverFutureObserveDefinition -Text $futureXml -Context $futureContext
    Assert-True 'Observe recovery accepts production boot interval and catch-up setting with future boundary' ($futureEvidence.Kind -eq [DateTimeKind]::Utc -and $futureEvidence.Year -eq 2099)
    Assert-ThrowsCode 'future definition rejects stale boundary before drain' {Assert-CutoverFutureObserveDefinition -Text ($futureXml.Replace('2099-01-01T00:00:00Z','2026-01-01T00:00:00Z')) -Context $futureContext} 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'
    Assert-ThrowsCode 'future definition rejects non-fifteen-minute interval' {Assert-CutoverFutureObserveDefinition -Text ($futureXml.Replace('PT15M','PT5M')) -Context $futureContext} 'OBSERVE_RECOVERY_TRIGGER_SHAPE_INVALID'
    Assert-ThrowsCode 'future definition rejects unexpected registration trigger' {Assert-CutoverFutureObserveDefinition -Text ($futureXml.Replace('<BootTrigger id="AtBoot"/>','<RegistrationTrigger/>')) -Context $futureContext} 'OBSERVE_RECOVERY_TRIGGER_SHAPE_INVALID'
    Assert-ThrowsCode 'future definition enforces production StartWhenAvailable true' {Assert-CutoverFutureObserveDefinition -Text ($futureXml.Replace('<StartWhenAvailable>true</StartWhenAvailable>','<StartWhenAvailable>false</StartWhenAvailable>')) -Context $futureContext} 'OBSERVE_RECOVERY_SETTINGS_INVALID'

    $context = New-TestContext
    $context.mode='Observe';$context.timeout_seconds=420;$context.max_runs=8
    $script:DisabledRecoveryXml=$futureXml.Replace('<Enabled>true</Enabled>','<Enabled>false</Enabled>')
    $script:ObserveRecoveryXml=$futureXml
    $context.expected_disabled_xml_sha256=(Get-CutoverTaskXmlEvidence -Text $script:DisabledRecoveryXml).utf8_text_sha256
    $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryInstalls=0;$script:ObserveRecoveryDrains=0;$script:ObserveRecoveryCleanup=0
    Set-TestMock 'Assert-CutoverPinnedExecutables' {param($Context)}
    Set-TestMock 'Get-CutoverExactInstallerStatus' {param($Context,$ScriptPath,$ExpectedMode,$ExpectedTaskState) New-TestExactStatus -State $ExpectedTaskState}
    Set-TestMock 'Export-CutoverTaskXml' {if($script:ObserveRecoveryInstalled){$script:ObserveRecoveryXml}else{$script:DisabledRecoveryXml}}
    Set-TestMock 'Get-CutoverState' {param($Path) [pscustomobject]@{value=(New-TestState -Mode Observe -RunId ('a'*32) -Status no_eligible_order);checkpoint=[pscustomobject]@{length=1;sha256=('a'*64)}}}
    Set-TestMock 'Get-CutoverLogCheckpoint' {param($Path) [pscustomobject]@{length=1;sha256=('a'*64)}}
    Set-TestMock 'Assert-CutoverCurrentTerminalRun' {param($Context,$State,$ExactStatus,$LogCheckpoint,$ExpectedMode,$ExpectedStatus) [pscustomobject]@{run_id=('a'*32)}}
    Set-TestMock 'Get-CutoverProtectedSnapshot' {param($Context) 'protected-same'}
    Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' {param($Before,$Path,$MaximumBytes,$Code)}
    Set-TestMock 'Invoke-CutoverInstaller' {
        param($Context,$ScriptPath,$RequestedAction,$RequestedMode,$ExpectedCurrentTaskXmlSha256)
        if($RequestedAction -cne 'InstallFromDisabledNoStop' -or $RequestedMode -cne 'Observe' -or $ExpectedCurrentTaskXmlSha256 -cne (Get-CutoverTaskXmlEvidence -Text $script:DisabledRecoveryXml).utf8_text_sha256){throw 'RECOVERY_INSTALL_CONTRACT_CHANGED'}
        $script:ObserveRecoveryInstalls++;$script:ObserveRecoveryInstalled=$true
        New-TestInstallerStatusReceipt -Context $Context -Mode Observe
    }
    Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' {param($Context,$ExpectedUtf8TextSha256,$ExpectedUtf16LeBomSha256) if($ExpectedUtf8TextSha256 -cne (Get-CutoverTaskXmlEvidence -Text $script:DisabledRecoveryXml).utf8_text_sha256){throw 'BACKUP_PIN_CHANGED'}}
    Set-TestMock 'Invoke-CutoverDrainObserve' {
        param($Context,$Installer,$AllowInheritedTaskResult,$AdmittedBaseline)
        if($Context.mode -cne 'Observe' -or $Context.timeout_seconds -ne 420 -or $Context.natural_trigger_margin_seconds -ne 60 -or $Context.max_runs -ne 8){throw 'OBSERVE_DRAIN_CONTRACT_CHANGED'}
        if($null -eq $AdmittedBaseline -or $AdmittedBaseline.run_id -cne ('a'*32) -or $AdmittedBaseline.last_run_utc.Ticks -ne (New-TestExactStatus).last_run_utc.Ticks -or $AdmittedBaseline.state_checkpoint.sha256 -cne ('a'*64) -or $AdmittedBaseline.log_checkpoint.sha256 -cne ('a'*64)){throw 'OBSERVE_DRAIN_ADMISSION_NOT_BOUND'}
        $script:ObserveRecoveryDrains++
        [pscustomobject]@{status='OBSERVE_DRAINED';runs=2;final_run_id=('b'*32);final_worker_status='no_eligible_order';final_last_run_utc='2026-09-16T11:00:00.000Z';log_appended_bytes=100}
    }
    Set-TestMock 'Disable-CutoverTaskAfterFailure' {param($Context,$Installer,$ExpectedModes) $script:ObserveRecoveryCleanup++;$script:ObserveRecoveryInstalled=$false;[pscustomobject]@{mode='Observe';future_triggers_disabled=$true;definition_preserved_except_enabled=$true;task_stopped=$false}}
    $observeRecovery=Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context
    Assert-True 'disabled recovery installs only Observe via caller-pinned no-stop installer' ($script:ObserveRecoveryInstalls -eq 1 -and $observeRecovery.old_execute_task_not_enabled -and -not $observeRecovery.task_stopped -and $observeRecovery.candidate_install_action -ceq 'InstallFromDisabledNoStop')
    Assert-True 'disabled recovery requires bounded Observe drain ending no eligible' ($script:ObserveRecoveryDrains -eq 1 -and $observeRecovery.runs -eq 2 -and $observeRecovery.final_worker_status -ceq 'no_eligible_order' -and $observeRecovery.state_not_restored -and $observeRecovery.protected_fingerprints_unchanged)
    Assert-True 'disabled recovery receipt preserves exact disabled backup and future Observe definition' ($observeRecovery.authenticated_disabled_xml_sha256 -ceq $context.expected_disabled_xml_sha256 -and $observeRecovery.backup_matches_authenticated_disabled_xml -and $observeRecovery.state_and_log_preserved_through_install -and $observeRecovery.observe_start_boundary_utc -ceq '2099-01-01T00:00:00.0000000Z')
    $savedRecoveryStateMock = (Get-Item Function:Get-CutoverState).ScriptBlock
    $savedRecoveryStatusMock = (Get-Item Function:Get-CutoverExactInstallerStatus).ScriptBlock
    $savedRecoveryBackupMock = (Get-Item Function:Assert-CutoverBackupMatchesExpectedXml).ScriptBlock
    $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1
    Set-TestMock 'Get-CutoverState' {param($Path) $s=New-TestState -Mode Observe -RunId ('a'*32) -Status no_eligible_order;$s.success.event='candidate_observed';[pscustomobject]@{value=$s;checkpoint=[pscustomobject]@{length=1;sha256=('a'*64)}}}
    $beforeCorruptInstalls=$script:ObserveRecoveryInstalls
    Assert-ThrowsCode 'Observe recovery rejects state-only terminal corruption before installation' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'STATE_NO_ELIGIBLE_EVIDENCE_INVALID'
    Assert-True 'corrupt terminal evidence cannot install or drain' ($script:ObserveRecoveryInstalls -eq $beforeCorruptInstalls -and $script:ObserveRecoveryDrains -eq 1)
    Set-TestMock 'Get-CutoverState' {param($Path) [pscustomobject]@{value=(New-TestState -Mode Execute -RunId ('a'*32) -Status result_confirmed -WorkId WORK -RowId ROW);checkpoint=[pscustomobject]@{length=1;sha256=('a'*64)}}}
    $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1
    $beforeResultInstalls=$script:ObserveRecoveryInstalls
    Assert-ThrowsCode 'Observe-only recovery refuses result-confirmed baseline without target inputs' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'DISABLED_BASELINE_NOT_HEALTHY'
    Assert-True 'result-confirmed baseline cannot install or drain' ($script:ObserveRecoveryInstalls -eq $beforeResultInstalls -and $script:ObserveRecoveryDrains -eq 1)
    Set-TestMock 'Get-CutoverState' $savedRecoveryStateMock
    foreach($finalFailure in @('NATURAL_TRIGGER_WINDOW_UNAVAILABLE','TASK_CHANGED_BEFORE_OBSERVE_DRAIN')){
        $script:RecoveryBackupReadComplete=$false;$script:RecoveryFinalFailure=$finalFailure
        $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1
        Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' {param($Context,$ExpectedUtf8TextSha256,$ExpectedUtf16LeBomSha256) $script:RecoveryBackupReadComplete=$true}
        Set-TestMock 'Get-CutoverExactInstallerStatus' {param($Context,$ScriptPath,$ExpectedMode,$ExpectedTaskState)
            if($script:RecoveryBackupReadComplete -and $ExpectedTaskState -ceq 'Ready'){
                if($script:RecoveryFinalFailure -ceq 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'){New-TestExactStatus -Next ([DateTime]::UtcNow.AddSeconds(1))}
                else{New-TestExactStatus -Last (([DateTime]'2026-09-07T00:00:00Z').ToUniversalTime().AddSeconds(1))}
            }else{New-TestExactStatus -State $ExpectedTaskState}
        }
        Assert-ThrowsCode ('Observe recovery rechecks final handoff '+$finalFailure+' after backup') {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} $finalFailure
        Assert-True ('final handoff '+$finalFailure+' never delegates to drain') ($script:ObserveRecoveryDrains -eq 1 -and -not $script:ObserveRecoveryInstalled)
    }
    Set-TestMock 'Get-CutoverExactInstallerStatus' $savedRecoveryStatusMock
    Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' $savedRecoveryBackupMock
    $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryInstalls=1;$script:ObserveRecoveryDrains=1
    $script:ObserveRecoveryInstalled=$false
    $savedPin=$context.expected_disabled_xml_sha256;$context.expected_disabled_xml_sha256=('f'*64)
    Assert-ThrowsCode 'Observe recovery rejects wrong original disabled definition' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'DISABLED_TASK_XML_NOT_AUTHENTICATED'
    Assert-True 'wrong pin never installs or drains' ($script:ObserveRecoveryInstalls -eq 1 -and $script:ObserveRecoveryDrains -eq 1)
    $script:ObserveRecoveryInstalls=1;$script:ObserveRecoveryDrains=1;$script:ObserveRecoveryInstalled=$false
    $context.expected_disabled_xml_sha256=$savedPin
    foreach($driftCode in @('STATE_CHANGED_BEFORE_OBSERVE_RECOVERY','LOG_CHANGED_BEFORE_OBSERVE_RECOVERY','STATE_CHANGED_DURING_OBSERVE_RECOVERY','LOG_CHANGED_DURING_OBSERVE_RECOVERY')){
        $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1;$script:RecoveryDriftCode=$driftCode
        Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' {param($Before,$Path,$MaximumBytes,$Code) if($Code -ceq $script:RecoveryDriftCode){throw $Code}}
        Assert-ThrowsCode ('Observe recovery rejects '+$driftCode) {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} $driftCode
        Assert-True ($driftCode+' cannot reach drain and disables on failure') ($script:ObserveRecoveryDrains -eq 1 -and -not $script:ObserveRecoveryInstalled)
    }
    Set-TestMock 'Assert-CutoverFileCheckpointUnchanged' {param($Before,$Path,$MaximumBytes,$Code)}
    $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1
    Set-TestMock 'Get-CutoverExactInstallerStatus' {param($Context,$ScriptPath,$ExpectedMode,$ExpectedTaskState) if($ExpectedTaskState -ceq 'Ready'){New-TestExactStatus -State Ready -Last (([DateTime]'2026-09-07T00:00:00Z').ToUniversalTime().AddSeconds(1))}else{New-TestExactStatus -State Disabled}}
    Assert-ThrowsCode 'Observe recovery rejects a task run during definition replacement' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'TASK_RAN_DURING_OBSERVE_RECOVERY'
    Assert-True 'intervening task run never drains again' ($script:ObserveRecoveryDrains -eq 1 -and -not $script:ObserveRecoveryInstalled)
    Set-TestMock 'Get-CutoverExactInstallerStatus' {param($Context,$ScriptPath,$ExpectedMode,$ExpectedTaskState) New-TestExactStatus -State $ExpectedTaskState}
    foreach($protectedDriftStage in @(2,3,4)){
        $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1;$script:ObserveProtectedCalls=0;$script:ObserveProtectedDriftStage=$protectedDriftStage
        Set-TestMock 'Get-CutoverProtectedSnapshot' {param($Context) $script:ObserveProtectedCalls++;if($script:ObserveProtectedCalls -eq $script:ObserveProtectedDriftStage){'changed'}else{'protected-same'}}
        $protectedCode=switch($protectedDriftStage){2{'PROTECTED_CHANGED_BEFORE_OBSERVE_RECOVERY'};3{'PROTECTED_CHANGED_DURING_OBSERVE_RECOVERY'};4{'PROTECTED_FINGERPRINT_CHANGED'}}
        Assert-ThrowsCode ('Observe recovery rejects protected drift stage '+$protectedDriftStage) {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} $protectedCode
        Assert-True ('protected drift stage '+$protectedDriftStage+' never reports success and disables') (-not $script:ObserveRecoveryInstalled -and $script:ObserveRecoveryDrains -eq $(if($protectedDriftStage -eq 4){2}else{1}))
    }
    Set-TestMock 'Get-CutoverProtectedSnapshot' {param($Context) 'protected-same'}
    $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1
    $savedObserveXml=$script:ObserveRecoveryXml
    $script:ObserveRecoveryXml=$savedObserveXml.Replace('<Enabled>true</Enabled>','<Enabled>false</Enabled>')
    Assert-ThrowsCode 'Observe recovery rejects disabled post-registration definition' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'RECOVERY_OBSERVE_TASK_NOT_ENABLED'
    Assert-True 'disabled post-registration definition never drains' ($script:ObserveRecoveryDrains -eq 1 -and -not $script:ObserveRecoveryInstalled)
    $script:ObserveRecoveryXml=$savedObserveXml
    $script:ObserveRecoveryXml=$savedObserveXml.Replace('2099-01-01T00:00:00Z','2026-01-01T00:00:00Z')
    Assert-ThrowsCode 'Observe recovery refuses a past start boundary before drain' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'
    Assert-True 'past start boundary never drains and disables Observe' ($script:ObserveRecoveryDrains -eq 1 -and -not $script:ObserveRecoveryInstalled)
    $script:ObserveRecoveryInstalled=$false;$script:ObserveRecoveryDrains=1
    $script:ObserveRecoveryXml=$savedObserveXml
    Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' {param($Context,$ExpectedUtf8TextSha256,$ExpectedUtf16LeBomSha256) throw 'BACKUP_XML_HASH_MISMATCH'}
    Assert-ThrowsCode 'Observe recovery rejects unauthenticated backup before any drain' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'BACKUP_XML_HASH_MISMATCH'
    Assert-True 'backup failure never drains and disables Observe' ($script:ObserveRecoveryDrains -eq 1 -and -not $script:ObserveRecoveryInstalled)
    Set-TestMock 'Assert-CutoverBackupMatchesExpectedXml' {param($Context,$ExpectedUtf8TextSha256,$ExpectedUtf16LeBomSha256)}
    Set-TestMock 'Assert-CutoverCurrentTerminalRun' {param($Context,$State,$ExactStatus,$LogCheckpoint,$ExpectedMode,$ExpectedStatus) throw 'LOG_CURRENT_RUN_INVALID'}
    $installBaseline=$script:ObserveRecoveryInstalls
    Assert-ThrowsCode 'Observe recovery requires independently current baseline log and run' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'LOG_CURRENT_RUN_INVALID'
    Assert-True 'invalid baseline log never installs or drains' ($script:ObserveRecoveryInstalls -eq $installBaseline -and $script:ObserveRecoveryDrains -eq 1)
    Set-TestMock 'Assert-CutoverCurrentTerminalRun' {param($Context,$State,$ExactStatus,$LogCheckpoint,$ExpectedMode,$ExpectedStatus) [pscustomobject]@{run_id=('a'*32)}}
    Set-TestMock 'Get-CutoverState' {param($Path) [pscustomobject]@{value=(New-TestState -Mode Observe -RunId ('a'*32) -Status stale_order_ignored);checkpoint=[pscustomobject]@{length=1;sha256=('a'*64)}}}
    Assert-ThrowsCode 'Observe recovery refuses stale baseline without a terminal healthy current run' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'DISABLED_BASELINE_NOT_HEALTHY'
    Assert-True 'unhealthy baseline never installs or drains' ($script:ObserveRecoveryInstalls -eq $installBaseline -and $script:ObserveRecoveryDrains -eq 1)
    Set-TestMock 'Get-CutoverState' {param($Path) [pscustomobject]@{value=(New-TestState -Mode Observe -RunId ('a'*32) -Status no_eligible_order);checkpoint=[pscustomobject]@{length=1;sha256=('a'*64)}}}
    Set-TestMock 'Invoke-CutoverDrainObserve' {param($Context,$Installer) throw 'OBSERVE_CANDIDATE_REQUIRES_EXTERNAL_RESOLUTION'}
    Assert-ThrowsCode 'fresh unrelated candidate is observed not executed and requires resolution' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'OBSERVE_CANDIDATE_REQUIRES_EXTERNAL_RESOLUTION'
    Assert-True 'unrelated fresh candidate failure disables Observe and never claims success' (-not $script:ObserveRecoveryInstalled)
    $context.mode='Execute'
    Assert-ThrowsCode 'direct recovery invocation rejects Execute mode before installer' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'ACTION_REQUIRES_OBSERVE_MODE'
    $context.mode='Observe';$context.expected_work_id='ALREADY-PRUNED-WORK'
    Assert-ThrowsCode 'direct recovery cannot replay a target even with pruned durable work' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY'
    $context.expected_work_id='';$context.expected_row_id='99999999-9999-4999-8999-999999999999'
    Assert-ThrowsCode 'direct recovery forbids board-only receipt target identities' {Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context} 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY'
    Reset-TestMocks

    $env:ProgramData=$fakeProgramData
    $recoveryValues=$contextValues.Clone()
    $recoveryValues.InstallerPath=$wiredIncidentContext.installer_path
    $recoveryValues.ExpectedReleaseId=$wiredIncidentContext.release_id
    $recoveryValues.ExpectedInstallerSha256=(Get-FileHash -LiteralPath $wiredIncidentContext.installer_path -Algorithm SHA256).Hash.ToLowerInvariant()
    $recoveryValues.Mode='Observe';$recoveryValues.ExpectedCurrentTaskResult='';$recoveryValues.ExpectedCurrentFailureCode='';$recoveryValues.ExpectedCurrentRunId=''
    $recoveryValues.ExpectedTerminalStatus='';$recoveryValues.ExpectedWorkId='';$recoveryValues.ExpectedRowId='';$recoveryValues.ExpectedResultStatus=''
    $recoveryValues.ExpectedDisabledXmlSha256=('a'*64);$recoveryValues.TimeoutSeconds='420'
    $wiredRecovery=New-CutoverContext -RequestedAction InstallObserveAndDrainFromDisabledExecute -Values $recoveryValues
    Assert-True 'new recovery context binds exact disabled pin and Observe-only full allowance' ($wiredRecovery.expected_disabled_xml_sha256 -ceq ('a'*64) -and $wiredRecovery.mode -ceq 'Observe' -and $wiredRecovery.timeout_seconds -eq 420)
    $badRecovery=$recoveryValues.Clone();$badRecovery.ExpectedDisabledXmlSha256=('A'*64)
    Assert-ThrowsCode 'new recovery context rejects malformed disabled pin' {New-CutoverContext -RequestedAction InstallObserveAndDrainFromDisabledExecute -Values $badRecovery} 'EXPECTED_DISABLED_XML_SHA256_INVALID'
    $badRecovery=$recoveryValues.Clone();$badRecovery.Mode='Execute'
    Assert-ThrowsCode 'new recovery context rejects Execute before mutation' {New-CutoverContext -RequestedAction InstallObserveAndDrainFromDisabledExecute -Values $badRecovery} 'ACTION_REQUIRES_OBSERVE_MODE'
    $badRecovery=$recoveryValues.Clone();$badRecovery.ExpectedWorkId='EXPIRED-OR-PRUNED'
    Assert-ThrowsCode 'new recovery context forbids any dispatch target replay' {New-CutoverContext -RequestedAction InstallObserveAndDrainFromDisabledExecute -Values $badRecovery} 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY'
    $badRecovery=$recoveryValues.Clone();$badRecovery.TimeoutSeconds='780'
    Assert-ThrowsCode 'new recovery context rejects impossible interval window' {New-CutoverContext -RequestedAction InstallObserveAndDrainFromDisabledExecute -Values $badRecovery} 'OBSERVE_RECOVERY_WINDOW_CANNOT_FIT_INTERVAL'
    Assert-ThrowsCode 'normal Execute start rejects disabled recovery pin' {New-CutoverContext -RequestedAction StartAndAwait -Values $recoveryValues} 'ACTION_REQUIRES_EXECUTE_MODE'
    $badRecovery=$recoveryValues.Clone();$badRecovery.Mode='Execute';$badRecovery.ExpectedTerminalStatus='no_eligible_order'
    Assert-ThrowsCode 'normal Execute no eligible start rejects disabled recovery pin' {New-CutoverContext -RequestedAction StartAndAwait -Values $badRecovery} 'DISABLED_RECOVERY_INPUTS_ACTION_MISMATCH'
    $env:ProgramData=$oldProgramData

    # Main workflow wiring must invoke this suite on Windows PowerShell 5.1.
    $workflow = [IO.File]::ReadAllText((Join-Path $repoRoot '.github\workflows\order-acceptance.yml'))
    # This assertion becomes true after the workflow patch below and protects it
    # from later path-filter/test-step regression.
    Assert-True 'workflow includes cutover driver test path' ($workflow.Contains('tests/test_order_cutover_phase.ps1'))
    Assert-True 'workflow includes cutover driver source path' ($workflow.Contains('infra/azure/order_cutover_phase.ps1'))
} finally {
    Reset-TestMocks
    if ($null -ne $oldProgramData) { $env:ProgramData = $oldProgramData }
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $temporaryRoot)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
