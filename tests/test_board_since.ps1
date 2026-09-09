#Requires -Version 5.1
[CmdletBinding()]
param()

<#
Fixtures for the two defects an exact-head review found in scripts/board_since.ps1.

Neither was hypothetical. Substring addressing was reproduced against three real
tag pairs, and the duplicate-timestamp loss was measured on a 1,648-row board
snapshot that contained 11 exact duplicate-timestamp groups, including groups of
three and four.

These tests call the selection logic directly. No bus, no network, no board, no
credentials, and nothing here writes a cursor file.
#>

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $script:RepoRoot (Join-Path 'scripts' 'board_since.lib.ps1'))

$script:Passed = 0
$script:Failed = 0

function Assert-True {
  param([string]$Name, [bool]$Condition, [string]$Detail = '')
  if ($Condition) {
    $script:Passed++
    Write-Output ('PASS ' + $Name)
  } else {
    $script:Failed++
    Write-Output ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
  }
}

function New-Row {
  param([string]$RowId, [string]$Ts, [string]$Writer, [string]$Payload)
  return , @($RowId, $Ts, $Writer, 'Blackboard Alpha DB', 'APPEND', $Payload, 'OPEN', 'ID', 'gist', 'sub')
}

# ── Addressing: exact tokens, never substrings ───────────────────────────────

Assert-True 'a prefix tag does not consume another instance mail' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=chatgpt-codex-desktop|from=x' -Tag 'codex')
) 'codex must not match chatgpt-codex-desktop'

Assert-True 'a suffix tag does not consume another instance mail' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=vm-claude-code-cli|from=x' -Tag 'claude-code-cli')
) 'claude-code-cli must not match vm-claude-code-cli'

Assert-True 'the reverse collision is also refused' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=claude-code-cli|from=x' -Tag 'vm-claude-code-cli')
)

Assert-True 'an exact recipient still matches' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=claude-code-cli|from=x' -Tag 'claude-code-cli'
)

Assert-True 'a comma list matches on any exact member' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=vm-chrome, claude-code-cli|from=x' -Tag 'claude-code-cli'
)

Assert-True 'case does not matter' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=Claude-Code-CLI|from=x' -Tag 'claude-code-cli'
)

Assert-True 'a broadcast reaches everyone' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=someone-else|cc=ALL|from=x' -Tag 'anyone'
)

Assert-True 'cc is honoured as well as to' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=someone|cc=claude-code-cli|from=x' -Tag 'claude-code-cli'
)

Assert-True 'a row addressed to nobody relevant is skipped' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=vm-chrome|from=x' -Tag 'claude-code-cli')
)

Assert-True 'ALLOCATE is not the ALL broadcast' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=ALLOCATE|from=x' -Tag 'claude-code-cli')
) 'the old \bALL\b regex was word-bounded, but token equality is what is meant'

# ── Cursor: anchored by Row_ID, and ties are never dropped ───────────────────

# Three rows share the newest timestamp. The old timestamp cursor stored that
# value and then skipped everything `-le` it, so rows B and C could never be
# seen again by any later run.
$tied = @(
  (New-Row -RowId 'r1' -Ts '2026-09-09T05:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=A'),
  (New-Row -RowId 'r2' -Ts '2026-09-09T06:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=B'),
  (New-Row -RowId 'r3' -Ts '2026-09-09T06:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=C'),
  (New-Row -RowId 'r4' -Ts '2026-09-09T06:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=D')
)

$afterFirst = New-BoardCursor -Rows @($tied[0], $tied[1])
Assert-True 'the cursor records an index and the Row_ID that proves it' (
  $afterFirst.lastIndex -eq 1 -and $afterFirst.lastRowId -eq 'r2'
) ("got index=" + $afterFirst.lastIndex + " rowId=" + $afterFirst.lastRowId)

$resume = Resolve-BoardStartIndex -Rows $tied -Cursor ([pscustomobject]$afterFirst)
Assert-True 'resuming after a tied timestamp starts at the next row, not past the tie' (
  $resume.Anchor -eq 'index' -and $resume.StartIndex -eq 2
) ("anchor=" + $resume.Anchor + " start=" + $resume.StartIndex)

$remaining = @()
for ($i = [int]$resume.StartIndex; $i -lt $tied.Count; $i++) { $remaining += [string]$tied[$i][0] }
Assert-True 'the rows sharing the newest timestamp are still delivered' (
  ($remaining -join ',') -eq 'r3,r4'
) ("got " + ($remaining -join ','))

# The old behaviour, asserted directly so the regression is legible: a
# timestamp cursor at 06:00 with `-le` would have yielded nothing at all.
$tsOnlySkipped = @($tied | Where-Object { ([string]$_[1]) -le '2026-09-09T06:00:00Z' }).Count
Assert-True 'the timestamp-only cursor really did drop the tie' (
  $tsOnlySkipped -eq 4
) 'all four rows are -le the newest timestamp, which is why every tie vanished'

# Rows inserted above the anchor: the anchor must be found by search.
# Built through a list on purpose. `@($row) + $rows` splats the leading row into
# its ten cells, which produced a 12-element "board" of bare strings and a
# start index of 12 the first time this fixture was written.
$shiftedList = New-Object System.Collections.ArrayList
[void]$shiftedList.Add(@('r0', '2026-09-09T04:00:00Z', 'other', 'Blackboard Alpha DB', 'APPEND', 'BCB|v=1|to=x', 'OPEN', 'ID', 'gist', 'sub'))
foreach ($row in $tied) { [void]$shiftedList.Add($row) }
$shifted = $shiftedList.ToArray()
Assert-True 'the shifted fixture is a board of rows, not a bag of cells' (
  $shifted.Count -eq 5 -and $shifted[0].Count -eq 10
) ("count=" + $shifted.Count)
$searched = Resolve-BoardStartIndex -Rows $shifted -Cursor ([pscustomobject]$afterFirst)
Assert-True 'an anchor that moved is found by Row_ID rather than lost' (
  $searched.Anchor -eq 'searched' -and $searched.StartIndex -eq 3
) ("anchor=" + $searched.Anchor + " start=" + $searched.StartIndex)

# Anchor gone entirely: fall back INCLUSIVELY. Repeating is acceptable; skipping
# is the failure this whole change exists to prevent.
$lostCursor = [pscustomobject]@{ lastIndex = 1; lastRowId = 'vanished'; lastTs = '2026-09-09T06:00:00Z' }
$lost = Resolve-BoardStartIndex -Rows $tied -Cursor $lostCursor
Assert-True 'a lost anchor falls back inclusively, never past the tie' (
  $lost.Anchor -eq 'anchor-lost' -and $lost.StartIndex -eq 1
) ("anchor=" + $lost.Anchor + " start=" + $lost.StartIndex)

Assert-True 'a lost anchor says so, rather than failing quietly' (
  $lost.Note -match 'INCLUSIVE'
)

# A cursor written by the previous version has no Row_ID at all.
$legacy = Resolve-BoardStartIndex -Rows $tied -Cursor ([pscustomobject]@{ lastTs = '2026-09-09T06:00:00Z' })
Assert-True 'a legacy timestamp cursor is migrated inclusively' (
  $legacy.Anchor -eq 'legacy-ts' -and $legacy.StartIndex -eq 1
) ("anchor=" + $legacy.Anchor + " start=" + $legacy.StartIndex)

$firstRun = Resolve-BoardStartIndex -Rows $tied -Cursor $null
Assert-True 'a first run reads from the top' (
  $firstRun.Anchor -eq 'first-run' -and $firstRun.StartIndex -eq 0
)

$empty = New-BoardCursor -Rows @()
Assert-True 'an empty board yields a cursor that cannot skip anything' (
  $empty.lastIndex -eq -1 -and $empty.lastRowId -eq ''
)

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
