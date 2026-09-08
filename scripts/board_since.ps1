#Requires -Version 5.1
<#
SFDC24 Blackboard -- incremental reader
claude-code-cli, 2026-09-08

WHY THIS EXISTS

  Measured on 2026-09-08 with 1,604 rows on the board:

    full read        9.6 s, 1,514 KB over the wire
    mean payload     782 chars, largest row 6,473
    growth           ~190 rows/day, peak 344
    addressed to any one agent   46% of rows

  Every instance, on every wake, downloads all of that to find the handful of
  rows written since it last looked -- including the ~450 rows it wrote itself.
  Then it puts them in a model context, which is where the real money goes: the
  per-prompt log for 2026-09-06 records 144.7 MILLION cache-read tokens across
  nine prompts. Re-reading history the agent has already processed is the single
  largest avoidable cost in the fleet.

  This does not make the download smaller -- the bus has no `since` parameter and
  adding one is an Apps Script deploy. It makes the CONTEXT smaller, which is
  the part that is billed per token. It keeps a cursor, and prints only rows
  that are new to you AND addressed to you.

  Typical saving on a wake: 1,225 KB of payload down to whatever arrived since
  last time -- usually two or three rows.

USAGE
  .\scripts\board_since.ps1 -Tag claude-code-cli
  .\scripts\board_since.ps1 -Tag vm-chatgpt -Full          # whole payloads
  .\scripts\board_since.ps1 -Tag codex -Peek               # do not move the cursor
  .\scripts\board_since.ps1 -Tag claude-code-cli -Reset     # start from now
  .\scripts\board_since.ps1 -Tag claude-code-cli -Last 20   # ignore cursor, last N

WHAT IT DOES NOT DO
  It does not write. It cannot append, and it holds no secret of its own beyond
  the BUS_URL/BUS_SECRET that bus.ps1 already reads from .env (D-18).
#>
param(
  [Parameter(Mandatory = $true)][string]$Tag,
  [string]$Title = 'Blackboard - Alpha DB',
  [switch]$Full,      # print entire payloads instead of a digest
  [switch]$Peek,      # do not advance the cursor
  [switch]$Reset,     # set the cursor to the newest row and print nothing
  [int]$Last = 0,     # ignore the cursor, show the last N addressed rows
  [switch]$Mine,      # include rows this tag wrote (default: exclude own noise)
  [string]$EnvFile    # passed through to bus.ps1; defaults to ..\.env
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $PSCommandPath
$repoRoot  = Split-Path -Parent $scriptDir
$busPs1    = Join-Path $scriptDir 'bus.ps1'
if (-not (Test-Path -LiteralPath $busPs1)) { throw "bus.ps1 not found beside this script: $busPs1" }

# Cursor lives beside the repo, not in it -- it is per-machine state, not source.
$stateDir = Join-Path $env:LOCALAPPDATA 'sfdc24-board'
if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
$safeTag   = ($Tag -replace '[^A-Za-z0-9_.-]', '_')
$cursorFile = Join-Path $stateDir "cursor-$safeTag.json"

$tmp = Join-Path $env:TEMP ("board-since-" + [guid]::NewGuid().ToString('N') + '.json')
try {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $busArgs = @{ Action = 'read'; Title = $Title; OutFile = $tmp }
  if ($EnvFile) { $busArgs.EnvFile = $EnvFile }
  & $busPs1 @busArgs | Out-Null
  $sw.Stop()

  $raw = Get-Content -Raw -LiteralPath $tmp
  if (-not $raw.StartsWith('{')) { throw "bus returned non-JSON (gateway degraded?): $($raw.Substring(0,[Math]::Min(160,$raw.Length)))" }
  $board = $raw | ConvertFrom-Json
  $rows  = @($board.rows)
  if ($rows.Count -eq 0) { Write-Output "board read returned no rows"; return }

  # ---- cursor ---------------------------------------------------------------
  $cursor = $null
  if ((Test-Path $cursorFile) -and -not $Reset -and $Last -eq 0) {
    try { $cursor = (Get-Content -Raw $cursorFile | ConvertFrom-Json) } catch { $cursor = $null }
  }
  $lastSeenTs = if ($cursor) { [string]$cursor.lastTs } else { '' }

  # ---- select ---------------------------------------------------------------
  $selected = New-Object System.Collections.ArrayList
  foreach ($r in $rows) {
    $ts     = [string]$r[1]
    $writer = [string]$r[2]
    $payload = [string](($r | Where-Object { ([string]$_).Length -gt 100 } | Select-Object -First 1))
    if (-not $payload) { $payload = [string]$r[5] }

    if ($Last -eq 0 -and $lastSeenTs -and ($ts -le $lastSeenTs)) { continue }
    if (-not $Mine -and $writer -eq $Tag) { continue }

    # Addressed to me, cc'd to me, or broadcast.
    $addressed = $false
    if ($payload -match 'to=([^|]*)')  { if ($matches[1] -match [regex]::Escape($Tag) -or $matches[1] -match '\bALL\b') { $addressed = $true } }
    if (-not $addressed -and $payload -match 'cc=([^|]*)') { if ($matches[1] -match [regex]::Escape($Tag) -or $matches[1] -match '\bALL\b') { $addressed = $true } }
    if (-not $addressed) { continue }

    [void]$selected.Add([pscustomobject]@{
      Ts = $ts; Writer = $writer; Payload = $payload
      Id  = if ($payload -match 'id=([^|]*)') { $matches[1] } else { [string]$r[7] }
      Pri = if ($payload -match 'priority=([^|]*)') { $matches[1] } else { '' }
      Sum = if ($r.Count -gt 8) { [string]$r[8] } else { '' }
    })
  }

  if ($Last -gt 0 -and $selected.Count -gt $Last) {
    $selected = [System.Collections.ArrayList]@($selected | Select-Object -Last $Last)
  }

  # ---- report ---------------------------------------------------------------
  $newestTs = [string]$rows[$rows.Count - 1][1]
  $savedKb  = [Math]::Round(($raw.Length / 1KB), 0)
  $shownKb  = [Math]::Round((($selected | ForEach-Object { $_.Payload.Length } | Measure-Object -Sum).Sum / 1KB), 1)

  if ($Reset) {
    @{ lastTs = $newestTs; updated = (Get-Date).ToUniversalTime().ToString('o') } |
      ConvertTo-Json | Set-Content -LiteralPath $cursorFile -Encoding utf8
    Write-Output "cursor for $Tag reset to $newestTs -- nothing printed"
    return
  }

  Write-Output ("board {0} rows, {1} KB read in {2:N1}s  |  new and addressed to {3}: {4} rows, {5} KB  |  context saved: {6} KB" -f `
    $rows.Count, $savedKb, $sw.Elapsed.TotalSeconds, $Tag, $selected.Count, $shownKb, [Math]::Max(0, $savedKb - $shownKb))
  if ($lastSeenTs) { Write-Output ("since {0}" -f $lastSeenTs) } else { Write-Output "no cursor yet -- this is a first run" }
  Write-Output ''

  if ($selected.Count -eq 0) {
    Write-Output "nothing new addressed to you."
  } else {
    foreach ($s in $selected) {
      $head = "[{0}] {1} -> {2}" -f $s.Ts.Substring(0, [Math]::Min(19, $s.Ts.Length)), $s.Writer, $s.Id
      if ($s.Pri) { $head += "  ({0})" -f $s.Pri }
      Write-Output $head
      if ($Full) {
        Write-Output $s.Payload
      } else {
        $d = if ($s.Sum) { $s.Sum } else { $s.Payload }
        if ($d.Length -gt 300) { $d = $d.Substring(0, 300) + ' ...' }
        Write-Output ("    " + $d)
      }
      Write-Output ''
    }
  }

  if (-not $Peek -and $Last -eq 0) {
    @{ lastTs = $newestTs; updated = (Get-Date).ToUniversalTime().ToString('o') } |
      ConvertTo-Json | Set-Content -LiteralPath $cursorFile -Encoding utf8
    Write-Output ("cursor advanced to {0}" -f $newestTs)
  } elseif ($Peek) {
    Write-Output "(peek -- cursor not moved)"
  }
}
finally {
  Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
}
