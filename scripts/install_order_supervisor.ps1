#Requires -Version 5.1
<#
Install, inspect, uninstall, or roll back the SYSTEM scheduled task for the
one-shot Blackboard ORDER worker. Status is the non-mutating default. The
incident-only InstallFromDisabledNoStop action replaces a disabled definition
without calling any task stop, start, unregister, or automatic rollback path.

Rollback restores only the prior Task Scheduler definition. It does not copy or
delete runner files, so the referenced prior immutable runner must still exist.
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'InstallFromDisabledNoStop', 'Status', 'Uninstall', 'Rollback')][string]$Action = 'Status',
    [ValidateSet('Observe', 'Execute')][string]$Mode = 'Observe',
    [string]$UserProfilePath = 'C:\Users\akatiawam',
    [string]$WorkspacePath,
    [string]$EnvFile,
    [string]$StatePath,
    [string]$LogPath,
    [ValidateRange(30, 840)][int]$WallTimeoutSeconds = 720,
    [string]$ClaudeCommand = 'claude',
    [string]$ExpectedCurrentTaskXmlSha256 = '',
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$Action = switch ($Action.ToLowerInvariant()) {
    'install' { 'Install' }
    'installfromdisablednostop' { 'InstallFromDisabledNoStop' }
    'status' { 'Status' }
    'uninstall' { 'Uninstall' }
    'rollback' { 'Rollback' }
}
$Mode = if ($Mode -ieq 'Execute') { 'Execute' } else { 'Observe' }
$IsInstallAction = @('Install', 'InstallFromDisabledNoStop') -ccontains $Action
if ($Action -cne 'InstallFromDisabledNoStop' -and
    -not [string]::IsNullOrEmpty($ExpectedCurrentTaskXmlSha256)) {
    throw 'expected_current_task_xml_sha256_action_mismatch'
}

$TaskName = 'SFDC24 Blackboard Order Worker'
$TaskPath = '\'
$ManagedMarker = 'managed-by=install_order_supervisor.ps1; schema=v1'
$WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$RepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$RunnerPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'order_supervisor.ps1'))
$WorkspacePathWasExplicit = -not [string]::IsNullOrWhiteSpace($WorkspacePath)
if ($WorkspacePathWasExplicit) {
    # Only installation actions consume the candidate workspace. Recovery
    # actions must operate even when that candidate checkout is gone or unusable.
    if ($IsInstallAction -and -not [IO.Path]::IsPathRooted($WorkspacePath)) {
        throw 'workspace_path_must_be_absolute'
    }
    if ([IO.Path]::IsPathRooted($WorkspacePath)) {
        $WorkspacePath = [IO.Path]::GetFullPath($WorkspacePath)
    }
} else {
    # Observe cannot invoke Claude, so retaining the release root preserves the
    # prior read-only installation behavior. Execute is rejected in preflight.
    $WorkspacePath = $RepoRoot
}
$MetadataRoot = Join-Path $env:ProgramData 'SFDC24\OrderSupervisor'
$BackupXmlPath = Join-Path $MetadataRoot 'previous-task.xml'
$BackupManifestPath = Join-Path $MetadataRoot 'previous-task.json'
if (-not $StatePath) { $StatePath = Join-Path $MetadataRoot 'state.json' }
if (-not $LogPath) { $LogPath = Join-Path $MetadataRoot 'events.jsonl' }
if (-not $EnvFile) { $EnvFile = Join-Path $WorkspacePath '.env' }

function Write-Utf8 {
    param([string]$Path, [string]$Text)
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-Sha256 {
    param([string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $sha.Dispose()
    }
}

function Assert-NoQuote {
    param([string]$Name, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw ($Name + '_missing') }
    if ($Value.Contains('"')) { throw ($Name + '_contains_quote') }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-InstallerReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)
    return ([int]$Item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0
}

function Restore-InstallerProcessEnvironmentVariable {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()]$Value
    )

    if ($null -eq $Value) {
        $environmentPath = 'Env:\' + $Name
        if (Test-Path -LiteralPath $environmentPath) {
            Remove-Item -LiteralPath $environmentPath -Force -ErrorAction Stop
        }
        if (Test-Path -LiteralPath $environmentPath) { throw 'process_environment_restore_failed' }
        return
    }
    [Environment]::SetEnvironmentVariable($Name, [string]$Value, 'Process')
    if ([Environment]::GetEnvironmentVariable($Name, 'Process') -cne [string]$Value) {
        throw 'process_environment_restore_failed'
    }
}

function Assert-InstallerOwnedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ParentPath,
        [Parameter(Mandatory = $true)][string]$NamePattern
    )

    $parentFullPath = [IO.Path]::GetFullPath($ParentPath).TrimEnd('\')
    $candidateFullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ([IO.Path]::GetDirectoryName($candidateFullPath).TrimEnd('\') -cne $parentFullPath -or
        [IO.Path]::GetFileName($candidateFullPath) -cnotmatch $NamePattern) {
        throw 'claude_cli_isolation_directory_not_owned'
    }
    if (-not (Test-Path -LiteralPath $parentFullPath -PathType Container)) {
        throw 'claude_cli_isolation_directory_unsafe'
    }
    $parentItem = Get-Item -LiteralPath $parentFullPath -Force -ErrorAction Stop
    if (-not $parentItem.PSIsContainer -or (Test-InstallerReparsePoint -Item $parentItem)) {
        throw 'claude_cli_isolation_directory_unsafe'
    }
    if (Test-Path -LiteralPath $candidateFullPath) {
        $candidateItem = Get-Item -LiteralPath $candidateFullPath -Force -ErrorAction Stop
        if (-not $candidateItem.PSIsContainer -or (Test-InstallerReparsePoint -Item $candidateItem)) {
            throw 'claude_cli_isolation_directory_unsafe'
        }
    }
    return $candidateFullPath
}

function Test-InstallerTreeContainsReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    $pending = New-Object System.Collections.Generic.Stack[string]
    $pending.Push($Path)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($entryPath in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $attributes = [IO.File]::GetAttributes($entryPath)
            if (([int]$attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) { return $true }
            if (([int]$attributes -band [int][IO.FileAttributes]::Directory) -ne 0) {
                $pending.Push($entryPath)
            }
        }
    }
    return $false
}

function Remove-InstallerOwnedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ParentPath,
        [Parameter(Mandatory = $true)][string]$NamePattern
    )

    $ownedPath = Assert-InstallerOwnedDirectory -Path $Path -ParentPath $ParentPath -NamePattern $NamePattern
    if (-not (Test-Path -LiteralPath $ownedPath)) { return }
    if (Test-InstallerTreeContainsReparsePoint -Path $ownedPath) {
        throw 'claude_cli_isolation_directory_unsafe'
    }
    Remove-Item -LiteralPath $ownedPath -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $ownedPath) { throw 'claude_cli_isolation_directory_cleanup_failed' }
}

function Assert-ClaudeCliCompatibility {
    param(
        [Parameter(Mandatory = $true)][string]$CommandPath,
        [string]$IsolationToken = ''
    )

    if ([string]::IsNullOrWhiteSpace($IsolationToken)) {
        $IsolationToken = [Guid]::NewGuid().ToString('N')
    } elseif ($IsolationToken -cnotmatch '^[0-9a-f]{32}$') {
        throw 'claude_cli_isolation_token_invalid'
    }
    $temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $parentName = 'order-supervisor-cli-' + $IsolationToken
    $parentPath = Assert-InstallerOwnedDirectory `
        -Path (Join-Path $temporaryBase $parentName) `
        -ParentPath $temporaryBase `
        -NamePattern '^order-supervisor-cli-[0-9a-f]{32}$'
    $configName = 'claude-config-' + $IsolationToken
    $configPath = [IO.Path]::GetFullPath((Join-Path $parentPath $configName))
    $configEnvironmentBackup = [Environment]::GetEnvironmentVariable('CLAUDE_CONFIG_DIR', 'Process')
    $parentCreated = $false
    $configCreated = $false
    try {
        if (Test-Path -LiteralPath $parentPath) { throw 'claude_cli_isolation_parent_collision' }
        New-Item -ItemType Directory -Path $parentPath -ErrorAction Stop | Out-Null
        $parentCreated = $true
        $parentPath = Assert-InstallerOwnedDirectory `
            -Path $parentPath `
            -ParentPath $temporaryBase `
            -NamePattern '^order-supervisor-cli-[0-9a-f]{32}$'
        $configPath = Assert-InstallerOwnedDirectory `
            -Path $configPath `
            -ParentPath $parentPath `
            -NamePattern '^claude-config-[0-9a-f]{32}$'
        if (Test-Path -LiteralPath $configPath) { throw 'claude_cli_isolation_config_collision' }
        New-Item -ItemType Directory -Path $configPath -ErrorAction Stop | Out-Null
        $configCreated = $true
        $configPath = Assert-InstallerOwnedDirectory `
            -Path $configPath `
            -ParentPath $parentPath `
            -NamePattern '^claude-config-[0-9a-f]{32}$'
        [Environment]::SetEnvironmentVariable('CLAUDE_CONFIG_DIR', $configPath, 'Process')

        $versionOutput = @(& $CommandPath --version 2>&1)
        $versionExitCode = $LASTEXITCODE
        if ($versionExitCode -ne 0) { throw 'claude_cli_version_probe_failed' }
        $versionText = (($versionOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
        if ($versionText -cne '2.1.241 (Claude Code)') { throw 'claude_cli_version_unsupported' }

        $helpOutput = @(& $CommandPath --help 2>&1)
        $helpExitCode = $LASTEXITCODE
        if ($helpExitCode -ne 0) { throw 'claude_cli_help_probe_failed' }
        $helpText = ($helpOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        foreach ($requiredFlag in @('--json-schema', '--settings', '--tools', '--strict-mcp-config', '--max-budget-usd', '--permission-mode', '--disallowedTools', '--bare', '--no-session-persistence', '--disable-slash-commands')) {
            if (-not $helpText.Contains($requiredFlag)) {
                throw ('claude_cli_missing_flag_' + $requiredFlag.TrimStart('-'))
            }
        }
        if ($helpText -notmatch '(?s)--permission-mode\s+<mode>.*?\(choices:.*?"manual".*?\)') {
            throw 'claude_cli_manual_mode_missing'
        }
    } finally {
        $cleanupFailed = $false
        try { Restore-InstallerProcessEnvironmentVariable -Name 'CLAUDE_CONFIG_DIR' -Value $configEnvironmentBackup }
        catch { $cleanupFailed = $true }
        if ($configCreated) {
            try {
                Remove-InstallerOwnedDirectory `
                    -Path $configPath `
                    -ParentPath $parentPath `
                    -NamePattern '^claude-config-[0-9a-f]{32}$'
            } catch {
                $cleanupFailed = $true
            }
        }
        if ($parentCreated) {
            try {
                $ownedParent = Assert-InstallerOwnedDirectory `
                    -Path $parentPath `
                    -ParentPath $temporaryBase `
                    -NamePattern '^order-supervisor-cli-[0-9a-f]{32}$'
                if (@([IO.Directory]::EnumerateFileSystemEntries($ownedParent)).Count -ne 0) {
                    throw 'claude_cli_isolation_parent_not_empty'
                }
                Remove-Item -LiteralPath $ownedParent -Force -ErrorAction Stop
                if (Test-Path -LiteralPath $ownedParent) { throw 'claude_cli_isolation_directory_cleanup_failed' }
            } catch {
                $cleanupFailed = $true
            }
        }
        if ($cleanupFailed) { throw 'claude_cli_isolation_cleanup_failed' }
    }
}

function Test-ClaudeCliCompatibilityRequired {
    return $Action -ceq 'Install' -and $Mode -ceq 'Execute'
}

function Assert-MutationAuthorityPreflight {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
        throw 'mutations_require_windows_powershell_5_1'
    }
    if (-not (Test-Administrator)) { throw 'administrator_required_for_system_task' }
}

function Assert-InstallPreflight {
    Assert-MutationAuthorityPreflight
    if (-not (Test-Path -LiteralPath $WindowsPowerShell -PathType Leaf)) { throw 'windows_powershell_missing' }
    if (-not (Test-Path -LiteralPath $RunnerPath -PathType Leaf)) { throw 'runner_missing' }
    foreach ($dependency in @('OrderSupervisor.psm1', 'invoke_order_claude.ps1', 'order_supervisor_result.schema.json', 'bus.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $dependency) -PathType Leaf)) {
            throw ('runner_dependency_missing_' + $dependency)
        }
    }
    if (-not [IO.Path]::IsPathRooted($UserProfilePath)) { throw 'user_profile_path_must_be_absolute' }
    if (-not (Test-Path -LiteralPath $UserProfilePath -PathType Container)) { throw 'user_profile_missing' }
    if (-not (Test-Path -LiteralPath $WorkspacePath -PathType Container)) { throw 'workspace_path_missing' }
    if ($Mode -ceq 'Execute') {
        if (-not $WorkspacePathWasExplicit) { throw 'execute_requires_explicit_workspace_path' }
        if (-not (Test-Path -LiteralPath (Join-Path $WorkspacePath '.git') -PathType Container)) {
            throw 'execute_workspace_git_directory_missing'
        }
    }
    if (-not [IO.Path]::IsPathRooted($EnvFile)) { throw 'env_file_path_must_be_absolute' }
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw 'v1_bus_env_missing' }
    Assert-NoQuote -Name 'runner_path' -Value $RunnerPath
    Assert-NoQuote -Name 'repo_root' -Value $RepoRoot
    Assert-NoQuote -Name 'user_profile' -Value $UserProfilePath
    Assert-NoQuote -Name 'workspace_path' -Value $WorkspacePath
    Assert-NoQuote -Name 'env_file' -Value $EnvFile
    Assert-NoQuote -Name 'state_path' -Value $StatePath
    Assert-NoQuote -Name 'log_path' -Value $LogPath
    Assert-NoQuote -Name 'claude_command' -Value $ClaudeCommand
    if (Test-ClaudeCliCompatibilityRequired) {
        if (-not [IO.Path]::IsPathRooted($ClaudeCommand) -or
            -not (Test-Path -LiteralPath $ClaudeCommand -PathType Leaf)) {
            throw 'execute_requires_absolute_claude_command'
        }
        Assert-ClaudeCliCompatibility -CommandPath $ClaudeCommand
    }
}

function Get-TaskMatches {
    try {
        return @(Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop)
    } catch {
        if ($_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*' -or
            $_.Exception.Message -match 'cannot find|No MSFT_ScheduledTask') {
            return @()
        }
        throw
    }
}

function Get-RootTask {
    $matches = @(Get-TaskMatches)
    if ($matches.Count -gt 1) { throw 'task_name_ambiguous_across_folders' }
    if ($matches.Count -eq 1 -and $matches[0].TaskPath -cne $TaskPath) { throw 'task_name_exists_outside_root' }
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Get-TaskArguments {
    $tokens = @(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy Bypass',
        ('-File "' + $RunnerPath + '"'),
        ('-Mode ' + $Mode),
        '-AllowedSourcesCsv "chat-mobile,codex"',
        ('-UserProfilePath "' + $UserProfilePath + '"'),
        ('-WorkspacePath "' + $WorkspacePath + '"'),
        ('-EnvFile "' + $EnvFile + '"'),
        ('-StatePath "' + $StatePath + '"'),
        ('-LogPath "' + $LogPath + '"'),
        ('-WallTimeoutSeconds ' + [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $WallTimeoutSeconds)),
        ('-ClaudeCommand "' + $ClaudeCommand + '"')
    )
    return $tokens -join ' '
}

function Get-WallTimeoutReadback {
    param([AllowEmptyString()][string]$Arguments)

    $expected = [string]::Format(
        [Globalization.CultureInfo]::InvariantCulture,
        '{0}',
        $WallTimeoutSeconds
    )
    $matches = [Text.RegularExpressions.Regex]::Matches(
        $Arguments,
        '(?<!\S)-WallTimeoutSeconds\s+(?<value>\S+)(?=\s|$)',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    $actual = if ($matches.Count -eq 1) { [string]$matches[0].Groups['value'].Value } else { '' }
    return [pscustomobject][ordered]@{
        expected = $expected
        actual = $actual
        occurrence_count = $matches.Count
        confirmed = ($matches.Count -eq 1 -and $actual -ceq $expected)
    }
}

function New-ExpectedDefinition {
    $taskAction = New-ScheduledTaskAction -Id 'OrderSupervisor' -Execute $WindowsPowerShell -Argument (Get-TaskArguments) -WorkingDirectory $WorkspacePath
    $bootTrigger = New-ScheduledTaskTrigger -AtStartup
    $bootTrigger.Id = 'AtBoot'
    $intervalTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes 15)
    $intervalTrigger.Id = 'Every15Minutes'
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    return New-ScheduledTask -Action $taskAction -Trigger @($bootTrigger, $intervalTrigger) -Principal $taskPrincipal -Settings $taskSettings -Description ('SFDC24 Blackboard ORDER worker; ' + $ManagedMarker)
}

function Test-Managed {
    param($Task)
    return $Task -and ([string]$Task.Description).Contains($ManagedMarker)
}

function Assert-SystemServiceAccountPrincipal {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$ErrorCode
    )
    try {
        $principalId = [string]$Task.Principal.UserId
        $logonType = [string]$Task.Principal.LogonType
    } catch {
        throw $ErrorCode
    }
    if (@('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -cnotcontains $principalId -or
        $logonType -cne 'ServiceAccount') {
        throw $ErrorCode
    }
}

function Compare-Definition {
    param($Task)
    $problems = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Managed $Task)) { $problems.Add('description_marker') }
    if (@($Task.Actions).Count -ne 1) { $problems.Add('action_count') }
    if (@($Task.Actions).Count -eq 1) {
        if ([string]$Task.Actions[0].Execute -cne $WindowsPowerShell) { $problems.Add('action_execute') }
        if ([string]$Task.Actions[0].Arguments -cne (Get-TaskArguments)) { $problems.Add('action_arguments') }
        if ([string]$Task.Actions[0].WorkingDirectory -cne $WorkspacePath) { $problems.Add('working_directory') }
        $timeoutReadback = Get-WallTimeoutReadback -Arguments ([string]$Task.Actions[0].Arguments)
        if (-not [bool]$timeoutReadback.confirmed) { $problems.Add('wall_timeout_seconds') }
    }
    $principalId = [string]$Task.Principal.UserId
    if (@('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -cnotcontains $principalId) { $problems.Add('principal') }
    if ([string]$Task.Principal.LogonType -cne 'ServiceAccount') { $problems.Add('logon_type') }
    if ([string]$Task.Settings.MultipleInstances -cne 'IgnoreNew') { $problems.Add('multiple_instances') }
    if (@($Task.Triggers).Count -ne 2) { $problems.Add('trigger_count') }
    $boot = @($Task.Triggers | Where-Object { $_.Id -ceq 'AtBoot' -and $_.CimClass.CimClassName -ceq 'MSFT_TaskBootTrigger' })
    $interval = @($Task.Triggers | Where-Object { $_.Id -ceq 'Every15Minutes' -and $_.CimClass.CimClassName -ceq 'MSFT_TaskTimeTrigger' })
    if ($boot.Count -ne 1) { $problems.Add('boot_trigger') }
    if ($interval.Count -ne 1 -or [string]$interval[0].Repetition.Interval -cne 'PT15M') { $problems.Add('interval_trigger') }
    return $problems.ToArray()
}

function Save-PreviousTask {
    param($Existing)
    if ($Existing) {
        Assert-SystemServiceAccountPrincipal -Task $Existing -ErrorCode 'backup_task_principal_invalid'
    }
    if (-not (Test-Path -LiteralPath $MetadataRoot)) { New-Item -ItemType Directory -Path $MetadataRoot -Force | Out-Null }
    if ($Existing) {
        $xml = Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
        [IO.File]::WriteAllText($BackupXmlPath, $xml, [Text.Encoding]::Unicode)
        $manifest = [ordered]@{ schema = 'order_supervisor_task_backup.v1'; previous_existed = $true; xml_sha256 = (Get-Sha256 $xml); created_at = [DateTime]::UtcNow.ToString('o') }
    } else {
        if (Test-Path -LiteralPath $BackupXmlPath) { Remove-Item -LiteralPath $BackupXmlPath -Force }
        $manifest = [ordered]@{ schema = 'order_supervisor_task_backup.v1'; previous_existed = $false; xml_sha256 = ''; created_at = [DateTime]::UtcNow.ToString('o') }
    }
    Write-Utf8 -Path $BackupManifestPath -Text ($manifest | ConvertTo-Json)
}

function Read-ValidatedRollbackBackup {
    if (-not (Test-Path -LiteralPath $BackupManifestPath -PathType Leaf)) {
        throw 'rollback_manifest_missing'
    }

    try {
        $manifestText = [IO.File]::ReadAllText($BackupManifestPath, [Text.Encoding]::UTF8)
        $manifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw 'rollback_manifest_invalid'
    }

    if ($null -eq $manifest -or $manifest -isnot [pscustomobject]) {
        throw 'rollback_manifest_invalid'
    }
    $expectedProperties = @('schema', 'previous_existed', 'xml_sha256', 'created_at')
    $actualProperties = @($manifest.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($actualProperties.Count -ne $expectedProperties.Count -or
        @($actualProperties | Where-Object { $expectedProperties -cnotcontains $_ }).Count -ne 0 -or
        @($expectedProperties | Where-Object { $actualProperties -cnotcontains $_ }).Count -ne 0) {
        throw 'rollback_manifest_invalid'
    }
    if ($manifest.schema -isnot [string] -or
        [string]$manifest.schema -cne 'order_supervisor_task_backup.v1' -or
        $manifest.previous_existed -isnot [bool] -or
        $manifest.xml_sha256 -isnot [string] -or
        $manifest.created_at -isnot [string] -or
        [string]$manifest.created_at -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$') {
        throw 'rollback_manifest_invalid'
    }

    if (-not [bool]$manifest.previous_existed) {
        if ([string]$manifest.xml_sha256 -cne '') { throw 'rollback_manifest_invalid' }
        return [pscustomobject][ordered]@{
            previous_existed = $false
            xml = ''
            xml_sha256 = ''
            runner_path = ''
        }
    }

    if ([string]$manifest.xml_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'rollback_manifest_invalid'
    }
    if (-not (Test-Path -LiteralPath $BackupXmlPath -PathType Leaf)) {
        throw 'rollback_xml_missing'
    }
    try {
        $xml = [IO.File]::ReadAllText($BackupXmlPath, [Text.Encoding]::Unicode)
    } catch {
        throw 'rollback_xml_unreadable'
    }
    if ((Get-Sha256 $xml) -cne [string]$manifest.xml_sha256) {
        throw 'rollback_xml_digest_mismatch'
    }

    try {
        $document = New-Object Xml.XmlDocument
        $document.PreserveWhitespace = $true
        $document.XmlResolver = $null
        $document.LoadXml($xml)
    } catch {
        throw 'rollback_xml_invalid'
    }
    $taskNamespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    if ($null -eq $document.DocumentElement -or
        $document.DocumentElement.LocalName -cne 'Task' -or
        $document.DocumentElement.NamespaceURI -cne $taskNamespace) {
        throw 'rollback_xml_identity_invalid'
    }
    $namespaceManager = New-Object Xml.XmlNamespaceManager($document.NameTable)
    $namespaceManager.AddNamespace('t', $taskNamespace)

    $descriptions = @($document.SelectNodes('/t:Task/t:RegistrationInfo/t:Description', $namespaceManager))
    $uris = @($document.SelectNodes('/t:Task/t:RegistrationInfo/t:URI', $namespaceManager))
    $principals = @($document.SelectNodes('/t:Task/t:Principals/t:Principal', $namespaceManager))
    $actions = @($document.SelectNodes('/t:Task/t:Actions/*', $namespaceManager))
    $execActions = @($document.SelectNodes('/t:Task/t:Actions/t:Exec', $namespaceManager))
    if ($descriptions.Count -ne 1 -or
        -not ([string]$descriptions[0].InnerText).Contains($ManagedMarker) -or
        $uris.Count -ne 1 -or [string]$uris[0].InnerText -cne ('\' + $TaskName) -or
        $principals.Count -ne 1 -or
        $actions.Count -ne 1 -or $execActions.Count -ne 1 -or
        [string]$execActions[0].GetAttribute('id') -cne 'OrderSupervisor') {
        throw 'rollback_xml_identity_invalid'
    }
    $userIdNodes = @($principals[0].SelectNodes('t:UserId', $namespaceManager))
    $userIdElements = @($principals[0].ChildNodes | Where-Object {
        $_.NodeType -eq [Xml.XmlNodeType]::Element -and $_.LocalName -ceq 'UserId'
    })
    $logonTypeElements = @($principals[0].ChildNodes | Where-Object {
        $_.NodeType -eq [Xml.XmlNodeType]::Element -and $_.LocalName -ceq 'LogonType'
    })
    # Task Scheduler may omit LogonType when it exports a task whose principal
    # is the canonical S-1-5-18 account. Preserve the exact SYSTEM allow-list
    # for explicit ServiceAccount XML, reject namespace lookalikes, and bind
    # true omission to the observed canonical SID representation.
    if ($userIdNodes.Count -ne 1 -or
        $userIdElements.Count -ne 1 -or
        [string]$userIdElements[0].NamespaceURI -cne $taskNamespace -or
        @('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -cnotcontains [string]$userIdNodes[0].InnerText -or
        ($logonTypeElements.Count -eq 0 -and [string]$userIdNodes[0].InnerText -cne 'S-1-5-18') -or
        $logonTypeElements.Count -gt 1 -or
        ($logonTypeElements.Count -eq 1 -and (
            [string]$logonTypeElements[0].NamespaceURI -cne $taskNamespace -or
            [string]$logonTypeElements[0].InnerText -cne 'ServiceAccount'
        ))) {
        throw 'rollback_xml_identity_invalid'
    }

    $commandNodes = @($execActions[0].SelectNodes('t:Command', $namespaceManager))
    $argumentNodes = @($execActions[0].SelectNodes('t:Arguments', $namespaceManager))
    if ($commandNodes.Count -ne 1 -or [string]$commandNodes[0].InnerText -cne $WindowsPowerShell -or
        $argumentNodes.Count -ne 1) {
        throw 'rollback_xml_identity_invalid'
    }
    $runnerMatches = [Text.RegularExpressions.Regex]::Matches(
        [string]$argumentNodes[0].InnerText,
        '(?<!\S)-File\s+"(?<path>[^"]+)"(?=\s|$)',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if ($runnerMatches.Count -ne 1) { throw 'rollback_xml_runner_invalid' }
    $rollbackRunnerPath = [string]$runnerMatches[0].Groups['path'].Value
    try {
        if (-not [IO.Path]::IsPathRooted($rollbackRunnerPath)) { throw 'invalid' }
        $rollbackRunnerPath = [IO.Path]::GetFullPath($rollbackRunnerPath)
    } catch {
        throw 'rollback_xml_runner_invalid'
    }
    if (-not (Test-Path -LiteralPath $rollbackRunnerPath -PathType Leaf)) {
        throw 'rollback_runner_missing'
    }

    return [pscustomobject][ordered]@{
        previous_existed = $true
        xml = $xml
        xml_sha256 = [string]$manifest.xml_sha256
        runner_path = $rollbackRunnerPath
    }
}

function Restore-PreviousTask {
    # Fully authenticate the rollback material before observing or mutating the
    # current task. A corrupt backup must leave the live definition untouched.
    $backup = Read-ValidatedRollbackBackup
    $current = Get-RootTask
    if ($current) {
        if (-not (Test-Managed $current)) { throw 'refusing_to_replace_unmanaged_task' }
    }
    if ([bool]$backup.previous_existed) {
        if ($current) { Stop-ManagedTask $current }
        # -Force replaces in place; deliberately avoid an unregister gap.
        Register-ScheduledTask -Xml ([string]$backup.xml) -TaskName $TaskName -TaskPath $TaskPath -Force -ErrorAction Stop | Out-Null
        $restored = Get-RootTask
        if (-not $restored -or -not (Test-Managed $restored)) { throw 'rollback_restore_not_visible' }
        Assert-SystemServiceAccountPrincipal -Task $restored -ErrorCode 'rollback_restore_principal_invalid'
        try {
            $readbackXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
        } catch {
            throw 'rollback_restore_readback_failed'
        }
        if ((Get-Sha256 ([string]$readbackXml)) -cne [string]$backup.xml_sha256) {
            throw 'rollback_restore_definition_mismatch'
        }
        return
    }

    if ($current) {
        Stop-ManagedTask $current
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction Stop
    }
    if (Get-RootTask) {
        throw 'rollback_remove_not_verified'
    }
}

function Stop-ManagedTask {
    param($Task)
    if ([string]$Task.State -ceq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 250
            $Task = Get-RootTask
        } while ($Task -and [string]$Task.State -ceq 'Running' -and [DateTime]::UtcNow -lt $deadline)
        if ($Task -and [string]$Task.State -ceq 'Running') { throw 'task_stop_timeout' }
    }
    if ($Task -and @('Ready', 'Disabled') -cnotcontains [string]$Task.State) {
        throw 'task_not_quiescent'
    }
}

function Get-StatusObject {
    $task = Get-RootTask
    if (-not $task) {
        return [pscustomobject][ordered]@{
            status = 'ABSENT'
            task_name = $TaskName
            task_path = $TaskPath
            workspace_path = $WorkspacePath
            workspace_exists = (Test-Path -LiteralPath $WorkspacePath -PathType Container)
            workspace_git_directory_exists = (Test-Path -LiteralPath (Join-Path $WorkspacePath '.git') -PathType Container)
            wall_timeout_seconds = $WallTimeoutSeconds
            wall_timeout_readback = (Get-WallTimeoutReadback -Arguments '')
        }
    }
    $windowsPowerShellExists = Test-Path -LiteralPath $WindowsPowerShell -PathType Leaf
    $userProfilePathIsAbsolute = [IO.Path]::IsPathRooted($UserProfilePath)
    $userProfileExists = $userProfilePathIsAbsolute -and (Test-Path -LiteralPath $UserProfilePath -PathType Container)
    $workspacePathIsAbsolute = [IO.Path]::IsPathRooted($WorkspacePath)
    $workspaceExists = $workspacePathIsAbsolute -and (Test-Path -LiteralPath $WorkspacePath -PathType Container)
    $workspaceGitDirectoryExists = $workspaceExists -and (Test-Path -LiteralPath (Join-Path $WorkspacePath '.git') -PathType Container)
    $runnerExists = Test-Path -LiteralPath $RunnerPath -PathType Leaf
    $envFilePathIsAbsolute = [IO.Path]::IsPathRooted($EnvFile)
    $envFileExists = $envFilePathIsAbsolute -and (Test-Path -LiteralPath $EnvFile -PathType Leaf)
    $claudeCommandReady = $Mode -cne 'Execute' -or (
        [IO.Path]::IsPathRooted($ClaudeCommand) -and
        (Test-Path -LiteralPath $ClaudeCommand -PathType Leaf)
    )
    $driftList = New-Object System.Collections.Generic.List[string]
    foreach ($problem in @(Compare-Definition $task)) { $driftList.Add([string]$problem) }
    foreach ($readinessProblem in @(
        $(if (-not $windowsPowerShellExists) { 'windows_powershell_missing' }),
        $(if (-not $userProfilePathIsAbsolute) { 'user_profile_path_must_be_absolute' }),
        $(if ($userProfilePathIsAbsolute -and -not $userProfileExists) { 'user_profile_missing' }),
        $(if (-not $workspacePathIsAbsolute) { 'workspace_path_must_be_absolute' }),
        $(if ($workspacePathIsAbsolute -and -not $workspaceExists) { 'workspace_path_missing' }),
        $(if ($Mode -ceq 'Execute' -and -not $WorkspacePathWasExplicit) { 'execute_requires_explicit_workspace_path' }),
        $(if ($Mode -ceq 'Execute' -and -not $workspaceGitDirectoryExists) { 'workspace_git_directory_missing' }),
        $(if (-not $runnerExists) { 'runner_missing' }),
        $(if (-not $envFilePathIsAbsolute) { 'env_file_path_must_be_absolute' }),
        $(if ($envFilePathIsAbsolute -and -not $envFileExists) { 'v1_bus_env_missing' }),
        $(if (-not $claudeCommandReady) { 'execute_requires_absolute_claude_command' })
    )) {
        if (-not [string]::IsNullOrWhiteSpace([string]$readinessProblem) -and
            -not $driftList.Contains([string]$readinessProblem)) {
            $driftList.Add([string]$readinessProblem)
        }
    }
    $runnerDirectory = Split-Path -Parent $RunnerPath
    foreach ($dependency in @('OrderSupervisor.psm1', 'invoke_order_claude.ps1', 'order_supervisor_result.schema.json', 'bus.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $runnerDirectory $dependency) -PathType Leaf)) {
            $driftList.Add('runner_dependency_missing_' + $dependency)
        }
    }
    $drift = @($driftList.ToArray())
    $taskArguments = if (@($task.Actions).Count -eq 1) { [string]$task.Actions[0].Arguments } else { '' }
    $wallTimeoutReadback = Get-WallTimeoutReadback -Arguments $taskArguments
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
    $unsigned = [int64]$info.LastTaskResult -band 0xffffffff
    $resultHex = '0x' + [Convert]::ToString($unsigned, 16).PadLeft(8, '0')
    $runnerState = $null
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        try {
            $saved = [IO.File]::ReadAllText($StatePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $runnerState = [pscustomobject][ordered]@{
                schema = $saved.schema
                mode = $saved.mode
                source_tag = $saved.source_tag
                last_poll = $saved.last_poll
                seen = $saved.seen
                success = $saved.success
                error = $saved.error
            }
        } catch {
            $runnerState = [pscustomobject]@{ error = 'state_unreadable' }
        }
    }
    return [pscustomobject][ordered]@{
        status = $(
            if ($drift.Count) { 'DRIFTED' }
            elseif ([string]$task.State -ceq 'Running') { 'RUNNING' }
            elseif ([string]$task.State -ceq 'Disabled') { 'DISABLED' }
            elseif ([string]$task.State -ceq 'Ready') { 'READY' }
            else { 'NOT_READY' }
        )
        task_name = $TaskName
        task_path = $TaskPath
        state = [string]$task.State
        drift = @($drift)
        mode = $Mode
        user_profile = $UserProfilePath
        user_profile_exists = $userProfileExists
        workspace_path = $WorkspacePath
        workspace_exists = $workspaceExists
        workspace_git_directory_exists = $workspaceGitDirectoryExists
        wall_timeout_seconds = $WallTimeoutSeconds
        wall_timeout_readback = $wallTimeoutReadback
        runner_exists = $runnerExists
        env_file_exists = $envFileExists
        state_path = $StatePath
        log_path = $LogPath
        last_run_time = $info.LastRunTime
        next_run_time = $info.NextRunTime
        missed_runs = $info.NumberOfMissedRuns
        last_task_result = [int64]$info.LastTaskResult
        last_task_result_hex = $resultHex
        runner_state = $runnerState
    }
}

function Invoke-InstallAction {
    Assert-InstallPreflight
    $existing = Get-RootTask
    if ($existing -and -not (Test-Managed $existing)) { throw 'refusing_to_overwrite_unmanaged_task' }
    $expected = New-ExpectedDefinition
    if ($existing -and @(Compare-Definition $existing).Count -eq 0) {
        if ($Start) { Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop }
        return (Get-StatusObject)
    }
    Save-PreviousTask -Existing $existing
    try {
        if ($existing) { Stop-ManagedTask $existing }
        Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -InputObject $expected -Force -ErrorAction Stop | Out-Null
        $registered = Get-RootTask
        $problems = @(Compare-Definition $registered)
        if ($problems.Count) { throw ('task_readback_drift:' + ($problems -join ',')) }
    } catch {
        $installFailure = $_
        try {
            Restore-PreviousTask
        } catch {
            throw 'task_install_failed_and_rollback_failed'
        }
        throw $installFailure
    }
    if ($Start) { Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop }
    return (Get-StatusObject)
}

function Invoke-InstallFromDisabledNoStopAction {
    # This is intentionally a separate action rather than an option on ordinary
    # Install. Its call graph must contain no stop, start, unregister, or
    # automatic-restore path even if the disabled task becomes active after the
    # final child-side read. The outer cutover driver owns quarantine on failure.
    if ($Mode -cne 'Observe') { throw 'disabled_no_stop_requires_observe' }
    if ($Start) { throw 'disabled_no_stop_forbids_start' }
    if ($ExpectedCurrentTaskXmlSha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'disabled_no_stop_expected_xml_sha256_invalid'
    }

    Assert-InstallPreflight
    $existing = Get-RootTask
    if (-not $existing) { throw 'disabled_no_stop_requires_existing_task' }
    if (-not (Test-Managed $existing)) { throw 'refusing_to_overwrite_unmanaged_task' }
    if ([string]$existing.State -cne 'Disabled') { throw 'disabled_no_stop_requires_disabled_task' }
    try {
        $authenticatedXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
    } catch {
        throw 'disabled_no_stop_task_xml_export_failed'
    }
    if ((Get-Sha256 -Text ([string]$authenticatedXml)) -cne $ExpectedCurrentTaskXmlSha256) {
        throw 'disabled_no_stop_task_xml_mismatch'
    }

    $expected = New-ExpectedDefinition
    Save-PreviousTask -Existing $existing
    $backup = Read-ValidatedRollbackBackup
    if (-not [bool]$backup.previous_existed -or
        [string]$backup.xml_sha256 -cne $ExpectedCurrentTaskXmlSha256) {
        throw 'disabled_no_stop_backup_identity_mismatch'
    }

    # Save-PreviousTask performs filesystem I/O, so re-read immediately before
    # registration and bind the exported definition to the caller-authenticated
    # digest again. This narrows the race; structural absence of a stop path is
    # what keeps the no-stop guarantee valid across the remaining TOCTOU window.
    $current = Get-RootTask
    if (-not $current) { throw 'disabled_no_stop_task_changed_before_register' }
    if (-not (Test-Managed $current)) { throw 'disabled_no_stop_task_changed_before_register' }
    if ([string]$current.State -cne 'Disabled') { throw 'disabled_no_stop_task_changed_before_register' }
    try {
        $currentXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
    } catch {
        throw 'disabled_no_stop_task_changed_before_register'
    }
    if ((Get-Sha256 -Text ([string]$currentXml)) -cne $ExpectedCurrentTaskXmlSha256) {
        throw 'disabled_no_stop_task_changed_before_register'
    }

    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -InputObject $expected -Force -ErrorAction Stop | Out-Null
    $registered = Get-RootTask
    $problems = @(Compare-Definition $registered)
    if ($problems.Count) { throw ('task_readback_drift:' + ($problems -join ',')) }
    return (Get-StatusObject)
}

function Invoke-UninstallAction {
    Assert-MutationAuthorityPreflight
    $existing = Get-RootTask
    if (-not $existing) { return (Get-StatusObject) }
    if (-not (Test-Managed $existing)) { throw 'refusing_to_remove_unmanaged_task' }
    Stop-ManagedTask $existing
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction Stop
    if (Get-RootTask) { throw 'uninstall_not_verified' }
    return (Get-StatusObject)
}

function Invoke-RollbackAction {
    Assert-MutationAuthorityPreflight
    Restore-PreviousTask
    return (Get-StatusObject)
}

if ($Action -ceq 'Status') {
    Get-StatusObject | ConvertTo-Json -Depth 10
    exit 0
}
if ($Action -ceq 'Install') {
    Invoke-InstallAction | ConvertTo-Json -Depth 10
    exit 0
}
if ($Action -ceq 'InstallFromDisabledNoStop') {
    Invoke-InstallFromDisabledNoStopAction | ConvertTo-Json -Depth 10
    exit 0
}
if ($Action -ceq 'Uninstall') {
    Invoke-UninstallAction | ConvertTo-Json -Depth 10
    exit 0
}
if ($Action -ceq 'Rollback') {
    Invoke-RollbackAction | ConvertTo-Json -Depth 10
    exit 0
}
