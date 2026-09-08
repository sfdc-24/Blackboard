#Requires -Version 5.1
<#
.SYNOPSIS
Stages an immutable ORDER task-escrow tool below the stable guest tools root.

.DESCRIPTION
The caller supplies the exact tool bytes as canonical Base64 plus their
lowercase SHA-256 digest.  The tool is installed as:

  %ProgramData%\SFDC24\OrderSupervisor\tools\order_task_escrow.<sha256>.ps1

The destination is never overwritten.  A replay or concurrent winner is
accepted only after a complete digest, inventory, identity, and reparse-point
read-back.  Historical versions may coexist only when every tools-root entry
is a regular digest-qualified file whose content matches its embedded digest;
partials and unrelated entries fail closed.  The payload is non-secret
deployment material, but neither it nor file contents are included in receipts.
#>
[CmdletBinding()]
param(
    [AllowEmptyString()][string]$Payload = '',
    [AllowEmptyString()][string]$Sha256 = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:OrderEscrowTransportReceiptSchema = 'blackboard.order-escrow-tool-stage-receipt.v1'
$script:OrderEscrowTransportPrefix = 'order_task_escrow.'
$script:OrderEscrowTransportSuffix = '.ps1'
$script:OrderEscrowTransportMaxBase64Characters = 2000000
$script:OrderEscrowTransportMaxBytes = 1500000
$script:OrderEscrowTransportMaxInventory = 64

function Assert-OrderEscrowTransportRuntime {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or
        $PSVersionTable.PSVersion.Major -ne 5 -or
        $PSVersionTable.PSVersion.Minor -lt 1) {
        throw 'windows_powershell_5_1_required'
    }
}

function Get-OrderEscrowTransportBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $sha.Dispose()
    }
}

function Get-OrderEscrowTransportFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    $sha = $null
    try {
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $sha = [Security.Cryptography.SHA256]::Create()
        return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    } catch {
        throw 'tool_file_digest_failed'
    } finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Test-OrderEscrowTransportReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)

    return ([int]$Item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0
}

function Get-OrderEscrowTransportChildMatches {
    param(
        [Parameter(Mandatory = $true)][IO.DirectoryInfo]$Parent,
        [Parameter(Mandatory = $true)][string]$Name
    )

    try {
        return @(
            $Parent.EnumerateFileSystemInfos($Name, [IO.SearchOption]::TopDirectoryOnly) |
                Where-Object { $_.Name -ieq $Name }
        )
    } catch {
        throw 'tools_path_inventory_failed'
    }
}

function Assert-OrderEscrowTransportDirectoryEntry {
    param(
        [Parameter(Mandatory = $true)]$Item,
        [Parameter(Mandatory = $true)][string]$ExpectedName
    )

    if ($Item.Name -cne $ExpectedName) { throw 'tools_path_identity_mismatch' }
    if (Test-OrderEscrowTransportReparsePoint -Item $Item) { throw 'tools_path_reparse_point' }
    if (-not ($Item -is [IO.DirectoryInfo])) { throw 'tools_path_component_not_directory' }
}

function Assert-OrderEscrowTransportExistingAncestorChain {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        $volumeRoot = [IO.Path]::GetPathRoot($fullPath)
    } catch {
        throw 'program_data_invalid'
    }
    if ([string]::IsNullOrWhiteSpace($volumeRoot)) { throw 'program_data_invalid' }

    try { $current = Get-Item -LiteralPath $volumeRoot -Force -ErrorAction Stop }
    catch { throw 'program_data_volume_missing' }
    if (Test-OrderEscrowTransportReparsePoint -Item $current) { throw 'tools_path_reparse_point' }
    if (-not ($current -is [IO.DirectoryInfo])) { throw 'tools_path_component_not_directory' }

    $relative = $fullPath.Substring($volumeRoot.Length)
    $segments = @(
        $relative.Split(
            [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
            [StringSplitOptions]::RemoveEmptyEntries
        )
    )
    foreach ($segment in $segments) {
        $matches = @(Get-OrderEscrowTransportChildMatches -Parent $current -Name $segment)
        if ($matches.Count -eq 0) { throw 'program_data_component_missing' }
        if ($matches.Count -ne 1) { throw 'tools_path_collision' }
        Assert-OrderEscrowTransportDirectoryEntry -Item $matches[0] -ExpectedName $segment
        $current = $matches[0]
    }
    return $current
}

function Get-OrderEscrowTransportContext {
    $rawProgramData = [string]$env:ProgramData
    if ([string]::IsNullOrWhiteSpace($rawProgramData) -or
        -not [IO.Path]::IsPathRooted($rawProgramData)) {
        throw 'program_data_invalid'
    }
    try {
        $programData = [IO.Path]::GetFullPath($rawProgramData)
        $volumeRoot = [IO.Path]::GetPathRoot($programData)
        if ($programData.Length -gt $volumeRoot.Length) {
            $programData = $programData.TrimEnd(
                [IO.Path]::DirectorySeparatorChar,
                [IO.Path]::AltDirectorySeparatorChar
            )
        }
    } catch {
        throw 'program_data_invalid'
    }
    if ([string]::IsNullOrWhiteSpace($programData) -or
        [string]::IsNullOrWhiteSpace($volumeRoot)) {
        throw 'program_data_invalid'
    }
    $rawTrimmed = $rawProgramData
    if ($rawTrimmed.Length -gt $volumeRoot.Length) {
        $rawTrimmed = $rawTrimmed.TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
    }
    if (-not [string]::Equals($rawTrimmed, $programData, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'program_data_not_canonical'
    }
    if ([string]::Equals($programData, $volumeRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'program_data_volume_root_forbidden'
    }
    if ($programData.Length -gt 256) { throw 'program_data_path_too_long' }

    $toolsRoot = [IO.Path]::GetFullPath((Join-Path $programData 'SFDC24\OrderSupervisor\tools')).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    return [pscustomobject][ordered]@{
        program_data = $programData
        tools_root = $toolsRoot
    }
}

function Ensure-OrderEscrowTransportToolsRoot {
    param([Parameter(Mandatory = $true)]$Context)

    $current = Assert-OrderEscrowTransportExistingAncestorChain -Path ([string]$Context.program_data)
    foreach ($segment in @('SFDC24', 'OrderSupervisor', 'tools')) {
        $matches = @(Get-OrderEscrowTransportChildMatches -Parent $current -Name $segment)
        if ($matches.Count -gt 1) { throw 'tools_path_collision' }
        if ($matches.Count -eq 0) {
            $candidate = Join-Path $current.FullName $segment
            try { [IO.Directory]::CreateDirectory($candidate) | Out-Null }
            catch { throw 'tools_path_create_failed' }
            $matches = @(Get-OrderEscrowTransportChildMatches -Parent $current -Name $segment)
        }
        if ($matches.Count -ne 1) { throw 'tools_path_create_readback_failed' }
        Assert-OrderEscrowTransportDirectoryEntry -Item $matches[0] -ExpectedName $segment
        $current = $matches[0]
    }
    if ([IO.Path]::GetFullPath($current.FullName).TrimEnd('\', '/') -cne [string]$Context.tools_root) {
        throw 'tools_path_identity_mismatch'
    }
    return $current
}

function Assert-OrderEscrowTransportToolsRoot {
    param([Parameter(Mandatory = $true)]$Context)

    $current = Assert-OrderEscrowTransportExistingAncestorChain -Path ([string]$Context.program_data)
    foreach ($segment in @('SFDC24', 'OrderSupervisor', 'tools')) {
        $matches = @(Get-OrderEscrowTransportChildMatches -Parent $current -Name $segment)
        if ($matches.Count -eq 0) { throw 'tools_path_missing' }
        if ($matches.Count -ne 1) { throw 'tools_path_collision' }
        Assert-OrderEscrowTransportDirectoryEntry -Item $matches[0] -ExpectedName $segment
        $current = $matches[0]
    }
    if ([IO.Path]::GetFullPath($current.FullName).TrimEnd('\', '/') -cne [string]$Context.tools_root) {
        throw 'tools_path_identity_mismatch'
    }
    return $current
}

function Get-OrderEscrowTransportFinalName {
    param([Parameter(Mandatory = $true)][string]$Digest)

    if ($Digest -cnotmatch '^[0-9a-f]{64}$') { throw 'sha256_invalid' }
    return $script:OrderEscrowTransportPrefix + $Digest + $script:OrderEscrowTransportSuffix
}

function Assert-OrderEscrowTransportFileEntry {
    param(
        [Parameter(Mandatory = $true)]$Item,
        [Parameter(Mandatory = $true)][string]$ExpectedName
    )

    if ($Item.Name -cne $ExpectedName) { throw 'tool_file_identity_mismatch' }
    if (Test-OrderEscrowTransportReparsePoint -Item $Item) { throw 'tools_inventory_reparse_point' }
    if (-not ($Item -is [IO.FileInfo])) { throw 'tools_inventory_entry_not_file' }
}

function Get-OrderEscrowTransportInventory {
    param([Parameter(Mandatory = $true)]$Context)

    $root = Assert-OrderEscrowTransportToolsRoot -Context $Context
    try { $entries = @($root.EnumerateFileSystemInfos('*', [IO.SearchOption]::TopDirectoryOnly)) }
    catch { throw 'tools_inventory_read_failed' }
    if ($entries.Count -gt $script:OrderEscrowTransportMaxInventory) {
        throw 'tools_inventory_too_large'
    }

    $inventory = New-Object System.Collections.Generic.List[object]
    foreach ($entry in @($entries | Sort-Object Name)) {
        if (Test-OrderEscrowTransportReparsePoint -Item $entry) {
            throw 'tools_inventory_reparse_point'
        }
        if (-not ($entry -is [IO.FileInfo])) { throw 'tools_inventory_entry_not_file' }
        if ($entry.Name -cnotmatch '^order_task_escrow\.([0-9a-f]{64})\.ps1$') {
            throw 'tools_inventory_name_invalid'
        }
        $embeddedDigest = [string]$Matches[1]
        $actualDigest = Get-OrderEscrowTransportFileSha256 -Path $entry.FullName
        if ($actualDigest -cne $embeddedDigest) { throw 'tools_inventory_digest_mismatch' }
        $inventory.Add([pscustomobject][ordered]@{
            name = [string]$entry.Name
            sha256 = $actualDigest
            byte_count = [long]$entry.Length
        })
    }
    return $inventory.ToArray()
}

function Get-OrderEscrowTransportFinalEntry {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $root = Assert-OrderEscrowTransportToolsRoot -Context $Context
    $matches = @(Get-OrderEscrowTransportChildMatches -Parent $root -Name $Name)
    if ($matches.Count -gt 1) { throw 'tool_file_path_collision' }
    if ($matches.Count -eq 0) { return $null }
    Assert-OrderEscrowTransportFileEntry -Item $matches[0] -ExpectedName $Name
    return $matches[0]
}

function Assert-OrderEscrowTransportFinalFile {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Digest,
        [Parameter(Mandatory = $true)][long]$ByteCount
    )

    $entry = Get-OrderEscrowTransportFinalEntry -Context $Context -Name $Name
    if ($null -eq $entry) { throw 'tool_file_readback_missing' }
    if ([long]$entry.Length -ne $ByteCount) { throw 'existing_tool_length_mismatch' }
    $actualDigest = Get-OrderEscrowTransportFileSha256 -Path $entry.FullName
    if ($actualDigest -cne $Digest) { throw 'existing_tool_digest_mismatch' }
    return $entry
}

function Write-OrderEscrowTransportTemporaryFile {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$Digest
    )

    $root = Assert-OrderEscrowTransportToolsRoot -Context $Context
    $fullPath = [IO.Path]::GetFullPath($Path)
    if ([IO.Path]::GetDirectoryName($fullPath).TrimEnd('\', '/') -cne $root.FullName.TrimEnd('\', '/') -or
        [IO.Path]::GetFileName($fullPath) -cnotmatch ('^\.order_task_escrow\.' + [Regex]::Escape($Digest) + '\.tmp\.[0-9a-f]{32}\.ps1$')) {
        throw 'temporary_path_scope_invalid'
    }

    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $fullPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None,
            4096,
            [IO.FileOptions]::WriteThrough
        )
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    } catch {
        throw 'temporary_file_create_failed'
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }

    $root = Assert-OrderEscrowTransportToolsRoot -Context $Context
    $name = [IO.Path]::GetFileName($fullPath)
    $matches = @(Get-OrderEscrowTransportChildMatches -Parent $root -Name $name)
    if ($matches.Count -ne 1) { throw 'temporary_file_readback_missing' }
    if ($matches[0].Name -cne $name -or
        (Test-OrderEscrowTransportReparsePoint -Item $matches[0]) -or
        -not ($matches[0] -is [IO.FileInfo])) {
        throw 'temporary_file_readback_unsafe'
    }
    if ([long]$matches[0].Length -ne [long]$Bytes.Length -or
        (Get-OrderEscrowTransportFileSha256 -Path $matches[0].FullName) -cne $Digest) {
        throw 'temporary_file_digest_mismatch'
    }
}

function Move-OrderEscrowTransportFileNoOverwrite {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    [IO.File]::Move($Source, $Destination)
}

function Remove-OrderEscrowTransportTemporaryFile {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Digest
    )

    $root = Assert-OrderEscrowTransportToolsRoot -Context $Context
    $fullPath = [IO.Path]::GetFullPath($Path)
    $name = [IO.Path]::GetFileName($fullPath)
    if ([IO.Path]::GetDirectoryName($fullPath).TrimEnd('\', '/') -cne $root.FullName.TrimEnd('\', '/') -or
        $name -cnotmatch ('^\.order_task_escrow\.' + [Regex]::Escape($Digest) + '\.tmp\.[0-9a-f]{32}\.ps1$')) {
        throw 'temporary_cleanup_scope_invalid'
    }

    $matches = @(Get-OrderEscrowTransportChildMatches -Parent $root -Name $name)
    if ($matches.Count -eq 0) { return }
    if ($matches.Count -ne 1 -or
        $matches[0].Name -cne $name -or
        (Test-OrderEscrowTransportReparsePoint -Item $matches[0]) -or
        -not ($matches[0] -is [IO.FileInfo])) {
        throw 'temporary_cleanup_unsafe'
    }
    try { [IO.File]::Delete($matches[0].FullName) }
    catch { throw 'temporary_cleanup_failed' }
    $matches = @(Get-OrderEscrowTransportChildMatches -Parent $root -Name $name)
    if ($matches.Count -ne 0) { throw 'temporary_cleanup_readback_failed' }
}

function New-OrderEscrowTransportPreservedFailure {
    param(
        [Parameter(Mandatory = $true)]$PrimaryRecord,
        [Parameter(Mandatory = $true)]$CleanupRecord
    )

    $exception = New-Object InvalidOperationException(
        ([string]$PrimaryRecord.Exception.Message),
        $PrimaryRecord.Exception
    )
    $cleanupCode = Get-OrderEscrowTransportSafeErrorCode -Record $CleanupRecord
    $exception.Data['cleanup_code'] = $cleanupCode
    return $exception
}

function Assert-OrderEscrowTransportReadback {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Digest,
        [Parameter(Mandatory = $true)][long]$ByteCount
    )

    $file = Assert-OrderEscrowTransportFinalFile `
        -Context $Context `
        -Name $Name `
        -Digest $Digest `
        -ByteCount $ByteCount
    $inventory = @(Get-OrderEscrowTransportInventory -Context $Context)
    $matches = @($inventory | Where-Object {
        $_.name -ceq $Name -and
        $_.sha256 -ceq $Digest -and
        [long]$_.byte_count -eq $ByteCount
    })
    if ($matches.Count -ne 1) { throw 'tools_inventory_readback_mismatch' }
    return [pscustomobject][ordered]@{
        file = $file
        inventory_count = $inventory.Count
    }
}

function Invoke-OrderEscrowTransportStage {
    param(
        [AllowEmptyString()][string]$EncodedPayload,
        [AllowEmptyString()][string]$ExpectedSha256
    )

    Assert-OrderEscrowTransportRuntime
    if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'sha256_invalid' }
    if ([string]::IsNullOrEmpty($EncodedPayload) -or
        $EncodedPayload.Length -gt $script:OrderEscrowTransportMaxBase64Characters) {
        throw 'payload_base64_invalid'
    }
    try { [byte[]]$bytes = [Convert]::FromBase64String($EncodedPayload) }
    catch { throw 'payload_base64_invalid' }
    if ([Convert]::ToBase64String($bytes) -cne $EncodedPayload) {
        throw 'payload_base64_noncanonical'
    }
    if ($bytes.Length -eq 0 -or $bytes.Length -gt $script:OrderEscrowTransportMaxBytes) {
        throw 'payload_size_invalid'
    }
    if ((Get-OrderEscrowTransportBytesSha256 -Bytes $bytes) -cne $ExpectedSha256) {
        throw 'payload_digest_mismatch'
    }

    $context = Get-OrderEscrowTransportContext
    $null = Ensure-OrderEscrowTransportToolsRoot -Context $context
    $null = @(Get-OrderEscrowTransportInventory -Context $context)

    $finalName = Get-OrderEscrowTransportFinalName -Digest $ExpectedSha256
    $finalPath = Join-Path ([string]$context.tools_root) $finalName
    if ($finalPath.Length -gt 512) { throw 'tool_path_too_long' }
    $existing = Get-OrderEscrowTransportFinalEntry -Context $context -Name $finalName
    if ($null -ne $existing) {
        $readback = Assert-OrderEscrowTransportReadback `
            -Context $context `
            -Name $finalName `
            -Digest $ExpectedSha256 `
            -ByteCount $bytes.Length
        return [pscustomobject][ordered]@{
            schema = $script:OrderEscrowTransportReceiptSchema
            ok = $true
            status = 'ALREADY_STAGED'
            sha256 = $ExpectedSha256
            byte_count = [long]$bytes.Length
            file_name = $finalName
            tool_path = [string]$readback.file.FullName
            inventory_file_count = [int]$readback.inventory_count
        }
    }

    $temporaryName = '.' + $script:OrderEscrowTransportPrefix + $ExpectedSha256 +
        '.tmp.' + [Guid]::NewGuid().ToString('N') + $script:OrderEscrowTransportSuffix
    $temporaryPath = Join-Path ([string]$context.tools_root) $temporaryName
    $status = ''
    $primaryFailure = $null
    try {
        Write-OrderEscrowTransportTemporaryFile `
            -Context $context `
            -Path $temporaryPath `
            -Bytes $bytes `
            -Digest $ExpectedSha256
        $null = Assert-OrderEscrowTransportToolsRoot -Context $context
        $status = 'STAGED'
        try {
            Move-OrderEscrowTransportFileNoOverwrite -Source $temporaryPath -Destination $finalPath
        } catch {
            $winner = Get-OrderEscrowTransportFinalEntry -Context $context -Name $finalName
            if ($null -eq $winner) { throw 'tool_atomic_move_failed' }
            $null = Assert-OrderEscrowTransportFinalFile `
                -Context $context `
                -Name $finalName `
                -Digest $ExpectedSha256 `
                -ByteCount $bytes.Length
            $status = 'ALREADY_STAGED'
        }

    } catch {
        $primaryFailure = $_
    }

    $cleanupFailure = $null
    try {
        Remove-OrderEscrowTransportTemporaryFile `
            -Context $context `
            -Path $temporaryPath `
            -Digest $ExpectedSha256
    } catch {
        $cleanupFailure = $_
    }
    if ($null -ne $primaryFailure) {
        if ($null -ne $cleanupFailure) {
            throw (New-OrderEscrowTransportPreservedFailure `
                -PrimaryRecord $primaryFailure `
                -CleanupRecord $cleanupFailure)
        }
        throw $primaryFailure
    }
    if ($null -ne $cleanupFailure) { throw $cleanupFailure }
    $readback = Assert-OrderEscrowTransportReadback `
        -Context $context `
        -Name $finalName `
        -Digest $ExpectedSha256 `
        -ByteCount $bytes.Length
    return [pscustomobject][ordered]@{
        schema = $script:OrderEscrowTransportReceiptSchema
        ok = $true
        status = $status
        sha256 = $ExpectedSha256
        byte_count = [long]$bytes.Length
        file_name = $finalName
        tool_path = [string]$readback.file.FullName
        inventory_file_count = [int]$readback.inventory_count
    }
}

function Get-OrderEscrowTransportSafeErrorCode {
    param([Parameter(Mandatory = $true)]$Record)

    $message = [string]$Record.Exception.Message
    if ($message -cmatch '^[a-z][a-z0-9_]{0,95}$') { return $message }
    return 'order_escrow_transport_failed'
}

if ($MyInvocation.InvocationName -cne '.') {
    try {
        $receipt = Invoke-OrderEscrowTransportStage `
            -EncodedPayload $Payload `
            -ExpectedSha256 $Sha256
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Compress))
        exit 0
    } catch {
        $errorReceipt = [ordered]@{
            schema = $script:OrderEscrowTransportReceiptSchema
            ok = $false
            action = 'Stage'
            code = (Get-OrderEscrowTransportSafeErrorCode -Record $_)
        }
        if ($_.Exception.Data.Contains('cleanup_code')) {
            $cleanupCode = [string]$_.Exception.Data['cleanup_code']
            if ($cleanupCode -cmatch '^[a-z][a-z0-9_]{0,95}$') {
                $errorReceipt.cleanup_code = $cleanupCode
            }
        }
        [Console]::Error.WriteLine(($errorReceipt | ConvertTo-Json -Compress))
        exit 1
    }
}
