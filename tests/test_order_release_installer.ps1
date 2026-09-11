#Requires -Version 5.1
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$RepoRoot = Split-Path -Parent $PSScriptRoot
$InstallerPath = Join-Path $RepoRoot 'infra\azure\install_release_from_archive.ps1'
$SystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
)
$script:TestRoot = Join-Path $SystemTemp ('blackboard-order-release-installer-tests-' + [Guid]::NewGuid().ToString('N'))
$script:ReleaseRoot = Join-Path $script:TestRoot 'releases'
$script:InstallerTempRoot = Join-Path $script:TestRoot 'installer-temp'

# WHICH SHELL THE CHILD RUNS IN.
#
# Every case here runs install_release_from_archive.ps1 in a CHILD process, and that
# child was hardcoded to powershell.exe. On Linux the launch fails and the suite does
# not report a shell problem - it reports the SYMPTOM, "cannot find releases/<sha>
# because it does not exist", from a Get-ChildItem 120 lines later that is looking for
# output the installer never got to write. Measured: 0 assertions reached on Linux.
#
# ORDER_TEST_CHILD_SHELL overrides the child. Unset, this behaves exactly as before.
$script:ChildShell = $env:ORDER_TEST_CHILD_SHELL
if ([string]::IsNullOrWhiteSpace($script:ChildShell)) { $script:ChildShell = 'powershell.exe' }
$script:ResolvedChildShell = (Get-Command $script:ChildShell -CommandType Application -ErrorAction Stop |
                              Select-Object -First 1).Source
Write-Output ("CHILD_SHELL " + $script:ResolvedChildShell)

$script:Passed = 0
$script:Failed = 0
$script:RequiredFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)

function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) {
        $script:Passed++
        Write-Output ('PASS ' + $Name)
    }
    else {
        $script:Failed++
        Write-Output ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
    }
}

function Write-TestUtf8 {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string] $Text
    )

    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-TestSha256 {
    param([Parameter(Mandatory = $true)][string] $Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-TestDirectoryLink {
    # STAGES THE TRAVERSAL ATTACK ON WHICHEVER PLATFORM WE ARE ON.
    #
    # The installer refuses any release root, entry, or ancestor carrying
    # [IO.FileAttributes]::ReparsePoint. These fixtures exist to prove it refuses.
    # They were written with -ItemType Junction, which is NTFS-only, so on Linux the
    # fixture threw, the attack never staged, the installer had nothing to refuse,
    # and the assertion failed for the WRONG REASON - reporting an unguarded door as
    # a broken test. That is codex's commondir finding in another file.
    #
    # I measured the substitution rather than assuming it. On pwsh 7.5.4 on Linux a
    # symbolic link to a directory reports Attributes "Directory, ReparsePoint" and
    # LinkType "SymbolicLink", and writing through it lands OUTSIDE the intended root
    # - so the attack is real and all three guard sites see the attribute they key on.
    # The guard was already working on Linux; nothing proved it.
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Target
    )

    # $IsWindows exists only in PowerShell 6+. Under Windows PowerShell 5.1 with
    # Set-StrictMode -Version 2.0, reading it directly THROWS rather than returning
    # $null - so the presence test is not decoration. scripts/order_supervisor.ps1
    # already carries this exact guard and tests/test_order_linux_host.ps1 asserts it;
    # this follows that convention rather than inventing a second one.
    $onWindows = if (Test-Path Variable:IsWindows) { [bool]$IsWindows } else { $true }
    $itemType = if ($onWindows) { 'Junction' } else { 'SymbolicLink' }
    New-Item -ItemType $itemType -Path $Path -Target $Target | Out-Null

    # Never let a fixture report success without staging the attack. If the link is
    # not a reparse point, the guard under test would pass for having nothing to
    # refuse, which is the precise failure this helper exists to prevent.
    $created = Get-Item -LiteralPath $Path -Force
    if (($created.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
        throw ('test_directory_link_is_not_a_reparse_point:' + $Path + ':' + $created.Attributes)
    }
    return $created
}

function Get-TestArchiveDescriptor {
    param([Parameter(Mandatory = $true)][string] $Path)

    $archiveBytes = [IO.File]::ReadAllBytes($Path)
    return [pscustomobject]@{
        path = $Path
        payload = [Convert]::ToBase64String($archiveBytes)
        sha256 = Get-TestSha256 -Path $Path
    }
}

function New-TestArchive {
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [string] $Variant = 'A',
        [string[]] $Missing = @(),
        [hashtable] $Extra = @{}
    )

    $sourceRoot = Join-Path $script:TestRoot ($Name + '-source')
    $archivePath = Join-Path $script:TestRoot ($Name + '.zip')
    New-Item -ItemType Directory -Path $sourceRoot | Out-Null
    foreach ($relativePath in $script:RequiredFiles) {
        if ($Missing -ccontains $relativePath) { continue }
        Write-TestUtf8 -Path (Join-Path $sourceRoot $relativePath) -Text ($Variant + '|' + $relativePath)
    }
    foreach ($relativePath in $Extra.Keys) {
        Write-TestUtf8 -Path (Join-Path $sourceRoot $relativePath) -Text ([string]$Extra[$relativePath])
    }
    Compress-Archive -Path (Join-Path $sourceRoot 'scripts') -DestinationPath $archivePath -CompressionLevel Optimal
    return Get-TestArchiveDescriptor -Path $archivePath
}

function New-TestArchiveWithEntry {
    param(
        [Parameter(Mandatory = $true)] $SourceArchive,
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][string] $EntryPath
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archivePath = Join-Path $script:TestRoot ($Name + '.zip')
    Copy-Item -LiteralPath $SourceArchive.path -Destination $archivePath
    $archive = [IO.Compression.ZipFile]::Open($archivePath, [IO.Compression.ZipArchiveMode]::Update)
    try {
        $null = $archive.CreateEntry($EntryPath)
    }
    finally {
        $archive.Dispose()
    }
    return Get-TestArchiveDescriptor -Path $archivePath
}

function Invoke-Installer {
    param(
        [Parameter(Mandatory = $true)] $Archive,
        [Parameter(Mandatory = $true)][string] $ReleaseId,
        [string] $Digest = '',
        [string] $TargetReleaseRoot = '',
        [string] $InstallerUnderTest = ''
    )

    if ([string]::IsNullOrEmpty($Digest)) { $Digest = [string]$Archive.sha256 }
    if ([string]::IsNullOrEmpty($TargetReleaseRoot)) { $TargetReleaseRoot = $script:ReleaseRoot }
    if ([string]::IsNullOrEmpty($InstallerUnderTest)) { $InstallerUnderTest = $InstallerPath }
    $previousErrorActionPreference = $ErrorActionPreference
    $previousTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
    $previousTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
    try {
        $ErrorActionPreference = 'Continue'
        [Environment]::SetEnvironmentVariable('TEMP', $script:InstallerTempRoot, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $script:InstallerTempRoot, 'Process')
        $lines = @(
            & $script:ResolvedChildShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
                -File $InstallerUnderTest `
                -Payload ([string]$Archive.payload) `
                -ArchiveSha256 $Digest `
                -ReleaseId $ReleaseId `
                -ReleaseRoot $TargetReleaseRoot 2>&1
        )
        $installerExitCode = $LASTEXITCODE
    }
    finally {
        [Environment]::SetEnvironmentVariable('TEMP', $previousTemp, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $previousTmp, 'Process')
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return [pscustomobject]@{
        exit_code = $installerExitCode
        output = ($lines | ForEach-Object { [string]$_ }) -join "`n"
    }
}

function New-FaultInjectedInstaller {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('CleanupFailure', 'CleanupScopePreflight')]
        [string] $Mode
    )

    $source = [IO.File]::ReadAllText($InstallerPath, [Text.Encoding]::UTF8)
    if ($Mode -ceq 'CleanupFailure') {
        $needle = '        Remove-Item -LiteralPath $cleanupPath -Recurse -Force -ErrorAction Stop'
        $replacement = "        throw 'test_cleanup_failure'"
    }
    else {
        $needle = `
            '$temporaryRoot = Join-Path $resolvedSystemTemp (' +
            "'blackboard-release-' + [Guid]::NewGuid().ToString('N'))"
        $replacement = '$temporaryRoot = $resolvedSystemTemp'
    }
    $firstMatch = $source.IndexOf($needle, [StringComparison]::Ordinal)
    if (
        $firstMatch -lt 0 -or
        $firstMatch -ne $source.LastIndexOf($needle, [StringComparison]::Ordinal)
    ) {
        throw ('test_fault_injection_anchor_invalid:' + $Mode)
    }

    $instrumentedPath = Join-Path `
        $script:TestRoot `
        ('install_release_from_archive.' + $Mode.ToLowerInvariant() + '.ps1')
    Write-TestUtf8 `
        -Path $instrumentedPath `
        -Text $source.Replace($needle, $replacement)
    return $instrumentedPath
}

function Get-TestInstallerTempArtifacts {
    return @(
        Get-ChildItem -LiteralPath $script:InstallerTempRoot -Force -ErrorAction SilentlyContinue
    )
}

function Test-IsSingleBoundedInstallerTempArtifact {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]] $Items)

    if ($Items.Count -ne 1) { return $false }
    $item = $Items[0]
    $resolvedInstallerTemp = [IO.Path]::GetFullPath($script:InstallerTempRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $installerTempPrefix = $resolvedInstallerTemp + [IO.Path]::DirectorySeparatorChar
    $resolvedItem = [IO.Path]::GetFullPath($item.FullName).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    return (
        $resolvedItem.StartsWith($installerTempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        $item.PSIsContainer -and
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and
        [IO.Path]::GetFileName($resolvedItem) -cmatch '^blackboard-release-[0-9a-f]{32}$'
    )
}

function Remove-TestInstallerTempArtifacts {
    $resolvedInstallerTemp = [IO.Path]::GetFullPath($script:InstallerTempRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $installerTempPrefix = $resolvedInstallerTemp + [IO.Path]::DirectorySeparatorChar
    foreach ($item in @(Get-TestInstallerTempArtifacts)) {
        $resolvedItem = [IO.Path]::GetFullPath($item.FullName).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
        if (
            -not $resolvedItem.StartsWith($installerTempPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not $item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            [IO.Path]::GetFileName($resolvedItem) -cnotmatch '^blackboard-release-[0-9a-f]{32}$'
        ) {
            throw 'test_installer_temp_cleanup_scope_invalid'
        }
        Remove-Item -LiteralPath $resolvedItem -Recurse -Force -ErrorAction Stop
    }
}

function Read-ResultJson {
    param([Parameter(Mandatory = $true)][string] $Text)

    $jsonLine = @($Text -split "`r?`n" | Where-Object { $_ -match '^\{.*\}$' } | Select-Object -Last 1)
    if ($jsonLine.Count -ne 1) { return $null }
    try { return $jsonLine[0] | ConvertFrom-Json } catch { return $null }
}

function Get-TestRelativeInventory {
    param([Parameter(Mandatory = $true)][string] $Root)

    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $prefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    return @(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force |
            ForEach-Object {
                $relativePath = ([IO.Path]::GetFullPath($_.FullName)).Substring($prefix.Length).Replace('/', '\')
                if ($_.PSIsContainer) { $relativePath + '\' } else { $relativePath }
            } |
            Sort-Object
    )
}

try {
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
    New-Item -ItemType Directory -Path $script:InstallerTempRoot | Out-Null
    $cleanupFailureInstaller = New-FaultInjectedInstaller -Mode 'CleanupFailure'
    $cleanupScopePreflightInstaller = New-FaultInjectedInstaller -Mode 'CleanupScopePreflight'
    $archiveA = New-TestArchive -Name 'valid-a' -Variant 'A'
    $archiveB = New-TestArchive -Name 'valid-b' -Variant 'B'
    $missingArchive = New-TestArchive `
        -Name 'missing' `
        -Missing @('scripts\order_supervisor_result.schema.json')
    $extraArchive = New-TestArchive `
        -Name 'extra' `
        -Extra @{ 'scripts\unexpected.ps1' = 'unexpected' }
    $extraDirectoryArchive = New-TestArchiveWithEntry `
        -SourceArchive $archiveA `
        -Name 'extra-empty-directory' `
        -EntryPath 'unexpected-empty/'
    $explicitScriptsDirectoryArchive = New-TestArchiveWithEntry `
        -SourceArchive $archiveA `
        -Name 'explicit-scripts-directory' `
        -EntryPath 'scripts/'
    $duplicateEntryArchive = New-TestArchiveWithEntry `
        -SourceArchive $archiveA `
        -Name 'duplicate-entry' `
        -EntryPath 'scripts/bus.ps1'
    $caseCollisionArchive = New-TestArchiveWithEntry `
        -SourceArchive $archiveA `
        -Name 'case-collision' `
        -EntryPath 'scripts/BUS.ps1'

    $validReleaseId = '1111111111111111111111111111111111111111'
    $validInstall = Invoke-Installer -Archive $archiveA -ReleaseId $validReleaseId
    $validResult = Read-ResultJson -Text $validInstall.output
    $validDestination = Join-Path $script:ReleaseRoot $validReleaseId
    $validManifestPath = Join-Path $validDestination '.release.json'
    $validManifest = $null
    if (Test-Path -LiteralPath $validManifestPath -PathType Leaf) {
        $validManifest = [IO.File]::ReadAllText($validManifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    }
    Assert-True 'valid archive installs successfully' (
        $validInstall.exit_code -eq 0 -and
        $validResult -and
        $validResult.status -ceq 'INSTALLED' -and
        $validResult.release_id -ceq $validReleaseId -and
        $validResult.archive_sha256 -ceq $archiveA.sha256 -and
        [int]$validResult.required_file_count -eq 6
    ) $validInstall.output
    Assert-True 'fresh install inventory is exact' (
        ((Get-TestRelativeInventory -Root $validDestination) -join '|') -ceq ((@('scripts\') + $script:RequiredFiles + '.release.json' | Sort-Object) -join '|')
    )
    Assert-True 'manifest records exact identity and deterministic hash keys' (
        $validManifest -and
        $validManifest.schema -ceq 'blackboard.order-worker-release.v1' -and
        $validManifest.release_id -ceq $validReleaseId -and
        $validManifest.archive_sha256 -ceq $archiveA.sha256 -and
        (($validManifest.file_sha256.PSObject.Properties.Name) -join '|') -ceq ($script:RequiredFiles -join '|')
    )
    $validHashesMatch = $true
    foreach ($relativePath in $script:RequiredFiles) {
        if (
            [string]$validManifest.file_sha256.PSObject.Properties[$relativePath].Value -cne
            (Get-TestSha256 -Path (Join-Path $validDestination $relativePath))
        ) {
            $validHashesMatch = $false
        }
    }
    Assert-True 'manifest per-file hashes match installed bytes' $validHashesMatch

    $validOutputLines = @(
        $validInstall.output -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    Assert-True 'successful receipt is emitted once after explicit cleanup success' (
        $validOutputLines.Count -eq 1 -and
        (($validResult.PSObject.Properties.Name) -join '|') -ceq
            'status|release_id|archive_sha256|destination|required_file_count|cleanup_status|cleanup_code' -and
        $validResult.cleanup_status -ceq 'SUCCEEDED' -and
        $null -eq $validResult.cleanup_code
    ) $validInstall.output

    $cleanupFaultReleaseId = ('21' * 20)
    $cleanupFaultInstall = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId $cleanupFaultReleaseId `
        -InstallerUnderTest $cleanupFailureInstaller
    $cleanupFaultInstallResult = Read-ResultJson -Text $cleanupFaultInstall.output
    $cleanupFaultInstallLines = @(
        $cleanupFaultInstall.output -split "`r?`n" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    $cleanupFaultDestination = Join-Path $script:ReleaseRoot $cleanupFaultReleaseId
    $cleanupFaultInstallResidue = @(Get-TestInstallerTempArtifacts)
    Assert-True 'installed release returns one bounded success receipt when cleanup fails' (
        $cleanupFaultInstall.exit_code -eq 0 -and
        $cleanupFaultInstallLines.Count -eq 1 -and
        $cleanupFaultInstallResult -and
        $cleanupFaultInstallResult.status -ceq 'INSTALLED' -and
        $cleanupFaultInstallResult.cleanup_status -ceq 'FAILED' -and
        $cleanupFaultInstallResult.cleanup_code -ceq 'TEMPORARY_CLEANUP_FAILED' -and
        (Test-Path -LiteralPath $cleanupFaultDestination -PathType Container) -and
        (Test-IsSingleBoundedInstallerTempArtifact -Items $cleanupFaultInstallResidue)
    ) $cleanupFaultInstall.output
    Remove-TestInstallerTempArtifacts

    $cleanupFaultManifestPath = Join-Path $cleanupFaultDestination '.release.json'
    $cleanupFaultManifestBeforeReplay = [Convert]::ToBase64String(
        [IO.File]::ReadAllBytes($cleanupFaultManifestPath)
    )
    $cleanupFaultReplay = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId $cleanupFaultReleaseId `
        -InstallerUnderTest $cleanupFailureInstaller
    $cleanupFaultReplayResult = Read-ResultJson -Text $cleanupFaultReplay.output
    $cleanupFaultReplayLines = @(
        $cleanupFaultReplay.output -split "`r?`n" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    $cleanupFaultManifestAfterReplay = [Convert]::ToBase64String(
        [IO.File]::ReadAllBytes($cleanupFaultManifestPath)
    )
    $cleanupFaultReplayResidue = @(Get-TestInstallerTempArtifacts)
    Assert-True 'validated replay returns one bounded success receipt when cleanup fails' (
        $cleanupFaultReplay.exit_code -eq 0 -and
        $cleanupFaultReplayLines.Count -eq 1 -and
        $cleanupFaultReplayResult -and
        $cleanupFaultReplayResult.status -ceq 'ALREADY_INSTALLED' -and
        $cleanupFaultReplayResult.cleanup_status -ceq 'FAILED' -and
        $cleanupFaultReplayResult.cleanup_code -ceq 'TEMPORARY_CLEANUP_FAILED' -and
        $cleanupFaultManifestAfterReplay -ceq $cleanupFaultManifestBeforeReplay -and
        (Test-IsSingleBoundedInstallerTempArtifact -Items $cleanupFaultReplayResidue)
    ) $cleanupFaultReplay.output
    Remove-TestInstallerTempArtifacts

    $preflightCleanupReleaseId = ('24' * 20)
    $preflightCleanupResult = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId $preflightCleanupReleaseId `
        -InstallerUnderTest $cleanupScopePreflightInstaller
    $preflightCleanupJsonLines = @(
        $preflightCleanupResult.output -split "`r?`n" | Where-Object { $_ -match '^\{.*\}$' }
    )
    Assert-True 'cleanup scope preflight fails before destination mutation' (
        $preflightCleanupResult.exit_code -ne 0 -and
        $preflightCleanupResult.output -match 'temporary_cleanup_scope_invalid' -and
        $preflightCleanupJsonLines.Count -eq 0 -and
        @(Get-TestInstallerTempArtifacts).Count -eq 0 -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $preflightCleanupReleaseId))
    ) $preflightCleanupResult.output

    $primaryCleanupReleaseId = ('23' * 20)
    $primaryCleanupWrongDigest = ('0' * 64)
    if ($primaryCleanupWrongDigest -ceq $archiveA.sha256) {
        $primaryCleanupWrongDigest = ('f' * 64)
    }
    $primaryCleanupResult = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId $primaryCleanupReleaseId `
        -Digest $primaryCleanupWrongDigest `
        -InstallerUnderTest $cleanupFailureInstaller
    $primaryCleanupJsonLines = @(
        $primaryCleanupResult.output -split "`r?`n" | Where-Object { $_ -match '^\{.*\}$' }
    )
    $primaryCleanupResidue = @(Get-TestInstallerTempArtifacts)
    Assert-True 'primary error record survives a simultaneous cleanup failure' (
        $primaryCleanupResult.exit_code -ne 0 -and
        $primaryCleanupResult.output -match 'archive_digest_mismatch' -and
        $primaryCleanupResult.output -match 'FullyQualifiedErrorId\s*:\s*archive_digest_mismatch' -and
        $primaryCleanupResult.output -notmatch 'test_cleanup_failure' -and
        $primaryCleanupJsonLines.Count -eq 0 -and
        (Test-IsSingleBoundedInstallerTempArtifact -Items $primaryCleanupResidue) -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $primaryCleanupReleaseId))
    ) $primaryCleanupResult.output
    Remove-TestInstallerTempArtifacts

    $shortReleaseId = 'abcdef0'
    $shortReleaseResult = Invoke-Installer -Archive $archiveA -ReleaseId $shortReleaseId
    Assert-True 'release id must be a full 40-character commit id' (
        $shortReleaseResult.exit_code -ne 0 -and
        $shortReleaseResult.output -match '\^\[0-9a-f\]\{40\}\$' -and
        $shortReleaseResult.output -notmatch 'ALREADY_INSTALLED'
    ) $shortReleaseResult.output

    $volumeRoot = [IO.Path]::GetPathRoot($script:TestRoot)
    $volumeRootReleaseId = 'dddddddddddddddddddddddddddddddddddddddd'
    $volumeRootResult = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId $volumeRootReleaseId `
        -TargetReleaseRoot $volumeRoot
    Assert-True 'release root cannot be a volume root' (
        $volumeRootResult.exit_code -ne 0 -and
        $volumeRootResult.output -match 'release_root_volume_root_forbidden' -and
        $volumeRootResult.output -notmatch 'ALREADY_INSTALLED'
    ) $volumeRootResult.output

    $releaseRootJunctionTarget = Join-Path $script:TestRoot 'release-root-junction-target'
    New-Item -ItemType Directory -Path $releaseRootJunctionTarget | Out-Null
    Write-TestUtf8 `
        -Path (Join-Path $releaseRootJunctionTarget 'release-root-marker-must-not-be-read.txt') `
        -Text 'outside release root'
    $releaseRootJunction = Join-Path $script:TestRoot 'release-root-junction'
    $null = New-TestDirectoryLink -Path $releaseRootJunction -Target $releaseRootJunctionTarget
    $releaseRootJunctionResult = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId ('f' * 40) `
        -TargetReleaseRoot $releaseRootJunction
    Assert-True 'release root junction is rejected without traversal' (
        $releaseRootJunctionResult.exit_code -ne 0 -and
        $releaseRootJunctionResult.output -match 'release_root_reparse_point:' -and
        $releaseRootJunctionResult.output -notmatch 'release-root-marker-must-not-be-read' -and
        $releaseRootJunctionResult.output -notmatch 'ALREADY_INSTALLED'
    ) $releaseRootJunctionResult.output

    $ancestorJunctionTarget = Join-Path $script:TestRoot 'ancestor-junction-target'
    New-Item -ItemType Directory -Path $ancestorJunctionTarget | Out-Null
    Write-TestUtf8 `
        -Path (Join-Path $ancestorJunctionTarget 'ancestor-marker-must-not-be-read.txt') `
        -Text 'outside ancestor'
    $ancestorJunction = Join-Path $script:TestRoot 'ancestor-junction'
    $null = New-TestDirectoryLink -Path $ancestorJunction -Target $ancestorJunctionTarget
    $rootBelowJunction = Join-Path $ancestorJunction 'nested\releases'
    $ancestorJunctionResult = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId ('0' * 40) `
        -TargetReleaseRoot $rootBelowJunction
    Assert-True 'release root ancestor junction is rejected without traversal' (
        $ancestorJunctionResult.exit_code -ne 0 -and
        $ancestorJunctionResult.output -match 'release_root_reparse_point:' -and
        $ancestorJunctionResult.output -notmatch 'ancestor-marker-must-not-be-read' -and
        $ancestorJunctionResult.output -notmatch 'ALREADY_INSTALLED'
    ) $ancestorJunctionResult.output

    $wrongDigest = ('0' * 64)
    if ($wrongDigest -ceq $archiveA.sha256) { $wrongDigest = ('f' * 64) }
    $digestMismatchId = '2222222222222222222222222222222222222222'
    $digestMismatch = Invoke-Installer -Archive $archiveA -ReleaseId $digestMismatchId -Digest $wrongDigest
    Assert-True 'digest mismatch is rejected before install' (
        $digestMismatch.exit_code -ne 0 -and
        $digestMismatch.output -match 'archive_digest_mismatch' -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $digestMismatchId))
    ) $digestMismatch.output

    $existingDigestMismatch = Invoke-Installer -Archive $archiveA -ReleaseId $validReleaseId -Digest $wrongDigest
    Assert-True 'caller payload digest is verified before already-installed path' (
        $existingDigestMismatch.exit_code -ne 0 -and
        $existingDigestMismatch.output -match 'archive_digest_mismatch' -and
        $existingDigestMismatch.output -notmatch 'ALREADY_INSTALLED'
    ) $existingDigestMismatch.output

    $missingReleaseId = '3333333333333333333333333333333333333333'
    $missingResult = Invoke-Installer -Archive $missingArchive -ReleaseId $missingReleaseId
    Assert-True 'archive missing a required file is rejected atomically' (
        $missingResult.exit_code -ne 0 -and
        $missingResult.output -match 'release_files_missing' -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $missingReleaseId))
    ) $missingResult.output

    $extraReleaseId = '4444444444444444444444444444444444444444'
    $extraResult = Invoke-Installer -Archive $extraArchive -ReleaseId $extraReleaseId
    Assert-True 'archive with an extra file is rejected atomically' (
        $extraResult.exit_code -ne 0 -and
        $extraResult.output -match 'release_files_extra' -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $extraReleaseId))
    ) $extraResult.output

    $extraDirectoryReleaseId = '2222222222222222222222222222222222222222'
    $extraDirectoryResult = Invoke-Installer `
        -Archive $extraDirectoryArchive `
        -ReleaseId $extraDirectoryReleaseId
    Assert-True 'archive with an explicit extra empty directory is rejected' (
        $extraDirectoryResult.exit_code -ne 0 -and
        $extraDirectoryResult.output -match 'release_files_extra:unexpected-empty\\' -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $extraDirectoryReleaseId))
    ) $extraDirectoryResult.output

    $explicitScriptsDirectoryReleaseId = ('19' * 20)
    $explicitScriptsDirectoryResult = Invoke-Installer `
        -Archive $explicitScriptsDirectoryArchive `
        -ReleaseId $explicitScriptsDirectoryReleaseId
    Assert-True 'explicit required scripts directory entry is accepted once' (
        $explicitScriptsDirectoryResult.exit_code -eq 0 -and
        (Read-ResultJson -Text $explicitScriptsDirectoryResult.output).status -ceq 'INSTALLED'
    ) $explicitScriptsDirectoryResult.output

    $duplicateEntryReleaseId = ('16' * 20)
    $duplicateEntryResult = Invoke-Installer `
        -Archive $duplicateEntryArchive `
        -ReleaseId $duplicateEntryReleaseId
    Assert-True 'archive with duplicate entry is rejected' (
        $duplicateEntryResult.exit_code -ne 0 -and
        $duplicateEntryResult.output -match 'release_entries_duplicate:scripts\\bus\.ps1' -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $duplicateEntryReleaseId))
    ) $duplicateEntryResult.output

    $caseCollisionReleaseId = ('17' * 20)
    $caseCollisionResult = Invoke-Installer `
        -Archive $caseCollisionArchive `
        -ReleaseId $caseCollisionReleaseId
    Assert-True 'archive with case-colliding entry is rejected' (
        $caseCollisionResult.exit_code -ne 0 -and
        $caseCollisionResult.output -match 'release_entries_case_collision:scripts\\BUS\.ps1' -and
        -not (Test-Path -LiteralPath (Join-Path $script:ReleaseRoot $caseCollisionReleaseId))
    ) $caseCollisionResult.output

    $missingManifestId = '5555555555555555555555555555555555555555'
    $missingManifestInstall = Invoke-Installer -Archive $archiveA -ReleaseId $missingManifestId
    $missingManifestPath = Join-Path (Join-Path $script:ReleaseRoot $missingManifestId) '.release.json'
    Remove-Item -LiteralPath $missingManifestPath -Force
    $missingManifestReplay = Invoke-Installer -Archive $archiveA -ReleaseId $missingManifestId
    Assert-True 'existing release with missing manifest is rejected' (
        $missingManifestInstall.exit_code -eq 0 -and
        $missingManifestReplay.exit_code -ne 0 -and
        $missingManifestReplay.output -match 'existing_release_missing_manifest' -and
        $missingManifestReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $missingManifestReplay.output

    $partialManifestId = '6666666666666666666666666666666666666666'
    $partialManifestInstall = Invoke-Installer -Archive $archiveA -ReleaseId $partialManifestId
    $partialManifestPath = Join-Path (Join-Path $script:ReleaseRoot $partialManifestId) '.release.json'
    Write-TestUtf8 -Path $partialManifestPath -Text '{"schema":"blackboard.order-worker-release.v1"}'
    $partialManifestReplay = Invoke-Installer -Archive $archiveA -ReleaseId $partialManifestId
    Assert-True 'existing release with partial manifest is rejected' (
        $partialManifestInstall.exit_code -eq 0 -and
        $partialManifestReplay.exit_code -ne 0 -and
        $partialManifestReplay.output -match 'existing_release_manifest_invalid' -and
        $partialManifestReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $partialManifestReplay.output

    $typedManifestId = 'cccccccccccccccccccccccccccccccccccccccc'
    $typedManifestInstall = Invoke-Installer -Archive $archiveA -ReleaseId $typedManifestId
    $typedManifestPath = Join-Path (Join-Path $script:ReleaseRoot $typedManifestId) '.release.json'
    $typedManifest = [IO.File]::ReadAllText($typedManifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $typedManifest.release_id = @($typedManifestId)
    Write-TestUtf8 -Path $typedManifestPath -Text ($typedManifest | ConvertTo-Json -Depth 5)
    $typedManifestReplay = Invoke-Installer -Archive $archiveA -ReleaseId $typedManifestId
    Assert-True 'manifest identity fields must be strings' (
        $typedManifestInstall.exit_code -eq 0 -and
        $typedManifestReplay.exit_code -ne 0 -and
        $typedManifestReplay.output -match 'existing_release_manifest_invalid' -and
        $typedManifestReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $typedManifestReplay.output

    $duplicateKeyManifestId = ('10' * 20)
    $duplicateKeyInstall = Invoke-Installer -Archive $archiveA -ReleaseId $duplicateKeyManifestId
    $duplicateKeyManifestPath = Join-Path `
        (Join-Path $script:ReleaseRoot $duplicateKeyManifestId) `
        '.release.json'
    $duplicateKeyManifestText = [IO.File]::ReadAllText($duplicateKeyManifestPath, [Text.Encoding]::UTF8)
    $duplicateKeyManifestText = `
        '{"schema":"blackboard.order-worker-release.v1",' + $duplicateKeyManifestText.Substring(1)
    Write-TestUtf8 -Path $duplicateKeyManifestPath -Text $duplicateKeyManifestText
    $duplicateKeyReplay = Invoke-Installer -Archive $archiveA -ReleaseId $duplicateKeyManifestId
    Assert-True 'manifest with duplicate JSON key is rejected' (
        $duplicateKeyInstall.exit_code -eq 0 -and
        $duplicateKeyReplay.exit_code -ne 0 -and
        $duplicateKeyReplay.output -match 'existing_release_manifest_duplicate_key:schema' -and
        $duplicateKeyReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $duplicateKeyReplay.output

    $bomManifestId = ('12' * 20)
    $bomInstall = Invoke-Installer -Archive $archiveA -ReleaseId $bomManifestId
    $bomManifestPath = Join-Path (Join-Path $script:ReleaseRoot $bomManifestId) '.release.json'
    $bomOriginalBytes = [IO.File]::ReadAllBytes($bomManifestPath)
    $bomBytes = New-Object byte[] ($bomOriginalBytes.Length + 3)
    $bomBytes[0] = 0xEF
    $bomBytes[1] = 0xBB
    $bomBytes[2] = 0xBF
    [Array]::Copy($bomOriginalBytes, 0, $bomBytes, 3, $bomOriginalBytes.Length)
    [IO.File]::WriteAllBytes($bomManifestPath, $bomBytes)
    $bomReplay = Invoke-Installer -Archive $archiveA -ReleaseId $bomManifestId
    Assert-True 'manifest with UTF-8 BOM is rejected' (
        $bomInstall.exit_code -eq 0 -and
        $bomReplay.exit_code -ne 0 -and
        $bomReplay.output -match 'existing_release_manifest_utf8_bom_forbidden' -and
        $bomReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $bomReplay.output

    $invalidUtf8ManifestId = ('13' * 20)
    $invalidUtf8Install = Invoke-Installer -Archive $archiveA -ReleaseId $invalidUtf8ManifestId
    $invalidUtf8ManifestPath = Join-Path `
        (Join-Path $script:ReleaseRoot $invalidUtf8ManifestId) `
        '.release.json'
    $invalidUtf8Bytes = [IO.File]::ReadAllBytes($invalidUtf8ManifestPath)
    $invalidUtf8Bytes[5] = 0xFF
    [IO.File]::WriteAllBytes($invalidUtf8ManifestPath, $invalidUtf8Bytes)
    $invalidUtf8Replay = Invoke-Installer -Archive $archiveA -ReleaseId $invalidUtf8ManifestId
    Assert-True 'manifest with invalid UTF-8 is rejected' (
        $invalidUtf8Install.exit_code -eq 0 -and
        $invalidUtf8Replay.exit_code -ne 0 -and
        $invalidUtf8Replay.output -match 'existing_release_manifest_utf8_invalid' -and
        $invalidUtf8Replay.output -notmatch 'ALREADY_INSTALLED'
    ) $invalidUtf8Replay.output

    $nonExactTimestampId = ('14' * 20)
    $nonExactTimestampInstall = Invoke-Installer -Archive $archiveA -ReleaseId $nonExactTimestampId
    $nonExactTimestampPath = Join-Path `
        (Join-Path $script:ReleaseRoot $nonExactTimestampId) `
        '.release.json'
    $nonExactTimestampManifest = `
        [IO.File]::ReadAllText($nonExactTimestampPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $nonExactTimestampManifest.installed_at_utc = '2026-01-01T00:00:00Z'
    Write-TestUtf8 `
        -Path $nonExactTimestampPath `
        -Text ($nonExactTimestampManifest | ConvertTo-Json -Depth 5)
    $nonExactTimestampReplay = Invoke-Installer -Archive $archiveA -ReleaseId $nonExactTimestampId
    Assert-True 'manifest timestamp must use exact roundtrip format' (
        $nonExactTimestampInstall.exit_code -eq 0 -and
        $nonExactTimestampReplay.exit_code -ne 0 -and
        $nonExactTimestampReplay.output -match 'existing_release_manifest_timestamp_invalid' -and
        $nonExactTimestampReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $nonExactTimestampReplay.output

    $nonUtcTimestampId = ('15' * 20)
    $nonUtcTimestampInstall = Invoke-Installer -Archive $archiveA -ReleaseId $nonUtcTimestampId
    $nonUtcTimestampPath = Join-Path (Join-Path $script:ReleaseRoot $nonUtcTimestampId) '.release.json'
    $nonUtcTimestampManifest = `
        [IO.File]::ReadAllText($nonUtcTimestampPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $nonUtcTimestampManifest.installed_at_utc = '2026-01-01T00:00:00.0000000+01:00'
    Write-TestUtf8 `
        -Path $nonUtcTimestampPath `
        -Text ($nonUtcTimestampManifest | ConvertTo-Json -Depth 5)
    $nonUtcTimestampReplay = Invoke-Installer -Archive $archiveA -ReleaseId $nonUtcTimestampId
    Assert-True 'manifest timestamp must be UTC' (
        $nonUtcTimestampInstall.exit_code -eq 0 -and
        $nonUtcTimestampReplay.exit_code -ne 0 -and
        $nonUtcTimestampReplay.output -match 'existing_release_manifest_timestamp_invalid' -and
        $nonUtcTimestampReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $nonUtcTimestampReplay.output

    $zeroOffsetTimestampId = ('20' * 20)
    $zeroOffsetTimestampInstall = Invoke-Installer -Archive $archiveA -ReleaseId $zeroOffsetTimestampId
    $zeroOffsetTimestampPath = Join-Path `
        (Join-Path $script:ReleaseRoot $zeroOffsetTimestampId) `
        '.release.json'
    $zeroOffsetTimestampManifest = `
        [IO.File]::ReadAllText($zeroOffsetTimestampPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $zeroOffsetTimestampManifest.installed_at_utc = '2026-01-01T00:00:00.0000000+00:00'
    Write-TestUtf8 `
        -Path $zeroOffsetTimestampPath `
        -Text ($zeroOffsetTimestampManifest | ConvertTo-Json -Depth 5)
    $zeroOffsetTimestampReplay = Invoke-Installer -Archive $archiveA -ReleaseId $zeroOffsetTimestampId
    Assert-True 'manifest zero-offset timestamp must use canonical Z suffix' (
        $zeroOffsetTimestampInstall.exit_code -eq 0 -and
        $zeroOffsetTimestampReplay.exit_code -ne 0 -and
        $zeroOffsetTimestampReplay.output -match 'existing_release_manifest_timestamp_invalid' -and
        $zeroOffsetTimestampReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $zeroOffsetTimestampReplay.output

    $tamperedReleaseId = '7777777777777777777777777777777777777777'
    $tamperedInstall = Invoke-Installer -Archive $archiveA -ReleaseId $tamperedReleaseId
    $tamperedPath = Join-Path (Join-Path $script:ReleaseRoot $tamperedReleaseId) 'scripts\bus.ps1'
    Write-TestUtf8 -Path $tamperedPath -Text 'tampered'
    $tamperedReplay = Invoke-Installer -Archive $archiveA -ReleaseId $tamperedReleaseId
    Assert-True 'tampered existing release file is rejected' (
        $tamperedInstall.exit_code -eq 0 -and
        $tamperedReplay.exit_code -ne 0 -and
        $tamperedReplay.output -match 'existing_release_file_digest_mismatch:scripts\\bus\.ps1' -and
        $tamperedReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $tamperedReplay.output

    $collisionReleaseId = '8888888888888888888888888888888888888888'
    $collisionInstall = Invoke-Installer -Archive $archiveA -ReleaseId $collisionReleaseId
    $collisionReplay = Invoke-Installer -Archive $archiveB -ReleaseId $collisionReleaseId
    Assert-True 'same release id with different verified archive is rejected' (
        $collisionInstall.exit_code -eq 0 -and
        $collisionReplay.exit_code -ne 0 -and
        $collisionReplay.output -match 'existing_release_archive_collision' -and
        $collisionReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $collisionReplay.output

    $idempotentReleaseId = '9999999999999999999999999999999999999999'
    $idempotentInstall = Invoke-Installer -Archive $archiveA -ReleaseId $idempotentReleaseId
    $idempotentDestination = Join-Path $script:ReleaseRoot $idempotentReleaseId
    $idempotentManifestPath = Join-Path $idempotentDestination '.release.json'
    $manifestBeforeReplay = [Convert]::ToBase64String([IO.File]::ReadAllBytes($idempotentManifestPath))
    $idempotentReplay = Invoke-Installer -Archive $archiveA -ReleaseId $idempotentReleaseId
    $idempotentResult = Read-ResultJson -Text $idempotentReplay.output
    $manifestAfterReplay = [Convert]::ToBase64String([IO.File]::ReadAllBytes($idempotentManifestPath))
    Assert-True 'byte-identical replay returns already installed without mutation' (
        $idempotentInstall.exit_code -eq 0 -and
        $idempotentReplay.exit_code -eq 0 -and
        $idempotentResult -and
        $idempotentResult.status -ceq 'ALREADY_INSTALLED' -and
        $manifestAfterReplay -ceq $manifestBeforeReplay
    ) $idempotentReplay.output

    $extraExistingReleaseId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    $extraExistingInstall = Invoke-Installer -Archive $archiveA -ReleaseId $extraExistingReleaseId
    $extraExistingPath = Join-Path (Join-Path $script:ReleaseRoot $extraExistingReleaseId) 'rogue.txt'
    Write-TestUtf8 -Path $extraExistingPath -Text 'rogue'
    $extraExistingReplay = Invoke-Installer -Archive $archiveA -ReleaseId $extraExistingReleaseId
    Assert-True 'existing release with extra content is rejected' (
        $extraExistingInstall.exit_code -eq 0 -and
        $extraExistingReplay.exit_code -ne 0 -and
        $extraExistingReplay.output -match 'existing_release_files_extra:rogue\.txt' -and
        $extraExistingReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $extraExistingReplay.output

    $extraExistingDirectoryId = ('18' * 20)
    $extraExistingDirectoryInstall = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId $extraExistingDirectoryId
    $extraExistingDirectoryPath = Join-Path `
        (Join-Path $script:ReleaseRoot $extraExistingDirectoryId) `
        'rogue-empty-directory'
    New-Item -ItemType Directory -Path $extraExistingDirectoryPath | Out-Null
    $extraExistingDirectoryReplay = Invoke-Installer `
        -Archive $archiveA `
        -ReleaseId $extraExistingDirectoryId
    Assert-True 'existing release with extra empty directory is rejected' (
        $extraExistingDirectoryInstall.exit_code -eq 0 -and
        $extraExistingDirectoryReplay.exit_code -ne 0 -and
        $extraExistingDirectoryReplay.output -match 'existing_release_files_extra:rogue-empty-directory\\' -and
        $extraExistingDirectoryReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $extraExistingDirectoryReplay.output

    $missingExistingReleaseId = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    $missingExistingInstall = Invoke-Installer -Archive $archiveA -ReleaseId $missingExistingReleaseId
    $missingExistingPath = Join-Path `
        (Join-Path $script:ReleaseRoot $missingExistingReleaseId) `
        'scripts\invoke_order_claude.ps1'
    Remove-Item -LiteralPath $missingExistingPath -Force
    $missingExistingReplay = Invoke-Installer -Archive $archiveA -ReleaseId $missingExistingReleaseId
    Assert-True 'existing release with missing content is rejected' (
        $missingExistingInstall.exit_code -eq 0 -and
        $missingExistingReplay.exit_code -ne 0 -and
        $missingExistingReplay.output -match 'existing_release_files_missing:scripts\\invoke_order_claude\.ps1' -and
        $missingExistingReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $missingExistingReplay.output

    $reparseReleaseId = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
    $reparseInstall = Invoke-Installer -Archive $archiveA -ReleaseId $reparseReleaseId
    $reparseTarget = Join-Path $script:TestRoot 'reparse-target'
    New-Item -ItemType Directory -Path $reparseTarget | Out-Null
    Write-TestUtf8 -Path (Join-Path $reparseTarget 'must-not-be-traversed.txt') -Text 'outside release'
    $reparsePath = Join-Path (Join-Path $script:ReleaseRoot $reparseReleaseId) 'linked-directory'
    $null = New-TestDirectoryLink -Path $reparsePath -Target $reparseTarget
    $reparseReplay = Invoke-Installer -Archive $archiveA -ReleaseId $reparseReleaseId
    Assert-True 'existing release reparse directory is rejected before traversal' (
        $reparseInstall.exit_code -eq 0 -and
        $reparseReplay.exit_code -ne 0 -and
        $reparseReplay.output -match 'existing_release_reparse_point:linked-directory' -and
        $reparseReplay.output -notmatch 'must-not-be-traversed' -and
        $reparseReplay.output -notmatch 'ALREADY_INSTALLED'
    ) $reparseReplay.output

    # THE EXISTING RELEASE DIRECTORY *ITSELF* BEING A LINK.
    #
    # The case above links a directory INSIDE an installed release, which the
    # inventory walk catches at install_release_from_archive.ps1:183. The release root
    # and its ancestors are caught at :301. Between them sits a third, separate guard
    # at :163-166 - the existing destination itself carrying ReparsePoint - and until
    # now NOTHING reached it. codex proved that by disabling line 164 and watching the
    # suite stay green: three traversal assertions, two product guards, and a line
    # nobody's test depended on.
    #
    # That is the unproven-guard shape again, and it is the one I keep writing about
    # while leaving instances of it behind. It is a release-path traversal control: if
    # it stopped working, an attacker who can replace an installed release directory
    # with a link would have the installer inventory, hash and trust files that live
    # somewhere else entirely.
    #
    # The construction matters. The release must first install NORMALLY so the replay
    # takes the existing-release branch at all - a link where no release was ever
    # installed is a different path and proves nothing about this guard. So: install,
    # move the real directory aside, then put a link in its place pointing at what was
    # moved. The bytes are identical and every hash would match; only the reparse point
    # differs, which is precisely what the guard is for.
    # Every single-character id from 0 through f is already taken by a case above, and
    # reusing one silently replays into an existing release: the first install then
    # fails with existing_release_manifest_invalid and this assertion goes red while
    # the guard it targets is working perfectly. Mixed digits, so it collides with
    # nothing.
    $rootLinkReleaseId = '0123456789abcdef0123456789abcdef01234567'
    $rootLinkInstall = Invoke-Installer -Archive $archiveA -ReleaseId $rootLinkReleaseId
    $rootLinkDestination = Join-Path $script:ReleaseRoot $rootLinkReleaseId
    $rootLinkMovedAside = Join-Path $script:TestRoot 'root-link-moved-aside'
    Move-Item -LiteralPath $rootLinkDestination -Destination $rootLinkMovedAside
    # A marker that only exists via the link, so the assertion can show the installer
    # did not read THROUGH the reparse point before refusing.
    Write-TestUtf8 `
        -Path (Join-Path $rootLinkMovedAside 'must-not-be-traversed-through-root.txt') `
        -Text 'reached only by following the link'
    $null = New-TestDirectoryLink -Path $rootLinkDestination -Target $rootLinkMovedAside
    $rootLinkReplay = Invoke-Installer -Archive $archiveA -ReleaseId $rootLinkReleaseId
    Assert-True 'existing release root that is itself a link is rejected before traversal' (
        $rootLinkInstall.exit_code -eq 0 -and
        $rootLinkReplay.exit_code -ne 0 -and
        # ':.' is the root-item form. ':<relative path>' would be the entry guard at
        # :183, which the case above already covers - matching loosely here would let
        # this assertion pass on the wrong guard entirely.
        $rootLinkReplay.output -match 'existing_release_reparse_point:\.' -and
        $rootLinkReplay.output -notmatch 'must-not-be-traversed-through-root' -and
        $rootLinkReplay.output -notmatch 'ALREADY_INSTALLED'
    ) (
        # Name every component. A detail line carrying only the replay output cannot
        # say WHICH condition failed, and the first failure of this assertion showed
        # exactly the error it was looking for while still reporting red.
        'install_exit=' + $rootLinkInstall.exit_code +
        ' replay_exit=' + $rootLinkReplay.exit_code +
        ' install_out=' + ($rootLinkInstall.output -replace '\s+', ' ') +
        ' replay_out=' + ($rootLinkReplay.output -replace '\s+', ' ')
    )

    $remainingInstallerTempItems = @(
        Get-ChildItem -LiteralPath $script:InstallerTempRoot -Force -ErrorAction SilentlyContinue
    )
    Assert-True 'installer removes every suite-isolated temp expansion root' (
        $remainingInstallerTempItems.Count -eq 0
    ) (($remainingInstallerTempItems | ForEach-Object { $_.FullName }) -join ',')
}
finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $script:TestRoot -PathType Container)) {
        $resolvedTestRoot = [IO.Path]::GetFullPath($script:TestRoot)
        $testTempPrefix = $SystemTemp + [IO.Path]::DirectorySeparatorChar
        if (
            -not $resolvedTestRoot.StartsWith($testTempPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not ([IO.Path]::GetFileName($resolvedTestRoot) -match '^blackboard-order-release-installer-tests-[0-9a-f]{32}$')
        ) {
            throw 'test_cleanup_scope_invalid'
        }
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
