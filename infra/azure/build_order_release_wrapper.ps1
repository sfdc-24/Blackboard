#Requires -Version 5.1
<#
.SYNOPSIS
Builds a canonical Azure source.script wrapper for an ORDER release archive.

.DESCRIPTION
The six-file archive and archive installer are non-secret deployment inputs.
The builder validates their exact caller-bound SHA-256 digests, validates the
archive inventory, and emits a deterministic BOM-free wrapper.  At runtime the
wrapper revalidates both embedded byte streams before invoking the installer
in-process, avoiding Windows command-line payload limits.
#>
[CmdletBinding()]
param(
    [AllowEmptyString()][string]$ArchivePath = '',
    [AllowEmptyString()][string]$InstallerPath = '',
    [AllowEmptyString()][string]$OutputPath = '',
    [AllowEmptyString()][string]$ExpectedArchiveSha256 = '',
    [AllowEmptyString()][string]$ExpectedInstallerSha256 = '',
    [AllowEmptyString()][string]$ReleaseId = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:ReleaseWrapperReceiptSchema = 'blackboard.order-release-wrapper-build-receipt.v1'
$script:ReleaseWrapperMaxArchiveBytes = 10000000
$script:ReleaseWrapperMaxInstallerBytes = 1500000
$script:ReleaseWrapperFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$script:ReleaseWrapperInventory = @('scripts\') + $script:ReleaseWrapperFiles

function Get-ReleaseWrapperBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Read-ReleaseWrapperInputFile {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode,
        [Parameter(Mandatory = $true)][long]$MaximumBytes
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw $MissingCode
    }
    try { $fullPath = [IO.Path]::GetFullPath($Path) }
    catch { throw $MissingCode }
    try { $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop }
    catch { throw $MissingCode }
    if (-not ($item -is [IO.FileInfo]) -or
        ([int]$item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw $UnsafeCode
    }
    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($item.FullName) }
    catch { throw ($UnsafeCode + '_read_failed') }
    if ($bytes.Length -eq 0 -or $bytes.Length -gt $MaximumBytes) {
        throw ($UnsafeCode + '_size_invalid')
    }
    return [pscustomobject][ordered]@{
        path = [string]$item.FullName
        bytes = $bytes
        sha256 = Get-ReleaseWrapperBytesSha256 -Bytes $bytes
    }
}

function Assert-ReleaseWrapperInstallerUtf8 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    if ($Bytes.Length -ge 3 -and
        $Bytes[0] -eq 0xef -and $Bytes[1] -eq 0xbb -and $Bytes[2] -eq 0xbf) {
        throw 'installer_utf8_bom_forbidden'
    }
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $text = $utf8.GetString($Bytes)
    } catch { throw 'installer_utf8_invalid' }
    try { [void][ScriptBlock]::Create($text) }
    catch { throw 'installer_powershell_invalid' }
}

function Get-ReleaseWrapperArchiveInventory {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop }
    catch { throw 'archive_reader_unavailable' }
    try { $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath) }
    catch { throw 'archive_invalid' }
    try {
        $exactPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        $casePaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        $inventory = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($entry in @($archive.Entries)) {
            $entryPath = ([string]$entry.FullName).Replace('/', '\')
            if ([string]::IsNullOrWhiteSpace($entryPath) -or
                $entryPath.StartsWith('\') -or
                $entryPath.Contains(':')) {
                throw 'archive_entry_path_invalid'
            }
            if (-not $exactPaths.Add($entryPath)) { throw 'archive_entry_duplicate' }
            if (-not $casePaths.Add($entryPath)) { throw 'archive_entry_case_collision' }
            $segments = @($entryPath.TrimEnd('\').Split('\'))
            if ($segments.Count -eq 0 -or @($segments | Where-Object {
                [string]::IsNullOrWhiteSpace($_) -or $_ -ceq '.' -or $_ -ceq '..'
            }).Count -gt 0) {
                throw 'archive_entry_path_invalid'
            }

            $isDirectory = [string]::IsNullOrEmpty([string]$entry.Name) -or $entryPath.EndsWith('\')
            if ($isDirectory) {
                $normalized = $entryPath.TrimEnd('\') + '\'
                $null = $inventory.Add($normalized)
                $cursor = $normalized.TrimEnd('\')
            } else {
                $null = $inventory.Add($entryPath)
                $cursor = $entryPath
            }
            while ($cursor.LastIndexOf('\') -ge 0) {
                $separator = $cursor.LastIndexOf('\')
                $directory = $cursor.Substring(0, $separator + 1)
                $null = $inventory.Add($directory)
                $cursor = $directory.TrimEnd('\')
            }
        }
        return @($inventory | Sort-Object)
    } finally {
        $archive.Dispose()
    }
}

function Assert-ReleaseWrapperExactArchive {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    $actual = @(Get-ReleaseWrapperArchiveInventory -ArchivePath $ArchivePath)
    $missing = @($script:ReleaseWrapperInventory | Where-Object { $actual -cnotcontains $_ })
    if ($missing.Count -gt 0) { throw 'archive_files_missing' }
    $extra = @($actual | Where-Object { $script:ReleaseWrapperInventory -cnotcontains $_ })
    if ($extra.Count -gt 0) { throw 'archive_files_extra' }
    if ($actual.Count -ne $script:ReleaseWrapperInventory.Count) { throw 'archive_inventory_invalid' }
}

function Write-ReleaseWrapperNewUtf8File {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw 'output_path_invalid'
    }
    try { $fullPath = [IO.Path]::GetFullPath($Path) }
    catch { throw 'output_path_invalid' }
    $parentPath = [IO.Path]::GetDirectoryName($fullPath)
    try { $parent = Get-Item -LiteralPath $parentPath -Force -ErrorAction Stop }
    catch { throw 'output_parent_missing' }
    if (-not ($parent -is [IO.DirectoryInfo]) -or
        ([int]$parent.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'output_parent_unsafe'
    }
    if (Test-Path -LiteralPath $fullPath) { throw 'output_already_exists' }

    $utf8 = New-Object Text.UTF8Encoding($false, $true)
    [byte[]]$bytes = $utf8.GetBytes($Text)
    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $fullPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } catch { throw 'output_create_failed' }
    finally { if ($null -ne $stream) { $stream.Dispose() } }

    try { [byte[]]$readback = [IO.File]::ReadAllBytes($fullPath) }
    catch { throw 'output_readback_failed' }
    if ([Convert]::ToBase64String($readback) -cne [Convert]::ToBase64String($bytes)) {
        throw 'output_readback_mismatch'
    }
    return [pscustomobject][ordered]@{
        path = $fullPath
        bytes = $readback
        sha256 = Get-ReleaseWrapperBytesSha256 -Bytes $readback
    }
}

function New-OrderReleaseWrapper {
    param(
        [AllowEmptyString()][string]$SourceArchivePath,
        [AllowEmptyString()][string]$SourceInstallerPath,
        [AllowEmptyString()][string]$DestinationPath,
        [AllowEmptyString()][string]$ArchiveSha256,
        [AllowEmptyString()][string]$InstallerSha256,
        [AllowEmptyString()][string]$FullReleaseId
    )

    if ($ArchiveSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'archive_sha256_invalid' }
    if ($InstallerSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'installer_sha256_invalid' }
    if ($FullReleaseId -cnotmatch '^[0-9a-f]{40}$') { throw 'release_id_invalid' }
    $archive = Read-ReleaseWrapperInputFile `
        -Path $SourceArchivePath `
        -MissingCode 'archive_missing' `
        -UnsafeCode 'archive_unsafe' `
        -MaximumBytes $script:ReleaseWrapperMaxArchiveBytes
    $installer = Read-ReleaseWrapperInputFile `
        -Path $SourceInstallerPath `
        -MissingCode 'installer_missing' `
        -UnsafeCode 'installer_unsafe' `
        -MaximumBytes $script:ReleaseWrapperMaxInstallerBytes
    if ($archive.sha256 -cne $ArchiveSha256) { throw 'archive_digest_mismatch' }
    if ($installer.sha256 -cne $InstallerSha256) { throw 'installer_digest_mismatch' }
    Assert-ReleaseWrapperExactArchive -ArchivePath $archive.path
    Assert-ReleaseWrapperInstallerUtf8 -Bytes $installer.bytes

    $archivePayload = [Convert]::ToBase64String($archive.bytes)
    $installerPayload = [Convert]::ToBase64String($installer.bytes)
    $lines = @(
        '#Requires -Version 5.1',
        "`$ErrorActionPreference = 'Stop'",
        'Set-StrictMode -Version 2.0',
        "`$archiveBase64 = '$archivePayload'",
        "`$installerBase64 = '$installerPayload'",
        "`$expectedArchiveSha256 = '$ArchiveSha256'",
        "`$expectedInstallerSha256 = '$InstallerSha256'",
        "`$releaseId = '$FullReleaseId'",
        '$archiveBytes = [Convert]::FromBase64String($archiveBase64)',
        '$installerBytes = [Convert]::FromBase64String($installerBase64)',
        'function Get-EmbeddedSha256 {',
        '    param([Parameter(Mandatory = $true)][byte[]]$Bytes)',
        '    $sha = [Security.Cryptography.SHA256]::Create()',
        '    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString(''x2'') }) -join '''') }',
        '    finally { $sha.Dispose() }',
        '}',
        "if ((Get-EmbeddedSha256 -Bytes `$archiveBytes) -cne `$expectedArchiveSha256) { throw 'embedded_archive_digest_mismatch' }",
        "if ((Get-EmbeddedSha256 -Bytes `$installerBytes) -cne `$expectedInstallerSha256) { throw 'embedded_installer_digest_mismatch' }",
        '$strictUtf8 = New-Object Text.UTF8Encoding($false, $true)',
        'try { $installerText = $strictUtf8.GetString($installerBytes) }',
        "catch { throw 'embedded_installer_utf8_invalid' }",
        'try { $installer = [ScriptBlock]::Create($installerText) }',
        "catch { throw 'embedded_installer_powershell_invalid' }",
        "if ([string]::IsNullOrWhiteSpace(`$env:ProgramData) -or -not [IO.Path]::IsPathRooted(`$env:ProgramData)) { throw 'program_data_invalid' }",
        "`$releaseRoot = Join-Path ([IO.Path]::GetFullPath(`$env:ProgramData)) 'SFDC24\OrderSupervisor\releases'",
        '& $installer -Payload $archiveBase64 -ArchiveSha256 $expectedArchiveSha256 -ReleaseId $releaseId -ReleaseRoot $releaseRoot'
    )
    $wrapperText = ($lines -join "`r`n") + "`r`n"
    $output = Write-ReleaseWrapperNewUtf8File -Path $DestinationPath -Text $wrapperText
    return [pscustomobject][ordered]@{
        schema = $script:ReleaseWrapperReceiptSchema
        ok = $true
        output_path = [string]$output.path
        wrapper_sha256 = [string]$output.sha256
        wrapper_bytes = [long]$output.bytes.Length
        release_id = $FullReleaseId
        archive_sha256 = $ArchiveSha256
        installer_sha256 = $InstallerSha256
        release_file_count = [int]$script:ReleaseWrapperFiles.Count
    }
}

function Get-ReleaseWrapperSafeErrorCode {
    param([Parameter(Mandatory = $true)]$Record)

    $message = [string]$Record.Exception.Message
    if ($message -cmatch '^[a-z][a-z0-9_]{0,95}$') { return $message }
    return 'order_release_wrapper_build_failed'
}

if ($MyInvocation.InvocationName -cne '.') {
    try {
        $receipt = New-OrderReleaseWrapper `
            -SourceArchivePath $ArchivePath `
            -SourceInstallerPath $InstallerPath `
            -DestinationPath $OutputPath `
            -ArchiveSha256 $ExpectedArchiveSha256 `
            -InstallerSha256 $ExpectedInstallerSha256 `
            -FullReleaseId $ReleaseId
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Compress))
        exit 0
    } catch {
        $receipt = [ordered]@{
            schema = $script:ReleaseWrapperReceiptSchema
            ok = $false
            code = (Get-ReleaseWrapperSafeErrorCode -Record $_)
        }
        [Console]::Error.WriteLine(($receipt | ConvertTo-Json -Compress))
        exit 1
    }
}
