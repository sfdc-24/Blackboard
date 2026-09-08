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
        last_run_utc = $Last
        next_run_utc = $Next
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
        [string]$PollAt = '2026-09-07T00:00:00.000Z'
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
        error = $null
        counts = [pscustomobject]@{
            polls = 1; seen = 1; selected = $(if ($WorkId) { 1 } else { 0 })
            succeeded = $(if ($Status -ceq 'result_confirmed' -and $ResultStatus -cne 'failed') { 1 } else { 0 })
            errors = 0; ignored = 0
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

    $quoted = ConvertTo-CutoverCommandLineArgument -Value 'C:\A path\file.ps1'
    Assert-True 'native argument quoting wraps spaces' ($quoted -ceq '"C:\A path\file.ps1"')
    Assert-ThrowsCode 'single JSON parser rejects multiple records' {
        ConvertFrom-CutoverSingleJsonLine -Text "{}`n{}" -Code 'CHILD_JSON_INVALID' | Out-Null
    } 'CHILD_JSON_INVALID'
    Assert-ThrowsCode 'single JSON parser rejects duplicate object keys' {
        ConvertFrom-CutoverSingleJsonLine -Text '{"ok":true,"ok":false}' -Code 'CHILD_JSON_INVALID' | Out-Null
    } 'CHILD_JSON_INVALID'

    # Static surface: the driver itself has no cloud/bus transport and owns no
    # task register/stop path.  Restore and Install remain inside pinned tools.
    $source = [IO.File]::ReadAllText($driverPath)
    foreach ($forbidden in @('Invoke-RestMethod', 'Invoke-WebRequest', 'az rest', 'bus.ps1', 'Register-ScheduledTask', 'Stop-ScheduledTask')) {
        Assert-True ('driver excludes direct surface ' + $forbidden) ($source.IndexOf($forbidden, [StringComparison]::OrdinalIgnoreCase) -lt 0)
    }
    Assert-True 'driver exposes all six bounded actions' (@(
        'ValidateEscrowAndDisable', 'DrainObserve', 'InstallObserveAndDrain', 'InstallExecuteReady', 'RestoreReady', 'StartAndAwait' |
            Where-Object { $source.IndexOf($_, [StringComparison]::Ordinal) -ge 0 }
    ).Count -eq 6)
    Assert-True 'receipt byte cap is literal 3072' ($source.Contains('$script:CutoverReceiptMaximumBytes = 3072'))
    Assert-True 'log and protected inputs have finite byte caps' (
        $source.Contains('$script:CutoverLogMaximumBytes = 67108864') -and
        $source.Contains('$script:CutoverProtectedTreeMaximumBytes = 536870912')
    )

    # Path/hash binding uses the exact ProgramData identities for both tools.
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

    # Exercise the real escrow child-receipt gate: the tool path is pinned by
    # context and the receipt must also bind the exact acceptance directory.
    Reset-TestMocks
    $escrowContext = New-TestContext
    $escrowContext.escrow_tool_path = $toolPath
    $escrowContext.expected_escrow_tool_sha256 = $toolSha
    $script:ChildStdout = (New-TestEscrowReceipt | ConvertTo-Json -Compress)
    Set-TestMock 'Invoke-CutoverChildScript' {
        param($ScriptPath, $Arguments, $TimeoutSeconds, $FailureCode)
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
    Assert-True 'candidate and restored installer children recheck their distinct pins' (
        [bool]$candidateChild.ok -and [bool]$restoredChild.ok
    )
    [IO.File]::WriteAllBytes($installerPath, [Text.Encoding]::UTF8.GetBytes('changed-installer'))
    Assert-ThrowsCode 'candidate installer child rechecks pinned digest immediately before execution' {
        Invoke-CutoverInstaller -Context $installerContext -ScriptPath $installerPath -RequestedAction Status -RequestedMode Observe | Out-Null
    } 'INSTALLER_SHA256_MISMATCH'
    [IO.File]::WriteAllBytes($restoredInstallerPath, [Text.Encoding]::UTF8.GetBytes('changed-restored-installer'))
    Assert-ThrowsCode 'restored installer child rechecks pinned digest immediately before execution' {
        Invoke-CutoverInstaller -Context $installerContext -ScriptPath $restoredInstallerPath -RequestedAction Status -RequestedMode Execute | Out-Null
    } 'RESTORED_INSTALLER_SHA256_MISMATCH'
    Reset-TestMocks

    # XML evidence explicitly separates UTF-8 text hashes from the escrow's
    # UTF-16LE-with-BOM file representation.
    $xmlEnabled = '<?xml version="1.0"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings><Enabled>true</Enabled></Settings></Task>'
    $xmlDisabled = $xmlEnabled.Replace('<Enabled>true</Enabled>', '<Enabled>false</Enabled>')
    $enabledEvidence = Get-CutoverTaskXmlEvidence -Text $xmlEnabled
    $disabledEvidence = Get-CutoverTaskXmlEvidence -Text $xmlDisabled
    Assert-True 'task XML detects enabled and disabled states' ($enabledEvidence.enabled -and -not $disabledEvidence.enabled)
    Assert-True 'normalization proves only Enabled changed' ($enabledEvidence.normalized_sha256 -ceq $disabledEvidence.normalized_sha256)
    Assert-True 'UTF8 text and UTF16 BOM XML hashes remain distinct' ($enabledEvidence.utf8_text_sha256 -cne $enabledEvidence.utf16le_bom_sha256)

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
