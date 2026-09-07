#Requires -Version 5.1
<#
.SYNOPSIS
    Read-only validation of the two ORDER Azure Automation job-schedule links.

.DESCRIPTION
    The Automation LIST representation can project properties.parameters as
    null and may contain stale identity casing even when the stored link is
    canonical. LIST therefore supplies only case-insensitive schedule hints for
    a bounded, unique pair of job-schedule IDs. Each ID is read individually;
    only that resource decides the exact identity, runOn, and six case-exact
    parameters against the checked-in canonical definitions.

    VALID exits 0, DRIFT exits 2, and an execution or input failure exits 1.
    Every outcome emits exactly one bounded JSON receipt and performs GETs only.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string] $SubscriptionId = '604c029c-c254-4bc4-b173-05d6e5c3cab0',

    [Parameter()]
    [string] $ResourceGroupName = 'COPILOT-DEV-RG',

    [Parameter()]
    [string] $AutomationAccountName = 'sfdc24-blackboard-ops',

    [Parameter()]
    [string] $RunbookName = 'Blackboard-VM-Supervisor',

    [Parameter()]
    [string] $AzExecutable = 'az'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$apiVersion = '2023-11-01'
$receiptSchema = 'blackboard.order-job-schedule-link-validation.receipt.v1'
$scheduleNames = @('Blackboard-Supervisor-00', 'Blackboard-Supervisor-30')
$requiredParameterNames = @(
    'SubscriptionId',
    'ResourceGroupName',
    'VMName',
    'TaskName',
    'StaleAfterMinutes',
    'HungAfterMinutes'
)
$canonicalFiles = [ordered]@{
    'Blackboard-Supervisor-00' = 'job-schedule-00.json'
    'Blackboard-Supervisor-30' = 'job-schedule-30.json'
}
$maximumResponseCharacters = 1048576
$maximumCanonicalBytes = 65536
$maximumPageCount = 100
$maximumCollectionItems = 10000
$knownFailureCodes = @(
    'AZ_CLI_NOT_FOUND',
    'AZURE_COLLECTION_TOO_LARGE',
    'AZURE_NEXT_LINK_INVALID',
    'AZURE_PAGINATION_INVALID',
    'AZURE_RESPONSE_SHAPE_INVALID',
    'CALLER_INPUT_INVALID',
    'CANONICAL_FILE_INVALID',
    'CANONICAL_IDENTITY_INVALID',
    'CANONICAL_TARGET_MISMATCH',
    'TARGET_LINK_LIST_FAILED',
    'TARGET_LINK_READ_FAILED'
)

function Throw-Failure {
    param([Parameter(Mandatory = $true)][string] $Code)
    throw $Code
}

function Assert-CallerInputs {
    if (
        $SubscriptionId -cnotmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' -or
        $ResourceGroupName -cnotmatch '^[A-Za-z0-9._()\-]{1,90}$' -or
        $AutomationAccountName -cnotmatch '^[A-Za-z0-9\-]{1,50}$' -or
        $RunbookName -cnotmatch '^[A-Za-z0-9_\-]{1,63}$' -or
        [string]::IsNullOrWhiteSpace($AzExecutable) -or
        $AzExecutable.Length -gt 4096 -or
        $AzExecutable -cmatch '[\x00-\x1F]'
    ) {
        Throw-Failure -Code 'CALLER_INPUT_INVALID'
    }
    if (
        -not [IO.Path]::IsPathRooted($AzExecutable) -and
        $AzExecutable -cnotmatch '^[A-Za-z0-9._\-]{1,260}$'
    ) {
        Throw-Failure -Code 'CALLER_INPUT_INVALID'
    }
}

function Get-SafeFailureCode {
    param([Parameter(Mandatory = $true)] $ErrorRecord)

    $candidate = [string]$ErrorRecord.Exception.Message
    if ($knownFailureCodes -ccontains $candidate) { return $candidate }
    return 'VALIDATION_FAILED'
}

function Get-ExactPropertyResult {
    param(
        [Parameter(Mandatory = $true)][AllowNull()] $InputObject,
        [Parameter(Mandatory = $true)][string] $Name
    )

    if ($null -eq $InputObject) {
        return [pscustomobject]@{ found = $false; value = $null }
    }
    $matches = @($InputObject.PSObject.Properties | Where-Object { $_.Name -ceq $Name })
    if ($matches.Count -ne 1) {
        return [pscustomobject]@{ found = $false; value = $null }
    }
    return [pscustomobject]@{ found = $true; value = $matches[0].Value }
}

function Get-RequiredProperty {
    param(
        [Parameter(Mandatory = $true)][AllowNull()] $InputObject,
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    $result = Get-ExactPropertyResult -InputObject $InputObject -Name $Name
    if (-not $result.found) { Throw-Failure -Code $FailureCode }
    return $result.value
}

function Get-ExactPropertyNames {
    param([Parameter(Mandatory = $true)][AllowNull()] $InputObject)
    if ($null -eq $InputObject) { return @() }
    return @($InputObject.PSObject.Properties | ForEach-Object { [string]$_.Name })
}

function Assert-ExactPropertyNames {
    param(
        [Parameter(Mandatory = $true)][AllowNull()] $InputObject,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]] $Expected,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    $actual = @(Get-ExactPropertyNames -InputObject $InputObject)
    if ($actual.Count -ne $Expected.Count) { Throw-Failure -Code $FailureCode }
    foreach ($name in $Expected) {
        if ($actual -cnotcontains $name) { Throw-Failure -Code $FailureCode }
    }
    foreach ($name in $actual) {
        if ($Expected -cnotcontains $name) { Throw-Failure -Code $FailureCode }
    }
}

function Read-JsonStringToken {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][int] $Start,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    if ($Start -ge $Text.Length -or $Text[$Start] -ne [char]34) {
        Throw-Failure -Code $FailureCode
    }
    $builder = New-Object Text.StringBuilder
    $index = $Start + 1
    while ($index -lt $Text.Length) {
        $character = $Text[$index]
        if ($character -eq [char]34) {
            return [pscustomobject]@{ value = $builder.ToString(); next = $index + 1 }
        }
        if ([int]$character -lt 0x20) { Throw-Failure -Code $FailureCode }
        if ($character -ne [char]92) {
            $null = $builder.Append($character)
            $index++
            continue
        }

        $index++
        if ($index -ge $Text.Length) { Throw-Failure -Code $FailureCode }
        $escaped = [string]$Text[$index]
        switch -CaseSensitive ($escaped) {
            '"' { $null = $builder.Append([char]34) }
            '\' { $null = $builder.Append([char]92) }
            '/' { $null = $builder.Append([char]47) }
            'b' { $null = $builder.Append([char]8) }
            'f' { $null = $builder.Append([char]12) }
            'n' { $null = $builder.Append([char]10) }
            'r' { $null = $builder.Append([char]13) }
            't' { $null = $builder.Append([char]9) }
            'u' {
                if ($index + 4 -ge $Text.Length) { Throw-Failure -Code $FailureCode }
                $hex = $Text.Substring($index + 1, 4)
                if ($hex -cnotmatch '^[0-9a-fA-F]{4}$') { Throw-Failure -Code $FailureCode }
                try {
                    $codeUnit = [int]::Parse(
                        $hex,
                        [Globalization.NumberStyles]::HexNumber,
                        [Globalization.CultureInfo]::InvariantCulture
                    )
                }
                catch { Throw-Failure -Code $FailureCode }
                $null = $builder.Append([char]$codeUnit)
                $index += 4
            }
            default { Throw-Failure -Code $FailureCode }
        }
        $index++
    }
    Throw-Failure -Code $FailureCode
}

function Assert-NoDuplicateJsonKeys {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    $stack = New-Object Collections.Stack
    $index = 0
    while ($index -lt $Text.Length) {
        $character = $Text[$index]
        if ($character -eq [char]34) {
            $token = Read-JsonStringToken -Text $Text -Start $index -FailureCode $FailureCode
            $lookahead = [int]$token.next
            while ($lookahead -lt $Text.Length -and [char]::IsWhiteSpace($Text[$lookahead])) {
                $lookahead++
            }
            if (
                $lookahead -lt $Text.Length -and
                $Text[$lookahead] -eq [char]58 -and
                $stack.Count -gt 0 -and
                $stack.Peek().kind -ceq 'OBJECT'
            ) {
                if (-not $stack.Peek().keys.Add([string]$token.value)) {
                    Throw-Failure -Code $FailureCode
                }
            }
            $index = [int]$token.next
            continue
        }
        if ($character -eq [char]123) {
            $stack.Push([pscustomobject]@{
                kind = 'OBJECT'
                keys = (New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal))
            })
        }
        elseif ($character -eq [char]91) {
            $stack.Push([pscustomobject]@{ kind = 'ARRAY'; keys = $null })
        }
        elseif ($character -eq [char]125) {
            if ($stack.Count -eq 0 -or $stack.Peek().kind -cne 'OBJECT') {
                Throw-Failure -Code $FailureCode
            }
            $null = $stack.Pop()
        }
        elseif ($character -eq [char]93) {
            if ($stack.Count -eq 0 -or $stack.Peek().kind -cne 'ARRAY') {
                Throw-Failure -Code $FailureCode
            }
            $null = $stack.Pop()
        }
        $index++
    }
    if ($stack.Count -ne 0) { Throw-Failure -Code $FailureCode }
}

function ConvertFrom-StrictJsonText {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    Assert-NoDuplicateJsonKeys -Text $Text -FailureCode $FailureCode
    try { return $Text | ConvertFrom-Json }
    catch { Throw-Failure -Code $FailureCode }
}

function Read-StrictUtf8JsonFile {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][int] $MaximumBytes,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    try { $bytes = [IO.File]::ReadAllBytes($Path) }
    catch { Throw-Failure -Code $FailureCode }
    if ($bytes.Length -eq 0 -or $bytes.Length -gt $MaximumBytes) {
        Throw-Failure -Code $FailureCode
    }
    if (
        $bytes.Length -ge 3 -and
        $bytes[0] -eq 0xEF -and
        $bytes[1] -eq 0xBB -and
        $bytes[2] -eq 0xBF
    ) {
        Throw-Failure -Code $FailureCode
    }
    try { $text = (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes) }
    catch { Throw-Failure -Code $FailureCode }
    return ConvertFrom-StrictJsonText -Text $text -FailureCode $FailureCode
}

function Resolve-AzExecutable {
    param([Parameter(Mandatory = $true)][string] $Value)

    try {
        if ([IO.Path]::IsPathRooted($Value)) {
            $resolved = [IO.Path]::GetFullPath($Value)
            if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
                Throw-Failure -Code 'AZ_CLI_NOT_FOUND'
            }
            return $resolved
        }
        $command = Get-Command -Name $Value -CommandType Application -ErrorAction Stop |
            Select-Object -First 1
        return [string]$command.Source
    }
    catch { Throw-Failure -Code 'AZ_CLI_NOT_FOUND' }
}

function Invoke-AzGetRaw {
    param([Parameter(Mandatory = $true)][string] $Url)

    $arguments = @(
        'rest',
        '--method', 'get',
        '--url', $Url,
        '--output', 'json',
        '--only-show-errors'
    )
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $outputLines = @(& $script:ResolvedAzExecutable @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    catch {
        $outputLines = @()
        $exitCode = -1
    }
    finally { $ErrorActionPreference = $previousPreference }

    $output = ($outputLines | ForEach-Object { [string]$_ }) -join "`n"
    if ($output.Length -gt $maximumResponseCharacters) {
        return [pscustomobject]@{ exit_code = -2; output = '' }
    }
    return [pscustomobject]@{ exit_code = [int]$exitCode; output = $output }
}

function Test-AzNotFound {
    param([Parameter(Mandatory = $true)][string] $Text)
    return $Text -match '(?i)(\b404\b|resource.?not.?found|\bnotfound\b)'
}

function Convert-AzResponse {
    param(
        [Parameter(Mandatory = $true)] $Raw,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    if ($Raw.exit_code -ne 0 -or [string]::IsNullOrWhiteSpace([string]$Raw.output)) {
        Throw-Failure -Code $FailureCode
    }
    return ConvertFrom-StrictJsonText -Text ([string]$Raw.output) -FailureCode 'AZURE_RESPONSE_SHAPE_INVALID'
}

function Invoke-AzGetJson {
    param(
        [Parameter(Mandatory = $true)][string] $Url,
        [Parameter(Mandatory = $true)][string] $FailureCode
    )

    return Convert-AzResponse -Raw (Invoke-AzGetRaw -Url $Url) -FailureCode $FailureCode
}

function Invoke-AzGetJsonOptional {
    param([Parameter(Mandatory = $true)][string] $Url)

    $raw = Invoke-AzGetRaw -Url $Url
    if ($raw.exit_code -ne 0) {
        if (Test-AzNotFound -Text ([string]$raw.output)) {
            return [pscustomobject]@{ found = $false; value = $null }
        }
        Throw-Failure -Code 'TARGET_LINK_READ_FAILED'
    }
    return [pscustomobject]@{
        found = $true
        value = (Convert-AzResponse -Raw $raw -FailureCode 'TARGET_LINK_READ_FAILED')
    }
}

function Get-EscapedPathSegment {
    param([Parameter(Mandatory = $true)][string] $Value)
    return [Uri]::EscapeDataString($Value)
}

function New-ApiUrl {
    param([Parameter(Mandatory = $true)][string] $RelativePath)
    return $script:AccountBaseUrl + '/' + $RelativePath + '?api-version=' + $apiVersion
}

function Assert-AutomationApiUrl {
    param([Parameter(Mandatory = $true)][string] $Url)

    try { $uri = New-Object Uri($Url, [UriKind]::Absolute) }
    catch { Throw-Failure -Code 'AZURE_NEXT_LINK_INVALID' }
    if (
        $uri.Scheme -cne 'https' -or
        $uri.Host -cne 'management.azure.com' -or
        -not $uri.IsDefaultPort -or
        -not [string]::IsNullOrEmpty($uri.UserInfo) -or
        -not [string]::IsNullOrEmpty($uri.Fragment) -or
        -not $uri.AbsolutePath.StartsWith($script:AccountBasePath + '/', [StringComparison]::OrdinalIgnoreCase)
    ) {
        Throw-Failure -Code 'AZURE_NEXT_LINK_INVALID'
    }
    $queryParts = @($uri.Query.TrimStart('?').Split('&') | Where-Object { $_ })
    $apiParts = @($queryParts | Where-Object {
        ([Uri]::UnescapeDataString($_)).StartsWith('api-version=', [StringComparison]::OrdinalIgnoreCase)
    })
    if ($apiParts.Count -ne 1) { Throw-Failure -Code 'AZURE_NEXT_LINK_INVALID' }
    $apiValue = ([Uri]::UnescapeDataString($apiParts[0])).Substring('api-version='.Length)
    if ($apiValue -cne $apiVersion) { Throw-Failure -Code 'AZURE_NEXT_LINK_INVALID' }
}

function Get-PagedCollection {
    param([Parameter(Mandatory = $true)][string] $InitialUrl)

    $items = New-Object 'System.Collections.Generic.List[object]'
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $nextUrl = $InitialUrl
    $pageCount = 0
    while (-not [string]::IsNullOrEmpty($nextUrl)) {
        $pageCount++
        if ($pageCount -gt $maximumPageCount -or -not $seen.Add($nextUrl)) {
            Throw-Failure -Code 'AZURE_PAGINATION_INVALID'
        }
        Assert-AutomationApiUrl -Url $nextUrl
        $page = Invoke-AzGetJson -Url $nextUrl -FailureCode 'TARGET_LINK_LIST_FAILED'
        $values = Get-RequiredProperty `
            -InputObject $page `
            -Name 'value' `
            -FailureCode 'AZURE_RESPONSE_SHAPE_INVALID'
        foreach ($item in @($values)) {
            $items.Add($item)
            if ($items.Count -gt $maximumCollectionItems) {
                Throw-Failure -Code 'AZURE_COLLECTION_TOO_LARGE'
            }
        }
        $nextResult = Get-ExactPropertyResult -InputObject $page -Name 'nextLink'
        if (-not $nextResult.found -or $null -eq $nextResult.value) {
            $nextUrl = ''
        }
        elseif (-not ($nextResult.value -is [string]) -or [string]::IsNullOrWhiteSpace($nextResult.value)) {
            Throw-Failure -Code 'AZURE_NEXT_LINK_INVALID'
        }
        else { $nextUrl = [string]$nextResult.value }
    }
    return $items.ToArray()
}

function Read-CanonicalDefinitions {
    $definitions = New-Object 'System.Collections.Generic.List[object]'
    foreach ($scheduleName in $scheduleNames) {
        $path = Join-Path $PSScriptRoot ([string]$canonicalFiles[$scheduleName])
        $body = Read-StrictUtf8JsonFile `
            -Path $path `
            -MaximumBytes $maximumCanonicalBytes `
            -FailureCode 'CANONICAL_FILE_INVALID'
        Assert-ExactPropertyNames -InputObject $body -Expected @('properties') -FailureCode 'CANONICAL_FILE_INVALID'
        $properties = Get-RequiredProperty -InputObject $body -Name 'properties' -FailureCode 'CANONICAL_FILE_INVALID'
        Assert-ExactPropertyNames `
            -InputObject $properties `
            -Expected @('schedule', 'runbook', 'parameters') `
            -FailureCode 'CANONICAL_FILE_INVALID'
        $schedule = Get-RequiredProperty -InputObject $properties -Name 'schedule' -FailureCode 'CANONICAL_FILE_INVALID'
        $runbook = Get-RequiredProperty -InputObject $properties -Name 'runbook' -FailureCode 'CANONICAL_FILE_INVALID'
        $parameters = Get-RequiredProperty -InputObject $properties -Name 'parameters' -FailureCode 'CANONICAL_FILE_INVALID'
        Assert-ExactPropertyNames -InputObject $schedule -Expected @('name') -FailureCode 'CANONICAL_FILE_INVALID'
        Assert-ExactPropertyNames -InputObject $runbook -Expected @('name') -FailureCode 'CANONICAL_FILE_INVALID'
        Assert-ExactPropertyNames -InputObject $parameters -Expected $requiredParameterNames -FailureCode 'CANONICAL_FILE_INVALID'
        $canonicalSchedule = Get-RequiredProperty -InputObject $schedule -Name 'name' -FailureCode 'CANONICAL_FILE_INVALID'
        $canonicalRunbook = Get-RequiredProperty -InputObject $runbook -Name 'name' -FailureCode 'CANONICAL_FILE_INVALID'
        if (
            -not ($canonicalSchedule -is [string]) -or
            [string]$canonicalSchedule -cne $scheduleName -or
            -not ($canonicalRunbook -is [string]) -or
            [string]$canonicalRunbook -cne $RunbookName
        ) {
            Throw-Failure -Code 'CANONICAL_IDENTITY_INVALID'
        }
        foreach ($parameterName in $requiredParameterNames) {
            $value = Get-RequiredProperty `
                -InputObject $parameters `
                -Name $parameterName `
                -FailureCode 'CANONICAL_FILE_INVALID'
            if (-not ($value -is [string])) { Throw-Failure -Code 'CANONICAL_FILE_INVALID' }
        }
        if (
            [string](Get-RequiredProperty -InputObject $parameters -Name 'SubscriptionId' -FailureCode 'CANONICAL_FILE_INVALID') -cne $SubscriptionId -or
            [string](Get-RequiredProperty -InputObject $parameters -Name 'ResourceGroupName' -FailureCode 'CANONICAL_FILE_INVALID') -cne $ResourceGroupName
        ) {
            Throw-Failure -Code 'CANONICAL_TARGET_MISMATCH'
        }
        $definitions.Add([pscustomobject]@{
            schedule_name = $scheduleName
            parameters = $parameters
        })
    }
    return $definitions.ToArray()
}

function Get-CanonicalDefinition {
    param(
        [Parameter(Mandatory = $true)][object[]] $Definitions,
        [Parameter(Mandatory = $true)][string] $ScheduleName
    )

    $matches = @($Definitions | Where-Object { $_.schedule_name -ceq $ScheduleName })
    if ($matches.Count -ne 1) { Throw-Failure -Code 'CANONICAL_FILE_INVALID' }
    return $matches[0]
}

function Get-DiscoveryResult {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]] $AllLinks)

    $candidates = New-Object 'System.Collections.Generic.List[object]'
    foreach ($link in $AllLinks) {
        $properties = Get-RequiredProperty -InputObject $link -Name 'properties' -FailureCode 'AZURE_RESPONSE_SHAPE_INVALID'
        $schedule = Get-RequiredProperty -InputObject $properties -Name 'schedule' -FailureCode 'AZURE_RESPONSE_SHAPE_INVALID'
        $scheduleName = Get-RequiredProperty -InputObject $schedule -Name 'name' -FailureCode 'AZURE_RESPONSE_SHAPE_INVALID'
        $id = Get-RequiredProperty -InputObject $properties -Name 'jobScheduleId' -FailureCode 'AZURE_RESPONSE_SHAPE_INVALID'
        if (-not ($scheduleName -is [string]) -or -not ($id -is [string])) {
            Throw-Failure -Code 'AZURE_RESPONSE_SHAPE_INVALID'
        }
        $targetScheduleName = $null
        foreach ($targetSchedule in $scheduleNames) {
            if ([string]::Equals([string]$scheduleName, $targetSchedule, [StringComparison]::OrdinalIgnoreCase)) {
                $targetScheduleName = $targetSchedule
                break
            }
        }
        if ($null -ne $targetScheduleName) {
            $candidates.Add([pscustomobject]@{
                target_schedule_name = [string]$targetScheduleName
                job_schedule_id = [string]$id
            })
        }
    }

    $codes = New-Object 'System.Collections.Generic.List[string]'
    $resolved = New-Object 'System.Collections.Generic.List[object]'
    if ($candidates.Count -ne 2) {
        $codes.Add('TARGET_LINK_CARDINALITY_INVALID')
    }
    else {
        $seenIds = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        foreach ($scheduleName in $scheduleNames) {
            $matches = @($candidates | Where-Object { $_.target_schedule_name -ceq $scheduleName })
            if ($matches.Count -ne 1) {
                if ($codes -cnotcontains 'TARGET_LINK_MAPPING_INVALID') {
                    $codes.Add('TARGET_LINK_MAPPING_INVALID')
                }
                continue
            }
            $parsed = [Guid]::Empty
            if (-not [Guid]::TryParseExact($matches[0].job_schedule_id, 'D', [ref]$parsed)) {
                if ($codes -cnotcontains 'TARGET_LINK_MAPPING_INVALID') {
                    $codes.Add('TARGET_LINK_MAPPING_INVALID')
                }
                continue
            }
            if (-not $seenIds.Add($matches[0].job_schedule_id)) {
                if ($codes -cnotcontains 'TARGET_LINK_DUPLICATE_ID') {
                    $codes.Add('TARGET_LINK_DUPLICATE_ID')
                }
                continue
            }
            $resolved.Add([pscustomobject]@{
                schedule_hint = $scheduleName
                job_schedule_id = $matches[0].job_schedule_id
            })
        }
        if ($resolved.Count -ne 2 -and $codes.Count -eq 0) {
            $codes.Add('TARGET_LINK_MAPPING_INVALID')
        }
    }
    return [pscustomobject]@{
        target_count = $candidates.Count
        codes = $codes.ToArray()
        links = $resolved.ToArray()
    }
}

function Add-MismatchCode {
    param(
        [Parameter(Mandatory = $true)] $Codes,
        [Parameter(Mandatory = $true)][string] $Code
    )
    if (-not $Codes.Contains($Code)) { $Codes.Add($Code) }
}

function Get-AuthoritativeScheduleName {
    param([Parameter(Mandatory = $true)] $Link)

    $properties = Get-ExactPropertyResult -InputObject $Link -Name 'properties'
    if (-not $properties.found) { return $null }
    $schedule = Get-ExactPropertyResult -InputObject $properties.value -Name 'schedule'
    if (-not $schedule.found) { return $null }
    $name = Get-ExactPropertyResult -InputObject $schedule.value -Name 'name'
    if (-not $name.found -or -not ($name.value -is [string])) { return $null }
    foreach ($targetSchedule in $scheduleNames) {
        if ([string]$name.value -ceq $targetSchedule) { return $targetSchedule }
    }
    return $null
}

function Compare-IndividualLink {
    param(
        [Parameter(Mandatory = $true)] $Link,
        [Parameter(Mandatory = $true)][string] $ExpectedScheduleName,
        [Parameter(Mandatory = $true)][string] $ExpectedId,
        [Parameter(Mandatory = $true)] $CanonicalDefinition
    )

    $codes = New-Object 'System.Collections.Generic.List[string]'
    $propertiesResult = Get-ExactPropertyResult -InputObject $Link -Name 'properties'
    $resourceIdResult = Get-ExactPropertyResult -InputObject $Link -Name 'id'
    if (-not $propertiesResult.found) {
        Add-MismatchCode -Codes $codes -Code 'RESPONSE_SHAPE_MISMATCH'
        return $codes.ToArray()
    }
    $properties = $propertiesResult.value

    $idResult = Get-ExactPropertyResult -InputObject $properties -Name 'jobScheduleId'
    if (-not $idResult.found -or -not ($idResult.value -is [string]) -or [string]$idResult.value -cne $ExpectedId) {
        Add-MismatchCode -Codes $codes -Code 'JOB_SCHEDULE_ID_MISMATCH'
    }
    # AccountBasePath is URL-encoded for transport. ARM resource IDs are identity
    # values and Azure commonly returns legal characters such as parentheses in
    # their unescaped form, so compare a single normalized representation rather
    # than reusing the request path byte-for-byte.
    $expectedResourceId = [Uri]::UnescapeDataString($script:AccountBasePath) + '/jobSchedules/' + $ExpectedId
    $normalizedResourceId = $null
    if ($resourceIdResult.found -and $resourceIdResult.value -is [string]) {
        try { $normalizedResourceId = [Uri]::UnescapeDataString([string]$resourceIdResult.value) }
        catch { $normalizedResourceId = $null }
    }
    if (
        $null -eq $normalizedResourceId -or
        -not [string]::Equals($normalizedResourceId, $expectedResourceId, [StringComparison]::OrdinalIgnoreCase)
    ) {
        Add-MismatchCode -Codes $codes -Code 'RESOURCE_ID_MISMATCH'
    }

    $scheduleResult = Get-ExactPropertyResult -InputObject $properties -Name 'schedule'
    $runbookResult = Get-ExactPropertyResult -InputObject $properties -Name 'runbook'
    $actualSchedule = $(if ($scheduleResult.found) {
        Get-ExactPropertyResult -InputObject $scheduleResult.value -Name 'name'
    } else { [pscustomobject]@{ found = $false; value = $null } })
    $actualRunbook = $(if ($runbookResult.found) {
        Get-ExactPropertyResult -InputObject $runbookResult.value -Name 'name'
    } else { [pscustomobject]@{ found = $false; value = $null } })
    if (-not $actualSchedule.found -or -not ($actualSchedule.value -is [string]) -or [string]$actualSchedule.value -cne $ExpectedScheduleName) {
        Add-MismatchCode -Codes $codes -Code 'SCHEDULE_MISMATCH'
    }
    if (-not $actualRunbook.found -or -not ($actualRunbook.value -is [string]) -or [string]$actualRunbook.value -cne $RunbookName) {
        Add-MismatchCode -Codes $codes -Code 'RUNBOOK_MISMATCH'
    }

    $runOnResult = Get-ExactPropertyResult -InputObject $properties -Name 'runOn'
    if ($runOnResult.found -and $null -ne $runOnResult.value) {
        Add-MismatchCode -Codes $codes -Code 'RUN_ON_MISMATCH'
    }

    $parametersResult = Get-ExactPropertyResult -InputObject $properties -Name 'parameters'
    if (-not $parametersResult.found -or $null -eq $parametersResult.value) {
        Add-MismatchCode -Codes $codes -Code 'PARAMETERS_NULL_OR_MISSING'
        return $codes.ToArray()
    }
    $parameters = $parametersResult.value
    $actualNames = @(Get-ExactPropertyNames -InputObject $parameters)
    $keysMatch = $actualNames.Count -eq $requiredParameterNames.Count
    foreach ($name in $requiredParameterNames) {
        if ($actualNames -cnotcontains $name) { $keysMatch = $false }
    }
    foreach ($name in $actualNames) {
        if ($requiredParameterNames -cnotcontains $name) { $keysMatch = $false }
    }
    if (-not $keysMatch) {
        Add-MismatchCode -Codes $codes -Code 'PARAMETER_KEYS_MISMATCH'
        return $codes.ToArray()
    }
    foreach ($name in $requiredParameterNames) {
        $actual = Get-ExactPropertyResult -InputObject $parameters -Name $name
        $expected = Get-RequiredProperty `
            -InputObject $CanonicalDefinition.parameters `
            -Name $name `
            -FailureCode 'CANONICAL_FILE_INVALID'
        if (-not $actual.found -or -not ($actual.value -is [string])) {
            Add-MismatchCode -Codes $codes -Code ('PARAMETER_TYPE_MISMATCH_' + $name.ToUpperInvariant())
        }
        elseif ([string]$actual.value -cne [string]$expected) {
            Add-MismatchCode -Codes $codes -Code ('PARAMETER_VALUE_MISMATCH_' + $name.ToUpperInvariant())
        }
    }
    return $codes.ToArray()
}

function New-Receipt {
    param(
        [Parameter(Mandatory = $true)][string] $Status,
        [AllowNull()] $Canonical,
        [AllowNull()] $TargetLinkCount,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]] $DiscoveryCodes,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]] $Links,
        [AllowNull()] $FailureCode
    )

    return [ordered]@{
        schema = $receiptSchema
        status = $Status
        api_version = $apiVersion
        canonical = $Canonical
        target_link_count = $TargetLinkCount
        discovery_codes = $DiscoveryCodes
        links = $Links
        failure_code = $FailureCode
    }
}

$script:ResolvedAzExecutable = $null
$script:AccountBaseUrl = $null
$script:AccountBasePath = $null
$targetLinkCount = $null
$discoveryCodes = @()
$linkReceipts = @()
$receipt = $null

try {
    Assert-CallerInputs
    $script:ResolvedAzExecutable = Resolve-AzExecutable -Value $AzExecutable
    $script:AccountBasePath = (
        '/subscriptions/' + (Get-EscapedPathSegment -Value $SubscriptionId) +
        '/resourceGroups/' + (Get-EscapedPathSegment -Value $ResourceGroupName) +
        '/providers/Microsoft.Automation/automationAccounts/' +
        (Get-EscapedPathSegment -Value $AutomationAccountName)
    )
    $script:AccountBaseUrl = 'https://management.azure.com' + $script:AccountBasePath

    $canonicalDefinitions = @(Read-CanonicalDefinitions)
    $listedLinks = @(Get-PagedCollection -InitialUrl (New-ApiUrl -RelativePath 'jobSchedules'))
    $discovery = Get-DiscoveryResult -AllLinks $listedLinks
    $targetLinkCount = $discovery.target_count
    $discoveryCodes = @($discovery.codes)

    if ($discoveryCodes.Count -eq 0) {
        $observations = New-Object 'System.Collections.Generic.List[object]'
        foreach ($target in $discovery.links) {
            $read = Invoke-AzGetJsonOptional `
                -Url (New-ApiUrl -RelativePath (
                    'jobSchedules/' + (Get-EscapedPathSegment -Value $target.job_schedule_id)
                ))
            if (-not $read.found) {
                $observations.Add([pscustomobject]@{
                    authoritative_schedule_name = $null
                    receipt = [pscustomobject][ordered]@{
                        schedule_name = $null
                        job_schedule_id = $target.job_schedule_id
                        individual_get = 'MISSING'
                        canonical = $false
                        mismatch_codes = @('INDIVIDUAL_GET_MISSING')
                    }
                })
                continue
            }
            $authoritativeScheduleName = Get-AuthoritativeScheduleName -Link $read.value
            $comparisonScheduleName = $authoritativeScheduleName
            if ($null -eq $comparisonScheduleName) {
                $comparisonScheduleName = $target.schedule_hint
            }
            $definition = Get-CanonicalDefinition `
                -Definitions $canonicalDefinitions `
                -ScheduleName $comparisonScheduleName
            $mismatches = @(Compare-IndividualLink `
                -Link $read.value `
                -ExpectedScheduleName $comparisonScheduleName `
                -ExpectedId $target.job_schedule_id `
                -CanonicalDefinition $definition)
            $observations.Add([pscustomobject]@{
                authoritative_schedule_name = $authoritativeScheduleName
                receipt = [pscustomobject][ordered]@{
                    schedule_name = $authoritativeScheduleName
                    job_schedule_id = $target.job_schedule_id
                    individual_get = 'FOUND'
                    canonical = ($mismatches.Count -eq 0)
                    mismatch_codes = $mismatches
                }
            })
        }

        $authoritativeScheduleSetMatches = $true
        foreach ($scheduleName in $scheduleNames) {
            $matches = @($observations | Where-Object {
                $_.authoritative_schedule_name -ceq $scheduleName
            })
            if ($matches.Count -ne 1) { $authoritativeScheduleSetMatches = $false }
        }
        if (-not $authoritativeScheduleSetMatches) {
            foreach ($observation in $observations) {
                $codes = @($observation.receipt.mismatch_codes)
                if ($codes -cnotcontains 'AUTHORITATIVE_SCHEDULE_SET_MISMATCH') {
                    $observation.receipt.mismatch_codes = @(
                        $codes + @('AUTHORITATIVE_SCHEDULE_SET_MISMATCH')
                    )
                }
                $observation.receipt.canonical = $false
            }
        }
        $linkReceipts = @($observations | ForEach-Object { $_.receipt })
    }

    $allCanonical = (
        $discoveryCodes.Count -eq 0 -and
        $linkReceipts.Count -eq 2 -and
        @($linkReceipts | Where-Object { -not $_.canonical }).Count -eq 0
    )
    $receipt = New-Receipt `
        -Status $(if ($allCanonical) { 'VALID' } else { 'DRIFT' }) `
        -Canonical $allCanonical `
        -TargetLinkCount $targetLinkCount `
        -DiscoveryCodes $discoveryCodes `
        -Links $linkReceipts `
        -FailureCode $null
}
catch {
    $receipt = New-Receipt `
        -Status 'FAILED' `
        -Canonical $null `
        -TargetLinkCount $targetLinkCount `
        -DiscoveryCodes $discoveryCodes `
        -Links $linkReceipts `
        -FailureCode (Get-SafeFailureCode -ErrorRecord $_)
}

[Console]::Out.WriteLine(($receipt | ConvertTo-Json -Depth 8 -Compress))
if ($receipt.status -ceq 'FAILED') { exit 1 }
if ($receipt.status -ceq 'DRIFT') { exit 2 }
exit 0
