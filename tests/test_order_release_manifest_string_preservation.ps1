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
$script:TestRoot = Join-Path `
    $SystemTemp `
    ('blackboard-release-manifest-string-tests-' + [Guid]::NewGuid().ToString('N'))
$script:ReleaseRoot = Join-Path $script:TestRoot 'releases'
$script:InstallerTempRoot = Join-Path $script:TestRoot 'installer-temp'
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
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][bool] $Condition,
        [string] $Detail = ''
    )

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

function Get-BytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]] $Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Read-ResultJson {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string] $Text)

    $lines = @($Text -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -ne 1) { return $null }
    try { return $lines[0] | ConvertFrom-Json -ErrorAction Stop }
    catch { return $null }
}

function Invoke-Installer {
    param(
        [Parameter(Mandatory = $true)][string] $Payload,
        [Parameter(Mandatory = $true)][string] $ArchiveSha256,
        [Parameter(Mandatory = $true)][string] $ReleaseId
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $previousTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
    $previousTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
    $previousTmpDir = [Environment]::GetEnvironmentVariable('TMPDIR', 'Process')
    try {
        $ErrorActionPreference = 'Continue'
        [Environment]::SetEnvironmentVariable('TEMP', $script:InstallerTempRoot, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $script:InstallerTempRoot, 'Process')
        [Environment]::SetEnvironmentVariable('TMPDIR', $script:InstallerTempRoot, 'Process')
        $lines = @(
            & $script:ChildShell `
                -NoLogo `
                -NoProfile `
                -ExecutionPolicy Bypass `
                -File $InstallerPath `
                -Payload $Payload `
                -ArchiveSha256 $ArchiveSha256 `
                -ReleaseId $ReleaseId `
                -ReleaseRoot $script:ReleaseRoot 2>&1
        )
        $exitCode = $LASTEXITCODE
    }
    finally {
        [Environment]::SetEnvironmentVariable('TEMP', $previousTemp, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $previousTmp, 'Process')
        [Environment]::SetEnvironmentVariable('TMPDIR', $previousTmpDir, 'Process')
        $ErrorActionPreference = $previousErrorActionPreference
    }

    return [pscustomobject]@{
        exit_code = $exitCode
        output = ($lines | ForEach-Object { [string]$_ }) -join "`n"
    }
}

function Replace-InstalledAtUtcValue {
    param(
        [Parameter(Mandatory = $true)][string] $ManifestText,
        [Parameter(Mandatory = $true)][string] $JsonValue
    )

    $pattern = New-Object Text.RegularExpressions.Regex(
        '("installed_at_utc"\s*:\s*)"[^"\\]*"',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    $matches = $pattern.Matches($ManifestText)
    if ($matches.Count -ne 1) {
        throw ('test_manifest_timestamp_anchor_count:' + $matches.Count)
    }
    return $pattern.Replace($ManifestText, ('${1}' + $JsonValue), 1)
}

$childCommandName = if ($PSVersionTable.PSEdition -ceq 'Desktop') { 'powershell.exe' } else { 'pwsh' }
$childCommand = Get-Command $childCommandName -CommandType Application -ErrorAction Stop |
    Select-Object -First 1
$script:ChildShell = [string]$childCommand.Source
Write-Output (
    'CHILD_SHELL edition={0} version={1} path={2}' -f
    $PSVersionTable.PSEdition,
    $PSVersionTable.PSVersion,
    $script:ChildShell
)

try {
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
    New-Item -ItemType Directory -Path $script:InstallerTempRoot | Out-Null
    $sourceRoot = Join-Path $script:TestRoot 'source'
    New-Item -ItemType Directory -Path $sourceRoot | Out-Null
    foreach ($relativePath in $script:RequiredFiles) {
        Write-TestUtf8 `
            -Path (Join-Path $sourceRoot $relativePath) `
            -Text ('manifest-string-fixture|' + $relativePath)
    }

    $archivePath = Join-Path $script:TestRoot 'release.zip'
    Compress-Archive `
        -Path (Join-Path $sourceRoot 'scripts') `
        -DestinationPath $archivePath `
        -CompressionLevel Optimal
    $archiveBytes = [IO.File]::ReadAllBytes($archivePath)
    $payload = [Convert]::ToBase64String($archiveBytes)
    $archiveSha256 = Get-BytesSha256 -Bytes $archiveBytes
    $releaseId = ('ab' * 20)

    $install = Invoke-Installer `
        -Payload $payload `
        -ArchiveSha256 $archiveSha256 `
        -ReleaseId $releaseId
    $installResult = Read-ResultJson -Text $install.output
    Assert-True 'fresh install succeeds' (
        $install.exit_code -eq 0 -and
        $null -ne $installResult -and
        $installResult.status -ceq 'INSTALLED' -and
        $installResult.release_id -ceq $releaseId -and
        $installResult.archive_sha256 -ceq $archiveSha256
    ) $install.output

    $manifestPath = Join-Path (Join-Path $script:ReleaseRoot $releaseId) '.release.json'
    $canonicalManifestBytes = [IO.File]::ReadAllBytes($manifestPath)
    $canonicalManifestText = (New-Object Text.UTF8Encoding($false, $true)).GetString(
        $canonicalManifestBytes
    )
    $timestampMatches = [regex]::Matches(
        $canonicalManifestText,
        '"installed_at_utc"\s*:\s*"(?<value>[^"\\]*)"',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    Assert-True 'fresh manifest contains one canonical UTC timestamp string' (
        $timestampMatches.Count -eq 1 -and
        $timestampMatches[0].Groups['value'].Value -cmatch `
            '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}Z$'
    ) $canonicalManifestText

    $replay = Invoke-Installer `
        -Payload $payload `
        -ArchiveSha256 $archiveSha256 `
        -ReleaseId $releaseId
    $replayResult = Read-ResultJson -Text $replay.output
    $manifestAfterReplay = [IO.File]::ReadAllBytes($manifestPath)
    Assert-True 'canonical manifest replay succeeds without mutation' (
        $replay.exit_code -eq 0 -and
        $null -ne $replayResult -and
        $replayResult.status -ceq 'ALREADY_INSTALLED' -and
        $replayResult.release_id -ceq $releaseId -and
        $replayResult.archive_sha256 -ceq $archiveSha256 -and
        (Get-BytesSha256 -Bytes $manifestAfterReplay) -ceq `
            (Get-BytesSha256 -Bytes $canonicalManifestBytes) -and
        [Convert]::ToBase64String($manifestAfterReplay) -ceq `
            [Convert]::ToBase64String($canonicalManifestBytes)
    ) $replay.output

    $numericManifestText = Replace-InstalledAtUtcValue `
        -ManifestText $canonicalManifestText `
        -JsonValue '123'
    Write-TestUtf8 -Path $manifestPath -Text $numericManifestText
    $numericManifestBefore = [IO.File]::ReadAllBytes($manifestPath)
    $numericReplay = Invoke-Installer `
        -Payload $payload `
        -ArchiveSha256 $archiveSha256 `
        -ReleaseId $releaseId
    $numericManifestAfter = [IO.File]::ReadAllBytes($manifestPath)
    Assert-True 'numeric timestamp remains non-string and is rejected without mutation' (
        $numericReplay.exit_code -ne 0 -and
        $numericReplay.output -match 'existing_release_manifest_invalid' -and
        $numericReplay.output -notmatch 'ALREADY_INSTALLED' -and
        [Convert]::ToBase64String($numericManifestAfter) -ceq `
            [Convert]::ToBase64String($numericManifestBefore)
    ) $numericReplay.output

    $noncanonicalManifestText = Replace-InstalledAtUtcValue `
        -ManifestText $canonicalManifestText `
        -JsonValue '"2026-01-01T00:00:00.000Z"'
    Write-TestUtf8 -Path $manifestPath -Text $noncanonicalManifestText
    $noncanonicalManifestBefore = [IO.File]::ReadAllBytes($manifestPath)
    $noncanonicalReplay = Invoke-Installer `
        -Payload $payload `
        -ArchiveSha256 $archiveSha256 `
        -ReleaseId $releaseId
    $noncanonicalManifestAfter = [IO.File]::ReadAllBytes($manifestPath)
    Assert-True 'noncanonical timestamp string is rejected without mutation' (
        $noncanonicalReplay.exit_code -ne 0 -and
        $noncanonicalReplay.output -match 'existing_release_manifest_timestamp_invalid' -and
        $noncanonicalReplay.output -notmatch 'ALREADY_INSTALLED' -and
        [Convert]::ToBase64String($noncanonicalManifestAfter) -ceq `
        [Convert]::ToBase64String($noncanonicalManifestBefore)
    ) $noncanonicalReplay.output

    $truncatedManifestText = $canonicalManifestText.Substring(0, $canonicalManifestText.Length - 1)
    Write-TestUtf8 -Path $manifestPath -Text $truncatedManifestText
    $truncatedManifestBefore = [IO.File]::ReadAllBytes($manifestPath)
    $truncatedReplay = Invoke-Installer `
        -Payload $payload `
        -ArchiveSha256 $archiveSha256 `
        -ReleaseId $releaseId
    $truncatedManifestAfter = [IO.File]::ReadAllBytes($manifestPath)
    Assert-True 'malformed JSON manifest is rejected without mutation' (
        $truncatedReplay.exit_code -ne 0 -and
        $truncatedReplay.output -match 'existing_release_manifest_invalid' -and
        $truncatedReplay.output -notmatch 'ALREADY_INSTALLED' -and
        [Convert]::ToBase64String($truncatedManifestAfter) -ceq `
            [Convert]::ToBase64String($truncatedManifestBefore)
    ) $truncatedReplay.output

    $remainingInstallerTempItems = @(
        Get-ChildItem -LiteralPath $script:InstallerTempRoot -Force -ErrorAction SilentlyContinue
    )
    Assert-True 'installer removes every suite-isolated temporary expansion root' (
        $remainingInstallerTempItems.Count -eq 0
    ) (($remainingInstallerTempItems | ForEach-Object { $_.FullName }) -join ',')

    if ($script:Failed -eq 0) {
        Write-Output 'EXERCISED canonical_manifest_replay_string_preservation'
    }
}
finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $script:TestRoot -PathType Container)) {
        $resolvedTestRoot = [IO.Path]::GetFullPath($script:TestRoot)
        $expectedPrefix = $SystemTemp + [IO.Path]::DirectorySeparatorChar
        if (
            -not $resolvedTestRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not ([IO.Path]::GetFileName($resolvedTestRoot)).StartsWith(
                'blackboard-release-manifest-string-tests-',
                [StringComparison]::Ordinal
            )
        ) {
            throw ('test_cleanup_scope_invalid:' + $resolvedTestRoot)
        }
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}

Write-Output ("RESULT passed={0} failed={1}" -f $script:Passed, $script:Failed)
if ($script:Failed -ne 0) { exit 1 }
exit 0
