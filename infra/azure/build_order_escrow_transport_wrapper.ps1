#Requires -Version 5.1
<#
.SYNOPSIS
Builds a self-contained Azure source.script wrapper for escrow-tool staging.

.DESCRIPTION
Both inputs are non-secret deployment source.  Their exact bytes and
caller-supplied lowercase SHA-256 digests are bound into a BOM-free wrapper.
The wrapper verifies the embedded stage driver before invoking it in-process,
so the larger escrow-tool Base64 never crosses the Windows command line.
#>
[CmdletBinding()]
param(
    [AllowEmptyString()][string]$StageDriverPath = '',
    [AllowEmptyString()][string]$EscrowToolPath = '',
    [AllowEmptyString()][string]$OutputPath = '',
    [AllowEmptyString()][string]$ExpectedStageDriverSha256 = '',
    [AllowEmptyString()][string]$ExpectedEscrowToolSha256 = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:EscrowWrapperReceiptSchema = 'blackboard.order-escrow-stage-wrapper-build-receipt.v1'
$script:EscrowWrapperMaxInputBytes = 1500000

function Get-EscrowWrapperBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Read-EscrowWrapperInputFile {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw $MissingCode
    }
    try { $fullPath = [IO.Path]::GetFullPath($Path) }
    catch { throw $MissingCode }
    try { $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop }
    catch { throw $MissingCode }
    if (-not ($item -is [IO.FileInfo]) -or
        ([int]$item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw $UnsafeCode
    }
    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($item.FullName) }
    catch { throw ($UnsafeCode + '_read_failed') }
    if ($bytes.Length -eq 0 -or $bytes.Length -gt $script:EscrowWrapperMaxInputBytes) {
        throw ($UnsafeCode + '_size_invalid')
    }
    return [pscustomobject][ordered]@{
        path = [string]$item.FullName
        bytes = $bytes
        sha256 = Get-EscrowWrapperBytesSha256 -Bytes $bytes
    }
}

function Assert-EscrowWrapperExecutableUtf8 {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$ErrorPrefix
    )

    if ($Bytes.Length -ge 3 -and
        $Bytes[0] -eq 0xef -and $Bytes[1] -eq 0xbb -and $Bytes[2] -eq 0xbf) {
        throw ($ErrorPrefix + '_utf8_bom_forbidden')
    }
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $text = $utf8.GetString($Bytes)
    } catch { throw ($ErrorPrefix + '_utf8_invalid') }
    try { [void][ScriptBlock]::Create($text) }
    catch { throw ($ErrorPrefix + '_powershell_invalid') }
}

function Write-EscrowWrapperNewUtf8File {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw 'output_path_invalid'
    }
    try { $fullPath = [IO.Path]::GetFullPath($Path) }
    catch { throw 'output_path_invalid' }
    $parentPath = [IO.Path]::GetDirectoryName($fullPath)
    try { $parent = Get-Item -LiteralPath $parentPath -Force -ErrorAction Stop }
    catch { throw 'output_parent_missing' }
    if (-not ($parent -is [IO.DirectoryInfo]) -or
        ([int]$parent.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'output_parent_unsafe'
    }
    if (Test-Path -LiteralPath $fullPath) { throw 'output_already_exists' }

    $encoding = New-Object Text.UTF8Encoding($false, $true)
    [byte[]]$bytes = $encoding.GetBytes($Text)
    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $fullPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } catch { throw 'output_create_failed' }
    finally { if ($null -ne $stream) { $stream.Dispose() } }

    try { [byte[]]$readback = [IO.File]::ReadAllBytes($fullPath) }
    catch { throw 'output_readback_failed' }
    if ([Convert]::ToBase64String($readback) -cne [Convert]::ToBase64String($bytes)) {
        throw 'output_readback_mismatch'
    }
    return [pscustomobject][ordered]@{
        path = $fullPath
        bytes = $readback
        sha256 = Get-EscrowWrapperBytesSha256 -Bytes $readback
    }
}

function New-OrderEscrowTransportWrapper {
    param(
        [AllowEmptyString()][string]$DriverPath,
        [AllowEmptyString()][string]$ToolPath,
        [AllowEmptyString()][string]$DestinationPath,
        [AllowEmptyString()][string]$DriverSha256,
        [AllowEmptyString()][string]$ToolSha256
    )

    if ($DriverSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'stage_driver_sha256_invalid' }
    if ($ToolSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'escrow_tool_sha256_invalid' }
    $driver = Read-EscrowWrapperInputFile `
        -Path $DriverPath `
        -MissingCode 'stage_driver_missing' `
        -UnsafeCode 'stage_driver_unsafe'
    $tool = Read-EscrowWrapperInputFile `
        -Path $ToolPath `
        -MissingCode 'escrow_tool_missing' `
        -UnsafeCode 'escrow_tool_unsafe'
    if ($driver.sha256 -cne $DriverSha256) { throw 'stage_driver_digest_mismatch' }
    if ($tool.sha256 -cne $ToolSha256) { throw 'escrow_tool_digest_mismatch' }
    Assert-EscrowWrapperExecutableUtf8 -Bytes $driver.bytes -ErrorPrefix 'stage_driver'
    Assert-EscrowWrapperExecutableUtf8 -Bytes $tool.bytes -ErrorPrefix 'escrow_tool'

    $driverPayload = [Convert]::ToBase64String($driver.bytes)
    $toolPayload = [Convert]::ToBase64String($tool.bytes)
    $lines = @(
        '#Requires -Version 5.1',
        "`$ErrorActionPreference = 'Stop'",
        'Set-StrictMode -Version 2.0',
        "`$stageDriverBase64 = '$driverPayload'",
        "`$escrowToolBase64 = '$toolPayload'",
        "`$expectedStageDriverSha256 = '$DriverSha256'",
        "`$expectedEscrowToolSha256 = '$ToolSha256'",
        '$stageDriverBytes = [Convert]::FromBase64String($stageDriverBase64)',
        '$sha = [Security.Cryptography.SHA256]::Create()',
        'try { $actualStageDriverSha256 = (($sha.ComputeHash($stageDriverBytes) | ForEach-Object { $_.ToString(''x2'') }) -join '''') }',
        'finally { $sha.Dispose() }',
        "if (`$actualStageDriverSha256 -cne `$expectedStageDriverSha256) { throw 'embedded_stage_driver_digest_mismatch' }",
        '$strictUtf8 = New-Object Text.UTF8Encoding($false, $true)',
        'try { $stageDriverText = $strictUtf8.GetString($stageDriverBytes) }',
        "catch { throw 'embedded_stage_driver_utf8_invalid' }",
        'try { $stageDriver = [ScriptBlock]::Create($stageDriverText) }',
        "catch { throw 'embedded_stage_driver_powershell_invalid' }",
        '& $stageDriver -Payload $escrowToolBase64 -Sha256 $expectedEscrowToolSha256'
    )
    $wrapperText = ($lines -join "`r`n") + "`r`n"
    $output = Write-EscrowWrapperNewUtf8File -Path $DestinationPath -Text $wrapperText
    return [pscustomobject][ordered]@{
        schema = $script:EscrowWrapperReceiptSchema
        ok = $true
        output_path = [string]$output.path
        wrapper_sha256 = [string]$output.sha256
        wrapper_bytes = [long]$output.bytes.Length
        stage_driver_sha256 = $DriverSha256
        escrow_tool_sha256 = $ToolSha256
    }
}

function Get-EscrowWrapperSafeErrorCode {
    param([Parameter(Mandatory = $true)]$Record)

    $message = [string]$Record.Exception.Message
    if ($message -cmatch '^[a-z][a-z0-9_]{0,95}$') { return $message }
    return 'order_escrow_wrapper_build_failed'
}

if ($MyInvocation.InvocationName -cne '.') {
    try {
        $receipt = New-OrderEscrowTransportWrapper `
            -DriverPath $StageDriverPath `
            -ToolPath $EscrowToolPath `
            -DestinationPath $OutputPath `
            -DriverSha256 $ExpectedStageDriverSha256 `
            -ToolSha256 $ExpectedEscrowToolSha256
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Compress))
        exit 0
    } catch {
        $receipt = [ordered]@{
            schema = $script:EscrowWrapperReceiptSchema
            ok = $false
            code = (Get-EscrowWrapperSafeErrorCode -Record $_)
        }
        [Console]::Error.WriteLine(($receipt | ConvertTo-Json -Compress))
        exit 1
    }
}
