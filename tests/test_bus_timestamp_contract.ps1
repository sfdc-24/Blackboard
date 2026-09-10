#Requires -Version 5.1
[CmdletBinding()]
param([switch]$KeepArtifacts)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Passed = 0
$script:Failed = 0
$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:BusPath = Join-Path $script:RepoRoot 'scripts\bus.ps1'
$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('bus-timestamp-contract-' + [Guid]::NewGuid().ToString('N'))
$script:FakeSecret = 'TEST_BUS_SECRET_DO_NOT_LEAK_9fc71a'
$script:PayloadSecret = 'ghp_TEST_PAYLOAD_DO_NOT_LEAK_8d331b'

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

function Write-TestUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )

    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-TestPort {
    $probe = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    try {
        $probe.Start()
        return [int]$probe.LocalEndpoint.Port
    } finally {
        $probe.Stop()
    }
}

function Start-TransportTrap {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ReadyPath,
        [Parameter(Mandatory = $true)][string]$CapturePath
    )

    $job = Start-Job -ArgumentList $Port, $ReadyPath, $CapturePath -ScriptBlock {
        param($ListenPort, $ReadyFile, $CaptureFile)
        $ErrorActionPreference = 'Stop'
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, [int]$ListenPort)
        $client = $null
        try {
            $listener.Start()
            [IO.File]::WriteAllText($ReadyFile, 'ready', (New-Object Text.UTF8Encoding($false)))
            $client = $listener.AcceptTcpClient()
            $client.ReceiveTimeout = 10000
            $stream = $client.GetStream()
            $headerBytes = New-Object 'System.Collections.Generic.List[byte]'
            while ($true) {
                $nextByte = $stream.ReadByte()
                if ($nextByte -lt 0) { throw 'test_transport_trap_header_ended_early' }
                $headerBytes.Add([byte]$nextByte)
                if ($headerBytes.Count -gt 65536) { throw 'test_transport_trap_header_too_large' }
                if ($headerBytes.Count -ge 4 -and
                    $headerBytes[$headerBytes.Count - 4] -eq 13 -and
                    $headerBytes[$headerBytes.Count - 3] -eq 10 -and
                    $headerBytes[$headerBytes.Count - 2] -eq 13 -and
                    $headerBytes[$headerBytes.Count - 1] -eq 10) {
                    break
                }
            }
            $headerText = [Text.Encoding]::ASCII.GetString($headerBytes.ToArray())
            $contentLength = 0
            if ($headerText -match '(?im)^Content-Length:\s*([0-9]+)\s*$') {
                $contentLength = [int]$matches[1]
            }
            $buffer = New-Object byte[] $contentLength
            $readTotal = 0
            while ($readTotal -lt $contentLength) {
                $readNow = $stream.Read($buffer, $readTotal, $contentLength - $readTotal)
                if ($readNow -le 0) { break }
                $readTotal += $readNow
            }
            if ($readTotal -ne $contentLength) { throw 'test_transport_trap_body_ended_early' }
            $body = [Text.Encoding]::UTF8.GetString($buffer, 0, $readTotal)
            [IO.File]::WriteAllText($CaptureFile, $body, (New-Object Text.UTF8Encoding($false)))

            $responseBody = '{"ok":true}'
            $responseBytes = [Text.Encoding]::UTF8.GetBytes($responseBody)
            $responseHead = "HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($responseBytes.Length)`r`nConnection: close`r`n`r`n"
            $responseHeadBytes = [Text.Encoding]::ASCII.GetBytes($responseHead)
            $stream.Write($responseHeadBytes, 0, $responseHeadBytes.Length)
            $stream.Write($responseBytes, 0, $responseBytes.Length)
            $stream.Flush()
        } finally {
            if ($client) { $client.Close() }
            $listener.Stop()
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $ReadyPath -PathType Leaf) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    if (-not (Test-Path -LiteralPath $ReadyPath -PathType Leaf)) {
        $details = (@(Receive-Job -Job $job -Keep -ErrorAction SilentlyContinue) -join "`n")
        Stop-Job -Job $job -ErrorAction SilentlyContinue
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        throw ('test_transport_trap_start_failed' + $(if ($details) { ': ' + $details } else { '' }))
    }
    return $job
}

function Release-TransportTrap {
    param([Parameter(Mandatory = $true)][int]$Port)

    $client = New-Object Net.Sockets.TcpClient
    try {
        $client.ReceiveTimeout = 5000
        $client.Connect([Net.IPAddress]::Loopback, $Port)
        $stream = $client.GetStream()
        $request = [Text.Encoding]::ASCII.GetBytes("GET /test-release HTTP/1.1`r`nHost: 127.0.0.1`r`nConnection: close`r`n`r`n")
        $stream.Write($request, 0, $request.Length)
        $stream.Flush()
        $oneByte = New-Object byte[] 1
        try { [void]$stream.Read($oneByte, 0, 1) } catch { }
    } finally {
        $client.Close()
    }
}

function New-TestRowJson {
    param(
        [Parameter(Mandatory = $true)][string]$Timestamp,
        [int]$CellCount = 10,
        [string]$Envelope = '',
        [string]$Gist = ''
    )

    if (-not $Envelope) {
        $Envelope = 'BCB|v=1|id=TEST-TIMESTAMP|phase=RESULT|hold=' + $script:PayloadSecret
    }
    if (-not $Gist) { $Gist = 'credential=' + $script:PayloadSecret }
    $cells = @(
        'TEST-TIMESTAMP',
        $Timestamp,
        'codex',
        'claude-code-cli',
        'APPEND',
        $Envelope,
        'DONE',
        'BUS-TIMESTAMP-CONTRACT',
        $Gist,
        ''
    )
    if ($CellCount -lt $cells.Count) {
        $cells = @($cells[0..($CellCount - 1)])
    }
    return (ConvertTo-Json -InputObject @($cells) -Compress)
}

function Invoke-BusCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$RowJson
    )

    $caseRoot = Join-Path $script:TestRoot $Name
    New-Item -ItemType Directory -Path $caseRoot -Force | Out-Null
    $readyPath = Join-Path $caseRoot 'ready.txt'
    $capturePath = Join-Path $caseRoot 'request.json'
    $envPath = Join-Path $caseRoot 'test.env'
    $port = Get-TestPort
    Write-TestUtf8 -Path $envPath -Text ("BUS_URL=http://127.0.0.1:$port/`nBUS_SECRET=$($script:FakeSecret)`n")
    $job = Start-TransportTrap -Port $port -ReadyPath $readyPath -CapturePath $capturePath

    $records = @()
    $errorMessage = ''
    try {
        $records = @(& $script:BusPath -Action append -Title 'Blackboard - Alpha DB' -SheetRowJson $RowJson -EnvFile $envPath *>&1)
    } catch {
        $errorMessage = [string]$_.Exception.Message
    }

    Start-Sleep -Milliseconds 150
    if (-not (Test-Path -LiteralPath $capturePath -PathType Leaf)) {
        # A rejected row correctly leaves the server waiting. Send a bodyless
        # test-only request so the background listener exits without Stop-Job
        # having to interrupt a blocking AcceptTcpClient call.
        Release-TransportTrap -Port $port
    }
    Wait-Job -Job $job -Timeout 5 | Out-Null
    $captured = Test-Path -LiteralPath $capturePath -PathType Leaf
    $body = if ($captured) { [IO.File]::ReadAllText($capturePath, [Text.Encoding]::UTF8) } else { '' }
    $rendered = ((@($records | ForEach-Object { [string]$_ }) + @($errorMessage)) -join "`n").Trim()

    Stop-Job -Job $job -ErrorAction SilentlyContinue
    Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    return [pscustomobject][ordered]@{
        ErrorMessage = $errorMessage
        Output = $rendered
        TransportObserved = [bool]($captured -and $body.Length -gt 0)
        RequestBody = $body
    }
}

New-Item -ItemType Directory -Path $script:TestRoot -Force | Out-Null
try {
    $clock = [DateTime]::UtcNow.AddMinutes(-1)
    $accepted = @(
        [pscustomobject]@{ Name = 'canonical-seconds'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture) },
        [pscustomobject]@{ Name = 'canonical-one-fraction'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.fZ', [Globalization.CultureInfo]::InvariantCulture) },
        [pscustomobject]@{ Name = 'canonical-two-fractions'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.ffZ', [Globalization.CultureInfo]::InvariantCulture) },
        [pscustomobject]@{ Name = 'canonical-three-fractions'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture) },
        [pscustomobject]@{ Name = 'canonical-four-fractions'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.ffffZ', [Globalization.CultureInfo]::InvariantCulture) },
        [pscustomobject]@{ Name = 'canonical-five-fractions'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.fffffZ', [Globalization.CultureInfo]::InvariantCulture) },
        [pscustomobject]@{ Name = 'canonical-six-fractions'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ', [Globalization.CultureInfo]::InvariantCulture) },
        [pscustomobject]@{ Name = 'canonical-seven-fractions'; Timestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ', [Globalization.CultureInfo]::InvariantCulture) }
    )
    foreach ($case in $accepted) {
        $result = Invoke-BusCase -Name $case.Name -RowJson (New-TestRowJson -Timestamp $case.Timestamp)
        Assert-True ($case.Name + ' accepted') ($result.ErrorMessage.Length -eq 0) $result.ErrorMessage
        Assert-True ($case.Name + ' reached transport') $result.TransportObserved
        Assert-True ($case.Name + ' preserved byte-exact timestamp') ($result.RequestBody.Contains('"' + $case.Timestamp + '"'))
    }

    $withinSkewTimestamp = [DateTime]::UtcNow.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture)
    $withinSkewResult = Invoke-BusCase -Name 'within-future-skew' -RowJson (New-TestRowJson -Timestamp $withinSkewTimestamp)
    Assert-True 'timestamp within future-skew allowance accepted' ($withinSkewResult.ErrorMessage.Length -eq 0) $withinSkewResult.ErrorMessage
    Assert-True 'timestamp within future-skew allowance reaches transport' $withinSkewResult.TransportObserved
    Assert-True 'timestamp within future-skew allowance preserved' ($withinSkewResult.RequestBody.Contains('"' + $withinSkewTimestamp + '"'))

    $previousCulture = [Threading.Thread]::CurrentThread.CurrentCulture
    try {
        [Threading.Thread]::CurrentThread.CurrentCulture = [Globalization.CultureInfo]::GetCultureInfo('fr-FR')
        $dateTimeCultureValue = [string]([DateTime]::UtcNow)
    } finally {
        [Threading.Thread]::CurrentThread.CurrentCulture = $previousCulture
    }
    $unicodeDigitTimestamp = (@(
        0x0662, 0x0660, 0x0662, 0x0666, 0x002D, 0x0660, 0x0669, 0x002D, 0x0661, 0x0660,
        0x0054, 0x0660, 0x0663, 0x003A, 0x0660, 0x0660, 0x003A, 0x0662, 0x0666, 0x005A
    ) | ForEach-Object { [char]$_ }) -join ''
    $rejected = @(
        [pscustomobject]@{ Name = 'locale'; Timestamp = '09/10/2026 03:00:26'; Code = 'BOARD_TIMESTAMP_INVALID' },
        [pscustomobject]@{ Name = 'missing-z'; Timestamp = '2026-09-10T03:00:26'; Code = 'BOARD_TIMESTAMP_INVALID' },
        [pscustomobject]@{ Name = 'offset'; Timestamp = '2026-09-10T03:00:26+00:00'; Code = 'BOARD_TIMESTAMP_INVALID' },
        [pscustomobject]@{ Name = 'impossible-date'; Timestamp = '2026-02-30T03:00:26Z'; Code = 'BOARD_TIMESTAMP_INVALID' },
        [pscustomobject]@{ Name = 'unicode-digits'; Timestamp = $unicodeDigitTimestamp; Code = 'BOARD_TIMESTAMP_INVALID'; MessageContains = 'ending in uppercase Z' },
        [pscustomobject]@{ Name = 'datetime-culture'; Timestamp = $dateTimeCultureValue; Code = 'BOARD_TIMESTAMP_INVALID' },
        [pscustomobject]@{ Name = 'future'; Timestamp = ([DateTime]::UtcNow.AddMinutes(5).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ', [Globalization.CultureInfo]::InvariantCulture)); Code = 'BOARD_TIMESTAMP_FUTURE' }
    )
    foreach ($case in $rejected) {
        $result = Invoke-BusCase -Name $case.Name -RowJson (New-TestRowJson -Timestamp $case.Timestamp)
        Assert-True ($case.Name + ' rejected with stable code') ($result.ErrorMessage.StartsWith($case.Code + ':', [StringComparison]::Ordinal)) $result.ErrorMessage
        Assert-True ($case.Name + ' rejected before transport') (-not $result.TransportObserved)
        Assert-True ($case.Name + ' error is bounded') ($result.Output.Length -le 512) ('length=' + $result.Output.Length)
        Assert-True ($case.Name + ' error hides bus secret') (-not $result.Output.Contains($script:FakeSecret))
        Assert-True ($case.Name + ' error hides row payload') (-not $result.Output.Contains($script:PayloadSecret))
        Assert-True ($case.Name + ' error hides rejected value') (-not $result.Output.Contains($case.Timestamp))
        $messageExpectation = $case.PSObject.Properties['MessageContains']
        if ($messageExpectation) {
            Assert-True ($case.Name + ' is rejected by the ASCII wire-format gate') (
                $result.ErrorMessage.Contains([string]$messageExpectation.Value)
            ) $result.ErrorMessage
        }
    }

    $generalRow = New-TestRowJson -Timestamp 'September 10, 2026 3:00 AM' -Envelope 'GENERAL-SHEET-ROW'
    $generalResult = Invoke-BusCase -Name 'general-sheet-ten-cells' -RowJson $generalRow
    Assert-True 'general 10-cell sheet row remains compatible' ($generalResult.ErrorMessage.Length -eq 0) $generalResult.ErrorMessage
    Assert-True 'general 10-cell sheet row reaches transport' $generalResult.TransportObserved

    $unicodeGist = 'caf{0} | {1}{2} | {3}' -f (
        [char]0x00E9,
        [char]0x6F22,
        [char]0x5B57,
        [char]::ConvertFromUtf32(0x1F642)
    )
    $unicodeTimestamp = $clock.ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture)
    $unicodeResult = Invoke-BusCase -Name 'canonical-bcb-multibyte-cell' -RowJson (New-TestRowJson -Timestamp $unicodeTimestamp -Gist $unicodeGist)
    $unicodeRequest = $(if ($unicodeResult.TransportObserved) { $unicodeResult.RequestBody | ConvertFrom-Json } else { $null })
    Assert-True 'canonical BCB row with multibyte cell is accepted' ($unicodeResult.ErrorMessage.Length -eq 0) $unicodeResult.ErrorMessage
    Assert-True 'canonical BCB row with multibyte cell reaches transport' $unicodeResult.TransportObserved
    Assert-True 'transport trap receives a genuinely multibyte request body' (
        [Text.Encoding]::UTF8.GetByteCount($unicodeResult.RequestBody) -gt $unicodeResult.RequestBody.Length
    )
    Assert-True 'multibyte cell survives request JSON exactly' (
        $null -ne $unicodeRequest -and [string]$unicodeRequest.sheetRow[8] -ceq $unicodeGist
    )

    $legacyBcbRow = New-TestRowJson -Timestamp 'legacy local time' -CellCount 8
    $legacyResult = Invoke-BusCase -Name 'noncanonical-eight-cell-bcb' -RowJson $legacyBcbRow
    Assert-True 'noncanonical BCB row shape remains compatible' ($legacyResult.ErrorMessage.Length -eq 0) $legacyResult.ErrorMessage
    Assert-True 'noncanonical BCB row shape reaches transport' $legacyResult.TransportObserved
} finally {
    if ($KeepArtifacts) {
        [Console]::Out.WriteLine('Artifacts: ' + $script:TestRoot)
    } elseif (Test-Path -LiteralPath $script:TestRoot -PathType Container) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

[Console]::Out.WriteLine('')
[Console]::Out.WriteLine(('Passed: {0}  Failed: {1}' -f $script:Passed, $script:Failed))
if ($script:Failed -gt 0) { exit 1 }
