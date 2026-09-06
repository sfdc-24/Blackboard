#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunbookPath = Join-Path $RepoRoot 'infra\azure\blackboard_vm_supervisor.ps1'
$script:Passed = 0
$script:Failed = 0

function Assert-True {
    param(
        [string] $Name,
        [bool] $Condition,
        [string] $Detail = ''
    )

    if ($Condition) {
        $script:Passed++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Failed++
        Write-Output ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
    }
}

$tokens = $null
$parseErrors = $null
$runbookAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $RunbookPath,
    [ref]$tokens,
    [ref]$parseErrors
)
Assert-True `
    -Name 'runbook parses under Windows PowerShell 5.1' `
    -Condition (@($parseErrors).Count -eq 0) `
    -Detail ((@($parseErrors | ForEach-Object Message)) -join '; ')

$helperNames = @(
    'Get-VmReadinessState',
    'Get-VmReadinessFailureStatus',
    'Test-GuestSupervisorResult'
)
$helperDefinitions = New-Object System.Collections.Generic.List[string]
foreach ($helperName in $helperNames) {
    $matches = @($runbookAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
    }, $true) | Where-Object { $_.Name -ceq $helperName })
    Assert-True -Name ('runbook defines ' + $helperName) -Condition ($matches.Count -eq 1)
    if ($matches.Count -eq 1) {
        $helperDefinitions.Add($matches[0].Extent.Text)
    }
}

if ($helperDefinitions.Count -ne $helperNames.Count) {
    Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
    exit 1
}

. ([scriptblock]::Create(($helperDefinitions -join [Environment]::NewLine)))

function New-FakeVm {
    param(
        [string] $PowerState = 'PowerState/running',
        [string] $ProvisioningState = 'ProvisioningState/succeeded',
        [AllowNull()]
        [string] $GuestAgentState = 'ProvisioningState/succeeded'
    )

    $vmAgent = if ($null -eq $GuestAgentState) {
        $null
    } else {
        [pscustomobject]@{
            Statuses = @([pscustomobject]@{ Code = $GuestAgentState })
        }
    }

    return [pscustomobject]@{
        Statuses = @(
            [pscustomobject]@{ Code = $ProvisioningState },
            [pscustomobject]@{ Code = $PowerState }
        )
        VMAgent = $vmAgent
    }
}

$ready = Get-VmReadinessState -Vm (New-FakeVm)
Assert-True 'ready VM is accepted' (
    $ready.ready -and
    $ready.power_state -ceq 'PowerState/running' -and
    $ready.provisioning_state -ceq 'ProvisioningState/succeeded' -and
    $ready.guest_agent_state -ceq 'ProvisioningState/succeeded'
)
Assert-True 'ready VM has no failure status' ($null -eq (Get-VmReadinessFailureStatus -Readiness $ready))

$stopped = Get-VmReadinessState -Vm (New-FakeVm -PowerState 'PowerState/deallocated')
Assert-True 'non-running VM is rejected distinctly' (
    -not $stopped.ready -and
    (Get-VmReadinessFailureStatus -Readiness $stopped) -ceq 'VM_NOT_RUNNING'
)

$updating = Get-VmReadinessState -Vm (New-FakeVm -ProvisioningState 'ProvisioningState/updating')
Assert-True 'updating VM is rejected before Run Command' (
    -not $updating.ready -and
    (Get-VmReadinessFailureStatus -Readiness $updating) -ceq 'VM_PROVISIONING_NOT_READY'
)

$agentNotReady = Get-VmReadinessState -Vm (New-FakeVm -GuestAgentState 'ProvisioningState/unavailable')
Assert-True 'not-ready Guest Agent is rejected distinctly' (
    -not $agentNotReady.ready -and
    (Get-VmReadinessFailureStatus -Readiness $agentNotReady) -ceq 'VM_AGENT_NOT_READY'
)

$agentMissing = Get-VmReadinessState -Vm (New-FakeVm -GuestAgentState $null)
Assert-True 'missing Guest Agent status is rejected distinctly' (
    -not $agentMissing.ready -and
    (Get-VmReadinessFailureStatus -Readiness $agentMissing) -ceq 'VM_AGENT_NOT_READY'
)

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
foreach ($status in $allowedStatuses) {
    $candidate = [pscustomobject][ordered]@{
        schema_version = 'blackboard.worker-supervision.v0.1'
        status = $status
        task_name = 'SFDC24 Blackboard Order Worker'
        host_name = 'AKATIA-TEST'
    }
    $validation = Test-GuestSupervisorResult `
        -GuestJson $candidate `
        -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
    Assert-True ('allowlisted guest status ' + $status) (
        $validation.valid -and $validation.status -ceq $status
    )
}

$validGuest = [pscustomobject][ordered]@{
    schema_version = 'blackboard.worker-supervision.v0.1'
    status = 'TASK_HEALTHY'
    task_name = 'SFDC24 Blackboard Order Worker'
    host_name = 'AKATIA-TEST'
}

$missingGuest = Test-GuestSupervisorResult -GuestJson $null -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'missing guest JSON is rejected' (-not $missingGuest.valid -and $missingGuest.reason -ceq 'guest_json_missing')

$wrongSchema = $validGuest.PSObject.Copy()
$wrongSchema.schema_version = 'blackboard.worker-supervision.v0.2'
$validation = Test-GuestSupervisorResult -GuestJson $wrongSchema -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'wrong guest schema is rejected' (-not $validation.valid -and $validation.reason -ceq 'schema_version_invalid')

$wrongTask = $validGuest.PSObject.Copy()
$wrongTask.task_name = 'Different Task'
$validation = Test-GuestSupervisorResult -GuestJson $wrongTask -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'wrong guest task identity is rejected' (-not $validation.valid -and $validation.reason -ceq 'task_name_invalid')

$wrongTaskCase = $validGuest.PSObject.Copy()
$wrongTaskCase.task_name = 'sfdc24 blackboard order worker'
$validation = Test-GuestSupervisorResult -GuestJson $wrongTaskCase -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'guest task identity matching is case exact' (-not $validation.valid -and $validation.reason -ceq 'task_name_invalid')

$blankHost = $validGuest.PSObject.Copy()
$blankHost.host_name = '   '
$validation = Test-GuestSupervisorResult -GuestJson $blankHost -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'blank guest host identity is rejected' (-not $validation.valid -and $validation.reason -ceq 'host_name_invalid')

$unknownStatus = $validGuest.PSObject.Copy()
$unknownStatus.status = 'TASK_MAGIC'
$validation = Test-GuestSupervisorResult -GuestJson $unknownStatus -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'unknown guest status is rejected' (-not $validation.valid -and $validation.reason -ceq 'status_invalid')

$wrongCaseStatus = $validGuest.PSObject.Copy()
$wrongCaseStatus.status = 'task_healthy'
$validation = Test-GuestSupervisorResult -GuestJson $wrongCaseStatus -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'guest status matching is case exact' (-not $validation.valid -and $validation.reason -ceq 'status_invalid')

$arrayStatus = $validGuest.PSObject.Copy()
$arrayStatus.status = @('TASK_HEALTHY')
$validation = Test-GuestSupervisorResult -GuestJson $arrayStatus -ExpectedTaskName 'SFDC24 Blackboard Order Worker'
Assert-True 'non-scalar guest status is rejected' (-not $validation.valid -and $validation.reason -ceq 'status_invalid')

$runbookText = [IO.File]::ReadAllText($RunbookPath, [Text.Encoding]::UTF8)
$readinessGateIndex = $runbookText.IndexOf('$readinessFailureStatus = Get-VmReadinessFailureStatus', [StringComparison]::Ordinal)
$runCommandIndex = $runbookText.IndexOf('Invoke-AzVMRunCommand', [StringComparison]::Ordinal)
Assert-True 'VM readiness gate precedes Action Run Command' (
    $readinessGateIndex -ge 0 -and
    $runCommandIndex -gt $readinessGateIndex
)

$guestValidationIndex = $runbookText.IndexOf('$guestValidation = Test-GuestSupervisorResult', [StringComparison]::Ordinal)
$passMappingIndex = $runbookText.IndexOf("'PASS'", $guestValidationIndex, [StringComparison]::Ordinal)
Assert-True 'guest validation precedes PASS mapping' (
    $guestValidationIndex -ge 0 -and
    $passMappingIndex -gt $guestValidationIndex
)

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
