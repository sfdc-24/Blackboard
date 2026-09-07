#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0
$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:TransportPath = Join-Path $script:RepoRoot 'infra\azure\stage_order_escrow_tool.ps1'
$script:SystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$script:TestRoot = Join-Path $script:SystemTemp ('order-escrow-transport-test-' + [Guid]::NewGuid().ToString('N'))
$script:OriginalProgramData = [Environment]::GetEnvironmentVariable('ProgramData', 'Process')
$script:Junctions = New-Object System.Collections.Generic.List[string]

function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')

    if ($Condition) {
        $script:Passed++
        [Console]::Out.WriteLine('PASS ' + $Name)
    } else {
        $script:Failed++
        [Console]::Out.WriteLine('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
    }
}

function Get-TestSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function New-TestPayload {
    param([Parameter(Mandatory = $true)][string]$Text)

    [byte[]]$bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Text)
    return [pscustomobject][ordered]@{
        bytes = $bytes
        base64 = [Convert]::ToBase64String($bytes)
        sha256 = Get-TestSha256 -Bytes $bytes
    }
}

function New-TestProgramData {
    param([Parameter(Mandatory = $true)][string]$Name)

    $path = Join-Path (Join-Path $script:TestRoot $Name) 'ProgramData'
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    return [IO.Path]::GetFullPath($path)
}

function Get-TestToolsRoot {
    param([Parameter(Mandatory = $true)][string]$ProgramData)

    return Join-Path $ProgramData 'SFDC24\OrderSupervisor\tools'
}

function Quote-TestProcessArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return '"' + $Value.Replace('\', '\').Replace('"', '\"') + '"'
}

function Invoke-TestTransportProcess {
    param(
        [Parameter(Mandatory = $true)][string]$ProgramData,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$EncodedPayload,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Digest
    )

    $windowsPowerShell = Join-Path $PSHOME 'powershell.exe'
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $windowsPowerShell
    $startInfo.Arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-TestProcessArgument -Value $script:TransportPath),
        '-Payload',
        (Quote-TestProcessArgument -Value $EncodedPayload),
        '-Sha256',
        (Quote-TestProcessArgument -Value $Digest)
    ) -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables['ProgramData'] = $ProgramData

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'test_child_start_failed' }
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [pscustomobject][ordered]@{
            exit_code = [int]$process.ExitCode
            stdout = $stdout
            stderr = $stderr
        }
    } finally {
        $process.Dispose()
    }
}

function Get-TestNonEmptyLines {
    param([AllowEmptyString()][string]$Text)

    return @($Text -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Read-TestSuccessReceipt {
    param([Parameter(Mandatory = $true)]$Result)

    $stdoutLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stdout))
    $stderrLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stderr))
    $receipt = $null
    if ($stdoutLines.Count -eq 1) {
        try { $receipt = $stdoutLines[0] | ConvertFrom-Json } catch { $receipt = $null }
    }
    Assert-True 'success emits exactly one JSON stdout receipt and no stderr' (
        $Result.exit_code -eq 0 -and
        $stdoutLines.Count -eq 1 -and
        $stderrLines.Count -eq 0 -and
        $null -ne $receipt -and
        $receipt.schema -ceq 'blackboard.order-escrow-tool-stage-receipt.v1' -and
        $receipt.ok -eq $true
    ) (($Result.stdout + $Result.stderr).Trim())
    return $receipt
}

function Read-TestFailureReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][string]$ExpectedCode
    )

    $stdoutLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stdout))
    $stderrLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stderr))
    $receipt = $null
    if ($stderrLines.Count -eq 1) {
        try { $receipt = $stderrLines[0] | ConvertFrom-Json } catch { $receipt = $null }
    }
    Assert-True $Name (
        $Result.exit_code -ne 0 -and
        $stdoutLines.Count -eq 0 -and
        $stderrLines.Count -eq 1 -and
        $stderrLines[0].Length -lt 1024 -and
        $null -ne $receipt -and
        $receipt.schema -ceq 'blackboard.order-escrow-tool-stage-receipt.v1' -and
        $receipt.ok -eq $false -and
        $receipt.action -ceq 'Stage' -and
        $receipt.code -ceq $ExpectedCode
    ) (($Result.stdout + $Result.stderr).Trim())
    return $receipt
}

function Get-TestTransportInventory {
    param([Parameter(Mandatory = $true)][string]$ToolsRoot)

    if (-not (Test-Path -LiteralPath $ToolsRoot -PathType Container)) { return @() }
    return @(
        Get-ChildItem -LiteralPath $ToolsRoot -Force |
            Sort-Object Name |
            ForEach-Object { [string]$_.Name }
    )
}

try {
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
    $payloadA = New-TestPayload -Text "# escrow transport A`r`nWrite-Output 'A'`r`n"
    $payloadB = New-TestPayload -Text "# escrow transport B`r`nWrite-Output 'B'`r`n"

    $freshProgramData = New-TestProgramData -Name 'fresh'
    $fresh = Invoke-TestTransportProcess `
        -ProgramData $freshProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $freshReceipt = Read-TestSuccessReceipt -Result $fresh
    $freshToolsRoot = Get-TestToolsRoot -ProgramData $freshProgramData
    $freshName = 'order_task_escrow.' + $payloadA.sha256 + '.ps1'
    $freshPath = Join-Path $freshToolsRoot $freshName
    Assert-True 'fresh stage creates only the digest-qualified exact payload' (
        $freshReceipt.status -ceq 'STAGED' -and
        $freshReceipt.sha256 -ceq $payloadA.sha256 -and
        $freshReceipt.file_name -ceq $freshName -and
        [IO.Path]::GetFullPath([string]$freshReceipt.tool_path) -ceq [IO.Path]::GetFullPath($freshPath) -and
        $freshReceipt.inventory_file_count -eq 1 -and
        @(Get-TestTransportInventory -ToolsRoot $freshToolsRoot).Count -eq 1 -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($freshPath)) -ceq $payloadA.base64
    )
    $freshWriteTime = (Get-Item -LiteralPath $freshPath -Force).LastWriteTimeUtc.Ticks

    $replay = Invoke-TestTransportProcess `
        -ProgramData $freshProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $replayReceipt = Read-TestSuccessReceipt -Result $replay
    Assert-True 'replay accepts only a full digest match without rewriting the file' (
        $replayReceipt.status -ceq 'ALREADY_STAGED' -and
        $replayReceipt.inventory_file_count -eq 1 -and
        (Get-Item -LiteralPath $freshPath -Force).LastWriteTimeUtc.Ticks -eq $freshWriteTime -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($freshPath)) -ceq $payloadA.base64
    )

    $second = Invoke-TestTransportProcess `
        -ProgramData $freshProgramData `
        -EncodedPayload $payloadB.base64 `
        -Digest $payloadB.sha256
    $secondReceipt = Read-TestSuccessReceipt -Result $second
    $secondName = 'order_task_escrow.' + $payloadB.sha256 + '.ps1'
    Assert-True 'a second valid immutable version coexists while inventory remains exact' (
        $secondReceipt.status -ceq 'STAGED' -and
        $secondReceipt.inventory_file_count -eq 2 -and
        @(Get-TestTransportInventory -ToolsRoot $freshToolsRoot).Count -eq 2 -and
        (Test-Path -LiteralPath (Join-Path $freshToolsRoot $secondName) -PathType Leaf)
    )

    $tamperProgramData = New-TestProgramData -Name 'tamper'
    $tamperInstall = Invoke-TestTransportProcess `
        -ProgramData $tamperProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestSuccessReceipt -Result $tamperInstall
    $tamperRoot = Get-TestToolsRoot -ProgramData $tamperProgramData
    $tamperPath = Join-Path $tamperRoot $freshName
    [IO.File]::WriteAllText($tamperPath, 'tampered', (New-Object Text.UTF8Encoding($false)))
    $tamperedBytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($tamperPath))
    $tamperReplay = Invoke-TestTransportProcess `
        -ProgramData $tamperProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'tampered existing digest-qualified file is refused before mutation' `
        -Result $tamperReplay `
        -ExpectedCode 'tools_inventory_digest_mismatch'
    Assert-True 'tamper refusal does not replace or delete the collision' (
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($tamperPath)) -ceq $tamperedBytes -and
        @(Get-TestTransportInventory -ToolsRoot $tamperRoot).Count -eq 1
    )

    $partialProgramData = New-TestProgramData -Name 'partial'
    $partialRoot = Get-TestToolsRoot -ProgramData $partialProgramData
    New-Item -ItemType Directory -Path $partialRoot -Force | Out-Null
    $partialName = '.order_task_escrow.' + $payloadA.sha256 + '.tmp.' + [Guid]::NewGuid().ToString('N') + '.ps1'
    $partialPath = Join-Path $partialRoot $partialName
    [IO.File]::WriteAllText($partialPath, 'partial', (New-Object Text.UTF8Encoding($false)))
    $partialResult = Invoke-TestTransportProcess `
        -ProgramData $partialProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'pre-existing partial temporary inventory is refused' `
        -Result $partialResult `
        -ExpectedCode 'tools_inventory_name_invalid'
    Assert-True 'partial refusal leaves evidence and creates no final file' (
        (Test-Path -LiteralPath $partialPath -PathType Leaf) -and
        -not (Test-Path -LiteralPath (Join-Path $partialRoot $freshName))
    )

    $extraProgramData = New-TestProgramData -Name 'extra'
    $extraRoot = Get-TestToolsRoot -ProgramData $extraProgramData
    New-Item -ItemType Directory -Path $extraRoot -Force | Out-Null
    $extraPath = Join-Path $extraRoot 'unexpected.txt'
    [IO.File]::WriteAllText($extraPath, 'keep', (New-Object Text.UTF8Encoding($false)))
    $extraResult = Invoke-TestTransportProcess `
        -ProgramData $extraProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'unexpected extra tools-root inventory is refused' `
        -Result $extraResult `
        -ExpectedCode 'tools_inventory_name_invalid'
    Assert-True 'extra inventory refusal preserves the unexpected file and creates no final' (
        [IO.File]::ReadAllText($extraPath) -ceq 'keep' -and
        -not (Test-Path -LiteralPath (Join-Path $extraRoot $freshName))
    )

    $entryCollisionProgramData = New-TestProgramData -Name 'entry-collision'
    $entryCollisionRoot = Get-TestToolsRoot -ProgramData $entryCollisionProgramData
    New-Item -ItemType Directory -Path $entryCollisionRoot -Force | Out-Null
    $entryCollisionPath = Join-Path $entryCollisionRoot $freshName
    New-Item -ItemType Directory -Path $entryCollisionPath | Out-Null
    $entryCollisionResult = Invoke-TestTransportProcess `
        -ProgramData $entryCollisionProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'directory collision at the final filename is refused without overwrite' `
        -Result $entryCollisionResult `
        -ExpectedCode 'tools_inventory_entry_not_file'
    Assert-True 'directory collision remains untouched' (
        (Test-Path -LiteralPath $entryCollisionPath -PathType Container) -and
        @(Get-ChildItem -LiteralPath $entryCollisionRoot -Force).Count -eq 1
    )

    $rootReparseProgramData = New-TestProgramData -Name 'root-reparse'
    $rootReparseParent = Join-Path $rootReparseProgramData 'SFDC24\OrderSupervisor'
    New-Item -ItemType Directory -Path $rootReparseParent -Force | Out-Null
    $rootReparseTarget = Join-Path $script:TestRoot 'root-reparse-target'
    New-Item -ItemType Directory -Path $rootReparseTarget | Out-Null
    $rootReparseMarker = Join-Path $rootReparseTarget 'must-survive.txt'
    [IO.File]::WriteAllText($rootReparseMarker, 'target', (New-Object Text.UTF8Encoding($false)))
    $rootReparseLink = Join-Path $rootReparseParent 'tools'
    New-Item -ItemType Junction -Path $rootReparseLink -Target $rootReparseTarget | Out-Null
    $script:Junctions.Add($rootReparseLink)
    $rootReparseResult = Invoke-TestTransportProcess `
        -ProgramData $rootReparseProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'reparse-point tools root is refused before traversal' `
        -Result $rootReparseResult `
        -ExpectedCode 'tools_path_reparse_point'
    Assert-True 'tools-root reparse refusal never touches its target' (
        [IO.File]::ReadAllText($rootReparseMarker) -ceq 'target' -and
        @(Get-ChildItem -LiteralPath $rootReparseTarget -Force).Count -eq 1
    )

    $ancestorTarget = Join-Path $script:TestRoot 'ancestor-reparse-target'
    $ancestorProgramDataTarget = Join-Path $ancestorTarget 'ProgramData'
    New-Item -ItemType Directory -Path $ancestorProgramDataTarget -Force | Out-Null
    $ancestorMarker = Join-Path $ancestorTarget 'must-survive.txt'
    [IO.File]::WriteAllText($ancestorMarker, 'ancestor', (New-Object Text.UTF8Encoding($false)))
    $ancestorLink = Join-Path $script:TestRoot 'ancestor-reparse-link'
    New-Item -ItemType Junction -Path $ancestorLink -Target $ancestorTarget | Out-Null
    $script:Junctions.Add($ancestorLink)
    $ancestorProgramData = Join-Path $ancestorLink 'ProgramData'
    $ancestorResult = Invoke-TestTransportProcess `
        -ProgramData $ancestorProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'reparse point above ProgramData is refused before traversal' `
        -Result $ancestorResult `
        -ExpectedCode 'tools_path_reparse_point'
    Assert-True 'ancestor reparse refusal does not create descendants in the target' (
        [IO.File]::ReadAllText($ancestorMarker) -ceq 'ancestor' -and
        -not (Test-Path -LiteralPath (Join-Path $ancestorProgramDataTarget 'SFDC24'))
    )

    $volumeRoot = [IO.Path]::GetPathRoot($script:TestRoot)
    $volumeResult = Invoke-TestTransportProcess `
        -ProgramData $volumeRoot `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'volume root cannot be repurposed as ProgramData' `
        -Result $volumeResult `
        -ExpectedCode 'program_data_volume_root_forbidden'

    $traversalBase = Join-Path $script:TestRoot 'traversal'
    $canonicalProgramData = Join-Path $traversalBase 'ProgramData'
    New-Item -ItemType Directory -Path $canonicalProgramData -Force | Out-Null
    $nonCanonicalProgramData = Join-Path $canonicalProgramData '..\outside'
    $traversalResult = Invoke-TestTransportProcess `
        -ProgramData $nonCanonicalProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'non-canonical ProgramData traversal is refused' `
        -Result $traversalResult `
        -ExpectedCode 'program_data_not_canonical'
    Assert-True 'ProgramData traversal refusal creates no outside tools root' (
        -not (Test-Path -LiteralPath (Join-Path $traversalBase 'outside'))
    )

    $badHashProgramData = New-TestProgramData -Name 'bad-hash'
    $badHashResult = Invoke-TestTransportProcess `
        -ProgramData $badHashProgramData `
        -EncodedPayload $payloadA.base64 `
        -Digest '..\..\order_task_escrow.ps1'
    $badHashReceipt = Read-TestFailureReceipt `
        -Name 'path-like digest input is rejected in one bounded JSON receipt' `
        -Result $badHashResult `
        -ExpectedCode 'sha256_invalid'
    Assert-True 'invalid digest is rejected before creating the stable tools path or echoing payload' (
        -not (Test-Path -LiteralPath (Join-Path $badHashProgramData 'SFDC24')) -and
        $badHashResult.stderr -notmatch [Regex]::Escape($payloadA.base64) -and
        @($badHashReceipt.PSObject.Properties).Count -eq 4
    )

    $nonCanonicalProgramDataForPayload = New-TestProgramData -Name 'bad-base64'
    $badBase64Result = Invoke-TestTransportProcess `
        -ProgramData $nonCanonicalProgramDataForPayload `
        -EncodedPayload ($payloadA.base64 + "`r`n") `
        -Digest $payloadA.sha256
    $null = Read-TestFailureReceipt `
        -Name 'non-canonical Base64 with whitespace is refused' `
        -Result $badBase64Result `
        -ExpectedCode 'payload_base64_noncanonical'
    Assert-True 'invalid Base64 is rejected before filesystem mutation' (
        -not (Test-Path -LiteralPath (Join-Path $nonCanonicalProgramDataForPayload 'SFDC24'))
    )

    . $script:TransportPath
    $originalMove = ${function:Move-OrderEscrowTransportFileNoOverwrite}
    $originalCleanup = ${function:Remove-OrderEscrowTransportTemporaryFile}

    $raceProgramData = New-TestProgramData -Name 'race'
    [Environment]::SetEnvironmentVariable('ProgramData', $raceProgramData, 'Process')
    Set-Item -LiteralPath Function:Move-OrderEscrowTransportFileNoOverwrite -Value {
        param([string]$Source, [string]$Destination)
        [IO.File]::Copy($Source, $Destination, $false)
        throw 'simulated_concurrent_winner'
    }
    $raceReceipt = Invoke-OrderEscrowTransportStage `
        -EncodedPayload $payloadA.base64 `
        -ExpectedSha256 $payloadA.sha256
    $raceRoot = Get-TestToolsRoot -ProgramData $raceProgramData
    Assert-True 'atomic move race validates the immutable winner and returns replay status' (
        $raceReceipt.status -ceq 'ALREADY_STAGED' -and
        $raceReceipt.inventory_file_count -eq 1 -and
        @(Get-TestTransportInventory -ToolsRoot $raceRoot).Count -eq 1 -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $raceRoot $freshName))) -ceq $payloadA.base64
    )
    Assert-True 'race cleanup removes the losing same-directory temporary file' (
        @(Get-ChildItem -LiteralPath $raceRoot -Force | Where-Object { $_.Name.StartsWith('.') }).Count -eq 0
    )

    Set-Item -LiteralPath Function:Move-OrderEscrowTransportFileNoOverwrite -Value $originalMove
    $moveFailureProgramData = New-TestProgramData -Name 'move-failure'
    [Environment]::SetEnvironmentVariable('ProgramData', $moveFailureProgramData, 'Process')
    Set-Item -LiteralPath Function:Move-OrderEscrowTransportFileNoOverwrite -Value {
        param([string]$Source, [string]$Destination)
        throw 'simulated_move_failure'
    }
    $moveFailureCode = ''
    try {
        Invoke-OrderEscrowTransportStage `
            -EncodedPayload $payloadA.base64 `
            -ExpectedSha256 $payloadA.sha256 | Out-Null
    } catch { $moveFailureCode = [string]$_.Exception.Message }
    $moveFailureRoot = Get-TestToolsRoot -ProgramData $moveFailureProgramData
    Assert-True 'failed atomic move surfaces a stable error and cleans the temporary file' (
        $moveFailureCode -ceq 'tool_atomic_move_failed' -and
        @(Get-TestTransportInventory -ToolsRoot $moveFailureRoot).Count -eq 0
    ) $moveFailureCode

    $cleanupFailureProgramData = New-TestProgramData -Name 'cleanup-failure'
    [Environment]::SetEnvironmentVariable('ProgramData', $cleanupFailureProgramData, 'Process')
    Set-Item -LiteralPath Function:Remove-OrderEscrowTransportTemporaryFile -Value {
        param($Context, [string]$Path, [string]$Digest)
        throw 'temporary_cleanup_failed'
    }
    $cleanupFailureRecord = $null
    try {
        Invoke-OrderEscrowTransportStage `
            -EncodedPayload $payloadA.base64 `
            -ExpectedSha256 $payloadA.sha256 | Out-Null
    } catch { $cleanupFailureRecord = $_ }
    $cleanupFailureRoot = Get-TestToolsRoot -ProgramData $cleanupFailureProgramData
    $cleanupTemps = @(Get-ChildItem -LiteralPath $cleanupFailureRoot -Force | Where-Object { $_.Name.StartsWith('.') })
    Assert-True 'cleanup failure never masks the primary move failure' (
        $null -ne $cleanupFailureRecord -and
        [string]$cleanupFailureRecord.Exception.Message -ceq 'tool_atomic_move_failed' -and
        [string]$cleanupFailureRecord.Exception.Data['cleanup_code'] -ceq 'temporary_cleanup_failed'
    ) $(if ($null -ne $cleanupFailureRecord) { [string]$cleanupFailureRecord.Exception.Message } else { 'no error' })
    Assert-True 'strict cleanup failure cannot return success and leaves bounded inspectable evidence' (
        $cleanupTemps.Count -eq 1 -and
        -not (Test-Path -LiteralPath (Join-Path $cleanupFailureRoot $freshName))
    )
    foreach ($cleanupTemp in $cleanupTemps) { [IO.File]::Delete($cleanupTemp.FullName) }

    Set-Item -LiteralPath Function:Move-OrderEscrowTransportFileNoOverwrite -Value $originalMove
    Set-Item -LiteralPath Function:Remove-OrderEscrowTransportTemporaryFile -Value $originalCleanup
} finally {
    [Environment]::SetEnvironmentVariable('ProgramData', $script:OriginalProgramData, 'Process')
    foreach ($junction in @($script:Junctions)) {
        if ([IO.Directory]::Exists($junction)) {
            try { [IO.Directory]::Delete($junction) } catch { }
        }
    }
    if (-not $KeepArtifacts -and [IO.Directory]::Exists($script:TestRoot)) {
        $resolvedTestRoot = [IO.Path]::GetFullPath($script:TestRoot).TrimEnd('\', '/')
        $tempPrefix = $script:SystemTemp.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if ($resolvedTestRoot.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolvedTestRoot) -match '^order-escrow-transport-test-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction Stop
        }
    }
}

Write-Output ("RESULT passed={0} failed={1}" -f $script:Passed, $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
