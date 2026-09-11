#Requires -Version 7.0
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0
$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:Preflight = Join-Path $script:RepoRoot 'infra/azure/verify_order_release_candidate.ps1'
$script:NativeTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('/', '\')
$script:TestRoot = Join-Path $script:NativeTemp ('order-commondir-linux-' + [Guid]::NewGuid().ToString('N'))
$script:VictimRepository = Join-Path $script:TestRoot 'victim'
$script:ForeignRepository = Join-Path $script:TestRoot 'foreign'
$script:Utf8 = New-Object Text.UTF8Encoding($false)

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

function Get-TestFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash([IO.File]::ReadAllBytes($Path)) |
            ForEach-Object { $_.ToString('x2') }) -join '')
    } finally { $sha.Dispose() }
}

function Get-TestTreeInventory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $root = [IO.Path]::GetFullPath($Path).TrimEnd('/', '\')
    $rows = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in @(Get-ChildItem -LiteralPath $root -Force -Recurse | Sort-Object FullName)) {
        $relative = [string]$item.FullName.Substring($root.Length).TrimStart('/', '\')
        if ($item -is [IO.DirectoryInfo]) {
            $rows.Add('D|' + $relative)
        } elseif ($item -is [IO.FileInfo]) {
            $rows.Add(('F|{0}|{1}|{2}' -f
                $relative,
                [long]$item.Length,
                (Get-TestFileSha256 -Path ([string]$item.FullName))))
        } else {
            $rows.Add(('O|{0}|{1}' -f $relative, [string]$item.GetType().FullName))
        }
    }
    return @($rows.ToArray()) -join "`n"
}

function Invoke-TestGit {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )

    $output = @(& git -C $Repository @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $result = [pscustomobject][ordered]@{
        exit_code = [int]$exitCode
        output = (@($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
    }
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw ('fixture_git_failed:' + $result.output)
    }
    return $result
}

function New-TestRepository {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Marker
    )

    New-Item -ItemType Directory -Path $Path | Out-Null
    $initOutput = @(& git init --quiet $Path 2>&1)
    if ($LASTEXITCODE -ne 0) { throw ('fixture_git_init_failed:' + ($initOutput -join ' ')) }
    foreach ($setting in @(
        @('user.name', 'ORDER Test'),
        @('user.email', 'order-test@example.invalid'),
        @('core.autocrlf', 'false'),
        @('core.ignorecase', 'false')
    )) {
        [void](Invoke-TestGit -Repository $Path -Arguments @('config', $setting[0], $setting[1]))
    }
    [IO.File]::WriteAllText((Join-Path $Path ($Marker + '.txt')), ($Marker + "`n"), $script:Utf8)
    [void](Invoke-TestGit -Repository $Path -Arguments @('add', '--', ($Marker + '.txt')))
    $oldAuthor = $env:GIT_AUTHOR_DATE
    $oldCommitter = $env:GIT_COMMITTER_DATE
    try {
        $env:GIT_AUTHOR_DATE = '2001-02-03T04:05:06Z'
        $env:GIT_COMMITTER_DATE = '2001-02-03T04:05:06Z'
        [void](Invoke-TestGit -Repository $Path -Arguments @('commit', '--quiet', '-m', ($Marker + ' fixture')))
    } finally {
        $env:GIT_AUTHOR_DATE = $oldAuthor
        $env:GIT_COMMITTER_DATE = $oldCommitter
    }
    return [string](Invoke-TestGit -Repository $Path -Arguments @('rev-parse', 'HEAD')).output
}

function Test-TestCommitVisible {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Commit
    )

    $result = Invoke-TestGit `
        -Repository $Repository `
        -Arguments @('cat-file', '-e', ($Commit + '^{commit}')) `
        -AllowFailure
    return $result.exit_code -eq 0
}

function Invoke-TestPreflight {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Receipt,
        [Parameter(Mandatory = $true)][string]$Installer
    )

    $arguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', $script:Preflight,
        '-RepositoryPath', $Repository,
        '-CommitId', $Commit,
        '-ArchivePath', $Archive,
        '-PackagerReceiptPath', $Receipt,
        '-InstallerPath', $Installer,
        '-ExpectedInstallerSha256', ('0' * 64)
    )
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = (Get-Process -Id $PID -ErrorAction Stop).Path
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $arguments) { [void]$startInfo.ArgumentList.Add([string]$argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'test_preflight_start_failed' }
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

function Read-TestFailureReceipt {
    param([Parameter(Mandatory = $true)]$Result)

    $stdoutLines = @([string]$Result.stdout -split '\r?\n' |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $stderrLines = @([string]$Result.stderr -split '\r?\n' |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $receipt = $null
    if ($stderrLines.Count -eq 1) {
        try { $receipt = $stderrLines[0] | ConvertFrom-Json -ErrorAction Stop }
        catch { $receipt = $null }
    }
    return [pscustomobject][ordered]@{
        stdout_lines = $stdoutLines
        stderr_lines = $stderrLines
        receipt = $receipt
    }
}

try {
    if (-not $IsLinux) { throw 'This test must run on Linux.' }
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null

    $caseProbe = Join-Path $script:TestRoot 'CaseProbe'
    [IO.File]::WriteAllText($caseProbe, "probe`n", $script:Utf8)
    $caseSensitive = -not (Test-Path -LiteralPath (Join-Path $script:TestRoot 'caseprobe'))
    Assert-True 'fixture root is a case-sensitive Linux filesystem' $caseSensitive $script:TestRoot
    if (-not $caseSensitive) { throw 'test_filesystem_not_case_sensitive' }
    [IO.File]::Delete($caseProbe)

    $victimCommit = New-TestRepository -Path $script:VictimRepository -Marker 'victim'
    $foreignCommit = New-TestRepository -Path $script:ForeignRepository -Marker 'foreign'
    Assert-True 'foreign attack commit starts outside the victim object store' (
        $victimCommit -cne $foreignCommit -and
        -not (Test-TestCommitVisible -Repository $script:VictimRepository -Commit $foreignCommit)
    )

    $victimGitDirectory = Join-Path $script:VictimRepository '.git'
    $foreignGitDirectory = Join-Path $script:ForeignRepository '.git'
    $disabledMetadata = Join-Path $victimGitDirectory 'commondir.disabled'
    [IO.File]::WriteAllText($disabledMetadata, $foreignGitDirectory + "`n", $script:Utf8)
    Assert-True 'a harmless commondir-like filename does not redirect Git' (
        -not (Test-TestCommitVisible -Repository $script:VictimRepository -Commit $foreignCommit)
    )
    $missingArchive = Join-Path $script:TestRoot 'missing-archive.zip'
    $missingReceipt = Join-Path $script:TestRoot 'missing-receipt.json'
    $missingInstaller = Join-Path $script:TestRoot 'missing-installer.ps1'
    $disabledRun = Invoke-TestPreflight `
        -Repository $script:VictimRepository `
        -Commit $foreignCommit `
        -Archive $missingArchive `
        -Receipt $missingReceipt `
        -Installer $missingInstaller
    $disabledEvidence = Read-TestFailureReceipt -Result $disabledRun
    Assert-True 'harmless metadata reaches the artifact boundary instead of the commondir guard' (
        $disabledRun.exit_code -eq 1 -and
        $disabledEvidence.stdout_lines.Count -eq 0 -and
        $disabledEvidence.stderr_lines.Count -eq 1 -and
        $null -ne $disabledEvidence.receipt -and
        $disabledEvidence.receipt.ok -eq $false -and
        $disabledEvidence.receipt.code -ceq 'archive_missing'
    ) ('exit=' + $disabledRun.exit_code + ' code=' + $(if ($null -ne $disabledEvidence.receipt) {
        [string]$disabledEvidence.receipt.code
    } else { '<unparseable>' }))
    [IO.File]::Delete($disabledMetadata)

    $exactMetadata = Join-Path $victimGitDirectory 'commondir'
    [IO.File]::WriteAllText($exactMetadata, $foreignGitDirectory + "`n", $script:Utf8)
    Assert-True 'exact-case commondir makes the foreign commit visible to Git' (
        Test-TestCommitVisible -Repository $script:VictimRepository -Commit $foreignCommit
    )
    $reportedCommonDirectory = [string](Invoke-TestGit `
        -Repository $script:VictimRepository `
        -Arguments @('rev-parse', '--path-format=absolute', '--git-common-dir')).output
    Assert-True 'Git resolves the attack to the foreign common directory' (
        [IO.Path]::GetFullPath($reportedCommonDirectory).TrimEnd('/', '\') -ceq
            [IO.Path]::GetFullPath($foreignGitDirectory).TrimEnd('/', '\')
    ) $reportedCommonDirectory

    $archive = Join-Path $script:TestRoot 'sentinel-archive.zip'
    $receipt = Join-Path $script:TestRoot 'sentinel-receipt.json'
    $installer = Join-Path $script:TestRoot 'sentinel-installer.ps1'
    [IO.File]::WriteAllText($archive, "not-a-zip`n", $script:Utf8)
    [IO.File]::WriteAllText($receipt, "{}`n", $script:Utf8)
    [IO.File]::WriteAllText($installer, "'not-read'`n", $script:Utf8)
    $victimInventoryBefore = Get-TestTreeInventory -Path $script:VictimRepository
    $foreignInventoryBefore = Get-TestTreeInventory -Path $script:ForeignRepository
    $archiveShaBefore = Get-TestFileSha256 -Path $archive
    $receiptShaBefore = Get-TestFileSha256 -Path $receipt
    $installerShaBefore = Get-TestFileSha256 -Path $installer

    $blockedRun = Invoke-TestPreflight `
        -Repository $script:VictimRepository `
        -Commit $foreignCommit `
        -Archive $archive `
        -Receipt $receipt `
        -Installer $installer
    $blockedEvidence = Read-TestFailureReceipt -Result $blockedRun
    Assert-True 'preflight rejects exact-case commondir before artifact or foreign-object resolution' (
        $blockedRun.exit_code -eq 1 -and
        $blockedEvidence.stdout_lines.Count -eq 0 -and
        $blockedEvidence.stderr_lines.Count -eq 1 -and
        [Text.Encoding]::UTF8.GetByteCount([string]$blockedEvidence.stderr_lines[0]) -lt 3072 -and
        $null -ne $blockedEvidence.receipt -and
        $blockedEvidence.receipt.schema -ceq 'blackboard.order-release-candidate-preflight-receipt.v1' -and
        $blockedEvidence.receipt.ok -eq $false -and
        $blockedEvidence.receipt.code -ceq 'repository_common_directory_forbidden'
    ) ('exit=' + $blockedRun.exit_code + ' code=' + $(if ($null -ne $blockedEvidence.receipt) {
        [string]$blockedEvidence.receipt.code
    } else { '<unparseable>' }))
    Assert-True 'commondir rejection leaves repositories and artifacts byte-identical' (
        $victimInventoryBefore -ceq (Get-TestTreeInventory -Path $script:VictimRepository) -and
        $foreignInventoryBefore -ceq (Get-TestTreeInventory -Path $script:ForeignRepository) -and
        $archiveShaBefore -ceq (Get-TestFileSha256 -Path $archive) -and
        $receiptShaBefore -ceq (Get-TestFileSha256 -Path $receipt) -and
        $installerShaBefore -ceq (Get-TestFileSha256 -Path $installer)
    )

    [IO.File]::Delete($exactMetadata)
    Assert-True 'removing exact-case commondir restores foreign-commit isolation' (
        -not (Test-TestCommitVisible -Repository $script:VictimRepository -Commit $foreignCommit)
    )

    if ($script:Failed -eq 0) {
        [Console]::Out.WriteLine('EXERCISED linux_exact_case_commondir')
    }
} finally {
    if ($KeepArtifacts) {
        [Console]::Out.WriteLine('ARTIFACTS ' + $script:TestRoot)
    } elseif (Test-Path -LiteralPath $script:TestRoot -PathType Container) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force
    }
}

[Console]::Out.WriteLine(('RESULT passed={0} failed={1}' -f $script:Passed, $script:Failed))
if ($script:Failed -ne 0) { exit 1 }
exit 0
