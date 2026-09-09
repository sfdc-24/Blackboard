#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0
$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:Preflight = Join-Path $script:RepoRoot 'infra\azure\verify_order_release_candidate.ps1'
$script:SystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$script:TestRoot = Join-Path $script:SystemTemp ('order-release-candidate-preflight-test-' + [Guid]::NewGuid().ToString('N'))
$script:FixtureRepository = Join-Path $script:TestRoot 'fixture-repository'
$script:ForeignRepository = Join-Path $script:TestRoot 'foreign-repository'
$script:InstallerRelativePath = 'infra\azure\install_release_from_archive.ps1'
$script:ReleaseFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$script:OriginalBytes = [ordered]@{}
$script:ReplacementBytes = [ordered]@{}
$script:JunctionPath = $null

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

function Get-TestBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Get-TestFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return Get-TestBytesSha256 -Bytes ([IO.File]::ReadAllBytes($Path))
}

function Quote-TestNativeArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    if ($Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"', '\"') + '"'
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
    $output = @(& git @commandArguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw ('fixture_git_failed:' + ($output -join ' ')) }
    return (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
}

function Invoke-TestPreflight {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Receipt,
        [Parameter(Mandatory = $true)][string]$Installer,
        [Parameter(Mandatory = $true)][string]$InstallerSha256,
        [hashtable]$Environment = @{}
    )

    $arguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $script:Preflight,
        '-RepositoryPath', $Repository,
        '-CommitId', $Commit,
        '-ArchivePath', $Archive,
        '-PackagerReceiptPath', $Receipt,
        '-InstallerPath', $Installer,
        '-ExpectedInstallerSha256', $InstallerSha256
    )
    $argumentText = (@($arguments | ForEach-Object {
        Quote-TestNativeArgument -Value ([string]$_)
    }) -join ' ')
    $hostPath = (Get-Process -Id $PID).Path
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $hostPath
    $startInfo.Arguments = $argumentText
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
    } finally { $process.Dispose() }
}

function Get-TestNonEmptyLines {
    param([AllowEmptyString()][string]$Text)

    return @($Text -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Read-TestPreflightReceipt {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][bool]$Success
    )

    $stdoutLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stdout))
    $stderrLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stderr))
    $lines = @(if ($Success) { $stdoutLines } else { $stderrLines })
    $receipt = $null
    if ($lines.Count -eq 1) {
        try { $receipt = $lines[0] | ConvertFrom-Json -ErrorAction Stop }
        catch { $receipt = $null }
    }
    Assert-True $(if ($Success) { 'command emits one bounded success receipt' } else { 'command emits one bounded failure receipt' }) (
        $null -ne $receipt -and
        $lines.Count -eq 1 -and
        [Text.Encoding]::UTF8.GetByteCount([string]$lines[0]) -lt 3072 -and
        $(if ($Success) {
            $Result.exit_code -eq 0 -and $stderrLines.Count -eq 0 -and $receipt.ok -eq $true
        } else {
            $Result.exit_code -ne 0 -and $stdoutLines.Count -eq 0 -and $receipt.ok -eq $false
        })
    ) (($Result.stdout + $Result.stderr).Trim())
    return $receipt
}

function Get-TestCrc32 {
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

function New-TestCanonicalArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$BytesByPath
    )

    $memory = New-Object IO.MemoryStream
    $writer = New-Object IO.BinaryWriter($memory)
    $central = New-Object 'System.Collections.Generic.List[object]'
    try {
        foreach ($relativePath in $script:ReleaseFiles) {
            [byte[]]$bytes = $BytesByPath[$relativePath]
            [byte[]]$nameBytes = (New-Object Text.UTF8Encoding($false)).GetBytes($relativePath.Replace('\', '/'))
            [uint32]$crc = Get-TestCrc32 -Bytes $bytes
            $central.Add([pscustomobject][ordered]@{
                name = $nameBytes
                bytes = $bytes
                crc = $crc
                offset = [uint32]$memory.Position
            })
            foreach ($value in @(
                [uint32]67324752, [uint16]20, [uint16]2048, [uint16]0,
                [uint16]0, [uint16]33, [uint32]$crc, [uint32]$bytes.Length,
                [uint32]$bytes.Length, [uint16]$nameBytes.Length, [uint16]0
            )) { $writer.Write($value) }
            $writer.Write($nameBytes)
            $writer.Write($bytes)
        }
        [uint32]$centralOffset = $memory.Position
        foreach ($item in $central) {
            foreach ($value in @(
                [uint32]33639248, [uint16]20, [uint16]20, [uint16]2048,
                [uint16]0, [uint16]0, [uint16]33, [uint32]$item.crc,
                [uint32]$item.bytes.Length, [uint32]$item.bytes.Length,
                [uint16]$item.name.Length, [uint16]0, [uint16]0, [uint16]0,
                [uint16]0, [uint32]0, [uint32]$item.offset
            )) { $writer.Write($value) }
            $writer.Write([byte[]]$item.name)
        }
        [uint32]$centralSize = $memory.Position - $centralOffset
        foreach ($value in @(
            [uint32]101010256, [uint16]0, [uint16]0, [uint16]6,
            [uint16]6, [uint32]$centralSize, [uint32]$centralOffset, [uint16]0
        )) { $writer.Write($value) }
        $writer.Flush()
        [IO.File]::WriteAllBytes($Path, [byte[]]$memory.ToArray())
    } finally {
        $writer.Dispose()
        $memory.Dispose()
    }
}

function New-TestPackagerReceiptObject {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$BytesByPath
    )

    $hashes = [ordered]@{}
    foreach ($relativePath in $script:ReleaseFiles) {
        $hashes[$relativePath] = Get-TestBytesSha256 -Bytes ([byte[]]$BytesByPath[$relativePath])
    }
    return [pscustomobject][ordered]@{
        schema = 'blackboard.order-release-archive-build-receipt.v1'
        ok = $true
        output_path = [IO.Path]::GetFullPath($ArchivePath)
        release_id = $Commit
        commit_id = $Commit
        archive_sha256 = Get-TestFileSha256 -Path $ArchivePath
        archive_bytes = [long](Get-Item -LiteralPath $ArchivePath).Length
        release_file_count = 6
        file_sha256 = [pscustomobject]$hashes
    }
}

function Write-TestJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $text = $Value | ConvertTo-Json -Compress -Depth 6
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding($false)))
}

function New-TestRepository {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Marker
    )

    New-Item -ItemType Directory -Path $Path | Out-Null
    Invoke-TestGit -Repository $Path -NoRepository -Arguments @('init', '--quiet', $Path) | Out-Null
    Invoke-TestGit -Repository $Path -Arguments @('config', 'user.name', 'ORDER Test') | Out-Null
    Invoke-TestGit -Repository $Path -Arguments @('config', 'user.email', 'order-test@example.invalid') | Out-Null
    Invoke-TestGit -Repository $Path -Arguments @('config', 'core.autocrlf', 'false') | Out-Null
    Invoke-TestGit -Repository $Path -Arguments @('config', 'core.ignorecase', 'false') | Out-Null
    $utf8 = New-Object Text.UTF8Encoding($false)
    $bytesByPath = [ordered]@{}
    $ordinal = 0
    foreach ($relativePath in $script:ReleaseFiles) {
        $ordinal++
        [byte[]]$bytes = $utf8.GetBytes(($Marker + '-' + $ordinal + '|' + $relativePath.Replace('\', '/') + "`n"))
        $bytesByPath[$relativePath] = $bytes
        $fullPath = Join-Path $Path $relativePath
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($fullPath)) -Force | Out-Null
        [IO.File]::WriteAllBytes($fullPath, $bytes)
    }
    $installerPath = Join-Path $Path $script:InstallerRelativePath
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($installerPath)) -Force | Out-Null
    [IO.File]::WriteAllText(
        $installerPath,
        "#Requires -Version 5.1`n[CmdletBinding()]`nparam()`n'fixture-installer'`n",
        $utf8
    )
    Invoke-TestGit -Repository $Path -Arguments @('add', '--', 'scripts', 'infra/azure/install_release_from_archive.ps1') | Out-Null
    $oldAuthor = $env:GIT_AUTHOR_DATE
    $oldCommitter = $env:GIT_COMMITTER_DATE
    try {
        $env:GIT_AUTHOR_DATE = '2001-02-03T04:05:06Z'
        $env:GIT_COMMITTER_DATE = '2001-02-03T04:05:06Z'
        Invoke-TestGit -Repository $Path -Arguments @('commit', '--quiet', '-m', ($Marker + ' fixture')) | Out-Null
    } finally {
        $env:GIT_AUTHOR_DATE = $oldAuthor
        $env:GIT_COMMITTER_DATE = $oldCommitter
    }
    return [pscustomobject][ordered]@{
        commit = Invoke-TestGit -Repository $Path -Arguments @('rev-parse', 'HEAD')
        installer = $installerPath
        bytes = $bytesByPath
    }
}

function Copy-TestReceiptWithMutation {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][scriptblock]$Mutation
    )

    $value = Get-Content -LiteralPath $Source -Raw | ConvertFrom-Json
    & $Mutation $value
    Write-TestJson -Path $Destination -Value $value
}

try {
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
    $fixture = New-TestRepository -Path $script:FixtureRepository -Marker 'original'
    $script:OriginalBytes = $fixture.bytes
    $commit = [string]$fixture.commit
    $installer = [string]$fixture.installer
    $installerSha = Get-TestFileSha256 -Path $installer
    $archive = Join-Path $script:TestRoot 'release.zip'
    New-TestCanonicalArchive -Path $archive -BytesByPath $script:OriginalBytes
    $receiptPath = Join-Path $script:TestRoot 'packager-receipt.json'
    $receiptObject = New-TestPackagerReceiptObject `
        -ArchivePath $archive `
        -Commit $commit `
        -BytesByPath $script:OriginalBytes
    Write-TestJson -Path $receiptPath -Value $receiptObject

    $statusBefore = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('status', '--porcelain=v1')
    $validOne = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $validReceipt = Read-TestPreflightReceipt -Result $validOne -Success $true
    $validTwo = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    [void](Read-TestPreflightReceipt -Result $validTwo -Success $true)
    $statusAfter = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('status', '--porcelain=v1')
    $expectedTree = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('rev-parse', ($commit + '^{tree}'))
    Assert-True 'valid candidate binds commit, tree, archive, installer, and all six file hashes' (
        $validReceipt.schema -ceq 'blackboard.order-release-candidate-preflight-receipt.v1' -and
        $validReceipt.status -ceq 'VERIFIED_OFFLINE' -and
        $validReceipt.release_id -ceq $commit -and
        $validReceipt.commit_id -ceq $commit -and
        $validReceipt.tree_id -ceq $expectedTree -and
        $validReceipt.archive_sha256 -ceq (Get-TestFileSha256 -Path $archive) -and
        $validReceipt.installer_sha256 -ceq $installerSha -and
        $validReceipt.release_file_count -eq 6 -and
        @($validReceipt.file_sha256.PSObject.Properties).Count -eq 6 -and
        $validReceipt.git_context_status -ceq 'EXPLICIT_REPOSITORY_REPLACEMENTS_DISABLED' -and
        $validReceipt.transport_status -ceq 'NOT_STARTED'
    )
    Assert-True 'positive installer control uses the exact canonical Git-blob bytes' (
        (Get-TestFileSha256 -Path $installer) -ceq $installerSha -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($installer)) -ceq
            [Convert]::ToBase64String((New-Object Text.UTF8Encoding($false)).GetBytes(
                "#Requires -Version 5.1`n[CmdletBinding()]`nparam()`n'fixture-installer'`n"
            ))
    )
    Assert-True 'success receipt is deterministic and preflight does not change the repository' (
        $validOne.stdout -ceq $validTwo.stdout -and
        $statusBefore -ceq $statusAfter
    )

    $redirectedReceiptPath = Join-Path $script:TestRoot 'redirected-packager-receipt.json'
    [IO.File]::WriteAllText(
        $redirectedReceiptPath,
        ((Get-Content -LiteralPath $receiptPath -Raw) + "`r`n"),
        (New-Object Text.UTF8Encoding($false))
    )
    $redirectedRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $redirectedReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    [void](Read-TestPreflightReceipt -Result $redirectedRun -Success $true)
    Assert-True 'one redirected Console.WriteLine terminator is accepted without weakening JSON shape' (
        $redirectedRun.exit_code -eq 0 -and $redirectedRun.stdout -ceq $validOne.stdout
    )

    $sourceText = Get-Content -LiteralPath $script:Preflight -Raw
    Assert-True 'implementation explicitly neutralizes inherited Git context and replacement refs' (
        $sourceText -match "StartsWith\('GIT_'" -and
        $sourceText -match "StartsWith\('GCM_'" -and
        $sourceText -match "'--no-replace-objects'" -and
        $sourceText -match "'--literal-pathspecs'" -and
        $sourceText -match "'--git-dir='" -and
        $sourceText -match "'--work-tree='" -and
        $sourceText -match "GIT_NO_REPLACE_OBJECTS'\] = '1'" -and
        $sourceText -match "repository_replace_refs_forbidden" -and
        $sourceText -match "repository_alternates_forbidden" -and
        $sourceText -notmatch "(?im)@\('(?:fetch|pull|checkout|reset)'"
    )
    Assert-True 'implementation contains no Azure, VM, ScheduledTasks, extraction, or output-file mutation command' (
        $sourceText -notmatch '(?i)\baz\s+|Invoke-Az|Get-Az|Set-Az|ScheduledTask|Expand-Archive|WriteAllBytes\(|WriteAllText\(|CreateNew|Remove-Item|Copy-Item|Move-Item'
    )

    $foreign = New-TestRepository -Path $script:ForeignRepository -Marker 'foreign'
    $ambientRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha `
        -Environment @{
            GIT_DIR = (Join-Path $script:ForeignRepository '.git')
            GIT_WORK_TREE = $script:ForeignRepository
            GIT_OBJECT_DIRECTORY = (Join-Path $script:ForeignRepository '.git\objects')
            GIT_REPLACE_REF_BASE = 'refs/attacker/'
            GIT_CONFIG_COUNT = '1'
            GIT_CONFIG_KEY_0 = 'core.hooksPath'
            GIT_CONFIG_VALUE_0 = 'attacker'
        }
    [void](Read-TestPreflightReceipt -Result $ambientRun -Success $true)
    Assert-True 'foreign inherited Git context cannot redirect the explicitly supplied repository' (
        $ambientRun.exit_code -eq 0 -and $ambientRun.stdout -ceq $validOne.stdout
    ) (($ambientRun.stdout + $ambientRun.stderr).Trim())

    $hostileConfig = Join-Path $script:TestRoot 'hostile-include.config'
    [IO.File]::WriteAllText(
        $hostileConfig,
        "[core]`nworktree = " + $script:ForeignRepository.Replace('\', '/') + "`n",
        (New-Object Text.UTF8Encoding($false))
    )
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @(
        'config', 'include.path', $hostileConfig.Replace('\', '/')
    ) | Out-Null
    try {
        $includeRun = Invoke-TestPreflight `
            -Repository $script:FixtureRepository `
            -Commit $commit `
            -Archive $archive `
            -Receipt $receiptPath `
            -Installer $installer `
            -InstallerSha256 $installerSha
        $includeFailure = Read-TestPreflightReceipt -Result $includeRun -Success $false
        Assert-True 'repository-local external config includes are rejected before object resolution' (
            $includeFailure.code -ceq 'repository_config_includes_forbidden'
        ) (($includeRun.stdout + $includeRun.stderr).Trim())
    } finally {
        Invoke-TestGit -Repository $script:FixtureRepository -Arguments @(
            'config', '--unset', 'include.path'
        ) | Out-Null
    }

    $alternatesPath = Join-Path $script:FixtureRepository '.git\objects\info\alternates'
    [IO.File]::WriteAllText(
        $alternatesPath,
        (Join-Path $script:ForeignRepository '.git\objects').Replace('\', '/') + "`n",
        (New-Object Text.UTF8Encoding($false))
    )
    try {
        $alternateRun = Invoke-TestPreflight `
            -Repository $script:FixtureRepository `
            -Commit $commit `
            -Archive $archive `
            -Receipt $receiptPath `
            -Installer $installer `
            -InstallerSha256 $installerSha
        $alternateFailure = Read-TestPreflightReceipt -Result $alternateRun -Success $false
        Assert-True 'alternate Git object stores are rejected before object resolution' (
            $alternateFailure.code -ceq 'repository_alternates_forbidden'
        ) (($alternateRun.stdout + $alternateRun.stderr).Trim())
    } finally {
        [IO.File]::Delete($alternatesPath)
    }

    $gitMetadataJunction = Join-Path $script:FixtureRepository '.git\untrusted-junction'
    [void](New-Item -ItemType Junction -Path $gitMetadataJunction -Target $script:ForeignRepository)
    try {
        $gitJunctionRun = Invoke-TestPreflight `
            -Repository $script:FixtureRepository `
            -Commit $commit `
            -Archive $archive `
            -Receipt $receiptPath `
            -Installer $installer `
            -InstallerSha256 $installerSha
        $gitJunctionFailure = Read-TestPreflightReceipt -Result $gitJunctionRun -Success $false
        Assert-True 'reparse point anywhere in Git metadata is rejected before Git execution' (
            $gitJunctionFailure.code -ceq 'repository_git_reparse_point'
        ) (($gitJunctionRun.stdout + $gitJunctionRun.stderr).Trim())
    } finally {
        if (Test-Path -LiteralPath $gitMetadataJunction) {
            [IO.Directory]::Delete($gitMetadataJunction)
        }
    }

    $script:ReplacementBytes = [ordered]@{}
    foreach ($relativePath in $script:ReleaseFiles) {
        $script:ReplacementBytes[$relativePath] = [byte[]]$script:OriginalBytes[$relativePath].Clone()
    }
    [byte[]]$replacementBus = (New-Object Text.UTF8Encoding($false)).GetBytes("replacement-object-must-not-ship`n")
    $script:ReplacementBytes['scripts\bus.ps1'] = $replacementBus
    [IO.File]::WriteAllBytes((Join-Path $script:FixtureRepository 'scripts\bus.ps1'), $replacementBus)
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('add', '--', 'scripts/bus.ps1') | Out-Null
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('commit', '--quiet', '-m', 'replacement fixture') | Out-Null
    $replacementCommit = Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('rev-parse', 'HEAD')
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('replace', $commit, $replacementCommit) | Out-Null
    $replaceBlockedRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $replaceBlockedFailure = Read-TestPreflightReceipt -Result $replaceBlockedRun -Success $false
    Assert-True 'repository-local refs/replace are rejected before object resolution' (
        $replaceBlockedFailure.code -ceq 'repository_replace_refs_forbidden'
    ) (($replaceBlockedRun.stdout + $replaceBlockedRun.stderr).Trim())
    Invoke-TestGit -Repository $script:FixtureRepository -Arguments @('replace', '-d', $commit) | Out-Null

    $replacementArchive = Join-Path $script:TestRoot 'replacement-release.zip'
    New-TestCanonicalArchive -Path $replacementArchive -BytesByPath $script:ReplacementBytes
    $replacementReceiptPath = Join-Path $script:TestRoot 'replacement-receipt.json'
    $replacementReceipt = New-TestPackagerReceiptObject `
        -ArchivePath $replacementArchive `
        -Commit $commit `
        -BytesByPath $script:ReplacementBytes
    Write-TestJson -Path $replacementReceiptPath -Value $replacementReceipt
    $replacementRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $replacementArchive `
        -Receipt $replacementReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $replacementFailure = Read-TestPreflightReceipt -Result $replacementRun -Success $false
    Assert-True 'replacement-generated archive and internally matching receipt are rejected against original objects' (
        $replacementFailure.code -ceq 'packager_receipt_file_digest_mismatch'
    ) (($replacementRun.stdout + $replacementRun.stderr).Trim())

    $dirtyPath = Join-Path $script:FixtureRepository 'scripts\OrderSupervisor.psm1'
    [IO.File]::WriteAllText($dirtyPath, "dirty-working-tree`n", (New-Object Text.UTF8Encoding($false)))
    $dirtyRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    [void](Read-TestPreflightReceipt -Result $dirtyRun -Success $true)
    Assert-True 'dirty working-tree bytes are ignored in favor of pinned Git objects' ($dirtyRun.exit_code -eq 0)

    $wrongHashReceiptPath = Join-Path $script:TestRoot 'wrong-file-hash-receipt.json'
    Copy-TestReceiptWithMutation -Source $receiptPath -Destination $wrongHashReceiptPath -Mutation {
        param($value)
        $value.file_sha256.'scripts\bus.ps1' = ('f' * 64)
    }
    $wrongHashRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $wrongHashReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $wrongHashFailure = Read-TestPreflightReceipt -Result $wrongHashRun -Success $false
    Assert-True 'receipt file hashes must match independently read Git blobs' (
        $wrongHashFailure.code -ceq 'packager_receipt_file_digest_mismatch'
    )

    $wrongOutputReceiptPath = Join-Path $script:TestRoot 'wrong-output-receipt.json'
    Copy-TestReceiptWithMutation -Source $receiptPath -Destination $wrongOutputReceiptPath -Mutation {
        param($value)
        $value.output_path = 'C:\foreign\release.zip'
    }
    $wrongOutputRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $wrongOutputReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $wrongOutputFailure = Read-TestPreflightReceipt -Result $wrongOutputRun -Success $false
    Assert-True 'receipt output path must name the exact archive being preflighted' (
        $wrongOutputFailure.code -ceq 'packager_receipt_output_path_mismatch'
    )

    $extraReceiptPath = Join-Path $script:TestRoot 'extra-property-receipt.json'
    $extraReceiptObject = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $extraReceiptObject | Add-Member -NotePropertyName unexpected -NotePropertyValue 'value'
    Write-TestJson -Path $extraReceiptPath -Value $extraReceiptObject
    $extraRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $extraReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $extraFailure = Read-TestPreflightReceipt -Result $extraRun -Success $false
    Assert-True 'receipt rejects extra properties rather than silently ignoring them' (
        $extraFailure.code -ceq 'packager_receipt_shape_invalid'
    )

    $duplicateReceiptPath = Join-Path $script:TestRoot 'duplicate-key-receipt.json'
    $validReceiptText = Get-Content -LiteralPath $receiptPath -Raw
    $duplicateText = $validReceiptText.Replace(
        '{"schema":',
        '{"schema":"attacker","schema":'
    )
    [IO.File]::WriteAllText($duplicateReceiptPath, $duplicateText, (New-Object Text.UTF8Encoding($false)))
    $duplicateRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $duplicateReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $duplicateFailure = Read-TestPreflightReceipt -Result $duplicateRun -Success $false
    Assert-True 'duplicate JSON keys fail closed before object conversion' (
        $duplicateFailure.code -ceq 'packager_receipt_duplicate_key'
    ) (($duplicateRun.stdout + $duplicateRun.stderr).Trim())

    $bomReceiptPath = Join-Path $script:TestRoot 'bom-receipt.json'
    [IO.File]::WriteAllText($bomReceiptPath, $validReceiptText, (New-Object Text.UTF8Encoding($true)))
    $bomRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $bomReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $bomFailure = Read-TestPreflightReceipt -Result $bomRun -Success $false
    Assert-True 'packager receipt must be canonical BOM-free UTF-8' (
        $bomFailure.code -ceq 'packager_receipt_utf8_bom_forbidden'
    )

    $tamperedArchive = Join-Path $script:TestRoot 'tampered-release.zip'
    [byte[]]$tamperedBytes = [IO.File]::ReadAllBytes($archive)
    $tamperedBytes[50] = $tamperedBytes[50] -bxor 1
    [IO.File]::WriteAllBytes($tamperedArchive, $tamperedBytes)
    $tamperedReceiptPath = Join-Path $script:TestRoot 'tampered-receipt.json'
    Copy-TestReceiptWithMutation -Source $receiptPath -Destination $tamperedReceiptPath -Mutation {
        param($value)
        $value.output_path = $tamperedArchive
    }
    $tamperedRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $tamperedArchive `
        -Receipt $tamperedReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $tamperedFailure = Read-TestPreflightReceipt -Result $tamperedRun -Success $false
    Assert-True 'archive byte changes fail the caller-bound packager digest first' (
        $tamperedFailure.code -ceq 'archive_digest_mismatch'
    )

    $nonCanonicalArchive = Join-Path $script:TestRoot 'noncanonical-release.zip'
    [byte[]]$nonCanonicalBytes = [IO.File]::ReadAllBytes($archive)
    $endOffset = $nonCanonicalBytes.Length - 22
    [uint32]$centralOffset = [BitConverter]::ToUInt32($nonCanonicalBytes, $endOffset + 16)
    $nonCanonicalBytes[[int]$centralOffset + 38] = 1
    [IO.File]::WriteAllBytes($nonCanonicalArchive, $nonCanonicalBytes)
    $nonCanonicalReceiptPath = Join-Path $script:TestRoot 'noncanonical-receipt.json'
    $nonCanonicalReceipt = New-TestPackagerReceiptObject `
        -ArchivePath $nonCanonicalArchive `
        -Commit $commit `
        -BytesByPath $script:OriginalBytes
    Write-TestJson -Path $nonCanonicalReceiptPath -Value $nonCanonicalReceipt
    $nonCanonicalRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $nonCanonicalArchive `
        -Receipt $nonCanonicalReceiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $nonCanonicalFailure = Read-TestPreflightReceipt -Result $nonCanonicalRun -Success $false
    Assert-True 'internally matching receipt cannot bless noncanonical ZIP metadata' (
        $nonCanonicalFailure.code -ceq 'archive_not_canonical_for_commit'
    )

    $mutatedInstaller = Join-Path $script:TestRoot 'mutated-installer.ps1'
    [IO.File]::WriteAllText(
        $mutatedInstaller,
        "#Requires -Version 5.1`nparam()`n'mutated-installer'`n",
        (New-Object Text.UTF8Encoding($false))
    )
    $mutatedInstallerRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $mutatedInstaller `
        -InstallerSha256 (Get-TestFileSha256 -Path $mutatedInstaller)
    $mutatedInstallerFailure = Read-TestPreflightReceipt -Result $mutatedInstallerRun -Success $false
    Assert-True 'caller-matching installer bytes must still be the exact installer blob from the commit' (
        $mutatedInstallerFailure.code -ceq 'installer_not_from_commit'
    )

    $crlfInstaller = Join-Path $script:TestRoot 'crlf-checkout-installer.ps1'
    $canonicalInstallerText = (New-Object Text.UTF8Encoding($false, $true)).GetString(
        [IO.File]::ReadAllBytes($installer)
    )
    [IO.File]::WriteAllText(
        $crlfInstaller,
        $canonicalInstallerText.Replace("`n", "`r`n"),
        (New-Object Text.UTF8Encoding($false))
    )
    $crlfInstallerRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $crlfInstaller `
        -InstallerSha256 (Get-TestFileSha256 -Path $crlfInstaller)
    $crlfInstallerFailure = Read-TestPreflightReceipt -Result $crlfInstallerRun -Success $false
    Assert-True 'core.autocrlf-style installer projection is rejected even when its caller digest matches' (
        $crlfInstallerFailure.code -ceq 'installer_not_from_commit' -and
        (Get-Item -LiteralPath $crlfInstaller).Length -gt (Get-Item -LiteralPath $installer).Length
    )

    $wrongInstallerDigestRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $installer `
        -InstallerSha256 ('0' * 64)
    $wrongInstallerDigestFailure = Read-TestPreflightReceipt -Result $wrongInstallerDigestRun -Success $false
    Assert-True 'installer must match the independent caller-pinned digest' (
        $wrongInstallerDigestFailure.code -ceq 'installer_digest_mismatch'
    )

    $shortCommitRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit.Substring(0, 12) `
        -Archive $archive `
        -Receipt $receiptPath `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $shortCommitFailure = Read-TestPreflightReceipt -Result $shortCommitRun -Success $false
    Assert-True 'abbreviated commit identity is rejected before Git access' (
        $shortCommitFailure.code -ceq 'commit_id_invalid'
    )

    $junctionTarget = Join-Path $script:TestRoot 'junction-target'
    $script:JunctionPath = Join-Path $script:TestRoot 'junction-input'
    New-Item -ItemType Directory -Path $junctionTarget | Out-Null
    [void](New-Item -ItemType Junction -Path $script:JunctionPath -Target $junctionTarget)
    $junctionArchive = Join-Path $junctionTarget 'release.zip'
    $junctionReceipt = Join-Path $junctionTarget 'receipt.json'
    Copy-Item -LiteralPath $archive -Destination $junctionArchive
    $junctionReceiptObject = New-TestPackagerReceiptObject `
        -ArchivePath (Join-Path $script:JunctionPath 'release.zip') `
        -Commit $commit `
        -BytesByPath $script:OriginalBytes
    Write-TestJson -Path $junctionReceipt -Value $junctionReceiptObject
    $junctionRun = Invoke-TestPreflight `
        -Repository $script:FixtureRepository `
        -Commit $commit `
        -Archive (Join-Path $script:JunctionPath 'release.zip') `
        -Receipt (Join-Path $script:JunctionPath 'receipt.json') `
        -Installer $installer `
        -InstallerSha256 $installerSha
    $junctionFailure = Read-TestPreflightReceipt -Result $junctionRun -Success $false
    Assert-True 'reparse point in an input ancestor fails closed before reading the artifact' (
        $junctionFailure.code -ceq 'archive_unsafe'
    ) (($junctionRun.stdout + $junctionRun.stderr).Trim())
}
finally {
    if ($null -ne $script:JunctionPath -and (Test-Path -LiteralPath $script:JunctionPath)) {
        try { [IO.Directory]::Delete($script:JunctionPath) }
        catch { [Console]::Out.WriteLine('WARN junction cleanup failed: ' + $_.Exception.Message) }
    }
    if ($KeepArtifacts) {
        [Console]::Out.WriteLine('ARTIFACTS ' + $script:TestRoot)
    } elseif (Test-Path -LiteralPath $script:TestRoot) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

[Console]::Out.WriteLine(('RESULT {0} passed, {1} failed' -f $script:Passed, $script:Failed))
if ($script:Failed -ne 0) { exit 1 }
