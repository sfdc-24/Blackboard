<#
.SYNOPSIS
    Keeps the Blackboard coordinator VM and its bounded ORDER worker alive.

.DESCRIPTION
    This runbook runs outside the coordinator VM under an Azure Automation
    system-assigned managed identity. It starts the VM when needed, then uses
    Azure Run Command to inspect the Windows scheduled task. A stale, idle task
    is started once. No Blackboard credentials are stored in this runbook.

    Schedule this runbook using two hourly schedules offset by 30 minutes. The
    guest task remains the primary 15-minute trigger; this is the independent
    recovery supervisor.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidatePattern('^[0-9a-fA-F-]{36}$')]
    [string] $SubscriptionId = '604c029c-c254-4bc4-b173-05d6e5c3cab0',

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^[A-Za-z0-9._()\-]+$')]
    [string] $ResourceGroupName = 'COPILOT-DEV-RG',

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^[A-Za-z0-9._\-]+$')]
    [string] $VmName = 'AkatiaVM-regular',

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^[A-Za-z0-9 ._()\-]+$')]
    [string] $TaskName = 'SFDC24 Blackboard Order Worker',

    [Parameter(Mandatory = $false)]
    [ValidateRange(15, 240)]
    [int] $StaleAfterMinutes = 30,

    [Parameter(Mandatory = $false)]
    [ValidateRange(30, 480)]
    [int] $HungAfterMinutes = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-SupervisorResult {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable] $Result
    )

    $Result['observed_at_utc'] = [DateTime]::UtcNow.ToString('o')
    $Result | ConvertTo-Json -Depth 8 -Compress | Write-Output
}

function Get-VmReadinessState {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Vm
    )

    $provisioningStatus = $Vm.Statuses |
        Where-Object { [string]$_.Code -like 'ProvisioningState/*' } |
        Select-Object -First 1
    $powerStatus = $Vm.Statuses |
        Where-Object { [string]$_.Code -like 'PowerState/*' } |
        Select-Object -First 1

    $agentStatus = $null
    $vmAgentProperty = $Vm.PSObject.Properties['VMAgent']
    if ($null -ne $vmAgentProperty -and $null -ne $vmAgentProperty.Value) {
        $agentStatus = $vmAgentProperty.Value.Statuses |
            Where-Object { [string]$_.Code -like 'ProvisioningState/*' } |
            Select-Object -First 1
    }

    $provisioningState = if ($null -ne $provisioningStatus) { [string]$provisioningStatus.Code } else { '' }
    $powerState = if ($null -ne $powerStatus) { [string]$powerStatus.Code } else { '' }
    $guestAgentState = if ($null -ne $agentStatus) { [string]$agentStatus.Code } else { '' }

    [pscustomobject][ordered]@{
        ready = (
            $powerState -ceq 'PowerState/running' -and
            $provisioningState -ceq 'ProvisioningState/succeeded' -and
            $guestAgentState -ceq 'ProvisioningState/succeeded'
        )
        power_state = $powerState
        provisioning_state = $provisioningState
        guest_agent_state = $guestAgentState
    }
}

function Get-VmReadinessFailureStatus {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Readiness
    )

    if ([string]$Readiness.power_state -cne 'PowerState/running') {
        return 'VM_NOT_RUNNING'
    }
    if ([string]$Readiness.provisioning_state -cne 'ProvisioningState/succeeded') {
        return 'VM_PROVISIONING_NOT_READY'
    }
    if ([string]$Readiness.guest_agent_state -cne 'ProvisioningState/succeeded') {
        return 'VM_AGENT_NOT_READY'
    }
    return $null
}

function Test-GuestSupervisorResult {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object] $GuestJson,

        [Parameter(Mandatory = $true)]
        [string] $ExpectedTaskName
    )

    if ($null -eq $GuestJson) {
        return [pscustomobject][ordered]@{ valid = $false; reason = 'guest_json_missing'; status = '' }
    }

    $schemaProperty = $GuestJson.PSObject.Properties['schema_version']
    if ($null -eq $schemaProperty -or
        $schemaProperty.Value -isnot [string] -or
        [string]$schemaProperty.Value -cne 'blackboard.worker-supervision.v0.1') {
        return [pscustomobject][ordered]@{ valid = $false; reason = 'schema_version_invalid'; status = '' }
    }

    $taskNameProperty = $GuestJson.PSObject.Properties['task_name']
    if ($null -eq $taskNameProperty -or
        $taskNameProperty.Value -isnot [string] -or
        [string]$taskNameProperty.Value -cne $ExpectedTaskName) {
        return [pscustomobject][ordered]@{ valid = $false; reason = 'task_name_invalid'; status = '' }
    }

    $hostNameProperty = $GuestJson.PSObject.Properties['host_name']
    if ($null -eq $hostNameProperty -or
        $hostNameProperty.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$hostNameProperty.Value)) {
        return [pscustomobject][ordered]@{ valid = $false; reason = 'host_name_invalid'; status = '' }
    }

    $statusProperty = $GuestJson.PSObject.Properties['status']
    $allowedStatuses = @(
        'TASK_MISSING',
        'TASK_AMBIGUOUS',
        'TASK_UNMANAGED',
        'TASK_DISABLED',
        'TASK_RUNNING',
        'TASK_HUNG',
        'TASK_FAILED',
        'TASK_HEALTHY',
        'TASK_RECOVERY_STARTED',
        'TASK_RECOVERED',
        'TASK_RECOVERY_FAILED'
    )
    if ($null -eq $statusProperty -or
        $statusProperty.Value -isnot [string] -or
        $allowedStatuses -cnotcontains [string]$statusProperty.Value) {
        return [pscustomobject][ordered]@{ valid = $false; reason = 'status_invalid'; status = '' }
    }

    return [pscustomobject][ordered]@{
        valid = $true
        reason = ''
        status = [string]$statusProperty.Value
    }
}

Disable-AzContextAutosave -Scope Process | Out-Null
$identityContext = (Connect-AzAccount -Identity).Context
$azureContext = Set-AzContext -SubscriptionId $SubscriptionId -DefaultProfile $identityContext

$vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VmName -Status -DefaultProfile $azureContext
$powerState = ($vm.Statuses | Where-Object Code -Like 'PowerState/*' | Select-Object -First 1).Code
$startedVm = $false

if ($powerState -ne 'PowerState/running') {
    Start-AzVM -ResourceGroupName $ResourceGroupName -Name $VmName -DefaultProfile $azureContext | Out-Null
    $startedVm = $true
    $vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VmName -Status -DefaultProfile $azureContext
    $powerState = ($vm.Statuses | Where-Object Code -Like 'PowerState/*' | Select-Object -First 1).Code
}

$vmReadyTimeoutSeconds = 180
$vmReadyPollSeconds = 10
$readinessDeadline = [DateTime]::UtcNow.AddSeconds($vmReadyTimeoutSeconds)
$readiness = Get-VmReadinessState -Vm $vm
while (-not $readiness.ready -and [DateTime]::UtcNow -lt $readinessDeadline) {
    $remainingSeconds = [int][Math]::Ceiling(($readinessDeadline - [DateTime]::UtcNow).TotalSeconds)
    if ($remainingSeconds -le 0) { break }
    Start-Sleep -Seconds ([Math]::Min($vmReadyPollSeconds, $remainingSeconds))
    $vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VmName -Status -DefaultProfile $azureContext
    $readiness = Get-VmReadinessState -Vm $vm
}

$powerState = [string]$readiness.power_state
$readinessFailureStatus = Get-VmReadinessFailureStatus -Readiness $readiness
if ($readinessFailureStatus) {
    Write-SupervisorResult -Result @{
        schema_version = 'blackboard.supervisor.v0.1'
        status = $readinessFailureStatus
        vm_name = $VmName
        power_state = $powerState
        provisioning_state = [string]$readiness.provisioning_state
        guest_agent_state = [string]$readiness.guest_agent_state
        vm_start_attempted = $startedVm
        readiness_timeout_seconds = $vmReadyTimeoutSeconds
    }
    throw "VM '$VmName' did not become ready for Run Command: $readinessFailureStatus."
}

$escapedTaskName = $TaskName.Replace("'", "''")
$guestScript = @"
`$ErrorActionPreference = 'Stop'
`$taskName = '$escapedTaskName'
`$taskPath = '\'
`$managedMarker = 'managed-by=install_order_supervisor.ps1; schema=v1'
`$staleAfterMinutes = $StaleAfterMinutes
`$hungAfterMinutes = $HungAfterMinutes
`$matches = @(Get-ScheduledTask -TaskName `$taskName -ErrorAction SilentlyContinue)

if (`$matches.Count -eq 0) {
    [ordered]@{
        schema_version = 'blackboard.worker-supervision.v0.1'
        status = 'TASK_MISSING'
        task_name = `$taskName
        host_name = `$env:COMPUTERNAME
        observed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    exit 3
}

if (`$matches.Count -ne 1 -or `$matches[0].TaskPath -cne `$taskPath) {
    [ordered]@{
        schema_version = 'blackboard.worker-supervision.v0.1'
        status = 'TASK_AMBIGUOUS'
        task_name = `$taskName
        match_count = `$matches.Count
        host_name = `$env:COMPUTERNAME
        observed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    exit 7
}

`$task = `$matches[0]
if (-not ([string]`$task.Description).Contains(`$managedMarker)) {
    [ordered]@{
        schema_version = 'blackboard.worker-supervision.v0.1'
        status = 'TASK_UNMANAGED'
        task_name = `$taskName
        task_path = [string]`$task.TaskPath
        host_name = `$env:COMPUTERNAME
        observed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    exit 8
}

`$info = Get-ScheduledTaskInfo -TaskName `$taskName -TaskPath `$taskPath
`$now = Get-Date
`$hasRun = `$info.LastRunTime -gt [DateTime]'2000-01-01T00:00:00Z'
`$ageMinutes = if (`$hasRun) { (`$now - `$info.LastRunTime).TotalMinutes } else { [double]::PositiveInfinity }
`$lastResult = [int64]`$info.LastTaskResult
`$lastResultHex = '0x' + [Convert]::ToString((`$lastResult -band [int64]4294967295), 16).PadLeft(8, '0')
`$hasNextRun = `$info.NextRunTime -gt [DateTime]'2000-01-01T00:00:00Z'
`$nextRunMissed = `$hasNextRun -and `$info.NextRunTime -lt `$now.AddMinutes(-5)
`$preStartLastRunTime = `$info.LastRunTime
`$startRequested = `$false
`$startReason = ''

if (`$task.State -eq 'Disabled') {
    [ordered]@{
        schema_version = 'blackboard.worker-supervision.v0.1'
        status = 'TASK_DISABLED'
        task_name = `$taskName
        task_state = [string]`$task.State
        last_task_result = `$lastResult
        last_task_result_hex = `$lastResultHex
        host_name = `$env:COMPUTERNAME
        observed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    exit 4
}

if (`$task.State -eq 'Running') {
    `$status = if (`$hasRun -and `$ageMinutes -ge `$hungAfterMinutes) { 'TASK_HUNG' } else { 'TASK_RUNNING' }
    [ordered]@{
        schema_version = 'blackboard.worker-supervision.v0.1'
        status = `$status
        task_name = `$taskName
        task_state = [string]`$task.State
        last_run_time = `$info.LastRunTime.ToUniversalTime().ToString('o')
        last_run_age_minutes = [Math]::Round(`$ageMinutes, 2)
        last_task_result = `$lastResult
        last_task_result_hex = `$lastResultHex
        next_run_time = `$info.NextRunTime.ToUniversalTime().ToString('o')
        start_requested = `$false
        host_name = `$env:COMPUTERNAME
        observed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    if (`$status -eq 'TASK_HUNG') { exit 5 }
    exit 0
}

if (`$hasRun -and `$lastResult -ne 0) {
    [ordered]@{
        schema_version = 'blackboard.worker-supervision.v0.1'
        status = 'TASK_FAILED'
        task_name = `$taskName
        task_path = [string]`$task.TaskPath
        task_state = [string]`$task.State
        last_run_time = `$info.LastRunTime.ToUniversalTime().ToString('o')
        last_run_age_minutes = [Math]::Round(`$ageMinutes, 2)
        last_task_result = `$lastResult
        last_task_result_hex = `$lastResultHex
        next_run_time = `$info.NextRunTime.ToUniversalTime().ToString('o')
        start_requested = `$false
        host_name = `$env:COMPUTERNAME
        observed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress
    exit 9
} elseif (-not `$hasRun) {
    `$startReason = 'never_run'
} elseif (`$ageMinutes -ge `$staleAfterMinutes) {
    `$startReason = 'stale'
} elseif (`$nextRunMissed) {
    `$startReason = 'next_run_missed'
}

if (`$startReason) {
    Start-ScheduledTask -TaskName `$taskName -TaskPath `$taskPath
    `$startRequested = `$true
    Start-Sleep -Seconds 8
    `$task = Get-ScheduledTask -TaskName `$taskName -TaskPath `$taskPath
    `$info = Get-ScheduledTaskInfo -TaskName `$taskName -TaskPath `$taskPath
    `$hasRun = `$info.LastRunTime -gt [DateTime]'2000-01-01T00:00:00Z'
    `$ageMinutes = if (`$hasRun) { ((Get-Date) - `$info.LastRunTime).TotalMinutes } else { [double]::PositiveInfinity }
    `$lastResult = [int64]`$info.LastTaskResult
    `$lastResultHex = '0x' + [Convert]::ToString((`$lastResult -band [int64]4294967295), 16).PadLeft(8, '0')
}

`$status = if (-not `$startRequested) {
    'TASK_HEALTHY'
} elseif (`$task.State -eq 'Running') {
    'TASK_RECOVERY_STARTED'
} elseif (`$info.LastRunTime -gt `$preStartLastRunTime -and `$info.LastTaskResult -eq 0) {
    'TASK_RECOVERED'
} else {
    'TASK_RECOVERY_FAILED'
}

[ordered]@{
    schema_version = 'blackboard.worker-supervision.v0.1'
    status = `$status
    task_name = `$taskName
    task_state = [string]`$task.State
    last_run_time = `$info.LastRunTime.ToUniversalTime().ToString('o')
    last_run_age_minutes = if (`$hasRun) { [Math]::Round(`$ageMinutes, 2) } else { `$null }
    last_task_result = `$info.LastTaskResult
    last_task_result_hex = `$lastResultHex
    next_run_time = `$info.NextRunTime.ToUniversalTime().ToString('o')
    start_requested = `$startRequested
    start_reason = `$startReason
    host_name = `$env:COMPUTERNAME
    observed_at_utc = [DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json -Compress
if (`$status -eq 'TASK_RECOVERY_FAILED') { exit 6 }
"@

try {
    $runCommand = Invoke-AzVMRunCommand `
        -ResourceGroupName $ResourceGroupName `
        -VMName $VmName `
        -CommandId 'RunPowerShellScript' `
        -ScriptString $guestScript `
        -DefaultProfile $azureContext

}
catch {
    $runCommandError = $_.Exception.Message
    if ($runCommandError -match 'Run command extension execution is in progress' -or
        $runCommandError -match '(?<!\d)409(?!\d)') {
        Write-SupervisorResult -Result @{
            schema_version = 'blackboard.supervisor.v0.1'
            status = 'RUN_COMMAND_BUSY'
            vm_name = $VmName
            power_state = $powerState
            vm_start_attempted = $startedVm
            guest_status = 'RUN_COMMAND_BUSY'
            retry_policy = 'next_scheduled_run'
        }
        throw
    }
    Write-SupervisorResult -Result @{
        schema_version = 'blackboard.supervisor.v0.1'
        status = 'RUN_COMMAND_FAILED'
        vm_name = $VmName
        power_state = $powerState
        vm_start_attempted = $startedVm
        error = $runCommandError
    }
    throw
}

$messages = @($runCommand.Value | ForEach-Object Message | Where-Object { $_ })
$guestOutput = ($messages -join "`n").Trim()
$guestJson = $null
foreach ($line in ($guestOutput -split "`r?`n")) {
    $candidate = $line.Trim()
    if ($candidate.StartsWith('{') -and $candidate.EndsWith('}')) {
        try {
            $guestJson = $candidate | ConvertFrom-Json
        }
        catch {
            # Continue scanning because Run Command may mix status text with output.
        }
    }
}

$guestValidation = Test-GuestSupervisorResult -GuestJson $guestJson -ExpectedTaskName $TaskName
$guestStatus = if ($guestValidation.valid) {
    [string]$guestValidation.status
} elseif ($null -eq $guestJson) {
    'UNPARSEABLE'
} else {
    'GUEST_SCHEMA_INVALID'
}
$outerStatus = if ($guestValidation.valid -and @('TASK_HEALTHY', 'TASK_RUNNING', 'TASK_RECOVERED') -ccontains $guestStatus) {
    'PASS'
} elseif ($guestValidation.valid -and $guestStatus -ceq 'TASK_RECOVERY_STARTED') {
    'RECOVERY_PENDING'
} else {
    'FAIL'
}

Write-SupervisorResult -Result @{
    schema_version = 'blackboard.supervisor.v0.1'
    status = $outerStatus
    vm_name = $VmName
    power_state = $powerState
    vm_start_attempted = $startedVm
    guest_status = $guestStatus
    guest_validation_error = if ($guestValidation.valid) { $null } else { [string]$guestValidation.reason }
    guest = $guestJson
    raw_guest_output = if ($null -eq $guestJson) { $guestOutput } else { $null }
}

if ($outerStatus -eq 'FAIL') {
    throw "Guest worker supervision returned '$guestStatus'."
}
