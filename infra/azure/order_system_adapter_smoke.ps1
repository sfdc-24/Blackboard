#Requires -Version 5.1
<#
.SYNOPSIS
    Performs a provider-free SYSTEM smoke test of one immutable ORDER adapter release.

.DESCRIPTION
    This script is intended for Azure Managed Run Command after a candidate
    scheduled task has been installed in Observe mode. It does not parse or
    import the real provider environment file, invoke a provider, write to
    Blackboard, or modify the scheduled task. The production adapter is
    exercised with a generated local fake Claude executable and a synthetic,
    non-secret env file. The real env file is hashed for the protected
    before/after snapshot,
    but is never parsed, imported, or passed to a child. Exactly one bounded,
    secret-free JSON receipt is emitted after owned temporary artifacts have
    been cleaned up.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseId,
    [Parameter(Mandatory = $true)][string]$ArchiveSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedFileHashesBase64,
    [Parameter(Mandatory = $true)][string]$ReleaseRoot,
    [Parameter(Mandatory = $true)][string]$UserProfilePath,
    [Parameter(Mandatory = $true)][string]$WorkspacePath,
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$StatePath,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [Parameter(Mandatory = $true)][string]$ClaudePath,
    [Parameter(Mandatory = $true)][string]$ExpectedClaudeSha256,
    [Parameter(Mandatory = $true)][string]$GitPath,
    [Parameter(Mandatory = $true)][string]$ExpectedGitSha256,
    [Parameter(Mandatory = $true)][string]$TaskName,
    [ValidateRange(60, 1800)][int]$QuietWindowSeconds = 360,
    [ValidateRange(30, 300)][int]$ProcessTimeoutSeconds = 120
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RequiredReleaseFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$script:ManifestProperties = @('schema', 'release_id', 'archive_sha256', 'installed_at_utc', 'file_sha256')
$script:ManagedMarker = 'managed-by=install_order_supervisor.ps1; schema=v1'
$script:ExpectedClaudeVersion = '2.1.241 (Claude Code)'
$script:WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$script:EnvironmentBackups = @{}
$script:EnvironmentTouched = New-Object 'System.Collections.Generic.List[string]'
$script:TemporaryRoot = $null
$script:TemporaryParent = $null

function Throw-Smoke {
    param([Parameter(Mandatory = $true)][string]$Code)
    throw ('SMOKE:' + $Code)
}

function Assert-Smoke {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Code
    )
    if (-not $Condition) { Throw-Smoke -Code $Code }
}

function Test-SmokeReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)
    return ([int]$Item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0
}

function Resolve-SmokeLocalAbsolutePath {
    param(
        [AllowEmptyString()][string]$Value,
        [Parameter(Mandatory = $true)][string]$Code,
        [switch]$ForbidVolumeRoot
    )
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 1024 -or
        $Value -cmatch '["\x00-\x1F\x7F]' -or -not [IO.Path]::IsPathRooted($Value)) {
        Throw-Smoke -Code $Code
    }
    try { $full = [IO.Path]::GetFullPath($Value) }
    catch { Throw-Smoke -Code $Code }
    if ($full -cnotmatch '^[A-Za-z]:\\') { Throw-Smoke -Code $Code }
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) { $full = $full.TrimEnd('\') }
    if ($ForbidVolumeRoot -and [string]::Equals($full, $root, [StringComparison]::OrdinalIgnoreCase)) {
        Throw-Smoke -Code $Code
    }
    return $full
}

function Assert-SmokePathChainSafe {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Code
    )
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    $current = $root.TrimEnd('\')
    if ([string]::IsNullOrWhiteSpace($current)) { $current = $root }
    $tail = $full.Substring($root.Length)
    foreach ($segment in @($tail.Split(@('\'), [StringSplitOptions]::RemoveEmptyEntries))) {
        $current = if ($current.EndsWith('\')) { $current + $segment } else { $current + '\' + $segment }
        if (-not (Test-Path -LiteralPath $current)) { break }
        try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop }
        catch { Throw-Smoke -Code $Code }
        if (Test-SmokeReparsePoint -Item $item) { Throw-Smoke -Code $Code }
    }
}

function Assert-SmokeSafeDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )
    Assert-SmokePathChainSafe -Path $Path -Code $UnsafeCode
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { Throw-Smoke -Code $MissingCode }
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { Throw-Smoke -Code $MissingCode }
    if (-not $item.PSIsContainer -or (Test-SmokeReparsePoint -Item $item)) { Throw-Smoke -Code $UnsafeCode }
}

function Assert-SmokeSafeFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )
    Assert-SmokePathChainSafe -Path $Path -Code $UnsafeCode
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Throw-Smoke -Code $MissingCode }
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { Throw-Smoke -Code $MissingCode }
    if ($item.PSIsContainer -or (Test-SmokeReparsePoint -Item $item)) { Throw-Smoke -Code $UnsafeCode }
}

function Get-SmokeBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Get-SmokeTextSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    $encoding = New-Object Text.UTF8Encoding($false, $true)
    return Get-SmokeBytesSha256 -Bytes $encoding.GetBytes($Text)
}

function Get-SmokeFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = $null
    $sha = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $sha = [Security.Cryptography.SHA256]::Create()
        return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    } catch { Throw-Smoke -Code 'FILE_DIGEST_FAILED' }
    finally {
        if ($sha) { $sha.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Assert-SmokeNoDuplicateJsonKeys {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$Prefix
    )
    try { Add-Type -AssemblyName System.Runtime.Serialization -ErrorAction Stop }
    catch { Throw-Smoke -Code ($Prefix + '_JSON_INVALID') }
    $reader = $null
    try {
        $reader = [Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader(
            $Bytes,
            [Xml.XmlDictionaryReaderQuotas]::Max
        )
        $document = New-Object Xml.XmlDocument
        $document.XmlResolver = $null
        $document.Load($reader)
    } catch { Throw-Smoke -Code ($Prefix + '_JSON_INVALID') }
    finally { if ($reader) { $reader.Close() } }
    $pending = New-Object System.Collections.Queue
    $pending.Enqueue($document.DocumentElement)
    while ($pending.Count -gt 0) {
        $node = $pending.Dequeue()
        if ($node.NodeType -ne [Xml.XmlNodeType]::Element) { continue }
        if ($node.GetAttribute('type') -ceq 'object') {
            $keys = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
            foreach ($child in @($node.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })) {
                $key = if ($child.NamespaceURI -ceq 'item' -and $child.LocalName -ceq 'item') {
                    [string]$child.GetAttribute('item')
                } else { [string]$child.LocalName }
                if (-not $keys.Add($key)) { Throw-Smoke -Code ($Prefix + '_DUPLICATE_KEY') }
            }
        }
        foreach ($child in @($node.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })) {
            $pending.Enqueue($child)
        }
    }
}

function ConvertFrom-SmokeStrictJsonBytes {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$Prefix,
        [int]$MaximumBytes = 65536
    )
    if ($Bytes.Length -eq 0 -or $Bytes.Length -gt $MaximumBytes) { Throw-Smoke -Code ($Prefix + '_SIZE_INVALID') }
    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xef -and $Bytes[1] -eq 0xbb -and $Bytes[2] -eq 0xbf) {
        Throw-Smoke -Code ($Prefix + '_ENCODING_INVALID')
    }
    try {
        $decoder = New-Object Text.UTF8Encoding($false, $true)
        $text = $decoder.GetString($Bytes)
    } catch { Throw-Smoke -Code ($Prefix + '_ENCODING_INVALID') }
    Assert-SmokeNoDuplicateJsonKeys -Bytes $Bytes -Prefix $Prefix
    try { return $text | ConvertFrom-Json -ErrorAction Stop }
    catch { Throw-Smoke -Code ($Prefix + '_JSON_INVALID') }
}

function Read-SmokeStrictJsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Prefix,
        [int]$MaximumBytes = 65536
    )
    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($Path) }
    catch { Throw-Smoke -Code ($Prefix + '_READ_FAILED') }
    return ConvertFrom-SmokeStrictJsonBytes -Bytes $bytes -Prefix $Prefix -MaximumBytes $MaximumBytes
}

function Read-SmokeStrictJsonProcessFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Prefix,
        [int]$MaximumBytes = 65536
    )
    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($Path) }
    catch { Throw-Smoke -Code ($Prefix + '_READ_FAILED') }
    # Windows PowerShell 5.1's native redirection writes UTF-8 with a preamble
    # after the adapter sets Out-File:Encoding. Trust inputs remain BOMless;
    # captured process output permits exactly one leading UTF-8 preamble.
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xef -and $bytes[1] -eq 0xbb -and $bytes[2] -eq 0xbf) {
        $trimmed = New-Object byte[] ($bytes.Length - 3)
        [Array]::Copy($bytes, 3, $trimmed, 0, $trimmed.Length)
        $bytes = $trimmed
    }
    return ConvertFrom-SmokeStrictJsonBytes -Bytes $bytes -Prefix $Prefix -MaximumBytes $MaximumBytes
}

function Assert-SmokeExactProperties {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Code
    )
    if ($null -eq $Object -or $Object -is [System.Array] -or $Object -is [string]) { Throw-Smoke -Code $Code }
    $actual = @($Object.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($actual.Count -ne $Expected.Count) { Throw-Smoke -Code $Code }
    foreach ($name in $Expected) {
        if ($actual -cnotcontains $name) { Throw-Smoke -Code $Code }
    }
}

function Assert-SmokeUtcTimestamp {
    param([AllowEmptyString()][string]$Value)
    if ($Value -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}Z$') {
        Throw-Smoke -Code 'RELEASE_MANIFEST_TIMESTAMP_INVALID'
    }
    $parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParseExact(
        $Value,
        'o',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$parsed
    ) -or $parsed.Offset -ne [TimeSpan]::Zero) {
        Throw-Smoke -Code 'RELEASE_MANIFEST_TIMESTAMP_INVALID'
    }
}

function Read-SmokeExpectedHashes {
    param([AllowEmptyString()][string]$Base64)
    if ([string]::IsNullOrWhiteSpace($Base64) -or $Base64.Length -gt 32768) {
        Throw-Smoke -Code 'EXPECTED_HASHES_BASE64_INVALID'
    }
    try { [byte[]]$bytes = [Convert]::FromBase64String($Base64) }
    catch { Throw-Smoke -Code 'EXPECTED_HASHES_BASE64_INVALID' }
    $map = ConvertFrom-SmokeStrictJsonBytes -Bytes $bytes -Prefix 'EXPECTED_HASHES' -MaximumBytes 16384
    Assert-SmokeExactProperties -Object $map -Expected $script:RequiredReleaseFiles -Code 'EXPECTED_HASHES_SHAPE_INVALID'
    $result = New-Object 'System.Collections.Generic.List[object]'
    foreach ($path in $script:RequiredReleaseFiles) {
        $hash = $map.PSObject.Properties[$path].Value
        if ($hash -isnot [string] -or [string]$hash -cnotmatch '^[0-9a-f]{64}$') {
            Throw-Smoke -Code 'EXPECTED_HASHES_VALUE_INVALID'
        }
        $result.Add([pscustomobject][ordered]@{ path = $path; sha256 = [string]$hash })
    }
    return $result.ToArray()
}

function Get-SmokeVerifiedRelease {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$ExpectedArchive,
        [Parameter(Mandatory = $true)][object[]]$ExpectedHashes
    )
    Assert-SmokeSafeDirectory -Path $Root -MissingCode 'RELEASE_ROOT_MISSING' -UnsafeCode 'RELEASE_ROOT_UNSAFE'
    $releasePath = [IO.Path]::GetFullPath((Join-Path $Root $Id))
    if ([IO.Path]::GetDirectoryName($releasePath).TrimEnd('\') -cne $Root.TrimEnd('\')) {
        Throw-Smoke -Code 'RELEASE_PATH_OUTSIDE_ROOT'
    }
    Assert-SmokeSafeDirectory -Path $releasePath -MissingCode 'RELEASE_MISSING' -UnsafeCode 'RELEASE_UNSAFE'
    $scriptsPath = Join-Path $releasePath 'scripts'
    Assert-SmokeSafeDirectory -Path $scriptsPath -MissingCode 'RELEASE_SCRIPTS_MISSING' -UnsafeCode 'RELEASE_UNSAFE'
    try { $rootEntries = @(Get-ChildItem -LiteralPath $releasePath -Force -ErrorAction Stop) }
    catch { Throw-Smoke -Code 'RELEASE_INVENTORY_FAILED' }
    if ($rootEntries.Count -ne 2 -or
        @($rootEntries | Where-Object { $_.Name -ceq '.release.json' -and -not $_.PSIsContainer }).Count -ne 1 -or
        @($rootEntries | Where-Object { $_.Name -ceq 'scripts' -and $_.PSIsContainer }).Count -ne 1) {
        Throw-Smoke -Code 'RELEASE_INVENTORY_INVALID'
    }
    foreach ($entry in $rootEntries) {
        if (Test-SmokeReparsePoint -Item $entry) { Throw-Smoke -Code 'RELEASE_UNSAFE' }
    }
    try { $scriptEntries = @(Get-ChildItem -LiteralPath $scriptsPath -Force -ErrorAction Stop) }
    catch { Throw-Smoke -Code 'RELEASE_INVENTORY_FAILED' }
    if ($scriptEntries.Count -ne $script:RequiredReleaseFiles.Count -or
        @($scriptEntries | Where-Object { $_.PSIsContainer }).Count -ne 0) {
        Throw-Smoke -Code 'RELEASE_INVENTORY_INVALID'
    }
    $expectedLeafNames = @($script:RequiredReleaseFiles | ForEach-Object { Split-Path -Leaf $_ })
    foreach ($entry in $scriptEntries) {
        if ($expectedLeafNames -cnotcontains [string]$entry.Name -or (Test-SmokeReparsePoint -Item $entry)) {
            Throw-Smoke -Code 'RELEASE_INVENTORY_INVALID'
        }
    }
    $manifestPath = Join-Path $releasePath '.release.json'
    Assert-SmokeSafeFile -Path $manifestPath -MissingCode 'RELEASE_MANIFEST_MISSING' -UnsafeCode 'RELEASE_UNSAFE'
    $manifest = Read-SmokeStrictJsonFile -Path $manifestPath -Prefix 'RELEASE_MANIFEST' -MaximumBytes 65536
    Assert-SmokeExactProperties -Object $manifest -Expected $script:ManifestProperties -Code 'RELEASE_MANIFEST_SHAPE_INVALID'
    foreach ($property in @('schema', 'release_id', 'archive_sha256', 'installed_at_utc')) {
        if ($manifest.PSObject.Properties[$property].Value -isnot [string]) { Throw-Smoke -Code 'RELEASE_MANIFEST_TYPE_INVALID' }
    }
    if ([string]$manifest.schema -cne 'blackboard.order-worker-release.v1' -or
        [string]$manifest.release_id -cne $Id -or
        [string]$manifest.archive_sha256 -cne $ExpectedArchive) {
        Throw-Smoke -Code 'RELEASE_MANIFEST_VALUE_INVALID'
    }
    Assert-SmokeUtcTimestamp -Value ([string]$manifest.installed_at_utc)
    Assert-SmokeExactProperties -Object $manifest.file_sha256 -Expected $script:RequiredReleaseFiles -Code 'RELEASE_MANIFEST_HASHES_SHAPE_INVALID'
    $canonical = New-Object 'System.Collections.Generic.List[string]'
    foreach ($expected in $ExpectedHashes) {
        $relative = [string]$expected.path
        $full = [IO.Path]::GetFullPath((Join-Path $releasePath $relative))
        if (-not $full.StartsWith($releasePath.TrimEnd('\') + '\', [StringComparison]::Ordinal)) {
            Throw-Smoke -Code 'RELEASE_FILE_OUTSIDE_ROOT'
        }
        Assert-SmokeSafeFile -Path $full -MissingCode 'RELEASE_FILE_MISSING' -UnsafeCode 'RELEASE_UNSAFE'
        $manifestHash = $manifest.file_sha256.PSObject.Properties[$relative].Value
        if ($manifestHash -isnot [string] -or [string]$manifestHash -cnotmatch '^[0-9a-f]{64}$' -or
            [string]$manifestHash -cne [string]$expected.sha256) {
            Throw-Smoke -Code 'RELEASE_MANIFEST_HASH_MISMATCH'
        }
        $actual = Get-SmokeFileSha256 -Path $full
        if ($actual -cne [string]$expected.sha256) { Throw-Smoke -Code 'RELEASE_FILE_HASH_MISMATCH' }
        $canonical.Add($relative + '=' + $actual)
    }
    return [pscustomobject][ordered]@{
        path = $releasePath
        adapter_path = (Join-Path $releasePath 'scripts\invoke_order_claude.ps1')
        schema_path = (Join-Path $releasePath 'scripts\order_supervisor_result.schema.json')
        expected_hashes_sha256 = (Get-SmokeTextSha256 -Text ($canonical -join "`n"))
        manifest_sha256 = (Get-SmokeFileSha256 -Path $manifestPath)
    }
}

function Get-SmokeIdentitySid {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        return [string]$identity.User.Value
    } catch { Throw-Smoke -Code 'IDENTITY_QUERY_FAILED' }
}

function Get-SmokeTaskEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ExpectedReleasePath,
        [Parameter(Mandatory = $true)][string]$ExpectedUserProfile,
        [Parameter(Mandatory = $true)][string]$ExpectedWorkspace,
        [Parameter(Mandatory = $true)][string]$ExpectedEnv,
        [Parameter(Mandatory = $true)][string]$ExpectedState,
        [Parameter(Mandatory = $true)][string]$ExpectedLog,
        [Parameter(Mandatory = $true)][string]$ExpectedClaude,
        [Parameter(Mandatory = $true)][int]$MinimumQuietSeconds
    )
    try { $matches = @(Get-ScheduledTask -TaskName $Name -ErrorAction Stop) }
    catch { Throw-Smoke -Code 'TASK_QUERY_FAILED' }
    if ($matches.Count -ne 1) { Throw-Smoke -Code 'TASK_IDENTITY_AMBIGUOUS' }
    $task = $matches[0]
    if ([string]$task.TaskName -cne $Name -or [string]$task.TaskPath -cne '\') {
        Throw-Smoke -Code 'TASK_IDENTITY_INVALID'
    }
    if (-not ([string]$task.Description).Contains($script:ManagedMarker)) { Throw-Smoke -Code 'TASK_UNMANAGED' }
    if ([string]$task.State -cne 'Ready') { Throw-Smoke -Code 'TASK_NOT_READY' }
    $actions = @($task.Actions)
    if ($actions.Count -ne 1) { Throw-Smoke -Code 'TASK_ACTION_COUNT_INVALID' }
    $action = $actions[0]
    $expectedRunner = Join-Path $ExpectedReleasePath 'scripts\order_supervisor.ps1'
    $expectedArguments = @(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy Bypass',
        ('-File "' + $expectedRunner + '"'),
        '-Mode Observe',
        '-AllowedSourcesCsv "chat-mobile,codex"',
        ('-UserProfilePath "' + $ExpectedUserProfile + '"'),
        ('-WorkspacePath "' + $ExpectedWorkspace + '"'),
        ('-EnvFile "' + $ExpectedEnv + '"'),
        ('-StatePath "' + $ExpectedState + '"'),
        ('-LogPath "' + $ExpectedLog + '"'),
        '-WallTimeoutSeconds 720',
        ('-ClaudeCommand "' + $ExpectedClaude + '"')
    ) -join ' '
    if ([string]$action.Id -cne 'OrderSupervisor' -or
        [string]$action.Execute -cne $script:WindowsPowerShell -or
        [string]$action.WorkingDirectory -cne $ExpectedWorkspace -or
        [string]$action.Arguments -cne $expectedArguments) {
        Throw-Smoke -Code 'TASK_ACTION_CONTRACT_INVALID'
    }
    $principalId = [string]$task.Principal.UserId
    if (@('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -cnotcontains $principalId -or
        [string]$task.Principal.LogonType -cne 'ServiceAccount' -or
        [string]$task.Principal.RunLevel -cne 'Highest') {
        Throw-Smoke -Code 'TASK_PRINCIPAL_INVALID'
    }
    if ([string]$task.Settings.MultipleInstances -cne 'IgnoreNew') {
        Throw-Smoke -Code 'TASK_MULTIPLE_INSTANCES_INVALID'
    }
    try { $info = Get-ScheduledTaskInfo -TaskName $Name -TaskPath '\' -ErrorAction Stop }
    catch { Throw-Smoke -Code 'TASK_INFO_QUERY_FAILED' }
    if ([int64]$info.LastTaskResult -ne 0) { Throw-Smoke -Code 'TASK_LAST_RESULT_NONZERO' }
    try {
        $nextUtc = ([DateTime]$info.NextRunTime).ToUniversalTime()
        $lastUtc = ([DateTime]$info.LastRunTime).ToUniversalTime()
    } catch { Throw-Smoke -Code 'TASK_TIME_INVALID' }
    $quietSeconds = ($nextUtc - [DateTime]::UtcNow).TotalSeconds
    if ($quietSeconds -le $MinimumQuietSeconds) { Throw-Smoke -Code 'TASK_QUIET_WINDOW_UNAVAILABLE' }
    try { $xml = [string](Export-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop) }
    catch { Throw-Smoke -Code 'TASK_EXPORT_FAILED' }
    if ([string]::IsNullOrWhiteSpace($xml) -or $xml.Length -gt 1048576) { Throw-Smoke -Code 'TASK_EXPORT_INVALID' }
    $fingerprintSource = @(
        (Get-SmokeTextSha256 -Text $xml),
        [string]$task.State,
        ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', [int64]$info.LastTaskResult)),
        ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $lastUtc.Ticks)),
        ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $nextUtc.Ticks))
    ) -join '|'
    return [pscustomobject][ordered]@{
        fingerprint = (Get-SmokeTextSha256 -Text $fingerprintSource)
        quiet_seconds = [Math]::Floor($quietSeconds)
    }
}

function Get-SmokeFileFingerprint {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-SmokePathChainSafe -Path $Path -Code 'PROTECTED_PATH_UNSAFE'
    if (-not (Test-Path -LiteralPath $Path)) { return 'MISSING' }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or (Test-SmokeReparsePoint -Item $item)) { Throw-Smoke -Code 'PROTECTED_PATH_UNSAFE' }
    return Get-SmokeTextSha256 -Text (
        'F|{0}|{1}|{2}' -f $item.Length, $item.LastWriteTimeUtc.Ticks, (Get-SmokeFileSha256 -Path $Path)
    )
}

function Get-SmokeTreeFingerprint {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-SmokePathChainSafe -Path $Path -Code 'PROTECTED_PATH_UNSAFE'
    if (-not (Test-Path -LiteralPath $Path)) { return 'MISSING' }
    Assert-SmokeSafeDirectory -Path $Path -MissingCode 'PROTECTED_PATH_MISSING' -UnsafeCode 'PROTECTED_PATH_UNSAFE'
    $root = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $records = New-Object 'System.Collections.Generic.List[string]'
    try { $rootItem = Get-Item -LiteralPath $root -Force -ErrorAction Stop }
    catch { Throw-Smoke -Code 'PROTECTED_TREE_READ_FAILED' }
    $records.Add(('R|{0}' -f $rootItem.LastWriteTimeUtc.Ticks))
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($root)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        try { $entries = @([IO.Directory]::EnumerateFileSystemEntries($directory)) }
        catch { Throw-Smoke -Code 'PROTECTED_TREE_READ_FAILED' }
        foreach ($entryPath in $entries) {
            if ($records.Count -ge 50000) { Throw-Smoke -Code 'PROTECTED_TREE_TOO_LARGE' }
            try { $entry = Get-Item -LiteralPath $entryPath -Force -ErrorAction Stop }
            catch { Throw-Smoke -Code 'PROTECTED_TREE_READ_FAILED' }
            if (Test-SmokeReparsePoint -Item $entry) { Throw-Smoke -Code 'PROTECTED_PATH_UNSAFE' }
            $relative = $entry.FullName.Substring($root.Length)
            if ($relative.Length -gt 2048) { Throw-Smoke -Code 'PROTECTED_TREE_PATH_TOO_LONG' }
            if ($entry.PSIsContainer) {
                $records.Add(('D|{0}|{1}' -f $relative, $entry.LastWriteTimeUtc.Ticks))
                $pending.Push($entry.FullName)
            } else {
                $records.Add(('F|{0}|{1}|{2}|{3}' -f $relative, $entry.Length, $entry.LastWriteTimeUtc.Ticks, (Get-SmokeFileSha256 -Path $entry.FullName)))
            }
        }
    }
    $sorted = @($records.ToArray() | Sort-Object)
    return Get-SmokeTextSha256 -Text ($sorted -join "`n")
}

function Get-SmokeProtectedSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Profile,
        [Parameter(Mandatory = $true)][string]$Workspace,
        [Parameter(Mandatory = $true)][string]$EnvironmentFile,
        [Parameter(Mandatory = $true)][string]$StateFile,
        [Parameter(Mandatory = $true)][string]$LogFile,
        [Parameter(Mandatory = $true)][string]$ClaudeFile,
        [Parameter(Mandatory = $true)][string]$GitFile
    )
    $snapshot = [ordered]@{
        env_file = (Get-SmokeFileFingerprint -Path $EnvironmentFile)
        state_file = (Get-SmokeFileFingerprint -Path $StateFile)
        log_file = (Get-SmokeFileFingerprint -Path $LogFile)
        claude_file = (Get-SmokeFileFingerprint -Path $ClaudeFile)
        git_file = (Get-SmokeFileFingerprint -Path $GitFile)
        profile_claude = (Get-SmokeTreeFingerprint -Path (Join-Path $Profile '.claude'))
        profile_claude_json = (Get-SmokeFileFingerprint -Path (Join-Path $Profile '.claude.json'))
        workspace_claude = (Get-SmokeTreeFingerprint -Path (Join-Path $Workspace '.claude'))
        workspace_git_hooks = (Get-SmokeTreeFingerprint -Path (Join-Path $Workspace '.git\hooks'))
    }
    return Get-SmokeTextSha256 -Text (($snapshot | ConvertTo-Json -Compress))
}

function Set-SmokeEnvironmentVariable {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()]$Value
    )
    if (-not $script:EnvironmentBackups.ContainsKey($Name)) {
        $script:EnvironmentBackups[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
        $script:EnvironmentTouched.Add($Name)
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    $actual = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($null -eq $Value) {
        if ($null -ne $actual) { Throw-Smoke -Code 'ENVIRONMENT_SCRUB_FAILED' }
    } elseif ([string]$actual -cne [string]$Value) {
        Throw-Smoke -Code 'ENVIRONMENT_SET_FAILED'
    }
}

function Restore-SmokeEnvironment {
    $failed = $false
    for ($index = $script:EnvironmentTouched.Count - 1; $index -ge 0; $index--) {
        $name = $script:EnvironmentTouched[$index]
        $value = $script:EnvironmentBackups[$name]
        try {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
            $actual = [Environment]::GetEnvironmentVariable($name, 'Process')
            if ($null -eq $value) {
                if ($null -ne $actual) { $failed = $true }
            } elseif ([string]$actual -cne [string]$value) { $failed = $true }
        } catch { $failed = $true }
    }
    return (-not $failed)
}

function Assert-SmokeTreeContainsNoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Path)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($entryPath in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $attributes = [IO.File]::GetAttributes($entryPath)
            if (([int]$attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
                Throw-Smoke -Code 'TEMPORARY_ROOT_UNSAFE'
            }
            if (([int]$attributes -band [int][IO.FileAttributes]::Directory) -ne 0) { $pending.Push($entryPath) }
        }
    }
}

function Initialize-SmokeTemporaryRoot {
    $parent = Resolve-SmokeLocalAbsolutePath -Value ([IO.Path]::GetTempPath()) -Code 'TEMPORARY_PARENT_INVALID' -ForbidVolumeRoot
    Assert-SmokeSafeDirectory -Path $parent -MissingCode 'TEMPORARY_PARENT_MISSING' -UnsafeCode 'TEMPORARY_PARENT_UNSAFE'
    $name = 'blackboard-order-system-smoke-' + [Guid]::NewGuid().ToString('N')
    $path = [IO.Path]::GetFullPath((Join-Path $parent $name))
    if ([IO.Path]::GetDirectoryName($path).TrimEnd('\') -cne $parent.TrimEnd('\') -or
        [IO.Path]::GetFileName($path) -cnotmatch '^blackboard-order-system-smoke-[0-9a-f]{32}$' -or
        (Test-Path -LiteralPath $path)) {
        Throw-Smoke -Code 'TEMPORARY_ROOT_INVALID'
    }
    $script:TemporaryParent = $parent
    $script:TemporaryRoot = $path
    try { New-Item -ItemType Directory -Path $path -ErrorAction Stop | Out-Null }
    catch { Throw-Smoke -Code 'TEMPORARY_ROOT_CREATE_FAILED' }
    Assert-SmokeSafeDirectory -Path $path -MissingCode 'TEMPORARY_ROOT_CREATE_FAILED' -UnsafeCode 'TEMPORARY_ROOT_UNSAFE'
    return $path
}

function Remove-SmokeTemporaryRoot {
    if ([string]::IsNullOrWhiteSpace($script:TemporaryRoot)) { return $true }
    try {
        $full = [IO.Path]::GetFullPath($script:TemporaryRoot).TrimEnd('\')
        if ([IO.Path]::GetDirectoryName($full).TrimEnd('\') -cne $script:TemporaryParent.TrimEnd('\') -or
            [IO.Path]::GetFileName($full) -cnotmatch '^blackboard-order-system-smoke-[0-9a-f]{32}$') {
            return $false
        }
        if (Test-Path -LiteralPath $full) {
            Assert-SmokeSafeDirectory -Path $full -MissingCode 'TEMPORARY_ROOT_MISSING' -UnsafeCode 'TEMPORARY_ROOT_UNSAFE'
            Assert-SmokeTreeContainsNoReparsePoint -Path $full
            Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop
        }
        return (-not (Test-Path -LiteralPath $full))
    } catch { return $false }
}

function Write-SmokeUtf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false, $true)))
}

function Quote-SmokeProcessArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value -cmatch '["\x00-\x1F\x7F]') { Throw-Smoke -Code 'PROCESS_ARGUMENT_INVALID' }
    return '"' + $Value + '"'
}

function Invoke-SmokeBoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FailurePrefix
    )
    $stdoutPath = Join-Path $script:TemporaryRoot ($Label + '.stdout')
    $stderrPath = Join-Path $script:TemporaryRoot ($Label + '.stderr')
    $quoted = @($Arguments | ForEach-Object { Quote-SmokeProcessArgument -Value ([string]$_) })
    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList ($quoted -join ' ') `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru `
            -ErrorAction Stop
    } catch { Throw-Smoke -Code ($FailurePrefix + '_START_FAILED') }
    if (-not $process.WaitForExit($ProcessTimeoutSeconds * 1000)) {
        $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        try { & $taskkill /PID $process.Id /T /F 1>$null 2>$null } catch {}
        try { $null = $process.WaitForExit(15000) } catch {}
        Throw-Smoke -Code ($FailurePrefix + '_TIMEOUT')
    }
    $process.WaitForExit()
    $process.Refresh()
    foreach ($path in @($stdoutPath, $stderrPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path -Force).Length -gt 1048576) {
            Throw-Smoke -Code ($FailurePrefix + '_OUTPUT_INVALID')
        }
    }
    return [pscustomobject][ordered]@{
        exit_code = [int]$process.ExitCode
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
    }
}

function Read-SmokeBoundedUtf8Text {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$MaximumBytes,
        [Parameter(Mandatory = $true)][string]$Code
    )
    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($Path) }
    catch { Throw-Smoke -Code $Code }
    if ($bytes.Length -gt $MaximumBytes) { Throw-Smoke -Code $Code }
    try {
        $decoder = New-Object Text.UTF8Encoding($false, $true)
        return $decoder.GetString($bytes)
    } catch { Throw-Smoke -Code $Code }
}

$receipt = [ordered]@{
    schema = 'blackboard.order-system-adapter-smoke.v1'
    pass = $false
    release_id = $null
    identity_system = $false
    release_verified = $false
    release_unchanged = $false
    expected_hashes_sha256 = $null
    task_verified = $false
    task_unchanged = $false
    claude_version = $null
    claude_sha256 = $null
    claude_version_verified = $false
    git_sha256 = $null
    requested_tools = @()
    effective_tools = @()
    adapter_boundary_verified = $false
    settings_verified = $false
    environment_scrub_verified = $false
    structured_output_verified = $false
    harmless_git_verified = $false
    protected_fingerprints_unchanged = $false
    provider_inference_attempted = $false
    external_write_attempted = $false
    environment_restore_status = 'NOT_REQUIRED'
    cleanup_status = 'NOT_CREATED'
    failure_code = $null
    observed_at_utc = $null
}
$primaryCode = $null
$environmentRestoreSucceeded = $true
$cleanupSucceeded = $true

try {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or
        $PSVersionTable.PSVersion.Major -ne 5 -or
        $PSVersionTable.PSVersion.Minor -lt 1) {
        Throw-Smoke -Code 'WINDOWS_POWERSHELL_51_REQUIRED'
    }
    if ($ReleaseId -cnotmatch '^[0-9a-f]{40}$') { Throw-Smoke -Code 'RELEASE_ID_INVALID' }
    if ($ArchiveSha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Smoke -Code 'ARCHIVE_SHA256_INVALID' }
    if ($ExpectedClaudeSha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Smoke -Code 'CLAUDE_SHA256_INVALID' }
    if ($ExpectedGitSha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Smoke -Code 'GIT_SHA256_INVALID' }
    if ([string]::IsNullOrWhiteSpace($TaskName) -or $TaskName.Length -gt 238 -or
        $TaskName -cmatch '[\\/\x00-\x1F\x7F]') {
        Throw-Smoke -Code 'TASK_NAME_INVALID'
    }
    $receipt.release_id = $ReleaseId

    $identitySid = Get-SmokeIdentitySid
    if ($identitySid -cne 'S-1-5-18') { Throw-Smoke -Code 'NOT_SYSTEM' }
    $receipt.identity_system = $true

    $resolvedReleaseRoot = Resolve-SmokeLocalAbsolutePath -Value $ReleaseRoot -Code 'RELEASE_ROOT_INVALID' -ForbidVolumeRoot
    $resolvedProfile = Resolve-SmokeLocalAbsolutePath -Value $UserProfilePath -Code 'USER_PROFILE_INVALID' -ForbidVolumeRoot
    $resolvedWorkspace = Resolve-SmokeLocalAbsolutePath -Value $WorkspacePath -Code 'WORKSPACE_INVALID' -ForbidVolumeRoot
    $resolvedEnv = Resolve-SmokeLocalAbsolutePath -Value $EnvFile -Code 'ENV_FILE_INVALID' -ForbidVolumeRoot
    $resolvedState = Resolve-SmokeLocalAbsolutePath -Value $StatePath -Code 'STATE_PATH_INVALID' -ForbidVolumeRoot
    $resolvedLog = Resolve-SmokeLocalAbsolutePath -Value $LogPath -Code 'LOG_PATH_INVALID' -ForbidVolumeRoot
    $resolvedClaude = Resolve-SmokeLocalAbsolutePath -Value $ClaudePath -Code 'CLAUDE_PATH_INVALID' -ForbidVolumeRoot
    $resolvedGit = Resolve-SmokeLocalAbsolutePath -Value $GitPath -Code 'GIT_PATH_INVALID' -ForbidVolumeRoot

    Assert-SmokeSafeDirectory -Path $resolvedProfile -MissingCode 'USER_PROFILE_MISSING' -UnsafeCode 'USER_PROFILE_UNSAFE'
    Assert-SmokeSafeDirectory -Path $resolvedWorkspace -MissingCode 'WORKSPACE_MISSING' -UnsafeCode 'WORKSPACE_UNSAFE'
    Assert-SmokeSafeDirectory -Path (Join-Path $resolvedWorkspace '.git') -MissingCode 'WORKSPACE_GIT_MISSING' -UnsafeCode 'WORKSPACE_GIT_UNSAFE'
    Assert-SmokeSafeFile -Path $resolvedEnv -MissingCode 'ENV_FILE_MISSING' -UnsafeCode 'ENV_FILE_UNSAFE'
    Assert-SmokeSafeFile -Path $resolvedState -MissingCode 'STATE_FILE_MISSING' -UnsafeCode 'STATE_FILE_UNSAFE'
    Assert-SmokeSafeFile -Path $resolvedLog -MissingCode 'LOG_FILE_MISSING' -UnsafeCode 'LOG_FILE_UNSAFE'
    Assert-SmokeSafeFile -Path $resolvedClaude -MissingCode 'CLAUDE_FILE_MISSING' -UnsafeCode 'CLAUDE_FILE_UNSAFE'
    $actualClaudeSha256 = Get-SmokeFileSha256 -Path $resolvedClaude
    if ($actualClaudeSha256 -cne $ExpectedClaudeSha256) { Throw-Smoke -Code 'CLAUDE_SHA256_MISMATCH' }
    $receipt.claude_sha256 = $actualClaudeSha256
    Assert-SmokeSafeFile -Path $resolvedGit -MissingCode 'GIT_FILE_MISSING' -UnsafeCode 'GIT_FILE_UNSAFE'
    $actualGitSha256 = Get-SmokeFileSha256 -Path $resolvedGit
    if ($actualGitSha256 -cne $ExpectedGitSha256) { Throw-Smoke -Code 'GIT_SHA256_MISMATCH' }
    $receipt.git_sha256 = $actualGitSha256
    Assert-SmokeSafeFile -Path $script:WindowsPowerShell -MissingCode 'WINDOWS_POWERSHELL_MISSING' -UnsafeCode 'WINDOWS_POWERSHELL_UNSAFE'

    $expectedHashes = @(Read-SmokeExpectedHashes -Base64 $ExpectedFileHashesBase64)
    $release = Get-SmokeVerifiedRelease `
        -Root $resolvedReleaseRoot `
        -Id $ReleaseId `
        -ExpectedArchive $ArchiveSha256 `
        -ExpectedHashes $expectedHashes
    $receipt.release_verified = $true
    $receipt.expected_hashes_sha256 = [string]$release.expected_hashes_sha256

    $null = Initialize-SmokeTemporaryRoot
    $receipt.cleanup_status = 'PENDING'

    $taskBefore = Get-SmokeTaskEvidence `
        -Name $TaskName `
        -ExpectedReleasePath ([string]$release.path) `
        -ExpectedUserProfile $resolvedProfile `
        -ExpectedWorkspace $resolvedWorkspace `
        -ExpectedEnv $resolvedEnv `
        -ExpectedState $resolvedState `
        -ExpectedLog $resolvedLog `
        -ExpectedClaude $resolvedClaude `
        -MinimumQuietSeconds $QuietWindowSeconds
    $receipt.task_verified = $true
    $protectedBefore = Get-SmokeProtectedSnapshot `
        -Profile $resolvedProfile `
        -Workspace $resolvedWorkspace `
        -EnvironmentFile $resolvedEnv `
        -StateFile $resolvedState `
        -LogFile $resolvedLog `
        -ClaudeFile $resolvedClaude `
        -GitFile $resolvedGit

    $providerConflictNames = @(
        'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_CUSTOM_HEADERS',
        'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST',
        'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
        'CLAUDE_CODE_USE_MANTLE', 'CLAUDE_CODE_USE_ANTHROPIC_AWS',
        'ANTHROPIC_FOUNDRY_API_KEY', 'ANTHROPIC_FOUNDRY_AUTH_TOKEN',
        'ANTHROPIC_FOUNDRY_BASE_URL', 'ANTHROPIC_FOUNDRY_RESOURCE',
        'ANTHROPIC_BEDROCK_BASE_URL', 'ANTHROPIC_BEDROCK_MANTLE_BASE_URL',
        'ANTHROPIC_VERTEX_BASE_URL', 'ANTHROPIC_VERTEX_PROJECT_ID',
        'ANTHROPIC_AWS_BASE_URL', 'ANTHROPIC_AWS_WORKSPACE_ID'
    )
    $isolationNames = @(
        'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'CLAUDE_CONFIG_DIR',
        'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB', 'CLAUDE_CODE_USE_POWERSHELL_TOOL'
    ) + $providerConflictNames
    $claudeTrafficControlNames = @('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', 'DISABLE_AUTOUPDATER')
    $boardNames = @(
        [Environment]::GetEnvironmentVariables('Process').Keys |
            ForEach-Object { [string]$_ } |
            Where-Object { $_ -match '^(BUS|ALPHA|GLASSES|BLACKBOARD)_' }
    )
    $ambientGitNames = @(
        [Environment]::GetEnvironmentVariables('Process').Keys |
            ForEach-Object { [string]$_ } |
            Where-Object { $_ -match '^GIT_' }
    )
    foreach ($name in @($boardNames + $isolationNames + $claudeTrafficControlNames + $ambientGitNames)) {
        Set-SmokeEnvironmentVariable -Name $name -Value $null
    }
    $remainingBoard = @(
        [Environment]::GetEnvironmentVariables('Process').Keys |
            ForEach-Object { [string]$_ } |
            Where-Object { $_ -match '^(BUS|ALPHA|GLASSES|BLACKBOARD)_' }
    )
    $remainingConflicts = @($isolationNames | Where-Object {
        $null -ne [Environment]::GetEnvironmentVariable($_, 'Process')
    })
    if ($remainingBoard.Count -ne 0 -or $remainingConflicts.Count -ne 0) {
        Throw-Smoke -Code 'PARENT_ENVIRONMENT_SCRUB_FAILED'
    }

    $versionHome = Join-Path $script:TemporaryRoot 'version-home'
    $versionConfig = Join-Path $script:TemporaryRoot 'version-config'
    $processTemp = Join-Path $script:TemporaryRoot 'process-temp'
    $processAppData = Join-Path $script:TemporaryRoot 'appdata-roaming'
    $processLocalAppData = Join-Path $script:TemporaryRoot 'appdata-local'
    New-Item -ItemType Directory -Path $versionHome, $versionConfig, $processTemp, $processAppData, $processLocalAppData -ErrorAction Stop | Out-Null
    Assert-SmokeSafeDirectory -Path $versionHome -MissingCode 'VERSION_HOME_CREATE_FAILED' -UnsafeCode 'TEMPORARY_ROOT_UNSAFE'
    Assert-SmokeSafeDirectory -Path $versionConfig -MissingCode 'VERSION_CONFIG_CREATE_FAILED' -UnsafeCode 'TEMPORARY_ROOT_UNSAFE'
    $versionRoot = [IO.Path]::GetPathRoot($versionHome).TrimEnd('\')
    Set-SmokeEnvironmentVariable -Name 'USERPROFILE' -Value $versionHome
    Set-SmokeEnvironmentVariable -Name 'HOME' -Value $versionHome
    Set-SmokeEnvironmentVariable -Name 'HOMEDRIVE' -Value $versionRoot
    Set-SmokeEnvironmentVariable -Name 'HOMEPATH' -Value $versionHome.Substring($versionRoot.Length)
    Set-SmokeEnvironmentVariable -Name 'CLAUDE_CONFIG_DIR' -Value $versionConfig
    Set-SmokeEnvironmentVariable -Name 'TEMP' -Value $processTemp
    Set-SmokeEnvironmentVariable -Name 'TMP' -Value $processTemp
    Set-SmokeEnvironmentVariable -Name 'APPDATA' -Value $processAppData
    Set-SmokeEnvironmentVariable -Name 'LOCALAPPDATA' -Value $processLocalAppData
    foreach ($name in $claudeTrafficControlNames) { Set-SmokeEnvironmentVariable -Name $name -Value '1' }

    $versionProcess = Invoke-SmokeBoundedProcess `
        -FilePath $resolvedClaude `
        -Arguments @('--version') `
        -WorkingDirectory $script:TemporaryRoot `
        -Label 'claude-version' `
        -FailurePrefix 'CLAUDE_VERSION'
    $versionStdout = (Read-SmokeBoundedUtf8Text -Path $versionProcess.stdout_path -MaximumBytes 4096 -Code 'CLAUDE_VERSION_OUTPUT_INVALID').Trim()
    $versionStderr = Read-SmokeBoundedUtf8Text -Path $versionProcess.stderr_path -MaximumBytes 4096 -Code 'CLAUDE_VERSION_OUTPUT_INVALID'
    if ($versionProcess.exit_code -ne 0 -or $versionStdout -cne $script:ExpectedClaudeVersion -or $versionStderr.Length -ne 0) {
        Throw-Smoke -Code 'CLAUDE_VERSION_MISMATCH'
    }
    if (@([IO.Directory]::EnumerateFileSystemEntries($versionHome)).Count -ne 0 -or
        @([IO.Directory]::EnumerateFileSystemEntries($versionConfig)).Count -ne 0) {
        Throw-Smoke -Code 'CLAUDE_VERSION_ISOLATION_CHANGED'
    }
    $receipt.claude_version = '2.1.241'
    $receipt.claude_version_verified = $true

    $profileRoot = [IO.Path]::GetPathRoot($resolvedProfile).TrimEnd('\')
    Set-SmokeEnvironmentVariable -Name 'USERPROFILE' -Value $resolvedProfile
    Set-SmokeEnvironmentVariable -Name 'HOME' -Value $resolvedProfile
    Set-SmokeEnvironmentVariable -Name 'HOMEDRIVE' -Value $profileRoot
    Set-SmokeEnvironmentVariable -Name 'HOMEPATH' -Value $resolvedProfile.Substring($profileRoot.Length)
    Set-SmokeEnvironmentVariable -Name 'CLAUDE_CONFIG_DIR' -Value $null
    Set-SmokeEnvironmentVariable -Name 'GIT_OPTIONAL_LOCKS' -Value '0'
    Set-SmokeEnvironmentVariable -Name 'GIT_TERMINAL_PROMPT' -Value '0'
    Set-SmokeEnvironmentVariable -Name 'GIT_CONFIG_NOSYSTEM' -Value '1'
    Set-SmokeEnvironmentVariable -Name 'GIT_CONFIG_GLOBAL' -Value 'NUL'

    $gitProcess = Invoke-SmokeBoundedProcess `
        -FilePath $resolvedGit `
        -Arguments @('--no-optional-locks', '-c', ('safe.directory=' + $resolvedWorkspace), '-c', 'core.hooksPath=NUL', '-C', $resolvedWorkspace, 'rev-parse', '--is-inside-work-tree') `
        -WorkingDirectory $script:TemporaryRoot `
        -Label 'git-read-only' `
        -FailurePrefix 'GIT_READ_ONLY'
    $gitStdout = (Read-SmokeBoundedUtf8Text -Path $gitProcess.stdout_path -MaximumBytes 4096 -Code 'GIT_READ_ONLY_OUTPUT_INVALID').Trim()
    $gitStderr = Read-SmokeBoundedUtf8Text -Path $gitProcess.stderr_path -MaximumBytes 4096 -Code 'GIT_READ_ONLY_OUTPUT_INVALID'
    if ($gitProcess.exit_code -ne 0 -or $gitStdout -cne 'true' -or $gitStderr.Length -ne 0) {
        Throw-Smoke -Code 'GIT_READ_ONLY_FAILED'
    }
    $receipt.harmless_git_verified = $true

    $captureArgv = Join-Path $script:TemporaryRoot 'fake-argv.txt'
    $captureCwd = Join-Path $script:TemporaryRoot 'fake-cwd.txt'
    $captureEnvironment = Join-Path $script:TemporaryRoot 'fake-environment.txt'
    $captureSettingsPath = Join-Path $script:TemporaryRoot 'fake-settings-path.txt'
    $captureSettingsBytes = Join-Path $script:TemporaryRoot 'fake-settings.bin'
    $captureVersion = Join-Path $script:TemporaryRoot 'fake-version.txt'
    $captureStdin = Join-Path $script:TemporaryRoot 'fake-stdin.txt'
    $syntheticEnv = Join-Path $script:TemporaryRoot 'synthetic.env'
    $fakePrompt = Join-Path $script:TemporaryRoot 'fake-prompt.txt'
    $fakeStdout = Join-Path $script:TemporaryRoot 'fake-adapter.stdout'
    $fakeStderr = Join-Path $script:TemporaryRoot 'fake-adapter.stderr'
    $fakeExe = Join-Path $script:TemporaryRoot 'fake-claude.exe'
    $fakeApiKey = 'blackboard-smoke-placeholder-not-a-credential'
    $fakeModel = 'blackboard-smoke-model-placeholder'
    $fakePromptText = 'Provider-free ORDER adapter boundary probe.'
    Write-SmokeUtf8NoBom -Path $syntheticEnv -Text ("ANTHROPIC_API_KEY=$fakeApiKey`nANTHROPIC_MODEL=$fakeModel`n")
    Write-SmokeUtf8NoBom -Path $fakePrompt -Text $fakePromptText
    $captureMap = [ordered]@{
        ORDER_SMOKE_ARGV_PATH = $captureArgv
        ORDER_SMOKE_CWD_PATH = $captureCwd
        ORDER_SMOKE_ENV_PATH = $captureEnvironment
        ORDER_SMOKE_SETTINGS_PATH = $captureSettingsPath
        ORDER_SMOKE_SETTINGS_BYTES = $captureSettingsBytes
        ORDER_SMOKE_VERSION_PATH = $captureVersion
        ORDER_SMOKE_STDIN_PATH = $captureStdin
        ORDER_SMOKE_TEMP_ROOT = $script:TemporaryRoot
    }
    foreach ($name in $captureMap.Keys) { Set-SmokeEnvironmentVariable -Name $name -Value ([string]$captureMap[$name]) }

    $fakeSource = @"
using System;
using System.Collections;
using System.IO;
using System.Text;
public static class BlackboardOrderSystemSmokeFakeClaude {
    private static void WriteText(string envName, string text) {
        File.WriteAllText(Environment.GetEnvironmentVariable(envName), text, new UTF8Encoding(false));
    }
    public static int Main(string[] args) {
        if (args.Length == 1 && String.Equals(args[0], "--version", StringComparison.Ordinal)) {
            WriteText("ORDER_SMOKE_VERSION_PATH", "observed");
            Console.WriteLine("2.1.241 (Claude Code)");
            return 0;
        }
        string[] encoded = new string[args.Length];
        for (int i = 0; i < args.Length; i++) encoded[i] = Convert.ToBase64String(Encoding.UTF8.GetBytes(args[i]));
        File.WriteAllLines(Environment.GetEnvironmentVariable("ORDER_SMOKE_ARGV_PATH"), encoded, new UTF8Encoding(false));
        WriteText("ORDER_SMOKE_CWD_PATH", Environment.CurrentDirectory);
        int settingsCount = 0;
        string settingsPath = "";
        for (int i = 0; i < args.Length; i++) {
            if (String.Equals(args[i], "--settings", StringComparison.Ordinal)) {
                settingsCount++;
                if (i + 1 < args.Length) settingsPath = args[i + 1];
            }
        }
        if (settingsCount == 1 && File.Exists(settingsPath)) {
            WriteText("ORDER_SMOKE_SETTINGS_PATH", settingsPath);
            File.WriteAllBytes(Environment.GetEnvironmentVariable("ORDER_SMOKE_SETTINGS_BYTES"), File.ReadAllBytes(settingsPath));
        }
        bool boardAbsent = true;
        bool providerAbsent = true;
        string[] providerNames = new string[] {
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
            "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
            "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
            "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
            "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_AWS_WORKSPACE_ID"
        };
        foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables()) {
            string name = Convert.ToString(entry.Key);
            if (name.StartsWith("BUS_", StringComparison.OrdinalIgnoreCase) ||
                name.StartsWith("ALPHA_", StringComparison.OrdinalIgnoreCase) ||
                name.StartsWith("GLASSES_", StringComparison.OrdinalIgnoreCase) ||
                name.StartsWith("BLACKBOARD_", StringComparison.OrdinalIgnoreCase)) boardAbsent = false;
        }
        foreach (string name in providerNames) {
            if (!String.IsNullOrEmpty(Environment.GetEnvironmentVariable(name))) providerAbsent = false;
        }
        string config = Environment.GetEnvironmentVariable("CLAUDE_CONFIG_DIR") ?? "";
        string expectedRoot = Environment.GetEnvironmentVariable("ORDER_SMOKE_TEMP_ROOT") ?? "";
        bool configIsolated = config.StartsWith(expectedRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) &&
            Path.GetFileName(config).StartsWith("claude-config-", StringComparison.Ordinal);
        bool processDirectoriesIsolated = true;
        foreach (string name in new string[] { "TEMP", "TMP", "APPDATA", "LOCALAPPDATA" }) {
            string value = Environment.GetEnvironmentVariable(name) ?? "";
            if (!value.StartsWith(expectedRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) processDirectoriesIsolated = false;
        }
        File.WriteAllLines(Environment.GetEnvironmentVariable("ORDER_SMOKE_ENV_PATH"), new string[] {
            String.Equals(Environment.GetEnvironmentVariable("ANTHROPIC_API_KEY"), "$fakeApiKey", StringComparison.Ordinal).ToString(),
            String.Equals(Environment.GetEnvironmentVariable("ANTHROPIC_MODEL"), "$fakeModel", StringComparison.Ordinal).ToString(),
            String.Equals(Environment.GetEnvironmentVariable("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"), "1", StringComparison.Ordinal).ToString(),
            String.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLAUDE_CODE_USE_POWERSHELL_TOOL")).ToString(),
            boardAbsent.ToString(), providerAbsent.ToString(), configIsolated.ToString(),
            String.Equals(Environment.GetEnvironmentVariable("GIT_OPTIONAL_LOCKS"), "0", StringComparison.Ordinal).ToString(),
            String.Equals(Environment.GetEnvironmentVariable("GIT_TERMINAL_PROMPT"), "0", StringComparison.Ordinal).ToString(),
            String.Equals(Environment.GetEnvironmentVariable("GIT_CONFIG_NOSYSTEM"), "1", StringComparison.Ordinal).ToString(),
            String.Equals(Environment.GetEnvironmentVariable("GIT_CONFIG_GLOBAL"), "NUL", StringComparison.Ordinal).ToString(),
            processDirectoriesIsolated.ToString()
        }, new UTF8Encoding(false));
        string stdin = Console.In.ReadToEnd();
        WriteText("ORDER_SMOKE_STDIN_PATH", stdin);
        Console.WriteLine("{\"structured_output\":{\"schema\":\"order_supervisor_result.v1\",\"work_id\":\"SYSTEM-SMOKE\",\"status\":\"blocked\",\"summary\":\"provider-free fake adapter probe\",\"evidence\":[],\"error_code\":\"ASK_USER_QUESTION_DISABLED\"},\"num_turns\":1,\"is_error\":false}");
        return 0;
    }
}
"@
    try {
        Add-Type -TypeDefinition $fakeSource -OutputAssembly $fakeExe -OutputType ConsoleApplication -ErrorAction Stop | Out-Null
    } catch { Throw-Smoke -Code 'FAKE_CLAUDE_BUILD_FAILED' }
    Assert-SmokeSafeFile -Path $fakeExe -MissingCode 'FAKE_CLAUDE_BUILD_FAILED' -UnsafeCode 'TEMPORARY_ROOT_UNSAFE'

    $adapterArguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', [string]$release.adapter_path,
        '-PromptPath', $fakePrompt,
        '-SchemaPath', [string]$release.schema_path,
        '-StdoutPath', $fakeStdout,
        '-StderrPath', $fakeStderr,
        '-EnvFile', $syntheticEnv,
        '-WorkspacePath', $resolvedWorkspace,
        '-ClaudeCommand', $fakeExe,
        '-MaxBudgetUsd', '2.00'
    )
    $adapterProcess = Invoke-SmokeBoundedProcess `
        -FilePath $script:WindowsPowerShell `
        -Arguments $adapterArguments `
        -WorkingDirectory $resolvedWorkspace `
        -Label 'adapter-host' `
        -FailurePrefix 'FAKE_ADAPTER'
    if ($adapterProcess.exit_code -ne 0) { Throw-Smoke -Code 'FAKE_ADAPTER_EXIT_NONZERO' }
    if ((Get-Item -LiteralPath $adapterProcess.stdout_path -Force).Length -ne 0 -or
        (Get-Item -LiteralPath $adapterProcess.stderr_path -Force).Length -ne 0 -or
        -not (Test-Path -LiteralPath $fakeStderr -PathType Leaf) -or
        (Get-Item -LiteralPath $fakeStderr -Force).Length -ne 0) {
        Throw-Smoke -Code 'FAKE_ADAPTER_DIAGNOSTIC_OUTPUT_PRESENT'
    }
    foreach ($path in @($captureArgv, $captureCwd, $captureEnvironment, $captureSettingsPath, $captureSettingsBytes, $captureVersion, $captureStdin, $fakeStdout)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Throw-Smoke -Code 'FAKE_CAPTURE_MISSING' }
    }
    if ((Read-SmokeBoundedUtf8Text -Path $captureVersion -MaximumBytes 64 -Code 'FAKE_VERSION_CAPTURE_INVALID') -cne 'observed') {
        Throw-Smoke -Code 'FAKE_VERSION_PROBE_MISSING'
    }
    $fakeStdin = Read-SmokeBoundedUtf8Text -Path $captureStdin -MaximumBytes 4096 -Code 'FAKE_STDIN_CAPTURE_INVALID'
    if ($fakeStdin -cne ($fakePromptText + [Environment]::NewLine)) { Throw-Smoke -Code 'FAKE_STDIN_MISMATCH' }
    $argvLines = @([IO.File]::ReadAllLines($captureArgv, (New-Object Text.UTF8Encoding($false, $true))))
    $actualArgv = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in $argvLines) {
        try { $actualArgv.Add([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line))) }
        catch { Throw-Smoke -Code 'FAKE_ARGV_CAPTURE_INVALID' }
    }
    $schemaObject = Read-SmokeStrictJsonFile -Path ([string]$release.schema_path) -Prefix 'RESULT_SCHEMA' -MaximumBytes 65536
    $compactSchema = $schemaObject | ConvertTo-Json -Depth 12 -Compress
    $capturedSettings = Read-SmokeBoundedUtf8Text -Path $captureSettingsPath -MaximumBytes 2048 -Code 'FAKE_SETTINGS_PATH_INVALID'
    $settingsFull = Resolve-SmokeLocalAbsolutePath -Value $capturedSettings -Code 'FAKE_SETTINGS_PATH_INVALID' -ForbidVolumeRoot
    if ([IO.Path]::GetDirectoryName($settingsFull).TrimEnd('\') -cne $script:TemporaryRoot.TrimEnd('\') -or
        [IO.Path]::GetFileName($settingsFull) -cnotmatch '^claude-settings-[0-9a-f]{32}\.json$' -or
        (Test-Path -LiteralPath $settingsFull)) {
        Throw-Smoke -Code 'FAKE_SETTINGS_LIFECYCLE_INVALID'
    }
    $expectedArgv = @(
        '--print', '--output-format', 'json', '--json-schema', $compactSchema,
        '--settings', $settingsFull, '--tools', 'Read,Edit,PowerShell',
        '--strict-mcp-config', '--permission-mode', 'manual',
        '--disallowedTools', 'Bash', 'AskUserQuestion', '--max-budget-usd', '2.00',
        '--bare', '--no-session-persistence', '--disable-slash-commands'
    )
    if ($actualArgv.Count -ne $expectedArgv.Count) { Throw-Smoke -Code 'FAKE_ARGV_COUNT_INVALID' }
    for ($index = 0; $index -lt $expectedArgv.Count; $index++) {
        if ([string]$actualArgv[$index] -cne [string]$expectedArgv[$index]) { Throw-Smoke -Code 'FAKE_ARGV_MISMATCH' }
    }
    $receipt.requested_tools = @('Read', 'Edit', 'PowerShell')

    [byte[]]$settingsBytes = [IO.File]::ReadAllBytes($captureSettingsBytes)
    $settings = ConvertFrom-SmokeStrictJsonBytes -Bytes $settingsBytes -Prefix 'FAKE_SETTINGS' -MaximumBytes 16384
    Assert-SmokeExactProperties -Object $settings -Expected @('env', 'permissions') -Code 'FAKE_SETTINGS_SHAPE_INVALID'
    Assert-SmokeExactProperties -Object $settings.env -Expected @('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB', 'CLAUDE_CODE_USE_POWERSHELL_TOOL') -Code 'FAKE_SETTINGS_ENV_INVALID'
    Assert-SmokeExactProperties -Object $settings.permissions -Expected @('allow', 'deny') -Code 'FAKE_SETTINGS_PERMISSIONS_INVALID'
    if ($settings.env.CLAUDE_CODE_SUBPROCESS_ENV_SCRUB -isnot [string] -or
        [string]$settings.env.CLAUDE_CODE_SUBPROCESS_ENV_SCRUB -cne '1' -or
        $settings.env.CLAUDE_CODE_USE_POWERSHELL_TOOL -isnot [string] -or
        [string]$settings.env.CLAUDE_CODE_USE_POWERSHELL_TOOL -cne '1') {
        Throw-Smoke -Code 'FAKE_SETTINGS_ENV_INVALID'
    }
    $allow = @($settings.permissions.allow)
    if ($allow.Count -ne 3 -or [string]$allow[0] -cne 'Read' -or [string]$allow[1] -cne 'Edit' -or [string]$allow[2] -cne 'PowerShell') {
        Throw-Smoke -Code 'FAKE_SETTINGS_ALLOW_INVALID'
    }
    $permissionPath = ($syntheticEnv -replace '\\', '/')
    if ($permissionPath -cnotmatch '^(?<drive>[A-Za-z]):/(?<tail>.*)$') { Throw-Smoke -Code 'FAKE_SETTINGS_DENY_INVALID' }
    $permissionPath = '//' + $matches['drive'].ToLowerInvariant() + '/' + $matches['tail'].TrimEnd('/')
    $deny = @($settings.permissions.deny)
    if ($deny.Count -ne 2 -or [string]$deny[0] -cne ('Read(' + $permissionPath + ')') -or
        [string]$deny[1] -cne ('Edit(' + $permissionPath + ')')) {
        Throw-Smoke -Code 'FAKE_SETTINGS_DENY_INVALID'
    }
    $settingsText = (New-Object Text.UTF8Encoding($false, $true)).GetString($settingsBytes)
    if ($settingsText.Contains($fakeApiKey) -or $settingsText.Contains($fakeModel) -or
        $settingsText.IndexOf('ANTHROPIC_API_KEY', [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $settingsText.IndexOf('ANTHROPIC_MODEL', [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        Throw-Smoke -Code 'FAKE_SETTINGS_SECRET_MATERIAL_PRESENT'
    }
    $receipt.settings_verified = $true

    $fakeCwd = Read-SmokeBoundedUtf8Text -Path $captureCwd -MaximumBytes 2048 -Code 'FAKE_CWD_INVALID'
    if ([IO.Path]::GetFullPath($fakeCwd) -cne $resolvedWorkspace) { Throw-Smoke -Code 'FAKE_CWD_INVALID' }
    $fakeEnvironment = @([IO.File]::ReadAllLines($captureEnvironment, (New-Object Text.UTF8Encoding($false, $true))))
    if ($fakeEnvironment.Count -ne 12 -or @($fakeEnvironment | Where-Object { $_ -cne 'True' }).Count -ne 0) {
        Throw-Smoke -Code 'FAKE_ENVIRONMENT_BOUNDARY_INVALID'
    }
    foreach ($name in $isolationNames) {
        if ($null -ne [Environment]::GetEnvironmentVariable($name, 'Process')) {
            Throw-Smoke -Code 'PARENT_ENVIRONMENT_CHANGED_BY_ADAPTER'
        }
    }
    if (@(Get-ChildItem -LiteralPath $script:TemporaryRoot -Force -ErrorAction Stop | Where-Object {
        $_.Name -cmatch '^claude-(config-[0-9a-f]{32}|settings-[0-9a-f]{32}\.json)$'
    }).Count -ne 0) {
        Throw-Smoke -Code 'FAKE_ADAPTER_ISOLATION_CLEANUP_FAILED'
    }
    $receipt.environment_scrub_verified = $true

    $outer = Read-SmokeStrictJsonProcessFile -Path $fakeStdout -Prefix 'FAKE_OUTPUT' -MaximumBytes 65536
    Assert-SmokeExactProperties -Object $outer -Expected @('structured_output', 'num_turns', 'is_error') -Code 'FAKE_OUTPUT_SHAPE_INVALID'
    Assert-SmokeExactProperties -Object $outer.structured_output -Expected @('schema', 'work_id', 'status', 'summary', 'evidence', 'error_code') -Code 'FAKE_STRUCTURED_OUTPUT_SHAPE_INVALID'
    if ([string]$outer.structured_output.schema -cne 'order_supervisor_result.v1' -or
        [string]$outer.structured_output.work_id -cne 'SYSTEM-SMOKE' -or
        [string]$outer.structured_output.status -cne 'blocked' -or
        [string]$outer.structured_output.error_code -cne 'ASK_USER_QUESTION_DISABLED' -or
        @($outer.structured_output.evidence).Count -ne 0 -or
        [int]$outer.num_turns -ne 1 -or [bool]$outer.is_error) {
        Throw-Smoke -Code 'FAKE_STRUCTURED_OUTPUT_INVALID'
    }
    $receipt.structured_output_verified = $true
    $receipt.effective_tools = @('Read', 'Edit', 'PowerShell', 'StructuredOutput')
    $receipt.adapter_boundary_verified = $true

    $protectedAfter = Get-SmokeProtectedSnapshot `
        -Profile $resolvedProfile `
        -Workspace $resolvedWorkspace `
        -EnvironmentFile $resolvedEnv `
        -StateFile $resolvedState `
        -LogFile $resolvedLog `
        -ClaudeFile $resolvedClaude `
        -GitFile $resolvedGit
    if ($protectedAfter -cne $protectedBefore) { Throw-Smoke -Code 'PROTECTED_FINGERPRINT_CHANGED' }
    $receipt.protected_fingerprints_unchanged = $true
    $taskAfter = Get-SmokeTaskEvidence `
        -Name $TaskName `
        -ExpectedReleasePath ([string]$release.path) `
        -ExpectedUserProfile $resolvedProfile `
        -ExpectedWorkspace $resolvedWorkspace `
        -ExpectedEnv $resolvedEnv `
        -ExpectedState $resolvedState `
        -ExpectedLog $resolvedLog `
        -ExpectedClaude $resolvedClaude `
        -MinimumQuietSeconds $QuietWindowSeconds
    if ([string]$taskAfter.fingerprint -cne [string]$taskBefore.fingerprint) { Throw-Smoke -Code 'TASK_CHANGED_DURING_SMOKE' }
    $receipt.task_unchanged = $true
    $releaseAfter = Get-SmokeVerifiedRelease `
        -Root $resolvedReleaseRoot `
        -Id $ReleaseId `
        -ExpectedArchive $ArchiveSha256 `
        -ExpectedHashes $expectedHashes
    if ([string]$releaseAfter.path -cne [string]$release.path -or
        [string]$releaseAfter.adapter_path -cne [string]$release.adapter_path -or
        [string]$releaseAfter.schema_path -cne [string]$release.schema_path -or
        [string]$releaseAfter.expected_hashes_sha256 -cne [string]$release.expected_hashes_sha256 -or
        [string]$releaseAfter.manifest_sha256 -cne [string]$release.manifest_sha256) {
        Throw-Smoke -Code 'RELEASE_CHANGED_DURING_SMOKE'
    }
    $receipt.release_unchanged = $true
} catch {
    $message = [string]$_.Exception.Message
    if ($message -cmatch '^SMOKE:(?<code>[A-Z0-9_]{1,80})$') { $primaryCode = [string]$matches['code'] }
    else { $primaryCode = 'UNEXPECTED_SMOKE_FAILURE' }
} finally {
    if ($script:EnvironmentTouched.Count -gt 0) {
        $environmentRestoreSucceeded = Restore-SmokeEnvironment
        $receipt.environment_restore_status = if ($environmentRestoreSucceeded) { 'SUCCEEDED' } else { 'FAILED' }
    }
    if (-not [string]::IsNullOrWhiteSpace($script:TemporaryRoot)) {
        $cleanupSucceeded = Remove-SmokeTemporaryRoot
        $receipt.cleanup_status = if ($cleanupSucceeded) { 'SUCCEEDED' } else { 'FAILED' }
    }
}

if ($null -eq $primaryCode -and -not $environmentRestoreSucceeded) { $primaryCode = 'ENVIRONMENT_RESTORE_FAILED' }
if ($null -eq $primaryCode -and -not $cleanupSucceeded) { $primaryCode = 'TEMPORARY_CLEANUP_FAILED' }
$receipt.failure_code = $primaryCode
$receipt.pass = ($null -eq $primaryCode)
$receipt.observed_at_utc = [DateTime]::UtcNow.ToString(
    'yyyy-MM-ddTHH:mm:ss.fffffffZ',
    [Globalization.CultureInfo]::InvariantCulture
)
$receiptJson = $receipt | ConvertTo-Json -Depth 8 -Compress
$receiptByteCount = [Text.Encoding]::UTF8.GetByteCount($receiptJson)
if ($receiptByteCount -gt 3072) {
    $receiptJson = '{"schema":"blackboard.order-system-adapter-smoke.v1","pass":false,"failure_code":"RECEIPT_TOO_LARGE"}'
    $primaryCode = 'RECEIPT_TOO_LARGE'
}
Write-Output $receiptJson
if ($null -eq $primaryCode) { exit 0 }
exit 1
