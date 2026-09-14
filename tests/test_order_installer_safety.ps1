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
      <UserId>S-1-5-18</UserId>
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
    $installerText.Contains("'installfromdisablednostop' { 'InstallFromDisabledNoStop' }") -and
    $installerText.Contains("'rollback' { 'Rollback' }")
)

$requiredFunctions = @(
    'Get-Sha256',
    'Write-Utf8',
    'Get-WallTimeoutReadback',
    'Assert-SystemServiceAccountPrincipal',
    'Save-PreviousTask',
    'Read-ValidatedRollbackBackup',
    'Restore-PreviousTask',
    'Stop-ManagedTask',
    'Get-StatusObject',
    'Invoke-InstallAction',
    'Invoke-InstallFromDisabledNoStopAction',
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

$installerFunctionDefinitions = @{}
foreach ($definition in @($installerAst.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst]
}, $true))) {
    $installerFunctionDefinitions[[string]$definition.Name] = $definition
}
$noStopDefinitions = @($installerFunctionDefinitions['Invoke-InstallFromDisabledNoStopAction'])
$forbiddenNoStopCommands = @(
    'Stop-ManagedTask',
    'Stop-ScheduledTask',
    'Restore-PreviousTask',
    'Start-ScheduledTask',
    'Unregister-ScheduledTask',
    'Stop-Process',
    'taskkill',
    'taskkill.exe',
    'schtasks',
    'schtasks.exe'
)
$reachableForbiddenCommands = @()
if ($noStopDefinitions.Count -eq 1) {
    $pendingFunctions = New-Object System.Collections.Generic.Stack[string]
    $visitedFunctions = @{}
    $pendingFunctions.Push('Invoke-InstallFromDisabledNoStopAction')
    while ($pendingFunctions.Count -gt 0) {
        $currentFunctionName = $pendingFunctions.Pop()
        if ($visitedFunctions.ContainsKey($currentFunctionName)) { continue }
        $visitedFunctions[$currentFunctionName] = $true
        $currentFunction = $installerFunctionDefinitions[$currentFunctionName]
        foreach ($command in @($currentFunction.FindAll({
            param($node)
            $node -is [Management.Automation.Language.CommandAst]
        }, $true))) {
            $commandName = [string]$command.GetCommandName()
            if ([string]::IsNullOrWhiteSpace($commandName)) { continue }
            if ($forbiddenNoStopCommands -ccontains $commandName) {
                $reachableForbiddenCommands += ($currentFunctionName + '->' + $commandName)
            }
            if ($installerFunctionDefinitions.ContainsKey($commandName) -and
                -not $visitedFunctions.ContainsKey($commandName)) {
                $pendingFunctions.Push($commandName)
            }
        }
    }
}
Assert-True 'disabled no-stop action transitively contains no task or process stop start unregister or restore command' (
    $noStopDefinitions.Count -eq 1 -and $reachableForbiddenCommands.Count -eq 0
) ($reachableForbiddenCommands -join ',')

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-installer-safety-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$TaskName = 'SFDC24 Blackboard Order Worker'
$TaskPath = '\'
$ManagedMarker = 'managed-by=install_order_supervisor.ps1; schema=v1'
$WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$BackupManifestPath = Join-Path $testRoot 'previous-task.json'
$BackupXmlPath = Join-Path $testRoot 'previous-task.xml'
$MetadataRoot = $testRoot
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

    $systemBackupWithoutLogonType = $backupXml.Replace(
        '      <LogonType>ServiceAccount</LogonType>',
        ''
    )
    $omittedLogonDocument = New-Object Xml.XmlDocument
    $omittedLogonDocument.LoadXml($systemBackupWithoutLogonType)
    $omittedLogonNamespace = New-Object Xml.XmlNamespaceManager($omittedLogonDocument.NameTable)
    $omittedLogonNamespace.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    Assert-True 'omitted-LogonType fixture has canonical SID and no LogonType element in any namespace' (
        @($omittedLogonDocument.SelectNodes('/t:Task/t:Principals/t:Principal/*[local-name()="LogonType"]', $omittedLogonNamespace)).Count -eq 0 -and
        [string]$omittedLogonDocument.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId', $omittedLogonNamespace).InnerText -ceq 'S-1-5-18'
    )
    $script:BackupXmlForMock = $systemBackupWithoutLogonType
    $safeExistingTask = [pscustomobject]@{
        Principal = [pscustomobject]@{ UserId = 'SYSTEM'; LogonType = 'ServiceAccount' }
    }
    $systemBackupWithoutLogonTypeError = Invoke-ExpectedFailure -ScriptBlock {
        Save-PreviousTask -Existing $safeExistingTask
        $savedBackup = Read-ValidatedRollbackBackup
        if ([string]$savedBackup.xml -cne $systemBackupWithoutLogonType) { throw 'saved_xml_mismatch' }
    }
    Assert-True 'backup capture and validation accept canonical SYSTEM XML with omitted optional LogonType' (
        $systemBackupWithoutLogonTypeError -ceq 'NO_ERROR' -and
        $script:MutationLog.Count -eq 0 -and $script:RootTaskReads -eq 0
    ) $systemBackupWithoutLogonTypeError

    $backupBytesBeforeUnsafeSave = [IO.File]::ReadAllBytes($BackupXmlPath)
    $manifestBytesBeforeUnsafeSave = [IO.File]::ReadAllBytes($BackupManifestPath)
    $unsafeExistingTask = [pscustomobject]@{
        Principal = [pscustomobject]@{ UserId = 'SYSTEM'; LogonType = 'Password' }
    }
    $unsafeSaveError = Invoke-ExpectedFailure -ScriptBlock { Save-PreviousTask -Existing $unsafeExistingTask }
    Assert-True 'backup capture rejects a non-ServiceAccount live principal before changing backup files' (
        $unsafeSaveError -ceq 'backup_task_principal_invalid' -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($BackupXmlPath)) -ceq [Convert]::ToBase64String($backupBytesBeforeUnsafeSave) -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($BackupManifestPath)) -ceq [Convert]::ToBase64String($manifestBytesBeforeUnsafeSave)
    ) $unsafeSaveError

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
            name = 'missing logon type for a non-SYSTEM principal'
            expected = 'rollback_xml_identity_invalid'
            setup = {
                $script:CaseXml = $systemBackupWithoutLogonType.Replace('<UserId>S-1-5-18</UserId>', '<UserId>LOCAL SERVICE</UserId>')
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        },
        [pscustomobject]@{
            name = 'missing logon type for a noncanonical SYSTEM alias'
            expected = 'rollback_xml_identity_invalid'
            setup = {
                $script:CaseXml = $systemBackupWithoutLogonType.Replace('<UserId>S-1-5-18</UserId>', '<UserId>SYSTEM</UserId>')
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        },
        [pscustomobject]@{
            name = 'explicit non-ServiceAccount logon type'
            expected = 'rollback_xml_identity_invalid'
            setup = {
                $script:CaseXml = $backupXml.Replace('<LogonType>ServiceAccount</LogonType>', '<LogonType>Password</LogonType>')
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        },
        [pscustomobject]@{
            name = 'duplicate logon type elements'
            expected = 'rollback_xml_identity_invalid'
            setup = {
                $script:CaseXml = $backupXml.Replace(
                    '<LogonType>ServiceAccount</LogonType>',
                    '<LogonType>ServiceAccount</LogonType><LogonType>ServiceAccount</LogonType>'
                )
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        },
        [pscustomobject]@{
            name = 'foreign namespace logon type lookalike'
            expected = 'rollback_xml_identity_invalid'
            setup = {
                $script:CaseXml = $backupXml.Replace(
                    '<LogonType>ServiceAccount</LogonType>',
                    '<LogonType xmlns="">Password</LogonType>'
                )
                [IO.File]::WriteAllText($BackupXmlPath, $script:CaseXml, [Text.Encoding]::Unicode)
                Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $script:CaseXml)
            }
        },
        [pscustomobject]@{
            name = 'duplicate user id elements'
            expected = 'rollback_xml_identity_invalid'
            setup = {
                $script:CaseXml = $backupXml.Replace(
                    '<UserId>S-1-5-18</UserId>',
                    '<UserId>S-1-5-18</UserId><UserId>S-1-5-18</UserId>'
                )
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
    [IO.File]::WriteAllText($BackupXmlPath, $systemBackupWithoutLogonType, [Text.Encoding]::Unicode)
    Write-BackupManifest -PreviousExisted $true -XmlSha256 (Get-Sha256 $systemBackupWithoutLogonType)
    $script:RegisteredXmlForMock = $null
    $script:ExportXmlOverrideForMock = $null
    $script:RegisterSawForce = $false
    $script:RestoredPrincipalUserId = 'SYSTEM'
    $script:RestoredPrincipalLogonType = 'ServiceAccount'
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
        $script:CurrentTask = [pscustomobject]@{
            Description = ('restored; ' + $ManagedMarker)
            State = 'Ready'
            Principal = [pscustomobject]@{
                UserId = $script:RestoredPrincipalUserId
                LogonType = $script:RestoredPrincipalLogonType
            }
        }
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
        $script:RegisterSawForce -and $script:RegisteredXmlForMock -ceq $systemBackupWithoutLogonType
    )

    $script:MutationLog.Clear()
    $script:RestoredPrincipalLogonType = 'Password'
    $invalidPrincipalReadback = Invoke-ExpectedFailure -ScriptBlock { Restore-PreviousTask }
    Assert-True 'rollback rejects a restored task whose live principal is not ServiceAccount' (
        $invalidPrincipalReadback -ceq 'rollback_restore_principal_invalid' -and
        ($script:MutationLog -join ',') -ceq 'stop,register'
    ) ($invalidPrincipalReadback + '; ' + ($script:MutationLog -join ','))

    $script:MutationLog.Clear()
    $script:RestoredPrincipalLogonType = 'ServiceAccount'
    $script:ExportXmlOverrideForMock = $systemBackupWithoutLogonType + [Environment]::NewLine
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

    # Exercise the production Save -> Validate boundary through the incident
    # no-stop action using the canonical Task Scheduler export that omits
    # LogonType. All scheduler effects remain mocked and bounded.
    Invoke-Expression $installerFunctionDefinitions['Save-PreviousTask'].Extent.Text
    Invoke-Expression $installerFunctionDefinitions['Read-ValidatedRollbackBackup'].Extent.Text
    $script:RealNoStopCurrentReads = 0
    $script:RealNoStopExportReads = 0
    $script:RealNoStopMutations = New-Object System.Collections.Generic.List[string]
    $script:RealNoStopExisting = [pscustomobject]@{
        managed = $true
        State = 'Disabled'
        Principal = [pscustomobject]@{ UserId = 'SYSTEM'; LogonType = 'ServiceAccount' }
    }
    function Assert-InstallPreflight { }
    function Test-Managed { param($Task) return $Task -and [bool]$Task.managed }
    function Get-RootTask {
        $script:RealNoStopCurrentReads++
        if ($script:RealNoStopCurrentReads -le 2) { return $script:RealNoStopExisting }
        return [pscustomobject]@{ managed = $true; State = 'Ready' }
    }
    function Export-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
        $script:RealNoStopExportReads++
        return $systemBackupWithoutLogonType
    }
    function New-ExpectedDefinition { return [pscustomobject]@{ definition = 'candidate' } }
    function Register-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, $InputObject, [switch]$Force)
        if (-not $Force) { throw 'test_register_requires_force' }
        $script:RealNoStopMutations.Add('register')
    }
    function Compare-Definition { param($Task) return @() }
    function Get-StatusObject { return [pscustomobject]@{ status = 'READY' } }
    $Mode = 'Observe'
    $Start = $false
    $ExpectedCurrentTaskXmlSha256 = Get-Sha256 -Text $systemBackupWithoutLogonType
    $realNoStopError = Invoke-ExpectedFailure -ScriptBlock {
        $script:RealNoStopResult = Invoke-InstallFromDisabledNoStopAction
    }
    Assert-True 'disabled no-stop production backup path accepts canonical omitted LogonType and registers once' (
        $realNoStopError -ceq 'NO_ERROR' -and
        [string]$script:RealNoStopResult.status -ceq 'READY' -and
        $script:RealNoStopCurrentReads -eq 3 -and
        $script:RealNoStopExportReads -eq 3 -and
        ($script:RealNoStopMutations -join ',') -ceq 'register' -and
        [IO.File]::ReadAllText($BackupXmlPath, [Text.Encoding]::Unicode) -ceq $systemBackupWithoutLogonType
    ) ($realNoStopError + '; reads=' + $script:RealNoStopCurrentReads + '; exports=' + $script:RealNoStopExportReads + '; mutations=' + ($script:RealNoStopMutations -join ','))

    # The incident-only installer action is a structurally separate contract.
    # It starts only from a managed Disabled task, re-reads that state just
    # before registration, and deliberately has no automatic rollback path.
    $script:NoStopPreflightCalls = 0
    $script:NoStopMutations = New-Object System.Collections.Generic.List[string]
    $script:NoStopAuthenticatedXml = '<Task><Settings><Enabled>false</Enabled></Settings></Task>'
    $ExpectedCurrentTaskXmlSha256 = Get-Sha256 -Text $script:NoStopAuthenticatedXml
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    function Assert-InstallPreflight { $script:NoStopPreflightCalls++ }
    function Test-Managed { param($Task) return $Task -and [bool]$Task.managed }
    function New-ExpectedDefinition { return [pscustomobject]@{ definition = 'candidate' } }
    function Save-PreviousTask { param($Existing) $script:NoStopMutations.Add('backup') }
    function Read-ValidatedRollbackBackup {
        return [pscustomobject]@{
            previous_existed = $true
            xml_sha256 = $script:NoStopBackupSha256
        }
    }
    function Export-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
        $script:NoStopExportCalls++
        if ($script:NoStopExportCalls -eq 2 -and
            -not [string]::IsNullOrEmpty($script:NoStopSecondExportXml)) {
            return $script:NoStopSecondExportXml
        }
        return $script:NoStopAuthenticatedXml
    }
    function Register-ScheduledTask {
        [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, $InputObject, [switch]$Force)
        $script:NoStopMutations.Add('register')
        if ($script:NoStopRegisterFailure) { throw $script:NoStopRegisterFailure }
    }
    function Compare-Definition {
        param($Task)
        if ($script:NoStopDefinitionDrift) { return @('definition_drift') }
        return @()
    }
    function Get-StatusObject { return [pscustomobject]@{ status = $script:NoStopStatus } }
    function Stop-ManagedTask { param($Task) $script:NoStopMutations.Add('stop-managed') }
    function Stop-ScheduledTask { $script:NoStopMutations.Add('stop-scheduled') }
    function Restore-PreviousTask { $script:NoStopMutations.Add('restore') }
    function Start-ScheduledTask { $script:NoStopMutations.Add('start') }
    function Unregister-ScheduledTask { $script:NoStopMutations.Add('unregister') }

    $Mode = 'Execute'
    $Start = $false
    $script:NoStopPreflightCalls = 0
    $script:NoStopStatus = 'READY'
    $wrongModeError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop action rejects Execute before preflight' (
        $wrongModeError -ceq 'disabled_no_stop_requires_observe' -and $script:NoStopPreflightCalls -eq 0
    ) $wrongModeError

    $Mode = 'Observe'
    $Start = $true
    $script:NoStopPreflightCalls = 0
    $startError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop action rejects Start before preflight' (
        $startError -ceq 'disabled_no_stop_forbids_start' -and $script:NoStopPreflightCalls -eq 0
    ) $startError

    $Start = $false
    $ExpectedCurrentTaskXmlSha256 = 'not-a-digest'
    $script:NoStopPreflightCalls = 0
    $digestInputError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop action rejects a noncanonical expected XML digest before preflight' (
        $digestInputError -ceq 'disabled_no_stop_expected_xml_sha256_invalid' -and
        $script:NoStopPreflightCalls -eq 0
    ) $digestInputError
    $ExpectedCurrentTaskXmlSha256 = Get-Sha256 -Text $script:NoStopAuthenticatedXml

    foreach ($initialCase in @(
        [pscustomobject]@{ name = 'missing'; task = $null; expected = 'disabled_no_stop_requires_existing_task' },
        [pscustomobject]@{ name = 'unmanaged'; task = [pscustomobject]@{ managed = $false; State = 'Disabled' }; expected = 'refusing_to_overwrite_unmanaged_task' },
        [pscustomobject]@{ name = 'Ready'; task = [pscustomobject]@{ managed = $true; State = 'Ready' }; expected = 'disabled_no_stop_requires_disabled_task' },
        [pscustomobject]@{ name = 'Running'; task = [pscustomobject]@{ managed = $true; State = 'Running' }; expected = 'disabled_no_stop_requires_disabled_task' },
        [pscustomobject]@{ name = 'Queued'; task = [pscustomobject]@{ managed = $true; State = 'Queued' }; expected = 'disabled_no_stop_requires_disabled_task' },
        [pscustomobject]@{ name = 'Unknown'; task = [pscustomobject]@{ managed = $true; State = 'Unknown' }; expected = 'disabled_no_stop_requires_disabled_task' }
    )) {
        $script:NoStopCurrentTask = $initialCase.task
        function Get-RootTask { return $script:NoStopCurrentTask }
        $script:NoStopMutations.Clear()
        $script:NoStopPreflightCalls = 0
        $script:NoStopRegisterFailure = ''
        $script:NoStopDefinitionDrift = $false
        $script:NoStopExportCalls = 0
        $script:NoStopSecondExportXml = ''
        $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
        $initialError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
        Assert-True ('disabled no-stop action rejects initial ' + $initialCase.name + ' before mutation') (
            $initialError -ceq [string]$initialCase.expected -and
            $script:NoStopPreflightCalls -eq 1 -and
            $script:NoStopMutations.Count -eq 0
        ) ($initialError + '; ' + ($script:NoStopMutations -join ','))
    }

    $script:NoStopCurrentTask = [pscustomobject]@{ managed = $true; State = 'Disabled' }
    function Get-RootTask { return $script:NoStopCurrentTask }
    $script:NoStopMutations.Clear()
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    $ExpectedCurrentTaskXmlSha256 = ('0' * 64)
    $initialXmlMismatch = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop action rejects wrong caller XML identity before backup or registration' (
        $initialXmlMismatch -ceq 'disabled_no_stop_task_xml_mismatch' -and
        $script:NoStopMutations.Count -eq 0
    ) ($initialXmlMismatch + '; ' + ($script:NoStopMutations -join ','))
    $ExpectedCurrentTaskXmlSha256 = Get-Sha256 -Text $script:NoStopAuthenticatedXml

    $script:NoStopReadCount = 0
    function Get-RootTask {
        $script:NoStopReadCount++
        if ($script:NoStopReadCount -eq 1) {
            return [pscustomobject]@{ managed = $true; State = 'Disabled' }
        }
        return [pscustomobject]@{ managed = $true; State = 'Running' }
    }
    $script:NoStopMutations.Clear()
    $script:NoStopRegisterFailure = ''
    $script:NoStopDefinitionDrift = $false
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    $preRegisterRaceError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop action refuses a pre-register Running race without stop or register' (
        $preRegisterRaceError -ceq 'disabled_no_stop_task_changed_before_register' -and
        ($script:NoStopMutations -join ',') -ceq 'backup'
    ) ($preRegisterRaceError + '; ' + ($script:NoStopMutations -join ','))

    function Get-RootTask { return [pscustomobject]@{ managed = $true; State = 'Disabled' } }
    $script:NoStopMutations.Clear()
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = '<Task><Settings><Enabled>false</Enabled></Settings><RegistrationInfo><Description>concurrent</Description></RegistrationInfo></Task>'
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    $definitionRaceError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop action rejects a managed Disabled definition replacement before registration' (
        $definitionRaceError -ceq 'disabled_no_stop_task_changed_before_register' -and
        ($script:NoStopMutations -join ',') -ceq 'backup'
    ) ($definitionRaceError + '; ' + ($script:NoStopMutations -join ','))

    $script:NoStopMutations.Clear()
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = ('0' * 64)
    $backupIdentityError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop action rejects a backup not bound to the caller XML before registration' (
        $backupIdentityError -ceq 'disabled_no_stop_backup_identity_mismatch' -and
        ($script:NoStopMutations -join ',') -ceq 'backup'
    ) ($backupIdentityError + '; ' + ($script:NoStopMutations -join ','))

    function Get-RootTask { return [pscustomobject]@{ managed = $true; State = 'Disabled' } }
    $script:NoStopMutations.Clear()
    $script:NoStopRegisterFailure = 'register_race_failure'
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    $registerRaceError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop registration failure never stops or auto-restores' (
        $registerRaceError -ceq 'register_race_failure' -and
        ($script:NoStopMutations -join ',') -ceq 'backup,register'
    ) ($registerRaceError + '; ' + ($script:NoStopMutations -join ','))

    $script:NoStopReadCount = 0
    function Get-RootTask {
        $script:NoStopReadCount++
        if ($script:NoStopReadCount -le 2) {
            return [pscustomobject]@{ managed = $true; State = 'Disabled' }
        }
        return [pscustomobject]@{ managed = $true; State = 'Running' }
    }
    $script:NoStopMutations.Clear()
    $script:NoStopRegisterFailure = ''
    $script:NoStopDefinitionDrift = $true
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    $postRegisterRaceError = Invoke-ExpectedFailure -ScriptBlock { Invoke-InstallFromDisabledNoStopAction | Out-Null }
    Assert-True 'disabled no-stop readback failure leaves quarantine to the driver without stop or restore' (
        $postRegisterRaceError -ceq 'task_readback_drift:definition_drift' -and
        ($script:NoStopMutations -join ',') -ceq 'backup,register'
    ) ($postRegisterRaceError + '; ' + ($script:NoStopMutations -join ','))

    $script:NoStopReadCount = 0
    function Get-RootTask {
        $script:NoStopReadCount++
        if ($script:NoStopReadCount -le 2) {
            return [pscustomobject]@{ managed = $true; State = 'Disabled' }
        }
        return [pscustomobject]@{ managed = $true; State = 'Running' }
    }
    $script:NoStopMutations.Clear()
    $script:NoStopDefinitionDrift = $false
    $script:NoStopStatus = 'RUNNING'
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    $runningNoStopResult = Invoke-InstallFromDisabledNoStopAction
    Assert-True 'disabled no-stop action reports a matching post-register Running race without stop or restore' (
        [string]$runningNoStopResult.status -ceq 'RUNNING' -and
        ($script:NoStopMutations -join ',') -ceq 'backup,register'
    ) ($script:NoStopMutations -join ',')

    $script:NoStopReadCount = 0
    function Get-RootTask {
        $script:NoStopReadCount++
        if ($script:NoStopReadCount -le 2) {
            return [pscustomobject]@{ managed = $true; State = 'Disabled' }
        }
        return [pscustomobject]@{ managed = $true; State = 'Ready' }
    }
    $script:NoStopMutations.Clear()
    $script:NoStopDefinitionDrift = $false
    $script:NoStopStatus = 'READY'
    $script:NoStopExportCalls = 0
    $script:NoStopSecondExportXml = ''
    $script:NoStopBackupSha256 = $ExpectedCurrentTaskXmlSha256
    $noStopResult = Invoke-InstallFromDisabledNoStopAction
    Assert-True 'disabled no-stop action backs up and force-registers exactly once on success' (
        [string]$noStopResult.status -ceq 'READY' -and
        ($script:NoStopMutations -join ',') -ceq 'backup,register'
    ) ($script:NoStopMutations -join ',')

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
