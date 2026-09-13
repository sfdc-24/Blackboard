#Requires -Version 5.1
<#
.SYNOPSIS
    Installs an immutable ORDER-worker release delivered through Azure Run Command.

.DESCRIPTION
    The archive is supplied as a Base64 parameter so deployment does not depend
    on Git credentials inside the VM. The caller-bound payload is decoded,
    digest-checked, and validated as an exact six-file release before either a
    new install or an idempotent replay is accepted.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $Payload,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string] $ArchiveSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string] $ReleaseId,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string] $ReleaseRoot = 'C:\ProgramData\SFDC24\OrderSupervisor\releases'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$requiredFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$requiredDirectories = @('scripts\')
$requiredArchiveInventory = @($requiredDirectories + $requiredFiles)
$manifestFileName = '.release.json'
$manifestSchema = 'blackboard.order-worker-release.v1'

if (-not [IO.Path]::IsPathRooted($ReleaseRoot)) {
    throw 'release_root_must_be_absolute'
}
$resolvedReleaseRoot = [IO.Path]::GetFullPath($ReleaseRoot)
$releaseRootVolume = [IO.Path]::GetPathRoot($resolvedReleaseRoot)
if ($resolvedReleaseRoot.Length -gt $releaseRootVolume.Length) {
    $resolvedReleaseRoot = $resolvedReleaseRoot.TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
}
if ([string]::IsNullOrWhiteSpace($resolvedReleaseRoot)) {
    throw 'release_root_invalid'
}
if ([string]::Equals($resolvedReleaseRoot, $releaseRootVolume, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'release_root_volume_root_forbidden'
}

$destination = Join-Path $resolvedReleaseRoot $ReleaseId
$resolvedSystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$systemTempVolume = [IO.Path]::GetPathRoot($resolvedSystemTemp)
if ($resolvedSystemTemp.Length -gt $systemTempVolume.Length) {
    $resolvedSystemTemp = $resolvedSystemTemp.TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
}
$temporaryRoot = Join-Path $resolvedSystemTemp ('blackboard-release-' + [Guid]::NewGuid().ToString('N'))

function Get-ValidatedTemporaryCleanupPath {
    param(
        [Parameter(Mandatory = $true)][string] $TemporaryRoot,
        [Parameter(Mandatory = $true)][string] $SystemTemp
    )

    $resolvedTemporary = [IO.Path]::GetFullPath($TemporaryRoot)
    $resolvedTempBase = [IO.Path]::GetFullPath($SystemTemp)
    $temporaryVolume = [IO.Path]::GetPathRoot($resolvedTemporary)
    $tempBaseVolume = [IO.Path]::GetPathRoot($resolvedTempBase)
    if ($resolvedTemporary.Length -gt $temporaryVolume.Length) {
        $resolvedTemporary = $resolvedTemporary.TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
    }
    if ($resolvedTempBase.Length -gt $tempBaseVolume.Length) {
        $resolvedTempBase = $resolvedTempBase.TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
    }

    $systemTempPrefix = $resolvedTempBase
    if (
        -not $systemTempPrefix.EndsWith([string][IO.Path]::DirectorySeparatorChar) -and
        -not $systemTempPrefix.EndsWith([string][IO.Path]::AltDirectorySeparatorChar)
    ) {
        $systemTempPrefix += [IO.Path]::DirectorySeparatorChar
    }
    if (
        $resolvedTemporary -ceq $resolvedTempBase -or
        -not $resolvedTemporary.StartsWith($systemTempPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedTemporary) -cnotmatch '^blackboard-release-[0-9a-f]{32}$'
    ) {
        throw 'temporary_cleanup_scope_invalid'
    }
    return $resolvedTemporary
}

function Get-BytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]] $Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $sha.Dispose()
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string] $Path)

    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Get-ReleaseInventory {
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string] $ErrorPrefix
    )

    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $resolvedPathRoot = [IO.Path]::GetPathRoot($resolvedRoot)
    if ($resolvedRoot.Length -gt $resolvedPathRoot.Length) {
        $resolvedRoot = $resolvedRoot.TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
    }
    $rootPrefix = $resolvedRoot
    if (
        -not $rootPrefix.EndsWith([string][IO.Path]::DirectorySeparatorChar) -and
        -not $rootPrefix.EndsWith([string][IO.Path]::AltDirectorySeparatorChar)
    ) {
        $rootPrefix += [IO.Path]::DirectorySeparatorChar
    }
    $rootItem = Get-Item -LiteralPath $resolvedRoot -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ($ErrorPrefix + '_reparse_point:.')
    }
    if (-not $rootItem.PSIsContainer) {
        throw ($ErrorPrefix + '_not_directory')
    }

    $directories = New-Object System.Collections.Queue
    $files = New-Object 'System.Collections.Generic.List[string]'
    $directories.Enqueue($rootItem)
    while ($directories.Count -gt 0) {
        $directory = $directories.Dequeue()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory.FullName -Force)) {
            $resolvedItem = [IO.Path]::GetFullPath($item.FullName)
            if (-not $resolvedItem.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw ($ErrorPrefix + '_inventory_scope_invalid')
            }
            $relativePath = $resolvedItem.Substring($rootPrefix.Length).Replace('/', '\')
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ($ErrorPrefix + '_reparse_point:' + $relativePath)
            }
            if ($item.PSIsContainer) {
                $files.Add($relativePath + '\')
                $directories.Enqueue($item)
            }
            else {
                $files.Add($relativePath)
            }
        }
    }
    return @($files | Sort-Object)
}

function Assert-ExactInventory {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]] $Actual,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]] $Expected,
        [Parameter(Mandatory = $true)][string] $ErrorPrefix
    )

    $missing = @($Expected | Where-Object { $Actual -cnotcontains $_ })
    if ($missing.Count -gt 0) {
        throw ($ErrorPrefix + '_files_missing:' + ($missing -join ','))
    }
    $extra = @($Actual | Where-Object { $Expected -cnotcontains $_ })
    if ($extra.Count -gt 0) {
        throw ($ErrorPrefix + '_files_extra:' + ($extra -join ','))
    }
    if ($Actual.Count -ne $Expected.Count) {
        throw ($ErrorPrefix + '_files_duplicate')
    }
}

function Get-ArchiveInventory {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]] $Entries)

    $exactEntryPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $caseInsensitiveEntryPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $inventory = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($entry in $Entries) {
        $entryPath = ([string]$entry.FullName).Replace('/', '\')
        if ([string]::IsNullOrEmpty($entryPath)) {
            throw 'release_entry_path_invalid'
        }
        if (-not $exactEntryPaths.Add($entryPath)) {
            throw ('release_entries_duplicate:' + $entryPath)
        }
        if (-not $caseInsensitiveEntryPaths.Add($entryPath)) {
            throw ('release_entries_case_collision:' + $entryPath)
        }

        $isDirectory = [string]::IsNullOrEmpty([string]$entry.Name) -or $entryPath.EndsWith('\')
        if ($isDirectory) {
            $directoryPath = $entryPath.TrimEnd('\') + '\'
            $null = $inventory.Add($directoryPath)
            $cursor = $directoryPath.TrimEnd('\')
        }
        else {
            $null = $inventory.Add($entryPath)
            $cursor = $entryPath
        }
        while ($cursor.LastIndexOf('\') -ge 0) {
            $separatorIndex = $cursor.LastIndexOf('\')
            $directoryPath = $cursor.Substring(0, $separatorIndex + 1)
            $null = $inventory.Add($directoryPath)
            $cursor = $directoryPath.TrimEnd('\')
        }
    }
    return @($inventory | Sort-Object)
}

function Get-RequiredFileHashes {
    param([Parameter(Mandatory = $true)][string] $Root)

    $hashes = [ordered]@{}
    foreach ($relativePath in $requiredFiles) {
        $hashes[$relativePath] = Get-FileSha256 -Path (Join-Path $Root $relativePath)
    }
    return $hashes
}

function Get-ObjectPropertyNames {
    param([Parameter(Mandatory = $true)] $InputObject)

    return @($InputObject.PSObject.Properties | ForEach-Object { [string]$_.Name })
}

function Assert-ReleaseRootAncestorsSafe {
    param([Parameter(Mandatory = $true)][string] $Root)

    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $volumeRoot = [IO.Path]::GetPathRoot($resolvedRoot)
    $relativeRoot = $resolvedRoot.Substring($volumeRoot.Length)
    $segments = @(
        $relativeRoot.Split(
            [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
            [StringSplitOptions]::RemoveEmptyEntries
        )
    )
    $currentItem = Get-Item -LiteralPath $volumeRoot -Force
    if (($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ('release_root_reparse_point:' + $volumeRoot)
    }
    if (-not ($currentItem -is [IO.DirectoryInfo])) {
        throw ('release_root_ancestor_not_directory:' + $volumeRoot)
    }

    foreach ($segment in $segments) {
        $matches = @(
            $currentItem.EnumerateFileSystemInfos($segment, [IO.SearchOption]::TopDirectoryOnly) |
                Where-Object { $_.Name -ieq $segment }
        )
        if ($matches.Count -eq 0) { return }
        if ($matches.Count -gt 1) {
            throw ('release_root_path_collision:' + (Join-Path $currentItem.FullName $segment))
        }
        $currentItem = $matches[0]
        if (($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('release_root_reparse_point:' + $currentItem.FullName)
        }
        if (-not ($currentItem -is [IO.DirectoryInfo])) {
            throw ('release_root_ancestor_not_directory:' + $currentItem.FullName)
        }
    }
}

function Get-ExistingReleaseEntry {
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string] $Id
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $null
    }
    $matches = @(
        (Get-Item -LiteralPath $Root -Force).EnumerateFileSystemInfos(
            $Id,
            [IO.SearchOption]::TopDirectoryOnly
        ) |
            Where-Object { $_.Name -ieq $Id }
    )
    if ($matches.Count -gt 1) {
        throw 'existing_release_path_collision'
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    if ($matches[0].Name -cne $Id) {
        throw 'existing_release_identity_path_mismatch'
    }
    return $matches[0]
}

function Assert-NoDuplicateJsonKeys {
    param([Parameter(Mandatory = $true)][byte[]] $Bytes)

    Add-Type -AssemblyName System.Runtime.Serialization
    $reader = $null
    try {
        $reader = [Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader(
            $Bytes,
            [Xml.XmlDictionaryReaderQuotas]::Max
        )
        $document = New-Object Xml.XmlDocument
        $document.Load($reader)
    }
    catch {
        throw 'existing_release_manifest_invalid'
    }
    finally {
        if ($null -ne $reader) { $reader.Close() }
    }

    $nodes = New-Object System.Collections.Queue
    $nodes.Enqueue($document.DocumentElement)
    while ($nodes.Count -gt 0) {
        $node = $nodes.Dequeue()
        if ($node.NodeType -ne [Xml.XmlNodeType]::Element) { continue }
        if ($node.GetAttribute('type') -ceq 'object') {
            $keys = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
            foreach ($child in @($node.ChildNodes | Where-Object {
                $_.NodeType -eq [Xml.XmlNodeType]::Element
            })) {
                if ($child.NamespaceURI -ceq 'item' -and $child.LocalName -ceq 'item') {
                    $key = $child.GetAttribute('item')
                }
                else {
                    $key = $child.LocalName
                }
                if (-not $keys.Add([string]$key)) {
                    throw ('existing_release_manifest_duplicate_key:' + $key)
                }
            }
        }
        foreach ($child in @($node.ChildNodes | Where-Object {
            $_.NodeType -eq [Xml.XmlNodeType]::Element
        })) {
            $nodes.Enqueue($child)
        }
    }
}

function Assert-ExistingRelease {
    param(
        [Parameter(Mandatory = $true)][string] $ExistingDestination,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary] $ArchiveFileHashes
    )

    $expectedDestinationInventory = @($requiredDirectories + $requiredFiles + $manifestFileName)
    $destinationInventory = @(
        Get-ReleaseInventory -Root $ExistingDestination -ErrorPrefix 'existing_release'
    )
    $manifestPath = Join-Path $ExistingDestination $manifestFileName
    if ($destinationInventory -cnotcontains $manifestFileName) {
        throw 'existing_release_missing_manifest'
    }
    Assert-ExactInventory -Actual $destinationInventory -Expected $expectedDestinationInventory -ErrorPrefix 'existing_release'

    $manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
    if (
        $manifestBytes.Length -ge 3 -and
        $manifestBytes[0] -eq 0xEF -and
        $manifestBytes[1] -eq 0xBB -and
        $manifestBytes[2] -eq 0xBF
    ) {
        throw 'existing_release_manifest_utf8_bom_forbidden'
    }
    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    try {
        $manifestText = $strictUtf8.GetString($manifestBytes)
    }
    catch {
        throw 'existing_release_manifest_utf8_invalid'
    }
    try {
        Assert-NoDuplicateJsonKeys -Bytes $manifestBytes
    }
    catch {
        $jsonValidationError = [string]$_.Exception.Message
        if (
            $jsonValidationError -ceq 'existing_release_manifest_invalid' -or
            $jsonValidationError.StartsWith(
                'existing_release_manifest_duplicate_key:',
                [StringComparison]::Ordinal
            )
        ) {
            throw $jsonValidationError
        }
        throw 'existing_release_manifest_invalid'
    }
    $jsonParseArgs = @{ ErrorAction = 'Stop' }
    $convertFromJsonCommand = Get-Command ConvertFrom-Json -ErrorAction Stop
    if ($convertFromJsonCommand.Parameters.ContainsKey('DateKind')) {
        # PowerShell 7.5+ otherwise coerces ISO-8601 JSON strings to DateTime.
        # The manifest validator must see the exact signed text that was written.
        $jsonParseArgs['DateKind'] = 'String'
    }
    elseif ($PSVersionTable.PSEdition -cne 'Desktop') {
        # Windows PowerShell 5.1 already preserves strings. PowerShell Core
        # versions without -DateKind cannot parse this manifest faithfully.
        throw 'existing_release_manifest_json_date_coercion_unsafe'
    }
    try {
        $existing = ConvertFrom-Json -InputObject $manifestText @jsonParseArgs
    }
    catch {
        throw 'existing_release_manifest_invalid'
    }
    if ($null -eq $existing -or -not ($existing -is [pscustomobject])) {
        throw 'existing_release_manifest_invalid'
    }

    $expectedManifestProperties = @(
        'schema',
        'release_id',
        'archive_sha256',
        'installed_at_utc',
        'file_sha256'
    )
    $manifestProperties = @(Get-ObjectPropertyNames -InputObject $existing)
    try {
        Assert-ExactInventory -Actual $manifestProperties -Expected $expectedManifestProperties -ErrorPrefix 'existing_release_manifest'
    }
    catch {
        throw 'existing_release_manifest_invalid'
    }

    if (
        -not ($existing.schema -is [string]) -or
        -not ($existing.release_id -is [string]) -or
        -not ($existing.archive_sha256 -is [string]) -or
        -not ($existing.installed_at_utc -is [string])
    ) {
        throw 'existing_release_manifest_invalid'
    }
    if ($existing.schema -cne $manifestSchema) {
        throw 'existing_release_manifest_invalid'
    }
    if ($existing.release_id -cne $ReleaseId) {
        throw 'existing_release_identity_mismatch'
    }
    if ($existing.archive_sha256 -cne $ArchiveSha256) {
        throw 'existing_release_archive_collision'
    }
    if ($existing.installed_at_utc -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}Z$') {
        throw 'existing_release_manifest_timestamp_invalid'
    }
    $installedAt = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParseExact(
        [string]$existing.installed_at_utc,
        'o',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$installedAt
    ) -or $installedAt.Offset -ne [TimeSpan]::Zero) {
        throw 'existing_release_manifest_timestamp_invalid'
    }
    if ($null -eq $existing.file_sha256 -or -not ($existing.file_sha256 -is [pscustomobject])) {
        throw 'existing_release_manifest_invalid'
    }
    $manifestFileProperties = @(Get-ObjectPropertyNames -InputObject $existing.file_sha256)
    try {
        Assert-ExactInventory -Actual $manifestFileProperties -Expected $requiredFiles -ErrorPrefix 'existing_release_manifest'
    }
    catch {
        throw 'existing_release_manifest_invalid'
    }

    foreach ($relativePath in $requiredFiles) {
        $manifestHashValue = $existing.file_sha256.PSObject.Properties[$relativePath].Value
        if (-not ($manifestHashValue -is [string])) {
            throw 'existing_release_manifest_invalid'
        }
        $manifestHash = [string]$manifestHashValue
        if ($manifestHash -cnotmatch '^[0-9a-f]{64}$') {
            throw 'existing_release_manifest_invalid'
        }
        if ($manifestHash -cne [string]$ArchiveFileHashes[$relativePath]) {
            throw ('existing_release_manifest_file_digest_mismatch:' + $relativePath)
        }
        $destinationHash = Get-FileSha256 -Path (Join-Path $ExistingDestination $relativePath)
        if ($destinationHash -cne [string]$ArchiveFileHashes[$relativePath]) {
            throw ('existing_release_file_digest_mismatch:' + $relativePath)
        }
    }
}

$resolvedTemporaryRoot = Get-ValidatedTemporaryCleanupPath `
    -TemporaryRoot $temporaryRoot `
    -SystemTemp $resolvedSystemTemp
$archivePath = Join-Path $resolvedTemporaryRoot 'release.zip'
$unpackPath = Join-Path $resolvedTemporaryRoot 'unpacked'

$result = $null
$operationError = $null
try {
    Assert-ReleaseRootAncestorsSafe -Root $resolvedReleaseRoot
    New-Item -ItemType Directory -Path $resolvedTemporaryRoot | Out-Null
    New-Item -ItemType Directory -Path $unpackPath | Out-Null

    try {
        $bytes = [Convert]::FromBase64String($Payload)
    }
    catch {
        throw 'archive_payload_invalid'
    }
    $actualSha256 = Get-BytesSha256 -Bytes $bytes
    if ($actualSha256 -cne $ArchiveSha256) {
        throw 'archive_digest_mismatch'
    }
    [IO.File]::WriteAllBytes($archivePath, $bytes)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try {
        $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
    }
    catch {
        throw 'archive_invalid'
    }
    try {
        $archiveInventory = @(Get-ArchiveInventory -Entries @($archive.Entries))
    }
    finally {
        $archive.Dispose()
    }
    Assert-ExactInventory -Actual $archiveInventory -Expected $requiredArchiveInventory -ErrorPrefix 'release'

    try {
        Expand-Archive -LiteralPath $archivePath -DestinationPath $unpackPath -Force
    }
    catch {
        throw 'archive_invalid'
    }
    $expandedInventory = @(Get-ReleaseInventory -Root $unpackPath -ErrorPrefix 'release')
    Assert-ExactInventory -Actual $expandedInventory -Expected $requiredArchiveInventory -ErrorPrefix 'release'
    $archiveFileHashes = Get-RequiredFileHashes -Root $unpackPath

    Assert-ReleaseRootAncestorsSafe -Root $resolvedReleaseRoot
    $existingRelease = Get-ExistingReleaseEntry -Root $resolvedReleaseRoot -Id $ReleaseId
    if ($null -ne $existingRelease) {
        Assert-ExistingRelease -ExistingDestination $destination -ArchiveFileHashes $archiveFileHashes
        $result = [ordered]@{
            status = 'ALREADY_INSTALLED'
            release_id = $ReleaseId
            archive_sha256 = $ArchiveSha256
            destination = $destination
            required_file_count = $requiredFiles.Count
        }
    }
    else {
        if (-not [string]::Equals(
            $releaseRootVolume,
            [IO.Path]::GetPathRoot($temporaryRoot),
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'release_root_volume_mismatch'
        }

        $manifest = [ordered]@{
            schema = $manifestSchema
            release_id = $ReleaseId
            archive_sha256 = $ArchiveSha256
            installed_at_utc = [DateTime]::UtcNow.ToString('o')
            file_sha256 = $archiveFileHashes
        } | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText(
            (Join-Path $unpackPath $manifestFileName),
            $manifest,
            (New-Object Text.UTF8Encoding($false))
        )

        New-Item -ItemType Directory -Path $resolvedReleaseRoot -Force | Out-Null
        Assert-ReleaseRootAncestorsSafe -Root $resolvedReleaseRoot
        $installed = $false
        try {
            [IO.Directory]::Move($unpackPath, $destination)
            $installed = $true
        }
        catch {
            $moveFailure = $_
            Assert-ReleaseRootAncestorsSafe -Root $resolvedReleaseRoot
            $racedRelease = Get-ExistingReleaseEntry -Root $resolvedReleaseRoot -Id $ReleaseId
            if ($null -ne $racedRelease) {
                Assert-ExistingRelease -ExistingDestination $destination -ArchiveFileHashes $archiveFileHashes
            }
            else {
                throw $moveFailure
            }
        }

        $result = [ordered]@{
            status = $(if ($installed) { 'INSTALLED' } else { 'ALREADY_INSTALLED' })
            release_id = $ReleaseId
            archive_sha256 = $ArchiveSha256
            destination = $destination
            required_file_count = $requiredFiles.Count
        }
    }

}
catch {
    $operationError = $_
}

$cleanupStatus = 'SUCCEEDED'
$cleanupCode = $null
try {
    if (Test-Path -LiteralPath $resolvedTemporaryRoot) {
        $cleanupPath = Get-ValidatedTemporaryCleanupPath `
            -TemporaryRoot $resolvedTemporaryRoot `
            -SystemTemp $resolvedSystemTemp
        Remove-Item -LiteralPath $cleanupPath -Recurse -Force -ErrorAction Stop
    }
}
catch {
    $cleanupStatus = 'FAILED'
    $cleanupCode = 'TEMPORARY_CLEANUP_FAILED'
}

if ($null -ne $operationError) {
    throw $operationError
}
if ($null -eq $result) {
    throw 'installer_result_missing'
}
$result['cleanup_status'] = $cleanupStatus
$result['cleanup_code'] = $cleanupCode
$result | ConvertTo-Json -Compress
