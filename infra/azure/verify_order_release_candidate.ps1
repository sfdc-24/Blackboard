#Requires -Version 5.1
<#
.SYNOPSIS
Authenticates an ORDER release candidate before any transport or guest change.

.DESCRIPTION
This offline-only preflight treats the packager receipt and archive as
untrusted inputs. It recomputes the canonical six release blobs from one full
commit in an explicitly anchored repository, with inherited Git context and
replacement refs disabled, reconstructs the deterministic archive bytes, and
requires a byte-for-byte match. It also binds the exact release installer to
the same commit and to a caller-pinned SHA-256 value.

All input paths and their existing ancestors must be ordinary, non-reparse
filesystem entries. The command performs no fetch, checkout, extraction,
Azure operation, VM operation, task operation, or output-file write. Success
emits one deterministic bounded JSON receipt suitable as the gate immediately
before build_order_release_wrapper.ps1.
#>
[CmdletBinding()]
param(
    [AllowEmptyString()][string]$RepositoryPath = '',
    [AllowEmptyString()][string]$CommitId = '',
    [AllowEmptyString()][string]$ArchivePath = '',
    [AllowEmptyString()][string]$PackagerReceiptPath = '',
    [AllowEmptyString()][string]$InstallerPath = '',
    [AllowEmptyString()][string]$ExpectedInstallerSha256 = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:OrderReleasePreflightReceiptSchema = 'blackboard.order-release-candidate-preflight-receipt.v1'
$script:OrderReleasePackagerReceiptSchema = 'blackboard.order-release-archive-build-receipt.v1'
$script:OrderReleasePreflightMaximumArchiveBytes = 10000000
$script:OrderReleasePreflightMaximumReceiptBytes = 16384
$script:OrderReleasePreflightMaximumInstallerBytes = 1500000
$script:OrderReleasePreflightMaximumPayloadBytes = 9000000
$script:OrderReleasePreflightMaximumTreeOutputBytes = 5000000
$script:OrderReleasePreflightInstallerPath = 'infra\azure\install_release_from_archive.ps1'
$script:OrderReleasePreflightFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$script:OrderReleasePreflightDosTime = [uint16]0
$script:OrderReleasePreflightDosDate = [uint16]33

function Get-OrderReleasePreflightBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $sha.Dispose()
    }
}

function Test-OrderReleasePreflightBytesEqual {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Left,
        [Parameter(Mandatory = $true)][byte[]]$Right
    )

    if ($Left.Length -ne $Right.Length) { return $false }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) { return $false }
    }
    return $true
}

function Resolve-OrderReleasePreflightSafeItem {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('File', 'Directory')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$InvalidCode,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw $InvalidCode
    }
    try { $fullPath = [IO.Path]::GetFullPath($Path) }
    catch { throw $InvalidCode }

    $volumeRoot = [IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($volumeRoot)) { throw $InvalidCode }
    try { $current = Get-Item -LiteralPath $volumeRoot -Force -ErrorAction Stop }
    catch { throw $MissingCode }
    if (([int]$current.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0 -or
        -not ($current -is [IO.DirectoryInfo])) {
        throw $UnsafeCode
    }

    $relative = $fullPath.Substring($volumeRoot.Length)
    $segments = @($relative.Split(
        [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
        [StringSplitOptions]::RemoveEmptyEntries
    ))
    if ($segments.Count -eq 0) {
        if ($Kind -cne 'Directory') { throw $UnsafeCode }
        return [string]$current.FullName
    }

    for ($index = 0; $index -lt $segments.Count; $index++) {
        $segment = [string]$segments[$index]
        try {
            $matches = @($current.EnumerateFileSystemInfos(
                $segment,
                [IO.SearchOption]::TopDirectoryOnly
            ) | Where-Object { $_.Name -ieq $segment })
        } catch { throw $UnsafeCode }
        if ($matches.Count -eq 0) { throw $MissingCode }
        if ($matches.Count -ne 1) { throw $UnsafeCode }
        $current = $matches[0]
        if (([int]$current.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw $UnsafeCode
        }
        if ($index -lt ($segments.Count - 1) -and -not ($current -is [IO.DirectoryInfo])) {
            throw $UnsafeCode
        }
    }

    if ($Kind -ceq 'File' -and -not ($current -is [IO.FileInfo])) { throw $UnsafeCode }
    if ($Kind -ceq 'Directory' -and -not ($current -is [IO.DirectoryInfo])) { throw $UnsafeCode }
    return [string]$current.FullName
}

function Read-OrderReleasePreflightSafeFile {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][long]$MaximumBytes,
        [Parameter(Mandatory = $true)][string]$InvalidCode,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode,
        [Parameter(Mandatory = $true)][string]$SizeCode
    )

    $resolved = Resolve-OrderReleasePreflightSafeItem `
        -Path $Path `
        -Kind File `
        -InvalidCode $InvalidCode `
        -MissingCode $MissingCode `
        -UnsafeCode $UnsafeCode
    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($resolved) }
    catch { throw ($UnsafeCode + '_read_failed') }
    if ($bytes.Length -eq 0 -or $bytes.Length -gt $MaximumBytes) { throw $SizeCode }
    return [pscustomobject][ordered]@{
        path = $resolved
        bytes = $bytes
        sha256 = Get-OrderReleasePreflightBytesSha256 -Bytes $bytes
    }
}

function Assert-OrderReleasePreflightMetadataTreeSafe {
    param([Parameter(Mandatory = $true)][IO.DirectoryInfo]$GitDirectory)

    $directories = New-Object System.Collections.Queue
    $directories.Enqueue($GitDirectory)
    while ($directories.Count -gt 0) {
        $directory = $directories.Dequeue()
        try { $entries = @($directory.EnumerateFileSystemInfos()) }
        catch { throw 'repository_git_inventory_failed' }
        foreach ($entry in $entries) {
            if (([int]$entry.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'repository_git_reparse_point'
            }
            if ($entry -is [IO.DirectoryInfo]) { $directories.Enqueue($entry) }
        }
    }
}

function Assert-OrderReleasePreflightNoAlternateObjectState {
    param([Parameter(Mandatory = $true)][IO.DirectoryInfo]$GitDirectory)

    $objectsPath = Resolve-OrderReleasePreflightSafeItem `
        -Path (Join-Path $GitDirectory.FullName 'objects') `
        -Kind Directory `
        -InvalidCode 'repository_objects_invalid' `
        -MissingCode 'repository_objects_missing' `
        -UnsafeCode 'repository_objects_unsafe'
    $objects = Get-Item -LiteralPath $objectsPath -Force
    $infoMatches = @($objects.EnumerateFileSystemInfos('info', [IO.SearchOption]::TopDirectoryOnly) |
        Where-Object { [string]::Equals([string]$_.Name, 'info', [StringComparison]::OrdinalIgnoreCase) })
    if ($infoMatches.Count -gt 1) { throw 'repository_objects_path_collision' }
    if ($infoMatches.Count -eq 1) {
        $info = $infoMatches[0]
        if (([int]$info.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not ($info -is [IO.DirectoryInfo])) {
            throw 'repository_objects_unsafe'
        }
        foreach ($alternateName in @('alternates', 'http-alternates')) {
            $alternateMatches = @($info.EnumerateFileSystemInfos(
                $alternateName,
                [IO.SearchOption]::TopDirectoryOnly
            ) | Where-Object {
                [string]::Equals([string]$_.Name, $alternateName, [StringComparison]::OrdinalIgnoreCase)
            })
            if ($alternateMatches.Count -gt 0) { throw 'repository_alternates_forbidden' }
        }
    }

    $gitInfoMatches = @($GitDirectory.EnumerateFileSystemInfos('info', [IO.SearchOption]::TopDirectoryOnly) |
        Where-Object { [string]::Equals([string]$_.Name, 'info', [StringComparison]::OrdinalIgnoreCase) })
    if ($gitInfoMatches.Count -gt 1) { throw 'repository_git_path_collision' }
    if ($gitInfoMatches.Count -eq 1) {
        $gitInfo = $gitInfoMatches[0]
        if (([int]$gitInfo.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not ($gitInfo -is [IO.DirectoryInfo])) {
            throw 'repository_git_unsafe'
        }
        $graftMatches = @($gitInfo.EnumerateFileSystemInfos('grafts', [IO.SearchOption]::TopDirectoryOnly) |
            Where-Object { [string]::Equals([string]$_.Name, 'grafts', [StringComparison]::OrdinalIgnoreCase) })
        if ($graftMatches.Count -gt 0) { throw 'repository_grafts_forbidden' }
    }
}

function Resolve-OrderReleasePreflightRepository {
    param([AllowEmptyString()][string]$Path)

    $root = Resolve-OrderReleasePreflightSafeItem `
        -Path $Path `
        -Kind Directory `
        -InvalidCode 'repository_path_invalid' `
        -MissingCode 'repository_missing' `
        -UnsafeCode 'repository_unsafe'
    $gitDirectoryPath = Resolve-OrderReleasePreflightSafeItem `
        -Path (Join-Path $root '.git') `
        -Kind Directory `
        -InvalidCode 'repository_git_directory_invalid' `
        -MissingCode 'repository_git_directory_missing' `
        -UnsafeCode 'repository_git_unsafe'
    $gitDirectory = Get-Item -LiteralPath $gitDirectoryPath -Force
    Assert-OrderReleasePreflightMetadataTreeSafe -GitDirectory $gitDirectory
    Assert-OrderReleasePreflightNoAlternateObjectState -GitDirectory $gitDirectory
    return [pscustomobject][ordered]@{
        root = $root
        git_directory = [string]$gitDirectory.FullName
    }
}

function ConvertTo-OrderReleasePreflightNativeArgument {
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

function Resolve-OrderReleasePreflightGitPath {
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

function Invoke-OrderReleasePreflightGit {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$GitDirectory,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $allArguments = @(
        '--no-replace-objects',
        '--literal-pathspecs',
        ('--git-dir=' + $GitDirectory),
        ('--work-tree=' + $Repository)
    ) + $Arguments
    $argumentText = (@($allArguments | ForEach-Object {
        ConvertTo-OrderReleasePreflightNativeArgument -Value ([string]$_)
    }) -join ' ')
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $GitPath
    $startInfo.Arguments = $argumentText
    $startInfo.WorkingDirectory = $Repository
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    foreach ($nameObject in @($startInfo.EnvironmentVariables.Keys)) {
        $name = [string]$nameObject
        if ($name.StartsWith('GIT_', [StringComparison]::OrdinalIgnoreCase) -or
            $name.StartsWith('GCM_', [StringComparison]::OrdinalIgnoreCase)) {
            $startInfo.EnvironmentVariables.Remove($name)
        }
    }
    $startInfo.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $startInfo.EnvironmentVariables['GCM_INTERACTIVE'] = 'Never'
    $startInfo.EnvironmentVariables['GIT_OPTIONAL_LOCKS'] = '0'
    $startInfo.EnvironmentVariables['GIT_NO_REPLACE_OBJECTS'] = '1'
    $startInfo.EnvironmentVariables['GIT_CONFIG_SYSTEM'] = 'NUL'
    $startInfo.EnvironmentVariables['GIT_CONFIG_NOSYSTEM'] = '1'
    $startInfo.EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'
    $startInfo.EnvironmentVariables['GIT_ATTR_NOSYSTEM'] = '1'

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

function ConvertFrom-OrderReleasePreflightGitUtf8 {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        return $utf8.GetString($Bytes)
    } catch { throw $ErrorCode }
}

function Get-OrderReleasePreflightRepositoryContext {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$GitDirectory,
        [AllowEmptyString()][string]$FullCommitId
    )

    if ($FullCommitId -cnotmatch '^[0-9a-f]{40}$') { throw 'commit_id_invalid' }

    $topResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('rev-parse', '--absolute-git-dir')
    if ($topResult.exit_code -ne 0) { throw 'repository_not_git' }
    $reportedGitDirectory = (ConvertFrom-OrderReleasePreflightGitUtf8 `
        -Bytes $topResult.stdout_bytes `
        -ErrorCode 'repository_identity_invalid').Trim()
    try { $reportedGitDirectory = [IO.Path]::GetFullPath($reportedGitDirectory) }
    catch { throw 'repository_identity_invalid' }
    if (-not [string]::Equals($reportedGitDirectory, $GitDirectory, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'repository_identity_mismatch'
    }

    $topResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('rev-parse', '--path-format=absolute', '--show-toplevel')
    if ($topResult.exit_code -ne 0) { throw 'repository_not_git' }
    $topText = (ConvertFrom-OrderReleasePreflightGitUtf8 `
        -Bytes $topResult.stdout_bytes `
        -ErrorCode 'repository_identity_invalid').Trim()
    try { $topPath = [IO.Path]::GetFullPath($topText) }
    catch { throw 'repository_identity_invalid' }
    if (-not [string]::Equals($topPath.TrimEnd('\', '/'), $Repository.TrimEnd('\', '/'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'repository_identity_mismatch'
    }

    $includeResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('config', '--local', '--no-includes', '--name-only', '--get-regexp', '^include')
    if ($includeResult.exit_code -eq 0 -and $includeResult.stdout_bytes.Length -gt 0) {
        throw 'repository_config_includes_forbidden'
    }
    if (@(0, 1) -cnotcontains $includeResult.exit_code) { throw 'repository_config_check_failed' }

    $replaceResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('for-each-ref', '--format=%(refname)', 'refs/replace/')
    if ($replaceResult.exit_code -ne 0) { throw 'repository_replace_ref_check_failed' }
    if ($replaceResult.stdout_bytes.Length -gt 0) { throw 'repository_replace_refs_forbidden' }

    $commitResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('rev-parse', '--verify', '--quiet', ($FullCommitId + '^{commit}'))
    if ($commitResult.exit_code -ne 0) { throw 'commit_not_found' }
    $resolvedCommit = (ConvertFrom-OrderReleasePreflightGitUtf8 `
        -Bytes $commitResult.stdout_bytes `
        -ErrorCode 'commit_identity_invalid').Trim()
    if ($resolvedCommit -cne $FullCommitId) { throw 'commit_identity_invalid' }

    $treeIdentity = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('rev-parse', '--verify', '--quiet', ($FullCommitId + '^{tree}'))
    if ($treeIdentity.exit_code -ne 0) { throw 'tree_identity_unavailable' }
    $treeId = (ConvertFrom-OrderReleasePreflightGitUtf8 `
        -Bytes $treeIdentity.stdout_bytes `
        -ErrorCode 'tree_identity_invalid').Trim()
    if ($treeId -cnotmatch '^[0-9a-f]{40}$') { throw 'tree_identity_invalid' }

    $treeResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('ls-tree', '-r', '-z', '--name-only', $FullCommitId, '--')
    if ($treeResult.exit_code -ne 0) { throw 'git_tree_read_failed' }
    if ($treeResult.stdout_bytes.Length -gt $script:OrderReleasePreflightMaximumTreeOutputBytes) {
        throw 'git_tree_output_too_large'
    }
    $treeText = ConvertFrom-OrderReleasePreflightGitUtf8 `
        -Bytes $treeResult.stdout_bytes `
        -ErrorCode 'git_tree_paths_invalid_utf8'
    if ($treeText.Length -gt 0 -and $treeText[$treeText.Length - 1] -ne [char]0) {
        throw 'git_tree_output_invalid'
    }
    $treePaths = New-Object 'System.Collections.Generic.List[string]'
    if ($treeText.Length -gt 0) {
        foreach ($part in $treeText.Split(@([char]0), [StringSplitOptions]::None)) {
            if ($part.Length -gt 0) { $treePaths.Add([string]$part) }
        }
    }
    return [pscustomobject][ordered]@{
        commit_id = $resolvedCommit
        tree_id = $treeId
        tree_paths = @($treePaths.ToArray())
    }
}

function Get-OrderReleasePreflightGitBlob {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$GitDirectory,
        [Parameter(Mandatory = $true)][string]$FullCommitId,
        [Parameter(Mandatory = $true)][string[]]$TreePaths,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$CaseCode,
        [Parameter(Mandatory = $true)][string]$TypeCode,
        [Parameter(Mandatory = $true)][string]$ReadCode,
        [Parameter(Mandatory = $true)][long]$MaximumBytes,
        [Parameter(Mandatory = $true)][string]$SizeCode
    )

    $gitPathName = $RelativePath.Replace('\', '/')
    $caseMatches = @($TreePaths | Where-Object {
        [string]::Equals([string]$_, $gitPathName, [StringComparison]::OrdinalIgnoreCase)
    })
    $exactMatches = @($caseMatches | Where-Object {
        [string]::Equals([string]$_, $gitPathName, [StringComparison]::Ordinal)
    })
    if ($caseMatches.Count -gt 1) { throw $CaseCode }
    if ($exactMatches.Count -eq 0) {
        if ($caseMatches.Count -eq 1) { throw $CaseCode }
        throw $MissingCode
    }

    $metadataResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('ls-tree', '-z', $FullCommitId, '--', $gitPathName)
    if ($metadataResult.exit_code -ne 0) { throw $ReadCode }
    $metadataText = ConvertFrom-OrderReleasePreflightGitUtf8 `
        -Bytes $metadataResult.stdout_bytes `
        -ErrorCode $ReadCode
    $metadataPattern = '^([0-9]{6}) ([a-z]+) ([0-9a-f]{40})\t([^\x00]+)\x00$'
    $match = [Regex]::Match($metadataText, $metadataPattern, [Text.RegularExpressions.RegexOptions]::CultureInvariant)
    if (-not $match.Success -or
        -not [string]::Equals([string]$match.Groups[4].Value, $gitPathName, [StringComparison]::Ordinal)) {
        throw $ReadCode
    }
    if ([string]$match.Groups[2].Value -cne 'blob' -or
        @('100644', '100755') -cnotcontains [string]$match.Groups[1].Value) {
        throw $TypeCode
    }

    $sizeResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('cat-file', '-s', ($FullCommitId + ':' + $gitPathName))
    if ($sizeResult.exit_code -ne 0) { throw $ReadCode }
    $sizeText = (ConvertFrom-OrderReleasePreflightGitUtf8 `
        -Bytes $sizeResult.stdout_bytes `
        -ErrorCode $ReadCode).Trim()
    [long]$expectedSize = 0
    if ($sizeText -cnotmatch '^[0-9]+$' -or
        -not [long]::TryParse(
            $sizeText,
            [Globalization.NumberStyles]::None,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$expectedSize
        ) -or
        $expectedSize -lt 0 -or $expectedSize -gt $MaximumBytes) {
        throw $SizeCode
    }

    $blobResult = Invoke-OrderReleasePreflightGit `
        -GitPath $GitPath `
        -Repository $Repository `
        -GitDirectory $GitDirectory `
        -Arguments @('cat-file', 'blob', ($FullCommitId + ':' + $gitPathName))
    if ($blobResult.exit_code -ne 0) { throw $ReadCode }
    [byte[]]$bytes = $blobResult.stdout_bytes
    if ([long]$bytes.Length -ne $expectedSize) { throw $ReadCode }
    return [pscustomobject][ordered]@{
        relative_path = $RelativePath
        archive_path = $gitPathName
        bytes = $bytes
        sha256 = Get-OrderReleasePreflightBytesSha256 -Bytes $bytes
        mode = [string]$match.Groups[1].Value
    }
}

function Assert-OrderReleasePreflightNoDuplicateJsonKeys {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    try { Add-Type -AssemblyName System.Runtime.Serialization -ErrorAction Stop }
    catch { throw 'packager_receipt_json_invalid' }
    $reader = $null
    try {
        $reader = [Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader(
            $Bytes,
            [Xml.XmlDictionaryReaderQuotas]::Max
        )
        $document = New-Object Xml.XmlDocument
        $document.Load($reader)
    } catch { throw 'packager_receipt_json_invalid' }
    finally { if ($null -ne $reader) { $reader.Close() } }

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
                $key = $(if ($child.NamespaceURI -ceq 'item' -and $child.LocalName -ceq 'item') {
                    $child.GetAttribute('item')
                } else {
                    $child.LocalName
                })
                if (-not $keys.Add([string]$key)) { throw 'packager_receipt_duplicate_key' }
            }
        }
        foreach ($child in @($node.ChildNodes | Where-Object {
            $_.NodeType -eq [Xml.XmlNodeType]::Element
        })) {
            $nodes.Enqueue($child)
        }
    }
}

function Assert-OrderReleasePreflightExactProperties {
    param(
        [Parameter(Mandatory = $true)]$InputObject,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )

    $actual = @($InputObject.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($actual.Count -ne $Expected.Count) { throw $ErrorCode }
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        if ($actual[$index] -cne $Expected[$index]) { throw $ErrorCode }
    }
}

function Read-OrderReleasePreflightPackagerReceipt {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$ExpectedArchivePath
    )

    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xef -and $Bytes[1] -eq 0xbb -and $Bytes[2] -eq 0xbf) {
        throw 'packager_receipt_utf8_bom_forbidden'
    }
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $text = $utf8.GetString($Bytes)
    } catch { throw 'packager_receipt_utf8_invalid' }
    $jsonText = $text
    if ($jsonText.EndsWith("`r`n", [StringComparison]::Ordinal)) {
        $jsonText = $jsonText.Substring(0, $jsonText.Length - 2)
    } elseif ($jsonText.EndsWith("`n", [StringComparison]::Ordinal)) {
        $jsonText = $jsonText.Substring(0, $jsonText.Length - 1)
    }
    if ([string]::IsNullOrWhiteSpace($jsonText) -or $jsonText.Trim() -cne $jsonText -or
        $jsonText.IndexOf("`r", [StringComparison]::Ordinal) -ge 0 -or
        $jsonText.IndexOf("`n", [StringComparison]::Ordinal) -ge 0) {
        throw 'packager_receipt_json_invalid'
    }
    Assert-OrderReleasePreflightNoDuplicateJsonKeys -Bytes $Bytes
    try { $receipt = $jsonText | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'packager_receipt_json_invalid' }
    if ($null -eq $receipt -or -not ($receipt -is [pscustomobject])) {
        throw 'packager_receipt_json_invalid'
    }

    $expectedProperties = @(
        'schema',
        'ok',
        'output_path',
        'release_id',
        'commit_id',
        'archive_sha256',
        'archive_bytes',
        'release_file_count',
        'file_sha256'
    )
    Assert-OrderReleasePreflightExactProperties `
        -InputObject $receipt `
        -Expected $expectedProperties `
        -ErrorCode 'packager_receipt_shape_invalid'
    if (-not ($receipt.schema -is [string]) -or
        [string]$receipt.schema -cne $script:OrderReleasePackagerReceiptSchema -or
        -not ($receipt.ok -is [bool]) -or $receipt.ok -ne $true -or
        -not ($receipt.output_path -is [string]) -or
        -not ($receipt.release_id -is [string]) -or
        -not ($receipt.commit_id -is [string]) -or
        -not ($receipt.archive_sha256 -is [string]) -or
        -not (($receipt.archive_bytes -is [int]) -or ($receipt.archive_bytes -is [long])) -or
        -not (($receipt.release_file_count -is [int]) -or ($receipt.release_file_count -is [long])) -or
        $null -eq $receipt.file_sha256 -or -not ($receipt.file_sha256 -is [pscustomobject])) {
        throw 'packager_receipt_shape_invalid'
    }
    if ([string]$receipt.archive_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [long]$receipt.archive_bytes -le 0 -or
        [long]$receipt.archive_bytes -gt $script:OrderReleasePreflightMaximumArchiveBytes -or
        [long]$receipt.release_file_count -ne $script:OrderReleasePreflightFiles.Count) {
        throw 'packager_receipt_shape_invalid'
    }
    try { $receiptArchivePath = [IO.Path]::GetFullPath([string]$receipt.output_path) }
    catch { throw 'packager_receipt_output_path_invalid' }
    if (-not [IO.Path]::IsPathRooted([string]$receipt.output_path) -or
        -not [string]::Equals($receiptArchivePath, $ExpectedArchivePath, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'packager_receipt_output_path_mismatch'
    }
    Assert-OrderReleasePreflightExactProperties `
        -InputObject $receipt.file_sha256 `
        -Expected $script:OrderReleasePreflightFiles `
        -ErrorCode 'packager_receipt_file_hashes_invalid'
    foreach ($relativePath in $script:OrderReleasePreflightFiles) {
        $value = $receipt.file_sha256.PSObject.Properties[$relativePath].Value
        if (-not ($value -is [string]) -or [string]$value -cnotmatch '^[0-9a-f]{64}$') {
            throw 'packager_receipt_file_hashes_invalid'
        }
    }
    return $receipt
}

function Get-OrderReleasePreflightCrc32 {
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

function New-OrderReleasePreflightCanonicalArchiveBytes {
    param([Parameter(Mandatory = $true)][object[]]$Entries)

    if ($Entries.Count -ne $script:OrderReleasePreflightFiles.Count) {
        throw 'release_files_missing'
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
            [uint32]$crc32 = Get-OrderReleasePreflightCrc32 -Bytes ([byte[]]$entry.bytes)
            $centralEntries.Add([pscustomobject][ordered]@{
                entry = $entry
                name_bytes = $nameBytes
                crc32 = $crc32
                local_offset = [uint32]$memory.Position
            })

            $writer.Write([uint32]67324752)
            $writer.Write([uint16]20)
            $writer.Write([uint16]2048)
            $writer.Write([uint16]0)
            $writer.Write($script:OrderReleasePreflightDosTime)
            $writer.Write($script:OrderReleasePreflightDosDate)
            $writer.Write([uint32]$crc32)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint16]$nameBytes.Length)
            $writer.Write([uint16]0)
            $writer.Write($nameBytes)
            $writer.Write([byte[]]$entry.bytes)
        }

        if ($memory.Position -gt [uint32]::MaxValue) { throw 'archive_size_unsupported' }
        [uint32]$centralOffset = $memory.Position
        foreach ($central in $centralEntries) {
            $entry = $central.entry
            [byte[]]$nameBytes = $central.name_bytes
            $writer.Write([uint32]33639248)
            $writer.Write([uint16]20)
            $writer.Write([uint16]20)
            $writer.Write([uint16]2048)
            $writer.Write([uint16]0)
            $writer.Write($script:OrderReleasePreflightDosTime)
            $writer.Write($script:OrderReleasePreflightDosDate)
            $writer.Write([uint32]$central.crc32)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint32]$entry.bytes.Length)
            $writer.Write([uint16]$nameBytes.Length)
            $writer.Write([uint16]0)
            $writer.Write([uint16]0)
            $writer.Write([uint16]0)
            $writer.Write([uint16]0)
            $writer.Write([uint32]0)
            $writer.Write([uint32]$central.local_offset)
            $writer.Write($nameBytes)
        }
        [long]$centralSize = $memory.Position - $centralOffset
        if ($centralSize -gt [uint32]::MaxValue) { throw 'archive_size_unsupported' }
        $writer.Write([uint32]101010256)
        $writer.Write([uint16]0)
        $writer.Write([uint16]0)
        $writer.Write([uint16]$centralEntries.Count)
        $writer.Write([uint16]$centralEntries.Count)
        $writer.Write([uint32]$centralSize)
        $writer.Write([uint32]$centralOffset)
        $writer.Write([uint16]0)
        $writer.Flush()
        return ,([byte[]]$memory.ToArray())
    } finally {
        $writer.Dispose()
        $memory.Dispose()
    }
}

function Assert-OrderReleasePreflightInstaller {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xef -and $Bytes[1] -eq 0xbb -and $Bytes[2] -eq 0xbf) {
        throw 'installer_utf8_bom_forbidden'
    }
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $text = $utf8.GetString($Bytes)
    } catch { throw 'installer_utf8_invalid' }
    try { [void][ScriptBlock]::Create($text) }
    catch { throw 'installer_powershell_invalid' }
}

function Invoke-OrderReleaseCandidatePreflight {
    param(
        [AllowEmptyString()][string]$SourceRepositoryPath,
        [AllowEmptyString()][string]$SourceCommitId,
        [AllowEmptyString()][string]$SourceArchivePath,
        [AllowEmptyString()][string]$SourcePackagerReceiptPath,
        [AllowEmptyString()][string]$SourceInstallerPath,
        [AllowEmptyString()][string]$InstallerSha256
    )

    if ($SourceCommitId -cnotmatch '^[0-9a-f]{40}$') { throw 'commit_id_invalid' }
    if ($InstallerSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'installer_sha256_invalid' }
    $repository = Resolve-OrderReleasePreflightRepository -Path $SourceRepositoryPath
    $archive = Read-OrderReleasePreflightSafeFile `
        -Path $SourceArchivePath `
        -MaximumBytes $script:OrderReleasePreflightMaximumArchiveBytes `
        -InvalidCode 'archive_path_invalid' `
        -MissingCode 'archive_missing' `
        -UnsafeCode 'archive_unsafe' `
        -SizeCode 'archive_size_invalid'
    $receiptFile = Read-OrderReleasePreflightSafeFile `
        -Path $SourcePackagerReceiptPath `
        -MaximumBytes $script:OrderReleasePreflightMaximumReceiptBytes `
        -InvalidCode 'packager_receipt_path_invalid' `
        -MissingCode 'packager_receipt_missing' `
        -UnsafeCode 'packager_receipt_unsafe' `
        -SizeCode 'packager_receipt_size_invalid'
    $installer = Read-OrderReleasePreflightSafeFile `
        -Path $SourceInstallerPath `
        -MaximumBytes $script:OrderReleasePreflightMaximumInstallerBytes `
        -InvalidCode 'installer_path_invalid' `
        -MissingCode 'installer_missing' `
        -UnsafeCode 'installer_unsafe' `
        -SizeCode 'installer_size_invalid'

    $packagerReceipt = Read-OrderReleasePreflightPackagerReceipt `
        -Bytes $receiptFile.bytes `
        -ExpectedArchivePath $archive.path
    if ([string]$packagerReceipt.release_id -cne $SourceCommitId -or
        [string]$packagerReceipt.commit_id -cne $SourceCommitId) {
        throw 'packager_receipt_commit_mismatch'
    }
    if ([long]$packagerReceipt.archive_bytes -ne [long]$archive.bytes.Length) {
        throw 'packager_receipt_archive_size_mismatch'
    }
    if ([string]$packagerReceipt.archive_sha256 -cne [string]$archive.sha256) {
        throw 'archive_digest_mismatch'
    }
    if ([string]$installer.sha256 -cne $InstallerSha256) { throw 'installer_digest_mismatch' }

    $gitPath = Resolve-OrderReleasePreflightGitPath
    $context = Get-OrderReleasePreflightRepositoryContext `
        -GitPath $gitPath `
        -Repository $repository.root `
        -GitDirectory $repository.git_directory `
        -FullCommitId $SourceCommitId

    $entries = New-Object 'System.Collections.Generic.List[object]'
    [long]$payloadBytes = 0
    foreach ($relativePath in $script:OrderReleasePreflightFiles) {
        [long]$remainingPayloadBytes = $script:OrderReleasePreflightMaximumPayloadBytes - $payloadBytes
        if ($remainingPayloadBytes -lt 0) { throw 'release_files_too_large' }
        $entry = Get-OrderReleasePreflightGitBlob `
            -GitPath $gitPath `
            -Repository $repository.root `
            -GitDirectory $repository.git_directory `
            -FullCommitId $SourceCommitId `
            -TreePaths $context.tree_paths `
            -RelativePath $relativePath `
            -MissingCode 'release_file_missing' `
            -CaseCode 'release_file_case_mismatch' `
            -TypeCode 'release_file_type_invalid' `
            -ReadCode 'release_file_read_failed' `
            -MaximumBytes $remainingPayloadBytes `
            -SizeCode 'release_files_too_large'
        $payloadBytes += [long]$entry.bytes.Length
        if ($payloadBytes -gt $script:OrderReleasePreflightMaximumPayloadBytes) {
            throw 'release_files_too_large'
        }
        $receiptHash = [string]$packagerReceipt.file_sha256.PSObject.Properties[$relativePath].Value
        if ($receiptHash -cne [string]$entry.sha256) { throw 'packager_receipt_file_digest_mismatch' }
        $entries.Add($entry)
    }

    $installerBlob = Get-OrderReleasePreflightGitBlob `
        -GitPath $gitPath `
        -Repository $repository.root `
        -GitDirectory $repository.git_directory `
        -FullCommitId $SourceCommitId `
        -TreePaths $context.tree_paths `
        -RelativePath $script:OrderReleasePreflightInstallerPath `
        -MissingCode 'installer_commit_file_missing' `
        -CaseCode 'installer_commit_file_case_mismatch' `
        -TypeCode 'installer_commit_file_type_invalid' `
        -ReadCode 'installer_commit_file_read_failed' `
        -MaximumBytes $script:OrderReleasePreflightMaximumInstallerBytes `
        -SizeCode 'installer_commit_file_size_invalid'
    if (-not (Test-OrderReleasePreflightBytesEqual `
        -Left ([byte[]]$installer.bytes) `
        -Right ([byte[]]$installerBlob.bytes))) {
        throw 'installer_not_from_commit'
    }
    Assert-OrderReleasePreflightInstaller -Bytes ([byte[]]$installer.bytes)

    [byte[]]$expectedArchive = New-OrderReleasePreflightCanonicalArchiveBytes -Entries @($entries.ToArray())
    if (-not (Test-OrderReleasePreflightBytesEqual `
        -Left ([byte[]]$archive.bytes) `
        -Right $expectedArchive)) {
        throw 'archive_not_canonical_for_commit'
    }

    $fileSha256 = [ordered]@{}
    foreach ($entry in $entries) {
        $fileSha256[[string]$entry.relative_path] = [string]$entry.sha256
    }
    return [pscustomobject][ordered]@{
        schema = $script:OrderReleasePreflightReceiptSchema
        ok = $true
        status = 'VERIFIED_OFFLINE'
        release_id = $SourceCommitId
        commit_id = $SourceCommitId
        tree_id = [string]$context.tree_id
        archive_sha256 = [string]$archive.sha256
        archive_bytes = [long]$archive.bytes.Length
        installer_sha256 = [string]$installer.sha256
        release_file_count = [int]$entries.Count
        file_sha256 = $fileSha256
        git_context_status = 'EXPLICIT_REPOSITORY_REPLACEMENTS_DISABLED'
        transport_status = 'NOT_STARTED'
    }
}

function Get-OrderReleasePreflightSafeErrorCode {
    param([Parameter(Mandatory = $true)]$Record)

    $message = [string]$Record.Exception.Message
    if ($message -cmatch '^[a-z][a-z0-9_]{0,95}$') { return $message }
    return 'order_release_candidate_preflight_failed'
}

if ($MyInvocation.InvocationName -cne '.') {
    try {
        $receipt = Invoke-OrderReleaseCandidatePreflight `
            -SourceRepositoryPath $RepositoryPath `
            -SourceCommitId $CommitId `
            -SourceArchivePath $ArchivePath `
            -SourcePackagerReceiptPath $PackagerReceiptPath `
            -SourceInstallerPath $InstallerPath `
            -InstallerSha256 $ExpectedInstallerSha256
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Compress -Depth 5))
        exit 0
    } catch {
        $receipt = [ordered]@{
            schema = $script:OrderReleasePreflightReceiptSchema
            ok = $false
            code = Get-OrderReleasePreflightSafeErrorCode -Record $_
        }
        [Console]::Error.WriteLine(($receipt | ConvertTo-Json -Compress))
        exit 1
    }
}
