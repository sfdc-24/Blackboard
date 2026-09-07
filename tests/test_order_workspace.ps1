#Requires -Version 5.1
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunnerPath = Join-Path $RepoRoot 'scripts\order_supervisor.ps1'
$InstallerPath = Join-Path $RepoRoot 'scripts\install_order_supervisor.ps1'
$AdapterPath = Join-Path $RepoRoot 'scripts\invoke_order_claude.ps1'
$SchemaPath = Join-Path $RepoRoot 'scripts\order_supervisor_result.schema.json'
$FixturePath = Join-Path $PSScriptRoot 'fixtures\order_supervisor_board.json'
$script:Passed = 0
$script:Failed = 0

function Assert-True {
    param([string]$Name, [bool]$Condition)
    if ($Condition) {
        $script:Passed++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Failed++
        Write-Output ('FAIL ' + $Name)
    }
}

function Invoke-PowerShellChild {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][object[]]$Arguments
    )

    $engine = (Get-Process -Id $PID).Path
    $childArguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', $ScriptPath
    ) + @($Arguments)
    $priorErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $engine @childArguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $priorErrorActionPreference
    }
    [pscustomobject][ordered]@{
        exit_code = $exitCode
        output = @($output)
        output_text = (($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine)
    }
}

function New-AdapterArguments {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspacePath,
        [Parameter(Mandatory = $true)][string]$Prefix,
        [Parameter(Mandatory = $true)][string]$FakeClaudePath,
        [Parameter(Mandatory = $true)][string]$TemporaryRoot,
        [Parameter(Mandatory = $true)][string]$EnvFile
    )

    return @(
        '-PromptPath', (Join-Path $TemporaryRoot 'prompt.txt'),
        '-SchemaPath', $SchemaPath,
        '-StdoutPath', (Join-Path $TemporaryRoot ($Prefix + '-stdout.json')),
        '-StderrPath', (Join-Path $TemporaryRoot ($Prefix + '-stderr.txt')),
        '-EnvFile', $EnvFile,
        '-WorkspacePath', $WorkspacePath,
        '-ClaudeCommand', $FakeClaudePath,
        '-MaxBudgetUsd', '0.01'
    )
}

function Write-TestEnvFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )

    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Invoke-AdapterCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$WorkspacePath,
        [Parameter(Mandatory = $true)][string]$EnvFile,
        [Parameter(Mandatory = $true)][string]$FakeClaudePath,
        [Parameter(Mandatory = $true)][string]$TemporaryRoot,
        [switch]$OmitEnvFile
    )

    $safeName = $Name -replace '[^A-Za-z0-9_-]', '-'
    $prefix = 'case-' + $safeName
    $markerPath = Join-Path $TemporaryRoot ($prefix + '-invoked.txt')
    $configCapturePath = Join-Path $TemporaryRoot ($prefix + '-config.jsonl')
    $priorMarkerPath = [Environment]::GetEnvironmentVariable('ORDER_ADAPTER_FAKE_MARKER_PATH', 'Process')
    $priorConfigCapturePath = [Environment]::GetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', 'Process')
    [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_MARKER_PATH', $markerPath, 'Process')
    [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', $configCapturePath, 'Process')
    try {
        [object[]]$arguments = @(
            New-AdapterArguments `
                -WorkspacePath $WorkspacePath `
                -Prefix $prefix `
                -FakeClaudePath $FakeClaudePath `
                -TemporaryRoot $TemporaryRoot `
                -EnvFile $EnvFile
        )
        if ($OmitEnvFile) {
            $withoutEnv = New-Object System.Collections.Generic.List[object]
            for ($index = 0; $index -lt $arguments.Count; $index++) {
                if ([string]$arguments[$index] -ceq '-EnvFile') {
                    $index++
                    continue
                }
                $withoutEnv.Add($arguments[$index])
            }
            [object[]]$arguments = $withoutEnv.ToArray()
        }
        $run = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments $arguments
    } finally {
        [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_MARKER_PATH', $priorMarkerPath, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', $priorConfigCapturePath, 'Process')
    }

    return [pscustomobject][ordered]@{
        run = $run
        invoked = (Test-Path -LiteralPath $markerPath -PathType Leaf)
        stdout_path = Join-Path $TemporaryRoot ($prefix + '-stdout.json')
        stderr_path = Join-Path $TemporaryRoot ($prefix + '-stderr.txt')
        config_capture_path = $configCapturePath
    }
}

function Get-ConfigCaptureRecords {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    return @(
        [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { [string]$_ | ConvertFrom-Json }
    )
}

function Test-ConfigCapturesCleaned {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Records,
        [Parameter(Mandatory = $true)][string]$ExpectedParent
    )
    $parent = [IO.Path]::GetFullPath($ExpectedParent).TrimEnd('\')
    foreach ($record in $Records) {
        $path = [string]$record.config_path
        if (-not [bool]$record.config_exists -or -not [IO.Path]::IsPathRooted($path) -or
            [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($path)).TrimEnd('\') -cne $parent -or
            [IO.Path]::GetFileName($path) -cnotmatch '^claude-config-[0-9a-f]{32}$' -or
            (Test-Path -LiteralPath $path)) {
            return $false
        }
    }
    if (Test-Path -LiteralPath $parent -PathType Container) {
        $leftovers = @(
            Get-ChildItem -LiteralPath $parent -Directory -Force -ErrorAction Stop |
                Where-Object { $_.Name -cmatch '^claude-config-[0-9a-f]{32}$' }
        )
        if ($leftovers.Count -ne 0) { return $false }
    }
    return $true
}

function Invoke-AdapterConfigRestorationCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$WorkspacePath,
        [Parameter(Mandatory = $true)][string]$EnvFile,
        [Parameter(Mandatory = $true)][string]$FakeClaudePath,
        [Parameter(Mandatory = $true)][string]$TemporaryRoot,
        [AllowNull()][string]$AmbientConfigDirectory,
        [string]$FailureVariable = '',
        [string]$FailureValue = ''
    )

    $capturePath = Join-Path $TemporaryRoot ('restore-' + $Name + '-config.jsonl')
    $priorConfig = [Environment]::GetEnvironmentVariable('CLAUDE_CONFIG_DIR', 'Process')
    $priorCapture = [Environment]::GetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', 'Process')
    $priorFailure = if ([string]::IsNullOrWhiteSpace($FailureVariable)) { $null } else {
        [Environment]::GetEnvironmentVariable($FailureVariable, 'Process')
    }
    if ($null -eq $AmbientConfigDirectory) {
        Remove-Item -LiteralPath 'Env:\CLAUDE_CONFIG_DIR' -Force -ErrorAction SilentlyContinue
    } else {
        [Environment]::SetEnvironmentVariable('CLAUDE_CONFIG_DIR', $AmbientConfigDirectory, 'Process')
    }
    [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', $capturePath, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($FailureVariable)) {
        [Environment]::SetEnvironmentVariable($FailureVariable, $FailureValue, 'Process')
    }
    $pipeline = [PowerShell]::Create()
    $invokeError = ''
    try {
        $prefix = 'restore-' + $Name
        $null = $pipeline.AddCommand($AdapterPath)
        $null = $pipeline.AddParameter('PromptPath', (Join-Path $TemporaryRoot 'prompt.txt'))
        $null = $pipeline.AddParameter('SchemaPath', $SchemaPath)
        $null = $pipeline.AddParameter('StdoutPath', (Join-Path $TemporaryRoot ($prefix + '-stdout.json')))
        $null = $pipeline.AddParameter('StderrPath', (Join-Path $TemporaryRoot ($prefix + '-stderr.txt')))
        $null = $pipeline.AddParameter('EnvFile', $EnvFile)
        $null = $pipeline.AddParameter('WorkspacePath', $WorkspacePath)
        $null = $pipeline.AddParameter('ClaudeCommand', $FakeClaudePath)
        $null = $pipeline.AddParameter('MaxBudgetUsd', 0.01)
        try { $null = @($pipeline.Invoke()) }
        catch { $invokeError = [string]$_.Exception.Message }
        if ([string]$pipeline.InvocationStateInfo.State -ceq 'Failed' -and
            $null -ne $pipeline.InvocationStateInfo.Reason) {
            $invokeError = [string]$pipeline.InvocationStateInfo.Reason.Message
        }
        if ([string]::IsNullOrWhiteSpace($invokeError) -and $pipeline.Streams.Error.Count -gt 0) {
            $invokeError = [string]$pipeline.Streams.Error[$pipeline.Streams.Error.Count - 1].Exception.Message
        }
        $observedAfter = [Environment]::GetEnvironmentVariable('CLAUDE_CONFIG_DIR', 'Process')
    } finally {
        $pipeline.Dispose()
        if ($null -eq $priorConfig) { Remove-Item -LiteralPath 'Env:\CLAUDE_CONFIG_DIR' -Force -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable('CLAUDE_CONFIG_DIR', $priorConfig, 'Process') }
        [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', $priorCapture, 'Process')
        if (-not [string]::IsNullOrWhiteSpace($FailureVariable)) {
            [Environment]::SetEnvironmentVariable($FailureVariable, $priorFailure, 'Process')
        }
    }
    return [pscustomobject][ordered]@{
        restored = $(
            if ($null -eq $AmbientConfigDirectory) { $null -eq $observedAfter }
            else { [string]::Equals([string]$observedAfter, $AmbientConfigDirectory, [StringComparison]::Ordinal) }
        )
        records = @(Get-ConfigCaptureRecords -Path $capturePath)
        error = $invokeError
    }
}

function Test-TextExcludesSentinels {
    param(
        [AllowNull()][string]$Text,
        [Parameter(Mandatory = $true)][string[]]$Sentinels
    )

    $candidate = if ($null -eq $Text) { '' } else { [string]$Text }
    return @($Sentinels | Where-Object { -not [string]::IsNullOrEmpty($_) -and $candidate.Contains($_) }).Count -eq 0
}

function Assert-AdapterRejected {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Case,
        [Parameter(Mandatory = $true)][string]$ExpectedCode,
        [Parameter(Mandatory = $true)][string[]]$SecretSentinels
    )

    Assert-True ($Name + ' returns expected error') (
        $Case.run.exit_code -ne 0 -and $Case.run.output_text.Contains($ExpectedCode)
    )
    Assert-True ($Name + ' does not invoke fake Claude') (-not $Case.invoked)
    Assert-True ($Name + ' error excludes secret sentinels') (
        Test-TextExcludesSentinels -Text $Case.run.output_text -Sentinels $SecretSentinels
    )
    $stdoutSafe = -not (Test-Path -LiteralPath $Case.stdout_path -PathType Leaf) -or
        (Get-Item -LiteralPath $Case.stdout_path).Length -eq 0
    $stderrSafe = -not (Test-Path -LiteralPath $Case.stderr_path -PathType Leaf) -or
        (Get-Item -LiteralPath $Case.stderr_path).Length -eq 0
    Assert-True ($Name + ' creates no raw Claude output') ($stdoutSafe -and $stderrSafe)
    $configRecords = @(Get-ConfigCaptureRecords -Path ([string]$Case.config_capture_path))
    Assert-True ($Name + ' removes any isolated config created before rejection') (
        Test-ConfigCapturesCleaned -Records $configRecords -ExpectedParent ([IO.Path]::GetDirectoryName([string]$Case.config_capture_path))
    )
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-workspace-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
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
$testEnvironmentNames = @(
    'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'BUS_SECRET', 'ALPHA_SECRET',
    'OPENAI_API_KEY', 'ORDER_SUPERVISOR_DECOY', 'ORDER_ADAPTER_FAKE_MARKER_PATH',
    'ORDER_ADAPTER_FAKE_EXIT_CODE', 'ORDER_ADAPTER_FAKE_VERSION',
    'ORDER_ADAPTER_FAKE_VERSION_EXIT_CODE', 'ORDER_ADAPTER_FAKE_THROW',
    'ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', 'ORDER_ADAPTER_FAKE_CONFIG_REPARSE_TARGET',
    'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB',
    'CLAUDE_CODE_USE_POWERSHELL_TOOL', 'CLAUDE_CONFIG_DIR'
) + $providerConflictNames
$testEnvironmentBackup = @{}
foreach ($name in $testEnvironmentNames) {
    $testEnvironmentBackup[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    Remove-Item -LiteralPath ('Env:\' + $name) -Force -ErrorAction SilentlyContinue
}
try {
    $workspace = Join-Path $tempRoot 'Blackboard checkout'
    $gitDirectory = Join-Path $workspace '.git'
    $nonGitWorkspace = Join-Path $tempRoot 'not-a-checkout'
    New-Item -ItemType Directory -Path $gitDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $nonGitWorkspace -Force | Out-Null
    [IO.File]::WriteAllText(
        (Join-Path $tempRoot 'prompt.txt'),
        'bounded workspace test',
        (New-Object Text.UTF8Encoding($false))
    )

    $adapterEnvFile = Join-Path $tempRoot 'adapter config.env'
    $adapterApiKey = 'test-api-key=preserves#characters'
    $adapterModel = 'claude-test-model'
    $adapterEnvText = @"
ANTHROPIC_API_KEY="$adapterApiKey"
ANTHROPIC_MODEL=$adapterModel
BUS_SECRET=must-not-be-imported
ALPHA_SECRET=must-not-be-imported
OPENAI_API_KEY=must-not-be-imported
ORDER_SUPERVISOR_DECOY=must-not-be-imported
"@
    [IO.File]::WriteAllText($adapterEnvFile, $adapterEnvText, (New-Object Text.UTF8Encoding($false)))

$fakeClaudePath = Join-Path $tempRoot 'fake-claude.ps1'
$fakeClaude = @'
function Write-ConfigCapture([string]$Phase) {
    $configPath = [string]$env:CLAUDE_CONFIG_DIR
    $configExists = -not [string]::IsNullOrWhiteSpace($configPath) -and (Test-Path -LiteralPath $configPath -PathType Container)
    if ($configExists) {
        [IO.File]::WriteAllText((Join-Path $configPath ('fake-marker-' + $Phase + '.txt')), 'marker', [Text.UTF8Encoding]::new($false))
        if (-not [string]::IsNullOrWhiteSpace($env:ORDER_ADAPTER_FAKE_CONFIG_REPARSE_TARGET)) {
            $escapePath = Join-Path $configPath 'escape'
            if (-not (Test-Path -LiteralPath $escapePath)) {
                New-Item -ItemType Junction -Path $escapePath -Target $env:ORDER_ADAPTER_FAKE_CONFIG_REPARSE_TARGET | Out-Null
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH)) {
        $record = [ordered]@{ phase = $Phase; config_path = $configPath; config_exists = $configExists }
        [IO.File]::AppendAllText(
            $env:ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH,
            (($record | ConvertTo-Json -Compress) + [Environment]::NewLine),
            [Text.UTF8Encoding]::new($false)
        )
    }
}
if ($args.Count -eq 1 -and [string]$args[0] -ceq '--version') {
    Write-ConfigCapture -Phase 'version'
    if (-not [string]::IsNullOrWhiteSpace($env:ORDER_ADAPTER_FAKE_VERSION_EXIT_CODE)) {
        exit ([int]$env:ORDER_ADAPTER_FAKE_VERSION_EXIT_CODE)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ORDER_ADAPTER_FAKE_VERSION)) {
        Write-Output $env:ORDER_ADAPTER_FAKE_VERSION
    } else {
        Write-Output '2.1.241 (Claude Code)'
    }
    exit 0
}
Write-ConfigCapture -Phase 'inference'
if (-not [string]::IsNullOrWhiteSpace($env:ORDER_ADAPTER_FAKE_MARKER_PATH)) {
    [IO.File]::WriteAllText($env:ORDER_ADAPTER_FAKE_MARKER_PATH, 'invoked', [Text.UTF8Encoding]::new($false))
}
if ([string]$env:ORDER_ADAPTER_FAKE_THROW -ceq '1') { throw 'fake inference failure' }
$settingsIndexes = @(
    for ($index = 0; $index -lt $args.Count; $index++) {
        if ([string]$args[$index] -ceq '--settings') { $index }
    }
)
$settingsPath = if ($settingsIndexes.Count -eq 1 -and $settingsIndexes[0] + 1 -lt $args.Count) {
    [string]$args[$settingsIndexes[0] + 1]
} else {
    ''
}
$settingsExists = -not [string]::IsNullOrWhiteSpace($settingsPath) -and (Test-Path -LiteralPath $settingsPath -PathType Leaf)
$settingsBytes = if ($settingsExists) { [IO.File]::ReadAllBytes($settingsPath) } else { [byte[]]@() }
$settingsText = if ($settingsExists) { [Text.Encoding]::UTF8.GetString($settingsBytes) } else { '' }
$settingsObject = if ($settingsExists) { $settingsText | ConvertFrom-Json } else { $null }
$capture = [ordered]@{
    cwd = (Get-Location).Path
    api_key_match = ([string]$env:ANTHROPIC_API_KEY -ceq 'test-api-key=preserves#characters')
    model_match = ([string]$env:ANTHROPIC_MODEL -ceq 'claude-test-model')
    api_key_length = ([string]$env:ANTHROPIC_API_KEY).Length
    model_length = ([string]$env:ANTHROPIC_MODEL).Length
    bus_secret_absent = [string]::IsNullOrEmpty($env:BUS_SECRET)
    alpha_secret_absent = [string]::IsNullOrEmpty($env:ALPHA_SECRET)
    openai_key_absent = [string]::IsNullOrEmpty($env:OPENAI_API_KEY)
    decoy_absent = [string]::IsNullOrEmpty($env:ORDER_SUPERVISOR_DECOY)
    subprocess_scrub_forced = ([string]$env:CLAUDE_CODE_SUBPROCESS_ENV_SCRUB -ceq '1')
    powershell_tool_process_absent = [string]::IsNullOrEmpty($env:CLAUDE_CODE_USE_POWERSHELL_TOOL)
    config_path = [string]$env:CLAUDE_CONFIG_DIR
    config_exists_during_invoke = (Test-Path -LiteralPath ([string]$env:CLAUDE_CONFIG_DIR) -PathType Container)
    settings_argument_count = $settingsIndexes.Count
    settings_path = $settingsPath
    settings_exists_during_invoke = $settingsExists
    settings_utf8_no_bom = ($settingsBytes.Length -gt 0 -and -not (
        $settingsBytes.Length -ge 3 -and $settingsBytes[0] -eq 0xEF -and
        $settingsBytes[1] -eq 0xBB -and $settingsBytes[2] -eq 0xBF
    ))
    settings_bytes = $settingsBytes.Length
    settings_text = $settingsText
    settings = $settingsObject
    argv = @($args)
}
$capture | ConvertTo-Json -Depth 8 -Compress
$requestedExitCode = 0
if (-not [string]::IsNullOrWhiteSpace($env:ORDER_ADAPTER_FAKE_EXIT_CODE)) {
    $requestedExitCode = [int]$env:ORDER_ADAPTER_FAKE_EXIT_CODE
}
exit $requestedExitCode
'@
    [IO.File]::WriteAllText($fakeClaudePath, $fakeClaude, (New-Object Text.UTF8Encoding($false)))

    $env:ANTHROPIC_API_KEY = 'inherited-api-key-must-be-overridden'
    $env:ANTHROPIC_MODEL = 'inherited-model-must-be-overridden'
    $env:CLAUDE_CODE_SUBPROCESS_ENV_SCRUB = '0'
    $env:CLAUDE_CODE_USE_POWERSHELL_TOOL = '0'
    $ambientConfigDirectory = Join-Path $tempRoot 'ambient-claude-config'
    $validConfigCapturePath = Join-Path $tempRoot 'valid-config.jsonl'
    New-Item -ItemType Directory -Path $ambientConfigDirectory | Out-Null
    $env:CLAUDE_CONFIG_DIR = $ambientConfigDirectory
    $env:ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH = $validConfigCapturePath
    try {
        $adapterRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
            New-AdapterArguments -WorkspacePath $workspace -Prefix 'valid' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot -EnvFile $adapterEnvFile
        )
        $parentAllowedEnvironmentPreserved = (
            [string]$env:ANTHROPIC_API_KEY -ceq 'inherited-api-key-must-be-overridden' -and
            [string]$env:ANTHROPIC_MODEL -ceq 'inherited-model-must-be-overridden' -and
            [string]$env:CLAUDE_CODE_SUBPROCESS_ENV_SCRUB -ceq '0' -and
            [string]$env:CLAUDE_CODE_USE_POWERSHELL_TOOL -ceq '0' -and
            [string]$env:CLAUDE_CONFIG_DIR -ceq $ambientConfigDirectory
        )
    } finally {
        [Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', $null, 'Process')
        [Environment]::SetEnvironmentVariable('ANTHROPIC_MODEL', $null, 'Process')
        [Environment]::SetEnvironmentVariable('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB', $null, 'Process')
        [Environment]::SetEnvironmentVariable('CLAUDE_CODE_USE_POWERSHELL_TOOL', $null, 'Process')
        [Environment]::SetEnvironmentVariable('CLAUDE_CONFIG_DIR', $null, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', $null, 'Process')
    }
    Assert-True 'adapter accepts absolute existing git workspace' ($adapterRun.exit_code -eq 0)
    Assert-True 'adapter child does not alter parent provider environment' $parentAllowedEnvironmentPreserved
    Assert-True 'adapter host output excludes provider values' (
        Test-TextExcludesSentinels -Text $adapterRun.output_text -Sentinels @($adapterApiKey, $adapterModel)
    )
    $adapterOutputPath = Join-Path $tempRoot 'valid-stdout.json'
    Assert-True 'fake Claude output exists' (Test-Path -LiteralPath $adapterOutputPath -PathType Leaf)
    if ($adapterRun.exit_code -eq 0 -and
        (Test-Path -LiteralPath $adapterOutputPath -PathType Leaf) -and
        (Get-Item -LiteralPath $adapterOutputPath).Length -gt 0) {
        $adapterOutput = [IO.File]::ReadAllText($adapterOutputPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $actualWorkspace = [IO.Path]::GetFullPath([string]$adapterOutput.cwd).TrimEnd('\')
        $expectedWorkspace = [IO.Path]::GetFullPath($workspace).TrimEnd('\')
        Assert-True 'fake Claude executes from exact workspace' ($actualWorkspace -ceq $expectedWorkspace)
        Assert-True 'adapter imports exact API key without emitting it' ($adapterOutput.api_key_match -is [bool] -and $adapterOutput.api_key_match)
        Assert-True 'adapter imports exact pinned model' ($adapterOutput.model_match -is [bool] -and $adapterOutput.model_match)
        Assert-True 'adapter forces credential scrub for Claude subprocesses' (
            $adapterOutput.subprocess_scrub_forced -is [bool] -and $adapterOutput.subprocess_scrub_forced
        )
        Assert-True 'adapter makes settings own PowerShell enablement' (
            $adapterOutput.powershell_tool_process_absent -is [bool] -and $adapterOutput.powershell_tool_process_absent
        )
        $validConfigRecords = @(Get-ConfigCaptureRecords -Path $validConfigCapturePath)
        Assert-True 'version and inference both see one isolated existing Claude config directory' (
            $validConfigRecords.Count -eq 2 -and
            @($validConfigRecords | Where-Object { -not [bool]$_.config_exists }).Count -eq 0 -and
            @($validConfigRecords | Select-Object -ExpandProperty config_path -Unique).Count -eq 1 -and
            [string]$adapterOutput.config_path -ceq [string]$validConfigRecords[0].config_path -and
            [bool]$adapterOutput.config_exists_during_invoke
        )
        Assert-True 'isolated Claude config overrides ambient and is removed with child markers' (
            [string]$adapterOutput.config_path -cne $ambientConfigDirectory -and
            (Test-ConfigCapturesCleaned -Records $validConfigRecords -ExpectedParent $tempRoot)
        )
        Assert-True 'adapter does not import unrelated env-file keys' (
            $adapterOutput.bus_secret_absent -and $adapterOutput.alpha_secret_absent -and
            $adapterOutput.openai_key_absent -and $adapterOutput.decoy_absent
        )
        $capturedArgvText = (@($adapterOutput.argv) -join "`n")
        Assert-True 'provider values are absent from Claude argv' (
            -not $capturedArgvText.Contains($adapterApiKey) -and -not $capturedArgvText.Contains($adapterModel)
        )
        Assert-True 'adapter EnvFile stays out of Claude argv' (-not $capturedArgvText.Contains($adapterEnvFile))
        $capturedArgv = @($adapterOutput.argv | ForEach-Object { [string]$_ })
        $settingsArgumentIndexes = @(for ($index = 0; $index -lt $capturedArgv.Count; $index++) {
            if ($capturedArgv[$index] -ceq '--settings') { $index }
        })
        $toolsArgumentIndexes = @(for ($index = 0; $index -lt $capturedArgv.Count; $index++) {
            if ($capturedArgv[$index] -ceq '--tools') { $index }
        })
        $permissionArgumentIndexes = @(for ($index = 0; $index -lt $capturedArgv.Count; $index++) {
            if ($capturedArgv[$index] -ceq '--permission-mode') { $index }
        })
        $disallowedArgumentIndexes = @(for ($index = 0; $index -lt $capturedArgv.Count; $index++) {
            if ($capturedArgv[$index] -ceq '--disallowedTools') { $index }
        })
        Assert-True 'adapter passes one exact ephemeral settings path' (
            $settingsArgumentIndexes.Count -eq 1 -and
            $settingsArgumentIndexes[0] + 1 -lt $capturedArgv.Count -and
            [IO.Path]::IsPathRooted($capturedArgv[$settingsArgumentIndexes[0] + 1]) -and
            [string]$adapterOutput.settings_path -ceq $capturedArgv[$settingsArgumentIndexes[0] + 1] -and
            [IO.Path]::GetDirectoryName([string]$adapterOutput.settings_path) -ceq [IO.Path]::GetFullPath($tempRoot)
        )
        Assert-True 'ephemeral settings exists only during Claude invocation' (
            $adapterOutput.settings_exists_during_invoke -is [bool] -and $adapterOutput.settings_exists_during_invoke -and
            -not (Test-Path -LiteralPath ([string]$adapterOutput.settings_path))
        )
        Assert-True 'ephemeral settings is bounded UTF-8 without BOM' (
            $adapterOutput.settings_utf8_no_bom -is [bool] -and $adapterOutput.settings_utf8_no_bom -and
            [int64]$adapterOutput.settings_bytes -gt 0 -and [int64]$adapterOutput.settings_bytes -le 16384
        )
        $settingsTopNames = @($adapterOutput.settings.PSObject.Properties.Name | Sort-Object)
        $settingsEnvNames = @($adapterOutput.settings.env.PSObject.Properties.Name | Sort-Object)
        $settingsPermissionNames = @($adapterOutput.settings.permissions.PSObject.Properties.Name | Sort-Object)
        Assert-True 'ephemeral settings has exact top-level shape' (
            @(Compare-Object $settingsTopNames @('env', 'permissions')).Count -eq 0 -and
            @(Compare-Object $settingsPermissionNames @('allow', 'deny')).Count -eq 0
        )
        Assert-True 'ephemeral settings owns exact scrub and PowerShell controls' (
            @(Compare-Object $settingsEnvNames @('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB', 'CLAUDE_CODE_USE_POWERSHELL_TOOL')).Count -eq 0 -and
            [string]$adapterOutput.settings.env.CLAUDE_CODE_SUBPROCESS_ENV_SCRUB -ceq '1' -and
            [string]$adapterOutput.settings.env.CLAUDE_CODE_USE_POWERSHELL_TOOL -ceq '1'
        )
        Assert-True 'ephemeral settings grants exactly three work tools' (
            @(Compare-Object @($adapterOutput.settings.permissions.allow) @('Read', 'Edit', 'PowerShell')).Count -eq 0 -and
            @($adapterOutput.settings.permissions.allow).Count -eq 3
        )
        $expectedEnvPermissionPath = ([IO.Path]::GetFullPath($adapterEnvFile) -replace '\\', '/')
        $expectedEnvPermissionPath = '//' + $expectedEnvPermissionPath.Substring(0, 1).ToLowerInvariant() + $expectedEnvPermissionPath.Substring(2).TrimEnd('/')
        Assert-True 'ephemeral settings denies the exact configured env file' (
            @(Compare-Object @($adapterOutput.settings.permissions.deny) @(
                ('Read(' + $expectedEnvPermissionPath + ')')
                ('Edit(' + $expectedEnvPermissionPath + ')')
            )).Count -eq 0 -and @($adapterOutput.settings.permissions.deny).Count -eq 2
        )
        Assert-True 'ephemeral settings contains no provider value or provider variable name' (
            Test-TextExcludesSentinels -Text ([string]$adapterOutput.settings_text) -Sentinels @(
                $adapterApiKey, $adapterModel, 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL'
            )
        )
        Assert-True 'adapter passes exact restricted tool inventory' (
            $toolsArgumentIndexes.Count -eq 1 -and
            $capturedArgv[$toolsArgumentIndexes[0] + 1] -ceq 'Read,Edit,PowerShell'
        )
        Assert-True 'adapter requests explicit manual compatibility mode' (
            $permissionArgumentIndexes.Count -eq 1 -and
            $capturedArgv[$permissionArgumentIndexes[0] + 1] -ceq 'manual'
        )
        Assert-True 'adapter disallows Bash and AskUserQuestion exactly once' (
            $disallowedArgumentIndexes.Count -eq 1 -and
            $capturedArgv[$disallowedArgumentIndexes[0] + 1] -ceq 'Bash' -and
            $capturedArgv[$disallowedArgumentIndexes[0] + 2] -ceq 'AskUserQuestion'
        )
        Assert-True 'adapter uses bare strict-MCP mode and removes safe mode' (
            @($capturedArgv | Where-Object { $_ -ceq '--bare' }).Count -eq 1 -and
            @($capturedArgv | Where-Object { $_ -ceq '--strict-mcp-config' }).Count -eq 1 -and
            @($capturedArgv | Where-Object { $_ -ceq '--safe-mode' }).Count -eq 0
        )
    }

    $env:ORDER_ADAPTER_FAKE_EXIT_CODE = '7'
    $nonzeroConfigCapturePath = Join-Path $tempRoot 'nonzero-config.jsonl'
    $env:ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH = $nonzeroConfigCapturePath
    try {
        $nonzeroRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
            New-AdapterArguments -WorkspacePath $workspace -Prefix 'nonzero' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot -EnvFile $adapterEnvFile
        )
    } finally {
        [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_EXIT_CODE', $null, 'Process')
        [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_CONFIG_CAPTURE_PATH', $null, 'Process')
    }
    $nonzeroOutputPath = Join-Path $tempRoot 'nonzero-stdout.json'
    Assert-True 'adapter preserves a nonzero Claude exit code' ($nonzeroRun.exit_code -eq 7)
    $nonzeroConfigRecords = @(Get-ConfigCaptureRecords -Path $nonzeroConfigCapturePath)
    Assert-True 'adapter removes isolated config and markers after nonzero Claude exit' (
        $nonzeroConfigRecords.Count -eq 2 -and
        (Test-ConfigCapturesCleaned -Records $nonzeroConfigRecords -ExpectedParent $tempRoot)
    )
    if (Test-Path -LiteralPath $nonzeroOutputPath -PathType Leaf) {
        $nonzeroOutput = [IO.File]::ReadAllText($nonzeroOutputPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
        Assert-True 'adapter deletes ephemeral settings after nonzero Claude exit' (
            -not [string]::IsNullOrWhiteSpace([string]$nonzeroOutput.settings_path) -and
            -not (Test-Path -LiteralPath ([string]$nonzeroOutput.settings_path))
        )
    } else {
        Assert-True 'adapter deletes ephemeral settings after nonzero Claude exit' $false
    }

    $env:ORDER_ADAPTER_FAKE_THROW = '1'
    try {
        $inferenceFailureCase = Invoke-AdapterCase `
            -Name 'inference-failure' `
            -WorkspacePath $workspace `
            -EnvFile $adapterEnvFile `
            -FakeClaudePath $fakeClaudePath `
            -TemporaryRoot $tempRoot
    } finally {
        [Environment]::SetEnvironmentVariable('ORDER_ADAPTER_FAKE_THROW', $null, 'Process')
    }
    $inferenceFailureConfigRecords = @(Get-ConfigCaptureRecords -Path ([string]$inferenceFailureCase.config_capture_path))
    Assert-True 'adapter inference failure is nonzero after fake child invocation' (
        $inferenceFailureCase.run.exit_code -ne 0 -and $inferenceFailureCase.invoked
    )
    Assert-True 'adapter removes isolated config and markers after inference failure' (
        $inferenceFailureConfigRecords.Count -eq 2 -and
        (Test-ConfigCapturesCleaned -Records $inferenceFailureConfigRecords -ExpectedParent $tempRoot)
    )

    $versionCaseSpecs = @(
        [pscustomobject]@{ name = 'version-probe-failure'; variable = 'ORDER_ADAPTER_FAKE_VERSION_EXIT_CODE'; value = '9'; code = 'claude_cli_version_probe_failed' },
        [pscustomobject]@{ name = 'unsupported-version'; variable = 'ORDER_ADAPTER_FAKE_VERSION'; value = '2.1.242 (Claude Code)'; code = 'claude_cli_version_unsupported' }
    )
    foreach ($versionCaseSpec in $versionCaseSpecs) {
        [Environment]::SetEnvironmentVariable([string]$versionCaseSpec.variable, [string]$versionCaseSpec.value, 'Process')
        try {
            $versionCase = Invoke-AdapterCase `
                -Name ([string]$versionCaseSpec.name) `
                -WorkspacePath $workspace `
                -EnvFile $adapterEnvFile `
                -FakeClaudePath $fakeClaudePath `
                -TemporaryRoot $tempRoot
        } finally {
            [Environment]::SetEnvironmentVariable([string]$versionCaseSpec.variable, $null, 'Process')
        }
        Assert-AdapterRejected `
            -Name ('adapter ' + [string]$versionCaseSpec.name) `
            -Case $versionCase `
            -ExpectedCode ([string]$versionCaseSpec.code) `
            -SecretSentinels @($adapterApiKey, $adapterModel)
    }

    $restoreAmbientRoot = Join-Path $tempRoot 'restore-ambient'
    New-Item -ItemType Directory -Path $restoreAmbientRoot | Out-Null
    $restoreAmbientMarker = Join-Path $restoreAmbientRoot 'must-survive.txt'
    [IO.File]::WriteAllText($restoreAmbientMarker, 'ambient', (New-Object Text.UTF8Encoding($false)))
    $restorationSpecs = @(
        [pscustomobject]@{ Name = 'success'; Ambient = $restoreAmbientRoot; Variable = ''; Value = ''; RecordCount = 2 },
        [pscustomobject]@{ Name = 'nonzero'; Ambient = $null; Variable = 'ORDER_ADAPTER_FAKE_EXIT_CODE'; Value = '7'; RecordCount = 2 },
        [pscustomobject]@{ Name = 'version-preflight'; Ambient = $restoreAmbientRoot; Variable = 'ORDER_ADAPTER_FAKE_VERSION_EXIT_CODE'; Value = '9'; RecordCount = 1 },
        [pscustomobject]@{ Name = 'inference-failure'; Ambient = $null; Variable = 'ORDER_ADAPTER_FAKE_THROW'; Value = '1'; RecordCount = 2 }
    )
    foreach ($restoreSpec in $restorationSpecs) {
        $restoreCase = Invoke-AdapterConfigRestorationCase `
            -Name ([string]$restoreSpec.Name) `
            -WorkspacePath $workspace `
            -EnvFile $adapterEnvFile `
            -FakeClaudePath $fakeClaudePath `
            -TemporaryRoot $tempRoot `
            -AmbientConfigDirectory $restoreSpec.Ambient `
            -FailureVariable ([string]$restoreSpec.Variable) `
            -FailureValue ([string]$restoreSpec.Value)
        Assert-True ('adapter restores ambient CLAUDE_CONFIG_DIR after ' + [string]$restoreSpec.Name) ([bool]$restoreCase.restored)
        Assert-True ('adapter cleans isolated config after ' + [string]$restoreSpec.Name) (
            @($restoreCase.records).Count -eq [int]$restoreSpec.RecordCount -and
            (Test-ConfigCapturesCleaned -Records @($restoreCase.records) -ExpectedParent $tempRoot) -and
            @($restoreCase.records | Where-Object { [string]$_.config_path -ceq [string]$restoreSpec.Ambient }).Count -eq 0
        )
    }
    Assert-True 'adapter never removes ambient Claude config directory' (
        (Test-Path -LiteralPath $restoreAmbientRoot -PathType Container) -and
        (Test-Path -LiteralPath $restoreAmbientMarker -PathType Leaf)
    )

    $adapterReparseTarget = Join-Path $tempRoot 'adapter-config-reparse-target'
    New-Item -ItemType Directory -Path $adapterReparseTarget | Out-Null
    [IO.File]::WriteAllText((Join-Path $adapterReparseTarget 'must-survive.txt'), 'target', (New-Object Text.UTF8Encoding($false)))
    $adapterCleanupFailure = Invoke-AdapterConfigRestorationCase `
        -Name 'cleanup-reparse' `
        -WorkspacePath $workspace `
        -EnvFile $adapterEnvFile `
        -FakeClaudePath $fakeClaudePath `
        -TemporaryRoot $tempRoot `
        -AmbientConfigDirectory $restoreAmbientRoot `
        -FailureVariable 'ORDER_ADAPTER_FAKE_CONFIG_REPARSE_TARGET' `
        -FailureValue $adapterReparseTarget
    $adapterCleanupRecords = @($adapterCleanupFailure.records)
    $adapterUnsafeConfig = if ($adapterCleanupRecords.Count -gt 0) { [string]$adapterCleanupRecords[0].config_path } else { '' }
    Assert-True 'adapter returns fixed isolation cleanup failure after child reparse injection' (
        [string]$adapterCleanupFailure.error -ceq 'claude_isolation_cleanup_failed'
    ) ([string]$adapterCleanupFailure.error)
    Assert-True 'adapter restores ambient config and retains unsafe owned residue' (
        [bool]$adapterCleanupFailure.restored -and
        $adapterCleanupRecords.Count -eq 2 -and
        (Test-Path -LiteralPath (Join-Path $adapterUnsafeConfig 'escape') -PathType Container)
    )
    Assert-True 'adapter reparse refusal never deletes outside target' (
        (Test-Path -LiteralPath (Join-Path $adapterReparseTarget 'must-survive.txt') -PathType Leaf) -and
        (Test-Path -LiteralPath $restoreAmbientMarker -PathType Leaf)
    )
    if (-not [string]::IsNullOrWhiteSpace($adapterUnsafeConfig) -and
        (Test-Path -LiteralPath (Join-Path $adapterUnsafeConfig 'escape'))) {
        [IO.Directory]::Delete((Join-Path $adapterUnsafeConfig 'escape'))
        Remove-Item -LiteralPath $adapterUnsafeConfig -Recurse -Force
    }

    $singleQuotedEnvFile = Join-Path $tempRoot 'single-quoted-provider.env'
    Write-TestEnvFile -Path $singleQuotedEnvFile -Text @'
ANTHROPIC_API_KEY='test-api-key=preserves#characters'
ANTHROPIC_MODEL='claude-test-model'
'@
    $singleQuotedCase = Invoke-AdapterCase `
        -Name 'single-quoted-values' `
        -WorkspacePath $workspace `
        -EnvFile $singleQuotedEnvFile `
        -FakeClaudePath $fakeClaudePath `
        -TemporaryRoot $tempRoot
    Assert-True 'adapter accepts one matched single-quote pair' (
        $singleQuotedCase.run.exit_code -eq 0 -and $singleQuotedCase.invoked
    )
    if (Test-Path -LiteralPath $singleQuotedCase.stdout_path -PathType Leaf) {
        $singleQuotedOutput = [IO.File]::ReadAllText($singleQuotedCase.stdout_path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        Assert-True 'single-quoted provider values arrive exactly' (
            $singleQuotedOutput.api_key_match -is [bool] -and $singleQuotedOutput.api_key_match -and
            $singleQuotedOutput.model_match -is [bool] -and $singleQuotedOutput.model_match
        )
    } else {
        Assert-True 'single-quoted provider values arrive exactly' $false
    }

    $envCaseRoot = Join-Path $tempRoot 'env-cases'
    New-Item -ItemType Directory -Path $envCaseRoot -Force | Out-Null
    $missingEnvFile = Join-Path $envCaseRoot 'missing.env'
    $directoryEnvFile = Join-Path $envCaseRoot 'directory.env'
    New-Item -ItemType Directory -Path $directoryEnvFile | Out-Null

    $tooLargeEnvFile = Join-Path $envCaseRoot 'too-large.env'
    Write-TestEnvFile -Path $tooLargeEnvFile -Text (
        'ANTHROPIC_API_KEY=file-size-secret-' + ('x' * 1048576) + "`nANTHROPIC_MODEL=$adapterModel"
    )

    $tooManyLinesEnvFile = Join-Path $envCaseRoot 'too-many-lines.env'
    $tooManyLines = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt 4095; $index++) { $tooManyLines.Add('# padding') }
    $tooManyLines.Add('ANTHROPIC_API_KEY=line-count-secret')
    $tooManyLines.Add('ANTHROPIC_MODEL=' + $adapterModel)
    [IO.File]::WriteAllLines($tooManyLinesEnvFile, $tooManyLines.ToArray(), (New-Object Text.UTF8Encoding($false)))

    $malformedUtf8EnvFile = Join-Path $envCaseRoot 'malformed-utf8.env'
    $invalidUtf8Bytes = New-Object System.Collections.Generic.List[byte]
    $invalidUtf8Bytes.AddRange([Text.Encoding]::ASCII.GetBytes('ANTHROPIC_API_KEY=utf8-secret-'))
    $invalidUtf8Bytes.Add([byte]0xC3)
    $invalidUtf8Bytes.Add([byte]0x28)
    $invalidUtf8Bytes.AddRange([Text.Encoding]::ASCII.GetBytes("`nANTHROPIC_MODEL=$adapterModel"))
    [IO.File]::WriteAllBytes($malformedUtf8EnvFile, $invalidUtf8Bytes.ToArray())

    $oversizedApiValue = 'oversized-api-secret-' + ('x' * 8192)
    $oversizedModelValue = 'oversized-model-secret-' + ('x' * 8192)
    $failureCaseSpecs = @(
        [pscustomobject]@{ Name = 'mandatory-env-file'; File = $adapterEnvFile; Code = 'EnvFile'; Omit = $true },
        [pscustomobject]@{ Name = 'relative-env-file'; File = '.\relative-provider.env'; Code = 'claude_env_file_path_must_be_absolute'; Omit = $false },
        [pscustomobject]@{ Name = 'missing-env-file'; File = $missingEnvFile; Code = 'claude_env_file_missing'; Omit = $false },
        [pscustomobject]@{ Name = 'directory-env-file'; File = $directoryEnvFile; Code = 'claude_env_file_missing'; Omit = $false },
        [pscustomobject]@{ Name = 'oversized-env-file'; File = $tooLargeEnvFile; Code = 'claude_env_file_too_large'; Omit = $false },
        [pscustomobject]@{ Name = 'excessive-env-lines'; File = $tooManyLinesEnvFile; Code = 'claude_env_file_too_many_lines'; Omit = $false },
        [pscustomobject]@{ Name = 'malformed-utf8'; File = $malformedUtf8EnvFile; Code = 'claude_env_file_utf8_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'missing-api-key'; Text = "ANTHROPIC_MODEL=$adapterModel"; Code = 'anthropic_api_key_missing'; Omit = $false },
        [pscustomobject]@{ Name = 'missing-model'; Text = 'ANTHROPIC_API_KEY=missing-model-secret'; Code = 'anthropic_model_missing'; Omit = $false },
        [pscustomobject]@{ Name = 'blank-api-key'; Text = "ANTHROPIC_API_KEY=`"`"`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_value_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'blank-model'; Text = "ANTHROPIC_API_KEY=blank-model-secret`nANTHROPIC_MODEL='   '"; Code = 'claude_env_value_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'duplicate-api-key'; Text = "ANTHROPIC_API_KEY=duplicate-secret-one`nANTHROPIC_API_KEY=duplicate-secret-two`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_duplicate_key'; Omit = $false },
        [pscustomobject]@{ Name = 'duplicate-model'; Text = "ANTHROPIC_API_KEY=duplicate-model-secret`nANTHROPIC_MODEL=model-one`nANTHROPIC_MODEL=model-two"; Code = 'claude_env_duplicate_key'; Omit = $false },
        [pscustomobject]@{ Name = 'api-key-case'; Text = "Anthropic_API_KEY=case-secret`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_key_case_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'model-case'; Text = "ANTHROPIC_API_KEY=model-case-secret`nanthropic_model=$adapterModel"; Code = 'claude_env_key_case_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'malformed-api-key'; Text = "ANTHROPIC_API_KEY malformed-secret`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_line_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'malformed-model'; Text = "ANTHROPIC_API_KEY=malformed-model-secret`nANTHROPIC_MODEL malformed-model"; Code = 'claude_env_line_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'opening-quote-only'; Text = "ANTHROPIC_API_KEY=`"opening-quote-secret`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_quote_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'closing-quote-only'; Text = "ANTHROPIC_API_KEY=closing-quote-secret`"`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_quote_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'mixed-quotes'; Text = "ANTHROPIC_API_KEY='mixed-quote-secret`"`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_quote_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'control-character'; Text = "ANTHROPIC_API_KEY=control-secret`tvalue`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_value_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'oversized-api-value'; Text = "ANTHROPIC_API_KEY=$oversizedApiValue`nANTHROPIC_MODEL=$adapterModel"; Code = 'claude_env_value_invalid'; Omit = $false },
        [pscustomobject]@{ Name = 'oversized-model-value'; Text = "ANTHROPIC_API_KEY=oversized-model-api-secret`nANTHROPIC_MODEL=$oversizedModelValue"; Code = 'claude_env_value_invalid'; Omit = $false }
    )
    $secretSentinels = @(
        $adapterApiKey, 'must-not-be-imported', 'inherited-api-key-must-be-overridden',
        'file-size-secret-', 'line-count-secret', 'missing-model-secret', 'blank-model-secret',
        'utf8-secret-',
        'duplicate-secret-', 'duplicate-model-secret', 'case-secret', 'model-case-secret',
        'malformed-secret', 'malformed-model-secret', 'opening-quote-secret',
        'closing-quote-secret', 'mixed-quote-secret', 'control-secret',
        'oversized-api-secret-', 'oversized-model-secret-'
    )

    foreach ($spec in $failureCaseSpecs) {
        $fileProperty = $spec.PSObject.Properties['File']
        $caseEnvFile = if ($null -eq $fileProperty) { '' } else { [string]$fileProperty.Value }
        if ([string]::IsNullOrWhiteSpace($caseEnvFile)) {
            $caseEnvFile = Join-Path $envCaseRoot ([string]$spec.Name + '.env')
            $textProperty = $spec.PSObject.Properties['Text']
            Write-TestEnvFile -Path $caseEnvFile -Text ([string]$textProperty.Value)
        }
        $adapterCase = Invoke-AdapterCase `
            -Name ([string]$spec.Name) `
            -WorkspacePath $workspace `
            -EnvFile $caseEnvFile `
            -FakeClaudePath $fakeClaudePath `
            -TemporaryRoot $tempRoot `
            -OmitEnvFile:([bool]$spec.Omit)
        Assert-AdapterRejected `
            -Name ('adapter ' + [string]$spec.Name) `
            -Case $adapterCase `
            -ExpectedCode ([string]$spec.Code) `
            -SecretSentinels $secretSentinels
    }

    foreach ($conflictName in $providerConflictNames) {
        $conflictValue = 'provider-conflict-secret-' + $conflictName
        [Environment]::SetEnvironmentVariable($conflictName, $conflictValue, 'Process')
        try {
            $conflictCase = Invoke-AdapterCase `
                -Name ('conflict-' + $conflictName) `
                -WorkspacePath $workspace `
                -EnvFile $adapterEnvFile `
                -FakeClaudePath $fakeClaudePath `
                -TemporaryRoot $tempRoot
        } finally {
            [Environment]::SetEnvironmentVariable($conflictName, $null, 'Process')
        }
        Assert-AdapterRejected `
            -Name ('adapter conflict ' + $conflictName) `
            -Case $conflictCase `
            -ExpectedCode 'claude_provider_environment_conflict' `
            -SecretSentinels @($adapterApiKey, $conflictValue)
    }

    $maxFileSizeEnvFile = Join-Path $envCaseRoot 'max-file-size.env'
    $maxFileSizePrefix = "ANTHROPIC_API_KEY=$adapterApiKey`nANTHROPIC_MODEL=$adapterModel`n#"
    $maxFileSizeText = $maxFileSizePrefix + ('x' * (1048576 - $maxFileSizePrefix.Length))
    Write-TestEnvFile -Path $maxFileSizeEnvFile -Text $maxFileSizeText
    $maxFileSizeCase = Invoke-AdapterCase `
        -Name 'max-file-size' `
        -WorkspacePath $workspace `
        -EnvFile $maxFileSizeEnvFile `
        -FakeClaudePath $fakeClaudePath `
        -TemporaryRoot $tempRoot
    Assert-True 'adapter accepts exactly 1048576-byte env file' (
        (Get-Item -LiteralPath $maxFileSizeEnvFile).Length -eq 1048576 -and
        $maxFileSizeCase.run.exit_code -eq 0 -and $maxFileSizeCase.invoked
    )

    $maxLineCountEnvFile = Join-Path $envCaseRoot 'max-line-count.env'
    $maxLineCount = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt 4094; $index++) { $maxLineCount.Add('# padding') }
    $maxLineCount.Add('ANTHROPIC_API_KEY=' + $adapterApiKey)
    $maxLineCount.Add('ANTHROPIC_MODEL=' + $adapterModel)
    Write-TestEnvFile -Path $maxLineCountEnvFile -Text ($maxLineCount.ToArray() -join "`n")
    $maxLineCountCase = Invoke-AdapterCase `
        -Name 'max-line-count' `
        -WorkspacePath $workspace `
        -EnvFile $maxLineCountEnvFile `
        -FakeClaudePath $fakeClaudePath `
        -TemporaryRoot $tempRoot
    Assert-True 'adapter accepts exactly 4096 env-file lines' (
        ([IO.File]::ReadAllLines($maxLineCountEnvFile, [Text.Encoding]::UTF8)).Count -eq 4096 -and
        $maxLineCountCase.run.exit_code -eq 0 -and $maxLineCountCase.invoked
    )

    $maxApiValueEnvFile = Join-Path $envCaseRoot 'max-api-value.env'
    Write-TestEnvFile -Path $maxApiValueEnvFile -Text (
        'ANTHROPIC_API_KEY=' + ('a' * 8192) + "`nANTHROPIC_MODEL=$adapterModel"
    )
    $maxApiValueCase = Invoke-AdapterCase `
        -Name 'max-api-value' `
        -WorkspacePath $workspace `
        -EnvFile $maxApiValueEnvFile `
        -FakeClaudePath $fakeClaudePath `
        -TemporaryRoot $tempRoot
    Assert-True 'adapter accepts exactly 8192-character API key' (
        $maxApiValueCase.run.exit_code -eq 0 -and $maxApiValueCase.invoked
    )
    if (Test-Path -LiteralPath $maxApiValueCase.stdout_path -PathType Leaf) {
        $maxApiValueOutput = [IO.File]::ReadAllText($maxApiValueCase.stdout_path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        Assert-True 'fake Claude receives exactly 8192 API-key characters' (
            [int]$maxApiValueOutput.api_key_length -eq 8192
        )
    } else {
        Assert-True 'fake Claude receives exactly 8192 API-key characters' $false
    }

    $maxModelValueEnvFile = Join-Path $envCaseRoot 'max-model-value.env'
    Write-TestEnvFile -Path $maxModelValueEnvFile -Text (
        "ANTHROPIC_API_KEY=$adapterApiKey`nANTHROPIC_MODEL=" + ('m' * 8192)
    )
    $maxModelValueCase = Invoke-AdapterCase `
        -Name 'max-model-value' `
        -WorkspacePath $workspace `
        -EnvFile $maxModelValueEnvFile `
        -FakeClaudePath $fakeClaudePath `
        -TemporaryRoot $tempRoot
    Assert-True 'adapter accepts exactly 8192-character model value' (
        $maxModelValueCase.run.exit_code -eq 0 -and $maxModelValueCase.invoked
    )
    if (Test-Path -LiteralPath $maxModelValueCase.stdout_path -PathType Leaf) {
        $maxModelValueOutput = [IO.File]::ReadAllText($maxModelValueCase.stdout_path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        Assert-True 'fake Claude receives exactly 8192 model characters' (
            [int]$maxModelValueOutput.model_length -eq 8192
        )
    } else {
        Assert-True 'fake Claude receives exactly 8192 model characters' $false
    }

    $relativeRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
        New-AdapterArguments -WorkspacePath '.\relative' -Prefix 'relative' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot -EnvFile $adapterEnvFile
    )
    Assert-True 'adapter rejects relative workspace' (
        $relativeRun.exit_code -ne 0 -and $relativeRun.output_text.Contains('workspace_path_must_be_absolute')
    )

    $missingWorkspace = Join-Path $tempRoot 'missing-checkout'
    $missingRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
        New-AdapterArguments -WorkspacePath $missingWorkspace -Prefix 'missing' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot -EnvFile $adapterEnvFile
    )
    Assert-True 'adapter rejects missing workspace' (
        $missingRun.exit_code -ne 0 -and $missingRun.output_text.Contains('workspace_path_missing')
    )

    $nonGitRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
        New-AdapterArguments -WorkspacePath $nonGitWorkspace -Prefix 'nongit' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot -EnvFile $adapterEnvFile
    )
    Assert-True 'adapter rejects workspace without git directory' (
        $nonGitRun.exit_code -ne 0 -and $nonGitRun.output_text.Contains('execute_workspace_git_directory_missing')
    )

    $runnerSource = [IO.File]::ReadAllText($RunnerPath, [Text.Encoding]::UTF8)
    $adapterSource = [IO.File]::ReadAllText($AdapterPath, [Text.Encoding]::UTF8)
    $adapterTokens = $null
    $adapterErrors = $null
    $adapterAst = [Management.Automation.Language.Parser]::ParseFile(
        $AdapterPath,
        [ref]$adapterTokens,
        [ref]$adapterErrors
    )
    Assert-True 'adapter parses before config ownership extraction' (@($adapterErrors).Count -eq 0)
    foreach ($adapterFunctionName in @('Test-ReparsePoint', 'Assert-OwnedClaudeConfigDirectory', 'Test-TreeContainsReparsePoint', 'Remove-OwnedClaudeConfigDirectory')) {
        $adapterFunctions = @($adapterAst.FindAll({
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -ceq $adapterFunctionName
        }, $true))
        Assert-True ('adapter defines one executable ' + $adapterFunctionName) ($adapterFunctions.Count -eq 1)
        if ($adapterFunctions.Count -eq 1) { Invoke-Expression $adapterFunctions[0].Extent.Text }
    }
    $adapterHelperParent = Join-Path $tempRoot 'adapter-helper-parent'
    $adapterHelperOutside = Join-Path $tempRoot ('claude-config-' + ('d' * 32))
    New-Item -ItemType Directory -Path $adapterHelperParent, $adapterHelperOutside | Out-Null
    [IO.File]::WriteAllText((Join-Path $adapterHelperOutside 'must-survive.txt'), 'outside', (New-Object Text.UTF8Encoding($false)))
    $adapterOutsideRejected = $false
    try {
        $null = Assert-OwnedClaudeConfigDirectory -Path $adapterHelperOutside -ParentPath $adapterHelperParent
    } catch {
        $adapterOutsideRejected = [string]$_.Exception.Message -ceq 'claude_config_directory_not_owned'
    }
    Assert-True 'adapter ownership helper rejects and preserves an outside directory' (
        $adapterOutsideRejected -and
        (Test-Path -LiteralPath (Join-Path $adapterHelperOutside 'must-survive.txt') -PathType Leaf)
    )
    $adapterHelperTarget = Join-Path $tempRoot 'adapter-helper-target'
    $adapterHelperJunction = Join-Path $adapterHelperParent ('claude-config-' + ('e' * 32))
    New-Item -ItemType Directory -Path $adapterHelperTarget | Out-Null
    [IO.File]::WriteAllText((Join-Path $adapterHelperTarget 'must-survive.txt'), 'target', (New-Object Text.UTF8Encoding($false)))
    New-Item -ItemType Junction -Path $adapterHelperJunction -Target $adapterHelperTarget | Out-Null
    $adapterJunctionRejected = $false
    try {
        $null = Assert-OwnedClaudeConfigDirectory -Path $adapterHelperJunction -ParentPath $adapterHelperParent
    } catch {
        $adapterJunctionRejected = [string]$_.Exception.Message -ceq 'claude_config_directory_unsafe'
    }
    Assert-True 'adapter ownership helper rejects a config junction without target deletion' (
        $adapterJunctionRejected -and
        (Test-Path -LiteralPath (Join-Path $adapterHelperTarget 'must-survive.txt') -PathType Leaf)
    )
    [IO.Directory]::Delete($adapterHelperJunction)
    $providerBlock = [regex]::Match(
        $adapterSource,
        '(?s)\$providerConflictNames\s*=\s*@\((?<body>.*?)\)'
    )
    $actualProviderConflictNames = @(
        [regex]::Matches($providerBlock.Groups['body'].Value, "'(?<name>[A-Z][A-Z0-9_]*)'") |
            ForEach-Object { [string]$_.Groups['name'].Value }
    )
    $providerContractMatches = $providerBlock.Success -and
        $actualProviderConflictNames.Count -eq $providerConflictNames.Count
    if ($providerContractMatches) {
        for ($index = 0; $index -lt $providerConflictNames.Count; $index++) {
            if ($actualProviderConflictNames[$index] -cne $providerConflictNames[$index]) {
                $providerContractMatches = $false
                break
            }
        }
    }
    Assert-True 'provider-conflict test table exactly matches adapter contract' $providerContractMatches

    $allowedBlock = [regex]::Match(
        $adapterSource,
        '(?s)\$allowedEnvironmentNames\s*=\s*@\((?<body>.*?)\)'
    )
    $actualAllowedEnvironmentNames = @(
        [regex]::Matches($allowedBlock.Groups['body'].Value, "'(?<name>[A-Z][A-Z0-9_]*)'") |
            ForEach-Object { [string]$_.Groups['name'].Value }
    )
    Assert-True 'adapter allowlist remains exactly API key and model' (
        $allowedBlock.Success -and $actualAllowedEnvironmentNames.Count -eq 2 -and
        $actualAllowedEnvironmentNames[0] -ceq 'ANTHROPIC_API_KEY' -and
        $actualAllowedEnvironmentNames[1] -ceq 'ANTHROPIC_MODEL'
    )
    Assert-True 'runner forwards exact EnvFile path to Claude adapter' (
        $runnerSource.Contains("'-EnvFile', (Quote-ProcessArgument `$EnvFile)")
    )
    Assert-True 'runner never expands EnvFile contents into adapter argv' (
        -not $runnerSource.Contains('[IO.File]::ReadAllText($EnvFile)') -and
        -not $runnerSource.Contains('Get-Content -LiteralPath $EnvFile')
    )
    Assert-True 'runner waits for tree termination and recursively cleans the owned run directory' (
        $runnerSource.Contains('$process.WaitForExit(15000)') -and
        $runnerSource.Contains('Remove-OwnedRunDirectory -Path $tempRoot -ParentPath $runParent -ExpectedName $runDirectoryName')
    )
    Assert-True 'adapter never adopts a pre-existing config-directory collision for cleanup' (
        $adapterSource.Contains('$configDirectoryCreated = $false') -and
        $adapterSource.Contains('$configDirectoryCreated = $true') -and
        $adapterSource.Contains('if ($configDirectoryCreated) {')
    )

    $runnerTokens = $null
    $runnerErrors = $null
    $runnerAst = [Management.Automation.Language.Parser]::ParseFile(
        $RunnerPath,
        [ref]$runnerTokens,
        [ref]$runnerErrors
    )
    Assert-True 'runner parses before owned cleanup extraction' (@($runnerErrors).Count -eq 0)
    foreach ($cleanupFunctionName in @('Test-RunTreeContainsReparsePoint', 'Remove-OwnedRunDirectory')) {
        $cleanupFunctions = @($runnerAst.FindAll({
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -ceq $cleanupFunctionName
        }, $true))
        Assert-True ('runner defines one ' + $cleanupFunctionName + ' helper') ($cleanupFunctions.Count -eq 1)
        if ($cleanupFunctions.Count -eq 1) { Invoke-Expression $cleanupFunctions[0].Extent.Text }
    }
    $cleanupParent = Join-Path $tempRoot 'owned-run-cleanup'
    $ownedRunName = 'run-' + ('a' * 32)
    $ownedRunPath = Join-Path $cleanupParent $ownedRunName
    $nestedRunPath = Join-Path $ownedRunPath 'claude-config-test\nested'
    New-Item -ItemType Directory -Path $nestedRunPath -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $nestedRunPath 'marker.txt'), 'marker', (New-Object Text.UTF8Encoding($false)))
    Remove-OwnedRunDirectory -Path $ownedRunPath -ParentPath $cleanupParent -ExpectedName $ownedRunName
    Assert-True 'owned run cleanup removes nested timeout residue recursively' (-not (Test-Path -LiteralPath $ownedRunPath))

    $outsideRunPath = Join-Path $tempRoot 'outside-run-must-survive'
    New-Item -ItemType Directory -Path $outsideRunPath | Out-Null
    [IO.File]::WriteAllText((Join-Path $outsideRunPath 'marker.txt'), 'marker', (New-Object Text.UTF8Encoding($false)))
    $outsideRejected = $false
    try {
        Remove-OwnedRunDirectory -Path $outsideRunPath -ParentPath $cleanupParent -ExpectedName ('run-' + ('b' * 32))
    } catch {
        $outsideRejected = [string]$_.Exception.Message -ceq 'claude_run_directory_not_owned'
    }
    Assert-True 'owned run cleanup rejects and preserves an outside sibling' (
        $outsideRejected -and (Test-Path -LiteralPath (Join-Path $outsideRunPath 'marker.txt') -PathType Leaf)
    )

    $junctionTarget = Join-Path $tempRoot 'junction-target'
    $junctionParent = Join-Path $tempRoot 'junction-parent'
    $junctionRunName = 'run-' + ('c' * 32)
    $junctionRunPath = Join-Path $junctionTarget $junctionRunName
    New-Item -ItemType Directory -Path $junctionRunPath -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $junctionRunPath 'marker.txt'), 'marker', (New-Object Text.UTF8Encoding($false)))
    New-Item -ItemType Junction -Path $junctionParent -Target $junctionTarget | Out-Null
    $junctionRejected = $false
    try {
        Remove-OwnedRunDirectory `
            -Path (Join-Path $junctionParent $junctionRunName) `
            -ParentPath $junctionParent `
            -ExpectedName $junctionRunName
    } catch {
        $junctionRejected = [string]$_.Exception.Message -ceq 'claude_run_directory_unsafe'
    }
    Assert-True 'owned run cleanup rejects a reparse-point parent without traversing it' (
        $junctionRejected -and (Test-Path -LiteralPath (Join-Path $junctionRunPath 'marker.txt') -PathType Leaf)
    )
    [IO.Directory]::Delete($junctionParent)

    $runnerBase = @(
        '-Mode', 'Execute',
        '-BoardFixturePath', $FixturePath,
        '-EnvFile', $adapterEnvFile,
        '-StatePath', (Join-Path $tempRoot 'runner-state.json'),
        '-LogPath', (Join-Path $tempRoot 'runner-events.jsonl')
    )
    $runnerMissing = Invoke-PowerShellChild -ScriptPath $RunnerPath -Arguments $runnerBase
    Assert-True 'runner Execute requires explicit workspace' (
        $runnerMissing.exit_code -eq 20 -and
        $runnerMissing.output_text.Contains('EXECUTE_REQUIRES_EXPLICIT_WORKSPACE_PATH')
    )

    $runnerRelative = Invoke-PowerShellChild -ScriptPath $RunnerPath -Arguments (
        $runnerBase + @('-WorkspacePath', '.\relative')
    )
    Assert-True 'runner rejects relative workspace' (
        $runnerRelative.exit_code -eq 20 -and
        $runnerRelative.output_text.Contains('WORKSPACE_PATH_MUST_BE_ABSOLUTE')
    )

    $runnerNonGit = Invoke-PowerShellChild -ScriptPath $RunnerPath -Arguments (
        $runnerBase + @('-WorkspacePath', $nonGitWorkspace)
    )
    Assert-True 'runner Execute rejects workspace without git directory' (
        $runnerNonGit.exit_code -eq 20 -and
        $runnerNonGit.output_text.Contains('EXECUTE_WORKSPACE_GIT_DIRECTORY_MISSING')
    )

    $runnerValidWorkspace = Invoke-PowerShellChild -ScriptPath $RunnerPath -Arguments (
        $runnerBase + @('-WorkspacePath', $workspace)
    )
    Assert-True 'runner accepts workspace boundary before fixture gate' (
        $runnerValidWorkspace.exit_code -eq 20 -and
        $runnerValidWorkspace.output_text.Contains('FIXTURE_EXECUTE_FORBIDDEN')
    )

    $observeRun = Invoke-PowerShellChild -ScriptPath $RunnerPath -Arguments @(
        '-Mode', 'Observe',
        '-BoardFixturePath', $FixturePath,
        '-StatePath', (Join-Path $tempRoot 'observe-state.json'),
        '-LogPath', (Join-Path $tempRoot 'observe-events.jsonl')
    )
    Assert-True 'Observe remains compatible without explicit workspace' (
        $observeRun.exit_code -eq 0 -and $observeRun.output_text.Contains('tail_seeded')
    )

    $installerText = [IO.File]::ReadAllText($InstallerPath, [Text.Encoding]::UTF8)
    Assert-True 'installer exposes WorkspacePath parameter' ($installerText.Contains('[string]$WorkspacePath'))
    Assert-True 'installer pins a bounded 720 second wall timeout' (
        $installerText.Contains('[ValidateRange(30, 840)][int]$WallTimeoutSeconds = 720')
    )
    Assert-True 'installer passes exact WorkspacePath argument' (
        $installerText.Contains("('-WorkspacePath `"' + `$WorkspacePath + '`"')")
    )
    Assert-True 'installer passes explicit invariant wall timeout argument' (
        $installerText.Contains("('-WallTimeoutSeconds ' + [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', `$WallTimeoutSeconds))")
    )
    Assert-True 'installer task starts in WorkspacePath' ($installerText.Contains('-WorkingDirectory $WorkspacePath'))
    Assert-True 'installer drift check reads back WorkspacePath' ($installerText.Contains('WorkingDirectory -cne $WorkspacePath'))
    Assert-True 'installer Status exposes workspace path' (
        $installerText.Contains('workspace_path = $WorkspacePath') -and
        $installerText.Contains('workspace_exists =')
    )
    Assert-True 'installer Status exposes timeout configuration and exact readback' (
        $installerText.Contains('wall_timeout_seconds = $WallTimeoutSeconds') -and
        $installerText.Contains('wall_timeout_readback = $wallTimeoutReadback')
    )
    Assert-True 'installer drift check rejects an unconfirmed timeout argument' (
        $installerText.Contains("`$problems.Add('wall_timeout_seconds')") -and
        $installerText.Contains('Get-WallTimeoutReadback -Arguments ([string]$Task.Actions[0].Arguments)')
    )
    Assert-True 'installer Execute requires explicit workspace' ($installerText.Contains('execute_requires_explicit_workspace_path'))
    Assert-True 'installer Execute requires git directory' ($installerText.Contains('execute_workspace_git_directory_missing'))

    # Execute the installer's actual Task Scheduler argument serializer through
    # powershell.exe. This catches PowerShell's comma/operator-precedence trap:
    # unparenthesized concatenations inside @() become separate array elements,
    # which inserts spaces inside quoted path values when the array is joined.
    $installerTokens = $null
    $installerErrors = $null
    $installerAst = [Management.Automation.Language.Parser]::ParseFile(
        $InstallerPath,
        [ref]$installerTokens,
        [ref]$installerErrors
    )
    Assert-True 'installer parses before serializer extraction' (@($installerErrors).Count -eq 0)
    $argumentFunction = @($installerAst.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Get-TaskArguments'
    }, $true))
    Assert-True 'installer defines one task argument serializer' ($argumentFunction.Count -eq 1)
    if ($argumentFunction.Count -eq 1) {
        Invoke-Expression $argumentFunction[0].Extent.Text

        $timeoutReadbackFunction = @($installerAst.FindAll({
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -ceq 'Get-WallTimeoutReadback'
        }, $true))
        Assert-True 'installer defines one timeout readback validator' ($timeoutReadbackFunction.Count -eq 1)
        if ($timeoutReadbackFunction.Count -eq 1) {
            Invoke-Expression $timeoutReadbackFunction[0].Extent.Text
        }

        $taskProbePath = Join-Path $tempRoot 'capture task arguments.ps1'
        $taskProbeText = @'
param(
    [string]$Mode,
    [string]$AllowedSourcesCsv,
    [string]$UserProfilePath,
    [string]$WorkspacePath,
    [string]$EnvFile,
    [string]$StatePath,
    [string]$LogPath,
    [int]$WallTimeoutSeconds,
    [string]$ClaudeCommand
)
[ordered]@{
    mode = $Mode
    allowed_sources = $AllowedSourcesCsv
    user_profile = $UserProfilePath
    workspace = $WorkspacePath
    env_file = $EnvFile
    state_path = $StatePath
    log_path = $LogPath
    wall_timeout_seconds = $WallTimeoutSeconds
    claude_command = $ClaudeCommand
} | ConvertTo-Json -Compress
'@
        [IO.File]::WriteAllText($taskProbePath, $taskProbeText, (New-Object Text.UTF8Encoding($false)))

        $RunnerPath = $taskProbePath
        $Mode = 'Observe'
        $UserProfilePath = Join-Path $tempRoot 'Profile With Space'
        $WorkspacePath = $workspace
        $EnvFile = Join-Path $workspace 'bus config.env'
        $StatePath = Join-Path $tempRoot 'task state.json'
        $LogPath = Join-Path $tempRoot 'task events.jsonl'
        $WallTimeoutSeconds = 720
        $ClaudeCommand = 'C:\Program Files\Claude\claude.exe'
        $serializedArguments = Get-TaskArguments

        Assert-True 'task serializer keeps File path exact inside quotes' (
            $serializedArguments.Contains('-File "' + $RunnerPath + '"')
        )
        Assert-True 'task serializer keeps profile path exact inside quotes' (
            $serializedArguments.Contains('-UserProfilePath "' + $UserProfilePath + '"')
        )

        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $psi.Arguments = $serializedArguments
        $psi.WorkingDirectory = $WorkspacePath
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $taskProbe = New-Object Diagnostics.Process
        $taskProbe.StartInfo = $psi
        $taskProbe.Start() | Out-Null
        $taskProbeStdout = $taskProbe.StandardOutput.ReadToEnd()
        $taskProbeStderr = $taskProbe.StandardError.ReadToEnd()
        $taskProbe.WaitForExit()
        Assert-True 'serialized task action starts successfully' (
            $taskProbe.ExitCode -eq 0 -and [string]::IsNullOrWhiteSpace($taskProbeStderr)
        )
        if ($taskProbe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($taskProbeStdout)) {
            $captured = $taskProbeStdout | ConvertFrom-Json
            Assert-True 'serialized task action preserves every value exactly' (
                [string]$captured.mode -ceq $Mode -and
                [string]$captured.allowed_sources -ceq 'chat-mobile,codex' -and
                [string]$captured.user_profile -ceq $UserProfilePath -and
                [string]$captured.workspace -ceq $WorkspacePath -and
                [string]$captured.env_file -ceq $EnvFile -and
                [string]$captured.state_path -ceq $StatePath -and
                [string]$captured.log_path -ceq $LogPath -and
                [int]$captured.wall_timeout_seconds -eq $WallTimeoutSeconds -and
                [string]$captured.claude_command -ceq $ClaudeCommand
            )
        }

        if ($timeoutReadbackFunction.Count -eq 1) {
            $exactTimeout = Get-WallTimeoutReadback -Arguments $serializedArguments
            $wrongTimeout = Get-WallTimeoutReadback -Arguments ($serializedArguments.Replace('-WallTimeoutSeconds 720', '-WallTimeoutSeconds 300'))
            $duplicateTimeout = Get-WallTimeoutReadback -Arguments ($serializedArguments + ' -WallTimeoutSeconds 720')
            $wrongCaseTimeout = Get-WallTimeoutReadback -Arguments ($serializedArguments.Replace('-WallTimeoutSeconds 720', '-walltimeoutseconds 720'))
            Assert-True 'timeout readback confirms one exact configured value' (
                [bool]$exactTimeout.confirmed -and
                [string]$exactTimeout.expected -ceq '720' -and
                [string]$exactTimeout.actual -ceq '720' -and
                [int]$exactTimeout.occurrence_count -eq 1
            )
            Assert-True 'timeout readback rejects wrong duplicate and case-changed arguments' (
                -not [bool]$wrongTimeout.confirmed -and
                -not [bool]$duplicateTimeout.confirmed -and
                -not [bool]$wrongCaseTimeout.confirmed
            )
        }
    }

    $onboardingText = [IO.File]::ReadAllText((Join-Path $RepoRoot 'docs\ONBOARDING.md'), [Text.Encoding]::UTF8)
    Assert-True 'ORDER protocol requires one evidence domain per task' (
        $onboardingText.Contains('One executable ORDER covers exactly one evidence domain.') -and
        $onboardingText.Contains('do not bundle those domains into one') -and
        $onboardingText.Contains('research task.')
    )
} finally {
    foreach ($name in $testEnvironmentNames) {
        if ($null -eq $testEnvironmentBackup[$name]) {
            Remove-Item -LiteralPath ('Env:\' + $name) -Force -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $testEnvironmentBackup[$name], 'Process')
        }
    }
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $tempRoot -PathType Container)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
