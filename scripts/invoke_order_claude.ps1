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

if (-not (Test-Path -LiteralPath $PromptPath)) { throw 'prompt_file_missing' }
if (-not (Test-Path -LiteralPath $SchemaPath)) { throw 'schema_file_missing' }
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
$settingsPath = $null

$exitCode = 0
try {
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

    $resolved = Get-Command $ClaudeCommand -ErrorAction Stop
    $versionOutput = @(& $resolved.Source --version 2>&1)
    $versionExitCode = $LASTEXITCODE
    if ($versionExitCode -ne 0) { throw 'claude_cli_version_probe_failed' }
    $versionText = (($versionOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
    if ($versionText -cne '2.1.241 (Claude Code)') { throw 'claude_cli_version_unsupported' }
    $resolvedPromptPath = (Resolve-Path -LiteralPath $PromptPath).Path
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

    $envPermissionPath = $EnvFile -replace '\\', '/'
    if ($envPermissionPath -notmatch '^(?<drive>[A-Za-z]):/(?<tail>.*)$') {
        throw 'claude_env_file_drive_root_required'
    }
    $envPermissionPath = '//' + $matches['drive'].ToLowerInvariant() + '/' + $matches['tail'].TrimEnd('/')
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
    $invocationRoot = [IO.Path]::GetDirectoryName($resolvedPromptPath)
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
    foreach ($name in $allowedEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($name, $environmentBackup[$name], 'Process')
    }
    [Environment]::SetEnvironmentVariable($subprocessScrubName, $subprocessScrubBackup, 'Process')
    [Environment]::SetEnvironmentVariable($powershellToolName, $powershellToolBackup, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($settingsPath) -and (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
        Remove-Item -LiteralPath $settingsPath -Force -ErrorAction Stop
    }
}
exit $exitCode
