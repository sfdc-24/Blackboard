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
  [string]$EnvFile,   # passed through to bus.ps1; defaults to ..\.env
  [string]$BusScript  # explicit path to bus.ps1, for checkouts that lack it
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $PSCommandPath
$repoRoot  = Split-Path -Parent $scriptDir

# Selection and cursor logic lives beside this script so it can be tested
# without a bus, a network or a board. See tests/test_board_since.ps1.
. (Join-Path $scriptDir 'board_since.lib.ps1')
# bus.ps1 is NOT on main -- main carries only scripts/.gitkeep -- so a checkout
# of the default branch does not have it beside this file. Look in the obvious
# places and then say plainly what to pass, rather than failing with a path.
$candidates = @()
if ($BusScript)          { $candidates += $BusScript }
if ($env:SFDC24_BUS_PS1) { $candidates += $env:SFDC24_BUS_PS1 }
$candidates += (Join-Path $scriptDir 'bus.ps1')
$candidates += (Join-Path $repoRoot 'scripts/bus.ps1')
$busPs1 = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $busPs1) {
  throw ("bus.ps1 not found. Looked in:`n  " + ($candidates -join "`n  ") +
         "`nPass -BusScript <path>, or set SFDC24_BUS_PS1. " +
         "bus.ps1 currently lives only on branch session/bus-clients-and-docs, not on main.")
}

# Cursor lives beside the repo, not in it -- it is per-machine state, not source.
$stateDir = Join-Path $env:LOCALAPPDATA 'sfdc24-board'
if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
$safeTag   = ($Tag -replace '[^A-Za-z0-9_.-]', '_')
# Keyed by tag AND board. -Title picks a different sheet and the .env picks a
# different bus; sharing one cursor across them let a second board inherit the
# first board's anchor and skip every row it already had.
#
# The board's identity is BUS_URL, not the path to the file that holds it. A
# redeployed Apps Script gets a new /exec URL while .env stays exactly where it
# was, and the four bus.ps1 candidates above each imply a DIFFERENT default .env
# -- so keying on the path put two boards on one cursor by two separate routes.
# The value is hashed and never printed (D-18).
$envPath   = Resolve-BoardEnvPath -EnvFile $EnvFile -BusScript $busPs1
$bus       = Get-BoardBusIdentity -EnvPath $envPath
$sourceKey = Get-BoardSourceKey -Title $Title -Bus $bus.Value -BusSource $bus.Source
$cursorFile = Join-Path $stateDir ("cursor-{0}-{1}.json" -f $safeTag, $sourceKey)

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
  # Belt as well as braces: even with a per-board filename, refuse a cursor that
  # does not name this board. A mismatch reads as a first run, which repeats
  # rows at worst; adopting it would skip them.
  if ($cursor -and -not (Test-BoardCursorMatches -Cursor $cursor -SourceKey $sourceKey)) {
    Write-Warning 'cursor does not belong to this board -- ignoring it and reading from the top'
    $cursor = $null
  }

  # Anchored by Row_ID at a known index, not by timestamp. The board is not
  # unique by timestamp -- a snapshot on 2026-09-09 held 11 duplicate-timestamp
  # groups -- and the old `-le` comparison skipped every tied row permanently.
  $start = @{ StartIndex = 0; Anchor = 'first-run'; Note = 'no cursor yet -- this is a first run' }
  if ($Last -eq 0) { $start = Resolve-BoardStartIndex -Rows $rows -Cursor $cursor }

  # ---- select ---------------------------------------------------------------
  $selected = New-Object System.Collections.ArrayList
  for ($idx = [int]$start.StartIndex; $idx -lt $rows.Count; $idx++) {
    $r = $rows[$idx]
    $ts     = [string]$r[1]
    $writer = [string]$r[2]
    # Cell 5, always. See Get-BoardRowPayload for what the old length heuristic
    # did to a row whose gist was longer than its payload.
    $payload = Get-BoardRowPayload -Row $r

    if (-not $Mine -and $writer -eq $Tag) { continue }

    # Addressed to me, cc'd to me, or broadcast -- by EXACT token, never by
    # substring. `-Tag codex` must not consume mail for chatgpt-codex-desktop.
    #
    # BOTH addressing surfaces, not just the payload. Cell 3 is Target_Surface,
    # and a row whose recipients live only there -- a plain-prose row with no
    # to=/cc= tokens -- was invisible to every reader using this function. On
    # 2026-09-16 that hid CODEX-01A09BF0-PROTOTYPE-CONTRACT-REVIEW-20260916 from
    # claude-code-cli while the summary line confidently reported "3 rows".
    # Guarded on Count the way Get-BoardRowPayload guards cell 5: a short row
    # must not throw, it must simply carry no column addressing.
    $target = if ($r.Count -gt 3) { [string]$r[3] } else { '' }
    if (-not (Test-BoardAddressed -Payload $payload -Tag $Tag -TargetSurface $target)) { continue }

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
  $newestTs   = [string]$rows[$rows.Count - 1][1]
  $nextCursor = New-BoardCursor -Rows $rows -SourceKey $sourceKey
  $savedKb  = [Math]::Round(($raw.Length / 1KB), 0)
  $shownKb  = [Math]::Round((($selected | ForEach-Object { $_.Payload.Length } | Measure-Object -Sum).Sum / 1KB), 1)

  if ($Reset) {
    $nextCursor | ConvertTo-Json | Set-Content -LiteralPath $cursorFile -Encoding utf8
    Write-Output "cursor for $Tag reset to row $($nextCursor.lastIndex) ($newestTs) -- nothing printed"
    return
  }

  Write-Output ("board {0} rows, {1} KB read in {2:N1}s  |  new and addressed to {3}: {4} rows, {5} KB  |  context saved: {6} KB" -f `
    $rows.Count, $savedKb, $sw.Elapsed.TotalSeconds, $Tag, $selected.Count, $shownKb, [Math]::Max(0, $savedKb - $shownKb))
  if ($start.Note) { Write-Output $start.Note }
  elseif ($start.Anchor -eq 'index' -or $start.Anchor -eq 'searched') {
    Write-Output ("since row {0}" -f ([int]$start.StartIndex - 1))
  }
  if ($start.Anchor -eq 'anchor-lost') { Write-Warning 'board cursor anchor lost -- some rows above may be shown again' }
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
    $nextCursor | ConvertTo-Json | Set-Content -LiteralPath $cursorFile -Encoding utf8
    Write-Output ("cursor advanced to row {0} ({1})" -f $nextCursor.lastIndex, $nextCursor.lastRowId)
  } elseif ($Peek) {
    Write-Output "(peek -- cursor not moved)"
  }
}
finally {
  Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
}
