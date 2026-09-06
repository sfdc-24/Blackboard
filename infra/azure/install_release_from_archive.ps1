#Requires -Version 5.1
<#
.SYNOPSIS
    Installs an immutable ORDER-worker release delivered through Azure Run Command.

.DESCRIPTION
    The archive is supplied as a Base64 parameter so deployment does not depend
    on Git credentials inside the VM. The payload is digest-checked, expanded
    into a new versioned directory, and never overwrites an existing release.
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
    [ValidatePattern('^[0-9a-f]{7,40}$')]
    [string] $ReleaseId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$releaseRoot = 'C:\ProgramData\SFDC24\OrderSupervisor\releases'
$destination = Join-Path $releaseRoot $ReleaseId
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('blackboard-release-' + [Guid]::NewGuid().ToString('N'))
$archivePath = Join-Path $temporaryRoot 'release.zip'
$unpackPath = Join-Path $temporaryRoot 'unpacked'

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

if (Test-Path -LiteralPath $destination) {
    $manifestPath = Join-Path $destination '.release.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'existing_release_missing_manifest'
    }
    $existing = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($existing.release_id -cne $ReleaseId -or $existing.archive_sha256 -cne $ArchiveSha256) {
        throw 'existing_release_digest_mismatch'
    }
    [ordered]@{
        status = 'ALREADY_INSTALLED'
        release_id = $ReleaseId
        archive_sha256 = $ArchiveSha256
        destination = $destination
    } | ConvertTo-Json -Compress
    exit 0
}

try {
    New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $unpackPath -Force | Out-Null
    $bytes = [Convert]::FromBase64String($Payload)
    $actualSha256 = Get-BytesSha256 -Bytes $bytes
    if ($actualSha256 -cne $ArchiveSha256) { throw 'archive_digest_mismatch' }
    [IO.File]::WriteAllBytes($archivePath, $bytes)
    Expand-Archive -LiteralPath $archivePath -DestinationPath $unpackPath -Force

    $required = @(
        'scripts\OrderSupervisor.psm1',
        'scripts\bus.ps1',
        'scripts\install_order_supervisor.ps1',
        'scripts\invoke_order_claude.ps1',
        'scripts\order_supervisor.ps1',
        'scripts\order_supervisor_result.schema.json'
    )
    $missing = @($required | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $unpackPath $_) -PathType Leaf)
    })
    if ($missing.Count -gt 0) { throw ('release_files_missing:' + ($missing -join ',')) }

    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    Move-Item -LiteralPath $unpackPath -Destination $destination
    $manifest = [ordered]@{
        schema = 'blackboard.order-worker-release.v1'
        release_id = $ReleaseId
        archive_sha256 = $ArchiveSha256
        installed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json
    [IO.File]::WriteAllText(
        (Join-Path $destination '.release.json'),
        $manifest,
        (New-Object Text.UTF8Encoding($false))
    )

    [ordered]@{
        status = 'INSTALLED'
        release_id = $ReleaseId
        archive_sha256 = $ArchiveSha256
        destination = $destination
        required_file_count = $required.Count
    } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        $resolvedTemporary = [IO.Path]::GetFullPath($temporaryRoot)
        $resolvedSystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if (-not $resolvedTemporary.StartsWith($resolvedSystemTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'temporary_cleanup_scope_invalid'
        }
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
