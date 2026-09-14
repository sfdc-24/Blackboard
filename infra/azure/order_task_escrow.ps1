#Requires -Version 5.1
<#
.SYNOPSIS
Creates, validates, or restores a verified escrow of the live ORDER task.

.DESCRIPTION
Validate is the non-mutating default.  An escrow is bound to one known-good,
immutable release and is stored as a new directory directly below:

  %ProgramData%\SFDC24\OrderSupervisor\acceptance

The bundle contains the Task Scheduler XML (UTF-16 LE with BOM) and a strict
UTF-8 manifest.  Restore validates the entire bundle and the six release files
before it stops or replaces the current managed task.  This tool deliberately
has no candidate-release parameter: the rollback asset remains independent of
the release being evaluated.

Create requires TrustedFileHashesBase64: Base64 of a UTF-8 JSON object whose
only properties are the six canonical release-relative paths and whose values
are independently derived lowercase SHA-256 digests.  The trusted map must be
produced from the verified source/package, never from the guest directory that
the tool is being asked to escrow.  Validate and Restore use the trusted map
sealed into the escrow manifest and do not require this parameter.
#>
[CmdletBinding()]
param(
    [string]$Action = 'Validate',

    [string]$ExpectedReleaseId = '',

    [string]$EscrowId = '',

    [string]$TrustedFileHashesBase64 = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:OrderEscrowTaskName = 'SFDC24 Blackboard Order Worker'
$script:OrderEscrowTaskPath = '\'
$script:OrderEscrowManagedMarker = 'managed-by=install_order_supervisor.ps1; schema=v1'
$script:OrderEscrowSchema = 'blackboard.order-task-escrow.v1'
$script:OrderEscrowReceiptSchema = 'blackboard.order-task-escrow-receipt.v1'
$script:OrderEscrowTaskNamespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$script:OrderEscrowReleaseFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)

function Assert-OrderReleaseId {
    param([AllowEmptyString()][string]$Value)

    if ($Value -cnotmatch '^[0-9a-f]{40}$') {
        throw 'expected_release_id_invalid'
    }
}

function Assert-OrderEscrowId {
    param([AllowEmptyString()][string]$Value)

    if ($Value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' -or
        $Value -ceq '.' -or $Value -ceq '..') {
        throw 'escrow_id_invalid'
    }
}

function Test-OrderAdministrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Assert-OrderMutationPreflight {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
        throw 'mutations_require_windows_powershell_5_1'
    }
    if (-not (Test-OrderAdministrator)) { throw 'administrator_required_for_system_task' }
}

function Test-OrderReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)

    return ([int]$Item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0
}

function Assert-OrderSafeDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw $MissingCode }
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { throw $UnsafeCode }
    if (-not $item.PSIsContainer -or (Test-OrderReparsePoint -Item $item)) { throw $UnsafeCode }
}

function Assert-OrderSafeFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw $MissingCode }
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { throw $UnsafeCode }
    if ($item.PSIsContainer -or (Test-OrderReparsePoint -Item $item)) { throw $UnsafeCode }
}

function Assert-OrderFullAncestorChain {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )

    try {
        $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        $pathRoot = [IO.Path]::GetPathRoot($fullPath)
    } catch { throw $UnsafeCode }
    if ([string]::IsNullOrWhiteSpace($pathRoot)) { throw $UnsafeCode }
    Assert-OrderSafeDirectory -Path $pathRoot -MissingCode $MissingCode -UnsafeCode $UnsafeCode
    $current = $pathRoot
    $remainder = $fullPath.Substring($pathRoot.Length)
    foreach ($segment in @($remainder -split '[\\/]' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $current = Join-Path $current $segment
        Assert-OrderSafeDirectory -Path $current -MissingCode $MissingCode -UnsafeCode $UnsafeCode
    }
}

function Get-OrderEscrowContext {
    if ([string]::IsNullOrWhiteSpace($env:ProgramData) -or
        -not [IO.Path]::IsPathRooted($env:ProgramData)) {
        throw 'program_data_invalid'
    }

    try { $programData = [IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\') }
    catch { throw 'program_data_invalid' }
    if ([string]::IsNullOrWhiteSpace($programData)) { throw 'program_data_invalid' }

    $metadataRoot = [IO.Path]::GetFullPath((Join-Path $programData 'SFDC24\OrderSupervisor'))
    $acceptanceRoot = [IO.Path]::GetFullPath((Join-Path $metadataRoot 'acceptance'))
    $releaseRoot = [IO.Path]::GetFullPath((Join-Path $metadataRoot 'releases'))
    return [pscustomobject][ordered]@{
        program_data = $programData
        metadata_root = $metadataRoot
        acceptance_root = $acceptanceRoot
        release_root = $releaseRoot
    }
}

function Ensure-OrderAcceptanceRoot {
    param([Parameter(Mandatory = $true)]$Context)

    Assert-OrderFullAncestorChain `
        -Path ([string]$Context.program_data) `
        -MissingCode 'acceptance_root_missing' `
        -UnsafeCode 'acceptance_root_unsafe'
    $segments = @(
        [string]$Context.program_data,
        (Join-Path ([string]$Context.program_data) 'SFDC24'),
        [string]$Context.metadata_root,
        [string]$Context.acceptance_root
    )
    foreach ($segment in $segments) {
        if (-not (Test-Path -LiteralPath $segment)) {
            try { New-Item -ItemType Directory -Path $segment -ErrorAction Stop | Out-Null }
            catch { throw 'acceptance_root_create_failed' }
        }
        Assert-OrderSafeDirectory -Path $segment -MissingCode 'acceptance_root_missing' -UnsafeCode 'acceptance_root_unsafe'
    }
}

function Assert-OrderContextPathChain {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][ValidateSet('Acceptance', 'Release')][string]$Purpose
    )

    $purposePrefix = $Purpose.ToLowerInvariant()
    Assert-OrderFullAncestorChain `
        -Path ([string]$Context.program_data) `
        -MissingCode ($purposePrefix + '_path_component_missing') `
        -UnsafeCode ($purposePrefix + '_path_component_unsafe')
    $paths = @(
        [string]$Context.program_data,
        (Join-Path ([string]$Context.program_data) 'SFDC24'),
        [string]$Context.metadata_root
    )
    if ($Purpose -ceq 'Acceptance') { $paths += [string]$Context.acceptance_root }
    else { $paths += [string]$Context.release_root }
    foreach ($path in $paths) {
        Assert-OrderSafeDirectory `
            -Path $path `
            -MissingCode ($purposePrefix + '_path_component_missing') `
            -UnsafeCode ($purposePrefix + '_path_component_unsafe')
    }
}

function Get-OrderEscrowPath {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Id
    )

    Assert-OrderEscrowId -Value $Id
    try { $candidate = [IO.Path]::GetFullPath((Join-Path ([string]$Context.acceptance_root) $Id)).TrimEnd('\') }
    catch { throw 'escrow_path_invalid' }
    $expectedParent = ([string]$Context.acceptance_root).TrimEnd('\')
    $actualParent = [IO.Path]::GetDirectoryName($candidate).TrimEnd('\')
    if ($actualParent -cne $expectedParent -or [IO.Path]::GetFileName($candidate) -cne $Id) {
        throw 'escrow_path_outside_acceptance_root'
    }
    return $candidate
}

function Assert-OrderBundlePath {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedName,
        [switch]$Temporary
    )

    try { $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\') }
    catch { throw 'escrow_path_invalid' }
    $expectedParent = ([string]$Context.acceptance_root).TrimEnd('\')
    $actualParent = [IO.Path]::GetDirectoryName($fullPath).TrimEnd('\')
    if ($actualParent -cne $expectedParent) { throw 'escrow_path_outside_acceptance_root' }
    $actualName = [IO.Path]::GetFileName($fullPath)
    if ($Temporary) {
        $temporaryPattern = '^\.' + [Text.RegularExpressions.Regex]::Escape($ExpectedName) + '\.tmp\.[0-9a-f]{32}$'
        if ($actualName -cnotmatch $temporaryPattern) { throw 'escrow_temporary_path_invalid' }
    } elseif ($actualName -cne $ExpectedName) {
        throw 'escrow_path_outside_acceptance_root'
    }
    Assert-OrderSafeDirectory -Path $fullPath -MissingCode 'escrow_bundle_missing' -UnsafeCode 'escrow_bundle_unsafe'
    return $fullPath
}

function Get-OrderFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    $sha = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $sha = [Security.Cryptography.SHA256]::Create()
        return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    } catch {
        throw 'file_digest_failed'
    } finally {
        if ($sha) { $sha.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Get-OrderUnicodeTextSha256 {
    param([Parameter(Mandatory = $true)][string]$Text)

    $encoding = New-Object Text.UnicodeEncoding($false, $true)
    [byte[]]$bytes = @($encoding.GetPreamble()) + @($encoding.GetBytes($Text))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Write-OrderNewTextFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][ValidateSet('Unicode', 'Utf8')][string]$Encoding
    )

    if (Test-Path -LiteralPath $Path) { throw 'escrow_file_already_exists' }
    $encoder = if ($Encoding -ceq 'Unicode') {
        New-Object Text.UnicodeEncoding($false, $true)
    } else {
        New-Object Text.UTF8Encoding($false, $true)
    }
    [byte[]]$bytes = @($encoder.GetPreamble()) + @($encoder.GetBytes($Text))
    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $Path,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } catch {
        throw 'escrow_file_create_failed'
    } finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Read-OrderUnicodeXmlFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($Path) }
    catch { throw 'escrow_task_xml_read_failed' }
    if ($bytes.Length -lt 2 -or $bytes[0] -ne 0xff -or $bytes[1] -ne 0xfe) {
        throw 'escrow_task_xml_encoding_invalid'
    }
    try {
        $decoder = New-Object Text.UnicodeEncoding($false, $true, $true)
        return $decoder.GetString($bytes, 2, $bytes.Length - 2)
    } catch {
        throw 'escrow_task_xml_encoding_invalid'
    }
}

function Assert-OrderNoDuplicateJsonKeys {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$ErrorPrefix
    )

    try { Add-Type -AssemblyName System.Runtime.Serialization -ErrorAction Stop }
    catch { throw ($ErrorPrefix + '_json_invalid') }
    $reader = $null
    try {
        $reader = [Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader(
            $Bytes,
            [Xml.XmlDictionaryReaderQuotas]::Max
        )
        $document = New-Object Xml.XmlDocument
        $document.XmlResolver = $null
        $document.Load($reader)
    } catch { throw ($ErrorPrefix + '_json_invalid') }
    finally { if ($reader) { $reader.Close() } }

    $pending = New-Object System.Collections.Queue
    $pending.Enqueue($document.DocumentElement)
    while ($pending.Count -gt 0) {
        $node = $pending.Dequeue()
        if ($node.NodeType -ne [Xml.XmlNodeType]::Element) { continue }
        if ($node.GetAttribute('type') -ceq 'object') {
            $keys = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
            foreach ($child in @($node.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })) {
                $key = if ($child.NamespaceURI -ceq 'item' -and $child.LocalName -ceq 'item') {
                    [string]$child.GetAttribute('item')
                } else { [string]$child.LocalName }
                if (-not $keys.Add($key)) { throw ($ErrorPrefix + '_duplicate_key') }
            }
        }
        foreach ($child in @($node.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })) {
            $pending.Enqueue($child)
        }
    }
}

function Assert-OrderUtcRoundTripTimestamp {
    param(
        [AllowEmptyString()][string]$Value,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    if ($Value -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}Z$') {
        throw $ErrorCode
    }
    $parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParseExact(
        $Value,
        'o',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$parsed
    ) -or $parsed.Offset -ne [TimeSpan]::Zero) {
        throw $ErrorCode
    }
}

function Read-OrderUtf8JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$ErrorPrefix = 'escrow_manifest'
    )

    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($Path) }
    catch { throw ($ErrorPrefix + '_read_failed') }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xef -and $bytes[1] -eq 0xbb -and $bytes[2] -eq 0xbf) {
        throw ($ErrorPrefix + '_encoding_invalid')
    }
    try {
        $decoder = New-Object Text.UTF8Encoding($false, $true)
        $text = $decoder.GetString($bytes)
    } catch {
        throw ($ErrorPrefix + '_encoding_invalid')
    }
    Assert-OrderNoDuplicateJsonKeys -Bytes $bytes -ErrorPrefix $ErrorPrefix
    try { return $text | ConvertFrom-Json -ErrorAction Stop }
    catch { throw ($ErrorPrefix + '_json_invalid') }
}

function Read-OrderTrustedFileHashes {
    param([AllowEmptyString()][string]$Base64)

    if ([string]::IsNullOrWhiteSpace($Base64)) { throw 'trusted_file_hashes_missing' }
    try { [byte[]]$bytes = [Convert]::FromBase64String($Base64) }
    catch { throw 'trusted_file_hashes_base64_invalid' }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xef -and $bytes[1] -eq 0xbb -and $bytes[2] -eq 0xbf) {
        throw 'trusted_file_hashes_encoding_invalid'
    }
    try {
        $decoder = New-Object Text.UTF8Encoding($false, $true)
        $text = $decoder.GetString($bytes)
    } catch { throw 'trusted_file_hashes_encoding_invalid' }
    Assert-OrderNoDuplicateJsonKeys -Bytes $bytes -ErrorPrefix 'trusted_file_hashes'
    try { $hashMap = $text | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'trusted_file_hashes_json_invalid' }
    Assert-OrderExactProperties `
        -Object $hashMap `
        -Expected $script:OrderEscrowReleaseFiles `
        -ErrorCode 'trusted_file_hashes_shape_invalid'

    $entries = New-Object System.Collections.Generic.List[object]
    foreach ($relativePath in $script:OrderEscrowReleaseFiles) {
        $hash = $hashMap.$relativePath
        if ($hash -isnot [string] -or [string]$hash -cnotmatch '^[0-9a-f]{64}$') {
            throw 'trusted_file_hashes_value_invalid'
        }
        $entries.Add([pscustomobject][ordered]@{ path = $relativePath; sha256 = [string]$hash })
    }
    return $entries.ToArray()
}

function Assert-OrderExactProperties {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    if ($null -eq $Object -or $Object -is [System.Array] -or $Object -is [string]) { throw $ErrorCode }
    $actual = @($Object.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($actual.Count -ne $Expected.Count) { throw $ErrorCode }
    foreach ($name in $Expected) {
        if ($actual -cnotcontains $name) { throw $ErrorCode }
    }
}

function Get-OrderReleaseEvidence {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ReleaseId
    )

    Assert-OrderReleaseId -Value $ReleaseId
    $releaseRoot = [string]$Context.release_root
    Assert-OrderContextPathChain -Context $Context -Purpose Release
    $releasePath = [IO.Path]::GetFullPath((Join-Path $releaseRoot $ReleaseId))
    if ([IO.Path]::GetDirectoryName($releasePath).TrimEnd('\') -cne $releaseRoot.TrimEnd('\')) {
        throw 'release_path_outside_release_root'
    }
    Assert-OrderSafeDirectory -Path $releasePath -MissingCode 'expected_release_missing' -UnsafeCode 'expected_release_unsafe'
    $scriptsPath = Join-Path $releasePath 'scripts'
    Assert-OrderSafeDirectory -Path $scriptsPath -MissingCode 'expected_release_scripts_missing' -UnsafeCode 'expected_release_unsafe'

    try { $rootEntries = @(Get-ChildItem -LiteralPath $releasePath -Force -ErrorAction Stop) }
    catch { throw 'expected_release_inventory_failed' }
    if ($rootEntries.Count -ne 2 -or
        @($rootEntries | Where-Object { $_.Name -ceq '.release.json' -and -not $_.PSIsContainer }).Count -ne 1 -or
        @($rootEntries | Where-Object { $_.Name -ceq 'scripts' -and $_.PSIsContainer }).Count -ne 1) {
        throw 'expected_release_inventory_invalid'
    }
    foreach ($rootEntry in $rootEntries) {
        if (Test-OrderReparsePoint -Item $rootEntry) { throw 'expected_release_unsafe' }
    }

    $releaseManifestPath = Join-Path $releasePath '.release.json'
    $releaseManifest = Read-OrderUtf8JsonFile -Path $releaseManifestPath -ErrorPrefix 'release_manifest'
    $legacyProperties = @('schema', 'release_id', 'archive_sha256', 'installed_at_utc')
    $currentProperties = @('schema', 'release_id', 'archive_sha256', 'installed_at_utc', 'file_sha256')
    $actualManifestProperties = @($releaseManifest.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $manifestKind = ''
    try {
        if ($actualManifestProperties.Count -eq $legacyProperties.Count) {
            Assert-OrderExactProperties -Object $releaseManifest -Expected $legacyProperties -ErrorCode 'release_manifest_shape_invalid'
            $manifestKind = 'legacy'
        } elseif ($actualManifestProperties.Count -eq $currentProperties.Count) {
            Assert-OrderExactProperties -Object $releaseManifest -Expected $currentProperties -ErrorCode 'release_manifest_shape_invalid'
            $manifestKind = 'current'
        } else {
            throw 'release_manifest_shape_invalid'
        }
    } catch { throw 'release_manifest_shape_invalid' }
    foreach ($property in $legacyProperties) {
        if ($releaseManifest.$property -isnot [string]) { throw 'release_manifest_type_invalid' }
    }
    if ([string]$releaseManifest.schema -cne 'blackboard.order-worker-release.v1' -or
        [string]$releaseManifest.release_id -cne $ReleaseId -or
        [string]$releaseManifest.archive_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'release_manifest_value_invalid'
    }
    Assert-OrderUtcRoundTripTimestamp `
        -Value ([string]$releaseManifest.installed_at_utc) `
        -ErrorCode 'release_manifest_timestamp_invalid'

    try { $scriptEntries = @(Get-ChildItem -LiteralPath $scriptsPath -Force -ErrorAction Stop) }
    catch { throw 'expected_release_inventory_failed' }
    if ($scriptEntries.Count -ne $script:OrderEscrowReleaseFiles.Count -or
        @($scriptEntries | Where-Object { $_.PSIsContainer }).Count -ne 0) {
        throw 'expected_release_inventory_invalid'
    }

    $entries = New-Object System.Collections.Generic.List[object]
    foreach ($relativePath in $script:OrderEscrowReleaseFiles) {
        $fullPath = [IO.Path]::GetFullPath((Join-Path $releasePath $relativePath))
        if (-not $fullPath.StartsWith($releasePath.TrimEnd('\') + '\', [StringComparison]::Ordinal)) {
            throw 'release_file_path_outside_release'
        }
        Assert-OrderSafeFile -Path $fullPath -MissingCode 'expected_release_file_missing' -UnsafeCode 'expected_release_unsafe'
        $entries.Add([pscustomobject][ordered]@{
            path = $relativePath
            sha256 = (Get-OrderFileSha256 -Path $fullPath)
        })
    }
    $releaseFiles = $entries.ToArray()
    if ($manifestKind -ceq 'current') {
        if ($null -eq $releaseManifest.file_sha256 -or
            $releaseManifest.file_sha256 -is [System.Array] -or
            $releaseManifest.file_sha256 -is [string]) {
            throw 'release_manifest_file_hashes_type_invalid'
        }
        Assert-OrderExactProperties `
            -Object $releaseManifest.file_sha256 `
            -Expected $script:OrderEscrowReleaseFiles `
            -ErrorCode 'release_manifest_file_hashes_shape_invalid'
        foreach ($entry in $releaseFiles) {
            $recordedHash = $releaseManifest.file_sha256.([string]$entry.path)
            if ($recordedHash -isnot [string] -or
                [string]$recordedHash -cnotmatch '^[0-9a-f]{64}$' -or
                [string]$recordedHash -cne [string]$entry.sha256) {
                throw 'release_manifest_file_digest_mismatch'
            }
        }
    }
    return [pscustomobject][ordered]@{
        manifest_schema = [string]$releaseManifest.schema
        archive_sha256 = [string]$releaseManifest.archive_sha256
        manifest_sha256 = (Get-OrderFileSha256 -Path $releaseManifestPath)
        release_files = @($releaseFiles)
    }
}

function Get-OrderTaskMatches {
    try { return @(Get-ScheduledTask -TaskName $script:OrderEscrowTaskName -ErrorAction Stop) }
    catch {
        if ($_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*' -or
            $_.Exception.Message -match 'cannot find|No MSFT_ScheduledTask') {
            return @()
        }
        throw 'task_query_failed'
    }
}

function Get-OrderSingleRootTask {
    param([Parameter(Mandatory = $true)][string]$MissingCode)

    $matches = @(Get-OrderTaskMatches)
    if ($matches.Count -eq 0) { throw $MissingCode }
    if ($matches.Count -ne 1) { throw 'task_name_ambiguous_across_folders' }
    if ([string]$matches[0].TaskName -cne $script:OrderEscrowTaskName -or
        [string]$matches[0].TaskPath -cne $script:OrderEscrowTaskPath) {
        throw 'task_name_exists_outside_root'
    }
    return $matches[0]
}

function Assert-OrderManagedTask {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    $description = [string]$Task.Description
    if (-not $description.Contains($script:OrderEscrowManagedMarker)) { throw $ErrorCode }
}

function Get-OrderExpectedRunnerPath {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ReleaseId
    )

    return [IO.Path]::GetFullPath((Join-Path ([string]$Context.release_root) ($ReleaseId + '\scripts\order_supervisor.ps1')))
}

function Assert-OrderActionArguments {
    param(
        [AllowEmptyString()][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$ExpectedRunnerPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]$Context
    )

    $pattern = '^-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        '-File "(?<runner>[^"]+)" -Mode Execute -AllowedSourcesCsv "chat-mobile,codex" ' +
        '-UserProfilePath "(?<profile>[^"]+)" -WorkspacePath "(?<workspace>[^"]+)" ' +
        '-EnvFile "(?<env>[^"]+)" -StatePath "(?<state>[^"]+)" ' +
        '-LogPath "(?<log>[^"]+)" -WallTimeoutSeconds 720 -ClaudeCommand "(?<claude>[^"]+)"\z'
    $match = [Text.RegularExpressions.Regex]::Match(
        $Arguments,
        $pattern,
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $match.Success) { throw 'task_action_arguments_invalid' }
    if ([string]$match.Groups['runner'].Value -cne $ExpectedRunnerPath) { throw 'task_action_release_mismatch' }

    $profilePath = 'C:\Users\akatiawam'
    $workspacePath = Join-Path $profilePath 'Blackboard'
    $expectedPaths = [ordered]@{
        profile = $profilePath
        workspace = $workspacePath
        env = (Join-Path $workspacePath '.env')
        state = (Join-Path ([string]$Context.metadata_root) 'state.json')
        log = (Join-Path ([string]$Context.metadata_root) 'events.jsonl')
        claude = (Join-Path $profilePath '.local\bin\claude.exe')
    }
    foreach ($groupName in $expectedPaths.Keys) {
        $actualPath = [string]$match.Groups[$groupName].Value
        if (-not [IO.Path]::IsPathRooted($actualPath) -or
            $actualPath -cne [string]$expectedPaths[$groupName]) {
            throw 'task_action_path_contract_invalid'
        }
    }
    if ($WorkingDirectory -cne $workspacePath) {
        throw 'task_action_path_contract_invalid'
    }
}

function Assert-OrderLiveTaskSafe {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ReleaseId
    )

    Assert-OrderManagedTask -Task $Task -ErrorCode 'task_unmanaged'
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) { throw 'task_action_count_invalid' }
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if ([string]$actions[0].Execute -cne $windowsPowerShell) { throw 'task_action_execute_invalid' }
    if ([string]::IsNullOrWhiteSpace([string]$actions[0].WorkingDirectory) -or
        -not [IO.Path]::IsPathRooted([string]$actions[0].WorkingDirectory)) {
        throw 'task_working_directory_invalid'
    }
    Assert-OrderActionArguments `
        -Arguments ([string]$actions[0].Arguments) `
        -ExpectedRunnerPath (Get-OrderExpectedRunnerPath -Context $Context -ReleaseId $ReleaseId) `
        -WorkingDirectory ([string]$actions[0].WorkingDirectory) `
        -Context $Context

    $principalId = [string]$Task.Principal.UserId
    if (@('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -cnotcontains $principalId -or
        [string]$Task.Principal.LogonType -cne 'ServiceAccount') {
        throw 'task_principal_invalid'
    }
    if ([string]$Task.Settings.MultipleInstances -cne 'IgnoreNew') { throw 'task_multiple_instances_invalid' }
    $triggers = @($Task.Triggers)
    if ($triggers.Count -ne 2) { throw 'task_trigger_count_invalid' }
    $boot = @($triggers | Where-Object { $_.Id -ceq 'AtBoot' -and $_.CimClass.CimClassName -ceq 'MSFT_TaskBootTrigger' })
    $interval = @($triggers | Where-Object { $_.Id -ceq 'Every15Minutes' -and $_.CimClass.CimClassName -ceq 'MSFT_TaskTimeTrigger' })
    if ($boot.Count -ne 1) { throw 'task_boot_trigger_invalid' }
    if ($interval.Count -ne 1 -or [string]$interval[0].Repetition.Interval -cne 'PT15M') {
        throw 'task_interval_trigger_invalid'
    }
}

function Get-OrderXmlNodes {
    param(
        [Parameter(Mandatory = $true)][Xml.XmlDocument]$Document,
        [Parameter(Mandatory = $true)][Xml.XmlNamespaceManager]$NamespaceManager,
        [Parameter(Mandatory = $true)][string]$XPath,
        [Parameter(Mandatory = $true)][int]$Count,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    $nodes = @($Document.SelectNodes($XPath, $NamespaceManager))
    if ($nodes.Count -ne $Count) { throw $ErrorCode }
    return $nodes
}

function Get-OrderXmlText {
    param(
        [Parameter(Mandatory = $true)][Xml.XmlDocument]$Document,
        [Parameter(Mandatory = $true)][Xml.XmlNamespaceManager]$NamespaceManager,
        [Parameter(Mandatory = $true)][string]$XPath,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    $nodes = @(Get-OrderXmlNodes -Document $Document -NamespaceManager $NamespaceManager -XPath $XPath -Count 1 -ErrorCode $ErrorCode)
    return [string]$nodes[0].InnerText
}

function Assert-OrderTaskXml {
    param(
        [Parameter(Mandatory = $true)][string]$XmlText,
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ReleaseId
    )

    $document = New-Object Xml.XmlDocument
    $document.PreserveWhitespace = $true
    $document.XmlResolver = $null
    try { $document.LoadXml($XmlText) }
    catch { throw 'escrow_task_xml_invalid' }
    if ($document.DocumentElement.LocalName -cne 'Task' -or
        $document.DocumentElement.NamespaceURI -cne $script:OrderEscrowTaskNamespace) {
        throw 'escrow_task_xml_root_invalid'
    }
    $namespace = New-Object Xml.XmlNamespaceManager($document.NameTable)
    $namespace.AddNamespace('t', $script:OrderEscrowTaskNamespace)

    if ((Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:RegistrationInfo/t:URI' -ErrorCode 'task_xml_identity_missing') -cne ('\' + $script:OrderEscrowTaskName)) {
        throw 'task_xml_identity_invalid'
    }
    $description = Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:RegistrationInfo/t:Description' -ErrorCode 'task_xml_description_missing'
    if (-not $description.Contains($script:OrderEscrowManagedMarker)) { throw 'task_xml_unmanaged' }

    $actions = @(Get-OrderXmlNodes -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Actions/*' -Count 1 -ErrorCode 'task_xml_action_count_invalid')
    if ($actions[0].LocalName -cne 'Exec') { throw 'task_xml_action_type_invalid' }
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if ((Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Actions/t:Exec/t:Command' -ErrorCode 'task_xml_command_missing') -cne $windowsPowerShell) {
        throw 'task_xml_command_invalid'
    }
    $arguments = Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Actions/t:Exec/t:Arguments' -ErrorCode 'task_xml_arguments_missing'
    $workingDirectory = Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Actions/t:Exec/t:WorkingDirectory' -ErrorCode 'task_xml_working_directory_missing'
    if ([string]::IsNullOrWhiteSpace($workingDirectory) -or -not [IO.Path]::IsPathRooted($workingDirectory)) {
        throw 'task_xml_working_directory_invalid'
    }
    Assert-OrderActionArguments `
        -Arguments $arguments `
        -ExpectedRunnerPath (Get-OrderExpectedRunnerPath -Context $Context -ReleaseId $ReleaseId) `
        -WorkingDirectory $workingDirectory `
        -Context $Context

    $principals = @(Get-OrderXmlNodes -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Principals/t:Principal' -Count 1 -ErrorCode 'task_xml_principal_count_invalid')
    $principalId = Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Principals/t:Principal/t:UserId' -ErrorCode 'task_xml_principal_missing'
    $principalIdElements = @($principals[0].ChildNodes | Where-Object {
        $_.NodeType -eq [Xml.XmlNodeType]::Element -and $_.LocalName -ceq 'UserId'
    })
    if ($principalIdElements.Count -ne 1 -or
        [string]$principalIdElements[0].NamespaceURI -cne $script:OrderEscrowTaskNamespace -or
        @('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -cnotcontains $principalId) {
        throw 'task_xml_principal_invalid'
    }
    # Export-ScheduledTask can omit the optional LogonType element for the
    # canonical S-1-5-18 identity even though the live CIM principal reports
    # the required ServiceAccount value.  The live-task check above remains
    # strict. Treat foreign-namespace lookalikes as invalid, and permit true
    # omission only for the observed canonical SID representation.
    $logonTypeElements = @($principals[0].ChildNodes | Where-Object {
        $_.NodeType -eq [Xml.XmlNodeType]::Element -and $_.LocalName -ceq 'LogonType'
    })
    if ($logonTypeElements.Count -eq 0) {
        if ($principalId -cne 'S-1-5-18') { throw 'task_xml_logon_type_missing' }
    } elseif ($logonTypeElements.Count -ne 1 -or
        [string]$logonTypeElements[0].NamespaceURI -cne $script:OrderEscrowTaskNamespace -or
        [string]$logonTypeElements[0].InnerText -cne 'ServiceAccount') {
        throw 'task_xml_logon_type_invalid'
    }
    if ((Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Principals/t:Principal/t:RunLevel' -ErrorCode 'task_xml_run_level_missing') -cne 'HighestAvailable') {
        throw 'task_xml_run_level_invalid'
    }

    $triggers = @(Get-OrderXmlNodes -Document $document -NamespaceManager $namespace -XPath '/t:Task/t:Triggers/*' -Count 2 -ErrorCode 'task_xml_trigger_count_invalid')
    $boot = @($triggers | Where-Object { $_.LocalName -ceq 'BootTrigger' -and $_.SelectSingleNode('t:Id', $namespace).InnerText -ceq 'AtBoot' })
    $interval = @($triggers | Where-Object { $_.LocalName -ceq 'TimeTrigger' -and $_.SelectSingleNode('t:Id', $namespace).InnerText -ceq 'Every15Minutes' })
    if ($boot.Count -ne 1) { throw 'task_xml_boot_trigger_invalid' }
    if ($interval.Count -ne 1) { throw 'task_xml_interval_trigger_invalid' }
    $repetition = $interval[0].SelectSingleNode('t:Repetition/t:Interval', $namespace)
    if ($null -eq $repetition -or [string]$repetition.InnerText -cne 'PT15M') { throw 'task_xml_interval_invalid' }

    $settings = @{
        '/t:Task/t:Settings/t:MultipleInstancesPolicy' = 'IgnoreNew'
        '/t:Task/t:Settings/t:ExecutionTimeLimit' = 'PT2H'
        '/t:Task/t:Settings/t:StartWhenAvailable' = 'true'
        '/t:Task/t:Settings/t:DisallowStartIfOnBatteries' = 'false'
        '/t:Task/t:Settings/t:StopIfGoingOnBatteries' = 'false'
    }
    foreach ($settingPath in $settings.Keys) {
        if ((Get-OrderXmlText -Document $document -NamespaceManager $namespace -XPath $settingPath -ErrorCode 'task_xml_setting_missing') -cne [string]$settings[$settingPath]) {
            throw 'task_xml_setting_invalid'
        }
    }
}

function Export-OrderTaskXml {
    try { return [string](Export-ScheduledTask -TaskName $script:OrderEscrowTaskName -TaskPath $script:OrderEscrowTaskPath -ErrorAction Stop) }
    catch { throw 'task_export_failed' }
}

function Assert-OrderManifest {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ExpectedEscrowId,
        [Parameter(Mandatory = $true)][string]$ExpectedReleaseId
    )

    $properties = @(
        'schema', 'escrow_id', 'task_name', 'task_path', 'managed_marker',
        'expected_release_id', 'created_at_utc', 'task_xml_file',
        'task_xml_encoding', 'task_xml_sha256', 'release_manifest_schema',
        'release_archive_sha256', 'release_manifest_sha256', 'release_files'
    )
    Assert-OrderExactProperties -Object $Manifest -Expected $properties -ErrorCode 'escrow_manifest_shape_invalid'
    foreach ($stringProperty in @($properties | Where-Object { $_ -cne 'release_files' })) {
        if ($Manifest.$stringProperty -isnot [string]) { throw 'escrow_manifest_type_invalid' }
    }
    if ([string]$Manifest.schema -cne $script:OrderEscrowSchema -or
        [string]$Manifest.escrow_id -cne $ExpectedEscrowId -or
        [string]$Manifest.task_name -cne $script:OrderEscrowTaskName -or
        [string]$Manifest.task_path -cne $script:OrderEscrowTaskPath -or
        [string]$Manifest.managed_marker -cne $script:OrderEscrowManagedMarker -or
        [string]$Manifest.expected_release_id -cne $ExpectedReleaseId -or
        [string]$Manifest.task_xml_file -cne 'task.xml' -or
        [string]$Manifest.task_xml_encoding -cne 'utf-16le-bom' -or
        [string]$Manifest.task_xml_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$Manifest.release_manifest_schema -cne 'blackboard.order-worker-release.v1' -or
        [string]$Manifest.release_archive_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$Manifest.release_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'escrow_manifest_value_invalid'
    }
    Assert-OrderUtcRoundTripTimestamp `
        -Value ([string]$Manifest.created_at_utc) `
        -ErrorCode 'escrow_manifest_timestamp_invalid'

    if ($Manifest.release_files -isnot [System.Array]) { throw 'escrow_manifest_release_files_type_invalid' }
    $releaseFiles = @($Manifest.release_files)
    if ($releaseFiles.Count -ne $script:OrderEscrowReleaseFiles.Count) { throw 'escrow_manifest_release_files_count_invalid' }
    for ($index = 0; $index -lt $releaseFiles.Count; $index++) {
        Assert-OrderExactProperties -Object $releaseFiles[$index] -Expected @('path', 'sha256') -ErrorCode 'escrow_manifest_release_file_shape_invalid'
        if ($releaseFiles[$index].path -isnot [string] -or $releaseFiles[$index].sha256 -isnot [string] -or
            [string]$releaseFiles[$index].path -cne $script:OrderEscrowReleaseFiles[$index] -or
            [string]$releaseFiles[$index].sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw 'escrow_manifest_release_file_value_invalid'
        }
    }
}

function Get-OrderValidatedEscrow {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$BundlePath,
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$ReleaseId,
        [switch]$Temporary
    )

    Assert-OrderReleaseId -Value $ReleaseId
    Assert-OrderEscrowId -Value $Id
    Assert-OrderContextPathChain -Context $Context -Purpose Acceptance
    $validatedPath = Assert-OrderBundlePath -Context $Context -Path $BundlePath -ExpectedName $Id -Temporary:$Temporary
    $manifestPath = Join-Path $validatedPath 'manifest.json'
    $xmlPath = Join-Path $validatedPath 'task.xml'
    try { $entries = @(Get-ChildItem -LiteralPath $validatedPath -Force -ErrorAction Stop) }
    catch { throw 'escrow_bundle_inventory_failed' }
    if ($entries.Count -ne 2 -or
        @($entries | Where-Object { $_.Name -ceq 'manifest.json' -and -not $_.PSIsContainer }).Count -ne 1 -or
        @($entries | Where-Object { $_.Name -ceq 'task.xml' -and -not $_.PSIsContainer }).Count -ne 1) {
        throw 'escrow_bundle_inventory_invalid'
    }
    foreach ($entry in $entries) {
        if (Test-OrderReparsePoint -Item $entry) { throw 'escrow_bundle_unsafe' }
    }
    Assert-OrderSafeFile -Path $manifestPath -MissingCode 'escrow_manifest_missing' -UnsafeCode 'escrow_bundle_unsafe'
    Assert-OrderSafeFile -Path $xmlPath -MissingCode 'escrow_task_xml_missing' -UnsafeCode 'escrow_bundle_unsafe'

    $manifest = Read-OrderUtf8JsonFile -Path $manifestPath
    Assert-OrderManifest -Manifest $manifest -ExpectedEscrowId $Id -ExpectedReleaseId $ReleaseId
    $actualXmlHash = Get-OrderFileSha256 -Path $xmlPath
    if ($actualXmlHash -cne [string]$manifest.task_xml_sha256) { throw 'escrow_task_xml_digest_mismatch' }
    $xmlText = Read-OrderUnicodeXmlFile -Path $xmlPath
    Assert-OrderTaskXml -XmlText $xmlText -Context $Context -ReleaseId $ReleaseId

    $releaseEvidence = Get-OrderReleaseEvidence -Context $Context -ReleaseId $ReleaseId
    if ([string]$releaseEvidence.manifest_schema -cne [string]$manifest.release_manifest_schema -or
        [string]$releaseEvidence.archive_sha256 -cne [string]$manifest.release_archive_sha256 -or
        [string]$releaseEvidence.manifest_sha256 -cne [string]$manifest.release_manifest_sha256) {
        throw 'escrow_release_manifest_digest_mismatch'
    }
    $actualReleaseFiles = @($releaseEvidence.release_files)
    for ($index = 0; $index -lt $actualReleaseFiles.Count; $index++) {
        if ([string]$actualReleaseFiles[$index].path -cne [string]$manifest.release_files[$index].path -or
            [string]$actualReleaseFiles[$index].sha256 -cne [string]$manifest.release_files[$index].sha256) {
            throw 'escrow_release_file_digest_mismatch'
        }
    }

    return [pscustomobject][ordered]@{
        path = $validatedPath
        manifest = $manifest
        xml_text = $xmlText
        xml_sha256 = $actualXmlHash
        release_files = $actualReleaseFiles
    }
}

function New-OrderEscrowReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptAction,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)]$Validated,
        [bool]$TaskStopped = $false
    )

    return [pscustomobject][ordered]@{
        schema = $script:OrderEscrowReceiptSchema
        ok = $true
        action = $ReceiptAction
        status = $Status
        escrow_id = [string]$Validated.manifest.escrow_id
        escrow_path = [string]$Validated.path
        task_name = $script:OrderEscrowTaskName
        task_path = $script:OrderEscrowTaskPath
        expected_release_id = [string]$Validated.manifest.expected_release_id
        task_xml_sha256 = [string]$Validated.xml_sha256
        release_file_count = @($Validated.release_files).Count
        task_stopped = $TaskStopped
    }
}

function Remove-OrderOwnedTemporaryBundle {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Id
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }
    $safePath = Assert-OrderBundlePath -Context $Context -Path $Path -ExpectedName $Id -Temporary
    foreach ($name in @('manifest.json', 'task.xml')) {
        $filePath = Join-Path $safePath $name
        if (Test-Path -LiteralPath $filePath -PathType Leaf) {
            Remove-Item -LiteralPath $filePath -Force -ErrorAction SilentlyContinue
        }
    }
    try {
        if (@([IO.Directory]::EnumerateFileSystemEntries($safePath)).Count -eq 0) {
            [IO.Directory]::Delete($safePath)
        }
    } catch {}
}

function New-OrderTaskEscrow {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$ReleaseId,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$TrustedHashesBase64
    )

    Assert-OrderReleaseId -Value $ReleaseId
    Assert-OrderEscrowId -Value $Id
    Assert-OrderMutationPreflight
    $context = Get-OrderEscrowContext
    $targetPath = Get-OrderEscrowPath -Context $context -Id $Id
    if (Test-Path -LiteralPath $targetPath) { throw 'escrow_already_exists' }

    $trustedReleaseFiles = @(Read-OrderTrustedFileHashes -Base64 $TrustedHashesBase64)
    $releaseEvidence = Get-OrderReleaseEvidence -Context $context -ReleaseId $ReleaseId
    $releaseFiles = @($releaseEvidence.release_files)
    for ($index = 0; $index -lt $releaseFiles.Count; $index++) {
        if ([string]$releaseFiles[$index].path -cne [string]$trustedReleaseFiles[$index].path -or
            [string]$releaseFiles[$index].sha256 -cne [string]$trustedReleaseFiles[$index].sha256) {
            throw 'trusted_release_file_digest_mismatch'
        }
    }
    $task = Get-OrderSingleRootTask -MissingCode 'escrow_source_task_missing'
    Assert-OrderLiveTaskSafe -Task $task -Context $context -ReleaseId $ReleaseId
    $xmlText = Export-OrderTaskXml
    Assert-OrderTaskXml -XmlText $xmlText -Context $context -ReleaseId $ReleaseId

    Ensure-OrderAcceptanceRoot -Context $context
    if (Test-Path -LiteralPath $targetPath) { throw 'escrow_already_exists' }
    $temporaryName = '.' + $Id + '.tmp.' + [Guid]::NewGuid().ToString('N')
    $temporaryPath = Join-Path ([string]$context.acceptance_root) $temporaryName
    try { New-Item -ItemType Directory -Path $temporaryPath -ErrorAction Stop | Out-Null }
    catch { throw 'escrow_temporary_create_failed' }

    try {
        $xmlPath = Join-Path $temporaryPath 'task.xml'
        Write-OrderNewTextFile -Path $xmlPath -Text $xmlText -Encoding Unicode
        $manifest = [ordered]@{
            schema = $script:OrderEscrowSchema
            escrow_id = $Id
            task_name = $script:OrderEscrowTaskName
            task_path = $script:OrderEscrowTaskPath
            managed_marker = $script:OrderEscrowManagedMarker
            expected_release_id = $ReleaseId
            created_at_utc = [DateTime]::UtcNow.ToString('o')
            task_xml_file = 'task.xml'
            task_xml_encoding = 'utf-16le-bom'
            task_xml_sha256 = (Get-OrderFileSha256 -Path $xmlPath)
            release_manifest_schema = [string]$releaseEvidence.manifest_schema
            release_archive_sha256 = [string]$releaseEvidence.archive_sha256
            release_manifest_sha256 = [string]$releaseEvidence.manifest_sha256
            release_files = @($trustedReleaseFiles)
        }
        $manifestText = $manifest | ConvertTo-Json -Depth 5
        Write-OrderNewTextFile -Path (Join-Path $temporaryPath 'manifest.json') -Text $manifestText -Encoding Utf8
        $null = Get-OrderValidatedEscrow -Context $context -BundlePath $temporaryPath -Id $Id -ReleaseId $ReleaseId -Temporary
        try { [IO.Directory]::Move($temporaryPath, $targetPath) }
        catch {
            if (Test-Path -LiteralPath $targetPath) { throw 'escrow_already_exists' }
            throw 'escrow_commit_failed'
        }
    } finally {
        Remove-OrderOwnedTemporaryBundle -Context $context -Path $temporaryPath -Id $Id
    }

    $validated = Get-OrderValidatedEscrow -Context $context -BundlePath $targetPath -Id $Id -ReleaseId $ReleaseId
    return New-OrderEscrowReceipt -ReceiptAction 'Create' -Status 'CREATED' -Validated $validated
}

function Test-OrderTaskEscrow {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$ReleaseId
    )

    Assert-OrderReleaseId -Value $ReleaseId
    Assert-OrderEscrowId -Value $Id
    $context = Get-OrderEscrowContext
    Assert-OrderSafeDirectory -Path ([string]$context.acceptance_root) -MissingCode 'acceptance_root_missing' -UnsafeCode 'acceptance_root_unsafe'
    $targetPath = Get-OrderEscrowPath -Context $context -Id $Id
    $validated = Get-OrderValidatedEscrow -Context $context -BundlePath $targetPath -Id $Id -ReleaseId $ReleaseId
    return New-OrderEscrowReceipt -ReceiptAction 'Validate' -Status 'VALID' -Validated $validated
}

function Stop-OrderCurrentTask {
    param([Parameter(Mandatory = $true)]$Task)

    $state = [string]$Task.State
    if (@('Ready', 'Disabled') -ccontains $state) { return $false }
    if (@('Running', 'Queued') -cnotcontains $state) { throw 'restore_current_task_state_invalid' }
    try { Stop-ScheduledTask -TaskName $script:OrderEscrowTaskName -TaskPath $script:OrderEscrowTaskPath -ErrorAction Stop }
    catch { throw 'restore_task_stop_failed' }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $current = Get-OrderSingleRootTask -MissingCode 'restore_current_task_disappeared'
        Assert-OrderManagedTask -Task $current -ErrorCode 'restore_current_task_unmanaged'
        $state = [string]$current.State
    } while (@('Running', 'Queued') -ccontains $state -and [DateTime]::UtcNow -lt $deadline)
    if (@('Running', 'Queued') -ccontains $state) { throw 'restore_task_stop_timeout' }
    if (@('Ready', 'Disabled') -cnotcontains $state) { throw 'restore_current_task_state_invalid' }
    return $true
}

function Restore-OrderTaskEscrow {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$ReleaseId
    )

    Assert-OrderReleaseId -Value $ReleaseId
    Assert-OrderEscrowId -Value $Id
    Assert-OrderMutationPreflight
    $context = Get-OrderEscrowContext
    Assert-OrderSafeDirectory -Path ([string]$context.acceptance_root) -MissingCode 'acceptance_root_missing' -UnsafeCode 'acceptance_root_unsafe'
    $targetPath = Get-OrderEscrowPath -Context $context -Id $Id

    # This is intentionally the first operation capable of reaching the live
    # task.  Every byte in the bundle and old immutable release is proven first.
    $validated = Get-OrderValidatedEscrow -Context $context -BundlePath $targetPath -Id $Id -ReleaseId $ReleaseId

    $current = Get-OrderSingleRootTask -MissingCode 'restore_current_task_missing'
    Assert-OrderManagedTask -Task $current -ErrorCode 'restore_current_task_unmanaged'
    $stopped = Stop-OrderCurrentTask -Task $current
    try {
        Register-ScheduledTask `
            -Xml ([string]$validated.xml_text) `
            -TaskName $script:OrderEscrowTaskName `
            -TaskPath $script:OrderEscrowTaskPath `
            -Force `
            -ErrorAction Stop | Out-Null
    } catch { throw 'restore_task_register_failed' }

    $readbackTask = Get-OrderSingleRootTask -MissingCode 'restore_task_readback_missing'
    Assert-OrderLiveTaskSafe -Task $readbackTask -Context $context -ReleaseId $ReleaseId
    $readbackXml = Export-OrderTaskXml
    if ($readbackXml -cne [string]$validated.xml_text -or
        (Get-OrderUnicodeTextSha256 -Text $readbackXml) -cne [string]$validated.xml_sha256) {
        throw 'restore_task_xml_readback_mismatch'
    }
    Assert-OrderTaskXml -XmlText $readbackXml -Context $context -ReleaseId $ReleaseId

    return New-OrderEscrowReceipt -ReceiptAction 'Restore' -Status 'RESTORED' -Validated $validated -TaskStopped $stopped
}

function Invoke-OrderTaskEscrow {
    param(
        [Parameter(Mandatory = $true)][string]$RequestedAction,
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$ReleaseId,
        [AllowEmptyString()][string]$TrustedHashesBase64 = ''
    )

    Assert-OrderReleaseId -Value $ReleaseId
    Assert-OrderEscrowId -Value $Id
    if ($RequestedAction -ieq 'Create') {
        return New-OrderTaskEscrow -Id $Id -ReleaseId $ReleaseId -TrustedHashesBase64 $TrustedHashesBase64
    }
    if ($RequestedAction -ieq 'Validate') { return Test-OrderTaskEscrow -Id $Id -ReleaseId $ReleaseId }
    if ($RequestedAction -ieq 'Restore') { return Restore-OrderTaskEscrow -Id $Id -ReleaseId $ReleaseId }
    throw 'action_invalid'
}

function Get-OrderSafeErrorCode {
    param([Parameter(Mandatory = $true)]$Record)

    $message = [string]$Record.Exception.Message
    if ($message -cmatch '^[a-z][a-z0-9_]{0,79}$') { return $message }
    return 'order_task_escrow_failed'
}

function Get-OrderSafeActionLabel {
    param([AllowEmptyString()][string]$Value)

    foreach ($known in @('Create', 'Validate', 'Restore')) {
        if ($Value -ieq $known) { return $known }
    }
    return 'Invalid'
}

if ($MyInvocation.InvocationName -cne '.') {
    try {
        $receipt = Invoke-OrderTaskEscrow `
            -RequestedAction $Action `
            -Id $EscrowId `
            -ReleaseId $ExpectedReleaseId `
            -TrustedHashesBase64 $TrustedFileHashesBase64
        $receipt | ConvertTo-Json -Compress
        exit 0
    } catch {
        $errorReceipt = [pscustomobject][ordered]@{
            schema = $script:OrderEscrowReceiptSchema
            ok = $false
            action = (Get-OrderSafeActionLabel -Value $Action)
            code = (Get-OrderSafeErrorCode -Record $_)
        }
        [Console]::Error.WriteLine(($errorReceipt | ConvertTo-Json -Compress))
        exit 1
    }
}
