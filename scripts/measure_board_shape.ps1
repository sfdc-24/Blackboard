#Requires -Version 5.1
<#
Re-measure the four structural facts docs/COMMS-PROTOCOL.md section 9 depends on.

WHY THIS EXISTS
  That section stated counts measured on 2026-09-09 - "all 1,823 rows come back
  as twelve cells", "14 of 1,822 data rows" - as though they were current. They
  were true when taken and stale within hours: the board passed 1,870 rows the
  same afternoon. A reader six weeks later has no way to tell whether a figure
  still holds, and the honest answer to "is this number current" is a command,
  not a date.

  So the DURABLE PROPERTIES are the contract and live in the document. The
  COUNTS are a measurement, and this re-takes them.

  It asserts nothing and changes nothing. It reads the board and prints what it
  found, because a script that decided for you what "still true" means would be
  the same mistake one level up.

Run:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_board_shape.ps1
#>
param(
  [string]$Title = 'Blackboard - Alpha DB',
  [string]$EnvFile,
  [string]$BusScript
)

$ErrorActionPreference = 'Stop'
if (-not $BusScript) { $BusScript = Join-Path $PSScriptRoot 'bus.ps1' }
if (-not $EnvFile)   { $EnvFile   = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }

$tmp = [IO.Path]::GetTempFileName()
try {
  & $BusScript -Action read -Title $Title -EnvFile $EnvFile -OutFile $tmp | Out-Null
  $rows = @((Get-Content $tmp -Raw | ConvertFrom-Json).rows)
} finally {
  Remove-Item $tmp -ErrorAction SilentlyContinue
}
if ($rows.Count -lt 1) { throw 'board_read_empty' }

$widths = @{}
foreach ($r in $rows) {
  $w = @($r).Count
  if (-not $widths.ContainsKey($w)) { $widths[$w] = 0 }
  $widths[$w]++
}

$pastJ = New-Object System.Collections.Generic.List[int]
$badStamp = New-Object System.Collections.Generic.List[int]
for ($i = 1; $i -lt $rows.Count; $i++) {
  $cells = @($rows[$i])
  for ($j = 10; $j -lt $cells.Count; $j++) {
    if (-not [string]::IsNullOrEmpty([string]$cells[$j])) { $pastJ.Add($i + 1); break }
  }
  $ts = [string]$cells[1]
  if ($ts -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$') { $badStamp.Add($i + 1) }
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
Write-Output "board shape, measured $stamp"
Write-Output ("  rows including header      : {0}" -f $rows.Count)
Write-Output ("  data rows                  : {0}" -f ($rows.Count - 1))
Write-Output ("  cell widths                : {0}" -f (
  ($widths.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name) cells x $($_.Value)" }) -join ', '))
Write-Output ("  rows with data past col J  : {0}{1}" -f $pastJ.Count, $(
  if ($pastJ.Count -and $pastJ.Count -le 5) { "  (sheet rows " + ($pastJ -join ', ') + ")" } else { '' }))
Write-Output ("  timestamps not ISO-UTC-Z   : {0}" -f $badStamp.Count)
Write-Output ''
Write-Output 'The properties section 9 relies on are: every row returns at the sheet width, at'
Write-Output 'least one row carries data past column J, and some timestamps are not'
Write-Output 'ISO-UTC-Z. Those are what a reader must handle. The counts above will drift.'
