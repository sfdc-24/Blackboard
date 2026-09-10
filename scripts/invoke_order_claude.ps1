#Requires -Version 5.1
<#
Internal child-process adapter for order_supervisor.ps1.

The parent launches this script as a process so it can enforce a wall-clock
timeout over Claude and its descendants. The board payload is read from a
temporary file and sent on stdin; it is never placed on the process command
line. Do not call this adapter directly.
#>
param(
    [Parameter(Mandatory = $true)][string]$PromptPath,
    [Parameter(Mandatory = $true)][string]$SchemaPath,
    [Parameter(Mandatory = $true)][string]$StdoutPath,
    [Parameter(Mandatory = $true)][string]$StderrPath,
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$WorkspacePath,
    [string]$ClaudeCommand = 'claude',
    [ValidateRange(0.01, 20.0)][double]$MaxBudgetUsd = 2.0
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if (-not (Test-Path -LiteralPath $PromptPath -PathType Leaf)) { throw 'prompt_file_missing' }
if (-not (Test-Path -LiteralPath $SchemaPath -PathType Leaf)) { throw 'schema_file_missing' }
$resolvedPromptPath = (Resolve-Path -LiteralPath $PromptPath).Path
$invocationRoot = [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($resolvedPromptPath)).TrimEnd('\')
if (-not [IO.Path]::IsPathRooted($EnvFile)) { throw 'claude_env_file_path_must_be_absolute' }
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw 'claude_env_file_missing' }
$EnvFile = [IO.Path]::GetFullPath($EnvFile)
$envInfo = Get-Item -LiteralPath $EnvFile -Force
if ($envInfo.Length -gt 1048576) { throw 'claude_env_file_too_large' }
if (-not [IO.Path]::IsPathRooted($WorkspacePath)) { throw 'workspace_path_must_be_absolute' }
if (-not (Test-Path -LiteralPath $WorkspacePath -PathType Container)) { throw 'workspace_path_missing' }
$WorkspacePath = [IO.Path]::GetFullPath($WorkspacePath)
if (-not (Test-Path -LiteralPath (Join-Path $WorkspacePath '.git') -PathType Container)) {
    throw 'execute_workspace_git_directory_missing'
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)
    return ([int]$Item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0
}

function Restore-ProcessEnvironmentVariable {
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

function Assert-OwnedClaudeConfigDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ParentPath
    )

    $parentFullPath = [IO.Path]::GetFullPath($ParentPath).TrimEnd('\')
    $candidateFullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $candidateName = [IO.Path]::GetFileName($candidateFullPath)
    if ([IO.Path]::GetDirectoryName($candidateFullPath).TrimEnd('\') -cne $parentFullPath -or
        $candidateName -cnotmatch '^claude-config-[0-9a-f]{32}$') {
        throw 'claude_config_directory_not_owned'
    }

    $parentItem = Get-Item -LiteralPath $parentFullPath -Force -ErrorAction Stop
    if (-not $parentItem.PSIsContainer -or (Test-ReparsePoint -Item $parentItem)) {
        throw 'claude_invocation_root_unsafe'
    }
    if (Test-Path -LiteralPath $candidateFullPath) {
        $candidateItem = Get-Item -LiteralPath $candidateFullPath -Force -ErrorAction Stop
        if (-not $candidateItem.PSIsContainer -or (Test-ReparsePoint -Item $candidateItem)) {
            throw 'claude_config_directory_unsafe'
        }
    }
    return $candidateFullPath
}

function Test-TreeContainsReparsePoint {
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

function Remove-OwnedClaudeConfigDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ParentPath
    )

    $ownedPath = Assert-OwnedClaudeConfigDirectory -Path $Path -ParentPath $ParentPath
    if (-not (Test-Path -LiteralPath $ownedPath)) { return }
    if (Test-TreeContainsReparsePoint -Path $ownedPath) { throw 'claude_config_directory_reparse_point' }
    Remove-Item -LiteralPath $ownedPath -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $ownedPath) { throw 'claude_config_directory_cleanup_failed' }
}

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
foreach ($name in $providerConflictNames) {
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
        throw 'claude_provider_environment_conflict'
    }
}

$allowedEnvironmentNames = @('ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL')
$allowedWorkToolNames = @('Read', 'Edit', 'PowerShell')
$configuredEnvironment = @{}
try {
    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    $envLines = [IO.File]::ReadAllLines($EnvFile, $strictUtf8)
} catch {
    throw 'claude_env_file_utf8_invalid'
}
if ($envLines.Count -gt 4096) { throw 'claude_env_file_too_many_lines' }
foreach ($line in $envLines) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $name = [string]$matches[1]
        if ($allowedEnvironmentNames -icontains $name -and $allowedEnvironmentNames -cnotcontains $name) {
            throw 'claude_env_key_case_invalid'
        }
        if ($allowedEnvironmentNames -ccontains $name) {
            if ($configuredEnvironment.ContainsKey($name)) { throw 'claude_env_duplicate_key' }
            $value = ([string]$matches[2]).Trim()
            if ($value.Length -ge 2 -and
                (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or
                 ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            } elseif ($value.StartsWith('"') -or $value.EndsWith('"') -or
                      $value.StartsWith("'") -or $value.EndsWith("'")) {
                throw 'claude_env_quote_invalid'
            }
            if ([string]::IsNullOrWhiteSpace($value) -or $value.Length -gt 8192 -or $value -cmatch '[\x00-\x1F\x7F]') {
                throw 'claude_env_value_invalid'
            }
            $configuredEnvironment[$name] = $value
        }
    } elseif ($line -imatch '^\s*(ANTHROPIC_API_KEY|ANTHROPIC_MODEL)\b') {
        throw 'claude_env_line_invalid'
    }
}
if (-not $configuredEnvironment.ContainsKey('ANTHROPIC_API_KEY')) { throw 'anthropic_api_key_missing' }
if (-not $configuredEnvironment.ContainsKey('ANTHROPIC_MODEL')) { throw 'anthropic_model_missing' }
$environmentBackup = @{}
foreach ($name in $allowedEnvironmentNames) {
    $environmentBackup[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$subprocessScrubName = 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'
$subprocessScrubBackup = [Environment]::GetEnvironmentVariable($subprocessScrubName, 'Process')
$powershellToolName = 'CLAUDE_CODE_USE_POWERSHELL_TOOL'
$powershellToolBackup = [Environment]::GetEnvironmentVariable($powershellToolName, 'Process')
$configDirectoryEnvironmentName = 'CLAUDE_CONFIG_DIR'
$configDirectoryEnvironmentBackup = [Environment]::GetEnvironmentVariable($configDirectoryEnvironmentName, 'Process')
$settingsPath = $null
$configDirectoryCreated = $false
$configDirectoryPath = Assert-OwnedClaudeConfigDirectory `
    -Path (Join-Path $invocationRoot ('claude-config-' + [Guid]::NewGuid().ToString('N'))) `
    -ParentPath $invocationRoot

$exitCode = 0
try {
    if (Test-Path -LiteralPath $configDirectoryPath) { throw 'claude_config_directory_collision' }
    try {
        New-Item -ItemType Directory -Path $configDirectoryPath -ErrorAction Stop | Out-Null
        $configDirectoryCreated = $true
        $configDirectoryPath = Assert-OwnedClaudeConfigDirectory -Path $configDirectoryPath -ParentPath $invocationRoot
    } catch {
        throw 'claude_config_directory_create_failed'
    }
    foreach ($name in $allowedEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($name, [string]$configuredEnvironment[$name], 'Process')
    }
    # Keep the direct provider credential available to the Claude parent for its
    # API calls while removing recognized credentials from Claude-launched
    # tools, hooks, and stdio MCP servers. This control is owned by the adapter,
    # not imported from the workspace environment file.
    [Environment]::SetEnvironmentVariable($subprocessScrubName, '1', 'Process')
    # PowerShell enablement is owned by the explicit per-invocation settings
    # document. Clear any ambient value so the child cannot appear configured
    # when that document was ignored or malformed.
    [Environment]::SetEnvironmentVariable($powershellToolName, $null, 'Process')
    # Direct API-key authentication does not require Claude's persistent user
    # profile. Isolate all CLI configuration/state inside this invocation so
    # --no-session-persistence cannot still mutate ~/.claude or ~/.claude.json.
    [Environment]::SetEnvironmentVariable($configDirectoryEnvironmentName, $configDirectoryPath, 'Process')

    $resolved = Get-Command $ClaudeCommand -ErrorAction Stop
    $versionOutput = @(& $resolved.Source --version 2>&1)
    $versionExitCode = $LASTEXITCODE
    if ($versionExitCode -ne 0) { throw 'claude_cli_version_probe_failed' }
    $versionText = (($versionOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
    if ($versionText -cne '2.1.241 (Claude Code)') { throw 'claude_cli_version_unsupported' }
    $promptText = [IO.File]::ReadAllText($resolvedPromptPath, [Text.Encoding]::UTF8)
    $schemaText = ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $SchemaPath).Path, [Text.Encoding]::UTF8) | ConvertFrom-Json) | ConvertTo-Json -Depth 12 -Compress
    # Windows PowerShell 5.1's native argv marshaller otherwise removes the JSON
    # quotation marks. Backslash-escaped quotes arrive intact at the Node CLI.
    $schemaArgument = if ($PSVersionTable.PSEdition -eq 'Desktop') {
        ($schemaText -replace '"', '\"')
    } else {
        $schemaText
    }
    $PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

    # The deny rule below needs the env file as an ABSOLUTE path in the shape
    # Claude's permission matcher expects. On Windows that is //c/path/to/file;
    # on Linux an absolute path is already that shape.
    #
    # This used to accept ONLY the drive-letter form, so on Linux every single
    # invocation died with claude_env_file_drive_root_required before the
    # provider was ever reached. Codex found it; my Linux execute test could not,
    # because it drives a FAKE adapter and so never touches this line.
    #
    # A RELATIVE path is still refused on both platforms. The point of the rule
    # is to deny reads of one exact file, and a relative path does not name one.
    $envPermissionPath = $EnvFile -replace '\\', '/'
    $onWindows = $true
    if (Test-Path Variable:IsWindows) { $onWindows = [bool]$IsWindows }
    if ($envPermissionPath -match '^(?<drive>[A-Za-z]):/(?<tail>.*)$') {
        $envPermissionPath = '//' + $matches['drive'].ToLowerInvariant() + '/' + $matches['tail'].TrimEnd('/')
    } elseif (-not $onWindows -and $envPermissionPath.StartsWith('/')) {
        # Already absolute and already POSIX. TrimEnd matches the Windows branch
        # so a trailing slash cannot produce two different deny rules for one
        # file depending on the host.
        $envPermissionPath = $envPermissionPath.TrimEnd('/')
        if (-not $envPermissionPath) { throw 'claude_env_file_absolute_path_required' }
    } else {
        throw 'claude_env_file_drive_root_required'
    }
    $settingsDocument = [ordered]@{
        env = [ordered]@{
            CLAUDE_CODE_SUBPROCESS_ENV_SCRUB = '1'
            CLAUDE_CODE_USE_POWERSHELL_TOOL = '1'
        }
        permissions = [ordered]@{
            allow = $allowedWorkToolNames
            # These rules protect only Claude's built-in file tools. They do
            # not constrain arbitrary commands run by the PowerShell tool.
            deny = @(
                ('Read(' + $envPermissionPath + ')')
                ('Edit(' + $envPermissionPath + ')')
            )
        }
    }
    $settingsText = $settingsDocument | ConvertTo-Json -Depth 8
    if ($settingsText.Length -gt 16384) { throw 'claude_settings_too_large' }
    $settingsPath = Join-Path $invocationRoot ('claude-settings-' + [Guid]::NewGuid().ToString('N') + '.json')
    [IO.File]::WriteAllText($settingsPath, $settingsText, (New-Object Text.UTF8Encoding($false)))

    $arguments = @(
        '--print',
        '--output-format', 'json',
        '--json-schema', $schemaArgument,
        '--settings', $settingsPath,
        '--tools', ($allowedWorkToolNames -join ','),
        '--strict-mcp-config',
        '--permission-mode', 'manual',
        '--disallowedTools', 'Bash', 'AskUserQuestion',
        '--max-budget-usd', ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:0.00}', $MaxBudgetUsd)),
        '--bare',
        '--no-session-persistence',
        '--disable-slash-commands'
    )

    # Native output is isolated in files. The parent validates structured_output
    # and never copies stderr or raw model output into state or the JSONL log.
    Push-Location -LiteralPath $WorkspacePath
    try {
        $promptText | & $resolved.Source @arguments 1> $StdoutPath 2> $StderrPath
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    $cleanupFailed = $false
    foreach ($name in $allowedEnvironmentNames) {
        try { Restore-ProcessEnvironmentVariable -Name $name -Value $environmentBackup[$name] }
        catch { $cleanupFailed = $true }
    }
    try { Restore-ProcessEnvironmentVariable -Name $subprocessScrubName -Value $subprocessScrubBackup }
    catch { $cleanupFailed = $true }
    try { Restore-ProcessEnvironmentVariable -Name $powershellToolName -Value $powershellToolBackup }
    catch { $cleanupFailed = $true }
    try { Restore-ProcessEnvironmentVariable -Name $configDirectoryEnvironmentName -Value $configDirectoryEnvironmentBackup }
    catch { $cleanupFailed = $true }
    try {
        if (-not [string]::IsNullOrWhiteSpace($settingsPath) -and (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
            Remove-Item -LiteralPath $settingsPath -Force -ErrorAction Stop
        }
        if (-not [string]::IsNullOrWhiteSpace($settingsPath) -and (Test-Path -LiteralPath $settingsPath)) {
            throw 'claude_settings_cleanup_failed'
        }
    } catch {
        $cleanupFailed = $true
    }
    try {
        if ($configDirectoryCreated) {
            Remove-OwnedClaudeConfigDirectory -Path $configDirectoryPath -ParentPath $invocationRoot
        }
    } catch {
        $cleanupFailed = $true
    }
    if ($cleanupFailed) { throw 'claude_isolation_cleanup_failed' }
}
exit $exitCode
