#Requires -Version 5.1
<#
Install, inspect, uninstall, or roll back the SYSTEM scheduled task for the
one-shot Blackboard ORDER worker. Status is the non-mutating default.

Rollback restores only the prior Task Scheduler definition. The runner files
remain the version present in this checkout.
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Uninstall', 'Rollback')][string]$Action = 'Status',
    [ValidateSet('Observe', 'Execute')][string]$Mode = 'Observe',
    [string]$UserProfilePath = 'C:\Users\akatiawam',
    [string]$WorkspacePath,
    [string]$EnvFile,
    [string]$StatePath,
    [string]$LogPath,
    [string]$ClaudeCommand = 'claude',
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$Mode = if ($Mode -ieq 'Execute') { 'Execute' } else { 'Observe' }

$TaskName = 'SFDC24 Blackboard Order Worker'
$TaskPath = '\'
$ManagedMarker = 'managed-by=install_order_supervisor.ps1; schema=v1'
$WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$RepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$RunnerPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'order_supervisor.ps1'))
$WorkspacePathWasExplicit = -not [string]::IsNullOrWhiteSpace($WorkspacePath)
if ($WorkspacePathWasExplicit) {
    if (-not [IO.Path]::IsPathRooted($WorkspacePath)) { throw 'workspace_path_must_be_absolute' }
    $WorkspacePath = [IO.Path]::GetFullPath($WorkspacePath)
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

function Assert-MutationPreflight {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
        throw 'mutations_require_windows_powershell_5_1'
    }
    if (-not (Test-Administrator)) { throw 'administrator_required_for_system_task' }
    if (-not (Test-Path -LiteralPath $WindowsPowerShell -PathType Leaf)) { throw 'windows_powershell_missing' }
    if (-not (Test-Path -LiteralPath $RunnerPath -PathType Leaf)) { throw 'runner_missing' }
    foreach ($dependency in @('OrderSupervisor.psm1', 'invoke_order_claude.ps1', 'order_supervisor_result.schema.json', 'bus.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $dependency) -PathType Leaf)) {
            throw ('runner_dependency_missing_' + $dependency)
        }
    }
    if (-not (Test-Path -LiteralPath $UserProfilePath -PathType Container)) { throw 'user_profile_missing' }
    if (-not (Test-Path -LiteralPath $WorkspacePath -PathType Container)) { throw 'workspace_path_missing' }
    if ($Mode -ceq 'Execute') {
        if (-not $WorkspacePathWasExplicit) { throw 'execute_requires_explicit_workspace_path' }
        if (-not (Test-Path -LiteralPath (Join-Path $WorkspacePath '.git') -PathType Container)) {
            throw 'execute_workspace_git_directory_missing'
        }
    }
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw 'v1_bus_env_missing' }
    Assert-NoQuote -Name 'runner_path' -Value $RunnerPath
    Assert-NoQuote -Name 'repo_root' -Value $RepoRoot
    Assert-NoQuote -Name 'user_profile' -Value $UserProfilePath
    Assert-NoQuote -Name 'workspace_path' -Value $WorkspacePath
    Assert-NoQuote -Name 'env_file' -Value $EnvFile
    Assert-NoQuote -Name 'state_path' -Value $StatePath
    Assert-NoQuote -Name 'log_path' -Value $LogPath
    Assert-NoQuote -Name 'claude_command' -Value $ClaudeCommand
    if ($Mode -ceq 'Execute') {
        if (-not [IO.Path]::IsPathRooted($ClaudeCommand) -or
            -not (Test-Path -LiteralPath $ClaudeCommand -PathType Leaf)) {
            throw 'execute_requires_absolute_claude_command'
        }
        $helpText = (& $ClaudeCommand --help 2>&1) -join [Environment]::NewLine
        foreach ($requiredFlag in @('--json-schema', '--max-budget-usd', '--permission-mode', '--disallowedTools', '--safe-mode', '--no-session-persistence', '--disable-slash-commands')) {
            if (-not $helpText.Contains($requiredFlag)) {
                throw ('claude_cli_missing_flag_' + $requiredFlag.TrimStart('-'))
            }
        }
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
        ('-ClaudeCommand "' + $ClaudeCommand + '"')
    )
    return $tokens -join ' '
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

function Compare-Definition {
    param($Task)
    $problems = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Managed $Task)) { $problems.Add('description_marker') }
    if (@($Task.Actions).Count -ne 1) { $problems.Add('action_count') }
    if (@($Task.Actions).Count -eq 1) {
        if ([string]$Task.Actions[0].Execute -cne $WindowsPowerShell) { $problems.Add('action_execute') }
        if ([string]$Task.Actions[0].Arguments -cne (Get-TaskArguments)) { $problems.Add('action_arguments') }
        if ([string]$Task.Actions[0].WorkingDirectory -cne $WorkspacePath) { $problems.Add('working_directory') }
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

function Restore-PreviousTask {
    if (-not (Test-Path -LiteralPath $BackupManifestPath -PathType Leaf)) { throw 'rollback_manifest_missing' }
    $manifest = [IO.File]::ReadAllText($BackupManifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($manifest.schema -cne 'order_supervisor_task_backup.v1') { throw 'rollback_manifest_invalid' }
    $current = Get-RootTask
    if ($current) {
        if (-not (Test-Managed $current)) { throw 'refusing_to_replace_unmanaged_task' }
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction Stop
    }
    if ([bool]$manifest.previous_existed) {
        if (-not (Test-Path -LiteralPath $BackupXmlPath -PathType Leaf)) { throw 'rollback_xml_missing' }
        $xml = [IO.File]::ReadAllText($BackupXmlPath, [Text.Encoding]::Unicode)
        if ((Get-Sha256 $xml) -cne [string]$manifest.xml_sha256) { throw 'rollback_xml_digest_mismatch' }
        Register-ScheduledTask -Xml $xml -TaskName $TaskName -TaskPath $TaskPath -Force -ErrorAction Stop | Out-Null
        if (-not (Get-RootTask)) { throw 'rollback_restore_not_visible' }
    } elseif (Get-RootTask) {
        throw 'rollback_remove_not_verified'
    }
}

function Stop-ManagedTask {
    param($Task)
    if ($Task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 250
            $Task = Get-RootTask
        } while ($Task -and $Task.State -eq 'Running' -and [DateTime]::UtcNow -lt $deadline)
        if ($Task -and $Task.State -eq 'Running') { throw 'task_stop_timeout' }
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
        }
    }
    $drift = @(Compare-Definition $task)
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
        status = $(if ($drift.Count) { 'DRIFTED' } elseif ($task.State -eq 'Running') { 'RUNNING' } elseif ($task.State -eq 'Disabled') { 'DISABLED' } else { 'READY' })
        task_name = $TaskName
        task_path = $TaskPath
        state = [string]$task.State
        drift = @($drift)
        mode = $Mode
        user_profile = $UserProfilePath
        user_profile_exists = (Test-Path -LiteralPath $UserProfilePath -PathType Container)
        workspace_path = $WorkspacePath
        workspace_exists = (Test-Path -LiteralPath $WorkspacePath -PathType Container)
        workspace_git_directory_exists = (Test-Path -LiteralPath (Join-Path $WorkspacePath '.git') -PathType Container)
        runner_exists = (Test-Path -LiteralPath $RunnerPath -PathType Leaf)
        env_file_exists = (Test-Path -LiteralPath $EnvFile -PathType Leaf)
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

if ($Action -ceq 'Status') {
    Get-StatusObject | ConvertTo-Json -Depth 10
    exit 0
}

Assert-MutationPreflight
if ($Action -ceq 'Install') {
    $existing = Get-RootTask
    if ($existing -and -not (Test-Managed $existing)) { throw 'refusing_to_overwrite_unmanaged_task' }
    $expected = New-ExpectedDefinition
    if ($existing -and @(Compare-Definition $existing).Count -eq 0) {
        if ($Start) { Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop }
        Get-StatusObject | ConvertTo-Json -Depth 10
        exit 0
    }
    Save-PreviousTask -Existing $existing
    try {
        if ($existing) { Stop-ManagedTask $existing }
        Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -InputObject $expected -Force -ErrorAction Stop | Out-Null
        $registered = Get-RootTask
        $problems = @(Compare-Definition $registered)
        if ($problems.Count) { throw ('task_readback_drift:' + ($problems -join ',')) }
    } catch {
        try { Restore-PreviousTask } catch {}
        throw
    }
    if ($Start) { Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop }
    Get-StatusObject | ConvertTo-Json -Depth 10
    exit 0
}

if ($Action -ceq 'Uninstall') {
    $existing = Get-RootTask
    if (-not $existing) { Get-StatusObject | ConvertTo-Json -Depth 10; exit 0 }
    if (-not (Test-Managed $existing)) { throw 'refusing_to_remove_unmanaged_task' }
    Stop-ManagedTask $existing
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction Stop
    if (Get-RootTask) { throw 'uninstall_not_verified' }
    Get-StatusObject | ConvertTo-Json -Depth 10
    exit 0
}

if ($Action -ceq 'Rollback') {
    Restore-PreviousTask
    Get-StatusObject | ConvertTo-Json -Depth 10
    exit 0
}
