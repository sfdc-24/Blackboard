#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

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

function Assert-ThrowsCode {
    param(
        [string]$Name,
        [scriptblock]$Operation,
        [string]$ExpectedCode
    )
    $actual = ''
    try { & $Operation | Out-Null }
    catch { $actual = [string]$_.Exception.Message }
    Assert-True $Name ($actual -ceq $ExpectedCode)
}

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ToolPath = Join-Path $RepoRoot 'infra\azure\order_task_escrow.ps1'
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('order-task-escrow-test-' + [Guid]::NewGuid().ToString('N'))
$originalProgramData = [Environment]::GetEnvironmentVariable('ProgramData', 'Process')
$oldRelease = '1111111111111111111111111111111111111111'
$candidateRelease = '2222222222222222222222222222222222222222'
$testEscrowId = 'pre-candidate-1111111'

try {
    New-Item -ItemType Directory -Path $tempRoot -ErrorAction Stop | Out-Null
    [Environment]::SetEnvironmentVariable('ProgramData', $tempRoot, 'Process')
    . $ToolPath

    function New-TestRelease {
        param([string]$ReleaseId)
        $context = Get-OrderEscrowContext
        $releasePath = Join-Path ([string]$context.release_root) $ReleaseId
        $scriptsPath = Join-Path $releasePath 'scripts'
        New-Item -ItemType Directory -Path $scriptsPath -Force | Out-Null
        [IO.File]::WriteAllText(
            (Join-Path $releasePath '.release.json'),
            ('{"schema":"blackboard.order-worker-release.v1","release_id":"' + $ReleaseId + '","archive_sha256":"' + ('a' * 64) + '","installed_at_utc":"2026-09-06T00:00:00.0000000Z"}'),
            (New-Object Text.UTF8Encoding($false))
        )
        foreach ($relativePath in $script:OrderEscrowReleaseFiles) {
            $leaf = Join-Path $releasePath $relativePath
            [IO.File]::WriteAllText($leaf, ('known-good:' + $relativePath), (New-Object Text.UTF8Encoding($false)))
        }
        return $releasePath
    }

    function New-TestTaskXml {
        param([string]$ReleaseId, [string]$Description = '')
        if ([string]::IsNullOrWhiteSpace($Description)) {
            $Description = 'SFDC24 Blackboard ORDER worker; ' + $script:OrderEscrowManagedMarker
        }
        $context = Get-OrderEscrowContext
        $runner = Get-OrderExpectedRunnerPath -Context $context -ReleaseId $ReleaseId
        $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $profile = 'C:\Users\akatiawam'
        $workspace = 'C:\Users\akatiawam\Blackboard'
        $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $runner + '" -Mode Execute -AllowedSourcesCsv "chat-mobile,codex"' +
            ' -UserProfilePath "' + $profile + '" -WorkspacePath "' + $workspace + '" -EnvFile "' + (Join-Path $workspace '.env') +
            '" -StatePath "' + (Join-Path ([string]$context.metadata_root) 'state.json') + '" -LogPath "' +
            (Join-Path ([string]$context.metadata_root) 'events.jsonl') + '" -WallTimeoutSeconds 720 -ClaudeCommand "' +
            (Join-Path $profile '.local\bin\claude.exe') + '"'
        return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\SFDC24 Blackboard Order Worker</URI>
    <Description>$Description</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger id="AtBoot"><Enabled>true</Enabled></BootTrigger>
    <TimeTrigger id="Every15Minutes"><Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>2026-09-06T00:00:00</StartBoundary><Enabled>true</Enabled></TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>S-1-5-18</UserId><LogonType>ServiceAccount</LogonType><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>$windowsPowerShell</Command><Arguments>$arguments</Arguments><WorkingDirectory>C:\Users\akatiawam\Blackboard</WorkingDirectory></Exec>
  </Actions>
</Task>
"@
    }

    function New-TestTask {
        param(
            [string]$ReleaseId,
            [string]$State = 'Ready',
            [string]$Description = '',
            [string]$TaskPath = '\'
        )
        if ([string]::IsNullOrWhiteSpace($Description)) {
            $Description = 'SFDC24 Blackboard ORDER worker; ' + $script:OrderEscrowManagedMarker
        }
        $context = Get-OrderEscrowContext
        $runner = Get-OrderExpectedRunnerPath -Context $context -ReleaseId $ReleaseId
        $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $profile = 'C:\Users\akatiawam'
        $workspace = 'C:\Users\akatiawam\Blackboard'
        $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $runner + '" -Mode Execute -AllowedSourcesCsv "chat-mobile,codex"' +
            ' -UserProfilePath "' + $profile + '" -WorkspacePath "' + $workspace + '" -EnvFile "' + (Join-Path $workspace '.env') +
            '" -StatePath "' + (Join-Path ([string]$context.metadata_root) 'state.json') + '" -LogPath "' +
            (Join-Path ([string]$context.metadata_root) 'events.jsonl') + '" -WallTimeoutSeconds 720 -ClaudeCommand "' +
            (Join-Path $profile '.local\bin\claude.exe') + '"'
        $boot = [pscustomobject]@{
            Id = 'AtBoot'
            CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskBootTrigger' }
        }
        $interval = [pscustomobject]@{
            Id = 'Every15Minutes'
            CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskTimeTrigger' }
            Repetition = [pscustomobject]@{ Interval = 'PT15M' }
        }
        return [pscustomobject]@{
            TaskName = $script:OrderEscrowTaskName
            TaskPath = $TaskPath
            Description = $Description
            State = $State
            Actions = @([pscustomobject]@{
                Execute = $windowsPowerShell
                Arguments = $arguments
                WorkingDirectory = 'C:\Users\akatiawam\Blackboard'
            })
            Principal = [pscustomobject]@{ UserId = 'SYSTEM'; LogonType = 'ServiceAccount' }
            Settings = [pscustomobject]@{ MultipleInstances = 'IgnoreNew' }
            Triggers = @($boot, $interval)
        }
    }

    function Reset-TestScheduler {
        param([string]$CurrentRelease = $oldRelease, [string]$CurrentState = 'Ready')
        $script:MockTasks = @(New-TestTask -ReleaseId $CurrentRelease -State $CurrentState)
        $script:MockExportXml = New-TestTaskXml -ReleaseId $CurrentRelease
        $script:MockRestoredTask = New-TestTask -ReleaseId $oldRelease -State 'Ready'
        $script:MockRegisteredXml = ''
        $script:MockReadbackOverride = ''
        $script:RegisterUsedForce = $false
        $script:StopCalls = 0
        $script:RegisterCalls = 0
        $script:UnregisterCalls = 0
        $script:SleepCalls = 0
    }

    function Get-ScheduledTask {
        param([string]$TaskName, [string]$TaskPath, $ErrorAction)
        return @($script:MockTasks)
    }

    function Export-ScheduledTask {
        param([string]$TaskName, [string]$TaskPath, $ErrorAction)
        if ($script:RegisterCalls -gt 0) {
            if (-not [string]::IsNullOrWhiteSpace($script:MockReadbackOverride)) {
                return $script:MockReadbackOverride
            }
            return $script:MockRegisteredXml
        }
        return $script:MockExportXml
    }

    function Stop-ScheduledTask {
        param([string]$TaskName, [string]$TaskPath, $ErrorAction)
        $script:StopCalls++
        foreach ($task in @($script:MockTasks)) { $task.State = 'Ready' }
    }

    function Register-ScheduledTask {
        param([string]$Xml, [string]$TaskName, [string]$TaskPath, [switch]$Force, $ErrorAction)
        $script:RegisterCalls++
        $script:RegisterUsedForce = [bool]$Force
        $script:MockRegisteredXml = $Xml
        $script:MockTasks = @($script:MockRestoredTask)
        return $script:MockRestoredTask
    }

    function Unregister-ScheduledTask {
        param([string]$TaskName, [string]$TaskPath, [switch]$Confirm, $ErrorAction)
        $script:UnregisterCalls++
        throw 'unregister_must_not_be_called'
    }

    function Start-Sleep {
        param([int]$Milliseconds)
        $script:SleepCalls++
    }

    function Test-OrderAdministrator { return $true }

    function Get-TestTrustedHashesBase64 {
        param([string]$ReleasePath)
        $map = [ordered]@{}
        foreach ($relativePath in $script:OrderEscrowReleaseFiles) {
            $map[$relativePath] = Get-OrderFileSha256 -Path (Join-Path $ReleasePath $relativePath)
        }
        $json = $map | ConvertTo-Json -Compress
        return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    }

    $releasePath = New-TestRelease -ReleaseId $oldRelease
    $trustedHashesBase64 = Get-TestTrustedHashesBase64 -ReleasePath $releasePath
    Reset-TestScheduler

    $alteredTask = New-TestTask -ReleaseId $oldRelease
    $alteredTask.Actions[0].Arguments = $alteredTask.Actions[0].Arguments.Replace(
        '-AllowedSourcesCsv "chat-mobile,codex"',
        '-AllowedSourcesCsv "evil" -Unexpected injected'
    )
    Assert-ThrowsCode 'Exact action contract rejects altered allowed sources and unknown arguments' {
        Assert-OrderLiveTaskSafe -Task $alteredTask -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_action_arguments_invalid'
    $alteredPathTask = New-TestTask -ReleaseId $oldRelease
    $alteredPathTask.Actions[0].Arguments = $alteredPathTask.Actions[0].Arguments.Replace(
        '-WorkspacePath "C:\Users\akatiawam\Blackboard"',
        '-WorkspacePath "C:\Alternate\Blackboard"'
    )
    Assert-ThrowsCode 'Exact action contract rejects a noncanonical workspace path' {
        Assert-OrderLiveTaskSafe -Task $alteredPathTask -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_action_path_contract_invalid'
    $normalizedAliasTask = New-TestTask -ReleaseId $oldRelease
    $normalizedAliasTask.Actions[0].Arguments = $normalizedAliasTask.Actions[0].Arguments.Replace(
        '-WorkspacePath "C:\Users\akatiawam\Blackboard"',
        '-WorkspacePath "C:\Users\akatiawam\Blackboard\."'
    )
    Assert-ThrowsCode 'Exact action contract rejects a path alias that normalizes to the canonical workspace' {
        Assert-OrderLiveTaskSafe -Task $normalizedAliasTask -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_action_path_contract_invalid'
    $trailingLineFeedTask = New-TestTask -ReleaseId $oldRelease
    $trailingLineFeedTask.Actions[0].Arguments += "`n"
    Assert-ThrowsCode 'Exact action contract rejects a trailing line feed' {
        Assert-OrderLiveTaskSafe -Task $trailingLineFeedTask -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_action_arguments_invalid'

    $missingLiveLogonTypeTask = New-TestTask -ReleaseId $oldRelease
    $missingLiveLogonTypeTask.Principal.LogonType = $null
    Assert-ThrowsCode 'Live task validation still requires ServiceAccount logon type' {
        Assert-OrderLiveTaskSafe -Task $missingLiveLogonTypeTask -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_principal_invalid'

    $systemXmlWithoutLogonType = (New-TestTaskXml -ReleaseId $oldRelease).Replace(
        '<LogonType>ServiceAccount</LogonType>',
        ''
    )
    $omittedLogonDocument = New-Object Xml.XmlDocument
    $omittedLogonDocument.LoadXml($systemXmlWithoutLogonType)
    $omittedLogonNamespace = New-Object Xml.XmlNamespaceManager($omittedLogonDocument.NameTable)
    $omittedLogonNamespace.AddNamespace('t', $script:OrderEscrowTaskNamespace)
    Assert-True 'Omitted-LogonType fixture has canonical SID and no LogonType element in any namespace' (
        @($omittedLogonDocument.SelectNodes('/t:Task/t:Principals/t:Principal/*[local-name()="LogonType"]', $omittedLogonNamespace)).Count -eq 0 -and
        [string]$omittedLogonDocument.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId', $omittedLogonNamespace).InnerText -ceq 'S-1-5-18'
    )
    $systemXmlWithoutLogonTypeError = ''
    try {
        Assert-OrderTaskXml -XmlText $systemXmlWithoutLogonType -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } catch {
        $systemXmlWithoutLogonTypeError = [string]$_.Exception.Message
    }
    Assert-True 'Exported SYSTEM task XML may omit optional LogonType' ([string]::IsNullOrEmpty($systemXmlWithoutLogonTypeError))

    $canonicalTriggerDocument = New-Object Xml.XmlDocument
    $canonicalTriggerDocument.LoadXml($systemXmlWithoutLogonType)
    $canonicalTriggerNamespace = New-Object Xml.XmlNamespaceManager($canonicalTriggerDocument.NameTable)
    $canonicalTriggerNamespace.AddNamespace('t', $script:OrderEscrowTaskNamespace)
    $canonicalBoot = $canonicalTriggerDocument.SelectSingleNode('/t:Task/t:Triggers/t:BootTrigger', $canonicalTriggerNamespace)
    $canonicalInterval = $canonicalTriggerDocument.SelectSingleNode('/t:Task/t:Triggers/t:TimeTrigger', $canonicalTriggerNamespace)
    Assert-True 'Task XML fixture models the native unqualified lowercase trigger id attributes' (
        [string]$canonicalBoot.GetAttribute('id') -ceq 'AtBoot' -and
        [string]$canonicalInterval.GetAttribute('id') -ceq 'Every15Minutes' -and
        @($canonicalBoot.SelectNodes('*[local-name()="Id"]')).Count -eq 0 -and
        @($canonicalInterval.SelectNodes('*[local-name()="Id"]')).Count -eq 0
    )

    $nativeSparseTriggersXml = $systemXmlWithoutLogonType.Replace(
        '<BootTrigger id="AtBoot"><Enabled>true</Enabled></BootTrigger>',
        '<BootTrigger id="AtBoot" />'
    ).Replace(
        '<TimeTrigger id="Every15Minutes"><Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>2026-09-06T00:00:00</StartBoundary><Enabled>true</Enabled></TimeTrigger>',
        '<TimeTrigger id="Every15Minutes"><StartBoundary>2026-09-06T00:00:00</StartBoundary><Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>'
    )
    $nativeSparseTriggerError = ''
    try {
        Assert-OrderTaskXml -XmlText $nativeSparseTriggersXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } catch {
        $nativeSparseTriggerError = [string]$_.Exception.Message
    }
    Assert-True 'Native sparse exported triggers validate with exact id attributes' ([string]::IsNullOrEmpty($nativeSparseTriggerError))

    $legacyTriggerElementXml = $systemXmlWithoutLogonType.Replace(
        '<BootTrigger id="AtBoot"><Enabled>true</Enabled></BootTrigger>',
        '<BootTrigger><Enabled>true</Enabled><Id>AtBoot</Id></BootTrigger>'
    )
    Assert-ThrowsCode 'A child Id element cannot impersonate the native trigger id attribute' {
        Assert-OrderTaskXml -XmlText $legacyTriggerElementXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_boot_trigger_id_invalid'

    $missingBootTriggerIdXml = $systemXmlWithoutLogonType.Replace(' id="AtBoot"', '')
    Assert-ThrowsCode 'A missing boot trigger id attribute fails closed' {
        Assert-OrderTaskXml -XmlText $missingBootTriggerIdXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_boot_trigger_id_invalid'

    $wrongIntervalTriggerIdXml = $systemXmlWithoutLogonType.Replace('id="Every15Minutes"', 'id="Every30Minutes"')
    Assert-ThrowsCode 'An incorrect interval trigger id attribute fails closed' {
        Assert-OrderTaskXml -XmlText $wrongIntervalTriggerIdXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_interval_trigger_id_invalid'

    $foreignTriggerIdXml = $systemXmlWithoutLogonType.Replace(
        '<BootTrigger id="AtBoot">',
        '<BootTrigger xmlns:x="urn:not-task" id="AtBoot" x:id="AtBoot">'
    )
    Assert-ThrowsCode 'A foreign-namespace trigger id lookalike fails closed' {
        Assert-OrderTaskXml -XmlText $foreignTriggerIdXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_boot_trigger_id_invalid'

    $ancestorForeignTriggerChildXml = $systemXmlWithoutLogonType.Replace(
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" xmlns:x="urn:not-task">'
    ).Replace(
        '<BootTrigger id="AtBoot"><Enabled>true</Enabled></BootTrigger>',
        '<BootTrigger id="AtBoot"><Enabled>true</Enabled><x:id>AtBoot</x:id></BootTrigger>'
    )
    Assert-ThrowsCode 'An ancestor-declared foreign lowercase id child cannot shadow the trigger attribute' {
        Assert-OrderTaskXml -XmlText $ancestorForeignTriggerChildXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_boot_trigger_id_invalid'

    $foreignTriggerNodeXml = $systemXmlWithoutLogonType.Replace(
        '</Triggers>',
        '<BootTrigger xmlns="urn:not-task" id="AtBoot" /></Triggers>'
    )
    Assert-ThrowsCode 'A foreign-namespace trigger node cannot hide outside the exact inventory' {
        Assert-OrderTaskXml -XmlText $foreignTriggerNodeXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_trigger_count_invalid'

    $systemAliasXmlWithoutLogonType = $systemXmlWithoutLogonType.Replace('<UserId>S-1-5-18</UserId>', '<UserId>SYSTEM</UserId>')
    Assert-ThrowsCode 'Omitted XML LogonType is bound to canonical S-1-5-18' {
        Assert-OrderTaskXml -XmlText $systemAliasXmlWithoutLogonType -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_logon_type_missing'

    $nonSystemXmlWithoutLogonType = $systemXmlWithoutLogonType.Replace('<UserId>S-1-5-18</UserId>', '<UserId>LOCAL SERVICE</UserId>')
    Assert-ThrowsCode 'Missing XML LogonType is never accepted for a non-SYSTEM principal' {
        Assert-OrderTaskXml -XmlText $nonSystemXmlWithoutLogonType -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_principal_invalid'

    $wrongLogonTypeXml = (New-TestTaskXml -ReleaseId $oldRelease).Replace(
        '<LogonType>ServiceAccount</LogonType>',
        '<LogonType>Password</LogonType>'
    )
    Assert-ThrowsCode 'An explicit non-ServiceAccount XML LogonType remains invalid' {
        Assert-OrderTaskXml -XmlText $wrongLogonTypeXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_logon_type_invalid'

    $duplicateLogonTypeXml = (New-TestTaskXml -ReleaseId $oldRelease).Replace(
        '<LogonType>ServiceAccount</LogonType>',
        '<LogonType>ServiceAccount</LogonType><LogonType>ServiceAccount</LogonType>'
    )
    Assert-ThrowsCode 'Duplicate XML LogonType elements remain invalid' {
        Assert-OrderTaskXml -XmlText $duplicateLogonTypeXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_logon_type_invalid'

    $foreignNamespaceLogonTypeXml = (New-TestTaskXml -ReleaseId $oldRelease).Replace(
        '<LogonType>ServiceAccount</LogonType>',
        '<LogonType xmlns="">Password</LogonType>'
    )
    Assert-ThrowsCode 'A foreign-namespace LogonType lookalike is invalid rather than omitted' {
        Assert-OrderTaskXml -XmlText $foreignNamespaceLogonTypeXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_logon_type_invalid'

    $duplicatePrincipalIdXml = (New-TestTaskXml -ReleaseId $oldRelease).Replace(
        '<UserId>S-1-5-18</UserId>',
        '<UserId>S-1-5-18</UserId><UserId>S-1-5-18</UserId>'
    )
    Assert-ThrowsCode 'Duplicate XML UserId elements remain invalid' {
        Assert-OrderTaskXml -XmlText $duplicatePrincipalIdXml -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'task_xml_principal_missing'

    $badTrustedJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($trustedHashesBase64)) | ConvertFrom-Json
    $badTrustedJson.PSObject.Properties[$script:OrderEscrowReleaseFiles[0]].Value = ('0' * 64)
    $badTrustedBase64 = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes(($badTrustedJson | ConvertTo-Json -Compress))
    )
    Assert-ThrowsCode 'Create refuses a caller-trusted hash map that does not match release bytes' {
        New-OrderTaskEscrow -Id 'bad-trusted-map' -ReleaseId $oldRelease -TrustedHashesBase64 $badTrustedBase64
    } 'trusted_release_file_digest_mismatch'
    Assert-True 'Trusted-map refusal creates no escrow bundle' (
        -not (Test-Path -LiteralPath (Join-Path (Join-Path $tempRoot 'SFDC24\OrderSupervisor\acceptance') 'bad-trusted-map'))
    )

    Reset-TestScheduler
    $script:MockTasks[0].Principal.LogonType = $null
    $script:MockExportXml = $systemXmlWithoutLogonType
    Assert-ThrowsCode 'Create rejects an unsafe live principal before writing an escrow bundle' {
        New-OrderTaskEscrow -Id 'bad-live-principal' -ReleaseId $oldRelease -TrustedHashesBase64 $trustedHashesBase64
    } 'task_principal_invalid'
    Assert-True 'Unsafe live principal refusal creates no escrow bundle' (
        -not (Test-Path -LiteralPath (Join-Path (Join-Path $tempRoot 'SFDC24\OrderSupervisor\acceptance') 'bad-live-principal'))
    )

    Reset-TestScheduler
    $script:MockExportXml = $systemXmlWithoutLogonType
    $created = New-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease -TrustedHashesBase64 $trustedHashesBase64
    $bundlePath = Join-Path (Join-Path $tempRoot 'SFDC24\OrderSupervisor\acceptance') $testEscrowId
    $manifestPath = Join-Path $bundlePath 'manifest.json'
    $xmlPath = Join-Path $bundlePath 'task.xml'
    Assert-True 'Create returns a bounded CREATED receipt' (
        [bool]$created.ok -and [string]$created.action -ceq 'Create' -and
        [string]$created.status -ceq 'CREATED' -and [int]$created.release_file_count -eq 6 -and
        @($created.PSObject.Properties).Count -eq 12
    )
    Assert-True 'Create emits only the fixed two-file bundle' (
        (Test-Path -LiteralPath $manifestPath -PathType Leaf) -and
        (Test-Path -LiteralPath $xmlPath -PathType Leaf) -and
        @(Get-ChildItem -LiteralPath $bundlePath -Force).Count -eq 2
    )
    [byte[]]$xmlBytes = [IO.File]::ReadAllBytes($xmlPath)
    [byte[]]$manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
    Assert-True 'Task XML is Unicode and manifest is UTF-8 without BOM' (
        $xmlBytes[0] -eq 0xff -and $xmlBytes[1] -eq 0xfe -and
        -not ($manifestBytes.Length -ge 3 -and $manifestBytes[0] -eq 0xef -and $manifestBytes[1] -eq 0xbb -and $manifestBytes[2] -eq 0xbf)
    )
    Assert-ThrowsCode 'Create never overwrites an existing escrow' {
        New-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease -TrustedHashesBase64 $trustedHashesBase64
    } 'escrow_already_exists'

    Reset-TestScheduler
    $validated = Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    $receiptJson = $validated | ConvertTo-Json -Compress
    Assert-True 'Validate proves the complete bundle without scheduler mutation' (
        [string]$validated.status -ceq 'VALID' -and
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0 -and $script:UnregisterCalls -eq 0
    )
    Assert-True 'Receipts do not expose raw task XML' (-not $receiptJson.Contains('<Task'))

    $trustedJsonText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($trustedHashesBase64))
    $firstTrustedKeyJson = $script:OrderEscrowReleaseFiles[0] | ConvertTo-Json -Compress
    $duplicateTrustedJson = $trustedJsonText.Substring(0, $trustedJsonText.Length - 1) + ',' +
        $firstTrustedKeyJson + ':"' + ('b' * 64) + '"}'
    $duplicateTrustedBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($duplicateTrustedJson))
    Assert-ThrowsCode 'Trusted hash JSON rejects a duplicate canonical file key' {
        Read-OrderTrustedFileHashes -Base64 $duplicateTrustedBase64
    } 'trusted_file_hashes_duplicate_key'

    $escrowManifestOriginal = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8)
    $escrowClose = $escrowManifestOriginal.LastIndexOf('}')
    $duplicateEscrowManifest = $escrowManifestOriginal.Substring(0, $escrowClose) +
        ',"schema":"blackboard.order-task-escrow.v1"' + $escrowManifestOriginal.Substring($escrowClose)
    [IO.File]::WriteAllText($manifestPath, $duplicateEscrowManifest, (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'Escrow manifest rejects a duplicate root property' {
        Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'escrow_manifest_duplicate_key'
    [IO.File]::WriteAllText($manifestPath, $escrowManifestOriginal, (New-Object Text.UTF8Encoding($false)))

    $offsetEscrowManifest = $escrowManifestOriginal | ConvertFrom-Json
    $offsetEscrowManifest.created_at_utc = '2026-09-06T00:00:00.0000000+00:00'
    [IO.File]::WriteAllText($manifestPath, ($offsetEscrowManifest | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'Escrow timestamp requires exact UTC Z notation' {
        Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'escrow_manifest_timestamp_invalid'
    [IO.File]::WriteAllText($manifestPath, $escrowManifestOriginal, (New-Object Text.UTF8Encoding($false)))

    $toolText = [IO.File]::ReadAllText($ToolPath, [Text.Encoding]::UTF8)
    Assert-True 'Validate is the script default and the tool has no unregister gap' (
        $toolText.Contains("[string]`$Action = 'Validate'") -and
        -not $toolText.Contains('Unregister-ScheduledTask')
    )
    Assert-True 'Tool is candidate-independent by contract' (
        -not $toolText.Contains('CandidateRelease') -and
        -not $toolText.Contains('candidate_release')
    )
    Assert-True 'Create and Restore require elevated Windows PowerShell 5.1' (
        $toolText.Contains("`$PSVersionTable.PSEdition -cne 'Desktop'") -and
        $toolText.Contains('administrator_required_for_system_task') -and
        ([Text.RegularExpressions.Regex]::Matches($toolText, 'Assert-OrderMutationPreflight').Count -eq 3)
    )

    $defaultOutput = @(& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $ToolPath `
        -ExpectedReleaseId $oldRelease -EscrowId $testEscrowId 2>&1)
    $defaultExit = $LASTEXITCODE
    $defaultReceipt = $null
    if ($defaultExit -eq 0) {
        try { $defaultReceipt = (($defaultOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) | ConvertFrom-Json }
        catch { $defaultReceipt = $null }
    }
    Assert-True 'Omitted Action executes non-mutating Validate in a clean child process' (
        $defaultExit -eq 0 -and $null -ne $defaultReceipt -and
        [string]$defaultReceipt.action -ceq 'Validate' -and [string]$defaultReceipt.status -ceq 'VALID'
    )

    $failurePsi = New-Object Diagnostics.ProcessStartInfo
    $failurePsi.FileName = 'powershell.exe'
    $failurePsi.Arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $ToolPath +
        '" -ExpectedReleaseId ' + $oldRelease + ' -EscrowId "..\outside"'
    $failurePsi.UseShellExecute = $false
    $failurePsi.CreateNoWindow = $true
    $failurePsi.RedirectStandardOutput = $true
    $failurePsi.RedirectStandardError = $true
    $failureProcess = New-Object Diagnostics.Process
    $failureProcess.StartInfo = $failurePsi
    $failureProcess.Start() | Out-Null
    $failureStdout = $failureProcess.StandardOutput.ReadToEnd()
    $failureStderr = $failureProcess.StandardError.ReadToEnd().Trim()
    $failureProcess.WaitForExit()
    $failureReceipt = $null
    try { $failureReceipt = $failureStderr | ConvertFrom-Json }
    catch { $failureReceipt = $null }
    Assert-True 'Failure emits exactly one standalone bounded JSON stderr receipt' (
        $failureProcess.ExitCode -eq 1 -and [string]::IsNullOrWhiteSpace($failureStdout) -and
        $null -ne $failureReceipt -and @($failureReceipt.PSObject.Properties).Count -eq 4 -and
        [bool]$failureReceipt.ok -eq $false -and [string]$failureReceipt.code -ceq 'escrow_id_invalid' -and
        @($failureStderr -split "`r?`n").Count -eq 1
    )

    $badActionPsi = New-Object Diagnostics.ProcessStartInfo
    $badActionPsi.FileName = 'powershell.exe'
    $badActionPsi.Arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $ToolPath +
        '" -Action Bogus -ExpectedReleaseId ' + $oldRelease + ' -EscrowId ' + $testEscrowId
    $badActionPsi.UseShellExecute = $false
    $badActionPsi.CreateNoWindow = $true
    $badActionPsi.RedirectStandardOutput = $true
    $badActionPsi.RedirectStandardError = $true
    $badActionProcess = New-Object Diagnostics.Process
    $badActionProcess.StartInfo = $badActionPsi
    $badActionProcess.Start() | Out-Null
    $badActionStdout = $badActionProcess.StandardOutput.ReadToEnd()
    $badActionStderr = $badActionProcess.StandardError.ReadToEnd().Trim()
    $badActionProcess.WaitForExit()
    $badActionReceipt = $null
    try { $badActionReceipt = $badActionStderr | ConvertFrom-Json }
    catch { $badActionReceipt = $null }
    Assert-True 'Invalid Action is caught and emitted as one bounded JSON receipt' (
        $badActionProcess.ExitCode -eq 1 -and [string]::IsNullOrWhiteSpace($badActionStdout) -and
        $null -ne $badActionReceipt -and @($badActionReceipt.PSObject.Properties).Count -eq 4 -and
        [string]$badActionReceipt.action -ceq 'Invalid' -and [string]$badActionReceipt.code -ceq 'action_invalid' -and
        @($badActionStderr -split "`r?`n").Count -eq 1
    )

    $releaseManifestPath = Join-Path $releasePath '.release.json'
    $releaseManifestOriginal = [IO.File]::ReadAllText($releaseManifestPath, [Text.Encoding]::UTF8)
    $releaseClose = $releaseManifestOriginal.LastIndexOf('}')
    $duplicateReleaseManifest = $releaseManifestOriginal.Substring(0, $releaseClose) +
        ',"release_id":"' + $oldRelease + '"' + $releaseManifestOriginal.Substring($releaseClose)
    [IO.File]::WriteAllText($releaseManifestPath, $duplicateReleaseManifest, (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'Release manifest rejects a duplicate root property' {
        Get-OrderReleaseEvidence -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'release_manifest_duplicate_key'
    [IO.File]::WriteAllText($releaseManifestPath, $releaseManifestOriginal, (New-Object Text.UTF8Encoding($false)))
    $offsetReleaseManifest = $releaseManifestOriginal | ConvertFrom-Json
    $offsetReleaseManifest.installed_at_utc = '2026-09-06T00:00:00.0000000+00:00'
    [IO.File]::WriteAllText($releaseManifestPath, ($offsetReleaseManifest | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Assert-ThrowsCode 'Release timestamp requires exact UTC Z notation' {
        Get-OrderReleaseEvidence -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    } 'release_manifest_timestamp_invalid'
    [IO.File]::WriteAllText($releaseManifestPath, $releaseManifestOriginal, (New-Object Text.UTF8Encoding($false)))
    $wrongIdentityManifest = $releaseManifestOriginal | ConvertFrom-Json
    $wrongIdentityManifest.release_id = $candidateRelease
    [IO.File]::WriteAllText($releaseManifestPath, ($wrongIdentityManifest | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    Assert-ThrowsCode 'Legacy release manifest identity mismatch is refused before mutation' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'release_manifest_value_invalid'
    Assert-True 'Release-manifest identity refusal performs no scheduler mutation' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0
    )
    [IO.File]::WriteAllText($releaseManifestPath, $releaseManifestOriginal, (New-Object Text.UTF8Encoding($false)))

    $currentManifest = $releaseManifestOriginal | ConvertFrom-Json
    $currentHashMap = [ordered]@{}
    foreach ($relativePath in $script:OrderEscrowReleaseFiles) {
        $currentHashMap[$relativePath] = Get-OrderFileSha256 -Path (Join-Path $releasePath $relativePath)
    }
    $currentHashMap[$script:OrderEscrowReleaseFiles[0]] = ('f' * 64)
    $currentManifest | Add-Member -NotePropertyName file_sha256 -NotePropertyValue ([pscustomobject]$currentHashMap)
    [IO.File]::WriteAllText($releaseManifestPath, ($currentManifest | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    Assert-ThrowsCode 'Strengthened release manifest per-file mismatch is refused before mutation' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'release_manifest_file_digest_mismatch'
    Assert-True 'Strengthened-manifest refusal performs no scheduler mutation' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0
    )
    $currentHashMap[$script:OrderEscrowReleaseFiles[0]] = Get-OrderFileSha256 -Path (
        Join-Path $releasePath $script:OrderEscrowReleaseFiles[0]
    )
    $currentManifest.file_sha256 = [pscustomobject]$currentHashMap
    [IO.File]::WriteAllText($releaseManifestPath, ($currentManifest | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    $strengthenedEvidence = Get-OrderReleaseEvidence -Context (Get-OrderEscrowContext) -ReleaseId $oldRelease
    Assert-True 'A strict strengthened release manifest cross-checks and passes exact bytes' (
        @($strengthenedEvidence.release_files).Count -eq 6 -and
        [string]$strengthenedEvidence.archive_sha256 -ceq ('a' * 64)
    )
    [IO.File]::WriteAllText($releaseManifestPath, $releaseManifestOriginal, (New-Object Text.UTF8Encoding($false)))
    $null = Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease

    $busPath = Join-Path $releasePath 'scripts\bus.ps1'
    $busOriginal = [IO.File]::ReadAllText($busPath, [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($busPath, 'tampered', (New-Object Text.UTF8Encoding($false)))
    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    Assert-ThrowsCode 'Release tamper is refused before stop or registration' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'escrow_release_file_digest_mismatch'
    Assert-True 'Tamper refusal performs no scheduler mutation' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0 -and $script:UnregisterCalls -eq 0
    )
    [IO.File]::WriteAllText($busPath, $busOriginal, (New-Object Text.UTF8Encoding($false)))
    $null = Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease

    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    $script:MockTasks[0].Description = 'operator-owned task'
    Assert-ThrowsCode 'Restore refuses an unmanaged current task' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'restore_current_task_unmanaged'
    Assert-True 'Unmanaged refusal occurs before stop or registration' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0
    )

    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    $script:MockTasks = @()
    Assert-ThrowsCode 'Restore refuses a missing current task' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'restore_current_task_missing'
    Assert-True 'Missing-task refusal occurs before stop or registration' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0
    )

    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    $otherFolderTask = New-TestTask -ReleaseId $candidateRelease -State 'Running' -TaskPath '\Other\'
    $script:MockTasks = @($script:MockTasks[0], $otherFolderTask)
    Assert-ThrowsCode 'Restore refuses ambiguous same-name tasks across folders' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'task_name_ambiguous_across_folders'
    Assert-True 'Ambiguity refusal occurs before stop or registration' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0
    )

    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Unknown'
    Assert-ThrowsCode 'Restore rejects an unexpected scheduler state before mutation' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'restore_current_task_state_invalid'
    Assert-True 'Unexpected-state refusal performs no scheduler mutation' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0
    )

    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Queued'
    $queuedRestore = Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    Assert-True 'Restore stops and waits a queued task before registration' (
        [string]$queuedRestore.status -ceq 'RESTORED' -and [bool]$queuedRestore.task_stopped -and
        $script:StopCalls -eq 1 -and $script:SleepCalls -ge 1 -and $script:RegisterCalls -eq 1
    )

    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    $restored = Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    Assert-True 'Restore stops then Force-registers the validated old task' (
        [string]$restored.status -ceq 'RESTORED' -and [bool]$restored.task_stopped -and
        $script:StopCalls -eq 1 -and $script:RegisterCalls -eq 1 -and
        $script:RegisterUsedForce -and $script:UnregisterCalls -eq 0
    )
    Assert-True 'Restore accepts a different managed candidate without knowing its release id' (
        [string]$restored.expected_release_id -ceq $oldRelease -and
        $script:MockRegisteredXml -ceq $systemXmlWithoutLogonType
    )

    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    $script:MockReadbackOverride = $systemXmlWithoutLogonType + "`r`n"
    Assert-ThrowsCode 'Exact post-registration XML readback mismatch is surfaced' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'restore_task_xml_readback_mismatch'
    Assert-True 'Readback failure occurs after one stop and one gapless registration' (
        $script:StopCalls -eq 1 -and $script:RegisterCalls -eq 1 -and $script:UnregisterCalls -eq 0
    )

    Assert-ThrowsCode 'Escrow id traversal is rejected by path confinement' {
        Test-OrderTaskEscrow -Id '..\outside' -ReleaseId $oldRelease
    } 'escrow_id_invalid'
    Assert-ThrowsCode 'Non-hex release identity is rejected before any filesystem access' {
        Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId ('z' * 40)
    } 'expected_release_id_invalid'

    $junctionPath = Join-Path ([IO.Path]::GetTempPath()) ('order-escrow-junction-' + [Guid]::NewGuid().ToString('N'))
    try {
        $tempParent = [IO.Path]::GetDirectoryName($tempRoot)
        New-Item -ItemType Junction -Path $junctionPath -Target $tempParent -ErrorAction Stop | Out-Null
        $ordinaryProgramDataBelowJunction = Join-Path $junctionPath ([IO.Path]::GetFileName($tempRoot))
        [Environment]::SetEnvironmentVariable('ProgramData', $ordinaryProgramDataBelowJunction, 'Process')
        Assert-ThrowsCode 'Validate rejects a reparse point above an ordinary ProgramData directory' {
            Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
        } 'acceptance_path_component_unsafe'
    } finally {
        [Environment]::SetEnvironmentVariable('ProgramData', $tempRoot, 'Process')
        if (Test-Path -LiteralPath $junctionPath) { [IO.Directory]::Delete($junctionPath) }
    }

    $manifestText = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8)
    $manifest = $manifestText | ConvertFrom-Json
    $manifest | Add-Member -NotePropertyName unexpected -NotePropertyValue 'field'
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Reset-TestScheduler -CurrentRelease $candidateRelease -CurrentState 'Running'
    Assert-ThrowsCode 'Manifest shape tamper is refused before scheduler mutation' {
        Restore-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
    } 'escrow_manifest_shape_invalid'
    Assert-True 'Manifest refusal performs no scheduler mutation' (
        $script:StopCalls -eq 0 -and $script:RegisterCalls -eq 0
    )
    [IO.File]::WriteAllText($manifestPath, $manifestText, (New-Object Text.UTF8Encoding($false)))
    $null = Test-OrderTaskEscrow -Id $testEscrowId -ReleaseId $oldRelease
} finally {
    if ($null -eq $originalProgramData) {
        Remove-Item -LiteralPath 'Env:\ProgramData' -Force -ErrorAction SilentlyContinue
    } else {
        [Environment]::SetEnvironmentVariable('ProgramData', $originalProgramData, 'Process')
    }
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $tempRoot -PathType Container)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
