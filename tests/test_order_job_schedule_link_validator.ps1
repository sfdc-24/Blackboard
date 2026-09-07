#Requires -Version 5.1
param([switch]$KeepArtifacts)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:ValidatorPath = Join-Path $script:RepoRoot 'infra\azure\validate_order_job_schedule_links.ps1'
$script:Canonical00Path = Join-Path $script:RepoRoot 'infra\azure\job-schedule-00.json'
$script:Canonical30Path = Join-Path $script:RepoRoot 'infra\azure\job-schedule-30.json'
$script:SystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$script:TestRoot = Join-Path `
    $script:SystemTemp `
    ('blackboard-order-job-schedule-validator-tests-' + [Guid]::NewGuid().ToString('N'))
$script:Passed = 0
$script:Failed = 0
$script:SubscriptionId = '604c029c-c254-4bc4-b173-05d6e5c3cab0'
$script:ResourceGroupName = 'COPILOT-DEV-RG'
$script:AutomationAccountName = 'sfdc24-blackboard-ops'
$script:RunbookName = 'Blackboard-VM-Supervisor'
$script:ScheduleNames = @('Blackboard-Supervisor-00', 'Blackboard-Supervisor-30')
$script:LinkIds = @(
    '11111111-1111-1111-1111-111111111111',
    '22222222-2222-2222-2222-222222222222'
)
$script:AccountPath = (
    '/subscriptions/' + $script:SubscriptionId +
    '/resourceGroups/' + $script:ResourceGroupName +
    '/providers/Microsoft.Automation/automationAccounts/' + $script:AutomationAccountName
)

function Assert-True {
    param([string] $Name, [bool] $Condition, [string] $Detail = '')
    if ($Condition) {
        $script:Passed++
        Write-Output ('PASS ' + $Name)
    }
    else {
        $script:Failed++
        Write-Output ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
    }
}

function Write-TestUtf8 {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string] $Text
    )
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Copy-JsonValue {
    param([Parameter(Mandatory = $true)][AllowNull()] $Value)
    if ($null -eq $Value) { return $null }
    return ($Value | ConvertTo-Json -Depth 20 -Compress) | ConvertFrom-Json
}

function New-MockLink {
    param(
        [Parameter(Mandatory = $true)][string] $ScheduleName,
        [Parameter(Mandatory = $true)][string] $JobScheduleId,
        [Parameter(Mandatory = $true)][AllowNull()] $Parameters,
        [string] $RunbookName = 'Blackboard-VM-Supervisor',
        [AllowNull()] $RunOn = $null
    )
    return [pscustomobject][ordered]@{
        id = $script:AccountPath + '/jobSchedules/' + $JobScheduleId
        name = $JobScheduleId
        type = 'Microsoft.Automation/automationAccounts/jobSchedules'
        properties = [pscustomobject][ordered]@{
            jobScheduleId = $JobScheduleId
            schedule = [pscustomobject][ordered]@{ name = $ScheduleName }
            runbook = [pscustomobject][ordered]@{ name = $RunbookName }
            parameters = $Parameters
            runOn = $RunOn
        }
    }
}

function New-CanonicalLinks {
    return @(
        (New-MockLink `
            -ScheduleName $script:ScheduleNames[0] `
            -JobScheduleId $script:LinkIds[0] `
            -Parameters (Copy-JsonValue $script:CanonicalParameters[0])),
        (New-MockLink `
            -ScheduleName $script:ScheduleNames[1] `
            -JobScheduleId $script:LinkIds[1] `
            -Parameters (Copy-JsonValue $script:CanonicalParameters[1]))
    )
}

function New-MockState {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]] $Links,
        [AllowNull()][object[]] $ListLinks = $null,
        [bool] $ListParametersNull = $true,
        [string[]] $MissingIds = @(),
        [string] $RawIndividualId = '',
        [string] $RawIndividualJson = ''
    )
    return [pscustomobject][ordered]@{
        account_path = $script:AccountPath
        links = $Links
        list_links = $ListLinks
        list_parameters_null = $ListParametersNull
        missing_ids = $MissingIds
        raw_individual_id = $RawIndividualId
        raw_individual_json = $RawIndividualJson
        calls = @()
    }
}

function Get-Receipt {
    param([Parameter(Mandatory = $true)][string] $Output)
    $lines = @($Output -split "`r?`n" | Where-Object { $_ -match '^\{.*\}$' })
    if ($lines.Count -ne 1) { return $null }
    try { return $lines[0] | ConvertFrom-Json }
    catch { return $null }
}

function Get-ReceiptLineCount {
    param([Parameter(Mandatory = $true)][string] $Output)
    return @($Output -split "`r?`n" | Where-Object { $_ -match '^\{.*\}$' }).Count
}

function Test-ReadOnlyCalls {
    param([Parameter(Mandatory = $true)] $State)
    return @($State.calls | Where-Object { $_.method -cne 'get' }).Count -eq 0
}

function New-CanonicalFaultRuntime {
    param(
        [Parameter(Mandatory = $true)][string] $CaseRoot,
        [Parameter(Mandatory = $true)]
        [ValidateSet('DuplicateTop', 'DuplicateParameter', 'Malformed', 'Bom', 'InvalidUtf8')]
        [string] $Fault
    )

    $runtime = Join-Path $CaseRoot 'validator-runtime'
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $validatorCopy = Join-Path $runtime 'validate_order_job_schedule_links.ps1'
    $canonical00Copy = Join-Path $runtime 'job-schedule-00.json'
    $canonical30Copy = Join-Path $runtime 'job-schedule-30.json'
    [IO.File]::WriteAllBytes($validatorCopy, [IO.File]::ReadAllBytes($script:ValidatorPath))
    [IO.File]::WriteAllBytes($canonical00Copy, [IO.File]::ReadAllBytes($script:Canonical00Path))
    [IO.File]::WriteAllBytes($canonical30Copy, [IO.File]::ReadAllBytes($script:Canonical30Path))

    if ($Fault -ceq 'Bom') {
        $original = [IO.File]::ReadAllBytes($canonical00Copy)
        $bytes = New-Object byte[] ($original.Length + 3)
        $bytes[0] = 0xEF
        $bytes[1] = 0xBB
        $bytes[2] = 0xBF
        [Array]::Copy($original, 0, $bytes, 3, $original.Length)
        [IO.File]::WriteAllBytes($canonical00Copy, $bytes)
        return $validatorCopy
    }
    if ($Fault -ceq 'InvalidUtf8') {
        [IO.File]::WriteAllBytes(
            $canonical00Copy,
            [byte[]](0x7B, 0x22, 0xFF, 0x22, 0x3A, 0x31, 0x7D)
        )
        return $validatorCopy
    }

    $text = [IO.File]::ReadAllText($canonical00Copy, [Text.Encoding]::UTF8)
    if ($Fault -ceq 'DuplicateTop') {
        $text = '{"properties":null,' + $text.Substring(1)
    }
    elseif ($Fault -ceq 'DuplicateParameter') {
        $needle = '"VMName":'
        $text = $text.Replace($needle, '"VMName":"duplicate",' + $needle)
    }
    else {
        $text = '{"properties":'
    }
    Write-TestUtf8 -Path $canonical00Copy -Text $text
    return $validatorCopy
}

function New-CanonicalResourceGroupRuntime {
    param(
        [Parameter(Mandatory = $true)][string] $CaseRoot,
        [Parameter(Mandatory = $true)][string] $ResourceGroupName
    )

    $runtime = Join-Path $CaseRoot 'validator-runtime'
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $validatorCopy = Join-Path $runtime 'validate_order_job_schedule_links.ps1'
    [IO.File]::WriteAllBytes($validatorCopy, [IO.File]::ReadAllBytes($script:ValidatorPath))
    foreach ($source in @($script:Canonical00Path, $script:Canonical30Path)) {
        $definition = [IO.File]::ReadAllText($source, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $definition.properties.parameters.ResourceGroupName = $ResourceGroupName
        $destination = Join-Path $runtime ([IO.Path]::GetFileName($source))
        Write-TestUtf8 -Path $destination -Text ($definition | ConvertTo-Json -Depth 20)
    }
    return $validatorCopy
}

function Invoke-ValidatorCase {
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)] $State,
        [ValidateSet('', 'DuplicateTop', 'DuplicateParameter', 'Malformed', 'Bom', 'InvalidUtf8')]
        [string] $CanonicalFault = '',
        [string] $CanonicalResourceGroupName = '',
        [string[]] $ValidatorArguments = @(),
        [string] $AzExecutableOverride = ''
    )

    $caseRoot = Join-Path $script:TestRoot $Name
    $caseTemp = Join-Path $caseRoot 'temp'
    $statePath = Join-Path $caseRoot 'state.json'
    New-Item -ItemType Directory -Path $caseTemp -Force | Out-Null
    Write-TestUtf8 -Path $statePath -Text ($State | ConvertTo-Json -Depth 30 -Compress)
    $validator = $script:ValidatorPath
    if ($CanonicalFault) {
        $validator = New-CanonicalFaultRuntime -CaseRoot $caseRoot -Fault $CanonicalFault
    }
    elseif ($CanonicalResourceGroupName) {
        $validator = New-CanonicalResourceGroupRuntime `
            -CaseRoot $caseRoot `
            -ResourceGroupName $CanonicalResourceGroupName
    }

    $previousStatePath = [Environment]::GetEnvironmentVariable('BLACKBOARD_TEST_AZ_STATE', 'Process')
    $previousTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
    $previousTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
    $previousPreference = $ErrorActionPreference
    try {
        [Environment]::SetEnvironmentVariable('BLACKBOARD_TEST_AZ_STATE', $statePath, 'Process')
        [Environment]::SetEnvironmentVariable('TEMP', $caseTemp, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $caseTemp, 'Process')
        $ErrorActionPreference = 'Continue'
        $azExecutable = $script:MockAzPath
        if (-not [string]::IsNullOrEmpty($AzExecutableOverride)) {
            $azExecutable = $AzExecutableOverride
        }
        $processArguments = @(
            '-NoLogo',
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', $validator,
            '-AzExecutable', $azExecutable
        ) + @($ValidatorArguments)
        $lines = @(& powershell.exe @processArguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        [Environment]::SetEnvironmentVariable('BLACKBOARD_TEST_AZ_STATE', $previousStatePath, 'Process')
        [Environment]::SetEnvironmentVariable('TEMP', $previousTemp, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $previousTmp, 'Process')
        $ErrorActionPreference = $previousPreference
    }
    $output = ($lines | ForEach-Object { [string]$_ }) -join "`n"
    $finalState = [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    return [pscustomobject]@{
        exit_code = $exitCode
        output = $output
        receipt = Get-Receipt -Output $output
        state = $finalState
    }
}

$mockAzSource = @'
#Requires -Version 5.1
param([Parameter(ValueFromRemainingArguments = $true)][string[]] $Arguments)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$statePath = [Environment]::GetEnvironmentVariable('BLACKBOARD_TEST_AZ_STATE', 'Process')
if ([string]::IsNullOrEmpty($statePath)) { [Console]::Error.WriteLine('MOCK_STATE_MISSING'); exit 90 }

function Read-State {
    return [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
}
function Save-State {
    param([Parameter(Mandatory = $true)] $State)
    [IO.File]::WriteAllText(
        $statePath,
        ($State | ConvertTo-Json -Depth 30 -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
}
function Fail-Mock {
    param([string] $Text, [int] $Code = 91)
    [Console]::Error.WriteLine($Text)
    exit $Code
}
function Write-Response {
    param([Parameter(Mandatory = $true)] $Value)
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Depth 30 -Compress))
}

$methodIndex = [Array]::IndexOf($Arguments, '--method')
$urlIndex = [Array]::IndexOf($Arguments, '--url')
if ($methodIndex -lt 0 -or $urlIndex -lt 0) { Fail-Mock 'MOCK_ARGUMENTS_INVALID' }
$method = [string]$Arguments[$methodIndex + 1]
$url = [string]$Arguments[$urlIndex + 1]
$state = Read-State
$state.calls = @($state.calls) + @([pscustomobject][ordered]@{ method = $method; url = $url })
Save-State -State $state
if ($method -cne 'get') { Fail-Mock 'MOCK_NON_READ_METHOD' }

try { $uri = New-Object Uri($url, [UriKind]::Absolute) }
catch { Fail-Mock 'MOCK_URL_INVALID' }
if ($uri.Host -cne 'management.azure.com' -or $uri.Query -cne '?api-version=2023-11-01') {
    Fail-Mock 'MOCK_URL_CONTRACT_INVALID'
}
$path = [Uri]::UnescapeDataString($uri.AbsolutePath)
if (-not $path.StartsWith([string]$state.account_path, [StringComparison]::OrdinalIgnoreCase)) {
    Fail-Mock 'MOCK_ACCOUNT_SCOPE_INVALID'
}
$relative = $path.Substring(([string]$state.account_path).Length).TrimStart('/')
if ($relative -ceq 'jobSchedules') {
    $sourceLinks = $(if ($null -ne $state.list_links) { @($state.list_links) } else { @($state.links) })
    $projected = @(
        $sourceLinks | ForEach-Object {
            $copy = ($_ | ConvertTo-Json -Depth 30 -Compress) | ConvertFrom-Json
            if ([bool]$state.list_parameters_null) { $copy.properties.parameters = $null }
            $copy
        }
    )
    Write-Response ([pscustomobject][ordered]@{ value = $projected; nextLink = $null })
    exit 0
}
if ($relative -match '^jobSchedules/([^/]+)$') {
    $id = $Matches[1]
    if (@($state.missing_ids) -ccontains $id) { Fail-Mock '404 NotFound' 44 }
    if ([string]$state.raw_individual_id -ceq $id -and -not [string]::IsNullOrEmpty([string]$state.raw_individual_json)) {
        [Console]::Out.WriteLine([string]$state.raw_individual_json)
        exit 0
    }
    $matches = @($state.links | Where-Object { $_.properties.jobScheduleId -ceq $id })
    if ($matches.Count -ne 1) { Fail-Mock '404 NotFound' 44 }
    Write-Response $matches[0]
    exit 0
}
Fail-Mock 'MOCK_ENDPOINT_INVALID'
'@

try {
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
    $mockScriptPath = Join-Path $script:TestRoot 'mock-az.ps1'
    $script:MockAzPath = Join-Path $script:TestRoot 'az.cmd'
    Write-TestUtf8 -Path $mockScriptPath -Text $mockAzSource
    $mockCmd = "@echo off`r`npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$mockScriptPath`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    [IO.File]::WriteAllText($script:MockAzPath, $mockCmd, [Text.Encoding]::ASCII)

    $canonical00 = [IO.File]::ReadAllText($script:Canonical00Path, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $canonical30 = [IO.File]::ReadAllText($script:Canonical30Path, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $script:CanonicalParameters = @($canonical00.properties.parameters, $canonical30.properties.parameters)

    $validatorSource = [IO.File]::ReadAllText($script:ValidatorPath, [Text.Encoding]::UTF8)
    Assert-True 'validator source has one explicit GET-only az REST method' (
        [regex]::Matches($validatorSource, "'--method'").Count -eq 1 -and
        $validatorSource -match "'--method',\s*'get'" -and
        $validatorSource -notmatch "(?i)'(?:delete|put|post|patch)'" -and
        $validatorSource -notmatch '\[Validate(?:Pattern|NotNullOrEmpty)'
    )

    $canonicalLinks = @(New-CanonicalLinks)
    $validState = New-MockState -Links $canonicalLinks -ListParametersNull $true
    $valid = Invoke-ValidatorCase -Name 'list-null-individual-canonical' -State $validState
    Assert-True 'LIST parameters null plus canonical individual GETs is VALID' (
        $valid.exit_code -eq 0 -and
        $valid.receipt.status -ceq 'VALID' -and
        $valid.receipt.canonical -eq $true -and
        [int]$valid.receipt.target_link_count -eq 2 -and
        @($valid.receipt.links).Count -eq 2 -and
        @($valid.receipt.links | Where-Object { -not $_.canonical }).Count -eq 0 -and
        @($valid.state.calls).Count -eq 3 -and
        (Test-ReadOnlyCalls -State $valid.state) -and
        (Get-ReceiptLineCount -Output $valid.output) -eq 1 -and
        @($valid.output -split "`r?`n" | Where-Object { $_ }).Count -eq 1 -and
        $valid.output.Length -lt 8192
    ) $valid.output

    $legalResourceGroupName = 'COPILOT(TEST)-RG'
    $legalAccountPath = (
        '/subscriptions/' + $script:SubscriptionId +
        '/resourceGroups/' + $legalResourceGroupName +
        '/providers/Microsoft.Automation/automationAccounts/' + $script:AutomationAccountName
    )
    $legalLinks = @(New-CanonicalLinks)
    foreach ($legalLink in $legalLinks) {
        $legalLink.id = $legalAccountPath + '/jobSchedules/' + [string]$legalLink.properties.jobScheduleId
        $legalLink.properties.parameters.ResourceGroupName = $legalResourceGroupName
    }
    $legalState = New-MockState -Links $legalLinks -ListParametersNull $true
    $legalState.account_path = $legalAccountPath
    $legal = Invoke-ValidatorCase `
        -Name 'legal-parentheses-resource-group' `
        -State $legalState `
        -CanonicalResourceGroupName $legalResourceGroupName `
        -ValidatorArguments @('-ResourceGroupName', $legalResourceGroupName)
    Assert-True 'transport encoding does not create false resource-ID drift for legal parentheses' (
        $legal.exit_code -eq 0 -and
        $legal.receipt.status -ceq 'VALID' -and
        $legal.receipt.canonical -eq $true -and
        @($legal.receipt.links | Where-Object {
            $_.mismatch_codes -ccontains 'RESOURCE_ID_MISMATCH'
        }).Count -eq 0 -and
        @($legal.state.calls).Count -eq 3 -and
        (Test-ReadOnlyCalls -State $legal.state)
    ) $legal.output

    $staleListLinks = @(New-CanonicalLinks)
    $staleListLinks[0].id = '/stale/list/projection/one'
    $staleListLinks[0].properties.schedule.name = $script:ScheduleNames[1].ToLowerInvariant()
    $staleListLinks[0].properties.runbook.name = 'Stale-List-Runbook'
    $staleListLinks[1].id = '/stale/list/projection/two'
    $staleListLinks[1].properties.schedule.name = $script:ScheduleNames[0].ToUpperInvariant()
    $staleListLinks[1].properties.runbook.name = 'STALE-LIST-RUNBOOK'
    $staleListState = New-MockState `
        -Links (New-CanonicalLinks) `
        -ListLinks $staleListLinks `
        -ListParametersNull $true
    $staleList = Invoke-ValidatorCase -Name 'stale-list-canonical-individual' -State $staleListState
    Assert-True 'stale and case-drifted LIST identity cannot override canonical individual GETs' (
        $staleList.exit_code -eq 0 -and
        $staleList.receipt.status -ceq 'VALID' -and
        $staleList.receipt.canonical -eq $true -and
        @($staleList.receipt.links | Where-Object { -not $_.canonical }).Count -eq 0 -and
        @($staleList.state.calls).Count -eq 3 -and
        (Test-ReadOnlyCalls -State $staleList.state)
    ) $staleList.output

    $missingState = New-MockState -Links (New-CanonicalLinks) -MissingIds @($script:LinkIds[1])
    $missing = Invoke-ValidatorCase -Name 'individual-missing' -State $missingState
    Assert-True 'missing individual GET is unmistakable DRIFT with exit 2' (
        $missing.exit_code -eq 2 -and
        $missing.receipt.status -ceq 'DRIFT' -and
        $missing.receipt.canonical -eq $false -and
        @($missing.receipt.links | Where-Object {
            $_.individual_get -ceq 'MISSING' -and
            $_.mismatch_codes -ccontains 'INDIVIDUAL_GET_MISSING'
        }).Count -eq 1 -and
        $null -eq $missing.receipt.failure_code -and
        (Test-ReadOnlyCalls -State $missing.state)
    ) $missing.output

    $valueLinks = @(New-CanonicalLinks)
    $valueLinks[0].properties.parameters.VMName = 'WRONG_VALUE_DO_NOT_EMIT'
    $valueState = New-MockState -Links $valueLinks
    $value = Invoke-ValidatorCase -Name 'individual-value-mismatch' -State $valueState
    Assert-True 'individual parameter mismatch is DRIFT without leaking values' (
        $value.exit_code -eq 2 -and
        $value.receipt.status -ceq 'DRIFT' -and
        @($value.receipt.links | Where-Object {
            $_.mismatch_codes -ccontains 'PARAMETER_VALUE_MISMATCH_VMNAME'
        }).Count -eq 1 -and
        $value.output -notmatch 'WRONG_VALUE_DO_NOT_EMIT'
    ) $value.output

    $shapeLinks = @(New-CanonicalLinks)
    $caseParameters = [pscustomobject][ordered]@{
        subscriptionid = $script:SubscriptionId
        ResourceGroupName = $script:ResourceGroupName
        VMName = 'AkatiaVM-regular'
        TaskName = 'SFDC24 Blackboard Order Worker'
        StaleAfterMinutes = '30'
        HungAfterMinutes = '120'
    }
    $shapeLinks[0].properties.parameters = $caseParameters
    $shapeLinks[1].properties.runOn = 'UnexpectedHybridWorker'
    $shapeState = New-MockState -Links $shapeLinks
    $shape = Invoke-ValidatorCase -Name 'individual-shape-mismatch' -State $shapeState
    Assert-True 'case-exact parameter keys and null runOn are enforced from individual GET' (
        $shape.exit_code -eq 2 -and
        @($shape.receipt.links | Where-Object {
            $_.mismatch_codes -ccontains 'PARAMETER_KEYS_MISMATCH'
        }).Count -eq 1 -and
        @($shape.receipt.links | Where-Object {
            $_.mismatch_codes -ccontains 'RUN_ON_MISMATCH'
        }).Count -eq 1
    ) $shape.output

    $nullLinks = @(New-CanonicalLinks)
    $nullLinks[0].properties.parameters = $null
    $nullState = New-MockState -Links $nullLinks
    $nullResult = Invoke-ValidatorCase -Name 'individual-null-parameters' -State $nullState
    Assert-True 'individual GET null parameters is DRIFT even though LIST is also null' (
        $nullResult.exit_code -eq 2 -and
        @($nullResult.receipt.links | Where-Object {
            $_.mismatch_codes -ccontains 'PARAMETERS_NULL_OR_MISSING'
        }).Count -eq 1
    ) $nullResult.output

    $oneLinkState = New-MockState -Links @((New-CanonicalLinks)[0])
    $oneLink = Invoke-ValidatorCase -Name 'cardinality' -State $oneLinkState
    Assert-True 'LIST cardinality drift exits 2 without unsafe individual reads' (
        $oneLink.exit_code -eq 2 -and
        [int]$oneLink.receipt.target_link_count -eq 1 -and
        $oneLink.receipt.discovery_codes -ccontains 'TARGET_LINK_CARDINALITY_INVALID' -and
        @($oneLink.receipt.links).Count -eq 0 -and
        @($oneLink.state.calls).Count -eq 1
    ) $oneLink.output

    $duplicateLinks = @(
        (New-MockLink `
            -ScheduleName $script:ScheduleNames[0] `
            -JobScheduleId $script:LinkIds[0] `
            -Parameters (Copy-JsonValue $script:CanonicalParameters[0])),
        (New-MockLink `
            -ScheduleName $script:ScheduleNames[1] `
            -JobScheduleId $script:LinkIds[0] `
            -Parameters (Copy-JsonValue $script:CanonicalParameters[1]))
    )
    $duplicateState = New-MockState -Links $duplicateLinks
    $duplicate = Invoke-ValidatorCase -Name 'duplicate-id' -State $duplicateState
    Assert-True 'duplicate discovered IDs are DRIFT before individual reads' (
        $duplicate.exit_code -eq 2 -and
        $duplicate.receipt.discovery_codes -ccontains 'TARGET_LINK_DUPLICATE_ID' -and
        @($duplicate.state.calls).Count -eq 1
    ) $duplicate.output

    $listIdentity = @(New-CanonicalLinks)
    $individualIdentity = @(New-CanonicalLinks)
    $individualIdentity[0].properties.runbook.name = 'Wrong-Runbook'
    $identityState = New-MockState -Links $individualIdentity -ListLinks $listIdentity
    $identity = Invoke-ValidatorCase -Name 'individual-identity' -State $identityState
    Assert-True 'individual GET identity overrides a canonical LIST projection' (
        $identity.exit_code -eq 2 -and
        @($identity.receipt.links | Where-Object {
            $_.mismatch_codes -ccontains 'RUNBOOK_MISMATCH'
        }).Count -eq 1 -and
        @($identity.state.calls).Count -eq 3
    ) $identity.output

    $duplicateScheduleLinks = @(New-CanonicalLinks)
    $duplicateScheduleLinks[1].properties.schedule.name = $script:ScheduleNames[0]
    $duplicateScheduleState = New-MockState `
        -Links $duplicateScheduleLinks `
        -ListLinks (New-CanonicalLinks)
    $duplicateSchedule = Invoke-ValidatorCase `
        -Name 'authoritative-schedule-set' `
        -State $duplicateScheduleState
    Assert-True 'individual GETs must authoritatively contain exactly one link per target schedule' (
        $duplicateSchedule.exit_code -eq 2 -and
        $duplicateSchedule.receipt.status -ceq 'DRIFT' -and
        @($duplicateSchedule.receipt.links | Where-Object {
            $_.mismatch_codes -ccontains 'AUTHORITATIVE_SCHEDULE_SET_MISMATCH'
        }).Count -eq 2 -and
        @($duplicateSchedule.state.calls).Count -eq 3
    ) $duplicateSchedule.output

    $invalidInputs = @(
        [pscustomobject]@{
            name = 'invalid-subscription'
            arguments = @('-SubscriptionId', 'ATTACK_CANARY_SUBSCRIPTION')
        },
        [pscustomobject]@{
            name = 'invalid-resource-group'
            arguments = @('-ResourceGroupName', '..\ATTACK_CANARY_RESOURCE')
        },
        [pscustomobject]@{
            name = 'invalid-automation-account'
            arguments = @('-AutomationAccountName', 'ATTACK_CANARY/account')
        },
        [pscustomobject]@{
            name = 'path-like-runbook'
            arguments = @('-RunbookName', 'C:\ATTACK_CANARY\runbook')
        }
    )
    foreach ($invalidInput in $invalidInputs) {
        $inputState = New-MockState -Links (New-CanonicalLinks)
        $inputResult = Invoke-ValidatorCase `
            -Name ([string]$invalidInput.name) `
            -State $inputState `
            -ValidatorArguments @($invalidInput.arguments)
        Assert-True ('invalid caller input ' + $invalidInput.name + ' emits one safe bounded receipt') (
            $inputResult.exit_code -eq 1 -and
            $inputResult.receipt.status -ceq 'FAILED' -and
            $inputResult.receipt.failure_code -ceq 'CALLER_INPUT_INVALID' -and
            @($inputResult.state.calls).Count -eq 0 -and
            (Get-ReceiptLineCount -Output $inputResult.output) -eq 1 -and
            @($inputResult.output -split "`r?`n" | Where-Object { $_ }).Count -eq 1 -and
            $inputResult.output.Length -lt 8192 -and
            $inputResult.output -notmatch 'ATTACK_CANARY'
        ) $inputResult.output
    }

    $pathLikeExecutableState = New-MockState -Links (New-CanonicalLinks)
    $pathLikeExecutable = Invoke-ValidatorCase `
        -Name 'path-like-relative-az-executable' `
        -State $pathLikeExecutableState `
        -AzExecutableOverride '..\ATTACK_CANARY\az.exe'
    Assert-True 'relative path-like executable emits one safe bounded input receipt' (
        $pathLikeExecutable.exit_code -eq 1 -and
        $pathLikeExecutable.receipt.status -ceq 'FAILED' -and
        $pathLikeExecutable.receipt.failure_code -ceq 'CALLER_INPUT_INVALID' -and
        @($pathLikeExecutable.state.calls).Count -eq 0 -and
        (Get-ReceiptLineCount -Output $pathLikeExecutable.output) -eq 1 -and
        @($pathLikeExecutable.output -split "`r?`n" | Where-Object { $_ }).Count -eq 1 -and
        $pathLikeExecutable.output.Length -lt 8192 -and
        $pathLikeExecutable.output -notmatch 'ATTACK_CANARY'
    ) $pathLikeExecutable.output

    foreach ($fault in @('DuplicateTop', 'DuplicateParameter', 'Malformed', 'Bom', 'InvalidUtf8')) {
        $faultState = New-MockState -Links (New-CanonicalLinks)
        $faultResult = Invoke-ValidatorCase `
            -Name ('canonical-' + $fault.ToLowerInvariant()) `
            -State $faultState `
            -CanonicalFault $fault
        Assert-True ('canonical JSON ' + $fault + ' is rejected before Azure reads') (
            $faultResult.exit_code -eq 1 -and
            $faultResult.receipt.status -ceq 'FAILED' -and
            $faultResult.receipt.failure_code -ceq 'CANONICAL_FILE_INVALID' -and
            @($faultResult.state.calls).Count -eq 0 -and
            (Get-ReceiptLineCount -Output $faultResult.output) -eq 1
        ) $faultResult.output
    }

    $providerLink = (New-CanonicalLinks)[0]
    $providerRaw = $providerLink | ConvertTo-Json -Depth 20 -Compress
    $providerNeedle = '"VMName":"AkatiaVM-regular"'
    $providerRaw = $providerRaw.Replace(
        $providerNeedle,
        $providerNeedle + ',"VMName":"duplicate"'
    )
    $providerState = New-MockState `
        -Links (New-CanonicalLinks) `
        -RawIndividualId $script:LinkIds[0] `
        -RawIndividualJson $providerRaw
    $provider = Invoke-ValidatorCase -Name 'provider-duplicate-json' -State $providerState
    Assert-True 'duplicate keys in an individual Azure JSON response fail closed' (
        $provider.exit_code -eq 1 -and
        $provider.receipt.status -ceq 'FAILED' -and
        $provider.receipt.failure_code -ceq 'AZURE_RESPONSE_SHAPE_INVALID' -and
        (Test-ReadOnlyCalls -State $provider.state) -and
        (Get-ReceiptLineCount -Output $provider.output) -eq 1
    ) $provider.output
}
finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $script:TestRoot -PathType Container)) {
        $resolvedRoot = [IO.Path]::GetFullPath($script:TestRoot)
        $prefix = $script:SystemTemp + [IO.Path]::DirectorySeparatorChar
        if (
            -not $resolvedRoot.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($resolvedRoot) -cnotmatch
                '^blackboard-order-job-schedule-validator-tests-[0-9a-f]{32}$'
        ) {
            throw 'test_cleanup_scope_invalid'
        }
        $item = Get-Item -LiteralPath $resolvedRoot -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'test_cleanup_reparse_forbidden'
        }
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force -ErrorAction Stop
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
