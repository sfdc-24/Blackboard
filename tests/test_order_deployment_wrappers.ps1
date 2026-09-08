#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0
$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:EscrowBuilder = Join-Path $script:RepoRoot 'infra\azure\build_order_escrow_transport_wrapper.ps1'
$script:ReleaseBuilder = Join-Path $script:RepoRoot 'infra\azure\build_order_release_wrapper.ps1'
$script:StageDriver = Join-Path $script:RepoRoot 'infra\azure\stage_order_escrow_tool.ps1'
$script:EscrowTool = Join-Path $script:RepoRoot 'infra\azure\order_task_escrow.ps1'
$script:ReleaseInstaller = Join-Path $script:RepoRoot 'infra\azure\install_release_from_archive.ps1'
$script:SystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$script:TestRoot = Join-Path $script:SystemTemp ('order-deployment-wrapper-test-' + [Guid]::NewGuid().ToString('N'))
$script:ReleaseFiles = @(
    'scripts\OrderSupervisor.psm1',
    'scripts\bus.ps1',
    'scripts\install_order_supervisor.ps1',
    'scripts\invoke_order_claude.ps1',
    'scripts\order_supervisor.ps1',
    'scripts\order_supervisor_result.schema.json'
)

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

function Get-TestFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Quote-TestArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-TestPowerShellFile {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [hashtable]$Environment = @{}
    )

    $tokens = New-Object System.Collections.Generic.List[string]
    foreach ($token in @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File')) {
        $tokens.Add($token)
    }
    $tokens.Add((Quote-TestArgument -Value $FilePath))
    foreach ($argument in $ArgumentList) { $tokens.Add((Quote-TestArgument -Value ([string]$argument))) }

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = (Join-Path $PSHOME 'powershell.exe')
    $startInfo.Arguments = $tokens -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($name in $Environment.Keys) {
        $startInfo.EnvironmentVariables[[string]$name] = [string]$Environment[$name]
    }

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'test_process_start_failed' }
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [pscustomobject][ordered]@{
            exit_code = [int]$process.ExitCode
            stdout = $stdout
            stderr = $stderr
        }
    } finally { $process.Dispose() }
}

function Get-TestNonEmptyLines {
    param([AllowEmptyString()][string]$Text)

    return @($Text -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Read-TestJsonReceipt {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][bool]$Success
    )

    $stdoutLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stdout))
    $stderrLines = @(Get-TestNonEmptyLines -Text ([string]$Result.stderr))
    $lines = @(if ($Success) { $stdoutLines } else { $stderrLines })
    $receipt = $null
    if ($lines.Count -eq 1) {
        try { $receipt = $lines[0] | ConvertFrom-Json } catch { $receipt = $null }
    }
    $hasOk = $null -ne $receipt -and $null -ne $receipt.PSObject.Properties['ok']
    Assert-True $(if ($Success) { 'command emits one success JSON receipt' } else { 'command emits one failure JSON receipt' }) (
        $null -ne $receipt -and
        $lines.Count -eq 1 -and
        $lines[0].Length -lt 2048 -and
        $(if ($Success) {
            $Result.exit_code -eq 0 -and
            $stderrLines.Count -eq 0 -and
            (-not $hasOk -or $receipt.ok -eq $true)
        } else {
            $Result.exit_code -ne 0 -and
            $stdoutLines.Count -eq 0 -and
            $hasOk -and
            $receipt.ok -eq $false
        })
    ) (($Result.stdout + $Result.stderr).Trim())
    return $receipt
}

function Get-TestEmbeddedValue {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$VariableName
    )

    $pattern = '(?m)^\$' + [Regex]::Escape($VariableName) + " = '([^']*)'\r?$"
    $matches = [Regex]::Matches($Text, $pattern)
    if ($matches.Count -ne 1) { return $null }
    return [string]$matches[0].Groups[1].Value
}

function New-TestReleaseArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [hashtable]$Extra = @{}
    )

    $source = Join-Path $script:TestRoot ($Name + '-source')
    $archive = Join-Path $script:TestRoot ($Name + '.zip')
    foreach ($relativePath in $script:ReleaseFiles) {
        $path = Join-Path $source $relativePath
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($path)) -Force | Out-Null
        [IO.File]::WriteAllText(
            $path,
            ($Name + '|' + $relativePath),
            (New-Object Text.UTF8Encoding($false))
        )
    }
    foreach ($relativePath in $Extra.Keys) {
        $path = Join-Path $source $relativePath
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($path)) -Force | Out-Null
        [IO.File]::WriteAllText($path, [string]$Extra[$relativePath], (New-Object Text.UTF8Encoding($false)))
    }
    Compress-Archive -Path (Join-Path $source 'scripts') -DestinationPath $archive -CompressionLevel Optimal
    return $archive
}

try {
    New-Item -ItemType Directory -Path $script:TestRoot | Out-Null
    $stageSha = Get-TestFileSha256 -Path $script:StageDriver
    $escrowSha = Get-TestFileSha256 -Path $script:EscrowTool
    $escrowWrapperOne = Join-Path $script:TestRoot 'escrow-wrapper-one.ps1'
    $escrowWrapperTwo = Join-Path $script:TestRoot 'escrow-wrapper-two.ps1'
    $escrowBuilderArguments = @(
        '-StageDriverPath', $script:StageDriver,
        '-EscrowToolPath', $script:EscrowTool,
        '-ExpectedStageDriverSha256', $stageSha,
        '-ExpectedEscrowToolSha256', $escrowSha
    )
    $escrowBuildOne = Invoke-TestPowerShellFile `
        -FilePath $script:EscrowBuilder `
        -ArgumentList @($escrowBuilderArguments + @('-OutputPath', $escrowWrapperOne))
    $escrowBuildOneReceipt = Read-TestJsonReceipt -Result $escrowBuildOne -Success $true
    $escrowBuildTwo = Invoke-TestPowerShellFile `
        -FilePath $script:EscrowBuilder `
        -ArgumentList @($escrowBuilderArguments + @('-OutputPath', $escrowWrapperTwo))
    $escrowBuildTwoReceipt = Read-TestJsonReceipt -Result $escrowBuildTwo -Success $true
    [byte[]]$escrowWrapperBytes = [IO.File]::ReadAllBytes($escrowWrapperOne)
    $escrowWrapperText = (New-Object Text.UTF8Encoding($false, $true)).GetString($escrowWrapperBytes)
    Assert-True 'escrow wrapper is deterministic, BOM-free, and bound to both exact digests' (
        [Convert]::ToBase64String($escrowWrapperBytes) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($escrowWrapperTwo)) -and
        -not ($escrowWrapperBytes.Length -ge 3 -and $escrowWrapperBytes[0] -eq 0xef -and $escrowWrapperBytes[1] -eq 0xbb -and $escrowWrapperBytes[2] -eq 0xbf) -and
        $escrowBuildOneReceipt.wrapper_sha256 -ceq $escrowBuildTwoReceipt.wrapper_sha256 -and
        (Get-TestEmbeddedValue -Text $escrowWrapperText -VariableName 'expectedStageDriverSha256') -ceq $stageSha -and
        (Get-TestEmbeddedValue -Text $escrowWrapperText -VariableName 'expectedEscrowToolSha256') -ceq $escrowSha
    )
    $embeddedDriver = [Convert]::FromBase64String(
        (Get-TestEmbeddedValue -Text $escrowWrapperText -VariableName 'stageDriverBase64')
    )
    $embeddedEscrow = [Convert]::FromBase64String(
        (Get-TestEmbeddedValue -Text $escrowWrapperText -VariableName 'escrowToolBase64')
    )
    Assert-True 'escrow wrapper round-trip preserves exact stage-driver and escrow-tool bytes' (
        [Convert]::ToBase64String($embeddedDriver) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($script:StageDriver)) -and
        [Convert]::ToBase64String($embeddedEscrow) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($script:EscrowTool)) -and
        (Get-TestFileSha256 -Path $escrowWrapperOne) -ceq $escrowBuildOneReceipt.wrapper_sha256
    )
    Assert-True 'escrow builder receipt never emits either embedded Base64 payload' (
        $escrowBuildOne.stdout -notmatch [Regex]::Escape([Convert]::ToBase64String($embeddedDriver)) -and
        $escrowBuildOne.stdout -notmatch [Regex]::Escape([Convert]::ToBase64String($embeddedEscrow))
    )

    $escrowProgramData = Join-Path $script:TestRoot 'escrow-execution-ProgramData'
    New-Item -ItemType Directory -Path $escrowProgramData | Out-Null
    $escrowExecution = Invoke-TestPowerShellFile `
        -FilePath $escrowWrapperOne `
        -Environment @{ ProgramData = $escrowProgramData }
    $escrowExecutionReceipt = Read-TestJsonReceipt -Result $escrowExecution -Success $true
    $stagedEscrowPath = Join-Path $escrowProgramData (
        'SFDC24\OrderSupervisor\tools\order_task_escrow.' + $escrowSha + '.ps1'
    )
    Assert-True 'generated escrow wrapper executes in-process and stages exact bytes in an isolated root' (
        $escrowExecutionReceipt.status -ceq 'STAGED' -and
        $escrowExecutionReceipt.sha256 -ceq $escrowSha -and
        (Get-TestFileSha256 -Path $stagedEscrowPath) -ceq $escrowSha -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($stagedEscrowPath)) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($script:EscrowTool))
    )
    $escrowReplay = Invoke-TestPowerShellFile `
        -FilePath $escrowWrapperOne `
        -Environment @{ ProgramData = $escrowProgramData }
    $escrowReplayReceipt = Read-TestJsonReceipt -Result $escrowReplay -Success $true
    Assert-True 'generated escrow wrapper replay is immutable and idempotent' (
        $escrowReplayReceipt.status -ceq 'ALREADY_STAGED' -and
        @(Get-ChildItem -LiteralPath ([IO.Path]::GetDirectoryName($stagedEscrowPath)) -Force).Count -eq 1
    )

    $badEscrowOutput = Join-Path $script:TestRoot 'bad-escrow-wrapper.ps1'
    $badEscrowBuild = Invoke-TestPowerShellFile `
        -FilePath $script:EscrowBuilder `
        -ArgumentList @(
            '-StageDriverPath', $script:StageDriver,
            '-EscrowToolPath', $script:EscrowTool,
            '-OutputPath', $badEscrowOutput,
            '-ExpectedStageDriverSha256', ('0' * 64),
            '-ExpectedEscrowToolSha256', $escrowSha
        )
    $badEscrowReceipt = Read-TestJsonReceipt -Result $badEscrowBuild -Success $false
    Assert-True 'escrow builder refuses a caller-bound driver mismatch before output creation' (
        $badEscrowReceipt.code -ceq 'stage_driver_digest_mismatch' -and
        -not (Test-Path -LiteralPath $badEscrowOutput)
    )

    $badEscrowToolOutput = Join-Path $script:TestRoot 'bad-escrow-tool-wrapper.ps1'
    $badEscrowToolBuild = Invoke-TestPowerShellFile `
        -FilePath $script:EscrowBuilder `
        -ArgumentList @(
            '-StageDriverPath', $script:StageDriver,
            '-EscrowToolPath', $script:EscrowTool,
            '-OutputPath', $badEscrowToolOutput,
            '-ExpectedStageDriverSha256', $stageSha,
            '-ExpectedEscrowToolSha256', ('0' * 64)
        )
    $badEscrowToolReceipt = Read-TestJsonReceipt -Result $badEscrowToolBuild -Success $false
    Assert-True 'escrow builder refuses a caller-bound tool mismatch before output creation' (
        $badEscrowToolReceipt.code -ceq 'escrow_tool_digest_mismatch' -and
        -not (Test-Path -LiteralPath $badEscrowToolOutput)
    )

    $tamperedEscrowWrapper = Join-Path $script:TestRoot 'tampered-escrow-wrapper.ps1'
    $embeddedDriverBase64 = Get-TestEmbeddedValue -Text $escrowWrapperText -VariableName 'stageDriverBase64'
    $tamperedDriverBase64 = $(if ($embeddedDriverBase64[0] -ceq 'A') { 'B' } else { 'A' }) +
        $embeddedDriverBase64.Substring(1)
    $tamperedEscrowText = $escrowWrapperText.Replace(
        "`$stageDriverBase64 = '$embeddedDriverBase64'",
        "`$stageDriverBase64 = '$tamperedDriverBase64'"
    )
    [IO.File]::WriteAllText($tamperedEscrowWrapper, $tamperedEscrowText, (New-Object Text.UTF8Encoding($false)))
    $tamperedEscrowProgramData = Join-Path $script:TestRoot 'tampered-escrow-ProgramData'
    New-Item -ItemType Directory -Path $tamperedEscrowProgramData | Out-Null
    $tamperedEscrowExecution = Invoke-TestPowerShellFile `
        -FilePath $tamperedEscrowWrapper `
        -Environment @{ ProgramData = $tamperedEscrowProgramData }
    Assert-True 'escrow wrapper detects embedded driver corruption before guest mutation' (
        $tamperedEscrowExecution.exit_code -ne 0 -and
        ($tamperedEscrowExecution.stdout + $tamperedEscrowExecution.stderr) -match 'embedded_stage_driver_digest_mismatch' -and
        -not (Test-Path -LiteralPath (Join-Path $tamperedEscrowProgramData 'SFDC24'))
    ) (($tamperedEscrowExecution.stdout + $tamperedEscrowExecution.stderr).Trim())

    $releaseArchive = New-TestReleaseArchive -Name 'valid-release'
    $archiveSha = Get-TestFileSha256 -Path $releaseArchive
    $installerSha = Get-TestFileSha256 -Path $script:ReleaseInstaller
    $releaseId = '1234567890abcdef1234567890abcdef12345678'
    $releaseWrapperOne = Join-Path $script:TestRoot 'release-wrapper-one.ps1'
    $releaseWrapperTwo = Join-Path $script:TestRoot 'release-wrapper-two.ps1'
    $releaseBuilderArguments = @(
        '-ArchivePath', $releaseArchive,
        '-InstallerPath', $script:ReleaseInstaller,
        '-ExpectedArchiveSha256', $archiveSha,
        '-ExpectedInstallerSha256', $installerSha,
        '-ReleaseId', $releaseId
    )
    $releaseBuildOne = Invoke-TestPowerShellFile `
        -FilePath $script:ReleaseBuilder `
        -ArgumentList @($releaseBuilderArguments + @('-OutputPath', $releaseWrapperOne))
    $releaseBuildOneReceipt = Read-TestJsonReceipt -Result $releaseBuildOne -Success $true
    $releaseBuildTwo = Invoke-TestPowerShellFile `
        -FilePath $script:ReleaseBuilder `
        -ArgumentList @($releaseBuilderArguments + @('-OutputPath', $releaseWrapperTwo))
    $releaseBuildTwoReceipt = Read-TestJsonReceipt -Result $releaseBuildTwo -Success $true
    [byte[]]$releaseWrapperBytes = [IO.File]::ReadAllBytes($releaseWrapperOne)
    $releaseWrapperText = (New-Object Text.UTF8Encoding($false, $true)).GetString($releaseWrapperBytes)
    Assert-True 'release wrapper is deterministic, BOM-free, and binds full release identity and both digests' (
        [Convert]::ToBase64String($releaseWrapperBytes) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($releaseWrapperTwo)) -and
        -not ($releaseWrapperBytes.Length -ge 3 -and $releaseWrapperBytes[0] -eq 0xef -and $releaseWrapperBytes[1] -eq 0xbb -and $releaseWrapperBytes[2] -eq 0xbf) -and
        $releaseBuildOneReceipt.wrapper_sha256 -ceq $releaseBuildTwoReceipt.wrapper_sha256 -and
        $releaseBuildOneReceipt.release_id -ceq $releaseId -and
        $releaseBuildOneReceipt.release_file_count -eq 6 -and
        (Get-TestEmbeddedValue -Text $releaseWrapperText -VariableName 'releaseId') -ceq $releaseId -and
        (Get-TestEmbeddedValue -Text $releaseWrapperText -VariableName 'expectedArchiveSha256') -ceq $archiveSha -and
        (Get-TestEmbeddedValue -Text $releaseWrapperText -VariableName 'expectedInstallerSha256') -ceq $installerSha
    )
    $embeddedArchive = [Convert]::FromBase64String(
        (Get-TestEmbeddedValue -Text $releaseWrapperText -VariableName 'archiveBase64')
    )
    $embeddedInstaller = [Convert]::FromBase64String(
        (Get-TestEmbeddedValue -Text $releaseWrapperText -VariableName 'installerBase64')
    )
    Assert-True 'release wrapper round-trip preserves exact archive and installer bytes' (
        [Convert]::ToBase64String($embeddedArchive) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($releaseArchive)) -and
        [Convert]::ToBase64String($embeddedInstaller) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($script:ReleaseInstaller)) -and
        (Get-TestFileSha256 -Path $releaseWrapperOne) -ceq $releaseBuildOneReceipt.wrapper_sha256
    )
    Assert-True 'release builder receipt emits no embedded archive or installer payload' (
        $releaseBuildOne.stdout -notmatch [Regex]::Escape([Convert]::ToBase64String($embeddedArchive)) -and
        $releaseBuildOne.stdout -notmatch [Regex]::Escape([Convert]::ToBase64String($embeddedInstaller))
    )

    $releaseProgramData = Join-Path $script:TestRoot 'release-execution-ProgramData'
    New-Item -ItemType Directory -Path $releaseProgramData | Out-Null
    $releaseExecution = Invoke-TestPowerShellFile `
        -FilePath $releaseWrapperOne `
        -Environment @{ ProgramData = $releaseProgramData }
    $releaseExecutionReceipt = Read-TestJsonReceipt -Result $releaseExecution -Success $true
    $installedRelease = Join-Path $releaseProgramData ('SFDC24\OrderSupervisor\releases\' + $releaseId)
    $installedManifest = Get-Content -LiteralPath (Join-Path $installedRelease '.release.json') -Raw | ConvertFrom-Json
    $installedFilesValid = $true
    foreach ($relativePath in $script:ReleaseFiles) {
        $installedHash = Get-TestFileSha256 -Path (Join-Path $installedRelease $relativePath)
        if ($installedHash -cne [string]$installedManifest.file_sha256.$relativePath) {
            $installedFilesValid = $false
        }
    }
    Assert-True 'generated release wrapper executes against an isolated root with exact manifest and six-file hashes' (
        $releaseExecutionReceipt.status -ceq 'INSTALLED' -and
        $releaseExecutionReceipt.release_id -ceq $releaseId -and
        $installedManifest.release_id -ceq $releaseId -and
        $installedManifest.archive_sha256 -ceq $archiveSha -and
        @($installedManifest.file_sha256.PSObject.Properties).Count -eq 6 -and
        $installedFilesValid
    )
    $releaseReplay = Invoke-TestPowerShellFile `
        -FilePath $releaseWrapperOne `
        -Environment @{ ProgramData = $releaseProgramData }
    $releaseReplayReceipt = Read-TestJsonReceipt -Result $releaseReplay -Success $true
    Assert-True 'generated release wrapper replays the exact installed package idempotently' (
        $releaseReplayReceipt.status -ceq 'ALREADY_INSTALLED' -and
        $releaseReplayReceipt.release_id -ceq $releaseId
    )

    $badReleaseIdOutput = Join-Path $script:TestRoot 'bad-release-id-wrapper.ps1'
    $badReleaseIdBuild = Invoke-TestPowerShellFile `
        -FilePath $script:ReleaseBuilder `
        -ArgumentList @(
            '-ArchivePath', $releaseArchive,
            '-InstallerPath', $script:ReleaseInstaller,
            '-OutputPath', $badReleaseIdOutput,
            '-ExpectedArchiveSha256', $archiveSha,
            '-ExpectedInstallerSha256', $installerSha,
            '-ReleaseId', '1234567'
        )
    $badReleaseIdReceipt = Read-TestJsonReceipt -Result $badReleaseIdBuild -Success $false
    Assert-True 'release builder rejects abbreviated release IDs before output creation' (
        $badReleaseIdReceipt.code -ceq 'release_id_invalid' -and
        -not (Test-Path -LiteralPath $badReleaseIdOutput)
    )

    $badArchiveOutput = Join-Path $script:TestRoot 'bad-archive-wrapper.ps1'
    $badArchiveBuild = Invoke-TestPowerShellFile `
        -FilePath $script:ReleaseBuilder `
        -ArgumentList @(
            '-ArchivePath', $releaseArchive,
            '-InstallerPath', $script:ReleaseInstaller,
            '-OutputPath', $badArchiveOutput,
            '-ExpectedArchiveSha256', ('f' * 64),
            '-ExpectedInstallerSha256', $installerSha,
            '-ReleaseId', $releaseId
        )
    $badArchiveReceipt = Read-TestJsonReceipt -Result $badArchiveBuild -Success $false
    Assert-True 'release builder refuses archive digest mismatch before creating output' (
        $badArchiveReceipt.code -ceq 'archive_digest_mismatch' -and
        -not (Test-Path -LiteralPath $badArchiveOutput)
    )

    $badInstallerOutput = Join-Path $script:TestRoot 'bad-installer-wrapper.ps1'
    $badInstallerBuild = Invoke-TestPowerShellFile `
        -FilePath $script:ReleaseBuilder `
        -ArgumentList @(
            '-ArchivePath', $releaseArchive,
            '-InstallerPath', $script:ReleaseInstaller,
            '-OutputPath', $badInstallerOutput,
            '-ExpectedArchiveSha256', $archiveSha,
            '-ExpectedInstallerSha256', ('f' * 64),
            '-ReleaseId', $releaseId
        )
    $badInstallerReceipt = Read-TestJsonReceipt -Result $badInstallerBuild -Success $false
    Assert-True 'release builder refuses installer digest mismatch before creating output' (
        $badInstallerReceipt.code -ceq 'installer_digest_mismatch' -and
        -not (Test-Path -LiteralPath $badInstallerOutput)
    )

    $extraArchive = New-TestReleaseArchive -Name 'extra-release' -Extra @{ 'scripts\unexpected.ps1' = 'extra' }
    $extraArchiveOutput = Join-Path $script:TestRoot 'extra-archive-wrapper.ps1'
    $extraArchiveBuild = Invoke-TestPowerShellFile `
        -FilePath $script:ReleaseBuilder `
        -ArgumentList @(
            '-ArchivePath', $extraArchive,
            '-InstallerPath', $script:ReleaseInstaller,
            '-OutputPath', $extraArchiveOutput,
            '-ExpectedArchiveSha256', (Get-TestFileSha256 -Path $extraArchive),
            '-ExpectedInstallerSha256', $installerSha,
            '-ReleaseId', $releaseId
        )
    $extraArchiveReceipt = Read-TestJsonReceipt -Result $extraArchiveBuild -Success $false
    Assert-True 'release builder refuses non-canonical extra archive inventory' (
        $extraArchiveReceipt.code -ceq 'archive_files_extra' -and
        -not (Test-Path -LiteralPath $extraArchiveOutput)
    )

    $tamperedReleaseWrapper = Join-Path $script:TestRoot 'tampered-release-wrapper.ps1'
    $embeddedArchiveBase64 = Get-TestEmbeddedValue -Text $releaseWrapperText -VariableName 'archiveBase64'
    $tamperedArchiveBase64 = $(if ($embeddedArchiveBase64[0] -ceq 'A') { 'B' } else { 'A' }) +
        $embeddedArchiveBase64.Substring(1)
    $tamperedReleaseText = $releaseWrapperText.Replace(
        "`$archiveBase64 = '$embeddedArchiveBase64'",
        "`$archiveBase64 = '$tamperedArchiveBase64'"
    )
    [IO.File]::WriteAllText($tamperedReleaseWrapper, $tamperedReleaseText, (New-Object Text.UTF8Encoding($false)))
    $tamperedReleaseProgramData = Join-Path $script:TestRoot 'tampered-release-ProgramData'
    New-Item -ItemType Directory -Path $tamperedReleaseProgramData | Out-Null
    $tamperedReleaseExecution = Invoke-TestPowerShellFile `
        -FilePath $tamperedReleaseWrapper `
        -Environment @{ ProgramData = $tamperedReleaseProgramData }
    Assert-True 'release wrapper detects embedded archive corruption before guest mutation' (
        $tamperedReleaseExecution.exit_code -ne 0 -and
        ($tamperedReleaseExecution.stdout + $tamperedReleaseExecution.stderr) -match 'embedded_archive_digest_mismatch' -and
        -not (Test-Path -LiteralPath (Join-Path $tamperedReleaseProgramData 'SFDC24'))
    ) (($tamperedReleaseExecution.stdout + $tamperedReleaseExecution.stderr).Trim())

    $overwriteBytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($releaseWrapperOne))
    $overwriteBuild = Invoke-TestPowerShellFile `
        -FilePath $script:ReleaseBuilder `
        -ArgumentList @($releaseBuilderArguments + @('-OutputPath', $releaseWrapperOne))
    $overwriteReceipt = Read-TestJsonReceipt -Result $overwriteBuild -Success $false
    Assert-True 'wrapper builders never overwrite an existing output' (
        $overwriteReceipt.code -ceq 'output_already_exists' -and
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($releaseWrapperOne)) -ceq $overwriteBytes
    )

    foreach ($builderPath in @($script:EscrowBuilder, $script:ReleaseBuilder)) {
        $builderText = [IO.File]::ReadAllText($builderPath)
        Assert-True ('builder exposes no secret or credential parameter: ' + [IO.Path]::GetFileName($builderPath)) (
            $builderText -notmatch '(?im)^\s*\[[^\]]*\]\s*\$(Secret|ApiKey|Token|Credential)\b' -and
            $builderText -notmatch '(?im)^\s*\$(Secret|ApiKey|Token|Credential)\s*='
        )
    }
} finally {
    if (-not $KeepArtifacts -and [IO.Directory]::Exists($script:TestRoot)) {
        $resolvedTestRoot = [IO.Path]::GetFullPath($script:TestRoot).TrimEnd('\', '/')
        $tempPrefix = $script:SystemTemp + [IO.Path]::DirectorySeparatorChar
        if ($resolvedTestRoot.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolvedTestRoot) -match '^order-deployment-wrapper-test-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction Stop
        }
    }
}

[Console]::Out.WriteLine(("RESULT passed={0} failed={1}" -f $script:Passed, $script:Failed))
if ($script:Failed -gt 0) { exit 1 }
