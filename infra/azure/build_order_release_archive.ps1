#Requires -Version 5.1
<#
.SYNOPSIS
Builds the canonical six-file ORDER worker release archive from one Git commit.

.DESCRIPTION
The caller supplies a full lowercase 40-character commit ID. Every release
file is read from that immutable Git object, never from the checkout. The ZIP
uses a fixed entry order, stored (uncompressed) payloads, fixed DOS timestamps,
no comments or extra fields, and the exact blob bytes (including their EOLs).

The destination is create-new only. After writing, the builder reads the file
back, validates the exact case-sensitive inventory and payload bytes, and emits
a compact receipt binding the archive and all six file SHA-256 values to the
commit. No credential or secret input is accepted.
#>
[CmdletBinding()]
param(
    [AllowEmptyString()][string]$RepositoryPath = '',
    [AllowEmptyString()][string]$CommitId = '',
    [AllowEmptyString()][string]$OutputPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:OrderReleaseArchiveReceiptSchema = 'blackboard.order-release-archive-build-receipt.v1'
$script:OrderReleaseArchiveMaximumPayloadBytes = 9000000
$script:OrderReleaseArchiveFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$script:OrderReleaseArchiveDosTime = [uint16]0
$script:OrderReleaseArchiveDosDate = [uint16]33 # 1980-01-01

function Get-OrderReleaseBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $sha.Dispose()
    }
}

function Get-OrderReleaseCrc32 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    [uint32]$crc = [uint32]::MaxValue
    foreach ($byte in $Bytes) {
        $crc = [uint32]($crc -bxor [uint32]$byte)
        for ($bit = 0; $bit -lt 8; $bit++) {
            if (($crc -band [uint32]1) -ne 0) {
                $crc = [uint32](($crc -shr 1) -bxor [uint32]3988292384)
            } else {
                $crc = [uint32]($crc -shr 1)
            }
        }
    }
    return [uint32]($crc -bxor [uint32]::MaxValue)
}

function ConvertTo-OrderReleaseNativeArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    if ($Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }

    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-OrderReleaseGit {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $argumentText = (@($Arguments | ForEach-Object {
        ConvertTo-OrderReleaseNativeArgument -Value ([string]$_)
    }) -join ' ')
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $GitPath
    $startInfo.Arguments = $argumentText
    $startInfo.WorkingDirectory = $Repository
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $startInfo.EnvironmentVariables['GCM_INTERACTIVE'] = 'Never'
    $startInfo.EnvironmentVariables['GIT_OPTIONAL_LOCKS'] = '0'

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $memory = New-Object IO.MemoryStream
    try {
        if (-not $process.Start()) { throw 'git_process_start_failed' }
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardOutput.BaseStream.CopyTo($memory)
        $process.WaitForExit()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject][ordered]@{
            exit_code = [int]$process.ExitCode
            stdout_bytes = [byte[]]$memory.ToArray()
            stderr = [string]$stderr
        }
    } finally {
        $memory.Dispose()
        $process.Dispose()
    }
}

function ConvertFrom-OrderReleaseGitUtf8 {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        return $utf8.GetString($Bytes)
    } catch {
        throw $ErrorCode
    }
}

function Resolve-OrderReleaseRepository {
    param([AllowEmptyString()][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw 'repository_path_invalid'
    }
    try { $fullPath = [IO.Path]::GetFullPath($Path) }
    catch { throw 'repository_path_invalid' }
    try { $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop }
    catch { throw 'repository_missing' }
    if (-not ($item -is [IO.DirectoryInfo]) -or
        ([int]$item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'repository_unsafe'
    }
    return [string]$item.FullName
}

function Resolve-OrderReleaseGitPath {
    try { $command = Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1 }
    catch {
        try { $command = Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1 }
        catch { throw 'git_unavailable' }
    }
    if ($null -eq $command -or [string]::IsNullOrWhiteSpace([string]$command.Source)) {
        throw 'git_unavailable'
    }
    return [string]$command.Source
}

function Get-OrderReleaseCommitEntries {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Repository,
        [AllowEmptyString()][string]$FullCommitId
    )

    if ($FullCommitId -cnotmatch '^[0-9a-f]{40}$') { throw 'commit_id_invalid' }

    $repositoryCheck = Invoke-OrderReleaseGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -Arguments @('rev-parse', '--git-dir')
    if ($repositoryCheck.exit_code -ne 0) { throw 'repository_not_git' }

    $commitCheck = Invoke-OrderReleaseGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -Arguments @('rev-parse', '--verify', '--quiet', ($FullCommitId + '^{commit}'))
    if ($commitCheck.exit_code -ne 0) { throw 'commit_not_found' }
    $resolvedCommit = (ConvertFrom-OrderReleaseGitUtf8 `
        -Bytes $commitCheck.stdout_bytes `
        -ErrorCode 'commit_identity_invalid').Trim()
    if ($resolvedCommit -cnotmatch '^[0-9a-f]{40}$' -or $resolvedCommit -cne $FullCommitId) {
        throw 'commit_identity_invalid'
    }

    $treeResult = Invoke-OrderReleaseGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -Arguments @('ls-tree', '-r', '-z', '--name-only', $FullCommitId, '--')
    if ($treeResult.exit_code -ne 0) { throw 'git_tree_read_failed' }
    $treeText = ConvertFrom-OrderReleaseGitUtf8 `
        -Bytes $treeResult.stdout_bytes `
        -ErrorCode 'git_tree_paths_invalid_utf8'
    if ($treeText.Length -gt 0 -and $treeText[$treeText.Length - 1] -ne [char]0) {
        throw 'git_tree_output_invalid'
    }
    $treePaths = New-Object 'System.Collections.Generic.List[string]'
    if ($treeText.Length -gt 0) {
        $parts = $treeText.Split(@([char]0), [StringSplitOptions]::None)
        foreach ($part in $parts) {
            if ($part.Length -gt 0) { $treePaths.Add([string]$part) }
        }
    }

    $entries = New-Object 'System.Collections.Generic.List[object]'
    [long]$totalPayloadBytes = 0
    foreach ($relativePath in $script:OrderReleaseArchiveFiles) {
        $archiveEntryPath = $relativePath.Replace('\', '/')
        $caseMatches = @($treePaths | Where-Object {
            [string]::Equals([string]$_, $archiveEntryPath, [StringComparison]::OrdinalIgnoreCase)
        })
        $exactMatches = @($caseMatches | Where-Object {
            [string]::Equals([string]$_, $archiveEntryPath, [StringComparison]::Ordinal)
        })
        if ($caseMatches.Count -gt 1) { throw 'release_file_case_collision' }
        if ($exactMatches.Count -eq 0) {
            if ($caseMatches.Count -eq 1) { throw 'release_file_case_mismatch' }
            throw 'release_file_missing'
        }

        $metadataResult = Invoke-OrderReleaseGit `
            -GitPath $GitPath `
            -Repository $Repository `
            -Arguments @('ls-tree', '-z', $FullCommitId, '--', $archiveEntryPath)
        if ($metadataResult.exit_code -ne 0) { throw 'release_file_metadata_read_failed' }
        $metadataText = ConvertFrom-OrderReleaseGitUtf8 `
            -Bytes $metadataResult.stdout_bytes `
            -ErrorCode 'release_file_metadata_invalid_utf8'
        $metadataPattern = '^([0-9]{6}) ([a-z]+) ([0-9a-f]{40,64})\t([^\x00]+)\x00$'
        $metadataMatch = [Regex]::Match($metadataText, $metadataPattern, [Text.RegularExpressions.RegexOptions]::CultureInvariant)
        if (-not $metadataMatch.Success -or
            -not [string]::Equals([string]$metadataMatch.Groups[4].Value, $archiveEntryPath, [StringComparison]::Ordinal)) {
            throw 'release_file_metadata_invalid'
        }
        $mode = [string]$metadataMatch.Groups[1].Value
        $type = [string]$metadataMatch.Groups[2].Value
        if ($type -cne 'blob' -or @('100644', '100755') -cnotcontains $mode) {
            throw 'release_file_type_invalid'
        }

        $blobResult = Invoke-OrderReleaseGit `
            -GitPath $GitPath `
            -Repository $Repository `
            -Arguments @('cat-file', 'blob', ($FullCommitId + ':' + $archiveEntryPath))
        if ($blobResult.exit_code -ne 0) { throw 'release_file_read_failed' }
        [byte[]]$blobBytes = $blobResult.stdout_bytes
        $totalPayloadBytes += $blobBytes.Length
        if ($totalPayloadBytes -gt $script:OrderReleaseArchiveMaximumPayloadBytes) {
            throw 'release_files_too_large'
        }
        $entries.Add([pscustomobject][ordered]@{
            relative_path = $relativePath
            archive_path = $archiveEntryPath
            blob_id = [string]$metadataMatch.Groups[3].Value
            mode = $mode
            bytes = $blobBytes
            sha256 = Get-OrderReleaseBytesSha256 -Bytes $blobBytes
            crc32 = Get-OrderReleaseCrc32 -Bytes $blobBytes
        })
    }
    return @($entries.ToArray())
}

function New-OrderReleaseDeterministicZipBytes {
    param([Parameter(Mandatory = $true)][object[]]$Entries)

    if ($Entries.Count -ne $script:OrderReleaseArchiveFiles.Count) {
        throw 'archive_files_missing'
    }
    $memory = New-Object IO.MemoryStream
    $writer = New-Object IO.BinaryWriter($memory)
    $centralEntries = New-Object 'System.Collections.Generic.List[object]'
    try {
        foreach ($entry in $Entries) {
            [byte[]]$nameBytes = (New-Object Text.UTF8Encoding($false, $true)).GetBytes([string]$entry.archive_path)
            if ($nameBytes.Length -eq 0 -or $nameBytes.Length -gt [uint16]::MaxValue) {
                throw 'archive_entry_name_invalid'
            }
            if ([long]$entry.bytes.Length -gt [uint32]::MaxValue -or $memory.Position -gt [uint32]::MaxValue) {
                throw 'archive_size_unsupported'
            }
            $centralEntries.Add([pscustomobject][ordered]@{
                entry = $entry
                name_bytes = $nameBytes
                local_offset = [uint32]$memory.Position
            })

            $writer.Write([uint32]67324752)  # Local file header signature.
            $writer.Write([uint16]20)        # Version needed: ZIP 2.0.
            $writer.Write([uint16]2048)      # UTF-8 file names.
            $writer.Write([uint16]0)         # Stored: no runtime-dependent compression.
            $writer.Write($script:OrderReleaseArchiveDosTime)
            $writer.Write($script:OrderReleaseArchiveDosDate)
            $writer.Write([uint32]$entry.crc32)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint16]$nameBytes.Length)
            $writer.Write([uint16]0)         # No local extra fields.
            $writer.Write($nameBytes)
            $writer.Write([byte[]]$entry.bytes)
        }

        if ($memory.Position -gt [uint32]::MaxValue) { throw 'archive_size_unsupported' }
        [uint32]$centralOffset = $memory.Position
        foreach ($central in $centralEntries) {
            $entry = $central.entry
            [byte[]]$nameBytes = $central.name_bytes
            $writer.Write([uint32]33639248)  # Central directory header signature.
            $writer.Write([uint16]20)        # Created by ZIP 2.0 on MS-DOS.
            $writer.Write([uint16]20)
            $writer.Write([uint16]2048)
            $writer.Write([uint16]0)
            $writer.Write($script:OrderReleaseArchiveDosTime)
            $writer.Write($script:OrderReleaseArchiveDosDate)
            $writer.Write([uint32]$entry.crc32)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint16]$nameBytes.Length)
            $writer.Write([uint16]0)         # No central extra fields.
            $writer.Write([uint16]0)         # No file comment.
            $writer.Write([uint16]0)         # Disk number.
            $writer.Write([uint16]0)         # Internal attributes.
            $writer.Write([uint32]0)         # External attributes.
            $writer.Write([uint32]$central.local_offset)
            $writer.Write($nameBytes)
        }
        [long]$centralSize = $memory.Position - $centralOffset
        if ($centralSize -gt [uint32]::MaxValue -or $centralEntries.Count -gt [uint16]::MaxValue) {
            throw 'archive_size_unsupported'
        }
        $writer.Write([uint32]101010256) # End of central directory signature.
        $writer.Write([uint16]0)
        $writer.Write([uint16]0)
        $writer.Write([uint16]$centralEntries.Count)
        $writer.Write([uint16]$centralEntries.Count)
        $writer.Write([uint32]$centralSize)
        $writer.Write([uint32]$centralOffset)
        $writer.Write([uint16]0)         # No archive comment.
        $writer.Flush()
        return ,([byte[]]$memory.ToArray())
    } finally {
        $writer.Dispose()
        $memory.Dispose()
    }
}

function Resolve-OrderReleaseOutputPath {
    param([AllowEmptyString()][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw 'output_path_invalid'
    }
    try { $fullPath = [IO.Path]::GetFullPath($Path) }
    catch { throw 'output_path_invalid' }
    if ([IO.Path]::GetExtension($fullPath) -cne '.zip') { throw 'output_extension_invalid' }
    $parentPath = [IO.Path]::GetDirectoryName($fullPath)
    try { $parent = Get-Item -LiteralPath $parentPath -Force -ErrorAction Stop }
    catch { throw 'output_parent_missing' }
    if (-not ($parent -is [IO.DirectoryInfo]) -or
        ([int]$parent.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'output_parent_unsafe'
    }
    if (Test-Path -LiteralPath $fullPath) { throw 'output_already_exists' }
    return $fullPath
}

function Assert-OrderReleaseArchiveReadback {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object[]]$ExpectedEntries
    )

    try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop }
    catch { throw 'archive_reader_unavailable' }
    try { $archive = [IO.Compression.ZipFile]::OpenRead($Path) }
    catch { throw 'archive_readback_invalid' }
    try {
        $actualEntries = @($archive.Entries)
        if ($actualEntries.Count -lt $ExpectedEntries.Count) { throw 'archive_files_missing' }
        if ($actualEntries.Count -gt $ExpectedEntries.Count) { throw 'archive_files_extra' }

        $caseNames = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        for ($index = 0; $index -lt $ExpectedEntries.Count; $index++) {
            $actual = $actualEntries[$index]
            $expected = $ExpectedEntries[$index]
            $actualName = [string]$actual.FullName
            if (-not $caseNames.Add($actualName)) { throw 'archive_entry_case_collision' }
            if (-not [string]::Equals($actualName, [string]$expected.archive_path, [StringComparison]::Ordinal)) {
                $expectedNames = @($ExpectedEntries | ForEach-Object { [string]$_.archive_path })
                if ($expectedNames -icontains $actualName) { throw 'archive_entry_case_mismatch' }
                throw 'archive_entry_order_invalid'
            }
            if ([string]::IsNullOrEmpty([string]$actual.Name)) { throw 'archive_entry_type_invalid' }
        }

        for ($index = 0; $index -lt $ExpectedEntries.Count; $index++) {
            $actual = $actualEntries[$index]
            $expected = $ExpectedEntries[$index]
            $stream = $null
            $memory = New-Object IO.MemoryStream
            try {
                $stream = $actual.Open()
                $stream.CopyTo($memory)
                [byte[]]$actualBytes = $memory.ToArray()
            } finally {
                if ($null -ne $stream) { $stream.Dispose() }
                $memory.Dispose()
            }
            if ([Convert]::ToBase64String($actualBytes) -cne [Convert]::ToBase64String([byte[]]$expected.bytes)) {
                throw 'archive_entry_bytes_mismatch'
            }
            if ((Get-OrderReleaseBytesSha256 -Bytes $actualBytes) -cne [string]$expected.sha256) {
                throw 'archive_entry_digest_mismatch'
            }
        }
    } finally {
        $archive.Dispose()
    }
}

function Write-OrderReleaseArchiveNewFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][object[]]$ExpectedEntries
    )

    $stream = $null
    $created = $false
    try {
        try {
            $stream = New-Object IO.FileStream(
                $Path,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            $created = $true
            $stream.Write($Bytes, 0, $Bytes.Length)
            $stream.Flush($true)
        } catch {
            if (-not $created -and (Test-Path -LiteralPath $Path)) { throw 'output_already_exists' }
            throw 'output_create_failed'
        } finally {
            if ($null -ne $stream) {
                $stream.Dispose()
                $stream = $null
            }
        }

        try { [byte[]]$readback = [IO.File]::ReadAllBytes($Path) }
        catch { throw 'output_readback_failed' }
        if ([Convert]::ToBase64String($readback) -cne [Convert]::ToBase64String($Bytes)) {
            throw 'output_readback_mismatch'
        }
        Assert-OrderReleaseArchiveReadback -Path $Path -ExpectedEntries $ExpectedEntries
        return [pscustomobject][ordered]@{
            path = $Path
            bytes = $readback
            sha256 = Get-OrderReleaseBytesSha256 -Bytes $readback
        }
    } catch {
        $failure = $_
        if ($created -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
            try { Remove-Item -LiteralPath $Path -Force -ErrorAction Stop }
            catch { throw 'output_cleanup_failed' }
        }
        throw $failure
    }
}

function New-OrderReleaseArchive {
    param(
        [AllowEmptyString()][string]$SourceRepositoryPath,
        [AllowEmptyString()][string]$SourceCommitId,
        [AllowEmptyString()][string]$DestinationPath
    )

    $repository = Resolve-OrderReleaseRepository -Path $SourceRepositoryPath
    $destination = Resolve-OrderReleaseOutputPath -Path $DestinationPath
    $gitPath = Resolve-OrderReleaseGitPath
    $entries = @(Get-OrderReleaseCommitEntries `
        -GitPath $gitPath `
        -Repository $repository `
        -FullCommitId $SourceCommitId)
    [byte[]]$archiveBytes = New-OrderReleaseDeterministicZipBytes -Entries $entries
    $output = Write-OrderReleaseArchiveNewFile `
        -Path $destination `
        -Bytes $archiveBytes `
        -ExpectedEntries $entries

    $fileSha256 = [ordered]@{}
    foreach ($entry in $entries) {
        $fileSha256[[string]$entry.relative_path] = [string]$entry.sha256
    }
    return [pscustomobject][ordered]@{
        schema = $script:OrderReleaseArchiveReceiptSchema
        ok = $true
        output_path = [string]$output.path
        release_id = $SourceCommitId
        commit_id = $SourceCommitId
        archive_sha256 = [string]$output.sha256
        archive_bytes = [long]$output.bytes.Length
        release_file_count = [int]$entries.Count
        file_sha256 = $fileSha256
    }
}

function Get-OrderReleaseArchiveSafeErrorCode {
    param([Parameter(Mandatory = $true)]$Record)

    $message = [string]$Record.Exception.Message
    if ($message -cmatch '^[a-z][a-z0-9_]{0,95}$') { return $message }
    return 'order_release_archive_build_failed'
}

if ($MyInvocation.InvocationName -cne '.') {
    try {
        $receipt = New-OrderReleaseArchive `
            -SourceRepositoryPath $RepositoryPath `
            -SourceCommitId $CommitId `
            -DestinationPath $OutputPath
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Compress -Depth 5))
        exit 0
    } catch {
        $receipt = [ordered]@{
            schema = $script:OrderReleaseArchiveReceiptSchema
            ok = $false
            code = Get-OrderReleaseArchiveSafeErrorCode -Record $_
        }
        [Console]::Error.WriteLine(($receipt | ConvertTo-Json -Compress))
        exit 1
    }
}
