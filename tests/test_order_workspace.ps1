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
        [Parameter(Mandatory = $true)][string]$TemporaryRoot
    )

    return @(
        '-PromptPath', (Join-Path $TemporaryRoot 'prompt.txt'),
        '-SchemaPath', $SchemaPath,
        '-StdoutPath', (Join-Path $TemporaryRoot ($Prefix + '-stdout.json')),
        '-StderrPath', (Join-Path $TemporaryRoot ($Prefix + '-stderr.txt')),
        '-WorkspacePath', $WorkspacePath,
        '-ClaudeCommand', $FakeClaudePath,
        '-MaxBudgetUsd', '0.01'
    )
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-workspace-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
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

    $fakeClaudePath = Join-Path $tempRoot 'fake-claude.cmd'
    $fakeClaude = @'
@echo off
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "[Console]::Out.Write(([ordered]@{cwd=(Get-Location).Path} | ConvertTo-Json -Compress))"
exit /b 0
'@
    [IO.File]::WriteAllText($fakeClaudePath, $fakeClaude, (New-Object Text.UTF8Encoding($false)))

    $adapterRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
        New-AdapterArguments -WorkspacePath $workspace -Prefix 'valid' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot
    )
    Assert-True 'adapter accepts absolute existing git workspace' ($adapterRun.exit_code -eq 0)
    $adapterOutputPath = Join-Path $tempRoot 'valid-stdout.json'
    Assert-True 'fake Claude output exists' (Test-Path -LiteralPath $adapterOutputPath -PathType Leaf)
    if ($adapterRun.exit_code -eq 0 -and
        (Test-Path -LiteralPath $adapterOutputPath -PathType Leaf) -and
        (Get-Item -LiteralPath $adapterOutputPath).Length -gt 0) {
        $adapterOutput = [IO.File]::ReadAllText($adapterOutputPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $actualWorkspace = [IO.Path]::GetFullPath([string]$adapterOutput.cwd).TrimEnd('\')
        $expectedWorkspace = [IO.Path]::GetFullPath($workspace).TrimEnd('\')
        Assert-True 'fake Claude executes from exact workspace' ($actualWorkspace -ceq $expectedWorkspace)
    }

    $relativeRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
        New-AdapterArguments -WorkspacePath '.\relative' -Prefix 'relative' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot
    )
    Assert-True 'adapter rejects relative workspace' (
        $relativeRun.exit_code -ne 0 -and $relativeRun.output_text.Contains('workspace_path_must_be_absolute')
    )

    $missingWorkspace = Join-Path $tempRoot 'missing-checkout'
    $missingRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
        New-AdapterArguments -WorkspacePath $missingWorkspace -Prefix 'missing' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot
    )
    Assert-True 'adapter rejects missing workspace' (
        $missingRun.exit_code -ne 0 -and $missingRun.output_text.Contains('workspace_path_missing')
    )

    $nonGitRun = Invoke-PowerShellChild -ScriptPath $AdapterPath -Arguments (
        New-AdapterArguments -WorkspacePath $nonGitWorkspace -Prefix 'nongit' -FakeClaudePath $fakeClaudePath -TemporaryRoot $tempRoot
    )
    Assert-True 'adapter rejects workspace without git directory' (
        $nonGitRun.exit_code -ne 0 -and $nonGitRun.output_text.Contains('execute_workspace_git_directory_missing')
    )

    $runnerBase = @(
        '-Mode', 'Execute',
        '-BoardFixturePath', $FixturePath,
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
    Assert-True 'installer passes exact WorkspacePath argument' (
        $installerText.Contains("('-WorkspacePath `"' + `$WorkspacePath + '`"')")
    )
    Assert-True 'installer task starts in WorkspacePath' ($installerText.Contains('-WorkingDirectory $WorkspacePath'))
    Assert-True 'installer drift check reads back WorkspacePath' ($installerText.Contains('WorkingDirectory -cne $WorkspacePath'))
    Assert-True 'installer Status exposes workspace path' (
        $installerText.Contains('workspace_path = $WorkspacePath') -and
        $installerText.Contains('workspace_exists =')
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
                [string]$captured.claude_command -ceq $ClaudeCommand
            )
        }
    }
} finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $tempRoot -PathType Container)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
