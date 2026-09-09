#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0
$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:Packager = Join-Path $script:RepoRoot 'infra\azure\build_order_release_archive.ps1'
$script:WrapperBuilder = Join-Path $script:RepoRoot 'infra\azure\build_order_release_wrapper.ps1'
$script:ReleaseInstaller = Join-Path $script:RepoRoot 'infra\azure\install_release_from_archive.ps1'
$script:SystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$script:TestRoot = Join-Path $script:SystemTemp ('order-release-packager-test-' + [Guid]::NewGuid().ToString('N'))
$script:FixtureRepository = Join-Path $script:TestRoot 'fixture-repository'
$script:TestJunctions = New-Object 'System.Collections.Generic.List[string]'
$script:PowerShellExecutable = $(
    $candidateName = if ($PSVersionTable.PSEdition -ceq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
    $candidatePath = Join-Path $PSHOME $candidateName
    if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
        [IO.Path]::GetFullPath($candidatePath)
    } else {
        $processPath = [string](Get-Process -Id $PID).Path
        if ([string]::IsNullOrWhiteSpace($processPath) -or -not (Test-Path -LiteralPath $processPath -PathType Leaf)) {
            throw 'test_powershell_executable_missing'
        }
        [IO.Path]::GetFullPath($processPath)
    }
)
$script:ReleaseFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$script:CommittedBytes = [ordered]@{}

function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')

    if ($Condition) {
        $script:Passed++
        [Console]::Out.WriteLine('PASS ' + $Name)
    } else {
        $script:Failed++
        [Console]::Out.WriteLine('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
    }
}

function Quote-TestArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-TestPowerShellFile {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [hashtable]$Environment = @{}
    )

    $tokens = New-Object 'System.Collections.Generic.List[string]'
    foreach ($token in @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File')) {
        $tokens.Add($token)
    }
    $tokens.Add((Quote-TestArgument -Value $FilePath))
    foreach ($argument in $ArgumentList) { $tokens.Add((Quote-TestArgument -Value ([string]$argument))) }

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $script:PowerShellExecutable
    $startInfo.Arguments = $tokens -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($name in $Environment.Keys) {
        $startInfo.EnvironmentVariables[[string]$name] = [string]$Environment[$name]
    }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'test_process_start_failed' }
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [pscustomobject][ordered]@{
            exit_code = [int]$process.ExitCode
            stdout = [string]$stdout
            stderr = [string]$stderr
        }
    } finally {
        $process.Dispose()
    }
}

function Get-TestNonEmptyLines {
    param([AllowEmptyString()][string]$Text)

    return @($Text -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Read-TestReceipt {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][bool]$Success
    )

    $stdoutLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stdout))
    $stderrLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stderr))
    $receipt = $null
    $sourceLines = @(if ($Success) { $stdoutLines } else { $stderrLines })
    if ($sourceLines.Count -eq 1) {
        try { $receipt = $sourceLines[0] | ConvertFrom-Json -ErrorAction Stop }
        catch { $receipt = $null }
    }
    $hasOk = $null -ne $receipt -and $null -ne $receipt.PSObject.Properties['ok']
    Assert-True $(if ($Success) { 'command emits one compact success receipt' } else { 'command emits one compact failure receipt' }) (
        $null -ne $receipt -and
        $sourceLines.Count -eq 1 -and
        $sourceLines[0].Length -lt 2048 -and
        $(if ($Success) {
            $Result.exit_code -eq 0 -and $stderrLines.Count -eq 0 -and (-not $hasOk -or $receipt.ok -eq $true)
        } else {
            $Result.exit_code -ne 0 -and $stdoutLines.Count -eq 0 -and $hasOk -and $receipt.ok -eq $false
        })
    ) (($Result.stdout + $Result.stderr).Trim())
    return $receipt
}

function Invoke-TestGit {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$NoRepository
    )

    $commandArguments = @()
    if (-not $NoRepository) { $commandArguments += @('-C', $Repository) }
    $commandArguments += $Arguments
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git @commandArguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw ('fixture_git_failed:' + ($output -join ' '))
    }
    return (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
}

function New-TestFixtureRepository {
    New-Item -ItemType Directory -Path $script:FixtureRepository | Out-Null
    Invoke-TestGit -Repository $script:FixtureRepository -NoRepository -Arguments @(
        'init', '--quiet', $script:FixtureRepository
    ) | Out-Null
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('config', 'user.name', 'ORDER Test') | Out-Null
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('config', 'user.email', 'order-test@example.invalid') | Out-Null
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('config', 'core.autocrlf', 'false') | Out-Null
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('config', 'core.ignorecase', 'false') | Out-Null

    $utf8 = New-Object Text.UTF8Encoding($false)
    $ordinal = 0
    foreach ($relativePath in $script:ReleaseFiles) {
        $ordinal++
        $archivePath = $relativePath.Replace('\', '/')
        [byte[]]$bytes = $utf8.GetBytes(('committed-' + $ordinal + '|' + $archivePath + "`nsecond-line`n"))
        $script:CommittedBytes[$relativePath] = $bytes
        $fullPath = Join-Path $script:FixtureRepository $relativePath
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($fullPath)) -Force | Out-Null
        [IO.File]::WriteAllBytes($fullPath, $bytes)
    }
    $unrelated = Join-Path $script:FixtureRepository 'scripts\unrelated-working-tool.ps1'
    [IO.File]::WriteAllText($unrelated, "not part of the release`n", $utf8)
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('add', '--', 'scripts') | Out-Null
    $previousAuthorDate = $env:GIT_AUTHOR_DATE
    $previousCommitterDate = $env:GIT_COMMITTER_DATE
    try {
        $env:GIT_AUTHOR_DATE = '2001-02-03T04:05:06Z'
        $env:GIT_COMMITTER_DATE = '2001-02-03T04:05:06Z'
        Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('commit', '--quiet', '-m', 'fixture release') | Out-Null
    } finally {
        $env:GIT_AUTHOR_DATE = $previousAuthorDate
        $env:GIT_COMMITTER_DATE = $previousCommitterDate
    }
    return Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('rev-parse', 'HEAD')
}

function New-TestForeignRepository {
    param([Parameter(Mandatory = $true)][string]$SourceRepository)

    $foreignRepository = Join-Path $script:TestRoot 'foreign-repository'
    Invoke-TestGit -Repository $foreignRepository -NoRepository -Arguments @(
        'clone', '--quiet', '--no-hardlinks', $SourceRepository, $foreignRepository
    ) | Out-Null
    Invoke-TestGit -Repository $foreignRepository -Arguments @('config', 'user.name', 'Foreign ORDER Test') | Out-Null
    Invoke-TestGit -Repository $foreignRepository -Arguments @('config', 'user.email', 'foreign-order@example.invalid') | Out-Null
    [IO.File]::WriteAllText(
        (Join-Path $foreignRepository 'scripts\bus.ps1'),
        "foreign repository payload`n",
        (New-Object Text.UTF8Encoding($false))
    )
    Invoke-TestGit -Repository $foreignRepository -Arguments @('add', '--', 'scripts/bus.ps1') | Out-Null
    $previousAuthorDate = $env:GIT_AUTHOR_DATE
    $previousCommitterDate = $env:GIT_COMMITTER_DATE
    try {
        $env:GIT_AUTHOR_DATE = '2001-02-03T04:05:08Z'
        $env:GIT_COMMITTER_DATE = '2001-02-03T04:05:08Z'
        Invoke-TestGit -Repository $foreignRepository -Arguments @('commit', '--quiet', '-m', 'foreign release') | Out-Null
    } finally {
        $env:GIT_AUTHOR_DATE = $previousAuthorDate
        $env:GIT_COMMITTER_DATE = $previousCommitterDate
    }
    return [pscustomobject][ordered]@{
        path = $foreignRepository
        commit = Invoke-TestGit -Repository $foreignRepository -Arguments @('rev-parse', 'HEAD')
        git_directory = Join-Path $foreignRepository '.git'
        object_directory = Join-Path $foreignRepository '.git\objects'
    }
}

function New-TestJunction {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Target
    )

    $resolvedTestRoot = [IO.Path]::GetFullPath($script:TestRoot).TrimEnd('\', '/')
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $resolvedTarget = [IO.Path]::GetFullPath($Target)
    $testPrefix = $resolvedTestRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith($testPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not $resolvedTarget.StartsWith($testPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'test_junction_scope_invalid'
    }
    $junction = New-Item -ItemType Junction -Path $resolvedPath -Target $resolvedTarget -ErrorAction Stop
    if (([int]$junction.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -eq 0) {
        throw 'test_junction_not_reparse_point'
    }
    $script:TestJunctions.Add($resolvedPath)
    return [string]$junction.FullName
}

function New-TestPlumbingCommit {
    param(
        [Parameter(Mandatory = $true)][string]$BaseCommit,
        [Parameter(Mandatory = $true)][string[]]$IndexArguments,
        [Parameter(Mandatory = $true)][string]$Message
    )

    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('read-tree', $BaseCommit) | Out-Null
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments $IndexArguments | Out-Null
    $tree = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('write-tree')
    $previousAuthorDate = $env:GIT_AUTHOR_DATE
    $previousCommitterDate = $env:GIT_COMMITTER_DATE
    try {
        $env:GIT_AUTHOR_DATE = '2001-02-03T04:05:07Z'
        $env:GIT_COMMITTER_DATE = '2001-02-03T04:05:07Z'
        return Invoke-TestGit -Repository $script:FixtureRepository -Arguments @(
            'commit-tree', $tree, '-p', $BaseCommit, '-m', $Message
        )
    } finally {
        $env:GIT_AUTHOR_DATE = $previousAuthorDate
        $env:GIT_COMMITTER_DATE = $previousCommitterDate
    }
}

function Get-TestFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TestBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Read-TestArchiveEntries {
    param([Parameter(Mandatory = $true)][string]$Path)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $result = New-Object 'System.Collections.Generic.List[object]'
        foreach ($entry in $archive.Entries) {
            $stream = $entry.Open()
            $memory = New-Object IO.MemoryStream
            try {
                $stream.CopyTo($memory)
                $bytes = [byte[]]$memory.ToArray()
            } finally {
                $stream.Dispose()
                $memory.Dispose()
            }
            $result.Add([pscustomobject][ordered]@{
                name = [string]$entry.FullName
                bytes = $bytes
                compressed_length = [long]$entry.CompressedLength
                length = [long]$entry.Length
            })
        }
        return @($result.ToArray())
    } finally {
        $archive.Dispose()
    }
}

function Test-DeterministicZipStructure {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    $reader = New-Object IO.BinaryReader($stream)
    try {
        $localNames = New-Object 'System.Collections.Generic.List[string]'
        $localValid = $true
        foreach ($expectedPath in $script:ReleaseFiles) {
            $signature = $reader.ReadUInt32()
            $version = $reader.ReadUInt16()
            $flags = $reader.ReadUInt16()
            $method = $reader.ReadUInt16()
            $time = $reader.ReadUInt16()
            $date = $reader.ReadUInt16()
            [void]$reader.ReadUInt32()
            $compressed = $reader.ReadUInt32()
            $uncompressed = $reader.ReadUInt32()
            $nameLength = $reader.ReadUInt16()
            $extraLength = $reader.ReadUInt16()
            $name = [Text.Encoding]::UTF8.GetString($reader.ReadBytes($nameLength))
            [void]$reader.ReadBytes($extraLength)
            [void]$reader.ReadBytes([int]$compressed)
            $localNames.Add($name)
            if ($signature -ne [uint32]67324752 -or $version -ne 20 -or $flags -ne 2048 -or
                $method -ne 0 -or $time -ne 0 -or $date -ne 33 -or $extraLength -ne 0 -or
                $compressed -ne $uncompressed) {
                $localValid = $false
            }
        }
        $centralOffset = $stream.Position
        $centralNames = New-Object 'System.Collections.Generic.List[string]'
        $centralValid = $true
        foreach ($expectedPath in $script:ReleaseFiles) {
            $signature = $reader.ReadUInt32()
            $madeBy = $reader.ReadUInt16()
            $version = $reader.ReadUInt16()
            $flags = $reader.ReadUInt16()
            $method = $reader.ReadUInt16()
            $time = $reader.ReadUInt16()
            $date = $reader.ReadUInt16()
            [void]$reader.ReadUInt32()
            $compressed = $reader.ReadUInt32()
            $uncompressed = $reader.ReadUInt32()
            $nameLength = $reader.ReadUInt16()
            $extraLength = $reader.ReadUInt16()
            $commentLength = $reader.ReadUInt16()
            $disk = $reader.ReadUInt16()
            $internalAttributes = $reader.ReadUInt16()
            $externalAttributes = $reader.ReadUInt32()
            [void]$reader.ReadUInt32()
            $name = [Text.Encoding]::UTF8.GetString($reader.ReadBytes($nameLength))
            [void]$reader.ReadBytes($extraLength)
            [void]$reader.ReadBytes($commentLength)
            $centralNames.Add($name)
            if ($signature -ne [uint32]33639248 -or $madeBy -ne 20 -or $version -ne 20 -or
                $flags -ne 2048 -or $method -ne 0 -or $time -ne 0 -or $date -ne 33 -or
                $compressed -ne $uncompressed -or $extraLength -ne 0 -or $commentLength -ne 0 -or
                $disk -ne 0 -or $internalAttributes -ne 0 -or $externalAttributes -ne 0) {
                $centralValid = $false
            }
        }
        $endSignature = $reader.ReadUInt32()
        $disk = $reader.ReadUInt16()
        $centralDisk = $reader.ReadUInt16()
        $diskEntries = $reader.ReadUInt16()
        $totalEntries = $reader.ReadUInt16()
        $centralSize = $reader.ReadUInt32()
        $recordedCentralOffset = $reader.ReadUInt32()
        $commentLength = $reader.ReadUInt16()
        $endValid = $endSignature -eq [uint32]101010256 -and $disk -eq 0 -and $centralDisk -eq 0 -and
            $diskEntries -eq 6 -and $totalEntries -eq 6 -and $recordedCentralOffset -eq $centralOffset -and
            ($centralOffset + $centralSize + 22) -eq $stream.Length -and $commentLength -eq 0 -and
            $stream.Position -eq $stream.Length
        $expectedNames = @($script:ReleaseFiles | ForEach-Object { $_.Replace('\', '/') })
        return $localValid -and $centralValid -and $endValid -and
            (($localNames.ToArray() -join '|') -ceq ($expectedNames -join '|')) -and
            (($centralNames.ToArray() -join '|') -ceq ($expectedNames -join '|'))
    } catch {
        return $false
    } finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

function New-TestMalformedArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Names
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $file = New-Object IO.FileStream($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $archive = New-Object IO.Compression.ZipArchive($file, [IO.Compression.ZipArchiveMode]::Create, $true)
        try {
            foreach ($name in $Names) {
                $entry = $archive.CreateEntry($name, [IO.Compression.CompressionLevel]::NoCompression)
                $stream = $entry.Open()
                try {
                    [byte[]]$bytes = [Text.Encoding]::UTF8.GetBytes($name)
                    $stream.Write($bytes, 0, $bytes.Length)
                } finally { $stream.Dispose() }
            }
        } finally { $archive.Dispose() }
    } finally { $file.Dispose() }
}

function Get-TestValidationFailure {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object[]]$ExpectedEntries
    )

    try {
        Assert-OrderReleaseArchiveReadback -Path $Path -ExpectedEntries $ExpectedEntries
        return ''
    } catch {
        return [string]$_.Exception.Message
    }
}

try {
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
    $repositoryHead = Invoke-TestGit -Repository $script:RepoRoot -Arguments @('rev-parse', 'HEAD')
    $repositoryHeadArchive = Join-Path $script:TestRoot 'repository-head.zip'
    $repositoryHeadRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:RepoRoot,
        '-CommitId', $repositoryHead,
        '-OutputPath', $repositoryHeadArchive
    )
    $repositoryHeadReceipt = Read-TestReceipt -Result $repositoryHeadRun -Success $true
    $repositoryHeadEntries = @(Read-TestArchiveEntries -Path $repositoryHeadArchive)
    Assert-True 'current repository head is packageable by immutable full commit identity' (
        $repositoryHead -cmatch '^[0-9a-f]{40}$' -and
        $repositoryHeadReceipt.commit_id -ceq $repositoryHead -and
        $repositoryHeadReceipt.release_id -ceq $repositoryHead -and
        $repositoryHeadReceipt.release_file_count -eq 6 -and
        (($repositoryHeadEntries | ForEach-Object { $_.name }) -join '|') -ceq
            (($script:ReleaseFiles | ForEach-Object { $_.Replace('\', '/') }) -join '|')
    )

    $wrapperPath = Join-Path $script:TestRoot 'repository-head-wrapper.ps1'
    $installerSha256 = Get-TestFileSha256 -Path $script:ReleaseInstaller
    $wrapperRun = Invoke-TestPowerShellFile -FilePath $script:WrapperBuilder -ArgumentList @(
        '-ArchivePath', $repositoryHeadArchive,
        '-InstallerPath', $script:ReleaseInstaller,
        '-OutputPath', $wrapperPath,
        '-ExpectedArchiveSha256', ([string]$repositoryHeadReceipt.archive_sha256),
        '-ExpectedInstallerSha256', $installerSha256,
        '-ReleaseId', $repositoryHead
    )
    $wrapperReceipt = Read-TestReceipt -Result $wrapperRun -Success $true
    $programData = Join-Path $script:TestRoot 'ProgramData'
    New-Item -ItemType Directory -Path $programData | Out-Null
    $installRun = Invoke-TestPowerShellFile `
        -FilePath $wrapperPath `
        -Environment @{ ProgramData = $programData }
    $installReceipt = Read-TestReceipt -Result $installRun -Success $true
    $installedRoot = Join-Path $programData ('SFDC24\OrderSupervisor\releases\' + $repositoryHead)
    $installedManifest = Get-Content -LiteralPath (Join-Path $installedRoot '.release.json') -Raw | ConvertFrom-Json
    $installedHashesMatch = $true
    foreach ($relativePath in $script:ReleaseFiles) {
        if ((Get-TestFileSha256 -Path (Join-Path $installedRoot $relativePath)) -cne
            [string]$repositoryHeadReceipt.file_sha256.$relativePath) {
            $installedHashesMatch = $false
        }
    }
    Assert-True 'archive is accepted end-to-end by the existing wrapper and immutable installer' (
        $wrapperReceipt.release_id -ceq $repositoryHead -and
        $wrapperReceipt.archive_sha256 -ceq $repositoryHeadReceipt.archive_sha256 -and
        $installReceipt.status -ceq 'INSTALLED' -and
        $installReceipt.release_id -ceq $repositoryHead -and
        $installedManifest.release_id -ceq $repositoryHead -and
        $installedManifest.archive_sha256 -ceq $repositoryHeadReceipt.archive_sha256 -and
        $installedHashesMatch
    )

    $validCommit = New-TestFixtureRepository
    [IO.File]::WriteAllText(
        (Join-Path $script:FixtureRepository 'scripts\bus.ps1'),
        "mutable working tree`r`nmust not ship`r`n",
        (New-Object Text.UTF8Encoding($false))
    )
    [IO.File]::WriteAllText(
        (Join-Path $script:FixtureRepository 'scripts\untracked-extra.ps1'),
        "untracked and excluded`r`n",
        (New-Object Text.UTF8Encoding($false))
    )

    $archiveOne = Join-Path $script:TestRoot 'release-one.zip'
    $archiveTwo = Join-Path $script:TestRoot 'release-two.zip'
    $arguments = @('-RepositoryPath', $script:FixtureRepository, '-CommitId', $validCommit)
    $runOne = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @($arguments + @('-OutputPath', $archiveOne))
    $receiptOne = Read-TestReceipt -Result $runOne -Success $true
    $runTwo = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @($arguments + @('-OutputPath', $archiveTwo))
    $receiptTwo = Read-TestReceipt -Result $runTwo -Success $true
    $archiveEntries = @(Read-TestArchiveEntries -Path $archiveOne)

    $expectedNames = @($script:ReleaseFiles | ForEach-Object { $_.Replace('\', '/') })
    $payloadsMatch = $archiveEntries.Count -eq 6
    $hashesMatch = $null -ne $receiptOne
    $storedEntries = $archiveEntries.Count -eq 6
    for ($index = 0; $index -lt $archiveEntries.Count; $index++) {
        $relativePath = $script:ReleaseFiles[$index]
        if ([Convert]::ToBase64String([byte[]]$archiveEntries[$index].bytes) -cne
            [Convert]::ToBase64String([byte[]]$script:CommittedBytes[$relativePath])) {
            $payloadsMatch = $false
        }
        if ($null -eq $receiptOne -or
            [string]$receiptOne.file_sha256.$relativePath -cne (Get-TestBytesSha256 -Bytes ([byte[]]$script:CommittedBytes[$relativePath]))) {
            $hashesMatch = $false
        }
        if ([long]$archiveEntries[$index].compressed_length -ne [long]$archiveEntries[$index].length) {
            $storedEntries = $false
        }
    }
    Assert-True 'archive payloads are exact pinned Git-object bytes, not dirty or untracked working-tree bytes' (
        $payloadsMatch -and (($archiveEntries | ForEach-Object { $_.name }) -join '|') -ceq ($expectedNames -join '|')
    )
    Assert-True 'receipt binds full commit, exact archive digest, and all six file digests' (
        $receiptOne.release_id -ceq $validCommit -and
        $receiptOne.commit_id -ceq $validCommit -and
        $receiptOne.archive_sha256 -ceq (Get-TestFileSha256 -Path $archiveOne) -and
        $receiptOne.release_file_count -eq 6 -and
        @($receiptOne.file_sha256.PSObject.Properties).Count -eq 6 -and
        $hashesMatch
    )
    Assert-True 'same commit produces byte-identical archive and receipt digests' (
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($archiveOne)) -ceq
            [Convert]::ToBase64String([IO.File]::ReadAllBytes($archiveTwo)) -and
        $receiptOne.archive_sha256 -ceq $receiptTwo.archive_sha256
    )
    Assert-True 'archive fixes order, timestamps, storage method, flags, attributes, comments, and extra fields' (
        $storedEntries -and (Test-DeterministicZipStructure -Path $archiveOne)
    )

    $foreignRepository = New-TestForeignRepository -SourceRepository $script:FixtureRepository
    $hostileConfig = Join-Path $script:TestRoot 'hostile-git-config'
    [IO.File]::WriteAllText(
        $hostileConfig,
        "[core]`nworktree = " + ([string]$foreignRepository.path).Replace('\', '/') + "`n",
        (New-Object Text.UTF8Encoding($false))
    )
    $redirectedOutput = Join-Path $script:TestRoot 'redirected-by-environment.zip'
    $redirectedRun = Invoke-TestPowerShellFile `
        -FilePath $script:Packager `
        -ArgumentList @(
            '-RepositoryPath', $script:FixtureRepository,
            '-CommitId', ([string]$foreignRepository.commit),
            '-OutputPath', $redirectedOutput
        ) `
        -Environment @{
            GIT_DIR = [string]$foreignRepository.git_directory
            GIT_WORK_TREE = [string]$foreignRepository.path
            GIT_COMMON_DIR = [string]$foreignRepository.git_directory
            GIT_OBJECT_DIRECTORY = [string]$foreignRepository.object_directory
            git_alternate_object_directories = [string]$foreignRepository.object_directory
            GIT_REPLACE_REF_BASE = 'refs/hostile-replace/'
            GIT_CONFIG_GLOBAL = $hostileConfig
            GIT_CONFIG_COUNT = '1'
            GIT_CONFIG_KEY_0 = 'core.worktree'
            GIT_CONFIG_VALUE_0 = [string]$foreignRepository.path
        }
    $redirectedReceipt = Read-TestReceipt -Result $redirectedRun -Success $false
    Assert-True 'inherited Git environment and command-config cannot redirect the declared repository' (
        $redirectedReceipt.code -ceq 'commit_not_found' -and -not (Test-Path -LiteralPath $redirectedOutput)
    )

    $configuredOutput = Join-Path $script:TestRoot 'local-config-worktree.zip'
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @(
        'config', 'core.worktree', ([string]$foreignRepository.path).Replace('\', '/')
    ) | Out-Null
    try {
        $configuredRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
            '-RepositoryPath', $script:FixtureRepository,
            '-CommitId', $validCommit,
            '-OutputPath', $configuredOutput
        )
        $configuredReceipt = Read-TestReceipt -Result $configuredRun -Success $true
        $configuredEntries = @(Read-TestArchiveEntries -Path $configuredOutput)
        Assert-True 'explicit Git directory and work-tree anchors override hostile local core.worktree config' (
            $configuredReceipt.commit_id -ceq $validCommit -and
            [Convert]::ToBase64String([byte[]]$configuredEntries[1].bytes) -ceq
                [Convert]::ToBase64String([byte[]]$script:CommittedBytes['scripts\bus.ps1'])
        )
    } finally {
        Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('config', '--unset', 'core.worktree') | Out-Null
    }

    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @(
        'config', 'include.path', $hostileConfig.Replace('\', '/')
    ) | Out-Null
    try {
        $includedConfigOutput = Join-Path $script:TestRoot 'included-config.zip'
        $includedConfigRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
            '-RepositoryPath', $script:FixtureRepository,
            '-CommitId', $validCommit,
            '-OutputPath', $includedConfigOutput
        )
        $includedConfigReceipt = Read-TestReceipt -Result $includedConfigRun -Success $false
        Assert-True 'repository-local external config includes are rejected before object resolution' (
            $includedConfigReceipt.code -ceq 'repository_config_includes_forbidden' -and
            -not (Test-Path -LiteralPath $includedConfigOutput)
        )
    } finally {
        Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('config', '--unset', 'include.path') | Out-Null
    }

    $replacementBlobPath = Join-Path $script:TestRoot 'replacement-bus.ps1'
    [IO.File]::WriteAllText($replacementBlobPath, "replacement-ref payload`n", (New-Object Text.UTF8Encoding($false)))
    $replacementBlob = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @(
        'hash-object', '-w', $replacementBlobPath
    )
    $replacementCommit = New-TestPlumbingCommit `
        -BaseCommit $validCommit `
        -IndexArguments @(
            'update-index', '--cacheinfo', ('100644,' + $replacementBlob + ',scripts/bus.ps1')
        ) `
        -Message 'replacement commit'
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @(
        'replace', $validCommit, $replacementCommit
    ) | Out-Null
    try {
        $replacementOutput = Join-Path $script:TestRoot 'replacement-ref.zip'
        $replacementRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
            '-RepositoryPath', $script:FixtureRepository,
            '-CommitId', $validCommit,
            '-OutputPath', $replacementOutput
        )
        $replacementReceipt = Read-TestReceipt -Result $replacementRun -Success $false
        Assert-True 'replacement refs are rejected before a substituted commit can be packaged' (
            $replacementReceipt.code -ceq 'repository_replace_refs_forbidden' -and
            -not (Test-Path -LiteralPath $replacementOutput)
        )
    } finally {
        Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('replace', '-d', $validCommit) | Out-Null
    }

    $alternatesPath = Join-Path $script:FixtureRepository '.git\objects\info\alternates'
    [IO.File]::WriteAllText(
        $alternatesPath,
        ([string]$foreignRepository.object_directory).Replace('\', '/') + "`n",
        (New-Object Text.UTF8Encoding($false))
    )
    try {
        $alternatesOutput = Join-Path $script:TestRoot 'repository-alternates.zip'
        $alternatesRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
            '-RepositoryPath', $script:FixtureRepository,
            '-CommitId', $validCommit,
            '-OutputPath', $alternatesOutput
        )
        $alternatesReceipt = Read-TestReceipt -Result $alternatesRun -Success $false
        Assert-True 'repository object alternates are rejected before Git object resolution' (
            $alternatesReceipt.code -ceq 'repository_alternates_forbidden' -and
            -not (Test-Path -LiteralPath $alternatesOutput)
        )
    } finally {
        Remove-Item -LiteralPath $alternatesPath -Force -ErrorAction Stop
    }

    $junctionTarget = Join-Path $script:TestRoot 'junction-target'
    New-Item -ItemType Directory -Path $junctionTarget | Out-Null
    $repositoryJunction = New-TestJunction `
        -Path (Join-Path $script:TestRoot 'repository-junction') `
        -Target $junctionTarget
    $junctionRepositoryOutput = Join-Path $script:TestRoot 'repository-junction-result.zip'
    $junctionRepositoryRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', (Join-Path $repositoryJunction 'declared-repository'),
        '-CommitId', $validCommit,
        '-OutputPath', $junctionRepositoryOutput
    )
    $junctionRepositoryReceipt = Read-TestReceipt -Result $junctionRepositoryRun -Success $false
    Assert-True 'repository junction ancestors are rejected before traversal' (
        $junctionRepositoryReceipt.code -ceq 'repository_path_reparse_point' -and
        -not (Test-Path -LiteralPath $junctionRepositoryOutput)
    )

    $outputJunction = New-TestJunction `
        -Path (Join-Path $script:TestRoot 'output-junction') `
        -Target $junctionTarget
    $junctionOutputPath = Join-Path $outputJunction 'release.zip'
    $junctionOutputRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository,
        '-CommitId', $validCommit,
        '-OutputPath', $junctionOutputPath
    )
    $junctionOutputReceipt = Read-TestReceipt -Result $junctionOutputRun -Success $false
    Assert-True 'output junction ancestors are rejected before traversal or file creation' (
        $junctionOutputReceipt.code -ceq 'output_path_reparse_point' -and
        -not (Test-Path -LiteralPath (Join-Path $junctionTarget 'release.zip'))
    )

    $sentinelArchive = Join-Path $script:TestRoot 'sentinel.zip'
    [byte[]]$sentinel = [Text.Encoding]::UTF8.GetBytes('do-not-overwrite')
    [IO.File]::WriteAllBytes($sentinelArchive, $sentinel)
    $overwriteRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        $arguments + @('-OutputPath', $sentinelArchive)
    )
    $overwriteReceipt = Read-TestReceipt -Result $overwriteRun -Success $false
    Assert-True 'existing output is rejected and preserved byte-for-byte' (
        $overwriteReceipt.code -ceq 'output_already_exists' -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($sentinelArchive)) -ceq [Convert]::ToBase64String($sentinel)
    )

    $shortOutput = Join-Path $script:TestRoot 'short-id.zip'
    $shortRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository, '-CommitId', $validCommit.Substring(0, 12), '-OutputPath', $shortOutput
    )
    $shortReceipt = Read-TestReceipt -Result $shortRun -Success $false
    Assert-True 'abbreviated commit IDs fail before output creation' (
        $shortReceipt.code -ceq 'commit_id_invalid' -and -not (Test-Path -LiteralPath $shortOutput)
    )

    $uppercaseOutput = Join-Path $script:TestRoot 'uppercase-id.zip'
    $uppercaseRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository, '-CommitId', $validCommit.ToUpperInvariant(), '-OutputPath', $uppercaseOutput
    )
    $uppercaseReceipt = Read-TestReceipt -Result $uppercaseRun -Success $false
    Assert-True 'non-canonical uppercase commit IDs fail before output creation' (
        $uppercaseReceipt.code -ceq 'commit_id_invalid' -and -not (Test-Path -LiteralPath $uppercaseOutput)
    )

    $missingObjectOutput = Join-Path $script:TestRoot 'missing-object.zip'
    $missingObjectRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository, '-CommitId', ('f' * 40), '-OutputPath', $missingObjectOutput
    )
    $missingObjectReceipt = Read-TestReceipt -Result $missingObjectRun -Success $false
    Assert-True 'unknown full commit IDs fail before output creation' (
        $missingObjectReceipt.code -ceq 'commit_not_found' -and -not (Test-Path -LiteralPath $missingObjectOutput)
    )

    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('tag', '-a', 'fixture-tag', '-m', 'fixture tag', $validCommit) | Out-Null
    $tagObject = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('rev-parse', 'refs/tags/fixture-tag')
    $tagOutput = Join-Path $script:TestRoot 'tag-object.zip'
    $tagRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository, '-CommitId', $tagObject, '-OutputPath', $tagOutput
    )
    $tagReceipt = Read-TestReceipt -Result $tagRun -Success $false
    Assert-True 'a full tag-object ID cannot masquerade as its target commit' (
        $tagReceipt.code -ceq 'commit_identity_invalid' -and -not (Test-Path -LiteralPath $tagOutput)
    )

    $missingCommit = New-TestPlumbingCommit `
        -BaseCommit $validCommit `
        -IndexArguments @('update-index', '--force-remove', '--', 'scripts/order_supervisor_result.schema.json') `
        -Message 'missing canonical file'
    $missingOutput = Join-Path $script:TestRoot 'missing-file.zip'
    $missingRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository, '-CommitId', $missingCommit, '-OutputPath', $missingOutput
    )
    $missingReceipt = Read-TestReceipt -Result $missingRun -Success $false
    Assert-True 'missing canonical commit file is rejected before output creation' (
        $missingReceipt.code -ceq 'release_file_missing' -and -not (Test-Path -LiteralPath $missingOutput)
    )

    $caseBlobPath = Join-Path $script:TestRoot 'case-blob.txt'
    [IO.File]::WriteAllText($caseBlobPath, "case collision`n", (New-Object Text.UTF8Encoding($false)))
    $caseBlob = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('hash-object', '-w', $caseBlobPath)
    $caseCommit = New-TestPlumbingCommit `
        -BaseCommit $validCommit `
        -IndexArguments @('update-index', '--add', '--cacheinfo', ('100644,' + $caseBlob + ',scripts/BUS.ps1')) `
        -Message 'case collision'
    $caseOutput = Join-Path $script:TestRoot 'case-collision.zip'
    $caseRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository, '-CommitId', $caseCommit, '-OutputPath', $caseOutput
    )
    $caseReceipt = Read-TestReceipt -Result $caseRun -Success $false
    Assert-True 'case-colliding canonical commit paths are rejected before output creation' (
        $caseReceipt.code -ceq 'release_file_case_collision' -and -not (Test-Path -LiteralPath $caseOutput)
    )

    $linkCommit = New-TestPlumbingCommit `
        -BaseCommit $validCommit `
        -IndexArguments @('update-index', '--cacheinfo', ('120000,' + $caseBlob + ',scripts/bus.ps1')) `
        -Message 'symlink release file'
    $linkOutput = Join-Path $script:TestRoot 'symlink.zip'
    $linkRun = Invoke-TestPowerShellFile -FilePath $script:Packager -ArgumentList @(
        '-RepositoryPath', $script:FixtureRepository, '-CommitId', $linkCommit, '-OutputPath', $linkOutput
    )
    $linkReceipt = Read-TestReceipt -Result $linkRun -Success $false
    Assert-True 'non-regular canonical Git entries are rejected before output creation' (
        $linkReceipt.code -ceq 'release_file_type_invalid' -and -not (Test-Path -LiteralPath $linkOutput)
    )

    . $script:Packager
    $expectedValidationEntries = New-Object 'System.Collections.Generic.List[object]'
    foreach ($relativePath in $script:ReleaseFiles) {
        [byte[]]$bytes = $script:CommittedBytes[$relativePath]
        $expectedValidationEntries.Add([pscustomobject]@{
            archive_path = $relativePath.Replace('\', '/')
            bytes = $bytes
            sha256 = Get-TestBytesSha256 -Bytes $bytes
        })
    }
    $extraArchive = Join-Path $script:TestRoot 'extra-entry.zip'
    New-TestMalformedArchive -Path $extraArchive -Names @($expectedNames + 'scripts/extra.ps1')
    Assert-True 'read-back validator rejects extra archive entries' (
        (Get-TestValidationFailure -Path $extraArchive -ExpectedEntries $expectedValidationEntries.ToArray()) -ceq 'archive_files_extra'
    )
    $missingArchive = Join-Path $script:TestRoot 'missing-entry.zip'
    New-TestMalformedArchive -Path $missingArchive -Names @($expectedNames[0..4])
    Assert-True 'read-back validator rejects missing archive entries' (
        (Get-TestValidationFailure -Path $missingArchive -ExpectedEntries $expectedValidationEntries.ToArray()) -ceq 'archive_files_missing'
    )
    $collisionNames = @($expectedNames)
    $collisionNames[1] = $expectedNames[0].ToLowerInvariant()
    $collisionArchive = Join-Path $script:TestRoot 'entry-case-collision.zip'
    New-TestMalformedArchive -Path $collisionArchive -Names $collisionNames
    $collisionFailure = Get-TestValidationFailure `
        -Path $collisionArchive `
        -ExpectedEntries $expectedValidationEntries.ToArray()
    Assert-True 'read-back validator rejects case-colliding archive entries' (
        $collisionFailure -ceq 'archive_entry_case_collision'
    ) $collisionFailure

    $packagerText = [IO.File]::ReadAllText($script:Packager)
    Assert-True 'packager exposes no secret or credential parameter and never reads checkout payload files' (
        $packagerText -notmatch '(?im)^\s*\[[^\]]*\]\s*\$(Secret|ApiKey|Token|Credential)\b' -and
        $packagerText -notmatch '(?im)^\s*\$(Secret|ApiKey|Token|Credential)\s*=' -and
        $packagerText -notmatch '(?i)Get-Content\s+.*OrderSupervisor|ReadAllBytes\s*\(.*relativePath'
    )
} finally {
    foreach ($junctionPath in @($script:TestJunctions.ToArray())) {
        if (Test-Path -LiteralPath $junctionPath) {
            Remove-Item -LiteralPath $junctionPath -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $KeepArtifacts -and [IO.Directory]::Exists($script:TestRoot)) {
        $resolvedTestRoot = [IO.Path]::GetFullPath($script:TestRoot).TrimEnd('\', '/')
        $tempPrefix = $script:SystemTemp + [IO.Path]::DirectorySeparatorChar
        if ($resolvedTestRoot.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolvedTestRoot) -match '^order-release-packager-test-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction Stop
        }
    }
}

[Console]::Out.WriteLine(('RESULT passed={0} failed={1}' -f $script:Passed, $script:Failed))
if ($script:Failed -gt 0) { exit 1 }
