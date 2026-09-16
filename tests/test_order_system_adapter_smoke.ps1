#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$script:SourceSmoke = Join-Path $script:RepoRoot 'infra\azure\order_system_adapter_smoke.ps1'
$script:RequiredFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)
$script:ReleaseId = '1111111111111111111111111111111111111111'
$script:ArchiveSha256 = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
$script:PassCount = 0
$script:FailCount = 0
$script:SuiteRoot = Join-Path ([IO.Path]::GetTempPath()) ('blackboard-order-system-smoke-tests-' + [Guid]::NewGuid().ToString('N'))
$script:ReleaseRoot = Join-Path $script:SuiteRoot 'releases'
$script:ReleasePath = Join-Path $script:ReleaseRoot $script:ReleaseId
$script:ProfilePath = Join-Path $script:SuiteRoot 'profile'
$script:WorkspacePath = Join-Path $script:SuiteRoot 'workspace'
$script:MetadataPath = Join-Path $script:SuiteRoot 'metadata'
$script:EnvPath = Join-Path $script:WorkspacePath '.env'
$script:StatePath = Join-Path $script:MetadataPath 'state.json'
$script:LogPath = Join-Path $script:MetadataPath 'events.jsonl'
$script:ChildTemp = Join-Path $script:SuiteRoot 'child-temp'
$script:InstrumentedSmoke = Join-Path $script:SuiteRoot 'order_system_adapter_smoke.instrumented.ps1'
$script:CleanupFaultSmoke = Join-Path $script:SuiteRoot 'order_system_adapter_smoke.cleanup-fault.ps1'
$script:WrapperPath = Join-Path $script:SuiteRoot 'smoke-test-wrapper.ps1'
$script:ClaudePath = Join-Path $script:SuiteRoot 'claude-2.1.241.exe'
$script:WrongClaudePath = Join-Path $script:SuiteRoot 'claude-wrong.exe'
$script:ClaudeSha256 = ''
$script:GitPath = ''
$script:GitSha256 = ''
$script:HashMapBase64 = ''

function Test-Case {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Condition,
        [string]$Detail = ''
    )
    if ($Condition) {
        $script:PassCount++
        Write-Host ('PASS ' + $Name)
    } else {
        $script:FailCount++
        Write-Host ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' })) -ForegroundColor Red
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false, $true)))
}

function Get-LowerSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Get-PowerShellParseDiagnostics {
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    $diagnostics = @()
    foreach ($path in $Paths) {
        $fileErrors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$fileErrors)
        foreach ($fileError in @($fileErrors)) {
            $diagnostics += [pscustomobject]@{
                Path = $path
                Message = [string]$fileError.Message
            }
        }
    }
    return @($diagnostics)
}

function New-TestClaudeExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$TypeName
    )
    $source = @"
using System;
using System.IO;
using System.Text;
public static class $TypeName {
    public static int Main(string[] args) {
        string mutate = Environment.GetEnvironmentVariable("ORDER_SMOKE_TEST_MUTATE_PATH");
        if (!String.IsNullOrEmpty(mutate)) File.AppendAllText(mutate, "mutated", new UTF8Encoding(false));
        string transientDirectory = Environment.GetEnvironmentVariable("ORDER_SMOKE_TEST_TRANSIENT_DIRECTORY");
        if (!String.IsNullOrEmpty(transientDirectory)) {
            string transientPath = Path.Combine(transientDirectory, "transient-smoke-file.tmp");
            File.WriteAllText(transientPath, "transient", new UTF8Encoding(false));
            File.Delete(transientPath);
        }
        if (args.Length == 1 && String.Equals(args[0], "--version", StringComparison.Ordinal)) {
            Console.WriteLine("$Version");
            return 0;
        }
        return 97;
    }
}
"@
    Add-Type -TypeDefinition $source -OutputAssembly $Path -OutputType ConsoleApplication -ErrorAction Stop | Out-Null
}

function New-InstrumentedSmokeScripts {
    $source = [IO.File]::ReadAllText($script:SourceSmoke, [Text.Encoding]::UTF8)
    $identityPattern = '(?s)function Get-SmokeIdentitySid \{.*?\r?\n\}\r?\n\r?\nfunction Get-SmokeTaskEvidence'
    if ([Text.RegularExpressions.Regex]::Matches($source, $identityPattern).Count -ne 1) {
        throw 'identity_instrumentation_anchor_invalid'
    }
    $identityReplacement = @'
function Get-SmokeIdentitySid {
    return [Environment]::GetEnvironmentVariable('ORDER_SMOKE_TEST_IDENTITY_SID', 'Process')
}

function Get-SmokeTaskEvidence
'@
    $instrumented = [Text.RegularExpressions.Regex]::Replace($source, $identityPattern, $identityReplacement)

    $releaseVerificationAnchor = "    Assert-SmokeSafeDirectory -Path `$Root -MissingCode 'RELEASE_ROOT_MISSING' -UnsafeCode 'RELEASE_ROOT_UNSAFE'"
    if (($instrumented.Split(@($releaseVerificationAnchor), [StringSplitOptions]::None).Count - 1) -ne 1) {
        throw 'release_verification_instrumentation_anchor_invalid'
    }
    $releaseVerificationReplacement = @'
    $verificationCounter = Get-Variable -Name SmokeTestReleaseVerificationCount -Scope Script -ErrorAction SilentlyContinue
    if ($null -eq $verificationCounter) { $script:SmokeTestReleaseVerificationCount = 0 }
    $script:SmokeTestReleaseVerificationCount++
    if ($script:SmokeTestReleaseVerificationCount -gt 1) {
        if ([string]$global:SmokeTestFixture.SecondPassReleaseFailure -ceq 'MANIFEST_READ') {
            Throw-Smoke -Code 'RELEASE_MANIFEST_READ_FAILED'
        }
        if ([string]$global:SmokeTestFixture.SecondPassReleaseFailure -ceq 'UNEXPECTED') {
            throw 'injected unexpected release verification failure'
        }
    }
    Assert-SmokeSafeDirectory -Path $Root -MissingCode 'RELEASE_ROOT_MISSING' -UnsafeCode 'RELEASE_ROOT_UNSAFE'
'@
    $instrumented = $instrumented.Replace($releaseVerificationAnchor, $releaseVerificationReplacement.TrimEnd("`r", "`n"))
    Write-Utf8NoBom -Path $script:InstrumentedSmoke -Text $instrumented

    $cleanupAnchor = "            Remove-Item -LiteralPath `$full -Recurse -Force -ErrorAction Stop"
    if (($instrumented.Split(@($cleanupAnchor), [StringSplitOptions]::None).Count - 1) -ne 1) {
        throw 'cleanup_instrumentation_anchor_invalid'
    }
    $cleanupFault = $instrumented.Replace($cleanupAnchor, "            throw 'test cleanup fault before removal'")
    Write-Utf8NoBom -Path $script:CleanupFaultSmoke -Text $cleanupFault
}

function Reset-TestRelease {
    if (Test-Path -LiteralPath $script:ReleasePath) {
        Remove-Item -LiteralPath $script:ReleasePath -Recurse -Force
    }
    $scriptsPath = Join-Path $script:ReleasePath 'scripts'
    New-Item -ItemType Directory -Path $scriptsPath -Force | Out-Null
    foreach ($relative in $script:RequiredFiles) {
        $source = Join-Path $script:RepoRoot $relative
        $destination = Join-Path $script:ReleasePath $relative
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    Update-TestReleaseTrust
}

function Update-TestReleaseTrust {
    $hashes = [ordered]@{}
    foreach ($relative in $script:RequiredFiles) {
        $hashes[$relative] = Get-LowerSha256 -Path (Join-Path $script:ReleasePath $relative)
    }
    $hashText = $hashes | ConvertTo-Json -Compress
    $script:HashMapBase64 = [Convert]::ToBase64String((New-Object Text.UTF8Encoding($false)).GetBytes($hashText))
    $manifest = [ordered]@{
        schema = 'blackboard.order-worker-release.v1'
        release_id = $script:ReleaseId
        archive_sha256 = $script:ArchiveSha256
        installed_at_utc = '2026-09-07T12:34:56.1234567Z'
        file_sha256 = $hashes
    }
    Write-Utf8NoBom -Path (Join-Path $script:ReleasePath '.release.json') -Text ($manifest | ConvertTo-Json -Depth 8 -Compress)
}

function New-DefaultParameters {
    return [ordered]@{
        ReleaseId = $script:ReleaseId
        ArchiveSha256 = $script:ArchiveSha256
        ExpectedFileHashesBase64 = $script:HashMapBase64
        ReleaseRoot = $script:ReleaseRoot
        UserProfilePath = $script:ProfilePath
        WorkspacePath = $script:WorkspacePath
        EnvFile = $script:EnvPath
        StatePath = $script:StatePath
        LogPath = $script:LogPath
        ClaudePath = $script:ClaudePath
        ExpectedClaudeSha256 = $script:ClaudeSha256
        GitPath = $script:GitPath
        ExpectedGitSha256 = $script:GitSha256
        TaskName = 'SFDC24 Blackboard Order Worker'
        QuietWindowSeconds = 60
        ProcessTimeoutSeconds = 60
    }
}

function New-DefaultFixture {
    return [ordered]@{
        IdentitySid = 'S-1-5-18'
        TaskState = 'Ready'
        TaskMode = 'Observe'
        LastTaskResult = 0
        QuietSeconds = 600
        ActionCount = 1
        ActionId = ''
        ActionWorkspace = ''
        Managed = $true
        PrincipalId = 'NT AUTHORITY\SYSTEM'
        PrincipalLogonType = 'ServiceAccount'
        PrincipalRunLevel = 'Highest'
        MultipleInstances = 'IgnoreNew'
        TaskChange = $false
        MutatePath = ''
        TransientDirectory = ''
        SecondPassReleaseFailure = ''
    }
}

function Invoke-SmokeCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Parameters,
        [Parameter(Mandatory = $true)]$Fixture,
        [string]$SmokeScript = $script:InstrumentedSmoke
    )
    $caseRoot = Join-Path $script:SuiteRoot ('case-' + $Name + '-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $caseRoot | Out-Null
    $parameterPath = Join-Path $caseRoot 'parameters.json'
    $fixturePath = Join-Path $caseRoot 'fixture.json'
    Write-Utf8NoBom -Path $parameterPath -Text ($Parameters | ConvertTo-Json -Depth 8 -Compress)
    Write-Utf8NoBom -Path $fixturePath -Text ($Fixture | ConvertTo-Json -Depth 8 -Compress)

    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script:WrapperPath +
        '" -SmokeScript "' + $SmokeScript + '" -ParametersPath "' + $parameterPath +
        '" -FixturePath "' + $fixturePath + '"'
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $powerShell
    $startInfo.Arguments = $arguments
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables['TEMP'] = $script:ChildTemp
    $startInfo.EnvironmentVariables['TMP'] = $script:ChildTemp
    $startInfo.EnvironmentVariables['ORDER_SMOKE_TEST_IDENTITY_SID'] = [string]$Fixture.IdentitySid
    if (-not [string]::IsNullOrWhiteSpace([string]$Fixture.MutatePath)) {
        $startInfo.EnvironmentVariables['ORDER_SMOKE_TEST_MUTATE_PATH'] = [string]$Fixture.MutatePath
    } else {
        $startInfo.EnvironmentVariables.Remove('ORDER_SMOKE_TEST_MUTATE_PATH')
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$Fixture.TransientDirectory)) {
        $startInfo.EnvironmentVariables['ORDER_SMOKE_TEST_TRANSIENT_DIRECTORY'] = [string]$Fixture.TransientDirectory
    } else {
        $startInfo.EnvironmentVariables.Remove('ORDER_SMOKE_TEST_TRANSIENT_DIRECTORY')
    }
    $startInfo.EnvironmentVariables['BUS_TEST_SECRET'] = 'must-not-reach-fake-or-receipt'
    $startInfo.EnvironmentVariables['ANTHROPIC_FOUNDRY_API_KEY'] = 'must-not-reach-fake-or-receipt'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $null = $process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    if (-not $process.WaitForExit(180000)) {
        try { $process.Kill() } catch {}
        throw ('case_timeout:' + $Name)
    }
    $exitCode = $process.ExitCode
    $process.Dispose()
    $lines = @($stdout -split "`r?`n" | Where-Object { $_.Length -gt 0 })
    $receipt = $null
    if ($lines.Count -eq 1) {
        try { $receipt = $lines[0] | ConvertFrom-Json -ErrorAction Stop } catch {}
    }
    return [pscustomobject][ordered]@{
        exit_code = $exitCode
        stdout = $stdout
        stderr = $stderr
        lines = $lines
        receipt = $receipt
        case_root = $caseRoot
    }
}

function Test-ReceiptTelemetryContract {
    param([Parameter(Mandatory = $true)]$Run)

    if ($Run.lines.Count -ne 1 -or $null -eq $Run.receipt) { return $false }
    $provider = $Run.receipt.PSObject.Properties['provider_boundary_status']
    $mutation = $Run.receipt.PSObject.Properties['external_mutation_status']
    if ($null -eq $provider -or $null -eq $mutation) { return $false }
    return (
        @('NOT_CHECKED', 'VERIFIED_NO_SELECTOR_LEAK', 'SELECTOR_LEAK_DETECTED') -ccontains [string]$provider.Value -and
        @('NOT_CHECKED', 'NOT_DETECTED', 'DETECTED', 'VERIFICATION_FAILED') -ccontains [string]$mutation.Value -and
        $null -eq $Run.receipt.PSObject.Properties['provider_inference_attempted'] -and
        $null -eq $Run.receipt.PSObject.Properties['external_write_attempted'] -and
        [Text.Encoding]::UTF8.GetByteCount([string]$Run.lines[0]) -le 3072
    )
}

function Assert-FailureCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Run,
        [Parameter(Mandatory = $true)][string]$Code,
        [string]$CleanupStatus = 'SUCCEEDED'
    )
    $valid = $Run.exit_code -eq 1 -and $Run.lines.Count -eq 1 -and $null -ne $Run.receipt -and
        -not [bool]$Run.receipt.pass -and [string]$Run.receipt.failure_code -ceq $Code -and
        [string]$Run.receipt.cleanup_status -ceq $CleanupStatus -and [string]::IsNullOrEmpty($Run.stderr) -and
        (Test-ReceiptTelemetryContract -Run $Run)
    Test-Case -Name $Name -Condition $valid -Detail ($Run.stdout + ' STDERR=' + $Run.stderr)
}

function Get-OwnedSmokeResidue {
    return @(
        Get-ChildItem -LiteralPath $script:ChildTemp -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -cmatch '^blackboard-order-system-smoke-[0-9a-f]{32}$' }
    )
}

$wrapper = @'
#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$SmokeScript,
    [Parameter(Mandatory = $true)][string]$ParametersPath,
    [Parameter(Mandatory = $true)][string]$FixturePath
)
$ErrorActionPreference = 'Stop'
$script:Parameters = [IO.File]::ReadAllText($ParametersPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$script:Fixture = [IO.File]::ReadAllText($FixturePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$global:SmokeTestFixture = $script:Fixture
$global:SmokeTestExportCount = 0
$releasePath = Join-Path ([string]$script:Parameters.ReleaseRoot) ([string]$script:Parameters.ReleaseId)
$runner = Join-Path $releasePath 'scripts\order_supervisor.ps1'
$actionWorkspace = if ([string]::IsNullOrWhiteSpace([string]$script:Fixture.ActionWorkspace)) {
    [string]$script:Parameters.WorkspacePath
} else { [string]$script:Fixture.ActionWorkspace }
$arguments = @(
    '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy Bypass',
    ('-File "' + $runner + '"'), ('-Mode ' + [string]$script:Fixture.TaskMode),
    '-AllowedSourcesCsv "chat-mobile,codex"',
    ('-UserProfilePath "' + [string]$script:Parameters.UserProfilePath + '"'),
    ('-WorkspacePath "' + $actionWorkspace + '"'),
    ('-EnvFile "' + [string]$script:Parameters.EnvFile + '"'),
    ('-StatePath "' + [string]$script:Parameters.StatePath + '"'),
    ('-LogPath "' + [string]$script:Parameters.LogPath + '"'),
    '-WallTimeoutSeconds 720',
    ('-ClaudeCommand "' + [string]$script:Parameters.ClaudePath + '"')
) -join ' '
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = [pscustomobject]@{
    Id = [string]$script:Fixture.ActionId
    Execute = $windowsPowerShell
    WorkingDirectory = $actionWorkspace
    Arguments = $arguments
}
$actions = if ([int]$script:Fixture.ActionCount -eq 1) { @($action) } else { @($action, $action) }
$description = if ([bool]$script:Fixture.Managed) {
    'SFDC24 Blackboard ORDER worker; managed-by=install_order_supervisor.ps1; schema=v1'
} else { 'unmanaged test task' }
$global:SmokeTestTask = [pscustomobject]@{
    TaskName = [string]$script:Parameters.TaskName
    TaskPath = '\'
    Description = $description
    State = [string]$script:Fixture.TaskState
    Actions = $actions
    Principal = [pscustomobject]@{
        UserId = [string]$script:Fixture.PrincipalId
        LogonType = [string]$script:Fixture.PrincipalLogonType
        RunLevel = [string]$script:Fixture.PrincipalRunLevel
    }
    Settings = [pscustomobject]@{ MultipleInstances = [string]$script:Fixture.MultipleInstances }
}
$global:SmokeTestTaskInfo = [pscustomobject]@{
    LastTaskResult = [int64]$script:Fixture.LastTaskResult
    LastRunTime = [DateTime]::Now.AddMinutes(-5)
    NextRunTime = [DateTime]::Now.AddSeconds([int]$script:Fixture.QuietSeconds)
}
function global:Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    return $global:SmokeTestTask
}
function global:Get-ScheduledTaskInfo {
    [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
    return $global:SmokeTestTaskInfo
}
function global:Export-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
    $global:SmokeTestExportCount++
    if ([bool]$global:SmokeTestFixture.TaskChange -and $global:SmokeTestExportCount -gt 1) { return '<Task>changed</Task>' }
    return '<Task>stable</Task>'
}
& $SmokeScript `
    -ReleaseId ([string]$script:Parameters.ReleaseId) `
    -ArchiveSha256 ([string]$script:Parameters.ArchiveSha256) `
    -ExpectedFileHashesBase64 ([string]$script:Parameters.ExpectedFileHashesBase64) `
    -ReleaseRoot ([string]$script:Parameters.ReleaseRoot) `
    -UserProfilePath ([string]$script:Parameters.UserProfilePath) `
    -WorkspacePath ([string]$script:Parameters.WorkspacePath) `
    -EnvFile ([string]$script:Parameters.EnvFile) `
    -StatePath ([string]$script:Parameters.StatePath) `
    -LogPath ([string]$script:Parameters.LogPath) `
    -ClaudePath ([string]$script:Parameters.ClaudePath) `
    -ExpectedClaudeSha256 ([string]$script:Parameters.ExpectedClaudeSha256) `
    -GitPath ([string]$script:Parameters.GitPath) `
    -ExpectedGitSha256 ([string]$script:Parameters.ExpectedGitSha256) `
    -TaskName ([string]$script:Parameters.TaskName) `
    -QuietWindowSeconds ([int]$script:Parameters.QuietWindowSeconds) `
    -ProcessTimeoutSeconds ([int]$script:Parameters.ProcessTimeoutSeconds)
exit $LASTEXITCODE
'@

try {
    New-Item -ItemType Directory -Path $script:SuiteRoot, $script:ReleaseRoot, $script:ProfilePath, $script:WorkspacePath, $script:MetadataPath, $script:ChildTemp | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:ProfilePath '.claude'), (Join-Path $script:WorkspacePath '.claude') | Out-Null
    Write-Utf8NoBom -Path (Join-Path $script:ProfilePath '.claude\settings.json') -Text '{}'
    Write-Utf8NoBom -Path (Join-Path $script:ProfilePath '.claude.json') -Text '{}'
    Write-Utf8NoBom -Path (Join-Path $script:WorkspacePath '.claude\settings.json') -Text '{}'
    Write-Utf8NoBom -Path $script:EnvPath -Text "ANTHROPIC_API_KEY=real-file-must-never-be-read`nANTHROPIC_MODEL=real-model-must-never-be-read`n"
    Write-Utf8NoBom -Path $script:StatePath -Text '{"cursor":1}'
    Write-Utf8NoBom -Path $script:LogPath -Text ('{"event":"seed"}' + "`n")
    $git = [string](@(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0].Source)
    $script:GitPath = [IO.Path]::GetFullPath($git)
    $script:GitSha256 = Get-LowerSha256 -Path $script:GitPath
    & $git -c init.defaultBranch=main -C $script:WorkspacePath init --quiet 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'test_workspace_git_init_failed' }
    New-TestClaudeExecutable -Path $script:ClaudePath -Version '2.1.241 (Claude Code)' -TypeName 'SmokeTestClaudeGood'
    New-TestClaudeExecutable -Path $script:WrongClaudePath -Version '2.1.999 (Claude Code)' -TypeName 'SmokeTestClaudeWrong'
    $script:ClaudeSha256 = Get-LowerSha256 -Path $script:ClaudePath
    New-InstrumentedSmokeScripts
    Write-Utf8NoBom -Path $script:WrapperPath -Text $wrapper
    Reset-TestRelease

    $parseErrors = @(Get-PowerShellParseDiagnostics -Paths @($script:SourceSmoke, $script:InstrumentedSmoke, $script:CleanupFaultSmoke, $script:WrapperPath))
    Test-Case -Name 'PowerShell parser accepts source and harnesses' -Condition ($parseErrors.Count -eq 0) -Detail (@($parseErrors | ForEach-Object { $_.Path + ': ' + $_.Message }) -join '; ')

    $invalidParserProbe = Join-Path $script:SuiteRoot 'parser-probe-invalid.ps1'
    $validParserProbe = Join-Path $script:SuiteRoot 'parser-probe-valid.ps1'
    Write-Utf8NoBom -Path $invalidParserProbe -Text 'function Invoke-Broken {'
    Write-Utf8NoBom -Path $validParserProbe -Text 'function Invoke-Clean { return 0 }'
    $parserProbeErrors = @(Get-PowerShellParseDiagnostics -Paths @($invalidParserProbe, $validParserProbe))
    $invalidProbeErrors = @($parserProbeErrors | Where-Object { $_.Path -eq $invalidParserProbe })
    Test-Case -Name 'parser diagnostics retain an earlier-file error when a later file is clean' -Condition ($invalidProbeErrors.Count -gt 0) -Detail (@($parserProbeErrors | ForEach-Object { $_.Path + ': ' + $_.Message }) -join '; ')

    $productionSourceText = [IO.File]::ReadAllText($script:SourceSmoke, [Text.Encoding]::UTF8)
    Test-Case -Name 'production receipt guard uses the 3072-byte Managed Run Command transport bound' -Condition (
        $productionSourceText.Contains('$receiptByteCount = [Text.Encoding]::UTF8.GetByteCount($receiptJson)') -and
        $productionSourceText.Contains('if ($receiptByteCount -gt 3072)') -and
        -not $productionSourceText.Contains('if ($receiptJson.Length -gt 16384)')
    )
    Test-Case -Name 'production receipt uses factual status telemetry and removes misleading booleans' -Condition (
        $productionSourceText.Contains("provider_boundary_status = 'NOT_CHECKED'") -and
        $productionSourceText.Contains("external_mutation_status = 'NOT_CHECKED'") -and
        $productionSourceText.Contains("'VERIFICATION_FAILED'") -and
        -not $productionSourceText.Contains('provider_inference_attempted') -and
        -not $productionSourceText.Contains('external_write_attempted')
    )
    $readmeText = [IO.File]::ReadAllText((Join-Path $script:RepoRoot 'infra\azure\README.md'), [Text.Encoding]::UTF8)
    Test-Case -Name 'README requires a lowercase hexadecimal release id and exact telemetry states' -Condition (
        [Text.RegularExpressions.Regex]::Matches($readmeText, '40-character lowercase(?: |\r?\n\s*)hexadecimal release ID').Count -eq 2 -and
        -not $readmeText.Contains('40-hex release ID') -and
        $readmeText.Contains('provider_boundary_status:VERIFIED_NO_SELECTOR_LEAK') -and
        $readmeText.Contains('external_mutation_status:NOT_DETECTED') -and
        $readmeText.Contains('`VERIFICATION_FAILED`') -and
        -not $readmeText.Contains('provider_inference_attempted:false') -and
        -not $readmeText.Contains('external_write_attempted:false')
    )

    # Behavioral cases are below. Each case starts from a rebuilt six-file release.
    $parameters = New-DefaultParameters
    $fixture = New-DefaultFixture
    $protectedBefore = @(
        Get-LowerSha256 -Path $script:EnvPath
        Get-LowerSha256 -Path $script:StatePath
        Get-LowerSha256 -Path $script:LogPath
        Get-LowerSha256 -Path $script:ClaudePath
    ) -join '|'
    $success = Invoke-SmokeCase -Name 'success' -Parameters $parameters -Fixture $fixture
    $protectedAfter = @(
        Get-LowerSha256 -Path $script:EnvPath
        Get-LowerSha256 -Path $script:StatePath
        Get-LowerSha256 -Path $script:LogPath
        Get-LowerSha256 -Path $script:ClaudePath
    ) -join '|'
    $successFlags = $success.exit_code -eq 0 -and $success.lines.Count -eq 1 -and $null -ne $success.receipt -and
        [bool]$success.receipt.pass -and [string]$success.receipt.release_id -ceq $script:ReleaseId -and
        [bool]$success.receipt.identity_system -and [bool]$success.receipt.release_verified -and
        [bool]$success.receipt.release_unchanged -and
        [bool]$success.receipt.task_verified -and [bool]$success.receipt.task_unchanged -and
        [bool]$success.receipt.claude_version_verified -and [string]$success.receipt.claude_version -ceq '2.1.241' -and
        [string]$success.receipt.claude_sha256 -ceq $script:ClaudeSha256 -and
        [string]$success.receipt.git_sha256 -ceq $script:GitSha256 -and
        [bool]$success.receipt.adapter_boundary_verified -and [bool]$success.receipt.settings_verified -and
        [bool]$success.receipt.environment_scrub_verified -and [bool]$success.receipt.structured_output_verified -and
        [bool]$success.receipt.harmless_git_verified -and [bool]$success.receipt.protected_fingerprints_unchanged -and
        [string]$success.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$success.receipt.external_mutation_status -ceq 'NOT_DETECTED' -and
        [string]$success.receipt.environment_restore_status -ceq 'SUCCEEDED' -and
        [string]$success.receipt.cleanup_status -ceq 'SUCCEEDED' -and $null -eq $success.receipt.failure_code -and
        [string]::IsNullOrEmpty($success.stderr) -and (Test-ReceiptTelemetryContract -Run $success)
    Test-Case -Name 'provider-free SYSTEM smoke emits one passing receipt' -Condition $successFlags -Detail ($success.stdout + ' STDERR=' + $success.stderr)
    Test-Case -Name 'requested and effective tool sets are exact' -Condition (
        (@($success.receipt.requested_tools) -join '|') -ceq 'Read|Edit|PowerShell' -and
        (@($success.receipt.effective_tools) -join '|') -ceq 'Read|Edit|PowerShell|StructuredOutput'
    )
    Test-Case -Name 'receipt is bounded and excludes protected/sentinel material' -Condition (
        [Text.Encoding]::UTF8.GetByteCount($success.stdout) -le 3072 -and
        -not $success.stdout.Contains('real-file-must-never-be-read') -and
        -not $success.stdout.Contains('must-not-reach-fake-or-receipt') -and
        -not $success.stdout.Contains($script:EnvPath) -and
        -not $success.stdout.Contains($script:ClaudePath)
    ) -Detail $success.stdout
    Test-Case -Name 'protected file contents remain byte-identical' -Condition ($protectedAfter -ceq $protectedBefore)
    Test-Case -Name 'successful run removes its exact owned temporary root' -Condition (@(Get-OwnedSmokeResidue).Count -eq 0)

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.ActionId = 'OrderSupervisor'
    $run = Invoke-SmokeCase -Name 'canonical-action-id' -Parameters (New-DefaultParameters) -Fixture $fixture
    Test-Case -Name 'canonical named task action id remains accepted' -Condition ($run.exit_code -eq 0 -and [bool]$run.receipt.pass) -Detail $run.stdout

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.ActionId = 'WrongAction'
    $run = Invoke-SmokeCase -Name 'wrong-action-id' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'noncanonical task action id is rejected' -Run $run -Code 'TASK_ACTION_CONTRACT_INVALID'

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.ReleaseId = 'abcdef0'
    $run = Invoke-SmokeCase -Name 'short-release' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'short release id is rejected before mutation' -Run $run -Code 'RELEASE_ID_INVALID' -CleanupStatus 'NOT_CREATED'
    Test-Case -Name 'early failure reports both telemetry facts as not checked' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'NOT_CHECKED' -and
        [string]$run.receipt.external_mutation_status -ceq 'NOT_CHECKED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.ReleaseId = $script:ReleaseId.ToUpperInvariant().Replace('1', 'A')
    $run = Invoke-SmokeCase -Name 'uppercase-release' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'uppercase full release id is rejected' -Run $run -Code 'RELEASE_ID_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.ExpectedFileHashesBase64 = 'not base64***'
    $run = Invoke-SmokeCase -Name 'hash-base64' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'invalid expected-hash Base64 is rejected' -Run $run -Code 'EXPECTED_HASHES_BASE64_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $hashText = (New-Object Text.UTF8Encoding($false, $true)).GetString([Convert]::FromBase64String($script:HashMapBase64))
    $firstKey = $script:RequiredFiles[0]
    $firstValue = (Get-LowerSha256 -Path (Join-Path $script:ReleasePath $firstKey))
    $escapedFirstKey = $firstKey.Replace('\', '\\')
    $duplicateText = $hashText.Substring(0, $hashText.Length - 1) + ',"' + $escapedFirstKey + '":"' + $firstValue + '"}'
    $parameters = New-DefaultParameters
    $parameters.ExpectedFileHashesBase64 = [Convert]::ToBase64String((New-Object Text.UTF8Encoding($false)).GetBytes($duplicateText))
    $run = Invoke-SmokeCase -Name 'hash-duplicate' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'duplicate expected-hash key is rejected' -Run $run -Code 'EXPECTED_HASHES_DUPLICATE_KEY' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $hashText = (New-Object Text.UTF8Encoding($false, $true)).GetString([Convert]::FromBase64String($script:HashMapBase64))
    $caseText = $hashText.Replace('scripts\\OrderSupervisor.psm1', 'scripts\\ordersupervisor.psm1')
    $parameters = New-DefaultParameters
    $parameters.ExpectedFileHashesBase64 = [Convert]::ToBase64String((New-Object Text.UTF8Encoding($false)).GetBytes($caseText))
    $run = Invoke-SmokeCase -Name 'hash-case' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'case-drifted expected-hash key is rejected' -Run $run -Code 'EXPECTED_HASHES_SHAPE_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    [byte[]]$bomBytes = @(0xef, 0xbb, 0xbf) + @([Convert]::FromBase64String($script:HashMapBase64))
    $parameters = New-DefaultParameters
    $parameters.ExpectedFileHashesBase64 = [Convert]::ToBase64String($bomBytes)
    $run = Invoke-SmokeCase -Name 'hash-bom' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'BOM-bearing expected hashes are rejected' -Run $run -Code 'EXPECTED_HASHES_ENCODING_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.ExpectedFileHashesBase64 = [Convert]::ToBase64String([byte[]](0x7b, 0xff, 0x7d))
    $run = Invoke-SmokeCase -Name 'hash-utf8' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'invalid UTF-8 expected hashes are rejected' -Run $run -Code 'EXPECTED_HASHES_ENCODING_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $map = (New-Object Text.UTF8Encoding($false, $true)).GetString([Convert]::FromBase64String($script:HashMapBase64)) | ConvertFrom-Json
    $map.PSObject.Properties[$script:RequiredFiles[0]].Value = ('0' * 64)
    $parameters = New-DefaultParameters
    $parameters.ExpectedFileHashesBase64 = [Convert]::ToBase64String((New-Object Text.UTF8Encoding($false)).GetBytes(($map | ConvertTo-Json -Compress)))
    $run = Invoke-SmokeCase -Name 'hash-mismatch' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'caller-bound hash mismatch is rejected' -Run $run -Code 'RELEASE_MANIFEST_HASH_MISMATCH' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $manifestPath = Join-Path $script:ReleasePath '.release.json'
    $manifestText = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8)
    $manifestText = $manifestText.Replace('"schema":"blackboard.order-worker-release.v1"', '"schema":"blackboard.order-worker-release.v1","schema":"blackboard.order-worker-release.v1"')
    Write-Utf8NoBom -Path $manifestPath -Text $manifestText
    $run = Invoke-SmokeCase -Name 'manifest-duplicate' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'duplicate release-manifest key is rejected' -Run $run -Code 'RELEASE_MANIFEST_DUPLICATE_KEY' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $manifestPath = Join-Path $script:ReleasePath '.release.json'
    [byte[]]$manifestBom = @(0xef, 0xbb, 0xbf) + @([IO.File]::ReadAllBytes($manifestPath))
    [IO.File]::WriteAllBytes($manifestPath, $manifestBom)
    $run = Invoke-SmokeCase -Name 'manifest-bom' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'BOM-bearing release manifest is rejected' -Run $run -Code 'RELEASE_MANIFEST_ENCODING_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $manifestPath = Join-Path $script:ReleasePath '.release.json'
    $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $manifest.installed_at_utc = '2026-09-07T12:34:56.1234567+00:00'
    Write-Utf8NoBom -Path $manifestPath -Text ($manifest | ConvertTo-Json -Depth 8 -Compress)
    $run = Invoke-SmokeCase -Name 'manifest-timestamp' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'non-canonical UTC timestamp is rejected' -Run $run -Code 'RELEASE_MANIFEST_TIMESTAMP_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $manifestPath = Join-Path $script:ReleasePath '.release.json'
    $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $manifest.archive_sha256 = ('b' * 64)
    Write-Utf8NoBom -Path $manifestPath -Text ($manifest | ConvertTo-Json -Depth 8 -Compress)
    $run = Invoke-SmokeCase -Name 'manifest-archive' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'caller-bound archive digest mismatch is rejected' -Run $run -Code 'RELEASE_MANIFEST_VALUE_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    Write-Utf8NoBom -Path (Join-Path $script:ReleasePath 'rogue.txt') -Text 'rogue'
    $run = Invoke-SmokeCase -Name 'inventory-extra-root' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'extra logical root entry is rejected' -Run $run -Code 'RELEASE_INVENTORY_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    Write-Utf8NoBom -Path (Join-Path $script:ReleasePath 'scripts\rogue.ps1') -Text 'rogue'
    $run = Invoke-SmokeCase -Name 'inventory-extra-script' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'extra scripts entry is rejected' -Run $run -Code 'RELEASE_INVENTORY_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    Remove-Item -LiteralPath (Join-Path $script:ReleasePath $script:RequiredFiles[1]) -Force
    $run = Invoke-SmokeCase -Name 'inventory-missing' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'missing release file is rejected' -Run $run -Code 'RELEASE_INVENTORY_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    Add-Content -LiteralPath (Join-Path $script:ReleasePath $script:RequiredFiles[1]) -Value 'tampered'
    $run = Invoke-SmokeCase -Name 'actual-tamper' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'actual six-file hash tamper is rejected' -Run $run -Code 'RELEASE_FILE_HASH_MISMATCH' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $junctionRoot = Join-Path $script:SuiteRoot 'release-root-junction'
    $junction = New-Item -ItemType Junction -Path $junctionRoot -Target $script:ReleaseRoot -ErrorAction Stop
    $parameters = New-DefaultParameters
    $parameters.ReleaseRoot = $junctionRoot
    $run = Invoke-SmokeCase -Name 'release-root-reparse' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'release-root reparse traversal is rejected' -Run $run -Code 'RELEASE_ROOT_UNSAFE' -CleanupStatus 'NOT_CREATED'
    [IO.Directory]::Delete($junction.FullName)

    Reset-TestRelease
    $scriptsPath = Join-Path $script:ReleasePath 'scripts'
    $outsideScripts = Join-Path $script:SuiteRoot 'junction-target-scripts'
    [IO.Directory]::Move($scriptsPath, $outsideScripts)
    $scriptsJunction = New-Item -ItemType Junction -Path $scriptsPath -Target $outsideScripts -ErrorAction Stop
    $run = Invoke-SmokeCase -Name 'release-scripts-reparse' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'nested scripts reparse traversal is rejected' -Run $run -Code 'RELEASE_UNSAFE' -CleanupStatus 'NOT_CREATED'
    [IO.Directory]::Delete($scriptsJunction.FullName)
    Remove-Item -LiteralPath $outsideScripts -Recurse -Force

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.IdentitySid = 'S-1-5-21-1234'
    $run = Invoke-SmokeCase -Name 'not-system' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'non-SYSTEM identity is rejected before temp creation' -Run $run -Code 'NOT_SYSTEM' -CleanupStatus 'NOT_CREATED'

    $parameters = New-DefaultParameters
    $parameters.ReleaseRoot = 'C:\missing-release-root-for-non-system'
    $run = Invoke-SmokeCase -Name 'not-system-before-filesystem' -Parameters $parameters -Fixture $fixture
    Assert-FailureCase -Name 'non-SYSTEM gate precedes filesystem inspection' -Run $run -Code 'NOT_SYSTEM' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.TaskState = 'Running'
    $run = Invoke-SmokeCase -Name 'task-running' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'running task is not accepted as quiescent' -Run $run -Code 'TASK_NOT_READY'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.TaskMode = 'Execute'
    $run = Invoke-SmokeCase -Name 'task-execute' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'Execute candidate task is rejected' -Run $run -Code 'TASK_ACTION_CONTRACT_INVALID'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.LastTaskResult = 1
    $run = Invoke-SmokeCase -Name 'task-result' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'nonzero task result is rejected' -Run $run -Code 'TASK_LAST_RESULT_NONZERO'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.QuietSeconds = 30
    $run = Invoke-SmokeCase -Name 'task-quiet' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'insufficient next-run quiet window is rejected' -Run $run -Code 'TASK_QUIET_WINDOW_UNAVAILABLE'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.ActionCount = 2
    $run = Invoke-SmokeCase -Name 'task-actions' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'multiple task actions are rejected' -Run $run -Code 'TASK_ACTION_COUNT_INVALID'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.PrincipalId = 'TEST\operator'
    $run = Invoke-SmokeCase -Name 'task-principal' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'non-SYSTEM task principal is rejected' -Run $run -Code 'TASK_PRINCIPAL_INVALID'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.MultipleInstances = 'Parallel'
    $run = Invoke-SmokeCase -Name 'task-multiple-instances' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'unsafe task overlap policy is rejected' -Run $run -Code 'TASK_MULTIPLE_INSTANCES_INVALID'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.Managed = $false
    $run = Invoke-SmokeCase -Name 'task-unmanaged' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'unmanaged task marker is rejected' -Run $run -Code 'TASK_UNMANAGED'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.ActionWorkspace = $script:ProfilePath
    $run = Invoke-SmokeCase -Name 'task-paths' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'task path contract drift is rejected' -Run $run -Code 'TASK_ACTION_CONTRACT_INVALID'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.TaskChange = $true
    $run = Invoke-SmokeCase -Name 'task-change' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'task definition change during smoke is detected' -Run $run -Code 'TASK_CHANGED_DURING_SMOKE'
    Test-Case -Name 'task mismatch records verified provider boundary and detected external mutation' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$run.receipt.external_mutation_status -ceq 'DETECTED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.ClaudePath = $script:WrongClaudePath
    $parameters.ExpectedClaudeSha256 = Get-LowerSha256 -Path $script:WrongClaudePath
    $run = Invoke-SmokeCase -Name 'claude-version' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'Claude version drift is rejected' -Run $run -Code 'CLAUDE_VERSION_MISMATCH'

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.ExpectedClaudeSha256 = ('f' * 64)
    $run = Invoke-SmokeCase -Name 'claude-hash' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'caller-bound Claude executable hash mismatch is rejected' -Run $run -Code 'CLAUDE_SHA256_MISMATCH' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.ExpectedGitSha256 = ('f' * 64)
    $run = Invoke-SmokeCase -Name 'git-hash' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'caller-bound Git executable hash mismatch is rejected' -Run $run -Code 'GIT_SHA256_MISMATCH' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.MutatePath = $script:StatePath
    $run = Invoke-SmokeCase -Name 'protected-mutation' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'protected-file mutation is detected by before-after fingerprint' -Run $run -Code 'PROTECTED_FINGERPRINT_CHANGED'
    Test-Case -Name 'protected-file mismatch records detected external mutation' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$run.receipt.external_mutation_status -ceq 'DETECTED'
    ) -Detail $run.stdout
    Write-Utf8NoBom -Path $script:StatePath -Text '{"cursor":1}'

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.TransientDirectory = Join-Path $script:ProfilePath '.claude'
    $run = Invoke-SmokeCase -Name 'protected-transient-mutation' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'transient protected-tree write-delete is detected' -Run $run -Code 'PROTECTED_FINGERPRINT_CHANGED'
    Test-Case -Name 'transient protected-tree mutation records detected external mutation' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$run.receipt.external_mutation_status -ceq 'DETECTED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.MutatePath = Join-Path $script:ReleasePath 'scripts\bus.ps1'
    $run = Invoke-SmokeCase -Name 'release-mutation' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'unused release-file mutation during smoke cannot false-green' -Run $run -Code 'RELEASE_FILE_HASH_MISMATCH'
    Test-Case -Name 'second-pass release hash failure records detected external mutation' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$run.receipt.external_mutation_status -ceq 'DETECTED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.MutatePath = Join-Path $script:ReleasePath 'rogue.txt'
    $run = Invoke-SmokeCase -Name 'release-inventory-mutation' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'second-pass release inventory mutation cannot false-green' -Run $run -Code 'RELEASE_INVENTORY_INVALID'
    Test-Case -Name 'second-pass release inventory failure records detected external mutation' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$run.receipt.external_mutation_status -ceq 'DETECTED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.SecondPassReleaseFailure = 'MANIFEST_READ'
    $run = Invoke-SmokeCase -Name 'release-read-failure' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'second-pass release read failure cannot false-green' -Run $run -Code 'RELEASE_MANIFEST_READ_FAILED'
    Test-Case -Name 'second-pass release read failure is not mislabeled as detected mutation' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$run.receipt.external_mutation_status -ceq 'VERIFICATION_FAILED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.SecondPassReleaseFailure = 'UNEXPECTED'
    $run = Invoke-SmokeCase -Name 'release-verifier-unexpected' -Parameters (New-DefaultParameters) -Fixture $fixture
    Assert-FailureCase -Name 'unexpected second-pass verifier failure cannot false-green' -Run $run -Code 'UNEXPECTED_SMOKE_FAILURE'
    Test-Case -Name 'unexpected second-pass verifier failure remains factually indeterminate' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'VERIFIED_NO_SELECTOR_LEAK' -and
        [string]$run.receipt.external_mutation_status -ceq 'VERIFICATION_FAILED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $adapterPath = Join-Path $script:ReleasePath 'scripts\invoke_order_claude.ps1'
    $adapterText = [IO.File]::ReadAllText($adapterPath, [Text.Encoding]::UTF8)
    $selectorAnchor = "[Environment]::SetEnvironmentVariable(`$subprocessScrubName, '1', 'Process')"
    $changed = $adapterText.Replace(
        $selectorAnchor,
        $selectorAnchor + [Environment]::NewLine +
            "    [Environment]::SetEnvironmentVariable('CLAUDE_CODE_USE_FOUNDRY', '1', 'Process')"
    )
    if ($changed -ceq $adapterText) { throw 'provider_selector_leak_anchor_missing' }
    Write-Utf8NoBom -Path $adapterPath -Text $changed
    Update-TestReleaseTrust
    $run = Invoke-SmokeCase -Name 'provider-selector-leak' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'provider selector leak into fake child is rejected' -Run $run -Code 'FAKE_PROVIDER_SELECTOR_LEAK'
    Test-Case -Name 'provider selector leak is recorded before failure without claiming an external check' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'SELECTOR_LEAK_DETECTED' -and
        [string]$run.receipt.external_mutation_status -ceq 'NOT_CHECKED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $adapterPath = Join-Path $script:ReleasePath 'scripts\invoke_order_claude.ps1'
    $adapterText = [IO.File]::ReadAllText($adapterPath, [Text.Encoding]::UTF8)
    $changed = $adapterText.Replace("        '--bare',", "        '--bare-regressed',")
    if ($changed -ceq $adapterText) { throw 'bare_regression_anchor_missing' }
    Write-Utf8NoBom -Path $adapterPath -Text $changed
    Update-TestReleaseTrust
    $run = Invoke-SmokeCase -Name 'adapter-bare' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'regressed --bare boundary cannot false-green' -Run $run -Code 'FAKE_ARGV_MISMATCH'

    Reset-TestRelease
    $adapterPath = Join-Path $script:ReleasePath 'scripts\invoke_order_claude.ps1'
    $adapterText = [IO.File]::ReadAllText($adapterPath, [Text.Encoding]::UTF8)
    $changed = $adapterText.Replace("`$allowedWorkToolNames = @('Read', 'Edit', 'PowerShell')", "`$allowedWorkToolNames = @('Read', 'PowerShell')")
    if ($changed -ceq $adapterText) { throw 'tool_regression_anchor_missing' }
    Write-Utf8NoBom -Path $adapterPath -Text $changed
    Update-TestReleaseTrust
    $run = Invoke-SmokeCase -Name 'adapter-tools' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'requested-tool regression cannot false-green' -Run $run -Code 'FAKE_ARGV_MISMATCH'

    Reset-TestRelease
    $adapterPath = Join-Path $script:ReleasePath 'scripts\invoke_order_claude.ps1'
    $adapterText = [IO.File]::ReadAllText($adapterPath, [Text.Encoding]::UTF8)
    $changed = $adapterText.Replace(
        "[Environment]::SetEnvironmentVariable(`$subprocessScrubName, '1', 'Process')",
        "[Environment]::SetEnvironmentVariable(`$subprocessScrubName, '0', 'Process')"
    )
    if ($changed -ceq $adapterText) { throw 'scrub_regression_anchor_missing' }
    Write-Utf8NoBom -Path $adapterPath -Text $changed
    Update-TestReleaseTrust
    $run = Invoke-SmokeCase -Name 'adapter-scrub' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'subprocess-scrub regression cannot false-green' -Run $run -Code 'FAKE_ENVIRONMENT_BOUNDARY_INVALID'
    Test-Case -Name 'provider boundary is not called verified when another fake-env proof fails' -Condition (
        [string]$run.receipt.provider_boundary_status -ceq 'NOT_CHECKED' -and
        [string]$run.receipt.external_mutation_status -ceq 'NOT_CHECKED'
    ) -Detail $run.stdout

    Reset-TestRelease
    $adapterPath = Join-Path $script:ReleasePath 'scripts\invoke_order_claude.ps1'
    $adapterText = [IO.File]::ReadAllText($adapterPath, [Text.Encoding]::UTF8)
    $changed = $adapterText.Replace('$promptText | & $resolved.Source @arguments', "'' | & `$resolved.Source @arguments")
    if ($changed -ceq $adapterText) { throw 'stdin_regression_anchor_missing' }
    Write-Utf8NoBom -Path $adapterPath -Text $changed
    Update-TestReleaseTrust
    $run = Invoke-SmokeCase -Name 'adapter-stdin' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'prompt-delivery regression cannot false-green' -Run $run -Code 'FAKE_STDIN_MISMATCH'

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.UserProfilePath = 'relative-profile'
    $run = Invoke-SmokeCase -Name 'relative-profile' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'relative user profile is rejected' -Run $run -Code 'USER_PROFILE_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $parameters = New-DefaultParameters
    $parameters.TaskName = 'folder\task'
    $run = Invoke-SmokeCase -Name 'task-name-path' -Parameters $parameters -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'task name cannot smuggle a task path' -Run $run -Code 'TASK_NAME_INVALID' -CleanupStatus 'NOT_CREATED'

    Reset-TestRelease
    $stateBackup = [IO.File]::ReadAllBytes($script:StatePath)
    Remove-Item -LiteralPath $script:StatePath -Force
    $run = Invoke-SmokeCase -Name 'state-missing' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture)
    Assert-FailureCase -Name 'missing protected state file is rejected' -Run $run -Code 'STATE_FILE_MISSING' -CleanupStatus 'NOT_CREATED'
    [IO.File]::WriteAllBytes($script:StatePath, $stateBackup)

    Reset-TestRelease
    $run = Invoke-SmokeCase -Name 'cleanup-success-fault' -Parameters (New-DefaultParameters) -Fixture (New-DefaultFixture) -SmokeScript $script:CleanupFaultSmoke
    Assert-FailureCase -Name 'success path cannot hide temporary cleanup failure' -Run $run -Code 'TEMPORARY_CLEANUP_FAILED' -CleanupStatus 'FAILED'
    $residue = @(Get-OwnedSmokeResidue)
    Test-Case -Name 'cleanup-fault test leaves exactly one scoped non-reparse residue' -Condition (
        $residue.Count -eq 1 -and
        -not (([int]$residue[0].Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) -and
        [IO.Path]::GetDirectoryName($residue[0].FullName).TrimEnd('\') -ceq $script:ChildTemp.TrimEnd('\')
    )
    foreach ($item in $residue) { Remove-Item -LiteralPath $item.FullName -Recurse -Force }

    Reset-TestRelease
    $fixture = New-DefaultFixture
    $fixture.TaskState = 'Queued'
    $run = Invoke-SmokeCase -Name 'cleanup-primary-fault' -Parameters (New-DefaultParameters) -Fixture $fixture -SmokeScript $script:CleanupFaultSmoke
    Assert-FailureCase -Name 'primary task error wins over cleanup error' -Run $run -Code 'TASK_NOT_READY' -CleanupStatus 'FAILED'
    $residue = @(Get-OwnedSmokeResidue)
    Test-Case -Name 'primary-plus-cleanup fault leaves one auditable residue' -Condition ($residue.Count -eq 1)
    foreach ($item in $residue) { Remove-Item -LiteralPath $item.FullName -Recurse -Force }

    $sourceText = [IO.File]::ReadAllText($script:SourceSmoke, [Text.Encoding]::UTF8)
    Test-Case -Name 'production source has no test fault-injection surface' -Condition (
        $sourceText.IndexOf('ORDER_SMOKE_TEST_', [StringComparison]::Ordinal) -lt 0 -and
        $sourceText.IndexOf('cleanup fault', [StringComparison]::OrdinalIgnoreCase) -lt 0
    )
    Test-Case -Name 'production adapter call is bound only to generated fake executable' -Condition (
        $sourceText.Contains("'-ClaudeCommand', `$fakeExe") -and
        -not $sourceText.Contains('-ClaudeCommand $resolvedClaude') -and
        $sourceText.Contains("-Arguments @('--version')")
    )
    Test-Case -Name 'no owned smoke residue remains after the suite' -Condition (@(Get-OwnedSmokeResidue).Count -eq 0)
} finally {
    if ($KeepArtifacts) {
        Write-Host ('Artifacts retained at ' + $script:SuiteRoot)
    } elseif (Test-Path -LiteralPath $script:SuiteRoot) {
        $resolvedSuite = [IO.Path]::GetFullPath($script:SuiteRoot).TrimEnd('\')
        $expectedParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        if ([IO.Path]::GetDirectoryName($resolvedSuite).TrimEnd('\') -cne $expectedParent -or
            [IO.Path]::GetFileName($resolvedSuite) -cnotmatch '^blackboard-order-system-smoke-tests-[0-9a-f]{32}$') {
            throw 'test_cleanup_scope_invalid'
        }
        Remove-Item -LiteralPath $resolvedSuite -Recurse -Force
    }
}

Write-Host ("RESULT: {0} passed, {1} failed" -f $script:PassCount, $script:FailCount)
if ($script:FailCount -gt 0) { exit 1 }
exit 0
