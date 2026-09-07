#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Condition,
        [string]$Detail = ''
    )
    if ($Condition) {
        $script:Passed++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Failed++
        Write-Output ('FAIL ' + $Name + $(if ($Detail) { ' :: ' + $Detail } else { '' }))
    }
}

function Invoke-ExpectedFailure {
    param([Parameter(Mandatory = $true)][scriptblock]$ScriptBlock)
    try {
        & $ScriptBlock
        return 'NO_ERROR'
    } catch {
        return [string]$_.Exception.Message
    }
}

function Write-TestUtf8 {
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function New-ValidBackupXml {
    param([Parameter(Mandatory = $true)][string]$RollbackRunnerPath)
    $encodedRunner = [Security.SecurityElement]::Escape($RollbackRunnerPath)
    $encodedCommand = [Security.SecurityElement]::Escape($WindowsPowerShell)
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\$TaskName</URI>
    <Description>SFDC24 Blackboard ORDER worker; $ManagedMarker</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>SYSTEM</UserId>
      <LogonType>ServiceAccount</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings><Enabled>true</Enabled></Settings>
  <Triggers />
  <Actions Context="Author">
    <Exec id="OrderSupervisor">
      <Command>$encodedCommand</Command>
      <Arguments>-NoLogo -NoProfile -File &quot;$encodedRunner&quot; -Mode Execute</Arguments>
      <WorkingDirectory>C:\rollback-workspace</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

function Write-BackupManifest {
    param(
        [Parameter(Mandatory = $true)][bool]$PreviousExisted,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$XmlSha256
    )
    $manifest = [ordered]@{
        schema = 'order_supervisor_task_backup.v1'
        previous_existed = $PreviousExisted
        xml_sha256 = $XmlSha256
        created_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-TestUtf8 -Path $BackupManifestPath -Text ($manifest | ConvertTo-Json)
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$InstallerPath = Join-Path $RepoRoot 'scripts\install_order_supervisor.ps1'
$tokens = $null
$parseErrors = $null
$installerAst = [Management.Automation.Language.Parser]::ParseFile(
    $InstallerPath,
    [ref]$tokens,
    [ref]$parseErrors
)
Assert-True 'installer parses' (@($parseErrors).Count -eq 0) (($parseErrors | Out-String).Trim())
$installerText = [IO.File]::ReadAllText($InstallerPath, [Text.Encoding]::UTF8)
$actionNormalizationIndex = $installerText.IndexOf('$Action = switch', [StringComparison]::Ordinal)
$pathPreprocessingIndex = $installerText.IndexOf('$WorkspacePathWasExplicit =', [StringComparison]::Ordinal)
Assert-True 'action casing is canonicalized before action-sensitive preprocessing' (
    $actionNormalizationIndex -ge 0 -and
    $pathPreprocessingIndex -gt $actionNormalizationIndex -and
    $installerText.Contains("'rollback' { 'Rollback' }")
)

$requiredFunctions = @(
    'Get-Sha256',
    'Get-WallTimeoutReadback',
    'Read-ValidatedRollbackBackup',
    'Restore-PreviousTask',
    'Stop-ManagedTask',
    'Get-StatusObject',
    'Invoke-InstallAction',
    'Invoke-UninstallAction',
    'Invoke-RollbackAction'
)
foreach ($functionName in $requiredFunctions) {
    $definitions = @($installerAst.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq $functionName
    }, $true))
    Assert-True ('installer defines one ' + $functionName) ($definitions.Count -eq 1)
    if ($definitions.Count -eq 1) { Invoke-Expression $definitions[0].Extent.Text }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-installer-safety-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$TaskName = 'SFDC24 Blackboard Order Worker'
$TaskPath = '\'
$ManagedMarker = 'managed-by=install_order_supervisor.ps1; schema=v1'
$WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$BackupManifestPath = Join-Path $testRoot 'previous-task.json'
$BackupXmlPath = Join-Path $testRoot 'previous-task.xml'
$rollbackRunner = Join-Path $testRoot 'old-release\scripts\order_supervisor.ps1'
New-Item -ItemType Directory -Path (Split-Path -Parent $rollbackRunner) -Force | Out-Null
Write-TestUtf8 -Path $rollbackRunner -Text '# valid prior immutable runner'
$backupXml = New-ValidBackupXml -RollbackRunnerPath $rollbackRunner
[IO.File]::WriteAllText($BackupXmlPath, $backupXml, [Text.Encoding]::Unicode)

try {
    # Queued and unknown states are not quiescent. Restore must reject them
    # before Stop/Register/Unregister can mutate the scheduler definition.
    $script:CurrentTask = [pscustomobject]@{ Description = ('worker; ' + $ManagedMarker); State = 'Queued' }
    $script:MutationLog = New-Object System.Collections.Generic.List[string]
    function Get-RootTask { return $script:CurrentTask }
    function Test-Managed { param($Task) return $true }
    function Stop-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
        $script:MutationLog.Add('stop')
    }
    function Register-ScheduledTask {
        [CmdletBinding()] param([string]$Xml, [string]$TaskName, [string]$TaskPath, [switch]$Force)
        $script:MutationLog.Add('register')
    }
    function Unregister-ScheduledTask {
        [CmdletBinding(SupportsShouldProcess = $true)] param([string]$TaskName, [string]$TaskPath)
        $script:MutationLog.Add('unregister')
    }
    function Export-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
        return $backupXml
    }
    Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $backupXml)
    $queuedRestoreError = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
    Assert-True 'queued task blocks restore before scheduler mutation' (
        $queuedRestoreError -ceq 'task_not_quiescent' -and $script:MutationLog.Count -eq 0
    ) ($queuedRestoreError + '; ' + ($script:MutationLog -join ','))
    $script:CurrentTask.State = 'Unknown'
    $script:MutationLog.Clear()
    $unknownRestoreError = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
    Assert-True 'unknown task blocks restore before scheduler mutation' (
        $unknownRestoreError -ceq 'task_not_quiescent' -and $script:MutationLog.Count -eq 0
    ) ($unknownRestoreError + '; ' + ($script:MutationLog -join ','))

    # A JSON truthy string must never be coerced into the safety-critical
    # previous_existed boolean. Validation occurs before task discovery/mutation.
    $invalidManifest = [ordered]@{
        schema = 'order_supervisor_task_backup.v1'
        previous_existed = 'false'
        xml_sha256 = ''
        created_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-TestUtf8 -Path $BackupManifestPath -Text ($invalidManifest | ConvertTo-Json)
    $script:MutationLog = New-Object System.Collections.Generic.List[string]
    $script:RootTaskReads = 0
    function Get-RootTask { $script:RootTaskReads++; return $null }
    function Test-Managed { param($Task) return $true }
    function Stop-ManagedTask { param($Task) $script:MutationLog.Add('stop') }
    function Register-ScheduledTask {
        [CmdletBinding()] param([string]$Xml, [string]$TaskName, [string]$TaskPath, [switch]$Force)
        $script:MutationLog.Add('register')
    }
    function Unregister-ScheduledTask {
        [CmdletBinding(SupportsShouldProcess = $true)] param([string]$TaskName, [string]$TaskPath)
        $script:MutationLog.Add('unregister')
    }
    function Export-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
        return $script:BackupXmlForMock
    }
    $invalidError = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
    Assert-True 'strict previous_existed rejects a string' ($invalidError -ceq 'rollback_manifest_invalid') $invalidError
    Assert-True 'invalid backup causes no task mutation' ($script:MutationLog.Count -eq 0) ($script:MutationLog -join ',')
    Assert-True 'invalid backup is rejected before task discovery' ($script:RootTaskReads -eq 0) ([string]$script:RootTaskReads)

    $invalidBackupCases = @(
        [pscustomobject]@{
            name = 'extra manifest property'
            expected = 'rollback_manifest_invalid'
            setup = {
                $value = [ordered]@{
                    schema = 'order_supervisor_task_backup.v1'
                    previous_existed = $false
                    xml_sha256 = ''
                    created_at = [DateTime]::UtcNow.ToString('o')
                    unexpected = 'rejected'
                }
                Write-TestUtf8 -Path $BackupManifestPath -Text ($value | ConvertTo-Json)
            }
        },
        [pscustomobject]@{
            name = 'missing backup XML'
            expected = 'rollback_xml_missing'
            setup = {
                if (Test-Path -LiteralPath $BackupXmlPath) { Remove-Item -LiteralPath $BackupXmlPath -Force }
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $backupXml)
            }
        },
        [pscustomobject]@{
            name = 'backup XML digest mismatch'
            expected = 'rollback_xml_digest_mismatch'
            setup = {
                [IO.File]::WriteAllText($BackupXmlPath, $backupXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 ('0' * 64)
            }
        },
        [pscustomobject]@{
            name = 'malformed backup XML'
            expected = 'rollback_xml_invalid'
            setup = {
                $script:CaseXml = '<Task'
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        },
        [pscustomobject]@{
            name = 'wrong managed identity'
            expected = 'rollback_xml_identity_invalid'
            setup = {
                $script:CaseXml = $backupXml.Replace($ManagedMarker, 'managed-by=someone-else')
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        },
        [pscustomobject]@{
            name = 'missing referenced runner'
            expected = 'rollback_runner_missing'
            setup = {
                $missingRollbackRunner = Join-Path $testRoot 'missing-release\scripts\order_supervisor.ps1'
                $script:CaseXml = New-ValidBackupXml -RollbackRunnerPath $missingRollbackRunner
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        }
    )
    foreach ($invalidBackupCase in $invalidBackupCases) {
        $script:MutationLog.Clear()
        $script:RootTaskReads = 0
        & $invalidBackupCase.setup
        $caseError = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
        Assert-True ($invalidBackupCase.name + ' rejects before mutation') (
            $caseError -ceq [string]$invalidBackupCase.expected -and
            $script:MutationLog.Count -eq 0 -and
            $script:RootTaskReads -eq 0
        ) ($caseError + '; mutations=' + ($script:MutationLog -join ',') + '; reads=' + $script:RootTaskReads)
    }

    # A valid prior definition replaces the live task without any unregister
    # gap and is accepted only after byte-exact export readback.
    [IO.File]::WriteAllText($BackupXmlPath, $backupXml, [Text.Encoding]::Unicode)
    Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $backupXml)
    $script:RegisteredXmlForMock = $null
    $script:ExportXmlOverrideForMock = $null
    $script:RegisterSawForce = $false
    $script:CurrentTask = [pscustomobject]@{ Description = ('worker; ' + $ManagedMarker); State = 'Ready' }
    $script:MutationLog = New-Object System.Collections.Generic.List[string]
    function Get-RootTask { return $script:CurrentTask }
    function Test-Managed { param($Task) return $Task -and ([string]$Task.Description).Contains($ManagedMarker) }
    function Stop-ManagedTask { param($Task) $script:MutationLog.Add('stop') }
    function Register-ScheduledTask {
        [CmdletBinding()] param([string]$Xml, [string]$TaskName, [string]$TaskPath, [switch]$Force)
        if (-not $Force) { throw 'test_register_requires_force' }
        $script:RegisterSawForce = $true
        $script:RegisteredXmlForMock = $Xml
        $script:MutationLog.Add('register')
        $script:CurrentTask = [pscustomobject]@{ Description = ('restored; ' + $ManagedMarker); State = 'Ready' }
    }
    function Unregister-ScheduledTask {
        [CmdletBinding(SupportsShouldProcess = $true)] param([string]$TaskName, [string]$TaskPath)
        $script:MutationLog.Add('unregister')
        $script:CurrentTask = $null
    }
    function Export-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
        if ($null -ne $script:ExportXmlOverrideForMock) { return $script:ExportXmlOverrideForMock }
        return $script:RegisteredXmlForMock
    }
    $validRestoreError = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
    Assert-True 'valid rollback restore succeeds' ($validRestoreError -ceq 'NO_ERROR') $validRestoreError
    Assert-True 'valid rollback stops then replaces in place' (
        ($script:MutationLog -join ',') -ceq 'stop,register'
    ) ($script:MutationLog -join ',')
    Assert-True 'valid rollback has no unregister gap' (-not $script:MutationLog.Contains('unregister'))
    Assert-True 'valid rollback registers exact validated XML with Force' (
        $script:RegisterSawForce -and $script:RegisteredXmlForMock -ceq $backupXml
    )

    $script:MutationLog.Clear()
    $script:ExportXmlOverrideForMock = $backupXml + [Environment]::NewLine
    $readbackMismatch = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
    Assert-True 'rollback rejects a non-exact registered definition readback' (
        $readbackMismatch -ceq 'rollback_restore_definition_mismatch'
    ) $readbackMismatch
    Assert-True 'definition proof failure still uses no unregister gap' (
        ($script:MutationLog -join ',') -ceq 'stop,register'
    ) ($script:MutationLog -join ',')
    $script:ExportXmlOverrideForMock = $null

    # previous_existed=false is also fully validated before it can stop/remove.
    Write-BackupManifest -PreviousExisted $false -XmlSha256 ''
    $script:CurrentTask = [pscustomobject]@{ Description = ('worker; ' + $ManagedMarker); State = 'Ready' }
    $script:MutationLog = New-Object System.Collections.Generic.List[string]
    $removeError = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
    Assert-True 'valid absent rollback succeeds' ($removeError -ceq 'NO_ERROR') $removeError
    Assert-True 'valid absent rollback stops removes and reads back' (
        ($script:MutationLog -join ',') -ceq 'stop,unregister' -and $null -eq $script:CurrentTask
    ) ($script:MutationLog -join ',')

    # Recovery orchestration is authority-gated but does not call candidate
    # release/workspace/env/Claude preflight.
    $script:RecoveryCalls = New-Object System.Collections.Generic.List[string]
    function Assert-MutationAuthorityPreflight { $script:RecoveryCalls.Add('authority') }
    function Assert-InstallPreflight { throw 'candidate_preflight_must_not_run' }
    function Restore-PreviousTask { $script:RecoveryCalls.Add('restore') }
    function Get-StatusObject { return [pscustomobject]@{ status = 'RECOVERED' } }
    $rollbackResult = Invoke-RollbackAction
    Assert-True 'rollback is candidate independent' (
        [string]$rollbackResult.status -ceq 'RECOVERED' -and
        ($script:RecoveryCalls -join ',') -ceq 'authority,restore'
    ) ($script:RecoveryCalls -join ',')

    $script:RecoveryCalls.Clear()
    function Get-RootTask { return $null }
    $uninstallResult = Invoke-UninstallAction
    Assert-True 'uninstall is candidate independent' (
        [string]$uninstallResult.status -ceq 'RECOVERED' -and
        ($script:RecoveryCalls -join ',') -ceq 'authority'
    ) ($script:RecoveryCalls -join ',')

    # When installation and its automatic restore both fail, report the fixed
    # combined condition rather than losing the restore failure.
    function Assert-InstallPreflight { }
    function Get-RootTask { return [pscustomobject]@{ Description = $ManagedMarker; State = 'Ready' } }
    function Test-Managed { param($Task) return $true }
    function New-ExpectedDefinition { return [pscustomobject]@{} }
    function Compare-Definition { param($Task) return @('definition_drift') }
    function Save-PreviousTask { param($Existing) }
    function Stop-ManagedTask { param($Task) }
    function Register-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, $InputObject, [switch]$Force)
        throw 'primary_dynamic_failure_must_not_escape'
    }
    function Restore-PreviousTask { throw 'restore_dynamic_failure_must_not_escape' }
    $Start = $false
    $combinedError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallAction }
    Assert-True 'install plus rollback failure is fixed and visible' (
        $combinedError -ceq 'task_install_failed_and_rollback_failed'
    ) $combinedError

    # Reload the real status function after the orchestration mocks.
    $statusDefinition = @($installerAst.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Get-StatusObject'
    }, $true))[0]
    Invoke-Expression $statusDefinition.Extent.Text
    $missingRoot = Join-Path $testRoot 'missing-candidate'
    $UserProfilePath = Join-Path $missingRoot 'profile'
    $WorkspacePath = Join-Path $missingRoot 'workspace'
    $RunnerPath = Join-Path $missingRoot 'release\scripts\order_supervisor.ps1'
    $EnvFile = Join-Path $missingRoot 'workspace\.env'
    $StatePath = Join-Path $missingRoot 'state.json'
    $LogPath = Join-Path $missingRoot 'events.jsonl'
    $WallTimeoutSeconds = 720
    $Mode = 'Execute'
    $WorkspacePathWasExplicit = $true
    $ClaudeCommand = Join-Path $missingRoot 'claude.exe'
    $statusTask = [pscustomobject]@{
        Description = ('worker; ' + $ManagedMarker)
        State = 'Ready'
        Actions = @([pscustomobject]@{ Arguments = '-WallTimeoutSeconds 720' })
    }
    function Get-RootTask { return $script:StatusTaskForMock }
    function Compare-Definition { param($Task) return @() }
    function Get-ScheduledTaskInfo {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
        return [pscustomobject]@{
            LastTaskResult = 0
            LastRunTime = [DateTime]::MinValue
            NextRunTime = [DateTime]::MinValue
            NumberOfMissedRuns = 0
        }
    }
    $script:StatusTaskForMock = $statusTask
    $status = Get-StatusObject
    $expectedReadinessDrift = @(
        'user_profile_missing',
        'workspace_path_missing',
        'workspace_git_directory_missing',
        'runner_missing',
        'v1_bus_env_missing',
        'execute_requires_absolute_claude_command',
        'runner_dependency_missing_OrderSupervisor.psm1',
        'runner_dependency_missing_invoke_order_claude.ps1',
        'runner_dependency_missing_order_supervisor_result.schema.json',
        'runner_dependency_missing_bus.ps1'
    )
    Assert-True 'missing readiness cannot report READY' ([string]$status.status -ceq 'DRIFTED') ([string]$status.status)
    Assert-True 'status exposes fixed readiness drift codes' (
        @($status.drift).Count -eq $expectedReadinessDrift.Count -and
        @($expectedReadinessDrift | Where-Object { @($status.drift) -cnotcontains $_ }).Count -eq 0
    ) (@($status.drift) -join ',')
    Assert-True 'status readiness booleans agree with drift' (
        -not [bool]$status.user_profile_exists -and
        -not [bool]$status.workspace_exists -and
        -not [bool]$status.workspace_git_directory_exists -and
        -not [bool]$status.runner_exists -and
        -not [bool]$status.env_file_exists
    )

    # Observe reads the board but never invokes Claude, so a Git checkout is
    # not part of its readiness boundary.
    $observeRoot = Join-Path $testRoot 'observe-ready'
    $UserProfilePath = Join-Path $observeRoot 'profile'
    $WorkspacePath = Join-Path $observeRoot 'workspace'
    $RunnerPath = Join-Path $observeRoot 'release\scripts\order_supervisor.ps1'
    $EnvFile = Join-Path $WorkspacePath '.env'
    New-Item -ItemType Directory -Path $UserProfilePath -Force | Out-Null
    New-Item -ItemType Directory -Path $WorkspacePath -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $RunnerPath) -Force | Out-Null
    [IO.File]::WriteAllText($RunnerPath, '# observe runner')
    [IO.File]::WriteAllText($EnvFile, 'BUS_URL=https://example.invalid')
    foreach ($dependency in @('OrderSupervisor.psm1', 'invoke_order_claude.ps1', 'order_supervisor_result.schema.json', 'bus.ps1')) {
        [IO.File]::WriteAllText((Join-Path (Split-Path -Parent $RunnerPath) $dependency), '# observe dependency')
    }
    $Mode = 'Observe'
    $observeStatus = Get-StatusObject
    Assert-True 'Observe readiness does not require a Git directory' (
        [string]$observeStatus.status -ceq 'READY' -and
        @($observeStatus.drift).Count -eq 0 -and
        -not [bool]$observeStatus.workspace_git_directory_exists
    ) (@($observeStatus.drift) -join ',')

    $busDependency = Join-Path (Split-Path -Parent $RunnerPath) 'bus.ps1'
    Remove-Item -LiteralPath $busDependency -Force
    $missingDependencyStatus = Get-StatusObject
    Assert-True 'missing release dependency cannot report READY' (
        [string]$missingDependencyStatus.status -ceq 'DRIFTED' -and
        @($missingDependencyStatus.drift) -ccontains 'runner_dependency_missing_bus.ps1'
    ) (@($missingDependencyStatus.drift) -join ',')
    [IO.File]::WriteAllText($busDependency, '# observe dependency')

    $absoluteUserProfilePath = $UserProfilePath
    $UserProfilePath = '.'
    $relativeProfileStatus = Get-StatusObject
    Assert-True 'relative user profile cannot report READY' (
        [string]$relativeProfileStatus.status -ceq 'DRIFTED' -and
        @($relativeProfileStatus.drift) -ccontains 'user_profile_path_must_be_absolute' -and
        -not [bool]$relativeProfileStatus.user_profile_exists
    ) (@($relativeProfileStatus.drift) -join ',')
    $UserProfilePath = $absoluteUserProfilePath

    $statusTask.State = 'Queued'
    $queuedStatus = Get-StatusObject
    Assert-True 'Queued task cannot report READY' (
        [string]$queuedStatus.status -ceq 'NOT_READY'
    ) ([string]$queuedStatus.status)
    $statusTask.State = 'Unknown'
    $unknownStatus = Get-StatusObject
    Assert-True 'Unknown task cannot report READY' (
        [string]$unknownStatus.status -ceq 'NOT_READY'
    ) ([string]$unknownStatus.status)

    # Exercise the real script boundary: ValidateSet accepts lowercase, so the
    # script itself must canonicalize before case-exact dispatch.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lowerStatusOutput = @(
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $InstallerPath -Action status 2>&1
        )
        $lowerStatusExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $lowerStatusText = ($lowerStatusOutput | ForEach-Object { [string]$_ }) -join "`n"
    $lowerStatusJson = $null
    try { $lowerStatusJson = $lowerStatusText | ConvertFrom-Json } catch { }
    Assert-True 'lowercase Status dispatch returns a real status receipt' (
        $lowerStatusExit -eq 0 -and $null -ne $lowerStatusJson -and
        -not [string]::IsNullOrWhiteSpace([string]$lowerStatusJson.status)
    ) $lowerStatusText
} finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $testRoot -PathType Container)) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
