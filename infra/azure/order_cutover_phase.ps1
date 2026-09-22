#Requires -Version 5.1
<#
.SYNOPSIS
Runs one bounded, receipt-producing phase of the ORDER guest cutover.

.DESCRIPTION
This driver is intended to be delivered as the exact source of one Azure
Managed Run Command.  It never calls Azure or the Blackboard bus.  It reuses
the independently staged task-escrow tool and the immutable release installer
only after their absolute paths and caller-pinned SHA-256 digests match.

Every accepted invocation emits exactly one compact JSON receipt.  The receipt
is correlated by a caller-generated operation ID and is hard-bounded to 3072
UTF-8 bytes so Azure's retained instance-view output cannot silently truncate
it.  Child-process output is captured and never forwarded.

RestoreReady deliberately wraps Restore instead of relying on the escrow
tool's emergency stop behavior: the exact pinned Observe or Execute candidate
must be quiescent (Ready or already Disabled), and the underlying Restore
receipt must prove task_stopped=false.
#>
[CmdletBinding()]
param(
    [string]$Action = '',
    [string]$OperationId = '',

    [string]$EscrowToolPath = '',
    [string]$ExpectedEscrowToolSha256 = '',
    [string]$EscrowId = '',
    [string]$ExpectedEscrowReleaseId = '',

    [string]$InstallerPath = '',
    [string]$ExpectedInstallerSha256 = '',
    [string]$ExpectedReleaseId = '',

    [string]$RestoredInstallerPath = '',
    [string]$ExpectedRestoredInstallerSha256 = '',

    [string]$Mode = 'Observe',
    [string]$UserProfilePath = 'C:\Users\akatiawam',
    [string]$WorkspacePath = 'C:\Users\akatiawam\Blackboard',
    [string]$EnvFile = 'C:\Users\akatiawam\Blackboard\.env',
    [string]$StatePath = 'C:\ProgramData\SFDC24\OrderSupervisor\state.json',
    [string]$LogPath = 'C:\ProgramData\SFDC24\OrderSupervisor\events.jsonl',
    [string]$ClaudeCommand = 'C:\Users\akatiawam\.local\bin\claude.exe',
    [string]$WallTimeoutSeconds = '720',

    [string]$MaxRuns = '8',
    [string]$TimeoutSeconds = '600',
    [string]$PollMilliseconds = '500',
    [string]$NaturalTriggerMarginSeconds = '60',

    [string]$ExpectedTerminalStatus = '',
    [string]$ExpectedWorkId = '',
    [string]$ExpectedRowId = '',
    [string]$ExpectedResultStatus = '',

    [string]$ExpectedCurrentTaskResult = '',
    [string]$ExpectedCurrentFailureCode = '',
    [string]$ExpectedCurrentRunId = '',

    [string]$ExpectedDisabledXmlSha256 = '',
    [string]$ExpectedCurrentReleaseId = '',
    [string]$ExpectedCurrentXmlSha256 = '',

    [string]$GitPath = '',
    [string]$ExpectedGitSha256 = '',
    [string]$ExpectedClaudeSha256 = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:CutoverSchema = 'blackboard.order-cutover-phase-receipt.v1'
$script:CutoverReceiptMaximumBytes = 3072
$script:CutoverTaskName = 'SFDC24 Blackboard Order Worker'
$script:CutoverTaskPath = '\'
$script:CutoverStateSchema = 'order_supervisor_state.v1'
$script:CutoverSourceTag = 'vm-order-worker'
$script:CutoverMetadataRelativePath = 'SFDC24\OrderSupervisor'
$script:CutoverEscrowToolPrefix = 'order_task_escrow.'
$script:CutoverEscrowToolSuffix = '.ps1'
$script:CutoverInstallerRelativePath = 'scripts\install_order_supervisor.ps1'
$script:CutoverChildMaximumCharacters = 16384
$script:CutoverStateMaximumBytes = 1048576
$script:CutoverLogMaximumBytes = 67108864
$script:CutoverLogDeltaMaximumBytes = 1048576
$script:CutoverTreeMaximumEntries = 50000
$script:CutoverProtectedFileMaximumBytes = 268435456
$script:CutoverPinnedExecutableMaximumBytes = 536870912
$script:CutoverProtectedTreeMaximumBytes = 536870912
$script:CutoverClockToleranceSeconds = 2
$script:CutoverTaskStartupMaximumSeconds = 30

function Throw-Cutover {
    param([Parameter(Mandatory = $true)][string]$Code)
    throw $Code
}

function Get-CutoverSafeAction {
    param([AllowEmptyString()][string]$Value)
    foreach ($known in @(
        'ValidateEscrowAndDisable',
        'DrainObserve',
        'InstallObserveAndDrain',
        'InstallObserveAndDrainFromFailedExecute',
        'InstallExecuteReady',
        'InstallObserveReadyFromReadyObserve',
        'RestoreReady',
        'StartAndAwait',
        'InstallObserveAndDrainFromDisabledExecute',
        'InstallObserveReadyFromDisabledHttpError',
        'InstallObserveReadyFromDisabledObserveHttpError'
    )) {
        if ($Value -ieq $known) { return $known }
    }
    return 'Invalid'
}

function Get-CutoverSafeOperationId {
    param([AllowEmptyString()][string]$Value)
    if ($Value -cmatch '^[0-9a-f]{32}$') { return $Value }
    return 'invalid'
}

function Get-CutoverExpectedFailedExecuteCode {
    param([AllowEmptyString()][string]$Value)
    # This transition is an incident-specific bridge for the known old release
    # header-width incompatibility.  Transport/HTTP failures are intentionally
    # excluded because they can be transient and do not authenticate deploy lag.
    if ($Value -cne 'BOARD_HEADER_INVALID') {
        Throw-Cutover -Code 'EXPECTED_CURRENT_FAILURE_CODE_INVALID'
    }
    return $Value
}

function Get-CutoverSafeErrorCode {
    param([Parameter(Mandatory = $true)]$Record)
    $message = [string]$Record.Exception.Message
    if ($message -cmatch '^[A-Za-z][A-Za-z0-9_]{0,95}$') {
        return $message.ToUpperInvariant()
    }
    return 'ORDER_CUTOVER_PHASE_FAILED'
}

function Get-CutoverBytesSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Get-CutoverTextSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    return Get-CutoverBytesSha256 -Bytes ([Text.Encoding]::UTF8.GetBytes($Text))
}

function Get-CutoverUnicodeTextSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    $encoding = New-Object Text.UnicodeEncoding($false, $true)
    [byte[]]$bytes = @($encoding.GetPreamble()) + @($encoding.GetBytes($Text))
    return Get-CutoverBytesSha256 -Bytes $bytes
}

function Get-CutoverFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = $null
    $sha = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $sha = [Security.Cryptography.SHA256]::Create()
        return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    } catch { Throw-Cutover -Code 'FILE_DIGEST_FAILED' }
    finally {
        if ($sha) { $sha.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Test-CutoverReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)
    return (([int]$Item.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Resolve-CutoverAbsolutePath {
    param(
        [AllowEmptyString()][string]$Value,
        [Parameter(Mandatory = $true)][string]$Code,
        [switch]$ForbidVolumeRoot
    )
    if ([string]::IsNullOrWhiteSpace($Value) -or
        $Value.IndexOf([char]0) -ge 0 -or
        $Value.IndexOf('"') -ge 0 -or
        $Value.IndexOf("`r") -ge 0 -or
        $Value.IndexOf("`n") -ge 0 -or
        -not [IO.Path]::IsPathRooted($Value)) {
        Throw-Cutover -Code $Code
    }
    try { $full = [IO.Path]::GetFullPath($Value).TrimEnd('\', '/') }
    catch { Throw-Cutover -Code $Code }
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($full) -or
        ($ForbidVolumeRoot -and [string]::Equals($full, $root, [StringComparison]::OrdinalIgnoreCase))) {
        Throw-Cutover -Code $Code
    }
    return $full
}

function Assert-CutoverPathChainSafe {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Code
    )
    $current = [IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (Test-Path -LiteralPath $current) {
            try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop }
            catch { Throw-Cutover -Code $Code }
            if (Test-CutoverReparsePoint -Item $item) { Throw-Cutover -Code $Code }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrWhiteSpace($parent) -or
            [string]::Equals($parent, $current, [StringComparison]::OrdinalIgnoreCase)) { break }
        $current = $parent
    }
}

function Assert-CutoverSafeFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )
    Assert-CutoverPathChainSafe -Path $Path -Code $UnsafeCode
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Throw-Cutover -Code $MissingCode }
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { Throw-Cutover -Code $MissingCode }
    if ($item.PSIsContainer -or (Test-CutoverReparsePoint -Item $item)) { Throw-Cutover -Code $UnsafeCode }
}

function Assert-CutoverSafeDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$UnsafeCode
    )
    Assert-CutoverPathChainSafe -Path $Path -Code $UnsafeCode
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { Throw-Cutover -Code $MissingCode }
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { Throw-Cutover -Code $MissingCode }
    if (-not $item.PSIsContainer -or (Test-CutoverReparsePoint -Item $item)) { Throw-Cutover -Code $UnsafeCode }
}

function ConvertTo-CutoverInteger {
    param(
        [AllowEmptyString()][string]$Value,
        [Parameter(Mandatory = $true)][int]$Minimum,
        [Parameter(Mandatory = $true)][int]$Maximum,
        [Parameter(Mandatory = $true)][string]$Code
    )
    if ($Value -cnotmatch '^(0|[1-9][0-9]{0,8})$') { Throw-Cutover -Code $Code }
    $parsed = 0
    if (-not [int]::TryParse($Value, [Globalization.NumberStyles]::None, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed) -or
        $parsed -lt $Minimum -or $parsed -gt $Maximum) {
        Throw-Cutover -Code $Code
    }
    return $parsed
}

function ConvertTo-CutoverCommandLineArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 4096 -or $Value.IndexOf([char]0) -ge 0 -or
        $Value.IndexOf("`r") -ge 0 -or $Value.IndexOf("`n") -ge 0) {
        Throw-Cutover -Code 'CHILD_ARGUMENT_INVALID'
    }
    if ($Value -cnotmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    $null = $builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            $null = $builder.Append(('\' * (($slashes * 2) + 1)))
            $null = $builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) { $null = $builder.Append(('\' * $slashes)); $slashes = 0 }
        $null = $builder.Append($character)
    }
    if ($slashes -gt 0) { $null = $builder.Append(('\' * ($slashes * 2))) }
    $null = $builder.Append('"')
    return $builder.ToString()
}

function Assert-CutoverNoDuplicateJsonKeys {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$Code
    )
    try { Add-Type -AssemblyName System.Runtime.Serialization -ErrorAction Stop }
    catch { Throw-Cutover -Code $Code }
    $reader = $null
    try {
        $reader = [Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader(
            $Bytes,
            [Xml.XmlDictionaryReaderQuotas]::Max
        )
        $document = New-Object Xml.XmlDocument
        $document.XmlResolver = $null
        $document.Load($reader)
    } catch { Throw-Cutover -Code $Code }
    finally { if ($reader) { $reader.Close() } }

    $pending = New-Object System.Collections.Queue
    $pending.Enqueue($document.DocumentElement)
    while ($pending.Count -gt 0) {
        $node = $pending.Dequeue()
        if ($node.NodeType -ne [Xml.XmlNodeType]::Element) { continue }
        if ($node.GetAttribute('type') -ceq 'object') {
            $keys = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
            foreach ($child in @($node.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })) {
                $key = if ($child.NamespaceURI -ceq 'item' -and $child.LocalName -ceq 'item') {
                    [string]$child.GetAttribute('item')
                } else { [string]$child.LocalName }
                if (-not $keys.Add($key)) { Throw-Cutover -Code $Code }
            }
        }
        foreach ($child in @($node.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })) {
            $pending.Enqueue($child)
        }
    }
}

function ConvertFrom-CutoverJsonText {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][string]$Code
    )
    try { [byte[]]$bytes = (New-Object Text.UTF8Encoding($false, $true)).GetBytes($Text) }
    catch { Throw-Cutover -Code $Code }
    Assert-CutoverNoDuplicateJsonKeys -Bytes $bytes -Code $Code
    try { return $Text | ConvertFrom-Json -ErrorAction Stop }
    catch { Throw-Cutover -Code $Code }
}

function Invoke-CutoverProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$FailureCode
    )
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = (@($Arguments | ForEach-Object { ConvertTo-CutoverCommandLineArgument -Value ([string]$_) }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { Throw-Cutover -Code $FailureCode }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill() } catch {}
            Throw-Cutover -Code ($FailureCode + '_TIMEOUT')
        }
        $process.WaitForExit()
        $stdout = [string]$stdoutTask.Result
        $stderr = [string]$stderrTask.Result
        if ($stdout.Length -gt $script:CutoverChildMaximumCharacters -or
            $stderr.Length -gt $script:CutoverChildMaximumCharacters) {
            Throw-Cutover -Code ($FailureCode + '_OUTPUT_TOO_LARGE')
        }
        return [pscustomobject][ordered]@{
            exit_code = [int]$process.ExitCode
            stdout = $stdout
            stderr = $stderr
        }
    } catch {
        if ([string]$_.Exception.Message -cmatch '^[A-Z][A-Z0-9_]{0,95}$') { throw }
        Throw-Cutover -Code $FailureCode
    } finally {
        $process.Dispose()
    }
}

function ConvertFrom-CutoverSingleJsonLine {
    param(
        [AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][string]$Code
    )
    # A BLANK LINE IS BLANK ONLY BY JSON'S DEFINITION, HERE TOO.
    #
    # IsNullOrWhiteSpace drops every Char.IsWhiteSpace-only line, so a receipt
    # such as VT + CRLF + {"ok":true} + CRLF + VT was reduced to one line and
    # accepted. Measured on this exact base: VT, FF, NBSP and U+2028 lines were
    # all discarded that way, and this parser governs the escrow receipt and
    # every mutating installer action - the strict side of the contract. The
    # document parser's trim was narrowed for the same reason; this closes the
    # same gap one filter to the left. Found by Copilot on PR114.
    $jsonWhitespace = [char[]]@([char]0x20, [char]0x09, [char]0x0D, [char]0x0A)
    $lines = @($Text -split "`r?`n" | Where-Object { $_.Trim($jsonWhitespace).Length -gt 0 })
    if ($lines.Count -ne 1 -or $lines[0].Length -gt $script:CutoverChildMaximumCharacters) {
        Throw-Cutover -Code $Code
    }
    $value = ConvertFrom-CutoverJsonText -Text $lines[0] -Code $Code
    if ($null -eq $value -or $value.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
        Throw-Cutover -Code $Code
    }
    return $value
}

function ConvertFrom-CutoverSingleJsonDocument {
    param(
        [AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][string]$Code
    )
    if ([string]::IsNullOrWhiteSpace($Text)) { Throw-Cutover -Code $Code }
    # TRIM ONLY WHAT JSON ITSELF CALLS WHITESPACE.
    #
    # Argument-less String.Trim() strips every character char.IsWhiteSpace
    # accepts, which is a much larger set than RFC 8259's four.  Measured
    # under Windows PowerShell 5.1, it silently removes U+000B, U+000C,
    # U+0085, U+00A0, U+2028 and U+3000, none of which JSON permits as
    # framing - so a receipt wrapped in them parsed clean instead of failing
    # closed.  Routing every Status receipt through here widens that gap to
    # every release, so it is closed first.  Naming the four explicitly also
    # keeps the rule readable: space, tab, carriage return, line feed.
    $trimmed = $Text.Trim(
        [char]0x20, [char]0x09, [char]0x0D, [char]0x0A)
    if ($trimmed.Length -eq 0 -or [int][char]$trimmed[0] -eq 0xfeff) {
        Throw-Cutover -Code $Code
    }
    try { [byte[]]$bytes = (New-Object Text.UTF8Encoding($false, $true)).GetBytes($trimmed) }
    catch { Throw-Cutover -Code $Code }
    if ($bytes.Length -eq 0 -or $bytes.Length -gt $script:CutoverChildMaximumCharacters) {
        Throw-Cutover -Code $Code
    }
    $value = ConvertFrom-CutoverJsonText -Text $trimmed -Code $Code
    if ($null -eq $value -or $value.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
        Throw-Cutover -Code $Code
    }
    return $value
}

# WHY THE CARVEOUT IS KEYED ON THE ACTION AND NOT ON A PINNED RELEASE.
#
# Pinning it to one exact legacy release and digest is the narrower rule,
# and it was the first shape of this repair.  What ruled it out is the
# failure mode when a constant is wrong: measured, one byte off in either
# value falls straight through to the one-line rule and reproduces the
# identical INSTALLER_RECEIPT_INVALID being fixed here - discovered only
# after another Managed Run Command round trip against the guest.  Neither
# value is recorded anywhere in this repository, so neither can be checked
# before that round trip is spent.
#
# Keying on the action costs almost nothing in narrowness.  Status is
# read-only, and every MUTATING action keeps the one-physical-line
# contract, which is safe because mutating actions are only ever issued
# against $Context.installer_path - the candidate release - and that
# installer now emits -Compress.  A pretty Status receipt is accepted on
# its own merits instead of on a promise about which release wrote it:
# exactly one JSON document, no duplicate object keys, a PSCustomObject
# root, and a hard size bound.
function ConvertFrom-CutoverInstallerReceipt {
    param(
        [AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][string]$RequestedAction
    )
    if ($RequestedAction -ceq 'Status') {
        return ConvertFrom-CutoverSingleJsonDocument -Text $Text -Code 'INSTALLER_RECEIPT_INVALID'
    }
    return ConvertFrom-CutoverSingleJsonLine -Text $Text -Code 'INSTALLER_RECEIPT_INVALID'
}

function Get-CutoverWindowsPowerShell {
    if ([string]::IsNullOrWhiteSpace($env:SystemRoot)) { Throw-Cutover -Code 'SYSTEM_ROOT_INVALID' }
    $path = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Assert-CutoverSafeFile -Path $path -MissingCode 'WINDOWS_POWERSHELL_MISSING' -UnsafeCode 'WINDOWS_POWERSHELL_UNSAFE'
    return $path
}

function Assert-CutoverPinnedFile {
    param(
        [AllowEmptyString()][string]$Path,
        [AllowEmptyString()][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][string]$Prefix
    )
    if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Cutover -Code ($Prefix + '_SHA256_INVALID') }
    $resolved = Resolve-CutoverAbsolutePath -Value $Path -Code ($Prefix + '_PATH_INVALID') -ForbidVolumeRoot
    if ($resolved -cne $ExpectedPath) { Throw-Cutover -Code ($Prefix + '_PATH_IDENTITY_MISMATCH') }
    Assert-CutoverSafeFile -Path $resolved -MissingCode ($Prefix + '_MISSING') -UnsafeCode ($Prefix + '_UNSAFE')
    if ((Get-CutoverFileSha256 -Path $resolved) -cne $ExpectedSha256) {
        Throw-Cutover -Code ($Prefix + '_SHA256_MISMATCH')
    }
    return $resolved
}

function Resolve-CutoverInstaller {
    param(
        [AllowEmptyString()][string]$Path,
        [AllowEmptyString()][string]$ExpectedSha256,
        [AllowEmptyString()][string]$ReleaseId,
        [Parameter(Mandatory = $true)][string]$Prefix
    )
    if ($ReleaseId -cnotmatch '^[0-9a-f]{40}$') { Throw-Cutover -Code ($Prefix + '_RELEASE_ID_INVALID') }
    $programData = Resolve-CutoverAbsolutePath -Value $env:ProgramData -Code 'PROGRAM_DATA_INVALID' -ForbidVolumeRoot
    $expected = Join-Path $programData ($script:CutoverMetadataRelativePath + '\releases\' + $ReleaseId + '\' + $script:CutoverInstallerRelativePath)
    $expected = [IO.Path]::GetFullPath($expected).TrimEnd('\', '/')
    return Assert-CutoverPinnedFile -Path $Path -ExpectedSha256 $ExpectedSha256 -ExpectedPath $expected -Prefix $Prefix
}

function Resolve-CutoverEscrowTool {
    param(
        [AllowEmptyString()][string]$Path,
        [AllowEmptyString()][string]$ExpectedSha256
    )
    $programData = Resolve-CutoverAbsolutePath -Value $env:ProgramData -Code 'PROGRAM_DATA_INVALID' -ForbidVolumeRoot
    $expectedName = $script:CutoverEscrowToolPrefix + $ExpectedSha256 + $script:CutoverEscrowToolSuffix
    $expected = Join-Path $programData ($script:CutoverMetadataRelativePath + '\tools\' + $expectedName)
    $expected = [IO.Path]::GetFullPath($expected).TrimEnd('\', '/')
    return Assert-CutoverPinnedFile -Path $Path -ExpectedSha256 $ExpectedSha256 -ExpectedPath $expected -Prefix 'ESCROW_TOOL'
}

function Invoke-CutoverChildScript {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$FailureCode
    )
    $powershell = Get-CutoverWindowsPowerShell
    $allArguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $ScriptPath) + $Arguments
    return Invoke-CutoverProcess -FilePath $powershell -Arguments $allArguments -TimeoutSeconds $TimeoutSeconds -FailureCode $FailureCode
}

function Invoke-CutoverEscrow {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$RequestedAction
    )
    $null = Assert-CutoverPinnedFile `
        -Path $Context.escrow_tool_path `
        -ExpectedSha256 $Context.expected_escrow_tool_sha256 `
        -ExpectedPath $Context.escrow_tool_path `
        -Prefix 'ESCROW_TOOL'
    $result = Invoke-CutoverChildScript `
        -ScriptPath $Context.escrow_tool_path `
        -Arguments @(
            '-Action', $RequestedAction,
            '-ExpectedReleaseId', $Context.escrow_release_id,
            '-EscrowId', $Context.escrow_id
        ) `
        -TimeoutSeconds 120 `
        -FailureCode 'ESCROW_CHILD_FAILED'
    if ($result.exit_code -ne 0 -or -not [string]::IsNullOrWhiteSpace($result.stderr)) {
        Throw-Cutover -Code 'ESCROW_CHILD_FAILED'
    }
    $receipt = ConvertFrom-CutoverSingleJsonLine -Text $result.stdout -Code 'ESCROW_RECEIPT_INVALID'
    $expectedStatus = if ($RequestedAction -ceq 'Validate') { 'VALID' } else { 'RESTORED' }
    if ($receipt.schema -isnot [string] -or [string]$receipt.schema -cne 'blackboard.order-task-escrow-receipt.v1' -or
        $receipt.ok -isnot [bool] -or -not [bool]$receipt.ok -or
        $receipt.action -isnot [string] -or [string]$receipt.action -cne $RequestedAction -or
        $receipt.status -isnot [string] -or [string]$receipt.status -cne $expectedStatus -or
        $receipt.escrow_id -isnot [string] -or [string]$receipt.escrow_id -cne $Context.escrow_id -or
        $receipt.escrow_path -isnot [string] -or [string]$receipt.escrow_path -cne $Context.escrow_path -or
        $receipt.task_name -isnot [string] -or [string]$receipt.task_name -cne $script:CutoverTaskName -or
        $receipt.task_path -isnot [string] -or [string]$receipt.task_path -cne $script:CutoverTaskPath -or
        $receipt.expected_release_id -isnot [string] -or [string]$receipt.expected_release_id -cne $Context.escrow_release_id -or
        $receipt.task_xml_sha256 -isnot [string] -or [string]$receipt.task_xml_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $receipt.release_file_count -isnot [int] -or [int]$receipt.release_file_count -ne 6 -or
        $receipt.task_stopped -isnot [bool]) {
        Throw-Cutover -Code 'ESCROW_RECEIPT_INVALID'
    }
    if ($RequestedAction -ceq 'Validate' -and [bool]$receipt.task_stopped) {
        Throw-Cutover -Code 'ESCROW_VALIDATE_STOPPED_TASK'
    }
    return $receipt
}

function Get-CutoverInstallerArguments {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$RequestedAction,
        [Parameter(Mandatory = $true)][string]$RequestedMode,
        [AllowEmptyString()][string]$ExpectedCurrentTaskXmlSha256 = ''
    )
    $arguments = @(
        '-Action', $RequestedAction,
        '-Mode', $RequestedMode,
        '-UserProfilePath', $Context.user_profile_path,
        '-WorkspacePath', $Context.workspace_path,
        '-EnvFile', $Context.env_file,
        '-StatePath', $Context.state_path,
        '-LogPath', $Context.log_path,
        '-WallTimeoutSeconds', ([string]$Context.wall_timeout_seconds),
        '-ClaudeCommand', $Context.claude_command
    )
    if (-not [string]::IsNullOrEmpty($ExpectedCurrentTaskXmlSha256)) {
        $arguments += @('-ExpectedCurrentTaskXmlSha256', $ExpectedCurrentTaskXmlSha256)
    }
    return $arguments
}

function Invoke-CutoverInstaller {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$RequestedAction,
        [Parameter(Mandatory = $true)][string]$RequestedMode,
        [AllowEmptyString()][string]$ExpectedCurrentTaskXmlSha256 = ''
    )
    if ($ScriptPath -ceq $Context.installer_path) {
        $expectedSha256 = $Context.expected_installer_sha256
        $prefix = 'INSTALLER'
    } elseif ($ScriptPath -ceq $Context.restored_installer_path) {
        $expectedSha256 = $Context.expected_restored_installer_sha256
        $prefix = 'RESTORED_INSTALLER'
    } else {
        Throw-Cutover -Code 'INSTALLER_PATH_IDENTITY_MISMATCH'
    }
    $null = Assert-CutoverPinnedFile `
        -Path $ScriptPath `
        -ExpectedSha256 $expectedSha256 `
        -ExpectedPath $ScriptPath `
        -Prefix $prefix
    $result = Invoke-CutoverChildScript `
        -ScriptPath $ScriptPath `
        -Arguments (Get-CutoverInstallerArguments `
            -Context $Context `
            -RequestedAction $RequestedAction `
            -RequestedMode $RequestedMode `
            -ExpectedCurrentTaskXmlSha256 $ExpectedCurrentTaskXmlSha256) `
        -TimeoutSeconds $(if (@('Install', 'InstallFromDisabledNoStop') -ccontains $RequestedAction) { 180 } else { 60 }) `
        -FailureCode 'INSTALLER_CHILD_FAILED'
    if ($result.exit_code -ne 0 -or -not [string]::IsNullOrWhiteSpace($result.stderr)) {
        Throw-Cutover -Code 'INSTALLER_CHILD_FAILED'
    }
    # Read-only Status may arrive pretty-printed; every mutating action
    # still owes exactly one physical line.
    return ConvertFrom-CutoverInstallerReceipt `
        -Text $result.stdout `
        -RequestedAction $RequestedAction
}

function ConvertTo-CutoverUtcDateTime {
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Value,
        [Parameter(Mandatory = $true)][string]$Code
    )
    # A MISSING TIME IS A NAMED FAILURE, NOT A BINDING ERROR.
    #
    # The mandatory untyped parameter rejected $null during binding, so a status
    # or task read with no last or next run failed with "Cannot bind argument to
    # parameter 'Value' because it is null." - no cutover code, from a path no
    # caller can name. AllowNull lets it reach the cast, which refuses it, and
    # the catch turns that into $Code like every other malformed value.
    #
    # Copilot (PR114) argued the cast maps $null to DateTime.MinValue, which
    # would make this a fail-open needing an explicit guard. Measured, it does
    # not: [datetime]$null throws on Windows PowerShell 5.1.19041 and on pwsh
    # 7.5.4, as do '' and whitespace. An explicit guard was written and then
    # removed, because no test on either runtime could fail it - the shape this
    # repository calls a guard that cannot fail.
    try { return ([DateTime]$Value).ToUniversalTime() }
    catch { Throw-Cutover -Code $Code }
}

function Test-CutoverUtcStamp {
    param([AllowEmptyString()][string]$Value)
    if ($Value -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?:[0-9]{3}|[0-9]{7})Z$') {
        return $false
    }
    $format = if ($Value.Length -eq 24) { 'yyyy-MM-ddTHH:mm:ss.fffZ' } else { 'yyyy-MM-ddTHH:mm:ss.fffffffZ' }
    $parsed = [DateTime]::MinValue
    return [DateTime]::TryParseExact(
        $Value,
        $format,
        [Globalization.CultureInfo]::InvariantCulture,
        ([Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal),
        [ref]$parsed
    )
}

function Assert-CutoverInstallerStatus {
    param(
        [Parameter(Mandatory = $true)]$Status,
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskState,
        [bool]$RequireResultZero = $true,
        # THE RESULT A REPLACED TASK INHERITS IS NOT THE CANDIDATE'S RESULT.
        #
        # InstallFromDisabledNoStop replaces the definition without
        # unregistering it, and Task Scheduler keeps LastTaskResult from the
        # worker that ran before.  So immediately after the candidate is
        # installed the task still reports the OLD failure - 20 on the guest
        # - and demanding 0 there threw INSTALLER_STATUS_MISMATCH after the
        # disable and after the install, which is a post-mutation failure.
        #
        # This does NOT relax the rule to 'any non-zero'.  That would accept a
        # NEW failure appearing between the disable and the install, which is
        # exactly the drift these reads exist to catch.  The only tolerated
        # value is the one already pinned in expected_current_task_result and
        # checked twice against the exact failed run before the disable.  Once
        # the candidate has actually run, 0 is required again.
        [bool]$AllowInheritedTaskResult = $false
    )
    $expectedStatus = switch ($ExpectedTaskState) {
        'Ready' { 'READY' }
        'Disabled' { 'DISABLED' }
        'Running' { 'RUNNING' }
        'Queued' { 'NOT_READY' }
        default { Throw-Cutover -Code 'EXPECTED_TASK_STATE_INVALID' }
    }
    $lastTaskResultTypeValid = ($Status.last_task_result -is [long] -or $Status.last_task_result -is [int])
    # Decided here rather than inside the -or chain below so the type check
    # still guards the cast: the chain short-circuits, and reordering it would
    # cast a value that has not been proved numeric yet.
    $taskResultAccepted = $true
    if ($lastTaskResultTypeValid -and $RequireResultZero) {
        $taskResultAccepted = ([int64]$Status.last_task_result -eq 0)
        if (-not $taskResultAccepted -and $AllowInheritedTaskResult) {
            $taskResultAccepted = ([int64]$Status.last_task_result -eq [int64]$Context.expected_current_task_result)
        }
    }
    if ($Status.status -isnot [string] -or [string]$Status.status -cne $expectedStatus -or
        $Status.task_name -isnot [string] -or [string]$Status.task_name -cne $script:CutoverTaskName -or
        $Status.task_path -isnot [string] -or [string]$Status.task_path -cne $script:CutoverTaskPath -or
        $Status.state -isnot [string] -or [string]$Status.state -cne $ExpectedTaskState -or
        $Status.mode -isnot [string] -or [string]$Status.mode -cne $ExpectedMode -or
        $Status.user_profile -isnot [string] -or [string]$Status.user_profile -cne $Context.user_profile_path -or
        $Status.workspace_path -isnot [string] -or [string]$Status.workspace_path -cne $Context.workspace_path -or
        $Status.state_path -isnot [string] -or [string]$Status.state_path -cne $Context.state_path -or
        $Status.log_path -isnot [string] -or [string]$Status.log_path -cne $Context.log_path -or
        $Status.wall_timeout_seconds -isnot [int] -or [int]$Status.wall_timeout_seconds -ne $Context.wall_timeout_seconds -or
        -not $lastTaskResultTypeValid -or
        -not $taskResultAccepted -or
        $Status.runner_exists -isnot [bool] -or -not [bool]$Status.runner_exists -or
        $Status.env_file_exists -isnot [bool] -or -not [bool]$Status.env_file_exists -or
        $null -eq $Status.drift -or @($Status.drift).Count -ne 0 -or
        $null -eq $Status.wall_timeout_readback -or
        $Status.wall_timeout_readback.confirmed -isnot [bool] -or -not [bool]$Status.wall_timeout_readback.confirmed) {
        Throw-Cutover -Code 'INSTALLER_STATUS_MISMATCH'
    }
    if ($ExpectedMode -ceq 'Execute' -and
        ($Status.workspace_git_directory_exists -isnot [bool] -or -not [bool]$Status.workspace_git_directory_exists)) {
        Throw-Cutover -Code 'INSTALLER_STATUS_MISMATCH'
    }
    $last = ConvertTo-CutoverUtcDateTime -Value $Status.last_run_time -Code 'TASK_LAST_RUN_TIME_INVALID'
    $next = [DateTime]::MinValue
    if ($ExpectedTaskState -cne 'Disabled') {
        $next = ConvertTo-CutoverUtcDateTime -Value $Status.next_run_time -Code 'TASK_NEXT_RUN_TIME_INVALID'
    }
    return [pscustomobject][ordered]@{
        raw = $Status
        last_run_utc = $last
        next_run_utc = $next
    }
}

function Get-CutoverExactInstallerStatus {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskState,
        [bool]$RequireResultZero = $true,
        [bool]$AllowInheritedTaskResult = $false
    )
    $status = Invoke-CutoverInstaller -Context $Context -ScriptPath $ScriptPath -RequestedAction 'Status' -RequestedMode $ExpectedMode
    return Assert-CutoverInstallerStatus `
        -Status $status `
        -Context $Context `
        -ExpectedMode $ExpectedMode `
        -ExpectedTaskState $ExpectedTaskState `
        -RequireResultZero $RequireResultZero `
        -AllowInheritedTaskResult $AllowInheritedTaskResult
}

function Get-CutoverExactInstallerStatusAnyState {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [Parameter(Mandatory = $true)][string[]]$AllowedStates,
        [bool]$RequireResultZero = $true,
        [bool]$AllowInheritedTaskResult = $false
    )
    $states = @($AllowedStates | Select-Object -Unique)
    if ($states.Count -lt 1 -or
        @($states | Where-Object { @('Ready', 'Disabled', 'Running', 'Queued') -cnotcontains $_ }).Count -ne 0) {
        Throw-Cutover -Code 'EXPECTED_TASK_STATE_INVALID'
    }
    $status = Invoke-CutoverInstaller `
        -Context $Context `
        -ScriptPath $ScriptPath `
        -RequestedAction 'Status' `
        -RequestedMode $ExpectedMode
    if ($status.state -isnot [string] -or $states -cnotcontains [string]$status.state) {
        Throw-Cutover -Code 'INSTALLER_STATUS_MISMATCH'
    }
    return Assert-CutoverInstallerStatus `
        -Status $status `
        -Context $Context `
        -ExpectedMode $ExpectedMode `
        -ExpectedTaskState ([string]$status.state) `
        -RequireResultZero $RequireResultZero `
        -AllowInheritedTaskResult $AllowInheritedTaskResult
}

function Get-CutoverTaskRuntime {
    try { $matches = @(Get-ScheduledTask -TaskName $script:CutoverTaskName -ErrorAction Stop) }
    catch { Throw-Cutover -Code 'TASK_QUERY_FAILED' }
    if ($matches.Count -ne 1 -or [string]$matches[0].TaskName -cne $script:CutoverTaskName -or
        [string]$matches[0].TaskPath -cne $script:CutoverTaskPath) {
        Throw-Cutover -Code 'TASK_IDENTITY_INVALID'
    }
    try { $info = Get-ScheduledTaskInfo -TaskName $script:CutoverTaskName -TaskPath $script:CutoverTaskPath -ErrorAction Stop }
    catch { Throw-Cutover -Code 'TASK_INFO_FAILED' }
    return ConvertTo-CutoverTaskRuntimeInfo -State ([string]$matches[0].State) -Info $info
}


function ConvertTo-CutoverTaskRuntimeInfo {
    param([Parameter(Mandatory = $true)][string]$State, [Parameter(Mandatory = $true)]$Info)
    return [pscustomobject][ordered]@{
        state = $State
        last_run_utc = (ConvertTo-CutoverUtcDateTime -Value $Info.LastRunTime -Code 'TASK_LAST_RUN_TIME_INVALID')
        next_run_utc = $(if ($State -ceq 'Disabled') { [DateTime]::MinValue } else { ConvertTo-CutoverUtcDateTime -Value $Info.NextRunTime -Code 'TASK_NEXT_RUN_TIME_INVALID' })
        last_task_result = [int64]$Info.LastTaskResult
    }
}

function Start-CutoverTask {
    try { Start-ScheduledTask -TaskName $script:CutoverTaskName -TaskPath $script:CutoverTaskPath -ErrorAction Stop }
    catch { Throw-Cutover -Code 'TASK_START_FAILED' }
}

function Disable-CutoverTask {
    try { Disable-ScheduledTask -TaskName $script:CutoverTaskName -TaskPath $script:CutoverTaskPath -ErrorAction Stop | Out-Null }
    catch { Throw-Cutover -Code 'TASK_DISABLE_FAILED' }
}

function Disable-CutoverTaskAfterFailure {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Installer,
        [Parameter(Mandatory = $true)][string[]]$ExpectedModes,
        [int]$CleanupTimeoutSeconds = 0
    )
    $modes = @($ExpectedModes | Select-Object -Unique)
    if ($modes.Count -lt 1 -or $modes.Count -gt 2 -or
        @($modes | Where-Object { @('Observe', 'Execute') -cnotcontains $_ }).Count -ne 0) {
        Throw-Cutover -Code 'FAILURE_CLEANUP_MODE_INVALID'
    }
    if ($CleanupTimeoutSeconds -eq 0) {
        $cleanupSeconds = [Math]::Min(900, [Math]::Max(60, ([int]$Context.wall_timeout_seconds + 60)))
    } elseif ($CleanupTimeoutSeconds -ge 1 -and $CleanupTimeoutSeconds -le 900) {
        $cleanupSeconds = $CleanupTimeoutSeconds
    } else {
        Throw-Cutover -Code 'FAILURE_CLEANUP_TIMEOUT_INVALID'
    }

    $runtime = Get-CutoverTaskRuntime
    if (@('Ready', 'Running', 'Queued', 'Disabled') -cnotcontains $runtime.state) {
        Throw-Cutover -Code 'FAILURE_CLEANUP_TASK_STATE_INVALID'
    }
    $initialState = [string]$runtime.state
    $xmlBefore = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    $disablePerformed = $false
    if ($xmlBefore.enabled) {
        Disable-CutoverTask
        $disablePerformed = $true
    }
    $xmlAfter = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    if ($xmlAfter.enabled) {
        $exception = New-Object InvalidOperationException('FAILURE_CLEANUP_DISABLE_NOT_VERIFIED')
        $exception.Data['future_triggers_disabled'] = $false
        $exception.Data['cleanup_task_stopped'] = $false
        throw $exception
    }
    if ($xmlAfter.normalized_sha256 -cne $xmlBefore.normalized_sha256) {
        $exception = New-Object InvalidOperationException('FAILURE_CLEANUP_DEFINITION_DRIFT')
        $exception.Data['future_triggers_disabled'] = $true
        $exception.Data['cleanup_definition_preserved'] = $false
        $exception.Data['cleanup_task_stopped'] = $false
        throw $exception
    }

    # Disabling a running Scheduled Task prevents future triggers but does not
    # terminate its current instance.  Wait only for natural completion; this
    # driver intentionally owns no stop/kill path.
    $deadline = [DateTime]::UtcNow.AddSeconds($cleanupSeconds)
    do {
        $runtime = Get-CutoverTaskRuntime
        if ($runtime.state -ceq 'Disabled') { break }
        if (@('Running', 'Queued') -cnotcontains $runtime.state) {
            $exception = New-Object InvalidOperationException('FAILURE_CLEANUP_TASK_STATE_INVALID')
            $exception.Data['future_triggers_disabled'] = $true
            $exception.Data['cleanup_definition_preserved'] = $true
            $exception.Data['cleanup_task_stopped'] = $false
            throw $exception
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            $exception = New-Object InvalidOperationException('FAILURE_CLEANUP_WAIT_TIMEOUT')
            $exception.Data['future_triggers_disabled'] = $true
            $exception.Data['cleanup_definition_preserved'] = $true
            $exception.Data['cleanup_task_stopped'] = $false
            $exception.Data['active_instance_status'] = 'PERSISTED'
            throw $exception
        }
        Start-Sleep -Milliseconds 500
    } while ($true)

    $finalXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    if ($finalXml.enabled -or $finalXml.normalized_sha256 -cne $xmlBefore.normalized_sha256) {
        $exception = New-Object InvalidOperationException('FAILURE_CLEANUP_DEFINITION_DRIFT')
        $exception.Data['future_triggers_disabled'] = (-not $finalXml.enabled)
        $exception.Data['cleanup_definition_preserved'] = $false
        $exception.Data['cleanup_task_stopped'] = $false
        throw $exception
    }

    $verifiedModes = New-Object 'System.Collections.Generic.List[string]'
    foreach ($mode in $modes) {
        try {
            $null = Get-CutoverExactInstallerStatus `
                -Context $Context `
                -ScriptPath $Installer `
                -ExpectedMode $mode `
                -ExpectedTaskState 'Disabled' `
                -RequireResultZero $false
            $verifiedModes.Add($mode)
        } catch {}
    }
    if ($verifiedModes.Count -ne 1) {
        $exception = New-Object InvalidOperationException('FAILURE_CLEANUP_DEFINITION_NOT_VERIFIED')
        $exception.Data['future_triggers_disabled'] = $true
        $exception.Data['cleanup_definition_preserved'] = $true
        $exception.Data['cleanup_task_stopped'] = $false
        throw $exception
    }
    return [pscustomobject][ordered]@{
        mode = $verifiedModes[0]
        initial_task_state = $initialState
        final_task_state = 'Disabled'
        disable_performed = $disablePerformed
        future_triggers_disabled = $true
        definition_preserved_except_enabled = $true
        task_stopped = $false
    }
}

function Throw-CutoverAfterCleanup {
    param(
        [Parameter(Mandatory = $true)]$FailureRecord,
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Installer,
        [Parameter(Mandatory = $true)][string[]]$ExpectedModes,
        [AllowEmptyString()][string]$FallbackInstaller = '',
        [string[]]$FallbackModes = @()
    )
    if ($FailureRecord.Exception.Data.Contains('cleanup_status')) { throw $FailureRecord }
    $originalCode = Get-CutoverSafeErrorCode -Record $FailureRecord
    $cleanupFailure = $null
    $cleanup = $null
    try {
        $cleanup = Disable-CutoverTaskAfterFailure `
            -Context $Context `
            -Installer $Installer `
            -ExpectedModes $ExpectedModes
    } catch { $cleanupFailure = $_ }
    if ($null -ne $cleanupFailure -and
        (Get-CutoverSafeErrorCode -Record $cleanupFailure) -ceq 'FAILURE_CLEANUP_DEFINITION_NOT_VERIFIED' -and
        -not [string]::IsNullOrEmpty($FallbackInstaller) -and @($FallbackModes).Count -gt 0) {
        try {
            $cleanup = Disable-CutoverTaskAfterFailure `
                -Context $Context `
                -Installer $FallbackInstaller `
                -ExpectedModes $FallbackModes
            $cleanupFailure = $null
        } catch { $cleanupFailure = $_ }
    }
    if ($null -ne $cleanupFailure) {
        $cleanupCode = Get-CutoverSafeErrorCode -Record $cleanupFailure
        $exception = New-Object InvalidOperationException('CUTOVER_FAILURE_CLEANUP_FAILED')
        $exception.Data['original_code'] = $originalCode
        $exception.Data['cleanup_status'] = 'FAILED'
        $exception.Data['cleanup_code'] = $cleanupCode
        foreach ($name in @('future_triggers_disabled', 'cleanup_definition_preserved', 'cleanup_task_stopped', 'active_instance_status')) {
            if ($cleanupFailure.Exception.Data.Contains($name)) {
                $exception.Data[$name] = $cleanupFailure.Exception.Data[$name]
            }
        }
        throw $exception
    }
    $FailureRecord.Exception.Data['original_code'] = $originalCode
    $FailureRecord.Exception.Data['cleanup_status'] = 'DISABLED_VERIFIED'
    $FailureRecord.Exception.Data['cleanup_mode'] = [string]$cleanup.mode
    $FailureRecord.Exception.Data['future_triggers_disabled'] = [bool]$cleanup.future_triggers_disabled
    $FailureRecord.Exception.Data['cleanup_definition_preserved'] = [bool]$cleanup.definition_preserved_except_enabled
    $FailureRecord.Exception.Data['cleanup_task_stopped'] = [bool]$cleanup.task_stopped
    if ($null -ne $cleanup.PSObject.Properties['initial_task_state']) {
        $FailureRecord.Exception.Data['cleanup_initial_task_state'] = [string]$cleanup.initial_task_state
    }
    if ($null -ne $cleanup.PSObject.Properties['final_task_state']) {
        $FailureRecord.Exception.Data['cleanup_final_task_state'] = [string]$cleanup.final_task_state
    }
    if ($null -ne $cleanup.PSObject.Properties['disable_performed']) {
        $FailureRecord.Exception.Data['cleanup_disable_performed'] = [bool]$cleanup.disable_performed
    }
    throw $FailureRecord
}

function Export-CutoverTaskXml {
    try { $xml = [string](Export-ScheduledTask -TaskName $script:CutoverTaskName -TaskPath $script:CutoverTaskPath -ErrorAction Stop) }
    catch { Throw-Cutover -Code 'TASK_EXPORT_FAILED' }
    if ([string]::IsNullOrWhiteSpace($xml) -or [Text.Encoding]::UTF8.GetByteCount($xml) -gt 1048576) {
        Throw-Cutover -Code 'TASK_XML_INVALID'
    }
    return $xml
}

function Get-CutoverTaskXmlEvidence {
    param([Parameter(Mandatory = $true)][string]$Text)
    $document = New-Object Xml.XmlDocument
    $document.PreserveWhitespace = $true
    $document.XmlResolver = $null
    try { $document.LoadXml($Text) }
    catch { Throw-Cutover -Code 'TASK_XML_INVALID' }
    $settings = @($document.SelectNodes('/*[local-name()="Task"]/*[local-name()="Settings"]'))
    $enabled = @($document.SelectNodes('/*[local-name()="Task"]/*[local-name()="Settings"]/*[local-name()="Enabled"]'))
    if ($settings.Count -ne 1 -or $enabled.Count -gt 1 -or
        ($enabled.Count -eq 1 -and @('true', 'false') -cnotcontains [string]$enabled[0].InnerText)) {
        Throw-Cutover -Code 'TASK_XML_ENABLED_INVALID'
    }
    # Task Scheduler can omit Enabled from an exported enabled task even though
    # the schema's default is true. Canonicalize omitted and explicit values to
    # one explicit true node before comparing pre/post definitions. Reloading
    # without insignificant whitespace keeps an omitted enabled line and an
    # explicit disabled line comparable after the scheduler materializes it.
    $normalized = New-Object Xml.XmlDocument
    $normalized.PreserveWhitespace = $false
    $normalized.XmlResolver = $null
    try { $normalized.LoadXml($Text) }
    catch { Throw-Cutover -Code 'TASK_XML_INVALID' }
    $normalizedSettings = @($normalized.SelectNodes('/*[local-name()="Task"]/*[local-name()="Settings"]'))
    $normalizedEnabled = @($normalized.SelectNodes('/*[local-name()="Task"]/*[local-name()="Settings"]/*[local-name()="Enabled"]'))
    if ($normalizedSettings.Count -ne 1 -or $normalizedEnabled.Count -gt 1) {
        Throw-Cutover -Code 'TASK_XML_ENABLED_INVALID'
    }
    if ($normalizedEnabled.Count -eq 1) {
        $null = $normalizedSettings[0].RemoveChild($normalizedEnabled[0])
    }
    $canonicalEnabled = $normalized.CreateElement('Enabled', [string]$normalizedSettings[0].NamespaceURI)
    $canonicalEnabled.InnerText = 'true'
    $null = $normalizedSettings[0].AppendChild($canonicalEnabled)
    $isEnabled = ($enabled.Count -eq 0 -or [string]$enabled[0].InnerText -ceq 'true')
    return [pscustomobject][ordered]@{
        enabled = $isEnabled
        utf8_text_sha256 = (Get-CutoverTextSha256 -Text $Text)
        utf16le_bom_sha256 = (Get-CutoverUnicodeTextSha256 -Text $Text)
        normalized_sha256 = (Get-CutoverTextSha256 -Text $normalized.OuterXml)
    }
}

function Assert-CutoverTriggerWindow {
    param(
        [Parameter(Mandatory = $true)][DateTime]$NextRunUtc,
        [Parameter(Mandatory = $true)][int]$RequiredSeconds
    )
    if ($NextRunUtc.Year -lt 2000 -or ($NextRunUtc - [DateTime]::UtcNow).TotalSeconds -le $RequiredSeconds) {
        Throw-Cutover -Code 'NATURAL_TRIGGER_WINDOW_UNAVAILABLE'
    }
}

function Assert-CutoverStableReadyReadback {
    param(
        [Parameter(Mandatory = $true)]$Initial,
        [Parameter(Mandatory = $true)]$Readback,
        [Parameter(Mandatory = $true)][int]$RequiredSeconds,
        [Parameter(Mandatory = $true)][string]$DriftCode
    )
    if ($Readback.last_run_utc.Ticks -ne $Initial.last_run_utc.Ticks) {
        Throw-Cutover -Code $DriftCode
    }
    Assert-CutoverTriggerWindow -NextRunUtc $Readback.next_run_utc -RequiredSeconds $RequiredSeconds
}

function Get-CutoverFileCheckpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][long]$MaximumBytes,
        [Parameter(Mandatory = $true)][string]$MissingCode,
        [Parameter(Mandatory = $true)][string]$InvalidCode
    )
    Assert-CutoverSafeFile -Path $Path -MissingCode $MissingCode -UnsafeCode $InvalidCode
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { Throw-Cutover -Code $InvalidCode }
    if ($item.Length -lt 0 -or $item.Length -gt $MaximumBytes) { Throw-Cutover -Code $InvalidCode }
    return [pscustomobject][ordered]@{
        length = [long]$item.Length
        sha256 = (Get-CutoverFileSha256 -Path $Path)
    }
}

function Assert-CutoverFileCheckpointUnchanged {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][long]$MaximumBytes,
        [Parameter(Mandatory = $true)][string]$Code
    )
    $after = Get-CutoverFileCheckpoint -Path $Path -MaximumBytes $MaximumBytes -MissingCode $Code -InvalidCode $Code
    if ($after.length -ne $Before.length -or [string]$after.sha256 -cne [string]$Before.sha256) {
        Throw-Cutover -Code $Code
    }
}

function Assert-CutoverStateShape {
    param([Parameter(Mandatory = $true)]$State)
    if ($null -eq $state -or $state.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
        Throw-Cutover -Code 'STATE_CONTRACT_INVALID'
    }
    $stateProperties = @($state.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $requiredStateProperties = @(
        'schema', 'source_tag', 'mode', 'initialized', 'cursor', 'last_poll',
        'seen', 'success', 'error', 'counts', 'work'
    )
    if ($stateProperties.Count -ne $requiredStateProperties.Count -or
        @($requiredStateProperties | Where-Object { $stateProperties -cnotcontains $_ }).Count -ne 0 -or
        $state.schema -isnot [string] -or [string]$state.schema -cne $script:CutoverStateSchema -or
        $state.source_tag -isnot [string] -or [string]$state.source_tag -cne $script:CutoverSourceTag -or
        $state.mode -isnot [string] -or @('Observe', 'Execute') -cnotcontains [string]$state.mode -or
        $state.initialized -isnot [bool] -or -not [bool]$state.initialized -or
        $null -eq $state.counts -or $state.counts.GetType().FullName -cne 'System.Management.Automation.PSCustomObject' -or
        $null -eq $state.cursor -or $state.cursor.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
        Throw-Cutover -Code 'STATE_CONTRACT_INVALID'
    }
    $cursorProperties = @($state.cursor.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($cursorProperties.Count -ne 2 -or $cursorProperties -cnotcontains 'timestamp' -or $cursorProperties -cnotcontains 'row_id' -or
        $state.cursor.timestamp -isnot [string] -or $state.cursor.row_id -isnot [string] -or
        ([string]::IsNullOrEmpty([string]$state.cursor.timestamp) -xor [string]::IsNullOrEmpty([string]$state.cursor.row_id)) -or
        (-not [string]::IsNullOrEmpty([string]$state.cursor.timestamp) -and
         (([string]$state.cursor.timestamp -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}Z$') -or
          -not (Test-CutoverUtcStamp -Value ([string]$state.cursor.timestamp)) -or
          [string]$state.cursor.row_id -cnotmatch '^[^\x00-\x20\x7f]{1,140}$'))) {
        Throw-Cutover -Code 'STATE_CURSOR_INVALID'
    }
    $countNames = @('polls', 'seen', 'selected', 'succeeded', 'errors', 'ignored')
    $countProperties = @($state.counts.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($countProperties.Count -ne $countNames.Count -or
        @($countNames | Where-Object { $countProperties -cnotcontains $_ }).Count -ne 0) {
        Throw-Cutover -Code 'STATE_COUNTS_INVALID'
    }
    foreach ($name in $countNames) {
        $value = $state.counts.PSObject.Properties[$name].Value
        if (($value -isnot [int] -and $value -isnot [long]) -or [int64]$value -lt 0) {
            Throw-Cutover -Code 'STATE_COUNTS_INVALID'
        }
    }
    foreach ($optionalName in @('seen', 'success', 'error')) {
        $optional = $state.PSObject.Properties[$optionalName].Value
        if ($null -eq $optional) { continue }
        if ($optional.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
            Throw-Cutover -Code 'STATE_EVIDENCE_INVALID'
        }
        $required = switch ($optionalName) {
            'seen' { @('at', 'timestamp', 'row_id') }
            'success' { @('at', 'event', 'work_id', 'row_id') }
            'error' { @('at', 'code', 'message', 'work_id', 'row_id') }
        }
        $properties = @($optional.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if ($properties.Count -ne $required.Count -or @($required | Where-Object { $properties -cnotcontains $_ }).Count -ne 0 -or
            $optional.at -isnot [string] -or -not (Test-CutoverUtcStamp -Value ([string]$optional.at))) {
            Throw-Cutover -Code 'STATE_EVIDENCE_INVALID'
        }
        foreach ($name in @($required | Where-Object { $_ -cne 'at' })) {
            if ($optional.PSObject.Properties[$name].Value -isnot [string]) {
                Throw-Cutover -Code 'STATE_EVIDENCE_INVALID'
            }
        }
    }
    if ($null -eq $state.work -or $state.work.GetType().FullName -cne 'System.Object[]') {
        Throw-Cutover -Code 'STATE_WORK_INVALID'
    }
    $workItems = @($state.work)
    if ($workItems.Count -gt 500) { Throw-Cutover -Code 'STATE_WORK_INVALID' }
    $workKeys = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($item in $workItems) {
        if ($null -eq $item -or $item.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
            Throw-Cutover -Code 'STATE_WORK_INVALID'
        }
        $properties = @($item.PSObject.Properties | ForEach-Object { [string]$_.Name })
        $required = @('input_row_id', 'work_id', 'status', 'result_status', 'output_sha256', 'updated_at')
        if ($properties.Count -ne $required.Count -or @($required | Where-Object { $properties -cnotcontains $_ }).Count -ne 0 -or
            $item.input_row_id -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$item.input_row_id) -or
            $item.work_id -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$item.work_id) -or
            $item.status -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$item.status) -or
            $item.result_status -isnot [string] -or
            $item.output_sha256 -isnot [string] -or
            (-not [string]::IsNullOrEmpty([string]$item.output_sha256) -and [string]$item.output_sha256 -cnotmatch '^[0-9a-f]{64}$') -or
            $item.updated_at -isnot [string] -or -not (Test-CutoverUtcStamp -Value ([string]$item.updated_at))) {
            Throw-Cutover -Code 'STATE_WORK_INVALID'
        }
        $key = [string]$item.input_row_id + "`n" + [string]$item.work_id
        if (-not $workKeys.Add($key)) { Throw-Cutover -Code 'STATE_WORK_INVALID' }
    }
    return $State
}

function Get-CutoverState {
    param([Parameter(Mandatory = $true)][string]$Path)
    $checkpoint = Get-CutoverFileCheckpoint -Path $Path -MaximumBytes $script:CutoverStateMaximumBytes -MissingCode 'STATE_MISSING' -InvalidCode 'STATE_INVALID'
    try { [byte[]]$bytes = [IO.File]::ReadAllBytes($Path) }
    catch { Throw-Cutover -Code 'STATE_READ_FAILED' }
    if ($bytes.Length -ne $checkpoint.length -or
        (Get-CutoverBytesSha256 -Bytes $bytes) -cne [string]$checkpoint.sha256 -or
        ($bytes.Length -ge 3 -and $bytes[0] -eq 0xef -and $bytes[1] -eq 0xbb -and $bytes[2] -eq 0xbf)) {
        Throw-Cutover -Code 'STATE_INVALID'
    }
    $strict = New-Object Text.UTF8Encoding($false, $true)
    try { $text = $strict.GetString($bytes) }
    catch { Throw-Cutover -Code 'STATE_INVALID' }
    Assert-CutoverNoDuplicateJsonKeys -Bytes $bytes -Code 'STATE_INVALID'
    try { $state = $text | ConvertFrom-Json -ErrorAction Stop }
    catch { Throw-Cutover -Code 'STATE_INVALID' }
    $null = Assert-CutoverStateShape -State $state
    return [pscustomobject][ordered]@{
        value = $state
        checkpoint = $checkpoint
    }
}

function Get-CutoverLastPoll {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [AllowEmptyString()][string]$ExpectedUserProfile = ''
    )
    $lastPollProperties = if ($null -ne $State.last_poll) {
        @($State.last_poll.PSObject.Properties | ForEach-Object { [string]$_.Name })
    } else { @() }
    if ($State.mode -isnot [string] -or [string]$State.mode -cne $ExpectedMode -or
        $null -eq $State.last_poll -or
        $lastPollProperties.Count -ne 5 -or
        @('at', 'run_id', 'status', 'identity', 'user_profile' | Where-Object { $lastPollProperties -cnotcontains $_ }).Count -ne 0 -or
        $State.last_poll.at -isnot [string] -or -not (Test-CutoverUtcStamp -Value ([string]$State.last_poll.at)) -or
        $State.last_poll.run_id -isnot [string] -or [string]$State.last_poll.run_id -cnotmatch '^[0-9a-f]{32}$' -or
        $State.last_poll.status -isnot [string] -or
        $State.last_poll.identity -isnot [string] -or
        @('SYSTEM', 'NT AUTHORITY\SYSTEM') -cnotcontains [string]$State.last_poll.identity -or
        $State.last_poll.user_profile -isnot [string] -or
        -not [IO.Path]::IsPathRooted([string]$State.last_poll.user_profile) -or
        (-not [string]::IsNullOrEmpty($ExpectedUserProfile) -and
         [string]$State.last_poll.user_profile -cne $ExpectedUserProfile)) {
        Throw-Cutover -Code 'STATE_LAST_POLL_INVALID'
    }
    return $State.last_poll
}

function Get-CutoverLogCheckpoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $checkpoint = Get-CutoverFileCheckpoint -Path $Path -MaximumBytes $script:CutoverLogMaximumBytes -MissingCode 'LOG_MISSING' -InvalidCode 'LOG_INVALID'
    if ($checkpoint.length -gt 0) {
        $stream = $null
        try {
            $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            if ($stream.Length -lt $checkpoint.length) { Throw-Cutover -Code 'LOG_PREFIX_BOUNDARY_INVALID' }
            $null = $stream.Seek($checkpoint.length - 1, [IO.SeekOrigin]::Begin)
            if ($stream.ReadByte() -ne 10) { Throw-Cutover -Code 'LOG_PREFIX_BOUNDARY_INVALID' }
        } catch {
            if ([string]$_.Exception.Message -ceq 'LOG_PREFIX_BOUNDARY_INVALID') { throw }
            Throw-Cutover -Code 'LOG_PREFIX_BOUNDARY_INVALID'
        } finally { if ($stream) { $stream.Dispose() } }
    }
    return $checkpoint
}

function Get-CutoverPrefixSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][long]$Length
    )
    $stream = $null
    $sha = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $sha = [Security.Cryptography.SHA256]::Create()
        [byte[]]$buffer = New-Object byte[] 65536
        $remaining = $Length
        while ($remaining -gt 0) {
            $want = [int][Math]::Min([long]$buffer.Length, $remaining)
            $read = $stream.Read($buffer, 0, $want)
            if ($read -le 0) { Throw-Cutover -Code 'LOG_PREFIX_TRUNCATED' }
            $null = $sha.TransformBlock($buffer, 0, $read, $buffer, 0)
            $remaining -= $read
        }
        $null = $sha.TransformFinalBlock((New-Object byte[] 0), 0, 0)
        return (($sha.Hash | ForEach-Object { $_.ToString('x2') }) -join '')
    } catch {
        if ([string]$_.Exception.Message -ceq 'LOG_PREFIX_TRUNCATED') { throw }
        Throw-Cutover -Code 'LOG_PREFIX_READ_FAILED'
    } finally {
        if ($sha) { $sha.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Get-CutoverLogDelta {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Before
    )
    Assert-CutoverSafeFile -Path $Path -MissingCode 'LOG_MISSING' -UnsafeCode 'LOG_INVALID'
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { Throw-Cutover -Code 'LOG_READ_FAILED' }
    if ($item.Length -gt $script:CutoverLogMaximumBytes) { Throw-Cutover -Code 'LOG_TOO_LARGE' }
    if ($item.Length -le $Before.length) { Throw-Cutover -Code 'LOG_NOT_APPENDED' }
    if (($item.Length - $Before.length) -gt $script:CutoverLogDeltaMaximumBytes) { Throw-Cutover -Code 'LOG_DELTA_TOO_LARGE' }
    if ((Get-CutoverPrefixSha256 -Path $Path -Length $Before.length) -cne [string]$Before.sha256) {
        Throw-Cutover -Code 'LOG_PREFIX_CHANGED'
    }
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $null = $stream.Seek($Before.length, [IO.SeekOrigin]::Begin)
        [byte[]]$bytes = New-Object byte[] ([int]($item.Length - $Before.length))
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { Throw-Cutover -Code 'LOG_DELTA_READ_FAILED' }
            $offset += $read
        }
    } catch {
        if ([string]$_.Exception.Message -ceq 'LOG_DELTA_READ_FAILED') { throw }
        Throw-Cutover -Code 'LOG_DELTA_READ_FAILED'
    } finally { if ($stream) { $stream.Dispose() } }
    $strict = New-Object Text.UTF8Encoding($false, $true)
    try { $text = $strict.GetString($bytes) }
    catch { Throw-Cutover -Code 'LOG_DELTA_UTF8_INVALID' }
    if (-not $text.EndsWith("`n", [StringComparison]::Ordinal)) { Throw-Cutover -Code 'LOG_DELTA_INCOMPLETE' }
    $lines = @($text -split "`r?`n" | Where-Object { $_.Length -gt 0 })
    if ($lines.Count -lt 2 -or $lines.Count -gt 1000) { Throw-Cutover -Code 'LOG_DELTA_SHAPE_INVALID' }
    $entries = New-Object 'System.Collections.Generic.List[object]'
    foreach ($line in $lines) {
        $entry = ConvertFrom-CutoverJsonText -Text $line -Code 'LOG_DELTA_JSON_INVALID'
        if ($null -eq $entry -or $entry.GetType().FullName -cne 'System.Management.Automation.PSCustomObject' -or
            $entry.run_id -isnot [string] -or [string]$entry.run_id -cnotmatch '^[0-9a-f]{32}$' -or
            $entry.event -isnot [string]) {
            Throw-Cutover -Code 'LOG_DELTA_ENTRY_INVALID'
        }
        $entries.Add($entry)
    }
    return [pscustomobject][ordered]@{
        entries = $entries.ToArray()
        checkpoint = [pscustomobject][ordered]@{ length = [long]$item.Length; sha256 = (Get-CutoverFileSha256 -Path $Path) }
        appended_bytes = [long]$bytes.Length
    }
}

function Get-CutoverTrailingLogRun {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Checkpoint,
        [Parameter(Mandatory = $true)][string]$RunId
    )
    if ($RunId -cnotmatch '^[0-9a-f]{32}$' -or $Checkpoint.length -le 0) {
        Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID'
    }
    $readLength = [int][Math]::Min([long]$Checkpoint.length, $script:CutoverLogDeltaMaximumBytes)
    $offset = [long]$Checkpoint.length - $readLength
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        if ($stream.Length -lt $Checkpoint.length) { Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID' }
        $null = $stream.Seek($offset, [IO.SeekOrigin]::Begin)
        [byte[]]$bytes = New-Object byte[] $readLength
        $readOffset = 0
        while ($readOffset -lt $bytes.Length) {
            $count = $stream.Read($bytes, $readOffset, $bytes.Length - $readOffset)
            if ($count -le 0) { Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID' }
            $readOffset += $count
        }
    } catch {
        if ([string]$_.Exception.Message -ceq 'LOG_CURRENT_RUN_INVALID') { throw }
        Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID'
    } finally { if ($stream) { $stream.Dispose() } }

    $startIndex = 0
    if ($offset -gt 0) {
        while ($startIndex -lt $bytes.Length -and $bytes[$startIndex] -ne 10) { $startIndex++ }
        if ($startIndex -ge ($bytes.Length - 1)) { Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID' }
        $startIndex++
    }
    [byte[]]$completeBytes = if ($startIndex -eq 0) { $bytes } else { @($bytes[$startIndex..($bytes.Length - 1)]) }
    $strict = New-Object Text.UTF8Encoding($false, $true)
    try { $text = $strict.GetString($completeBytes) }
    catch { Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID' }
    if (-not $text.EndsWith("`n", [StringComparison]::Ordinal)) { Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID' }
    $lines = @($text -split "`r?`n" | Where-Object { $_.Length -gt 0 })
    if ($lines.Count -lt 2) { Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID' }
    $entries = New-Object 'System.Collections.Generic.List[object]'
    foreach ($line in $lines) {
        $entry = ConvertFrom-CutoverJsonText -Text $line -Code 'LOG_CURRENT_RUN_INVALID'
        if ($null -eq $entry -or $entry.GetType().FullName -cne 'System.Management.Automation.PSCustomObject' -or
            $entry.run_id -isnot [string] -or [string]$entry.run_id -cnotmatch '^[0-9a-f]{32}$') {
            Throw-Cutover -Code 'LOG_CURRENT_RUN_INVALID'
        }
        $entries.Add($entry)
    }
    if ([string]$entries[$entries.Count - 1].run_id -cne $RunId) {
        Throw-Cutover -Code 'LOG_CURRENT_RUN_ID_MISMATCH'
    }
    $first = $entries.Count - 1
    while ($first -gt 0 -and [string]$entries[$first - 1].run_id -ceq $RunId) { $first-- }
    return @($entries.ToArray()[$first..($entries.Count - 1)])
}

function Assert-CutoverCurrentTerminalRun {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$ExactStatus,
        [Parameter(Mandatory = $true)]$LogCheckpoint,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [Parameter(Mandatory = $true)][string]$ExpectedStatus,
        [AllowEmptyString()][string]$ExpectedWorkId = '',
        [AllowEmptyString()][string]$ExpectedRowId = '',
        [AllowEmptyString()][string]$ExpectedResultStatus = ''
    )
    $lastPoll = Get-CutoverLastPoll -State $State -ExpectedMode $ExpectedMode -ExpectedUserProfile $Context.user_profile_path
    if ([string]$lastPoll.status -cne $ExpectedStatus) { Throw-Cutover -Code 'STATE_TERMINAL_STATUS_MISMATCH' }
    $entries = @(Get-CutoverTrailingLogRun -Path $Context.log_path -Checkpoint $LogCheckpoint -RunId ([string]$lastPoll.run_id))
    $runWindowEndUtc = [DateTime]::UtcNow
    $logTerminal = Assert-CutoverLogRun `
        -Entries $entries `
        -RunId ([string]$lastPoll.run_id) `
        -Status $ExpectedStatus `
        -ExpectedWorkId $ExpectedWorkId `
        -ExpectedRowId $ExpectedRowId `
        -ExpectedResultStatus $ExpectedResultStatus `
        -ExpectedMode $ExpectedMode `
        -RunWindowStartUtc $ExactStatus.last_run_utc `
        -RunWindowEndUtc $runWindowEndUtc
    $terminal = Assert-CutoverStateTerminal `
        -State $State `
        -ExpectedMode $ExpectedMode `
        -ExpectedStatus $ExpectedStatus `
        -ExpectedWorkId $ExpectedWorkId `
        -ExpectedRowId $ExpectedRowId `
        -ExpectedResultStatus $ExpectedResultStatus `
        -ExpectedUserProfile $Context.user_profile_path
    $resultDigest = ''
    if ($ExpectedStatus -ceq 'result_confirmed') {
        $resultDigest = if ($null -ne $logTerminal.details) { [string]$logTerminal.details.output_sha256 } else { '' }
        if ($resultDigest -cnotmatch '^[0-9a-f]{64}$' -or
            $resultDigest -cne [string]$terminal.output_sha256) {
            Throw-Cutover -Code 'STATE_LOG_RESULT_DIGEST_MISMATCH'
        }
    }
    $pollStartedAtUtc = ConvertTo-CutoverUtcDateTime -Value ([string]$entries[0].at) -Code 'LOG_CURRENT_RUN_INVALID'
    $statePollAtUtc = ConvertTo-CutoverUtcDateTime -Value ([string]$lastPoll.at) -Code 'STATE_LAST_POLL_INVALID'
    if ([Math]::Abs(($statePollAtUtc - $pollStartedAtUtc).TotalSeconds) -gt $script:CutoverClockToleranceSeconds) {
        Throw-Cutover -Code 'STATE_LOG_POLL_TIME_MISMATCH'
    }
    return [pscustomobject][ordered]@{
        run_id = [string]$lastPoll.run_id
        last_run_utc = $ExactStatus.last_run_utc
        log_checkpoint = $LogCheckpoint
        output_sha256 = $resultDigest
    }
}

function Assert-CutoverLogRun {
    param(
        [Parameter(Mandatory = $true)][object[]]$Entries,
        [Parameter(Mandatory = $true)][string]$RunId,
        [Parameter(Mandatory = $true)][string]$Status,
        [AllowEmptyString()][string]$ExpectedWorkId,
        [AllowEmptyString()][string]$ExpectedRowId,
        [AllowEmptyString()][string]$ExpectedResultStatus,
        [AllowEmptyString()][string]$ExpectedErrorCode = '',
        [AllowEmptyString()][string]$ExpectedMode = '',
        [DateTime]$RunWindowStartUtc = [DateTime]::MinValue,
        [DateTime]$RunWindowEndUtc = [DateTime]::MaxValue
    )
    $allowedEvents = @(
        'poll_started', 'board_read_retry', 'row_ignored', 'poll_complete',
        'board_duplicate_rows_collapsed', 'board_schema_incident',
        'tail_seeded', 'overlap_suppressed', 'candidate_observed',
        'duplicate_suppressed', 'result_confirmed', 'run_error'
    )
    $previousAt = [DateTime]::MinValue
    foreach ($entry in $Entries) {
        if ($null -eq $entry -or $entry.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
            Throw-Cutover -Code 'LOG_DELTA_ENTRY_INVALID'
        }
        $properties = @($entry.PSObject.Properties | ForEach-Object { [string]$_.Name })
        $baseProperties = @('at', 'level', 'event', 'run_id', 'work_id', 'row_id', 'code', 'message')
        if ($properties.Count -lt 8 -or $properties.Count -gt 9 -or
            @($baseProperties | Where-Object { $properties -cnotcontains $_ }).Count -ne 0 -or
            @($properties | Where-Object { $baseProperties -cnotcontains $_ -and $_ -cne 'details' }).Count -ne 0 -or
            $entry.at -isnot [string] -or -not (Test-CutoverUtcStamp -Value ([string]$entry.at) ) -or
            $entry.level -isnot [string] -or @('debug', 'info', 'warning', 'error') -cnotcontains [string]$entry.level -or
            $entry.event -isnot [string] -or $allowedEvents -cnotcontains [string]$entry.event -or
            $entry.run_id -isnot [string] -or [string]$entry.run_id -cnotmatch '^[0-9a-f]{32}$' -or
            $entry.work_id -isnot [string] -or $entry.row_id -isnot [string] -or $entry.code -isnot [string] -or
            $entry.message -isnot [string] -or
            [string]$entry.event -notmatch '^[^\x00-\x1f\x7f]{1,100}$' -or
            [string]$entry.work_id -notmatch '^[^\x00-\x1f\x7f]{0,140}$' -or
            [string]$entry.row_id -notmatch '^[^\x00-\x1f\x7f]{0,140}$' -or
            [string]$entry.code -notmatch '^[^\x00-\x1f\x7f]{0,100}$' -or
            [string]$entry.message -notmatch '^[^\x00-\x1f\x7f]{0,500}$' -or
            ($properties -ccontains 'details' -and
             ($null -eq $entry.details -or $entry.details.GetType().FullName -cne 'System.Management.Automation.PSCustomObject'))) {
            Throw-Cutover -Code 'LOG_DELTA_ENTRY_INVALID'
        }
        $entryAt = ConvertTo-CutoverUtcDateTime -Value ([string]$entry.at) -Code 'LOG_DELTA_ENTRY_INVALID'
        if ($entryAt -lt $previousAt -or
            ($RunWindowStartUtc -ne [DateTime]::MinValue -and
             $entryAt -lt $RunWindowStartUtc.AddSeconds(-$script:CutoverClockToleranceSeconds)) -or
            ($RunWindowEndUtc -ne [DateTime]::MaxValue -and
             $entryAt -gt $RunWindowEndUtc.AddSeconds($script:CutoverClockToleranceSeconds))) {
            Throw-Cutover -Code 'LOG_TIMESTAMP_INVALID'
        }
        $previousAt = $entryAt
    }
    $runIds = @($Entries | ForEach-Object { [string]$_.run_id } | Sort-Object -Unique)
    if ($runIds.Count -ne 1 -or $runIds[0] -cne $RunId) { Throw-Cutover -Code 'LOG_RUN_ID_MISMATCH' }
    if (@($Entries | Where-Object { @('tail_seeded', 'overlap_suppressed') -ccontains [string]$_.event }).Count -gt 0) {
        Throw-Cutover -Code 'LOG_FORBIDDEN_TERMINAL_STATUS'
    }
    $pollStarts = @($Entries | Where-Object { [string]$_.event -ceq 'poll_started' })
    if ($pollStarts.Count -ne 1 -or -not [object]::ReferenceEquals($Entries[0], $pollStarts[0])) {
        Throw-Cutover -Code 'LOG_POLL_START_INVALID'
    }
    if (-not [string]::IsNullOrEmpty($ExpectedMode)) {
        $detailsProperty = $pollStarts[0].PSObject.Properties['details']
        if ($null -eq $detailsProperty -or $null -eq $detailsProperty.Value -or
            @($detailsProperty.Value.PSObject.Properties).Count -ne 1 -or
            $null -eq $detailsProperty.Value.PSObject.Properties['mode'] -or
            $detailsProperty.Value.mode -isnot [string] -or
            [string]$detailsProperty.Value.mode -cne $ExpectedMode) {
            Throw-Cutover -Code 'LOG_POLL_MODE_MISMATCH'
        }
    }
    $duplicateDiagnostics = @($Entries | Where-Object { [string]$_.event -ceq 'board_duplicate_rows_collapsed' })
    if ($duplicateDiagnostics.Count -gt 1) { Throw-Cutover -Code 'LOG_DIAGNOSTIC_EVENT_INVALID' }
    if ($duplicateDiagnostics.Count -eq 1) {
        $diagnostic = $duplicateDiagnostics[0]
        $detailsProperty = $diagnostic.PSObject.Properties['details']
        $details = if ($null -eq $detailsProperty) { $null } else { $detailsProperty.Value }
        $detailProperties = @()
        if ($null -ne $details) {
            $detailProperties = @($details.PSObject.Properties | ForEach-Object { [string]$_.Name })
        }
        if ([string]$diagnostic.level -cne 'warning' -or
            [string]$diagnostic.code -cne 'BOARD_CURSOR_DUPLICATES_COLLAPSED' -or
            -not [string]::IsNullOrEmpty([string]$diagnostic.work_id) -or
            -not [string]::IsNullOrEmpty([string]$diagnostic.row_id) -or
            -not [string]::IsNullOrEmpty([string]$diagnostic.message) -or
            $null -eq $details -or $detailProperties.Count -ne 2 -or
            $detailProperties -cnotcontains 'exact_duplicate_group_count' -or
            $detailProperties -cnotcontains 'exact_duplicate_row_count' -or
            $details.exact_duplicate_group_count -isnot [string] -or
            [string]$details.exact_duplicate_group_count -cnotmatch '^[1-9][0-9]{0,9}$' -or
            $details.exact_duplicate_row_count -isnot [string] -or
            [string]$details.exact_duplicate_row_count -cnotmatch '^[1-9][0-9]{0,9}$' -or
            [int64]$details.exact_duplicate_row_count -lt [int64]$details.exact_duplicate_group_count) {
            Throw-Cutover -Code 'LOG_DIAGNOSTIC_EVENT_INVALID'
        }
    }
    $schemaDiagnostics = @($Entries | Where-Object { [string]$_.event -ceq 'board_schema_incident' })
    if ($schemaDiagnostics.Count -gt 1) { Throw-Cutover -Code 'LOG_DIAGNOSTIC_EVENT_INVALID' }
    if ($schemaDiagnostics.Count -eq 1) {
        $diagnostic = $schemaDiagnostics[0]
        $detailsProperty = $diagnostic.PSObject.Properties['details']
        $details = if ($null -eq $detailsProperty) { $null } else { $detailsProperty.Value }
        $detailProperties = @()
        if ($null -ne $details) {
            $detailProperties = @($details.PSObject.Properties | ForEach-Object { [string]$_.Name })
        }
        if ([string]$diagnostic.level -cne 'warning' -or
            [string]$diagnostic.code -cne 'BOARD_KNOWN_TRAILING_ROW_IGNORED' -or
            -not [string]::IsNullOrEmpty([string]$diagnostic.work_id) -or
            -not [string]::IsNullOrEmpty([string]$diagnostic.row_id) -or
            -not [string]::IsNullOrEmpty([string]$diagnostic.message) -or
            $null -eq $details -or $detailProperties.Count -ne 1 -or
            $detailProperties -cnotcontains 'row_count' -or
            $details.row_count -isnot [string] -or [string]$details.row_count -cne '1') {
            Throw-Cutover -Code 'LOG_DIAGNOSTIC_EVENT_INVALID'
        }
    }
    $terminalEntries = @($Entries | Where-Object {
        @('poll_complete', 'tail_seeded', 'overlap_suppressed', 'candidate_observed',
          'duplicate_suppressed', 'result_confirmed', 'run_error') -ccontains [string]$_.event -or
        ([string]$_.event -ceq 'row_ignored' -and [string]$_.code -ceq 'STALE_ORDER')
    })
    if ($terminalEntries.Count -ne 1 -or
        -not [object]::ReferenceEquals($Entries[$Entries.Count - 1], $terminalEntries[0])) {
        Throw-Cutover -Code 'LOG_TERMINAL_CARDINALITY_INVALID'
    }
    $terminal = switch ($Status) {
        'no_eligible_order' {
            @($Entries | Where-Object {
                [string]$_.event -ceq 'poll_complete' -and $null -ne $_.details -and
                @($_.details.PSObject.Properties).Count -eq 2 -and
                $null -ne $_.details.PSObject.Properties['status'] -and
                $null -ne $_.details.PSObject.Properties['malformed'] -and
                $_.details.status -is [string] -and $_.details.malformed -is [string] -and
                [string]$_.details.malformed -cmatch '^(?:0|[1-9][0-9]{0,9})$' -and
                [string]$_.details.status -ceq 'no_eligible_order' -and
                [string]::IsNullOrEmpty([string]$_.work_id) -and
                [string]::IsNullOrEmpty([string]$_.row_id)
            })
        }
        'candidate_observed' {
            @($Entries | Where-Object {
                [string]$_.event -ceq 'candidate_observed' -and
                $null -ne $_.details -and @($_.details.PSObject.Properties).Count -eq 2 -and
                $_.details.mode -is [string] -and [string]$_.details.mode -ceq 'Observe' -and
                $_.details.source -is [string] -and -not [string]::IsNullOrWhiteSpace([string]$_.details.source) -and
                $(if ([string]::IsNullOrEmpty($ExpectedWorkId)) {
                    -not [string]::IsNullOrWhiteSpace([string]$_.work_id)
                } else { [string]$_.work_id -ceq $ExpectedWorkId }) -and
                $(if ([string]::IsNullOrEmpty($ExpectedRowId)) {
                    -not [string]::IsNullOrWhiteSpace([string]$_.row_id)
                } else { [string]$_.row_id -ceq $ExpectedRowId })
            })
        }
        'stale_order_ignored' {
            @($Entries | Where-Object {
                [string]$_.event -ceq 'row_ignored' -and [string]$_.code -ceq 'STALE_ORDER' -and
                $(if ([string]::IsNullOrEmpty($ExpectedWorkId)) {
                    -not [string]::IsNullOrWhiteSpace([string]$_.work_id)
                } else { [string]$_.work_id -ceq $ExpectedWorkId }) -and
                $(if ([string]::IsNullOrEmpty($ExpectedRowId)) {
                    -not [string]::IsNullOrWhiteSpace([string]$_.row_id)
                } else { [string]$_.row_id -ceq $ExpectedRowId })
            })
        }
        'result_confirmed' {
            @($Entries | Where-Object {
                [string]$_.event -ceq 'result_confirmed' -and
                [string]$_.work_id -ceq $ExpectedWorkId -and [string]$_.row_id -ceq $ExpectedRowId -and
                $null -ne $_.details -and @($_.details.PSObject.Properties).Count -eq 2 -and
                $_.details.status -is [string] -and [string]$_.details.status -ceq $ExpectedResultStatus -and
                $_.details.output_sha256 -is [string] -and [string]$_.details.output_sha256 -cmatch '^[0-9a-f]{64}$'
            })
        }
        'error' {
            if ($ExpectedErrorCode -cnotmatch '^[A-Z][A-Z0-9_]{0,95}$') {
                Throw-Cutover -Code 'EXPECTED_ERROR_CODE_INVALID'
            }
            @($Entries | Where-Object {
                [string]$_.event -ceq 'run_error' -and
                [string]$_.level -ceq 'error' -and
                [string]$_.code -ceq $ExpectedErrorCode -and
                -not [string]::IsNullOrEmpty([string]$_.message) -and
                [string]$_.work_id -ceq $ExpectedWorkId -and
                [string]$_.row_id -ceq $ExpectedRowId
            })
        }
        default { Throw-Cutover -Code 'TERMINAL_STATUS_INVALID' }
    }
    if (@($terminal).Count -ne 1 -or -not [object]::ReferenceEquals(@($terminal)[0], $terminalEntries[0])) {
        Throw-Cutover -Code 'LOG_TERMINAL_EVIDENCE_INVALID'
    }
    return $terminalEntries[0]
}

function Assert-CutoverCurrentFailedExecuteRun {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$ExactStatus,
        [Parameter(Mandatory = $true)]$LogCheckpoint,
        [Parameter(Mandatory = $true)][int]$ExpectedTaskResult,
        [Parameter(Mandatory = $true)][string]$ExpectedFailureCode,
        [Parameter(Mandatory = $true)][string]$ExpectedRunId
    )
    if ($ExpectedTaskResult -ne 20) {
        Throw-Cutover -Code 'EXPECTED_CURRENT_TASK_RESULT_INVALID'
    }
    $null = Get-CutoverExpectedFailedExecuteCode -Value $ExpectedFailureCode
    if ($null -eq $ExactStatus.raw -or
        $ExactStatus.raw.state -isnot [string] -or [string]$ExactStatus.raw.state -cne 'Ready' -or
        ($ExactStatus.raw.last_task_result -isnot [int] -and $ExactStatus.raw.last_task_result -isnot [long]) -or
        [int64]$ExactStatus.raw.last_task_result -ne [int64]$ExpectedTaskResult) {
        Throw-Cutover -Code $(if ($null -ne $ExactStatus.raw -and $ExactStatus.raw.state -is [string] -and
            [string]$ExactStatus.raw.state -cne 'Ready') { 'FAILED_EXECUTE_TASK_STATE_MISMATCH' } else { 'FAILED_EXECUTE_TASK_RESULT_MISMATCH' })
    }

    $null = Assert-CutoverStateShape -State $State
    $lastPoll = Get-CutoverLastPoll `
        -State $State `
        -ExpectedMode 'Execute' `
        -ExpectedUserProfile $Context.user_profile_path
    if ([string]$lastPoll.status -cne 'error') {
        Throw-Cutover -Code 'FAILED_EXECUTE_STATE_STATUS_MISMATCH'
    }
    if ($ExpectedRunId -cnotmatch '^[0-9a-f]{32}$') {
        Throw-Cutover -Code 'EXPECTED_CURRENT_RUN_ID_INVALID'
    }
    if ([string]$lastPoll.run_id -cne $ExpectedRunId) {
        Throw-Cutover -Code 'FAILED_EXECUTE_RUN_ID_MISMATCH'
    }
    if ($null -eq $State.error -or
        [string]$State.error.code -cne $ExpectedFailureCode -or
        [string]$State.error.message -cne 'board_header_invalid' -or
        -not [string]::IsNullOrEmpty([string]$State.error.work_id) -or
        -not [string]::IsNullOrEmpty([string]$State.error.row_id) -or
        [int64]$State.counts.errors -lt 1) {
        Throw-Cutover -Code 'FAILED_EXECUTE_ERROR_EVIDENCE_INVALID'
    }

    $entries = @(Get-CutoverTrailingLogRun `
        -Path $Context.log_path `
        -Checkpoint $LogCheckpoint `
        -RunId ([string]$lastPoll.run_id))
    if ($entries.Count -ne 2) {
        Throw-Cutover -Code 'FAILED_EXECUTE_LOG_SHAPE_INVALID'
    }
    $terminal = Assert-CutoverLogRun `
        -Entries $entries `
        -RunId ([string]$lastPoll.run_id) `
        -Status 'error' `
        -ExpectedWorkId '' `
        -ExpectedRowId '' `
        -ExpectedErrorCode $ExpectedFailureCode `
        -ExpectedMode 'Execute' `
        -RunWindowStartUtc $ExactStatus.last_run_utc `
        -RunWindowEndUtc ([DateTime]::UtcNow)
    $pollStarted = $entries[0]
    if ([string]$pollStarted.level -cne 'info' -or
        -not [string]::IsNullOrEmpty([string]$pollStarted.work_id) -or
        -not [string]::IsNullOrEmpty([string]$pollStarted.row_id) -or
        -not [string]::IsNullOrEmpty([string]$pollStarted.code) -or
        -not [string]::IsNullOrEmpty([string]$pollStarted.message)) {
        Throw-Cutover -Code 'FAILED_EXECUTE_POLL_EVIDENCE_INVALID'
    }
    if ([string]$terminal.message -cne [string]$State.error.message) {
        Throw-Cutover -Code 'FAILED_EXECUTE_STATE_LOG_MISMATCH'
    }
    $terminalDetailsProperty = $terminal.PSObject.Properties['details']
    if ($null -eq $terminalDetailsProperty) {
        Throw-Cutover -Code 'BOARD_HEADER_FAILURE_DETAILS_INVALID'
    }
    $null = Assert-CutoverBoardHeaderFailureDetails -Details $terminalDetailsProperty.Value

    $pollStartedAtUtc = ConvertTo-CutoverUtcDateTime -Value ([string]$entries[0].at) -Code 'FAILED_EXECUTE_LOG_TIME_INVALID'
    $statePollAtUtc = ConvertTo-CutoverUtcDateTime -Value ([string]$lastPoll.at) -Code 'FAILED_EXECUTE_STATE_TIME_INVALID'
    $terminalAtUtc = ConvertTo-CutoverUtcDateTime -Value ([string]$terminal.at) -Code 'FAILED_EXECUTE_LOG_TIME_INVALID'
    $stateErrorAtUtc = ConvertTo-CutoverUtcDateTime -Value ([string]$State.error.at) -Code 'FAILED_EXECUTE_STATE_TIME_INVALID'
    if ([Math]::Abs(($statePollAtUtc - $pollStartedAtUtc).TotalSeconds) -gt $script:CutoverClockToleranceSeconds -or
        $stateErrorAtUtc -lt $pollStartedAtUtc.AddSeconds(-$script:CutoverClockToleranceSeconds) -or
        $stateErrorAtUtc -gt $terminalAtUtc.AddSeconds($script:CutoverClockToleranceSeconds) -or
        $terminalAtUtc -gt $stateErrorAtUtc.AddSeconds($script:CutoverClockToleranceSeconds)) {
        Throw-Cutover -Code 'FAILED_EXECUTE_STATE_LOG_TIME_MISMATCH'
    }
    $startupDelaySeconds = ($pollStartedAtUtc - $ExactStatus.last_run_utc).TotalSeconds
    if ($startupDelaySeconds -lt -$script:CutoverClockToleranceSeconds -or
        $startupDelaySeconds -gt $script:CutoverTaskStartupMaximumSeconds) {
        Throw-Cutover -Code 'FAILED_EXECUTE_TASK_TIME_MISMATCH'
    }

    return [pscustomobject][ordered]@{
        run_id = [string]$lastPoll.run_id
        last_run_utc = $ExactStatus.last_run_utc
        poll_started_at_utc = $pollStartedAtUtc
        error_at_utc = $stateErrorAtUtc
        terminal_at_utc = $terminalAtUtc
        failure_code = $ExpectedFailureCode
        task_result = [int64]$ExpectedTaskResult
        log_checkpoint = $LogCheckpoint
    }
}

function Assert-CutoverBoardHeaderFailureDetails {
    param([Parameter(Mandatory = $true)]$Details)
    if ($null -eq $Details -or
        $Details.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
        Throw-Cutover -Code 'BOARD_HEADER_FAILURE_DETAILS_INVALID'
    }
    $expected = @('attempt', 'transport_exit', 'http_status', 'content_type_class', 'elapsed_ms')
    $properties = @($Details.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($properties.Count -ne $expected.Count -or
        @($expected | Where-Object { $properties -cnotcontains $_ }).Count -ne 0 -or
        $Details.attempt -isnot [string] -or [string]$Details.attempt -cne '1' -or
        $Details.transport_exit -isnot [string] -or [string]$Details.transport_exit -cne '0' -or
        $Details.http_status -isnot [string] -or [string]$Details.http_status -cne '200' -or
        $Details.content_type_class -isnot [string] -or [string]$Details.content_type_class -cne 'json' -or
        $Details.elapsed_ms -isnot [string] -or
        [string]$Details.elapsed_ms -cnotmatch '^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?$') {
        Throw-Cutover -Code 'BOARD_HEADER_FAILURE_DETAILS_INVALID'
    }
    $elapsed = 0.0
    if (-not [double]::TryParse(
        [string]$Details.elapsed_ms,
        [Globalization.NumberStyles]::AllowDecimalPoint,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$elapsed
    ) -or $elapsed -le 0.0 -or $elapsed -gt 180000.0) {
        Throw-Cutover -Code 'BOARD_HEADER_FAILURE_DETAILS_INVALID'
    }
    return $Details
}

function Assert-CutoverStateTerminal {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [Parameter(Mandatory = $true)][string]$ExpectedStatus,
        [AllowEmptyString()][string]$ExpectedWorkId,
        [AllowEmptyString()][string]$ExpectedRowId,
        [AllowEmptyString()][string]$ExpectedResultStatus,
        [AllowEmptyString()][string]$ExpectedUserProfile = ''
    )
    $null = Assert-CutoverStateShape -State $State
    $lastPoll = Get-CutoverLastPoll -State $State -ExpectedMode $ExpectedMode -ExpectedUserProfile $ExpectedUserProfile
    if ([string]$lastPoll.status -cne $ExpectedStatus) { Throw-Cutover -Code 'STATE_TERMINAL_STATUS_MISMATCH' }
    $evidenceWorkId = ''
    $evidenceRowId = ''
    $evidenceDigest = ''
    if ($ExpectedStatus -ceq 'result_confirmed') {
        $work = @($State.work | Where-Object {
            [string]$_.input_row_id -ceq $ExpectedRowId -and
            [string]$_.work_id -ceq $ExpectedWorkId -and
            [string]$_.status -ceq 'result_confirmed' -and
            [string]$_.result_status -ceq $ExpectedResultStatus
        })
        if ($work.Count -ne 1 -or $work[0].output_sha256 -isnot [string] -or
            [string]$work[0].output_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            $work[0].updated_at -isnot [string] -or -not (Test-CutoverUtcStamp -Value ([string]$work[0].updated_at)) -or
            $null -eq $State.success -or
            [string]$State.success.event -cne 'result_confirmed' -or
            [string]$State.success.work_id -cne $ExpectedWorkId -or
            [string]$State.success.row_id -cne $ExpectedRowId -or
            [string]$State.cursor.row_id -cne $ExpectedRowId) {
            Throw-Cutover -Code 'STATE_RESULT_IDENTITY_MISMATCH'
        }
        $evidenceWorkId = [string]$work[0].work_id
        $evidenceRowId = [string]$work[0].input_row_id
        $evidenceDigest = [string]$work[0].output_sha256
    }
    if ($ExpectedStatus -ceq 'candidate_observed') {
        if ($null -eq $State.success -or [string]$State.success.event -cne 'candidate_observed' -or
            [string]::IsNullOrWhiteSpace([string]$State.success.work_id) -or
            [string]::IsNullOrWhiteSpace([string]$State.success.row_id) -or
            (-not [string]::IsNullOrEmpty($ExpectedWorkId) -and [string]$State.success.work_id -cne $ExpectedWorkId) -or
            (-not [string]::IsNullOrEmpty($ExpectedRowId) -and [string]$State.success.row_id -cne $ExpectedRowId)) {
            Throw-Cutover -Code 'STATE_CANDIDATE_IDENTITY_INVALID'
        }
        $evidenceWorkId = [string]$State.success.work_id
        $evidenceRowId = [string]$State.success.row_id
    }
    if ($ExpectedStatus -ceq 'no_eligible_order' -and
        ($null -eq $State.success -or [string]$State.success.event -cne 'poll_complete' -or
         -not [string]::IsNullOrEmpty([string]$State.success.work_id) -or
         -not [string]::IsNullOrEmpty([string]$State.success.row_id))) {
        Throw-Cutover -Code 'STATE_NO_ELIGIBLE_EVIDENCE_INVALID'
    }
    return [pscustomobject][ordered]@{
        last_poll = $lastPoll
        work_id = $evidenceWorkId
        row_id = $evidenceRowId
        output_sha256 = $evidenceDigest
    }
}

function Compare-CutoverCursorTuple {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After
    )
    $beforeTimestamp = [string]$Before.timestamp
    $afterTimestamp = [string]$After.timestamp
    if ([string]::IsNullOrEmpty($beforeTimestamp)) {
        return $(if ([string]::IsNullOrEmpty($afterTimestamp)) { 0 } else { 1 })
    }
    if ([string]::IsNullOrEmpty($afterTimestamp)) { return -1 }
    $timestampComparison = [string]::CompareOrdinal($afterTimestamp, $beforeTimestamp)
    if ($timestampComparison -ne 0) { return $timestampComparison }
    return [string]::CompareOrdinal([string]$After.row_id, [string]$Before.row_id)
}

function Assert-CutoverStateTransition {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After,
        [Parameter(Mandatory = $true)][string]$Status,
        [AllowEmptyString()][string]$ExpectedWorkId = '',
        [AllowEmptyString()][string]$ExpectedRowId = '',
        [DateTime]$SchedulerLastRunUtc = [DateTime]::MinValue,
        [DateTime]$RunWindowEndUtc = [DateTime]::MaxValue,
        [DateTime]$PollStartedAtUtc = [DateTime]::MinValue
    )
    $null = Assert-CutoverStateShape -State $Before
    $null = Assert-CutoverStateShape -State $After
    $beforePollAt = ConvertTo-CutoverUtcDateTime -Value ([string]$Before.last_poll.at) -Code 'STATE_LAST_POLL_INVALID'
    $afterPollAt = ConvertTo-CutoverUtcDateTime -Value ([string]$After.last_poll.at) -Code 'STATE_LAST_POLL_INVALID'
    $schedulerLastRun = if ($SchedulerLastRunUtc -eq [DateTime]::MinValue) { [DateTime]::MinValue } else { $SchedulerLastRunUtc.ToUniversalTime() }
    $runWindowEnd = if ($RunWindowEndUtc -eq [DateTime]::MaxValue) { [DateTime]::MaxValue } else { $RunWindowEndUtc.ToUniversalTime() }
    $pollStartedAt = if ($PollStartedAtUtc -eq [DateTime]::MinValue) { [DateTime]::MinValue } else { $PollStartedAtUtc.ToUniversalTime() }
    if ($afterPollAt -le $beforePollAt -or
        ($schedulerLastRun -ne [DateTime]::MinValue -and
         $afterPollAt -lt $schedulerLastRun.AddSeconds(-$script:CutoverClockToleranceSeconds))) {
        Throw-Cutover -Code 'STATE_LAST_POLL_TIME_NOT_ADVANCED'
    }
    if (($runWindowEnd -ne [DateTime]::MaxValue -and
         $afterPollAt -gt $runWindowEnd.AddSeconds($script:CutoverClockToleranceSeconds)) -or
        ($pollStartedAt -ne [DateTime]::MinValue -and
         [Math]::Abs(($afterPollAt - $pollStartedAt).TotalSeconds) -gt $script:CutoverClockToleranceSeconds)) {
        Throw-Cutover -Code 'STATE_LAST_POLL_TIME_INVALID'
    }
    $countNames = @('polls', 'seen', 'selected', 'succeeded', 'errors', 'ignored')
    foreach ($name in $countNames) {
        if ([int64]$After.counts.PSObject.Properties[$name].Value -lt
            [int64]$Before.counts.PSObject.Properties[$name].Value) {
            Throw-Cutover -Code 'STATE_COUNT_ROLLBACK'
        }
    }
    if ([int64]$After.counts.polls -ne ([int64]$Before.counts.polls + 1)) {
        Throw-Cutover -Code 'STATE_POLL_COUNT_DELTA_INVALID'
    }
    $selectedDelta = if (@('candidate_observed', 'stale_order_ignored', 'result_confirmed') -ccontains $Status) { 1 } else { 0 }
    if ([int64]$After.counts.selected -ne ([int64]$Before.counts.selected + $selectedDelta)) {
        Throw-Cutover -Code 'STATE_SELECTED_COUNT_DELTA_INVALID'
    }
    $succeededDelta = if ($Status -ceq 'result_confirmed') { 1 } else { 0 }
    if ([int64]$After.counts.succeeded -ne ([int64]$Before.counts.succeeded + $succeededDelta) -or
        [int64]$After.counts.errors -ne [int64]$Before.counts.errors) {
        Throw-Cutover -Code 'STATE_RESULT_COUNT_DELTA_INVALID'
    }
    if ($Status -ceq 'stale_order_ignored' -and
        [int64]$After.counts.ignored -lt ([int64]$Before.counts.ignored + 1)) {
        Throw-Cutover -Code 'STATE_IGNORED_COUNT_DELTA_INVALID'
    }

    $cursorComparison = Compare-CutoverCursorTuple -Before $Before.cursor -After $After.cursor
    if ($cursorComparison -lt 0) { Throw-Cutover -Code 'STATE_CURSOR_ROLLBACK' }
    if ($Status -ceq 'candidate_observed' -and $cursorComparison -ne 0) {
        Throw-Cutover -Code 'STATE_CANDIDATE_CURSOR_CHANGED'
    }
    if ($Status -ceq 'stale_order_ignored' -and $cursorComparison -le 0) {
        Throw-Cutover -Code 'STATE_STALE_CURSOR_NOT_ADVANCED'
    }
    if ($Status -ceq 'result_confirmed' -and [string]$After.cursor.row_id -cne $ExpectedRowId) {
        Throw-Cutover -Code 'STATE_RESULT_CURSOR_MISMATCH'
    }

    $beforeWork = @($Before.work)
    $afterWork = @($After.work)
    if ($Status -cne 'result_confirmed') {
        if (($beforeWork | ConvertTo-Json -Depth 5 -Compress) -cne ($afterWork | ConvertTo-Json -Depth 5 -Compress)) {
            Throw-Cutover -Code 'STATE_WORK_HISTORY_CHANGED'
        }
        return
    }
    if (@($beforeWork | Where-Object {
        [string]$_.input_row_id -ceq $ExpectedRowId -and [string]$_.work_id -ceq $ExpectedWorkId
    }).Count -ne 0) {
        Throw-Cutover -Code 'STATE_RESULT_WORK_NOT_FRESH'
    }
    $expectedAfterCount = [Math]::Min(500, $beforeWork.Count + 1)
    if ($afterWork.Count -ne $expectedAfterCount) { Throw-Cutover -Code 'STATE_WORK_HISTORY_DELTA_INVALID' }
    $offset = if ($beforeWork.Count -eq 500) { 1 } else { 0 }
    for ($index = $offset; $index -lt $beforeWork.Count; $index++) {
        $afterIndex = $index - $offset
        if (($beforeWork[$index] | ConvertTo-Json -Depth 5 -Compress) -cne
            ($afterWork[$afterIndex] | ConvertTo-Json -Depth 5 -Compress)) {
            Throw-Cutover -Code 'STATE_WORK_HISTORY_CHANGED'
        }
    }
    $newWork = $afterWork[$afterWork.Count - 1]
    if ([string]$newWork.input_row_id -cne $ExpectedRowId -or
        [string]$newWork.work_id -cne $ExpectedWorkId -or
        [string]$newWork.status -cne 'result_confirmed') {
        Throw-Cutover -Code 'STATE_WORK_HISTORY_DELTA_INVALID'
    }
}

function Wait-CutoverTaskRun {
    param(
        [Parameter(Mandatory = $true)][DateTime]$PreviousLastRunUtc,
        [Parameter(Mandatory = $true)][DateTime]$DeadlineUtc,
        [Parameter(Mandatory = $true)][int]$PollMilliseconds
    )
    do {
        $runtime = Get-CutoverTaskRuntime
        if (@('Ready', 'Running', 'Queued') -cnotcontains $runtime.state) {
            Throw-Cutover -Code 'TASK_RUNTIME_STATE_INVALID'
        }
        if ($runtime.state -ceq 'Ready' -and $runtime.last_run_utc -gt $PreviousLastRunUtc) {
            if ($runtime.last_task_result -ne 0) { Throw-Cutover -Code 'TASK_RESULT_NONZERO' }
            return $runtime
        }
        if ([DateTime]::UtcNow -ge $DeadlineUtc) { Throw-Cutover -Code 'TASK_WAIT_TIMEOUT' }
        Start-Sleep -Milliseconds $PollMilliseconds
    } while ($true)
}

function Invoke-CutoverOneRun {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Installer,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [Parameter(Mandatory = $true)][DateTime]$DeadlineUtc,
        [Parameter(Mandatory = $true)][DateTime]$PreviousLastRunUtc,
        [Parameter(Mandatory = $true)][string]$PreviousRunId,
        [Parameter(Mandatory = $true)]$LogBefore,
        [AllowEmptyString()][string]$ExpectedStatus = '',
        [AllowEmptyString()][string]$ExpectedWorkId = '',
        [AllowEmptyString()][string]$ExpectedRowId = '',
        [AllowEmptyString()][string]$ExpectedResultStatus = '',
        $AdmittedBaseline = $null
    )
    $beforeStateResult = Get-CutoverState -Path $Context.state_path
    $beforeState = $beforeStateResult.value
    $beforeLastPoll = Get-CutoverLastPoll `
        -State $beforeState `
        -ExpectedMode ([string]$beforeState.mode) `
        -ExpectedUserProfile $Context.user_profile_path
    if ([string]$beforeLastPoll.run_id -cne $PreviousRunId) {
        Throw-Cutover -Code 'STATE_BASELINE_RUN_ID_MISMATCH'
    }
    if ($null -ne $AdmittedBaseline) {
        $admittedProtected = $AdmittedBaseline.PSObject.Properties['protected_fingerprint']
        $admittedDefinition = $AdmittedBaseline.PSObject.Properties['observe_definition_sha256']
        if ($ExpectedMode -cne 'Observe' -or $PreviousRunId -cne [string]$AdmittedBaseline.run_id -or
            $PreviousLastRunUtc.Ticks -ne $AdmittedBaseline.last_run_utc.Ticks -or
            $null -eq $admittedProtected -or $admittedProtected.Value -isnot [string] -or
            [string]::IsNullOrEmpty($admittedProtected.Value) -or
            $null -eq $admittedDefinition -or $admittedDefinition.Value -isnot [string] -or
            $admittedDefinition.Value -cnotmatch '^[0-9a-f]{64}$') {
            Throw-Cutover -Code 'OBSERVE_FIRST_START_ADMISSION_INVALID'
        }
        # The drain's native Ready read may be slow. Recheck original hashes
        # HERE, after that read and this state read, immediately before start.
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $admittedProtected.Value) { Throw-Cutover -Code 'PROTECTED_CHANGED_BEFORE_OBSERVE_FIRST_START' }
        $firstStartReady = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Installer -ExpectedMode 'Observe' -ExpectedTaskState 'Ready'
        if ($firstStartReady.last_run_utc.Ticks -ne $AdmittedBaseline.last_run_utc.Ticks) { Throw-Cutover -Code 'OBSERVE_FIRST_START_TASK_CHANGED' }
        $firstStartXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $firstStartXml.enabled -or $firstStartXml.normalized_sha256 -cne $admittedDefinition.Value) { Throw-Cutover -Code 'TASK_XML_CHANGED_BEFORE_OBSERVE_FIRST_START' }
        # The native status and XML reads can outlast the earlier fingerprint.
        # Recheck it here, then authenticate state/log after this slow tree read.
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $admittedProtected.Value) { Throw-Cutover -Code 'PROTECTED_CHANGED_BEFORE_OBSERVE_FIRST_START' }
        Assert-CutoverFileCheckpointUnchanged -Before $AdmittedBaseline.state_checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_BEFORE_OBSERVE_FIRST_START'
        Assert-CutoverFileCheckpointUnchanged -Before $AdmittedBaseline.log_checkpoint -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_BEFORE_OBSERVE_FIRST_START'
        if ([DateTime]::UtcNow -ge $DeadlineUtc) { Throw-Cutover -Code 'OBSERVE_FIRST_START_DEADLINE_EXCEEDED' }
        Assert-CutoverTriggerWindow -NextRunUtc $AdmittedBaseline.next_run_utc -RequiredSeconds ([Math]::Ceiling(($DeadlineUtc - [DateTime]::UtcNow).TotalSeconds) + $Context.natural_trigger_margin_seconds)
        Assert-CutoverTriggerWindow -NextRunUtc $firstStartReady.next_run_utc -RequiredSeconds ([Math]::Ceiling(($DeadlineUtc - [DateTime]::UtcNow).TotalSeconds) + $Context.natural_trigger_margin_seconds)
        # Window checks can be preempted; admit no start after the total deadline.
        if ([DateTime]::UtcNow -ge $DeadlineUtc) { Throw-Cutover -Code 'OBSERVE_FIRST_START_DEADLINE_EXCEEDED' }
    }
    $runWindowStartUtc = [DateTime]::UtcNow
    Start-CutoverTask
    $runtime = Wait-CutoverTaskRun -PreviousLastRunUtc $PreviousLastRunUtc -DeadlineUtc $DeadlineUtc -PollMilliseconds $Context.poll_milliseconds
    $exactStatus = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Installer -ExpectedMode $ExpectedMode -ExpectedTaskState 'Ready'
    if ($exactStatus.last_run_utc -le $PreviousLastRunUtc -or
        $exactStatus.last_run_utc.Ticks -ne $runtime.last_run_utc.Ticks) {
        Throw-Cutover -Code 'TASK_LAST_RUN_NOT_ADVANCED'
    }
    $stateResult = Get-CutoverState -Path $Context.state_path
    $state = $stateResult.value
    $lastPoll = Get-CutoverLastPoll -State $state -ExpectedMode $ExpectedMode
    if ([string]$lastPoll.run_id -ceq $PreviousRunId) { Throw-Cutover -Code 'STATE_RUN_ID_NOT_ADVANCED' }
    $status = [string]$lastPoll.status
    if (-not [string]::IsNullOrEmpty($ExpectedStatus) -and $status -cne $ExpectedStatus) {
        Throw-Cutover -Code 'STATE_TERMINAL_STATUS_MISMATCH'
    }
    if (@('tail_seeded', 'overlap_suppressed') -ccontains $status) {
        Throw-Cutover -Code 'FORBIDDEN_TERMINAL_STATUS'
    }
    $stateTerminal = Assert-CutoverStateTerminal `
        -State $state `
        -ExpectedMode $ExpectedMode `
        -ExpectedStatus $status `
        -ExpectedWorkId $ExpectedWorkId `
        -ExpectedRowId $ExpectedRowId `
        -ExpectedResultStatus $ExpectedResultStatus `
        -ExpectedUserProfile $Context.user_profile_path
    $logDelta = Get-CutoverLogDelta -Path $Context.log_path -Before $LogBefore
    $runWindowEndUtc = [DateTime]::UtcNow
    $logTerminal = Assert-CutoverLogRun `
        -Entries @($logDelta.entries) `
        -RunId ([string]$lastPoll.run_id) `
        -Status $status `
        -ExpectedWorkId $ExpectedWorkId `
        -ExpectedRowId $ExpectedRowId `
        -ExpectedResultStatus $ExpectedResultStatus `
        -ExpectedMode $ExpectedMode `
        -RunWindowStartUtc $runWindowStartUtc `
        -RunWindowEndUtc $runWindowEndUtc
    $pollStartedAtUtc = ConvertTo-CutoverUtcDateTime `
        -Value ([string]$logDelta.entries[0].at) `
        -Code 'LOG_DELTA_ENTRY_INVALID'
    Assert-CutoverStateTransition `
        -Before $beforeState `
        -After $state `
        -Status $status `
        -ExpectedWorkId $ExpectedWorkId `
        -ExpectedRowId $ExpectedRowId `
        -SchedulerLastRunUtc $exactStatus.last_run_utc `
        -RunWindowEndUtc $runWindowEndUtc `
        -PollStartedAtUtc $pollStartedAtUtc
    if ($status -ceq 'candidate_observed' -and
        ([string]$logTerminal.work_id -cne [string]$stateTerminal.work_id -or
         [string]$logTerminal.row_id -cne [string]$stateTerminal.row_id)) {
        Throw-Cutover -Code 'STATE_LOG_CANDIDATE_IDENTITY_MISMATCH'
    }
    if ($status -ceq 'result_confirmed') {
        $logDigest = if ($null -ne $logTerminal.details) { [string]$logTerminal.details.output_sha256 } else { '' }
        if ($logDigest -cnotmatch '^[0-9a-f]{64}$' -or
            $logDigest -cne [string]$stateTerminal.output_sha256) {
            Throw-Cutover -Code 'STATE_LOG_RESULT_DIGEST_MISMATCH'
        }
    }
    return [pscustomobject][ordered]@{
        status = $status
        run_id = [string]$lastPoll.run_id
        last_run_utc = $exactStatus.last_run_utc
        log_checkpoint = $logDelta.checkpoint
        log_appended_bytes = [long]$logDelta.appended_bytes
        state = $state
    }
}

function Get-CutoverFileFingerprint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateRange(1, 536870912)][long]$MaximumBytes = $script:CutoverProtectedFileMaximumBytes
    )
    Assert-CutoverPathChainSafe -Path $Path -Code 'PROTECTED_PATH_UNSAFE'
    if (-not (Test-Path -LiteralPath $Path)) { return 'MISSING' }
    Assert-CutoverSafeFile -Path $Path -MissingCode 'PROTECTED_PATH_MISSING' -UnsafeCode 'PROTECTED_PATH_UNSAFE'
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch { Throw-Cutover -Code 'PROTECTED_PATH_READ_FAILED' }
    if ($item.Length -lt 0 -or $item.Length -gt $MaximumBytes) {
        Throw-Cutover -Code 'PROTECTED_FILE_TOO_LARGE'
    }
    return Get-CutoverTextSha256 -Text ('F|{0}|{1}|{2}' -f $item.Length, $item.LastWriteTimeUtc.Ticks, (Get-CutoverFileSha256 -Path $Path))
}

function Get-CutoverTreeFingerprint {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-CutoverPathChainSafe -Path $Path -Code 'PROTECTED_PATH_UNSAFE'
    if (-not (Test-Path -LiteralPath $Path)) { return 'MISSING' }
    Assert-CutoverSafeDirectory -Path $Path -MissingCode 'PROTECTED_PATH_MISSING' -UnsafeCode 'PROTECTED_PATH_UNSAFE'
    $root = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $records = New-Object 'System.Collections.Generic.List[string]'
    $totalBytes = [long]0
    try { $rootItem = Get-Item -LiteralPath $root -Force -ErrorAction Stop }
    catch { Throw-Cutover -Code 'PROTECTED_TREE_READ_FAILED' }
    $records.Add(('R|{0}' -f $rootItem.LastWriteTimeUtc.Ticks))
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($root)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        try { $entries = @([IO.Directory]::EnumerateFileSystemEntries($directory)) }
        catch { Throw-Cutover -Code 'PROTECTED_TREE_READ_FAILED' }
        foreach ($entryPath in $entries) {
            if ($records.Count -ge $script:CutoverTreeMaximumEntries) { Throw-Cutover -Code 'PROTECTED_TREE_TOO_LARGE' }
            try { $entry = Get-Item -LiteralPath $entryPath -Force -ErrorAction Stop }
            catch { Throw-Cutover -Code 'PROTECTED_TREE_READ_FAILED' }
            if (Test-CutoverReparsePoint -Item $entry) { Throw-Cutover -Code 'PROTECTED_PATH_UNSAFE' }
            $relative = $entry.FullName.Substring($root.Length)
            if ($relative.Length -gt 2048) { Throw-Cutover -Code 'PROTECTED_TREE_PATH_TOO_LONG' }
            if ($entry.PSIsContainer) {
                $records.Add(('D|{0}|{1}' -f $relative, $entry.LastWriteTimeUtc.Ticks))
                $pending.Push($entry.FullName)
            } else {
                if ($entry.Length -lt 0 -or $entry.Length -gt $script:CutoverProtectedFileMaximumBytes) {
                    Throw-Cutover -Code 'PROTECTED_FILE_TOO_LARGE'
                }
                $totalBytes += [long]$entry.Length
                if ($totalBytes -gt $script:CutoverProtectedTreeMaximumBytes) {
                    Throw-Cutover -Code 'PROTECTED_TREE_TOO_LARGE'
                }
                $records.Add(('F|{0}|{1}|{2}|{3}' -f $relative, $entry.Length, $entry.LastWriteTimeUtc.Ticks, (Get-CutoverFileSha256 -Path $entry.FullName)))
            }
        }
    }
    return Get-CutoverTextSha256 -Text (@($records.ToArray() | Sort-Object) -join "`n")
}

function Invoke-CutoverGitRead {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][int[]]$AllowedExitCodes
    )
    $result = Invoke-CutoverProcess -FilePath $Context.git_path -Arguments $Arguments -TimeoutSeconds 30 -FailureCode 'GIT_READ_FAILED'
    if ($AllowedExitCodes -cnotcontains $result.exit_code -or -not [string]::IsNullOrWhiteSpace($result.stderr) -or
        $result.stdout.Length -gt 1048576) {
        Throw-Cutover -Code 'GIT_READ_FAILED'
    }
    return $result
}

function Get-CutoverGitSnapshot {
    param([Parameter(Mandatory = $true)]$Context)
    $head = Invoke-CutoverGitRead -Context $Context -Arguments @('-C', $Context.workspace_path, 'rev-parse', '--verify', 'HEAD') -AllowedExitCodes @(0)
    $branch = Invoke-CutoverGitRead -Context $Context -Arguments @('-C', $Context.workspace_path, 'symbolic-ref', '--quiet', 'HEAD') -AllowedExitCodes @(0)
    $status = Invoke-CutoverGitRead -Context $Context -Arguments @('-C', $Context.workspace_path, 'status', '--porcelain=v1', '--untracked-files=all') -AllowedExitCodes @(0)
    $headText = $head.stdout.Trim()
    $branchText = $branch.stdout.Trim()
    if ($headText -cnotmatch '^[0-9a-f]{40}$' -or $branchText -cnotmatch '^refs/heads/[A-Za-z0-9._/-]{1,240}$') {
        Throw-Cutover -Code 'GIT_IDENTITY_INVALID'
    }
    if (-not [string]::IsNullOrEmpty($status.stdout)) { Throw-Cutover -Code 'GIT_WORKSPACE_NOT_CLEAN' }
    return Get-CutoverTextSha256 -Text (($headText, $branchText, (Get-CutoverTextSha256 -Text '')) -join '|')
}

function Assert-CutoverNoIsolationResidue {
    param([Parameter(Mandatory = $true)][string]$MetadataRoot)
    Assert-CutoverSafeDirectory -Path $MetadataRoot -MissingCode 'METADATA_ROOT_MISSING' -UnsafeCode 'METADATA_ROOT_UNSAFE'
    try { $entries = @(Get-ChildItem -LiteralPath $MetadataRoot -Force -ErrorAction Stop) }
    catch { Throw-Cutover -Code 'ISOLATION_RESIDUE_CHECK_FAILED' }
    $residue = @($entries | Where-Object {
        $_.Name -cmatch '^(run-|claude-config-|claude-settings-)'
    })
    if ($residue.Count -ne 0) { Throw-Cutover -Code 'ISOLATION_RESIDUE_PRESENT' }
}

function Get-CutoverProtectedSnapshot {
    param([Parameter(Mandatory = $true)]$Context)
    Assert-CutoverNoIsolationResidue -MetadataRoot $Context.metadata_root
    $releasePath = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $Context.installer_path)))
    $snapshot = [ordered]@{
        git_identity = (Get-CutoverGitSnapshot -Context $Context)
        git_hooks = (Get-CutoverTreeFingerprint -Path (Join-Path $Context.workspace_path '.git\hooks'))
        env_file = (Get-CutoverFileFingerprint -Path $Context.env_file)
        release_tree = (Get-CutoverTreeFingerprint -Path $releasePath)
        # These two files were independently SHA-256 pinned before this snapshot.
        # Claude Code 2.1.241 is 337745056 bytes; retain the smaller limit for
        # unpinned configuration files and every protected tree entry.
        claude_file = (Get-CutoverFileFingerprint -Path $Context.claude_command -MaximumBytes $script:CutoverPinnedExecutableMaximumBytes)
        git_file = (Get-CutoverFileFingerprint -Path $Context.git_path -MaximumBytes $script:CutoverPinnedExecutableMaximumBytes)
        profile_claude = (Get-CutoverTreeFingerprint -Path (Join-Path $Context.user_profile_path '.claude'))
        profile_claude_json = (Get-CutoverFileFingerprint -Path (Join-Path $Context.user_profile_path '.claude.json'))
        workspace_claude = (Get-CutoverTreeFingerprint -Path (Join-Path $Context.workspace_path '.claude'))
    }
    return Get-CutoverTextSha256 -Text ($snapshot | ConvertTo-Json -Compress)
}

function Assert-CutoverPinnedExecutables {
    param([Parameter(Mandatory = $true)]$Context)
    $claude = Resolve-CutoverAbsolutePath -Value $Context.claude_command -Code 'CLAUDE_PATH_INVALID' -ForbidVolumeRoot
    $git = Resolve-CutoverAbsolutePath -Value $Context.git_path -Code 'GIT_PATH_INVALID' -ForbidVolumeRoot
    if ($Context.expected_claude_sha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Cutover -Code 'CLAUDE_SHA256_INVALID' }
    if ($Context.expected_git_sha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Cutover -Code 'GIT_SHA256_INVALID' }
    Assert-CutoverSafeFile -Path $claude -MissingCode 'CLAUDE_MISSING' -UnsafeCode 'CLAUDE_UNSAFE'
    Assert-CutoverSafeFile -Path $git -MissingCode 'GIT_MISSING' -UnsafeCode 'GIT_UNSAFE'
    if ((Get-CutoverFileSha256 -Path $claude) -cne $Context.expected_claude_sha256) { Throw-Cutover -Code 'CLAUDE_SHA256_MISMATCH' }
    if ((Get-CutoverFileSha256 -Path $git) -cne $Context.expected_git_sha256) { Throw-Cutover -Code 'GIT_SHA256_MISMATCH' }
    $Context.claude_command = $claude
    $Context.git_path = $git
}

function Assert-CutoverBackupMatchesExpectedXml {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$ExpectedUtf8TextSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedUtf16LeBomSha256
    )
    $manifestPath = Join-Path $Context.metadata_root 'previous-task.json'
    $xmlPath = Join-Path $Context.metadata_root 'previous-task.xml'
    $manifestCheckpoint = Get-CutoverFileCheckpoint -Path $manifestPath -MaximumBytes 65536 -MissingCode 'BACKUP_MANIFEST_MISSING' -InvalidCode 'BACKUP_MANIFEST_INVALID'
    if ($manifestCheckpoint.length -le 0) { Throw-Cutover -Code 'BACKUP_MANIFEST_INVALID' }
    try {
        [byte[]]$manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
        $strict = New-Object Text.UTF8Encoding($false, $true)
        $manifestText = $strict.GetString($manifestBytes)
        Assert-CutoverNoDuplicateJsonKeys -Bytes $manifestBytes -Code 'BACKUP_MANIFEST_INVALID'
        $manifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
    } catch { Throw-Cutover -Code 'BACKUP_MANIFEST_INVALID' }
    if ($manifest.schema -isnot [string] -or [string]$manifest.schema -cne 'order_supervisor_task_backup.v1' -or
        $manifest.previous_existed -isnot [bool] -or -not [bool]$manifest.previous_existed -or
        $manifest.xml_sha256 -isnot [string] -or [string]$manifest.xml_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        Throw-Cutover -Code 'BACKUP_MANIFEST_INVALID'
    }
    $xmlCheckpoint = Get-CutoverFileCheckpoint -Path $xmlPath -MaximumBytes 1048576 -MissingCode 'BACKUP_XML_MISSING' -InvalidCode 'BACKUP_XML_INVALID'
    try { [byte[]]$xmlBytes = [IO.File]::ReadAllBytes($xmlPath) }
    catch { Throw-Cutover -Code 'BACKUP_XML_INVALID' }
    if ($xmlCheckpoint.length -lt 2 -or $xmlBytes[0] -ne 0xff -or $xmlBytes[1] -ne 0xfe) {
        Throw-Cutover -Code 'BACKUP_XML_ENCODING_INVALID'
    }
    $strictUnicode = New-Object Text.UnicodeEncoding($false, $true, $true)
    try { $xmlText = $strictUnicode.GetString($xmlBytes, 2, $xmlBytes.Length - 2) }
    catch { Throw-Cutover -Code 'BACKUP_XML_ENCODING_INVALID' }
    $actualUtf8TextSha256 = Get-CutoverTextSha256 -Text $xmlText
    if ($actualUtf8TextSha256 -cne [string]$manifest.xml_sha256) {
        Throw-Cutover -Code 'BACKUP_MANIFEST_TEXT_DIGEST_MISMATCH'
    }
    if ($actualUtf8TextSha256 -cne $ExpectedUtf8TextSha256 -or
        (Get-CutoverUnicodeTextSha256 -Text $xmlText) -cne $ExpectedUtf16LeBomSha256 -or
        $xmlCheckpoint.sha256 -cne $ExpectedUtf16LeBomSha256) {
        Throw-Cutover -Code 'BACKUP_EXPECTED_XML_MISMATCH'
    }
}

function Invoke-CutoverDrainObserve {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Installer,
        # Applies to the two reads BEFORE the first bounded run only.  Every
        # read after Invoke-CutoverOneRun describes a task the candidate has
        # actually run, so those keep requiring 0 and are untouched here.
        [bool]$AllowInheritedTaskResult = $false,
        # Incident recovery must not adopt an intervening run as its baseline.
        # Ordinary drains retain their original admission contract.
        $AdmittedBaseline = $null
    )
    $initialStatus = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Installer -ExpectedMode 'Observe' -ExpectedTaskState 'Ready' -AllowInheritedTaskResult $AllowInheritedTaskResult
    try {
        $stateResult = Get-CutoverState -Path $Context.state_path
        $baselineState = $stateResult.value
        if ($baselineState.mode -isnot [string] -or @('Observe', 'Execute') -cnotcontains [string]$baselineState.mode) {
            Throw-Cutover -Code 'STATE_BASELINE_MODE_INVALID'
        }
        $baselineLastPoll = Get-CutoverLastPoll `
            -State $baselineState `
            -ExpectedMode ([string]$baselineState.mode) `
            -ExpectedUserProfile $Context.user_profile_path
        $previousRunId = [string]$baselineLastPoll.run_id
        $previousLastRun = $initialStatus.last_run_utc
        $firstLastRun = $previousLastRun
        $logCheckpoint = Get-CutoverLogCheckpoint -Path $Context.log_path
        if ($null -ne $AdmittedBaseline) {
            if ($initialStatus.last_run_utc.Ticks -ne $AdmittedBaseline.last_run_utc.Ticks -or
                $previousRunId -cne [string]$AdmittedBaseline.run_id) {
                Throw-Cutover -Code 'OBSERVE_DRAIN_ADMITTED_RUN_CHANGED'
            }
            $admittedStatusProperty = $AdmittedBaseline.PSObject.Properties['baseline_status']
            $admittedStatus = if ($null -eq $admittedStatusProperty -or [string]::IsNullOrEmpty([string]$admittedStatusProperty.Value)) { 'no_eligible_order' } else { [string]$admittedStatusProperty.Value }
            $admittedWorkId = if ($null -eq $AdmittedBaseline.PSObject.Properties['work_id']) { '' } else { [string]$AdmittedBaseline.work_id }
            $admittedRowId = if ($null -eq $AdmittedBaseline.PSObject.Properties['row_id']) { '' } else { [string]$AdmittedBaseline.row_id }
            $admittedResultStatus = if ($null -eq $AdmittedBaseline.PSObject.Properties['result_status']) { '' } else { [string]$AdmittedBaseline.result_status }
            $admittedOutputSha256 = if ($null -eq $AdmittedBaseline.PSObject.Properties['output_sha256']) { '' } else { [string]$AdmittedBaseline.output_sha256 }
            $admittedStateTerminal = Assert-CutoverStateTerminal `
                -State $baselineState `
                -ExpectedMode ([string]$baselineState.mode) `
                -ExpectedStatus $admittedStatus `
                -ExpectedWorkId $admittedWorkId `
                -ExpectedRowId $admittedRowId `
                -ExpectedResultStatus $admittedResultStatus `
                -ExpectedUserProfile $Context.user_profile_path
            if ($admittedStatus -ceq 'result_confirmed' -and
                ($admittedOutputSha256 -cnotmatch '^[0-9a-f]{64}$' -or
                 [string]$admittedStateTerminal.output_sha256 -cne $admittedOutputSha256)) {
                Throw-Cutover -Code 'OBSERVE_DRAIN_ADMITTED_RESULT_DIGEST_CHANGED'
            }
            if ($stateResult.checkpoint.length -ne $AdmittedBaseline.state_checkpoint.length -or
                $stateResult.checkpoint.sha256 -cne $AdmittedBaseline.state_checkpoint.sha256) {
                Throw-Cutover -Code 'OBSERVE_DRAIN_ADMITTED_STATE_CHANGED'
            }
            if ($logCheckpoint.length -ne $AdmittedBaseline.log_checkpoint.length -or
                $logCheckpoint.sha256 -cne $AdmittedBaseline.log_checkpoint.sha256) {
                Throw-Cutover -Code 'OBSERVE_DRAIN_ADMITTED_LOG_CHANGED'
            }
        }
        $preStart = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Installer -ExpectedMode 'Observe' -ExpectedTaskState 'Ready' -AllowInheritedTaskResult $AllowInheritedTaskResult
        Assert-CutoverStableReadyReadback `
            -Initial $initialStatus `
            -Readback $preStart `
            -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds) `
            -DriftCode 'TASK_CHANGED_BEFORE_START'
        $previousLastRun = $preStart.last_run_utc
        $firstLastRun = $previousLastRun
        $deadline = [DateTime]::UtcNow.AddSeconds($Context.timeout_seconds)
        $firstStartAdmission = $null
        if ($null -ne $AdmittedBaseline) {
            $firstStartAdmission = [pscustomobject]@{run_id=$AdmittedBaseline.run_id;last_run_utc=$AdmittedBaseline.last_run_utc;state_checkpoint=$AdmittedBaseline.state_checkpoint;log_checkpoint=$AdmittedBaseline.log_checkpoint;protected_fingerprint=$AdmittedBaseline.protected_fingerprint;observe_definition_sha256=$AdmittedBaseline.observe_definition_sha256;next_run_utc=$preStart.next_run_utc}
        }
        $runs = 0
        $appendedBytes = [long]0
        while ($runs -lt $Context.max_runs -and [DateTime]::UtcNow -lt $deadline) {
            $run = Invoke-CutoverOneRun `
                -Context $Context `
                -Installer $Installer `
                -ExpectedMode 'Observe' `
                -DeadlineUtc $deadline `
                -PreviousLastRunUtc $previousLastRun `
                -PreviousRunId $previousRunId `
                -LogBefore $logCheckpoint `
                -AdmittedBaseline $(if ($runs -eq 0) { $firstStartAdmission } else { $null })
            $runs++
            $previousLastRun = $run.last_run_utc
            $previousRunId = $run.run_id
            $logCheckpoint = $run.log_checkpoint
            $appendedBytes += $run.log_appended_bytes
            if ($run.status -ceq 'no_eligible_order') {
                return [pscustomobject][ordered]@{
                    status = 'OBSERVE_DRAINED'
                    runs = $runs
                    final_run_id = $run.run_id
                    final_worker_status = $run.status
                    first_last_run_utc = $firstLastRun.ToString('o')
                    final_last_run_utc = $run.last_run_utc.ToString('o')
                    log_appended_bytes = $appendedBytes
                }
            }
            if ($run.status -ceq 'candidate_observed') {
                Throw-Cutover -Code 'OBSERVE_CANDIDATE_REQUIRES_EXTERNAL_RESOLUTION'
            }
            if ($run.status -cne 'stale_order_ignored') {
                Throw-Cutover -Code 'OBSERVE_INTERMEDIATE_STATUS_INVALID'
            }
        }
        Throw-Cutover -Code 'OBSERVE_DRAIN_BOUND_EXCEEDED'
    } catch {
        Throw-CutoverAfterCleanup `
            -FailureRecord $_ `
            -Context $Context `
            -Installer $Installer `
            -ExpectedModes @('Observe')
    }
}

function Invoke-CutoverValidateEscrowAndDisable {
    param([Parameter(Mandatory = $true)]$Context)
    $escrow = Invoke-CutoverEscrow -Context $Context -RequestedAction 'Validate'
    $status = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode $Context.mode -ExpectedTaskState 'Ready'
    try {
        $stateBefore = (Get-CutoverState -Path $Context.state_path).checkpoint
        $logBefore = Get-CutoverLogCheckpoint -Path $Context.log_path
        $xmlBefore = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $xmlBefore.enabled -or $xmlBefore.utf16le_bom_sha256 -cne [string]$escrow.task_xml_sha256) {
            Throw-Cutover -Code 'CURRENT_TASK_ESCROW_XML_MISMATCH'
        }
        $preDisable = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode $Context.mode -ExpectedTaskState 'Ready'
        Assert-CutoverStableReadyReadback `
            -Initial $status `
            -Readback $preDisable `
            -RequiredSeconds (120 + $Context.natural_trigger_margin_seconds) `
            -DriftCode 'TASK_CHANGED_BEFORE_DISABLE'
        Disable-CutoverTask
        $null = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode $Context.mode -ExpectedTaskState 'Disabled'
        $xmlAfter = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($xmlAfter.enabled -or $xmlAfter.normalized_sha256 -cne $xmlBefore.normalized_sha256) {
            Throw-Cutover -Code 'TASK_DISABLE_DEFINITION_DRIFT'
        }
        Assert-CutoverFileCheckpointUnchanged -Before $stateBefore -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_DISABLE'
        Assert-CutoverFileCheckpointUnchanged -Before $logBefore -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_DISABLE'
        $receipt = [pscustomobject][ordered]@{
            schema = $script:CutoverSchema
            ok = $true
            action = 'ValidateEscrowAndDisable'
            operation_id = $Context.operation_id
            status = 'TASK_DISABLED'
            escrow_id = $Context.escrow_id
            escrow_release_id = $Context.escrow_release_id
            current_release_id = $Context.release_id
            escrow_xml_sha256 = [string]$escrow.task_xml_sha256
            pre_task_state = 'Ready'
            post_task_state = 'Disabled'
            task_stopped = $false
            pre_xml_utf8_sha256 = $xmlBefore.utf8_text_sha256
            post_xml_utf8_sha256 = $xmlAfter.utf8_text_sha256
            definition_preserved_except_enabled = $true
            state_and_log_preserved = $true
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup `
            -FailureRecord $_ `
            -Context $Context `
            -Installer $Context.installer_path `
            -ExpectedModes @($Context.mode)
    }
}

function Invoke-CutoverDrainObserveAction {
    param([Parameter(Mandatory = $true)]$Context)
    $drain = Invoke-CutoverDrainObserve -Context $Context -Installer $Context.installer_path
    return [pscustomobject][ordered]@{
        schema = $script:CutoverSchema
        ok = $true
        action = 'DrainObserve'
        operation_id = $Context.operation_id
        status = $drain.status
        release_id = $Context.release_id
        pre_task_state = 'Ready'
        post_task_state = 'Ready'
        task_stopped = $false
        runs = $drain.runs
        final_run_id = $drain.final_run_id
        final_worker_status = $drain.final_worker_status
        first_last_run_utc = $drain.first_last_run_utc
        final_last_run_utc = $drain.final_last_run_utc
        log_appended_bytes = $drain.log_appended_bytes
    }
}

function Invoke-CutoverInstallObserveAndDrain {
    param([Parameter(Mandatory = $true)]$Context)
    $escrow = Invoke-CutoverEscrow -Context $Context -RequestedAction 'Validate'
    $restored = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.restored_installer_path -ExpectedMode 'Execute' -ExpectedTaskState 'Ready'
    try {
        $restoredXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $restoredXml.enabled -or $restoredXml.utf16le_bom_sha256 -cne [string]$escrow.task_xml_sha256) {
            Throw-Cutover -Code 'RESTORED_TASK_ESCROW_XML_MISMATCH'
        }
        $stateBeforeInstall = (Get-CutoverState -Path $Context.state_path).checkpoint
        $logBeforeInstall = Get-CutoverLogCheckpoint -Path $Context.log_path
        $preDisable = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.restored_installer_path -ExpectedMode 'Execute' -ExpectedTaskState 'Ready'
        Assert-CutoverStableReadyReadback `
            -Initial $restored `
            -Readback $preDisable `
            -RequiredSeconds (180 + $Context.natural_trigger_margin_seconds) `
            -DriftCode 'TASK_CHANGED_BEFORE_DISABLE'
        Disable-CutoverTask
        $null = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.restored_installer_path -ExpectedMode 'Execute' -ExpectedTaskState 'Disabled'
        $disabledXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($disabledXml.enabled -or $disabledXml.normalized_sha256 -cne $restoredXml.normalized_sha256) {
            Throw-Cutover -Code 'TASK_DISABLE_DEFINITION_DRIFT'
        }
        Assert-CutoverFileCheckpointUnchanged -Before $stateBeforeInstall -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_DISABLE'
        Assert-CutoverFileCheckpointUnchanged -Before $logBeforeInstall -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_DISABLE'
        $install = Invoke-CutoverInstaller -Context $Context -ScriptPath $Context.installer_path -RequestedAction 'Install' -RequestedMode 'Observe'
        $null = Assert-CutoverInstallerStatus -Status $install -Context $Context -ExpectedMode 'Observe' -ExpectedTaskState 'Ready'
        $null = Get-CutoverExactInstallerStatus `
            -Context $Context `
            -ScriptPath $Context.installer_path `
            -ExpectedMode 'Observe' `
            -ExpectedTaskState 'Ready'
        Assert-CutoverFileCheckpointUnchanged -Before $stateBeforeInstall -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged -Before $logBeforeInstall -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_INSTALL'
        Assert-CutoverBackupMatchesExpectedXml `
            -Context $Context `
            -ExpectedUtf8TextSha256 $disabledXml.utf8_text_sha256 `
            -ExpectedUtf16LeBomSha256 $disabledXml.utf16le_bom_sha256
        $drain = Invoke-CutoverDrainObserve -Context $Context -Installer $Context.installer_path
        $receipt = [pscustomobject][ordered]@{
            schema = $script:CutoverSchema
            ok = $true
            action = 'InstallObserveAndDrain'
            operation_id = $Context.operation_id
            status = $drain.status
            release_id = $Context.release_id
            escrow_id = $Context.escrow_id
            escrow_release_id = $Context.escrow_release_id
            escrow_xml_sha256 = [string]$escrow.task_xml_sha256
            pre_task_state = 'Ready'
            post_disable_task_state = 'Disabled'
            candidate_task_state = 'Ready'
            task_stopped = $false
            enabled_escrow_xml_utf8_sha256 = $restoredXml.utf8_text_sha256
            post_disable_xml_utf8_sha256 = $disabledXml.utf8_text_sha256
            enabled_escrow_preserved = $true
            backup_matches_post_disable_xml = $true
            post_install_status_readback = $true
            state_not_restored = $true
            runs = $drain.runs
            final_run_id = $drain.final_run_id
            final_worker_status = $drain.final_worker_status
            final_last_run_utc = $drain.final_last_run_utc
            log_appended_bytes = $drain.log_appended_bytes
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup `
            -FailureRecord $_ `
            -Context $Context `
            -Installer $Context.installer_path `
            -ExpectedModes @('Observe') `
            -FallbackInstaller $Context.restored_installer_path `
            -FallbackModes @('Execute')
    }
}

function Invoke-CutoverInstallObserveAndDrainFromFailedExecute {
    param([Parameter(Mandatory = $true)]$Context)

    # Everything through the stable second readback is read-only.  In
    # particular, an unexpected natural run, result, failure code, state/log
    # record, or task definition must fail before Disable-CutoverTask is ever
    # reachable.
    $escrow = Invoke-CutoverEscrow -Context $Context -RequestedAction 'Validate'
    $failedStatus = Get-CutoverExactInstallerStatus `
        -Context $Context `
        -ScriptPath $Context.restored_installer_path `
        -ExpectedMode 'Execute' `
        -ExpectedTaskState 'Ready' `
        -RequireResultZero $false
    if ([int64]$failedStatus.raw.last_task_result -ne [int64]$Context.expected_current_task_result) {
        Throw-Cutover -Code 'FAILED_EXECUTE_TASK_RESULT_MISMATCH'
    }

    $stateResult = Get-CutoverState -Path $Context.state_path
    $stateBeforeInstall = $stateResult.checkpoint
    $logBeforeInstall = Get-CutoverLogCheckpoint -Path $Context.log_path
    $failedRun = Assert-CutoverCurrentFailedExecuteRun `
        -Context $Context `
        -State $stateResult.value `
        -ExactStatus $failedStatus `
        -LogCheckpoint $logBeforeInstall `
        -ExpectedTaskResult $Context.expected_current_task_result `
        -ExpectedFailureCode $Context.expected_current_failure_code `
        -ExpectedRunId $Context.expected_current_run_id

    $failedXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    if (-not $failedXml.enabled -or
        $failedXml.utf16le_bom_sha256 -cne [string]$escrow.task_xml_sha256) {
        Throw-Cutover -Code 'FAILED_EXECUTE_ESCROW_XML_MISMATCH'
    }

    $preDisable = Get-CutoverExactInstallerStatus `
        -Context $Context `
        -ScriptPath $Context.restored_installer_path `
        -ExpectedMode 'Execute' `
        -ExpectedTaskState 'Ready' `
        -RequireResultZero $false
    Assert-CutoverStableReadyReadback `
        -Initial $failedStatus `
        -Readback $preDisable `
        -RequiredSeconds (180 + $Context.natural_trigger_margin_seconds) `
        -DriftCode 'TASK_CHANGED_BEFORE_DISABLE'
    if ([int64]$preDisable.raw.last_task_result -ne [int64]$Context.expected_current_task_result) {
        Throw-Cutover -Code 'FAILED_EXECUTE_TASK_RESULT_MISMATCH'
    }
    if ($preDisable.next_run_utc.Ticks -ne $failedStatus.next_run_utc.Ticks) {
        Throw-Cutover -Code 'TASK_CHANGED_BEFORE_DISABLE'
    }
    $preDisableXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    if (-not $preDisableXml.enabled -or
        $preDisableXml.utf8_text_sha256 -cne $failedXml.utf8_text_sha256) {
        Throw-Cutover -Code 'TASK_CHANGED_BEFORE_DISABLE'
    }
    Assert-CutoverFileCheckpointUnchanged `
        -Before $stateBeforeInstall `
        -Path $Context.state_path `
        -MaximumBytes $script:CutoverStateMaximumBytes `
        -Code 'STATE_CHANGED_BEFORE_DISABLE'
    Assert-CutoverFileCheckpointUnchanged `
        -Before $logBeforeInstall `
        -Path $Context.log_path `
        -MaximumBytes $script:CutoverLogMaximumBytes `
        -Code 'LOG_CHANGED_BEFORE_DISABLE'

    try {
        Disable-CutoverTask
        $disabledStatus = Get-CutoverExactInstallerStatus `
            -Context $Context `
            -ScriptPath $Context.restored_installer_path `
            -ExpectedMode 'Execute' `
            -ExpectedTaskState 'Disabled' `
            -RequireResultZero $false
        if ($disabledStatus.last_run_utc.Ticks -ne $preDisable.last_run_utc.Ticks -or
            [int64]$disabledStatus.raw.last_task_result -ne [int64]$Context.expected_current_task_result) {
            Throw-Cutover -Code 'TASK_CHANGED_DURING_DISABLE'
        }
        $disabledXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($disabledXml.enabled -or $disabledXml.normalized_sha256 -cne $failedXml.normalized_sha256) {
            Throw-Cutover -Code 'TASK_DISABLE_DEFINITION_DRIFT'
        }
        Assert-CutoverFileCheckpointUnchanged `
            -Before $stateBeforeInstall `
            -Path $Context.state_path `
            -MaximumBytes $script:CutoverStateMaximumBytes `
            -Code 'STATE_CHANGED_DURING_DISABLE'
        Assert-CutoverFileCheckpointUnchanged `
            -Before $logBeforeInstall `
            -Path $Context.log_path `
            -MaximumBytes $script:CutoverLogMaximumBytes `
            -Code 'LOG_CHANGED_DURING_DISABLE'

        $install = Invoke-CutoverInstaller `
            -Context $Context `
            -ScriptPath $Context.installer_path `
            -RequestedAction 'InstallFromDisabledNoStop' `
            -RequestedMode 'Observe' `
            -ExpectedCurrentTaskXmlSha256 $disabledXml.utf8_text_sha256
        $null = Assert-CutoverInstallerStatus `
            -Status $install `
            -Context $Context `
            -ExpectedMode 'Observe' `
            -ExpectedTaskState 'Ready' `
            -AllowInheritedTaskResult $true
        $null = Get-CutoverExactInstallerStatus `
            -Context $Context `
            -ScriptPath $Context.installer_path `
            -ExpectedMode 'Observe' `
            -ExpectedTaskState 'Ready' `
            -AllowInheritedTaskResult $true
        Assert-CutoverFileCheckpointUnchanged `
            -Before $stateBeforeInstall `
            -Path $Context.state_path `
            -MaximumBytes $script:CutoverStateMaximumBytes `
            -Code 'STATE_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged `
            -Before $logBeforeInstall `
            -Path $Context.log_path `
            -MaximumBytes $script:CutoverLogMaximumBytes `
            -Code 'LOG_CHANGED_DURING_INSTALL'
        Assert-CutoverBackupMatchesExpectedXml `
            -Context $Context `
            -ExpectedUtf8TextSha256 $disabledXml.utf8_text_sha256 `
            -ExpectedUtf16LeBomSha256 $disabledXml.utf16le_bom_sha256

        $drain = Invoke-CutoverDrainObserve -Context $Context -Installer $Context.installer_path -AllowInheritedTaskResult $true
        $receipt = [pscustomobject][ordered]@{
            schema = $script:CutoverSchema
            ok = $true
            action = 'InstallObserveAndDrainFromFailedExecute'
            operation_id = $Context.operation_id
            status = $drain.status
            release_id = $Context.release_id
            escrow_id = $Context.escrow_id
            escrow_release_id = $Context.escrow_release_id
            escrow_xml_sha256 = [string]$escrow.task_xml_sha256
            failed_execute_code = $failedRun.failure_code
            failed_execute_task_result = $failedRun.task_result
            failed_execute_run_id = $failedRun.run_id
            failed_execute_last_run_utc = $failedRun.last_run_utc.ToString('o')
            failed_execute_error_at_utc = $failedRun.error_at_utc.ToString('o')
            pre_task_state = 'Ready'
            post_disable_task_state = 'Disabled'
            candidate_task_state = 'Ready'
            candidate_install_action = 'InstallFromDisabledNoStop'
            task_stopped = $false
            enabled_escrow_xml_utf8_sha256 = $failedXml.utf8_text_sha256
            post_disable_xml_utf8_sha256 = $disabledXml.utf8_text_sha256
            old_definition_preserved_except_enabled = $true
            backup_matches_post_disable_xml = $true
            post_install_status_readback = $true
            state_and_log_preserved_through_candidate_install = $true
            state_not_restored = $true
            rollback_action = 'RestoreReady'
            runs = $drain.runs
            final_run_id = $drain.final_run_id
            final_worker_status = $drain.final_worker_status
            final_last_run_utc = $drain.final_last_run_utc
            log_appended_bytes = $drain.log_appended_bytes
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup `
            -FailureRecord $_ `
            -Context $Context `
            -Installer $Context.installer_path `
            -ExpectedModes @('Observe') `
            -FallbackInstaller $Context.restored_installer_path `
            -FallbackModes @('Execute')
    }
}

function Get-CutoverReadyObserveReplacementAdmission {
    # Read-only admission for a future, separately wired release-replacement
    # action. No disable/install/cleanup is permitted until this proof returns.
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$CurrentInstallerPath,
        [Parameter(Mandatory = $true)][string]$ExpectedCurrentInstallerSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedCurrentReleaseId,
        [Parameter(Mandatory = $true)][string]$ExpectedCurrentXmlSha256
    )
    if ($Context.mode -cne 'Observe') { Throw-Cutover -Code 'ACTION_REQUIRES_OBSERVE_MODE' }
    if ($ExpectedCurrentReleaseId -cnotmatch '^[0-9a-f]{40}$' -or
        $ExpectedCurrentReleaseId -ceq $Context.release_id) {
        Throw-Cutover -Code 'OBSERVE_REPLACEMENT_RELEASE_INVALID'
    }
    if ($ExpectedCurrentXmlSha256 -cnotmatch '^[0-9a-f]{64}$') {
        Throw-Cutover -Code 'EXPECTED_CURRENT_XML_SHA256_INVALID'
    }
    $currentPath = Resolve-CutoverInstaller -Path $CurrentInstallerPath -ExpectedSha256 $ExpectedCurrentInstallerSha256 -ReleaseId $ExpectedCurrentReleaseId -Prefix 'CURRENT_INSTALLER'
    $current = $Context.PSObject.Copy()
    $current.installer_path = $currentPath
    $current.expected_installer_sha256 = $ExpectedCurrentInstallerSha256
    $current.release_id = $ExpectedCurrentReleaseId
    Assert-CutoverPinnedExecutables -Context $Context
    $escrow = Invoke-CutoverEscrow -Context $Context -RequestedAction 'Validate'
    $initial = Get-CutoverExactInstallerStatus -Context $current -ScriptPath $currentPath -ExpectedMode 'Observe' -ExpectedTaskState 'Ready'
    $xml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    if (-not $xml.enabled -or $xml.utf8_text_sha256 -cne $ExpectedCurrentXmlSha256) {
        Throw-Cutover -Code 'CURRENT_OBSERVE_XML_MISMATCH'
    }
    $state = Get-CutoverState -Path $Context.state_path
    $null = Assert-CutoverStateTerminal -State $state.value -ExpectedMode 'Observe' -ExpectedStatus 'no_eligible_order' -ExpectedUserProfile $Context.user_profile_path
    $log = Get-CutoverLogCheckpoint -Path $Context.log_path
    $run = Assert-CutoverCurrentTerminalRun -Context $current -State $state.value -ExactStatus $initial -LogCheckpoint $log -ExpectedMode 'Observe' -ExpectedStatus 'no_eligible_order'
    $currentProtected = Get-CutoverProtectedSnapshot -Context $current
    $candidateProtected = Get-CutoverProtectedSnapshot -Context $Context
    $readback = Get-CutoverExactInstallerStatus -Context $current -ScriptPath $currentPath -ExpectedMode 'Observe' -ExpectedTaskState 'Ready'
    Assert-CutoverStableReadyReadback -Initial $initial -Readback $readback -RequiredSeconds (180 + $Context.natural_trigger_margin_seconds) -DriftCode 'TASK_CHANGED_BEFORE_REPLACEMENT'
    $xmlReadback = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    if (-not $xmlReadback.enabled -or $xmlReadback.utf8_text_sha256 -cne $ExpectedCurrentXmlSha256) {
        Throw-Cutover -Code 'CURRENT_OBSERVE_XML_MISMATCH'
    }
    Assert-CutoverFileCheckpointUnchanged -Before $state.checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_BEFORE_REPLACEMENT'
    Assert-CutoverFileCheckpointUnchanged -Before $log -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_BEFORE_REPLACEMENT'
    if ((Get-CutoverProtectedSnapshot -Context $current) -cne $currentProtected -or
        (Get-CutoverProtectedSnapshot -Context $Context) -cne $candidateProtected) {
        Throw-Cutover -Code 'PROTECTED_CHANGED_BEFORE_REPLACEMENT'
    }
    return [pscustomobject]@{
        current_context = $current
        current_status = $readback
        current_xml = $xmlReadback
        state_checkpoint = $state.checkpoint
        log_checkpoint = $log
        current_run = $run
        current_protected = $currentProtected
        candidate_protected = $candidateProtected
        escrow_xml_sha256 = [string]$escrow.task_xml_sha256
    }
}

function Save-CutoverObserveDefinitionEvidence {
    param([Parameter(Mandatory = $true)]$Context, [Parameter(Mandatory = $true)][string]$Xml, [Parameter(Mandatory = $true)][string]$ExpectedSha256)
    if ($Context.operation_id -cnotmatch '^[0-9a-f]{32}$') { Throw-Cutover -Code 'OPERATION_ID_INVALID' }
    Assert-CutoverSafeDirectory -Path $Context.metadata_root -MissingCode 'METADATA_ROOT_MISSING' -UnsafeCode 'METADATA_ROOT_UNSAFE'
    $evidence = Get-CutoverTaskXmlEvidence -Text $Xml
    if (-not $evidence.enabled -or $evidence.utf8_text_sha256 -cne $ExpectedSha256) { Throw-Cutover -Code 'CURRENT_OBSERVE_XML_MISMATCH' }
    $path = Join-Path $Context.metadata_root ('observe-before-' + $Context.operation_id + '.xml')
    Assert-CutoverPathChainSafe -Path $path -Code 'OBSERVE_BACKUP_UNSAFE'
    if (Test-Path -LiteralPath $path) { Throw-Cutover -Code 'OBSERVE_BACKUP_ALREADY_EXISTS' }
    $bytes = [Text.Encoding]::UTF8.GetBytes($Xml)
    # CreateNew makes operation identity one-use even across a concurrent writer.
    # A partial file on failure stays as evidence; never overwrite or delete it.
    $stream = $null
    try {
        $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } catch { Throw-Cutover -Code 'OBSERVE_BACKUP_WRITE_FAILED' }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
    $checkpoint = Get-CutoverFileCheckpoint -Path $path -MaximumBytes $script:CutoverStateMaximumBytes -MissingCode 'OBSERVE_BACKUP_MISSING' -InvalidCode 'OBSERVE_BACKUP_INVALID'
    if ($checkpoint.sha256 -cne $ExpectedSha256 -or $checkpoint.length -ne $bytes.Length) { Throw-Cutover -Code 'OBSERVE_BACKUP_READBACK_MISMATCH' }
    return [pscustomobject]@{path=$path;checkpoint=$checkpoint}
}

function Invoke-CutoverInstallObserveReadyFromReadyObserve {
    # Separate Observe-only release replacement; never replay a stale Execute.
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$CurrentInstallerPath,
        [Parameter(Mandatory = $true)][string]$ExpectedCurrentInstallerSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedCurrentReleaseId,
        [Parameter(Mandatory = $true)][string]$ExpectedCurrentXmlSha256
    )
    $admissionArgs = @{Context=$Context;CurrentInstallerPath=$CurrentInstallerPath;ExpectedCurrentInstallerSha256=$ExpectedCurrentInstallerSha256;ExpectedCurrentReleaseId=$ExpectedCurrentReleaseId;ExpectedCurrentXmlSha256=$ExpectedCurrentXmlSha256}
    $admitted = Get-CutoverReadyObserveReplacementAdmission @admissionArgs
    $backup = Save-CutoverObserveDefinitionEvidence -Context $Context -Xml (Export-CutoverTaskXml) -ExpectedSha256 $ExpectedCurrentXmlSha256
    # Backup I/O can race a natural run. Re-admit, but never adopt a changed run
    # or checkpoint as the baseline merely because that later run was healthy.
    $latest = Get-CutoverReadyObserveReplacementAdmission @admissionArgs
    if ($latest.current_run.run_id -cne $admitted.current_run.run_id -or
        $latest.current_status.last_run_utc -ne $admitted.current_status.last_run_utc -or
        $latest.current_protected -cne $admitted.current_protected -or
        $latest.candidate_protected -cne $admitted.candidate_protected -or
        $latest.escrow_xml_sha256 -cne $admitted.escrow_xml_sha256) { Throw-Cutover -Code 'OBSERVE_ADMISSION_CHANGED' }
    Assert-CutoverFileCheckpointUnchanged -Before $admitted.state_checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_BEFORE_REPLACEMENT'
    Assert-CutoverFileCheckpointUnchanged -Before $admitted.log_checkpoint -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_BEFORE_REPLACEMENT'
    Assert-CutoverFileCheckpointUnchanged -Before $backup.checkpoint -Path $backup.path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'OBSERVE_BACKUP_CHANGED'
    $final = Get-CutoverExactInstallerStatus -Context $latest.current_context -ScriptPath $CurrentInstallerPath -ExpectedMode Observe -ExpectedTaskState Ready
    Assert-CutoverStableReadyReadback -Initial $latest.current_status -Readback $final -RequiredSeconds (180 + $Context.natural_trigger_margin_seconds) -DriftCode 'TASK_CHANGED_BEFORE_DISABLE'
    $finalXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
    if (-not $finalXml.enabled -or $finalXml.utf8_text_sha256 -cne $ExpectedCurrentXmlSha256) { Throw-Cutover -Code 'CURRENT_OBSERVE_XML_MISMATCH' }
    $transaction = $Context.PSObject.Copy()
    $transaction.restored_installer_path = $CurrentInstallerPath
    $transaction.expected_restored_installer_sha256 = $ExpectedCurrentInstallerSha256
    try {
        Disable-CutoverTask
        $disabled = Get-CutoverExactInstallerStatus -Context $latest.current_context -ScriptPath $CurrentInstallerPath -ExpectedMode Observe -ExpectedTaskState Disabled
        if ($disabled.last_run_utc -ne $final.last_run_utc) { Throw-Cutover -Code 'TASK_CHANGED_DURING_DISABLE' }
        $disabledXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($disabledXml.enabled -or $disabledXml.normalized_sha256 -cne $finalXml.normalized_sha256) { Throw-Cutover -Code 'TASK_DISABLE_DEFINITION_DRIFT' }
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.state_checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_DISABLE'
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.log_checkpoint -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_DISABLE'
        $installed = Invoke-CutoverInstaller -Context $transaction -ScriptPath $Context.installer_path -RequestedAction InstallFromDisabledNoStop -RequestedMode Observe -ExpectedCurrentTaskXmlSha256 $disabledXml.utf8_text_sha256
        $null = Assert-CutoverInstallerStatus -Status $installed -Context $transaction -ExpectedMode Observe -ExpectedTaskState Ready
        $post = Get-CutoverExactInstallerStatus -Context $transaction -ScriptPath $Context.installer_path -ExpectedMode Observe -ExpectedTaskState Ready
        if ($post.last_run_utc -ne $final.last_run_utc) { Throw-Cutover -Code 'TASK_CHANGED_DURING_INSTALL' }
        Assert-CutoverTriggerWindow -NextRunUtc $post.next_run_utc -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds)
        Assert-CutoverBackupMatchesExpectedXml -Context $transaction -ExpectedUtf8TextSha256 $disabledXml.utf8_text_sha256 -ExpectedUtf16LeBomSha256 $disabledXml.utf16le_bom_sha256
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.state_checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.log_checkpoint -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged -Before $backup.checkpoint -Path $backup.path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'OBSERVE_BACKUP_CHANGED'
        if ((Get-CutoverProtectedSnapshot -Context $latest.current_context) -cne $admitted.current_protected -or
            (Get-CutoverProtectedSnapshot -Context $Context) -cne $admitted.candidate_protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_DURING_INSTALL' }
        $escrow = Invoke-CutoverEscrow -Context $Context -RequestedAction Validate
        if ($escrow.task_xml_sha256 -cne $admitted.escrow_xml_sha256) { Throw-Cutover -Code 'ESCROW_CHANGED_DURING_INSTALL' }
        # Snapshot/escrow checks can be slow. Do not report an old Ready read
        # after a natural candidate run or trigger change during that work.
        $finalPost = Get-CutoverExactInstallerStatus -Context $transaction -ScriptPath $Context.installer_path -ExpectedMode Observe -ExpectedTaskState Ready
        Assert-CutoverStableReadyReadback -Initial $post -Readback $finalPost -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds) -DriftCode 'TASK_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.state_checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.log_checkpoint -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_INSTALL'
        $postXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $postXml.enabled) { Throw-Cutover -Code 'CANDIDATE_OBSERVE_TASK_NOT_ENABLED' }
        $finalEscrow = Invoke-CutoverEscrow -Context $Context -RequestedAction Validate
        if ($finalEscrow.task_xml_sha256 -cne $admitted.escrow_xml_sha256) { Throw-Cutover -Code 'ESCROW_CHANGED_DURING_INSTALL' }
        if ((Get-CutoverProtectedSnapshot -Context $latest.current_context) -cne $admitted.current_protected -or
            (Get-CutoverProtectedSnapshot -Context $Context) -cne $admitted.candidate_protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_DURING_INSTALL' }
        Assert-CutoverFileCheckpointUnchanged -Before $backup.checkpoint -Path $backup.path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'OBSERVE_BACKUP_CHANGED'
        # Recheck native runtime after the slow final integrity reads, without
        # launching another child process. Never adopt an intervening run.
        $handoffRuntime = Get-CutoverTaskRuntime
        if ($handoffRuntime.state -cne 'Ready' -or $handoffRuntime.last_task_result -ne 0 -or
            $handoffRuntime.last_run_utc -ne $post.last_run_utc -or $handoffRuntime.next_run_utc -ne $post.next_run_utc) { Throw-Cutover -Code 'TASK_CHANGED_DURING_INSTALL' }
        Assert-CutoverTriggerWindow -NextRunUtc $handoffRuntime.next_run_utc -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds)
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.state_checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged -Before $admitted.log_checkpoint -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_INSTALL'
        $receipt = [pscustomobject][ordered]@{
            schema=$script:CutoverSchema;ok=$true;action='InstallObserveReadyFromReadyObserve';operation_id=$Context.operation_id
            status='OBSERVE_READY_HEALTH_UNCONFIRMED';release_id=$Context.release_id;previous_release_id=$ExpectedCurrentReleaseId
            pre_mode='Observe';post_mode='Observe';pre_task_state='Ready';post_task_state='Ready';task_stopped=$false
            enabled_definition_backup=$backup.path;enabled_definition_sha256=$ExpectedCurrentXmlSha256
            disabled_definition_sha256=$disabledXml.utf8_text_sha256;candidate_definition_sha256=$postXml.utf8_text_sha256
            backup_matches_post_disable_xml=$true;state_and_log_preserved=$true;protected_unchanged=$true;escrow_preserved=$true
            task_started=$false;worker_health_confirmed=$false;previous_run_id=$admitted.current_run.run_id
            last_run_utc=$post.last_run_utc.ToString('o');next_run_utc=$post.next_run_utc.ToString('o')
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup -FailureRecord $_ -Context $transaction -Installer $Context.installer_path -ExpectedModes @('Observe') -FallbackInstaller $CurrentInstallerPath -FallbackModes @('Observe')
    }
}

function Invoke-CutoverInstallExecuteReady {
    param([Parameter(Mandatory = $true)]$Context)
    Assert-CutoverPinnedExecutables -Context $Context
    $observe = Get-CutoverExactInstallerStatus `
        -Context $Context `
        -ScriptPath $Context.installer_path `
        -ExpectedMode 'Observe' `
        -ExpectedTaskState 'Ready'
    try {
        $stateResult = Get-CutoverState -Path $Context.state_path
        $observeLastPoll = Get-CutoverLastPoll -State $stateResult.value -ExpectedMode 'Observe' -ExpectedUserProfile $Context.user_profile_path
        if (@('tail_seeded', 'overlap_suppressed') -ccontains [string]$observeLastPoll.status) {
            Throw-Cutover -Code 'FORBIDDEN_TERMINAL_STATUS'
        }
        $null = Assert-CutoverStateTerminal `
            -State $stateResult.value `
            -ExpectedMode 'Observe' `
            -ExpectedStatus 'no_eligible_order' `
            -ExpectedUserProfile $Context.user_profile_path
        $stateBeforeInstall = $stateResult.checkpoint
        $logBeforeInstall = Get-CutoverLogCheckpoint -Path $Context.log_path
        $currentObserveRun = Assert-CutoverCurrentTerminalRun `
            -Context $Context `
            -State $stateResult.value `
            -ExactStatus $observe `
            -LogCheckpoint $logBeforeInstall `
            -ExpectedMode 'Observe' `
            -ExpectedStatus 'no_eligible_order'
        $observeXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $observeXml.enabled) { Throw-Cutover -Code 'CANDIDATE_OBSERVE_TASK_NOT_ENABLED' }

        $preDisable = Get-CutoverExactInstallerStatus `
            -Context $Context `
            -ScriptPath $Context.installer_path `
            -ExpectedMode 'Observe' `
            -ExpectedTaskState 'Ready'
        Assert-CutoverStableReadyReadback `
            -Initial $observe `
            -Readback $preDisable `
            -RequiredSeconds (180 + $Context.natural_trigger_margin_seconds) `
            -DriftCode 'TASK_CHANGED_BEFORE_DISABLE'

        Disable-CutoverTask
        $null = Get-CutoverExactInstallerStatus `
            -Context $Context `
            -ScriptPath $Context.installer_path `
            -ExpectedMode 'Observe' `
            -ExpectedTaskState 'Disabled'
        $disabledXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($disabledXml.enabled -or $disabledXml.normalized_sha256 -cne $observeXml.normalized_sha256) {
            Throw-Cutover -Code 'TASK_DISABLE_DEFINITION_DRIFT'
        }
        Assert-CutoverFileCheckpointUnchanged `
            -Before $stateBeforeInstall `
            -Path $Context.state_path `
            -MaximumBytes $script:CutoverStateMaximumBytes `
            -Code 'STATE_CHANGED_DURING_DISABLE'
        Assert-CutoverFileCheckpointUnchanged `
            -Before $logBeforeInstall `
            -Path $Context.log_path `
            -MaximumBytes $script:CutoverLogMaximumBytes `
            -Code 'LOG_CHANGED_DURING_DISABLE'

        $install = Invoke-CutoverInstaller `
            -Context $Context `
            -ScriptPath $Context.installer_path `
            -RequestedAction 'Install' `
            -RequestedMode 'Execute'
        $null = Assert-CutoverInstallerStatus `
            -Status $install `
            -Context $Context `
            -ExpectedMode 'Execute' `
            -ExpectedTaskState 'Ready'
        $execute = Get-CutoverExactInstallerStatus `
            -Context $Context `
            -ScriptPath $Context.installer_path `
            -ExpectedMode 'Execute' `
            -ExpectedTaskState 'Ready'
        Assert-CutoverTriggerWindow `
            -NextRunUtc $execute.next_run_utc `
            -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds)
        Assert-CutoverFileCheckpointUnchanged `
            -Before $stateBeforeInstall `
            -Path $Context.state_path `
            -MaximumBytes $script:CutoverStateMaximumBytes `
            -Code 'STATE_CHANGED_DURING_INSTALL'
        Assert-CutoverFileCheckpointUnchanged `
            -Before $logBeforeInstall `
            -Path $Context.log_path `
            -MaximumBytes $script:CutoverLogMaximumBytes `
            -Code 'LOG_CHANGED_DURING_INSTALL'
        Assert-CutoverBackupMatchesExpectedXml `
            -Context $Context `
            -ExpectedUtf8TextSha256 $disabledXml.utf8_text_sha256 `
            -ExpectedUtf16LeBomSha256 $disabledXml.utf16le_bom_sha256

        $executeXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $executeXml.enabled) { Throw-Cutover -Code 'EXECUTE_TASK_NOT_ENABLED' }
        $receipt = [pscustomobject][ordered]@{
            schema = $script:CutoverSchema
            ok = $true
            action = 'InstallExecuteReady'
            operation_id = $Context.operation_id
            status = 'EXECUTE_READY'
            release_id = $Context.release_id
            pre_mode = 'Observe'
            post_mode = 'Execute'
            pre_task_state = 'Ready'
            post_disable_task_state = 'Disabled'
            execute_task_state = 'Ready'
            task_stopped = $false
            candidate_observe_xml_utf8_sha256 = $observeXml.utf8_text_sha256
            candidate_observe_xml_utf16le_bom_sha256 = $observeXml.utf16le_bom_sha256
            post_disable_xml_utf8_sha256 = $disabledXml.utf8_text_sha256
            execute_xml_utf8_sha256 = $executeXml.utf8_text_sha256
            definition_preserved_except_enabled = $true
            candidate_observe_recovery_evidence_preserved = $true
            backup_matches_post_disable_xml = $true
            post_install_status_readback = $true
            claude_sha256 = $Context.expected_claude_sha256
            git_sha256 = $Context.expected_git_sha256
            execute_executables_pinned = $true
            state_and_log_preserved = $true
            observe_run_id = $currentObserveRun.run_id
            observe_last_run_utc = $currentObserveRun.last_run_utc.ToString('o')
            last_run_utc = $execute.last_run_utc.ToString('o')
            execute_next_run_utc = $execute.next_run_utc.ToString('o')
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup `
            -FailureRecord $_ `
            -Context $Context `
            -Installer $Context.installer_path `
            -ExpectedModes @('Execute', 'Observe')
    }
}

function Invoke-CutoverRestoreReady {
    param([Parameter(Mandatory = $true)]$Context)
    $escrow = Invoke-CutoverEscrow -Context $Context -RequestedAction 'Validate'
    $candidate = Get-CutoverExactInstallerStatusAnyState `
        -Context $Context `
        -ScriptPath $Context.installer_path `
        -ExpectedMode $Context.mode `
        -AllowedStates @('Ready', 'Disabled', 'Running', 'Queued') `
        -RequireResultZero $false
    $candidateState = [string]$candidate.raw.state
    if (@('Running', 'Queued') -ccontains $candidateState) {
        $activeFailure = $null
        try { Throw-Cutover -Code 'RESTORE_CANDIDATE_ACTIVE_QUARANTINED' }
        catch { $activeFailure = $_ }
        Throw-CutoverAfterCleanup `
            -FailureRecord $activeFailure `
            -Context $Context `
            -Installer $Context.installer_path `
            -ExpectedModes @($Context.mode)
    }

    $disablePerformed = $false
    try {
        $stateBefore = (Get-CutoverState -Path $Context.state_path).checkpoint
        $logBefore = Get-CutoverLogCheckpoint -Path $Context.log_path
        $candidateXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($candidateState -ceq 'Ready' -and [int64]$candidate.raw.last_task_result -ne 0) {
            Throw-Cutover -Code 'RESTORE_CANDIDATE_RESULT_NONZERO'
        }
        if ($candidateState -ceq 'Ready') {
            if (-not $candidateXml.enabled) { Throw-Cutover -Code 'CANDIDATE_TASK_NOT_ENABLED' }
            $preDisable = Get-CutoverExactInstallerStatus `
                -Context $Context `
                -ScriptPath $Context.installer_path `
                -ExpectedMode $Context.mode `
                -ExpectedTaskState 'Ready'
            Assert-CutoverStableReadyReadback `
                -Initial $candidate `
                -Readback $preDisable `
                -RequiredSeconds (120 + $Context.natural_trigger_margin_seconds) `
                -DriftCode 'TASK_CHANGED_BEFORE_DISABLE'
            Disable-CutoverTask
            $disablePerformed = $true
            $null = Get-CutoverExactInstallerStatus `
                -Context $Context `
                -ScriptPath $Context.installer_path `
                -ExpectedMode $Context.mode `
                -ExpectedTaskState 'Disabled'
            $disabledCandidateXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
            if ($disabledCandidateXml.enabled -or
                $disabledCandidateXml.normalized_sha256 -cne $candidateXml.normalized_sha256) {
                Throw-Cutover -Code 'TASK_DISABLE_DEFINITION_DRIFT'
            }
        } else {
            if ($candidateXml.enabled) { Throw-Cutover -Code 'CANDIDATE_DISABLED_XML_ENABLED' }
            $stableDisabled = Get-CutoverExactInstallerStatus `
                -Context $Context `
                -ScriptPath $Context.installer_path `
                -ExpectedMode $Context.mode `
                -ExpectedTaskState 'Disabled' `
                -RequireResultZero $false
            if ($stableDisabled.last_run_utc.Ticks -ne $candidate.last_run_utc.Ticks -or
                [int64]$stableDisabled.raw.last_task_result -ne [int64]$candidate.raw.last_task_result) {
                Throw-Cutover -Code 'TASK_CHANGED_BEFORE_RESTORE'
            }
            $disabledCandidateXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
            if ($disabledCandidateXml.enabled -or
                $disabledCandidateXml.utf8_text_sha256 -cne $candidateXml.utf8_text_sha256) {
                Throw-Cutover -Code 'TASK_CHANGED_BEFORE_RESTORE'
            }
        }

        Assert-CutoverFileCheckpointUnchanged -Before $stateBefore -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_DISABLE'
        Assert-CutoverFileCheckpointUnchanged -Before $logBefore -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_DISABLE'
        $restore = Invoke-CutoverEscrow -Context $Context -RequestedAction 'Restore'
        if ([bool]$restore.task_stopped) { Throw-Cutover -Code 'RESTORE_STOPPED_ACTIVE_TASK' }
        $restored = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.restored_installer_path -ExpectedMode 'Execute' -ExpectedTaskState 'Ready'
        $restoredXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $restoredXml.enabled -or $restoredXml.utf16le_bom_sha256 -cne [string]$escrow.task_xml_sha256 -or
            [string]$restore.task_xml_sha256 -cne [string]$escrow.task_xml_sha256) {
            Throw-Cutover -Code 'RESTORE_XML_READBACK_MISMATCH'
        }
        Assert-CutoverFileCheckpointUnchanged -Before $stateBefore -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_RESTORE'
        Assert-CutoverFileCheckpointUnchanged -Before $logBefore -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_RESTORE'
        $receipt = [pscustomobject][ordered]@{
            schema = $script:CutoverSchema
            ok = $true
            action = 'RestoreReady'
            operation_id = $Context.operation_id
            status = 'RESTORED_READY'
            candidate_release_id = $Context.release_id
            candidate_mode = $Context.mode
            restored_release_id = $Context.escrow_release_id
            escrow_id = $Context.escrow_id
            escrow_xml_sha256 = [string]$escrow.task_xml_sha256
            pre_task_state = $candidateState
            candidate_last_task_result = [int64]$candidate.raw.last_task_result
            post_disable_task_state = 'Disabled'
            restored_task_state = 'Ready'
            candidate_disabled = $true
            candidate_disable_performed = $disablePerformed
            task_stopped = $false
            restored_last_run_utc = $restored.last_run_utc.ToString('o')
            state_and_log_preserved = $true
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup `
            -FailureRecord $_ `
            -Context $Context `
            -Installer $Context.restored_installer_path `
            -ExpectedModes @('Execute') `
            -FallbackInstaller $Context.installer_path `
            -FallbackModes @($Context.mode)
    }
}

function Invoke-CutoverStartAndAwait {
    param([Parameter(Mandatory = $true)]$Context)
    $initial = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode $Context.mode -ExpectedTaskState 'Ready'
    try {
        Assert-CutoverTriggerWindow -NextRunUtc $initial.next_run_utc -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds)
        $stateBefore = Get-CutoverState -Path $Context.state_path
        if ($stateBefore.value.mode -isnot [string] -or @('Observe', 'Execute') -cnotcontains [string]$stateBefore.value.mode) {
            Throw-Cutover -Code 'STATE_BASELINE_RUN_ID_INVALID'
        }
        $baselineLastPoll = Get-CutoverLastPoll `
            -State $stateBefore.value `
            -ExpectedMode ([string]$stateBefore.value.mode) `
            -ExpectedUserProfile $Context.user_profile_path
        Assert-CutoverPinnedExecutables -Context $Context
        $protectedBefore = Get-CutoverProtectedSnapshot -Context $Context
        $logBefore = Get-CutoverLogCheckpoint -Path $Context.log_path
        $preStart = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode $Context.mode -ExpectedTaskState 'Ready'
        Assert-CutoverStableReadyReadback `
            -Initial $initial `
            -Readback $preStart `
            -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds) `
            -DriftCode 'TASK_CHANGED_BEFORE_START'
        $deadline = [DateTime]::UtcNow.AddSeconds($Context.timeout_seconds)
        $run = Invoke-CutoverOneRun `
            -Context $Context `
            -Installer $Context.installer_path `
            -ExpectedMode $Context.mode `
            -DeadlineUtc $deadline `
            -PreviousLastRunUtc $preStart.last_run_utc `
            -PreviousRunId ([string]$baselineLastPoll.run_id) `
            -LogBefore $logBefore `
            -ExpectedStatus $Context.expected_terminal_status `
            -ExpectedWorkId $Context.expected_work_id `
            -ExpectedRowId $Context.expected_row_id `
            -ExpectedResultStatus $Context.expected_result_status
        $protectedAfter = Get-CutoverProtectedSnapshot -Context $Context
        if ($protectedAfter -cne $protectedBefore) { Throw-Cutover -Code 'PROTECTED_FINGERPRINT_CHANGED' }
        $receipt = [pscustomobject][ordered]@{
            schema = $script:CutoverSchema
            ok = $true
            action = 'StartAndAwait'
            operation_id = $Context.operation_id
            status = 'RUN_CONFIRMED'
            release_id = $Context.release_id
            mode = $Context.mode
            pre_task_state = 'Ready'
            post_task_state = 'Ready'
            task_stopped = $false
            worker_status = $run.status
            run_id = $run.run_id
            work_id = $Context.expected_work_id
            row_id = $Context.expected_row_id
            result_status = $Context.expected_result_status
            last_run_before_utc = $initial.last_run_utc.ToString('o')
            last_run_after_utc = $run.last_run_utc.ToString('o')
            log_appended_bytes = $run.log_appended_bytes
            log_prefix_preserved = $true
            protected_fingerprints_unchanged = $true
            isolation_residue_absent = $true
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup `
            -FailureRecord $_ `
            -Context $Context `
            -Installer $Context.installer_path `
            -ExpectedModes @('Execute')
    }
}

function Assert-CutoverFutureObserveDefinition {
    param([Parameter(Mandatory = $true)][string]$Text, [Parameter(Mandatory = $true)]$Context)
    $doc = New-Object Xml.XmlDocument
    $doc.XmlResolver = $null
    try { $doc.LoadXml($Text) } catch { Throw-Cutover -Code 'OBSERVE_RECOVERY_XML_INVALID' }
    $triggers = @($doc.SelectNodes('/*[local-name()="Task"]/*[local-name()="Triggers"]/*'))
    $starts = @($doc.SelectNodes('/*[local-name()="Task"]/*[local-name()="Triggers"]/*[local-name()="TimeTrigger"]/*[local-name()="StartBoundary"]'))
    $intervals = @($doc.SelectNodes('/*[local-name()="Task"]/*[local-name()="Triggers"]/*[local-name()="TimeTrigger"]/*[local-name()="Repetition"]/*[local-name()="Interval"]'))
    $catchup = @($doc.SelectNodes('/*[local-name()="Task"]/*[local-name()="Settings"]/*[local-name()="StartWhenAvailable"]'))
    if ($catchup.Count -ne 1 -or $catchup[0].InnerText -cne 'true') { Throw-Cutover -Code 'OBSERVE_RECOVERY_SETTINGS_INVALID' }
    if ($triggers.Count -ne 2 -or @($triggers | Where-Object { $_.LocalName -ceq 'BootTrigger' }).Count -ne 1 -or
        $starts.Count -ne 1 -or $intervals.Count -ne 1 -or $intervals[0].InnerText -cne 'PT15M') {
        Throw-Cutover -Code 'OBSERVE_RECOVERY_TRIGGER_SHAPE_INVALID'
    }
    $startUtc = ConvertTo-CutoverUtcDateTime -Value ([string]$starts[0].InnerText) -Code 'OBSERVE_RECOVERY_START_BOUNDARY_INVALID'
    Assert-CutoverTriggerWindow -NextRunUtc $startUtc -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds + 120)
    return $startUtc
}

function Assert-CutoverDisabledObserveRecoveryLimits {
    param([Parameter(Mandatory = $true)]$Context)
    if ($Context.timeout_seconds -ne 420 -or $Context.natural_trigger_margin_seconds -ne 60 -or
        ($Context.max_runs -isnot [int] -and $Context.max_runs -isnot [long]) -or
        $Context.max_runs -lt 1 -or $Context.max_runs -gt 8) {
        Throw-Cutover -Code 'OBSERVE_RECOVERY_LIMITS_INVALID'
    }
}

function Invoke-CutoverInstallObserveAndDrainFromDisabledExecute {
    param([Parameter(Mandatory = $true)]$Context)
    # Never enable the old Execute definition: StartWhenAvailable may replay
    # missed work. The pinned no-stop installer replaces it with future Observe.
    if ($Context.mode -cne 'Observe') { Throw-Cutover -Code 'ACTION_REQUIRES_OBSERVE_MODE' }
    $expectedBaselineStatus = 'no_eligible_order'
    $targetedResult = $false
    if ([string]::IsNullOrEmpty([string]$Context.expected_terminal_status)) {
        if ($Context.expected_work_id -or $Context.expected_row_id -or $Context.expected_result_status) {
            Throw-Cutover -Code 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY'
        }
    } elseif ([string]$Context.expected_terminal_status -ceq 'result_confirmed') {
        $expectedBaselineStatus = 'result_confirmed'
        $targetedResult = $true
        if ($Context.expected_work_id -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$') { Throw-Cutover -Code 'EXPECTED_WORK_ID_INVALID' }
        $rowGuid = [Guid]::Empty
        if (-not [Guid]::TryParseExact([string]$Context.expected_row_id, 'D', [ref]$rowGuid)) { Throw-Cutover -Code 'EXPECTED_ROW_ID_INVALID' }
        if (@('completed', 'blocked', 'rejected') -cnotcontains [string]$Context.expected_result_status) { Throw-Cutover -Code 'EXPECTED_RESULT_STATUS_INVALID' }
    } else {
        Throw-Cutover -Code 'EXPECTED_TERMINAL_STATUS_INVALID'
    }
    if ($Context.expected_disabled_xml_sha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Cutover -Code 'EXPECTED_DISABLED_XML_SHA256_INVALID' }
    Assert-CutoverDisabledObserveRecoveryLimits -Context $Context
    try {
        Assert-CutoverPinnedExecutables -Context $Context
        $initial = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode 'Execute' -ExpectedTaskState 'Disabled'
        $xml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($xml.enabled -or $xml.utf8_text_sha256 -cne $Context.expected_disabled_xml_sha256) { Throw-Cutover -Code 'DISABLED_TASK_XML_NOT_AUTHENTICATED' }
        if ($initial.last_run_utc.Year -le 1601) { Throw-Cutover -Code 'DISABLED_TASK_NEVER_RUN' }
        $state = Get-CutoverState -Path $Context.state_path
        $baselineMode = [string]$state.value.mode
        if ($targetedResult -and $baselineMode -cne 'Execute') {
            Throw-Cutover -Code 'DISABLED_RESULT_BASELINE_MODE_INVALID'
        }
        $baseline = Get-CutoverLastPoll -State $state.value -ExpectedMode $baselineMode -ExpectedUserProfile $Context.user_profile_path
        if ([string]$baseline.status -cne $expectedBaselineStatus) { Throw-Cutover -Code 'DISABLED_BASELINE_NOT_HEALTHY' }
        $terminal = Assert-CutoverStateTerminal `
            -State $state.value `
            -ExpectedMode $baselineMode `
            -ExpectedStatus $expectedBaselineStatus `
            -ExpectedWorkId ([string]$Context.expected_work_id) `
            -ExpectedRowId ([string]$Context.expected_row_id) `
            -ExpectedResultStatus ([string]$Context.expected_result_status) `
            -ExpectedUserProfile $Context.user_profile_path
        $log = Get-CutoverLogCheckpoint -Path $Context.log_path
        $currentRunArguments = @{
            Context = $Context
            State = $state.value
            ExactStatus = $initial
            LogCheckpoint = $log
            ExpectedMode = $baselineMode
            ExpectedStatus = $expectedBaselineStatus
        }
        if ($targetedResult) {
            $currentRunArguments.ExpectedWorkId = [string]$Context.expected_work_id
            $currentRunArguments.ExpectedRowId = [string]$Context.expected_row_id
            $currentRunArguments.ExpectedResultStatus = [string]$Context.expected_result_status
        }
        $currentRun = Assert-CutoverCurrentTerminalRun @currentRunArguments
        $protected = Get-CutoverProtectedSnapshot -Context $Context
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_BEFORE_OBSERVE_RECOVERY' }
        $preInstall = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode 'Execute' -ExpectedTaskState 'Disabled'
        $preXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($preInstall.last_run_utc.Ticks -ne $initial.last_run_utc.Ticks -or $preXml.enabled -or $preXml.utf8_text_sha256 -cne $xml.utf8_text_sha256) { Throw-Cutover -Code 'TASK_CHANGED_BEFORE_OBSERVE_RECOVERY' }
        Assert-CutoverFileCheckpointUnchanged -Before $state.checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_BEFORE_OBSERVE_RECOVERY'
        Assert-CutoverFileCheckpointUnchanged -Before $log -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_BEFORE_OBSERVE_RECOVERY'
        $install = Invoke-CutoverInstaller -Context $Context -ScriptPath $Context.installer_path -RequestedAction 'InstallFromDisabledNoStop' -RequestedMode 'Observe' -ExpectedCurrentTaskXmlSha256 $xml.utf8_text_sha256
        $null = Assert-CutoverInstallerStatus -Status $install -Context $Context -ExpectedMode 'Observe' -ExpectedTaskState 'Ready'
        $ready = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode 'Observe' -ExpectedTaskState 'Ready'
        if ($ready.last_run_utc.Ticks -ne $initial.last_run_utc.Ticks) { Throw-Cutover -Code 'TASK_RAN_DURING_OBSERVE_RECOVERY' }
        $observeText = Export-CutoverTaskXml
        $observeXml = Get-CutoverTaskXmlEvidence -Text $observeText
        if (-not $observeXml.enabled) { Throw-Cutover -Code 'RECOVERY_OBSERVE_TASK_NOT_ENABLED' }
        $startUtc = Assert-CutoverFutureObserveDefinition -Text $observeText -Context $Context
        Assert-CutoverTriggerWindow -NextRunUtc $ready.next_run_utc -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds + 120)
        Assert-CutoverFileCheckpointUnchanged -Before $state.checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_OBSERVE_RECOVERY'
        Assert-CutoverFileCheckpointUnchanged -Before $log -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_OBSERVE_RECOVERY'
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_DURING_OBSERVE_RECOVERY' }
        Assert-CutoverBackupMatchesExpectedXml -Context $Context -ExpectedUtf8TextSha256 $xml.utf8_text_sha256 -ExpectedUtf16LeBomSha256 $xml.utf16le_bom_sha256
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_BEFORE_OBSERVE_DRAIN' }
        # Slow protected-tree/backup reads do not consume an assumed reserve.
        # Validate the exact admitted Ready identity and remaining window again.
        $handoff = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode 'Observe' -ExpectedTaskState 'Ready'
        Assert-CutoverStableReadyReadback -Initial $ready -Readback $handoff -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds) -DriftCode 'TASK_CHANGED_BEFORE_OBSERVE_DRAIN'
        $handoffXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if (-not $handoffXml.enabled -or $handoffXml.normalized_sha256 -cne $observeXml.normalized_sha256) { Throw-Cutover -Code 'TASK_XML_CHANGED_BEFORE_OBSERVE_DRAIN' }
        Assert-CutoverFileCheckpointUnchanged -Before $state.checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_BEFORE_OBSERVE_DRAIN'
        Assert-CutoverFileCheckpointUnchanged -Before $log -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_BEFORE_OBSERVE_DRAIN'
        $admitted = [pscustomobject]@{
            run_id=[string]$currentRun.run_id
            last_run_utc=$initial.last_run_utc
            state_checkpoint=$state.checkpoint
            log_checkpoint=$log
            protected_fingerprint=$protected
            observe_definition_sha256=$observeXml.normalized_sha256
            baseline_status=$expectedBaselineStatus
            work_id=$(if ($targetedResult) { [string]$terminal.work_id } else { '' })
            row_id=$(if ($targetedResult) { [string]$terminal.row_id } else { '' })
            result_status=$(if ($targetedResult) { [string]$Context.expected_result_status } else { '' })
            output_sha256=$(if ($targetedResult) { [string]$terminal.output_sha256 } else { '' })
        }
        $drain = Invoke-CutoverDrainObserve -Context $Context -Installer $Context.installer_path -AdmittedBaseline $admitted
        if ($drain.status -cne 'OBSERVE_DRAINED' -or $drain.final_worker_status -cne 'no_eligible_order') { Throw-Cutover -Code 'OBSERVE_RECOVERY_NOT_DRAINED' }
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $protected) { Throw-Cutover -Code 'PROTECTED_FINGERPRINT_CHANGED' }
        $receipt = [pscustomobject][ordered]@{
            schema = $script:CutoverSchema; ok = $true; action = 'InstallObserveAndDrainFromDisabledExecute'
            operation_id = $Context.operation_id; status = 'OBSERVE_DRAINED'; release_id = $Context.release_id
            pre_mode = 'Execute'; post_mode = 'Observe'; pre_task_state = 'Disabled'; post_task_state = 'Ready'
            task_stopped = $false; old_execute_task_not_enabled = $true; candidate_install_action = 'InstallFromDisabledNoStop'
            authenticated_disabled_xml_sha256 = $xml.utf8_text_sha256; observe_xml_sha256 = $observeXml.utf8_text_sha256
            observe_start_boundary_utc = $startUtc.ToString('o'); backup_matches_authenticated_disabled_xml = $true
            state_and_log_preserved_through_install = $true; protected_fingerprints_unchanged = $true; state_not_restored = $true
            baseline_status = $expectedBaselineStatus
            recovered_work_id = $(if ($targetedResult) { [string]$terminal.work_id } else { '' })
            recovered_row_id = $(if ($targetedResult) { [string]$terminal.row_id } else { '' })
            recovered_result_status = $(if ($targetedResult) { [string]$Context.expected_result_status } else { '' })
            recovered_output_sha256 = $(if ($targetedResult) { [string]$terminal.output_sha256 } else { '' })
            runs = $drain.runs; final_run_id = $drain.final_run_id; final_worker_status = $drain.final_worker_status
            final_last_run_utc = $drain.final_last_run_utc; log_appended_bytes = $drain.log_appended_bytes
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        if ($_.Exception.Data.Contains('cleanup_status')) { throw }
        Throw-CutoverAfterCleanup -FailureRecord $_ -Context $Context -Installer $Context.installer_path -ExpectedModes @('Observe', 'Execute')
    }
}

function Assert-CutoverCurrentDisabledHttpReadRun {
    param($Context, $State, $ExactStatus, $LogCheckpoint, [ValidateSet('Execute','Observe')][string]$ExpectedMode = 'Execute')
    # Authenticate this specific two-read, pre-admission board-read incident:
    # either two 404/HTML transport failures, or a retry that reached JSON but
    # found no rows. This is not a generic permission to recover arbitrary
    # nonzero Execute results.
    $null = Assert-CutoverStateShape -State $State
    $poll = Get-CutoverLastPoll -State $State -ExpectedMode $ExpectedMode -ExpectedUserProfile $Context.user_profile_path
    $expectedFailureCode = [string]$Context.expected_current_failure_code
    $expectedFailureMessage = if ($expectedFailureCode -ceq 'BOARD_ROWS_MISSING') { 'board_rows_missing' } else { $expectedFailureCode }
    if ($ExactStatus.raw.state -cne 'Disabled' -or
        ($ExactStatus.raw.last_task_result -isnot [int] -and $ExactStatus.raw.last_task_result -isnot [long]) -or
        [int64]$ExactStatus.raw.last_task_result -ne 20 -or
        ($Context.expected_current_task_result -isnot [int] -and $Context.expected_current_task_result -isnot [long]) -or
        $Context.expected_current_task_result -ne 20 -or @('BOARD_READ_HTTP_ERROR','BOARD_ROWS_MISSING') -cnotcontains $expectedFailureCode -or
        $Context.expected_current_run_id -cnotmatch '^[0-9a-f]{32}$' -or $poll.run_id -cne $Context.expected_current_run_id -or
        $poll.status -cne 'error' -or $null -eq $State.error -or $State.error.code -cne $expectedFailureCode -or
        $State.error.message -cne $expectedFailureMessage -or -not [string]::IsNullOrEmpty([string]$State.error.work_id) -or
        -not [string]::IsNullOrEmpty([string]$State.error.row_id) -or [int64]$State.counts.errors -lt 1) {
        Throw-Cutover -Code 'DISABLED_HTTP_ERROR_IDENTITY_INVALID'
    }
    $entries = @(Get-CutoverTrailingLogRun -Path $Context.log_path -Checkpoint $LogCheckpoint -RunId $poll.run_id)
    if ($entries.Count -ne 3 -or $entries[0].event -cne 'poll_started' -or
        $entries[1].event -cne 'board_read_retry' -or $entries[2].event -cne 'run_error') {
        Throw-Cutover -Code 'DISABLED_HTTP_ERROR_LOG_SHAPE_INVALID'
    }
    $terminal = Assert-CutoverLogRun -Entries $entries -RunId $poll.run_id -Status error -ExpectedMode $ExpectedMode `
        -ExpectedWorkId '' -ExpectedRowId '' -ExpectedErrorCode $expectedFailureCode `
        -RunWindowStartUtc $ExactStatus.last_run_utc -RunWindowEndUtc ([DateTime]::UtcNow)
    foreach ($index in 0..2) {
        if (-not [string]::IsNullOrEmpty([string]$entries[$index].work_id) -or
            -not [string]::IsNullOrEmpty([string]$entries[$index].row_id)) {
            Throw-Cutover -Code 'DISABLED_HTTP_ERROR_ADMISSION_NOT_ABSENT'
        }
    }
    $allowedRetryCodes = if ($expectedFailureCode -ceq 'BOARD_ROWS_MISSING') { @('BOARD_READ_HTTP_ERROR', 'board_rows_missing') } else { @('BOARD_READ_HTTP_ERROR') }
    $retryCode = [string]$entries[1].code
    if ($entries[0].level -cne 'info' -or $entries[0].code -cne '' -or $entries[0].message -cne '' -or
        $entries[1].level -cne 'warning' -or $allowedRetryCodes -cnotcontains $retryCode -or
        $entries[1].message -cne 'A transient pre-admission board read failed; retrying once.' -or
        $terminal.message -cne $State.error.message) { Throw-Cutover -Code 'DISABLED_HTTP_ERROR_LOG_EVIDENCE_INVALID' }
    foreach ($index in 1..2) {
        $detailsProperty = $entries[$index].PSObject.Properties['details']
        $details = if ($null -ne $detailsProperty) { $detailsProperty.Value } else { $null }
        $entryCode = [string]$entries[$index].code
        $isHttp404 = $entryCode -ceq 'BOARD_READ_HTTP_ERROR'
        $isRowsMissing = $entryCode -ceq 'BOARD_ROWS_MISSING' -or $entryCode -ceq 'board_rows_missing'
        $expected = @('attempt', 'transport_exit', 'http_status', 'content_type_class', 'elapsed_ms')
        if ($isHttp404) { $expected += 'content_length', 'content_sha256' }
        if ($index -eq 1) { $expected += 'code' }
        if ($null -eq $details -or @($details.PSObject.Properties).Count -ne $expected.Count -or
            @($expected | Where-Object { $null -eq $details.PSObject.Properties[$_] }).Count -ne 0) {
            Throw-Cutover -Code 'DISABLED_HTTP_ERROR_TRANSPORT_EVIDENCE_INVALID'
        }
        foreach ($name in $expected) {
            if ($details.PSObject.Properties[$name].Value -isnot [string]) { Throw-Cutover -Code 'DISABLED_HTTP_ERROR_TRANSPORT_EVIDENCE_INVALID' }
        }
        if ($details.attempt -cne [string]$index -or $details.transport_exit -cne '0' -or
            $details.elapsed_ms -cnotmatch '^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?$' -or
            [double]::Parse($details.elapsed_ms, [Globalization.CultureInfo]::InvariantCulture) -le 0 -or
            ($index -eq 1 -and [string]$details.code -cne $entryCode)) {
            Throw-Cutover -Code 'DISABLED_HTTP_ERROR_TRANSPORT_EVIDENCE_INVALID'
        }
        if ($isHttp404 -and
            ($details.http_status -cne '404' -or $details.content_type_class -cne 'html' -or
             $details.content_length -cne '0' -or $details.content_sha256 -cne 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')) {
            Throw-Cutover -Code 'DISABLED_HTTP_ERROR_TRANSPORT_EVIDENCE_INVALID'
        }
        if ($isRowsMissing -and
            ($details.http_status -cne '200' -or $details.content_type_class -cne 'json')) {
            Throw-Cutover -Code 'DISABLED_HTTP_ERROR_TRANSPORT_EVIDENCE_INVALID'
        }
    }
    $started = ConvertTo-CutoverUtcDateTime -Value $entries[0].at -Code 'DISABLED_HTTP_ERROR_TIME_INVALID'
    $pollAt = ConvertTo-CutoverUtcDateTime -Value $poll.at -Code 'DISABLED_HTTP_ERROR_TIME_INVALID'
    $ended = ConvertTo-CutoverUtcDateTime -Value $terminal.at -Code 'DISABLED_HTTP_ERROR_TIME_INVALID'
    $errorAt = ConvertTo-CutoverUtcDateTime -Value $State.error.at -Code 'DISABLED_HTTP_ERROR_TIME_INVALID'
    $startup = ($started - $ExactStatus.last_run_utc).TotalSeconds
    if ([Math]::Abs(($started - $pollAt).TotalSeconds) -gt $script:CutoverClockToleranceSeconds -or
        [Math]::Abs(($ended - $errorAt).TotalSeconds) -gt $script:CutoverClockToleranceSeconds -or
        $errorAt -lt $started.AddSeconds(-$script:CutoverClockToleranceSeconds) -or
        $startup -lt -$script:CutoverClockToleranceSeconds -or $startup -gt $script:CutoverTaskStartupMaximumSeconds) {
        Throw-Cutover -Code 'DISABLED_HTTP_ERROR_TIME_INVALID'
    }
    return [pscustomobject]@{run_id=$poll.run_id;last_run_utc=$ExactStatus.last_run_utc}
}

function Invoke-CutoverInstallObserveReadyFromDisabledHttpError {
    param([Parameter(Mandatory = $true)]$Context)
    if ($Context.mode -cne 'Observe') { Throw-Cutover -Code 'ACTION_REQUIRES_OBSERVE_MODE' }
    if ($Context.expected_terminal_status -or $Context.expected_work_id -or $Context.expected_row_id -or $Context.expected_result_status) {
        Throw-Cutover -Code 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY'
    }
    Assert-CutoverDisabledObserveRecoveryLimits -Context $Context
    if ($Context.expected_disabled_xml_sha256 -cnotmatch '^[0-9a-f]{64}$') { Throw-Cutover -Code 'EXPECTED_DISABLED_XML_SHA256_INVALID' }
    try {
        $preMode = if ($Context.PSObject.Properties['expected_recovery_pre_mode']) { [string]$Context.expected_recovery_pre_mode } else { 'Execute' }
        if (@('Execute','Observe') -cnotcontains $preMode) { Throw-Cutover -Code 'RECOVERY_PRE_MODE_INVALID' }
        $receiptAction = if ($Context.PSObject.Properties['action']) { [string]$Context.action } else { 'InstallObserveReadyFromDisabledHttpError' }
        Assert-CutoverPinnedExecutables -Context $Context
        $initial = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode $preMode -ExpectedTaskState Disabled -RequireResultZero $false
        $xml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($xml.enabled -or $xml.utf8_text_sha256 -cne $Context.expected_disabled_xml_sha256) { Throw-Cutover -Code 'DISABLED_TASK_XML_NOT_AUTHENTICATED' }
        $state = Get-CutoverState -Path $Context.state_path
        $log = Get-CutoverLogCheckpoint -Path $Context.log_path
        $failed = Assert-CutoverCurrentDisabledHttpReadRun -Context $Context -State $state.value -ExactStatus $initial -LogCheckpoint $log -ExpectedMode $preMode
        $protected = Get-CutoverProtectedSnapshot -Context $Context
        $pre = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode $preMode -ExpectedTaskState Disabled -RequireResultZero $false
        $preXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($pre.last_run_utc.Ticks -ne $initial.last_run_utc.Ticks -or [int64]$pre.raw.last_task_result -ne 20 -or
            $preXml.enabled -or $preXml.utf8_text_sha256 -cne $xml.utf8_text_sha256) { Throw-Cutover -Code 'TASK_CHANGED_BEFORE_OBSERVE_RECOVERY' }
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_BEFORE_OBSERVE_RECOVERY' }
        Assert-CutoverFileCheckpointUnchanged -Before $state.checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_BEFORE_OBSERVE_RECOVERY'
        Assert-CutoverFileCheckpointUnchanged -Before $log -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_BEFORE_OBSERVE_RECOVERY'
        $install = Invoke-CutoverInstaller -Context $Context -ScriptPath $Context.installer_path -RequestedAction InstallFromDisabledNoStop -RequestedMode Observe -ExpectedCurrentTaskXmlSha256 $xml.utf8_text_sha256
        $null = Assert-CutoverInstallerStatus -Status $install -Context $Context -ExpectedMode Observe -ExpectedTaskState Ready -AllowInheritedTaskResult $true
        $ready = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode Observe -ExpectedTaskState Ready -AllowInheritedTaskResult $true
        $observeText = Export-CutoverTaskXml
        $observe = Get-CutoverTaskXmlEvidence -Text $observeText
        if (-not $observe.enabled) { Throw-Cutover -Code 'RECOVERY_OBSERVE_TASK_NOT_ENABLED' }
        $boundary = Assert-CutoverFutureObserveDefinition -Text $observeText -Context $Context
        Assert-CutoverBackupMatchesExpectedXml -Context $Context -ExpectedUtf8TextSha256 $xml.utf8_text_sha256 -ExpectedUtf16LeBomSha256 $xml.utf16le_bom_sha256
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_DURING_OBSERVE_RECOVERY' }
        $final = Get-CutoverExactInstallerStatus -Context $Context -ScriptPath $Context.installer_path -ExpectedMode Observe -ExpectedTaskState Ready -AllowInheritedTaskResult $true
        $finalXml = Get-CutoverTaskXmlEvidence -Text (Export-CutoverTaskXml)
        if ($ready.last_run_utc.Ticks -ne $initial.last_run_utc.Ticks -or $final.last_run_utc.Ticks -ne $initial.last_run_utc.Ticks -or
            [int64]$ready.raw.last_task_result -ne 20 -or [int64]$final.raw.last_task_result -ne 20) { Throw-Cutover -Code 'TASK_RAN_DURING_OBSERVE_RECOVERY' }
        if (-not $finalXml.enabled -or $finalXml.normalized_sha256 -cne $observe.normalized_sha256) { Throw-Cutover -Code 'TASK_XML_CHANGED_BEFORE_OBSERVE_DRAIN' }
        if ((Get-CutoverProtectedSnapshot -Context $Context) -cne $protected) { Throw-Cutover -Code 'PROTECTED_CHANGED_DURING_OBSERVE_RECOVERY' }
        Assert-CutoverFileCheckpointUnchanged -Before $state.checkpoint -Path $Context.state_path -MaximumBytes $script:CutoverStateMaximumBytes -Code 'STATE_CHANGED_DURING_OBSERVE_RECOVERY'
        Assert-CutoverFileCheckpointUnchanged -Before $log -Path $Context.log_path -MaximumBytes $script:CutoverLogMaximumBytes -Code 'LOG_CHANGED_DURING_OBSERVE_RECOVERY'
        Assert-CutoverTriggerWindow -NextRunUtc $final.next_run_utc -RequiredSeconds ($Context.timeout_seconds + $Context.natural_trigger_margin_seconds)
        $receipt = [pscustomobject][ordered]@{
            schema=$script:CutoverSchema;ok=$true;action=$receiptAction;operation_id=$Context.operation_id
            status='OBSERVE_READY_INHERITED_ERROR';release_id=$Context.release_id;pre_task_state='Disabled';pre_mode=$preMode;post_task_state='Ready';post_mode='Observe'
            failed_run_id=$failed.run_id;inherited_task_result=20;last_run_utc=$failed.last_run_utc.ToString('o')
            authenticated_disabled_xml_sha256=$xml.utf8_text_sha256;observe_xml_sha256=$finalXml.utf8_text_sha256;observe_start_boundary_utc=$boundary.ToString('o')
            backup_matches_authenticated_disabled_xml=$true;state_and_log_preserved=$true;protected_fingerprints_unchanged=$true
            old_execute_task_not_enabled=($preMode -ceq 'Execute');task_started=$false;task_stopped=$false;state_not_restored=$true;worker_health_not_yet_confirmed=$true
        }
        $null = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        return $receipt
    } catch {
        Throw-CutoverAfterCleanup -FailureRecord $_ -Context $Context -Installer $Context.installer_path -ExpectedModes @('Observe','Execute')
    }
}

function Invoke-CutoverInstallObserveReadyFromDisabledObserveHttpError {
    param([Parameter(Mandatory = $true)]$Context)
    # This is intentionally a separate admission action: it can only continue
    # a fail-closed Observe recovery after the same authenticated pre-admission
    # board-read failure. It never enables, starts, drains, or replays Execute.
    $Context | Add-Member -NotePropertyName expected_recovery_pre_mode -NotePropertyValue 'Observe' -Force
    return Invoke-CutoverInstallObserveReadyFromDisabledHttpError -Context $Context
}

function New-CutoverContext {
    param(
        [Parameter(Mandatory = $true)][string]$RequestedAction,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )
    $disabledInput = ''
    if ($Values.Contains('ExpectedDisabledXmlSha256')) { $disabledInput = [string]$Values.ExpectedDisabledXmlSha256 }
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
        Throw-Cutover -Code 'WINDOWS_POWERSHELL_5_1_REQUIRED'
    }
    if ($Values.OperationId -cnotmatch '^[0-9a-f]{32}$') { Throw-Cutover -Code 'OPERATION_ID_INVALID' }
    $modeValue = if ($Values.Mode -ieq 'Execute') { 'Execute' } elseif ($Values.Mode -ieq 'Observe') { 'Observe' } else { Throw-Cutover -Code 'MODE_INVALID' }
    $context = [pscustomobject][ordered]@{
        action = $RequestedAction
        operation_id = $Values.OperationId
        mode = $modeValue
        user_profile_path = (Resolve-CutoverAbsolutePath -Value $Values.UserProfilePath -Code 'USER_PROFILE_PATH_INVALID' -ForbidVolumeRoot)
        workspace_path = (Resolve-CutoverAbsolutePath -Value $Values.WorkspacePath -Code 'WORKSPACE_PATH_INVALID' -ForbidVolumeRoot)
        env_file = (Resolve-CutoverAbsolutePath -Value $Values.EnvFile -Code 'ENV_FILE_PATH_INVALID' -ForbidVolumeRoot)
        state_path = (Resolve-CutoverAbsolutePath -Value $Values.StatePath -Code 'STATE_PATH_INVALID' -ForbidVolumeRoot)
        log_path = (Resolve-CutoverAbsolutePath -Value $Values.LogPath -Code 'LOG_PATH_INVALID' -ForbidVolumeRoot)
        claude_command = (Resolve-CutoverAbsolutePath -Value $Values.ClaudeCommand -Code 'CLAUDE_PATH_INVALID' -ForbidVolumeRoot)
        wall_timeout_seconds = (ConvertTo-CutoverInteger -Value $Values.WallTimeoutSeconds -Minimum 30 -Maximum 840 -Code 'WALL_TIMEOUT_SECONDS_INVALID')
        max_runs = (ConvertTo-CutoverInteger -Value $Values.MaxRuns -Minimum 1 -Maximum 20 -Code 'MAX_RUNS_INVALID')
        timeout_seconds = (ConvertTo-CutoverInteger -Value $Values.TimeoutSeconds -Minimum 30 -Maximum 780 -Code 'TIMEOUT_SECONDS_INVALID')
        poll_milliseconds = (ConvertTo-CutoverInteger -Value $Values.PollMilliseconds -Minimum 100 -Maximum 5000 -Code 'POLL_MILLISECONDS_INVALID')
        natural_trigger_margin_seconds = (ConvertTo-CutoverInteger -Value $Values.NaturalTriggerMarginSeconds -Minimum 30 -Maximum 300 -Code 'NATURAL_TRIGGER_MARGIN_INVALID')
        installer_path = ''
        release_id = $Values.ExpectedReleaseId
        restored_installer_path = ''
        escrow_tool_path = ''
        expected_escrow_tool_sha256 = $Values.ExpectedEscrowToolSha256
        escrow_id = $Values.EscrowId
        escrow_path = ''
        escrow_release_id = $Values.ExpectedEscrowReleaseId
        expected_installer_sha256 = $Values.ExpectedInstallerSha256
        expected_restored_installer_sha256 = $Values.ExpectedRestoredInstallerSha256
        git_path = $Values.GitPath
        expected_git_sha256 = $Values.ExpectedGitSha256
        expected_claude_sha256 = $Values.ExpectedClaudeSha256
        expected_terminal_status = $Values.ExpectedTerminalStatus
        expected_work_id = $Values.ExpectedWorkId
        expected_row_id = $Values.ExpectedRowId
        expected_result_status = $Values.ExpectedResultStatus
        expected_current_task_result = 0
        expected_current_failure_code = ''
        expected_current_run_id = ''
        expected_disabled_xml_sha256 = ''
        expected_current_release_id = ''
        expected_current_xml_sha256 = ''
        metadata_root = ''
    }
    $programData = Resolve-CutoverAbsolutePath -Value $env:ProgramData -Code 'PROGRAM_DATA_INVALID' -ForbidVolumeRoot
    $context.metadata_root = [IO.Path]::GetFullPath((Join-Path $programData $script:CutoverMetadataRelativePath)).TrimEnd('\', '/')
    Assert-CutoverSafeDirectory -Path $context.user_profile_path -MissingCode 'USER_PROFILE_MISSING' -UnsafeCode 'USER_PROFILE_UNSAFE'
    Assert-CutoverSafeDirectory -Path $context.workspace_path -MissingCode 'WORKSPACE_MISSING' -UnsafeCode 'WORKSPACE_UNSAFE'

    if (@(
        'ValidateEscrowAndDisable',
        'InstallObserveAndDrain',
        'InstallObserveAndDrainFromFailedExecute',
        'InstallObserveReadyFromReadyObserve',
        'RestoreReady'
    ) -ccontains $RequestedAction) {
        if ($Values.EscrowId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' -or
            @('.', '..') -ccontains $Values.EscrowId) { Throw-Cutover -Code 'ESCROW_ID_INVALID' }
        if ($Values.ExpectedEscrowReleaseId -cnotmatch '^[0-9a-f]{40}$') { Throw-Cutover -Code 'ESCROW_RELEASE_ID_INVALID' }
        $context.escrow_tool_path = Resolve-CutoverEscrowTool -Path $Values.EscrowToolPath -ExpectedSha256 $Values.ExpectedEscrowToolSha256
        $context.escrow_path = [IO.Path]::GetFullPath((Join-Path $context.metadata_root ('acceptance\' + $Values.EscrowId))).TrimEnd('\', '/')
    }
    if (@(
        'ValidateEscrowAndDisable',
        'DrainObserve',
        'InstallObserveAndDrain',
        'InstallObserveAndDrainFromFailedExecute',
        'InstallExecuteReady',
        'InstallObserveReadyFromReadyObserve',
        'RestoreReady',
        'StartAndAwait',
        'InstallObserveAndDrainFromDisabledExecute',
        'InstallObserveReadyFromDisabledHttpError',
        'InstallObserveReadyFromDisabledObserveHttpError'
    ) -ccontains $RequestedAction) {
        $context.installer_path = Resolve-CutoverInstaller -Path $Values.InstallerPath -ExpectedSha256 $Values.ExpectedInstallerSha256 -ReleaseId $Values.ExpectedReleaseId -Prefix 'INSTALLER'
    }
    $currentReleaseInput = [string]$Values['ExpectedCurrentReleaseId']
    $currentXmlInput = [string]$Values['ExpectedCurrentXmlSha256']
    if ($RequestedAction -ceq 'InstallObserveReadyFromReadyObserve') {
        if ($context.mode -cne 'Observe') { Throw-Cutover -Code 'ACTION_REQUIRES_OBSERVE_MODE' }
        if ($currentReleaseInput -cnotmatch '^[0-9a-f]{40}$' -or $currentReleaseInput -ceq $context.release_id) { Throw-Cutover -Code 'OBSERVE_REPLACEMENT_RELEASE_INVALID' }
        if (($context.timeout_seconds + $context.natural_trigger_margin_seconds + 120) -ge 900) { Throw-Cutover -Code 'OBSERVE_RECOVERY_WINDOW_CANNOT_FIT_INTERVAL' }
        if ($currentXmlInput -cnotmatch '^[0-9a-f]{64}$') { Throw-Cutover -Code 'EXPECTED_CURRENT_XML_SHA256_INVALID' }
        if ($context.expected_terminal_status -or $context.expected_work_id -or $context.expected_row_id -or $context.expected_result_status) { Throw-Cutover -Code 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY' }
        $context.expected_current_release_id = $currentReleaseInput
        $context.expected_current_xml_sha256 = $currentXmlInput
        $context.restored_installer_path = Resolve-CutoverInstaller -Path $Values.RestoredInstallerPath -ExpectedSha256 $Values.ExpectedRestoredInstallerSha256 -ReleaseId $currentReleaseInput -Prefix 'CURRENT_INSTALLER'
    } elseif ($currentReleaseInput -or $currentXmlInput) { Throw-Cutover -Code 'CURRENT_OBSERVE_INPUTS_ACTION_MISMATCH' }
    if (@('InstallObserveAndDrain', 'InstallObserveAndDrainFromFailedExecute', 'RestoreReady') -ccontains $RequestedAction) {
        $context.restored_installer_path = Resolve-CutoverInstaller `
            -Path $Values.RestoredInstallerPath `
            -ExpectedSha256 $Values.ExpectedRestoredInstallerSha256 `
            -ReleaseId $Values.ExpectedEscrowReleaseId `
            -Prefix 'RESTORED_INSTALLER'
    }
    if (@('DrainObserve', 'InstallObserveAndDrain', 'InstallObserveAndDrainFromFailedExecute') -ccontains $RequestedAction -and $context.mode -cne 'Observe') {
        Throw-Cutover -Code 'ACTION_REQUIRES_OBSERVE_MODE'
    }
    if ($RequestedAction -ceq 'InstallObserveAndDrainFromFailedExecute') {
        $context.expected_current_task_result = ConvertTo-CutoverInteger `
            -Value ([string]$Values.ExpectedCurrentTaskResult) `
            -Minimum 20 `
            -Maximum 20 `
            -Code 'EXPECTED_CURRENT_TASK_RESULT_INVALID'
        $context.expected_current_failure_code = Get-CutoverExpectedFailedExecuteCode `
            -Value ([string]$Values.ExpectedCurrentFailureCode)
        if ([string]$Values.ExpectedCurrentRunId -cnotmatch '^[0-9a-f]{32}$') {
            Throw-Cutover -Code 'EXPECTED_CURRENT_RUN_ID_INVALID'
        }
        $context.expected_current_run_id = [string]$Values.ExpectedCurrentRunId
        if ([string]$context.release_id -ceq [string]$context.escrow_release_id) {
            Throw-Cutover -Code 'FAILED_EXECUTE_RELEASE_NOT_ADVANCED'
        }
    } elseif (@('InstallObserveReadyFromDisabledHttpError','InstallObserveReadyFromDisabledObserveHttpError') -ccontains $RequestedAction) {
        $context.expected_current_task_result = ConvertTo-CutoverInteger -Value ([string]$Values.ExpectedCurrentTaskResult) -Minimum 20 -Maximum 20 -Code 'EXPECTED_CURRENT_TASK_RESULT_INVALID'
        if (@('BOARD_READ_HTTP_ERROR','BOARD_ROWS_MISSING') -cnotcontains [string]$Values.ExpectedCurrentFailureCode) { Throw-Cutover -Code 'EXPECTED_CURRENT_FAILURE_CODE_INVALID' }
        if ([string]$Values.ExpectedCurrentRunId -cnotmatch '^[0-9a-f]{32}$') { Throw-Cutover -Code 'EXPECTED_CURRENT_RUN_ID_INVALID' }
        $context.expected_current_failure_code = [string]$Values.ExpectedCurrentFailureCode
        $context.expected_current_run_id = [string]$Values.ExpectedCurrentRunId
    } elseif (-not [string]::IsNullOrEmpty([string]$Values.ExpectedCurrentTaskResult) -or
              -not [string]::IsNullOrEmpty([string]$Values.ExpectedCurrentFailureCode) -or
              -not [string]::IsNullOrEmpty([string]$Values.ExpectedCurrentRunId)) {
        Throw-Cutover -Code 'FAILED_EXECUTE_INPUTS_ACTION_MISMATCH'
    }
    if ($RequestedAction -ceq 'ValidateEscrowAndDisable' -and $context.mode -cne 'Execute') {
        Throw-Cutover -Code 'ACTION_REQUIRES_EXECUTE_MODE'
    }
    if ($RequestedAction -ceq 'InstallExecuteReady' -and $context.mode -cne 'Execute') {
        Throw-Cutover -Code 'ACTION_REQUIRES_EXECUTE_MODE'
    }
    if ($RequestedAction -ceq 'StartAndAwait') {
        if ($context.mode -cne 'Execute') { Throw-Cutover -Code 'ACTION_REQUIRES_EXECUTE_MODE' }
        if (@('result_confirmed', 'no_eligible_order') -cnotcontains $Values.ExpectedTerminalStatus) {
            Throw-Cutover -Code 'EXPECTED_TERMINAL_STATUS_INVALID'
        }
        if ($Values.ExpectedTerminalStatus -ceq 'result_confirmed') {
            if ($context.mode -cne 'Execute') { Throw-Cutover -Code 'RESULT_CONFIRMATION_REQUIRES_EXECUTE' }
            if ($Values.ExpectedWorkId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$') { Throw-Cutover -Code 'EXPECTED_WORK_ID_INVALID' }
            $rowGuid = [Guid]::Empty
            if (-not [Guid]::TryParseExact($Values.ExpectedRowId, 'D', [ref]$rowGuid)) { Throw-Cutover -Code 'EXPECTED_ROW_ID_INVALID' }
            if (@('completed', 'blocked', 'rejected') -cnotcontains $Values.ExpectedResultStatus) { Throw-Cutover -Code 'EXPECTED_RESULT_STATUS_INVALID' }
        } elseif (-not [string]::IsNullOrEmpty($Values.ExpectedWorkId) -or
                  -not [string]::IsNullOrEmpty($Values.ExpectedRowId) -or
                  -not [string]::IsNullOrEmpty($Values.ExpectedResultStatus)) {
            Throw-Cutover -Code 'NO_ELIGIBLE_IDENTITY_MUST_BE_EMPTY'
        }
    }
    if (@('InstallObserveAndDrainFromDisabledExecute','InstallObserveReadyFromDisabledHttpError','InstallObserveReadyFromDisabledObserveHttpError') -ccontains $RequestedAction) {
        if ($context.mode -cne 'Observe') { Throw-Cutover -Code 'ACTION_REQUIRES_OBSERVE_MODE' }
        if ($disabledInput -cnotmatch '^[0-9a-f]{64}$') { Throw-Cutover -Code 'EXPECTED_DISABLED_XML_SHA256_INVALID' }
        if ($RequestedAction -ceq 'InstallObserveAndDrainFromDisabledExecute') {
            if ([string]::IsNullOrEmpty([string]$Values.ExpectedTerminalStatus)) {
                if (-not [string]::IsNullOrEmpty($Values.ExpectedWorkId) -or
                    -not [string]::IsNullOrEmpty($Values.ExpectedRowId) -or
                    -not [string]::IsNullOrEmpty($Values.ExpectedResultStatus)) {
                    Throw-Cutover -Code 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY'
                }
            } elseif ([string]$Values.ExpectedTerminalStatus -ceq 'result_confirmed') {
                if ($Values.ExpectedWorkId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$') { Throw-Cutover -Code 'EXPECTED_WORK_ID_INVALID' }
                $rowGuid = [Guid]::Empty
                if (-not [Guid]::TryParseExact($Values.ExpectedRowId, 'D', [ref]$rowGuid)) { Throw-Cutover -Code 'EXPECTED_ROW_ID_INVALID' }
                if (@('completed', 'blocked', 'rejected') -cnotcontains $Values.ExpectedResultStatus) { Throw-Cutover -Code 'EXPECTED_RESULT_STATUS_INVALID' }
            } else {
                Throw-Cutover -Code 'EXPECTED_TERMINAL_STATUS_INVALID'
            }
        } elseif ($context.expected_terminal_status -or $context.expected_work_id -or $context.expected_row_id -or $context.expected_result_status) {
            Throw-Cutover -Code 'OBSERVE_RECOVERY_FORBIDS_TARGET_IDENTITY'
        }
        if (($context.timeout_seconds + $context.natural_trigger_margin_seconds + 120) -ge 900) { Throw-Cutover -Code 'OBSERVE_RECOVERY_WINDOW_CANNOT_FIT_INTERVAL' }
        Assert-CutoverDisabledObserveRecoveryLimits -Context $context
        $context.expected_disabled_xml_sha256 = $disabledInput
    } elseif ($disabledInput) { Throw-Cutover -Code 'DISABLED_RECOVERY_INPUTS_ACTION_MISMATCH' }
    return $context
}

function Invoke-OrderCutoverPhase {
    param(
        [AllowEmptyString()][string]$RequestedAction,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )
    $canonicalAction = Get-CutoverSafeAction -Value $RequestedAction
    if ($canonicalAction -ceq 'Invalid') { Throw-Cutover -Code 'ACTION_INVALID' }
    $context = New-CutoverContext -RequestedAction $canonicalAction -Values $Values
    switch ($canonicalAction) {
        'ValidateEscrowAndDisable' { return Invoke-CutoverValidateEscrowAndDisable -Context $context }
        'DrainObserve' { return Invoke-CutoverDrainObserveAction -Context $context }
        'InstallObserveAndDrain' { return Invoke-CutoverInstallObserveAndDrain -Context $context }
        'InstallObserveAndDrainFromFailedExecute' { return Invoke-CutoverInstallObserveAndDrainFromFailedExecute -Context $context }
        'InstallExecuteReady' { return Invoke-CutoverInstallExecuteReady -Context $context }
        'InstallObserveReadyFromReadyObserve' { return Invoke-CutoverInstallObserveReadyFromReadyObserve -Context $context -CurrentInstallerPath $context.restored_installer_path -ExpectedCurrentInstallerSha256 $context.expected_restored_installer_sha256 -ExpectedCurrentReleaseId $context.expected_current_release_id -ExpectedCurrentXmlSha256 $context.expected_current_xml_sha256 }
        'RestoreReady' { return Invoke-CutoverRestoreReady -Context $context }
        'StartAndAwait' { return Invoke-CutoverStartAndAwait -Context $context }
        'InstallObserveAndDrainFromDisabledExecute' { return Invoke-CutoverInstallObserveAndDrainFromDisabledExecute -Context $context }
        'InstallObserveReadyFromDisabledHttpError' { return Invoke-CutoverInstallObserveReadyFromDisabledHttpError -Context $context }
        'InstallObserveReadyFromDisabledObserveHttpError' { return Invoke-CutoverInstallObserveReadyFromDisabledObserveHttpError -Context $context }
    }
    Throw-Cutover -Code 'ACTION_INVALID'
}

function ConvertTo-CutoverBoundedReceipt {
    param([Parameter(Mandatory = $true)]$Receipt)
    try { $json = $Receipt | ConvertTo-Json -Depth 8 -Compress }
    catch { Throw-Cutover -Code 'RECEIPT_SERIALIZATION_FAILED' }
    if ([Text.Encoding]::UTF8.GetByteCount($json) -gt $script:CutoverReceiptMaximumBytes) {
        Throw-Cutover -Code 'RECEIPT_TOO_LARGE'
    }
    return $json
}

function New-CutoverFailureReceipt {
    param(
        [AllowEmptyString()][string]$ActionValue,
        [AllowEmptyString()][string]$OperationIdValue,
        [Parameter(Mandatory = $true)]$FailureRecord
    )
    $fields = [ordered]@{
        schema = $script:CutoverSchema
        ok = $false
        action = (Get-CutoverSafeAction -Value $ActionValue)
        operation_id = (Get-CutoverSafeOperationId -Value $OperationIdValue)
        status = 'FAILED'
        code = (Get-CutoverSafeErrorCode -Record $FailureRecord)
    }
    $data = $FailureRecord.Exception.Data
    if ($null -ne $data -and $data.Contains('original_code') -and
        [string]$data['original_code'] -cmatch '^[A-Z][A-Z0-9_]{0,95}$') {
        $fields.original_code = [string]$data['original_code']
    }
    if ($null -ne $data -and $data.Contains('cleanup_status') -and
        @('DISABLED_VERIFIED', 'FAILED') -ccontains [string]$data['cleanup_status']) {
        $fields.cleanup_status = [string]$data['cleanup_status']
    }
    if ($null -ne $data -and $data.Contains('cleanup_mode') -and
        @('Observe', 'Execute') -ccontains [string]$data['cleanup_mode']) {
        $fields.cleanup_mode = [string]$data['cleanup_mode']
    }
    if ($null -ne $data -and $data.Contains('cleanup_code') -and
        [string]$data['cleanup_code'] -cmatch '^[A-Z][A-Z0-9_]{0,95}$') {
        $fields.cleanup_code = [string]$data['cleanup_code']
    }
    foreach ($booleanName in @('future_triggers_disabled', 'cleanup_definition_preserved', 'cleanup_task_stopped')) {
        if ($null -ne $data -and $data.Contains($booleanName) -and $data[$booleanName] -is [bool]) {
            $fields[$booleanName] = [bool]$data[$booleanName]
        }
    }
    if ($null -ne $data -and $data.Contains('cleanup_disable_performed') -and
        $data['cleanup_disable_performed'] -is [bool]) {
        $fields.cleanup_disable_performed = [bool]$data['cleanup_disable_performed']
    }
    foreach ($stateName in @('cleanup_initial_task_state', 'cleanup_final_task_state')) {
        if ($null -ne $data -and $data.Contains($stateName) -and
            @('Ready', 'Running', 'Queued', 'Disabled') -ccontains [string]$data[$stateName]) {
            $fields[$stateName] = [string]$data[$stateName]
        }
    }
    if ($null -ne $data -and $data.Contains('active_instance_status') -and
        @('PERSISTED', 'NONE') -ccontains [string]$data['active_instance_status']) {
        $fields.active_instance_status = [string]$data['active_instance_status']
    }
    return [pscustomobject]$fields
}

if ($MyInvocation.InvocationName -cne '.') {
    $values = @{
        OperationId = $OperationId
        EscrowToolPath = $EscrowToolPath
        ExpectedEscrowToolSha256 = $ExpectedEscrowToolSha256
        EscrowId = $EscrowId
        ExpectedEscrowReleaseId = $ExpectedEscrowReleaseId
        InstallerPath = $InstallerPath
        ExpectedInstallerSha256 = $ExpectedInstallerSha256
        ExpectedReleaseId = $ExpectedReleaseId
        RestoredInstallerPath = $RestoredInstallerPath
        ExpectedRestoredInstallerSha256 = $ExpectedRestoredInstallerSha256
        Mode = $Mode
        UserProfilePath = $UserProfilePath
        WorkspacePath = $WorkspacePath
        EnvFile = $EnvFile
        StatePath = $StatePath
        LogPath = $LogPath
        ClaudeCommand = $ClaudeCommand
        WallTimeoutSeconds = $WallTimeoutSeconds
        MaxRuns = $MaxRuns
        TimeoutSeconds = $TimeoutSeconds
        PollMilliseconds = $PollMilliseconds
        NaturalTriggerMarginSeconds = $NaturalTriggerMarginSeconds
        ExpectedTerminalStatus = $ExpectedTerminalStatus
        ExpectedWorkId = $ExpectedWorkId
        ExpectedRowId = $ExpectedRowId
        ExpectedResultStatus = $ExpectedResultStatus
        ExpectedCurrentTaskResult = $ExpectedCurrentTaskResult
        ExpectedCurrentFailureCode = $ExpectedCurrentFailureCode
        ExpectedCurrentRunId = $ExpectedCurrentRunId
        ExpectedDisabledXmlSha256 = $ExpectedDisabledXmlSha256
        ExpectedCurrentReleaseId = $ExpectedCurrentReleaseId
        ExpectedCurrentXmlSha256 = $ExpectedCurrentXmlSha256
        GitPath = $GitPath
        ExpectedGitSha256 = $ExpectedGitSha256
        ExpectedClaudeSha256 = $ExpectedClaudeSha256
    }
    try {
        $receipt = Invoke-OrderCutoverPhase -RequestedAction $Action -Values $values
        $json = ConvertTo-CutoverBoundedReceipt -Receipt $receipt
        [Console]::Out.WriteLine($json)
        exit 0
    } catch {
        $failure = New-CutoverFailureReceipt `
            -ActionValue $Action `
            -OperationIdValue $OperationId `
            -FailureRecord $_
        try { $json = ConvertTo-CutoverBoundedReceipt -Receipt $failure }
        catch { $json = '{"schema":"blackboard.order-cutover-phase-receipt.v1","ok":false,"action":"Invalid","operation_id":"invalid","status":"FAILED","code":"RECEIPT_SERIALIZATION_FAILED"}' }
        [Console]::Out.WriteLine($json)
        exit 1
    }
}
