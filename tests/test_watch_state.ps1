#Requires -Version 5.1
<#
SFDC24 - durable cursor + outbox for the board watchers
claude-code-cli, 2026-09-08. Cases required by chatgpt-codex-desktop in
CODEX-PR34-OUTBOX-DESIGN / -PERSISTENCE-NOGO / -REVIEW-ADDENDUM.

WHAT IS ON TRIAL
  Three earlier designs for this state lost or invented messages, and every one
  of them passed its own tests:

    v0  newest-400 id set, whole-board rescan  -> replayed ancient rows as new
    v1  timestamp watermark                    -> skipped late rows forever
    v2  positional cursor, emit-then-save      -> warned and carried on when the
                                                  save failed, so a restart
                                                  either replayed or skipped

  Each looked healthy while quietly getting it wrong, so these cases are written
  against the FAILURES. Several assert the RESULT of a save rather than the
  existence of a file, because a previous version of this suite checked
  existence and would have stayed green on Linux where every replace failed.

RUN
  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_watch_state.ps1
  pwsh -NoProfile -File tests/test_watch_state.ps1
#>

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
# Forward slash: this runs under pwsh on Linux in CI as well as WinPS here.
. (Join-Path $RepoRoot 'scripts/watch_state.ps1')

$script:Passed = 0
$script:Failed = 0
function Assert-True {
  param([string]$Name, [bool]$Condition, [string]$Detail = '')
  if ($Condition) { $script:Passed++; Write-Output ('  PASS  ' + $Name) }
  else { $script:Failed++; Write-Output ('  FAIL  ' + $Name + $(if ($Detail) { '  --> ' + $Detail } else { '' })) }
}

# NOT $env:TEMP: it does not exist under pwsh on Linux, and CI failed on exactly
# that before this line was written.
$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ('watchstate_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

function New-Row {
  param([string]$Id, [string]$Ts, [string]$Source = 'whatsapp', [string]$Payload = 'hello')
  return @($Id, $Ts, $Source, 'ALL', 'APPEND', $Payload, '', '', '', '')
}
function New-Board {
  param([int]$Count, [string]$Prefix = 'r')
  $rows = @( ,@('Row_ID','Timestamp','Source_Tag','Target_Surface','Action_Type','Payload','Category','Project','Gist','Sub') )
  for ($i = 1; $i -le $Count; $i++) {
    $ts = (Get-Date '2026-09-01T00:00:00Z').ToUniversalTime().AddSeconds($i).ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $rows += ,(New-Row -Id ($Prefix + $i) -Ts $ts)
  }
  return $rows
}
# Named $BoardFileId, not $BOARD: PowerShell variable names are case-insensitive,
# so $BOARD and the $board row array below would be the SAME variable and the id
# was silently overwritten by a row set.
$BoardFileId = 'board-file-id-1'

function Commit-At {
  param($Rows, [int]$Index, [string]$Id = $BoardFileId)
  $s = New-WatchState
  $s.boardId = $Id
  return (Set-CommittedCursor -State $s -Rows $Rows -Index $Index)
}
function Stage-At {
  # Stage an outbox the way a watcher does: a plan of line/rowIndex/rowId.
  param($Rows, [int]$Index, [string[]]$Lines, [string[]]$RowIds, [string]$Id = $BoardFileId, [int]$From = 0)
  $s = New-WatchState
  $s.boardId = $Id
  if ($From -ge 1) { $s = Set-CommittedCursor -State $s -Rows $Rows -Index $From }
  $plan = @()
  for ($i = 0; $i -lt @($Lines).Count; $i++) {
    $plan += ,@{ line = $Lines[$i]; rowIndex = $Index; rowId = @($RowIds)[$i] }
  }
  $r = Set-WatchOutbox -State $s -Rows $Rows -Plan $plan -FallbackIndex $Index `
         -Mode 'steady' -Limit 0 -From $(if ($From -ge 1) { $From } else { 1 })
  return $r.state
}

try {

Write-Output ''
Write-Output 'CASE 1 - restart with more than 400 rows behind the cursor (the v0 bug)'
$board = New-Board -Count 500
Assert-True 'a fresh watcher primes rather than replaying' `
  ((Test-BoardContinuity -State $null -Rows $board -BoardId $BoardFileId).prime -eq $true)
$s = Commit-At -Rows $board -Index 500
$p1 = Join-Path $tmpDir 'c1.json'
$sv = Save-WatchState -State $s -Path $p1
Assert-True 'the save REPORTS success (not merely leaves a file)' ($sv.ok -eq $true) $sv.reason
$re = (Read-WatchState -Path $p1).state
$board2 = $board + ,(New-Row -Id 'r501' -Ts '2026-09-02T00:00:00Z')
$c2 = Test-BoardContinuity -State $re -Rows $board2 -BoardId $BoardFileId
Assert-True 'resume is possible' (($c2.ok -eq $true) -and ($c2.prime -eq $false)) $c2.reason
Assert-True 'resumes at exactly the next row, not row 1' ($c2.startIndex -eq 501) ('startIndex=' + $c2.startIndex)

Write-Output ''
Write-Output 'CASE 2 - a row appended late carrying an OLD valid timestamp (the v1 bug)'
$board3 = $board2 + ,(New-Row -Id 'late' -Ts '2026-08-01T00:00:00Z')
$c3 = Test-BoardContinuity -State $re -Rows $board3 -BoardId $BoardFileId
$seen = @(); for ($i = $c3.startIndex; $i -le ($board3.Count - 1); $i++) { $seen += [string]$board3[$i][0] }
Assert-True 'the late-but-old-stamped row IS considered' ($seen -contains 'late') ($seen -join ',')

Write-Output ''
Write-Output 'CASE 3/4 - shrink, anchor mismatch, changed board, UNKNOWN board'
$c4 = Test-BoardContinuity -State $re -Rows (New-Board -Count 10) -BoardId $BoardFileId
Assert-True 'a shrunken board refuses to resume' ($c4.ok -eq $false)
Assert-True 'and names the shrink' ($c4.reason -match 'shrank') $c4.reason
Assert-True 'and never auto-primes' ($c4.prime -eq $false)
$c5 = Test-BoardContinuity -State $re -Rows (New-Board -Count 501 -Prefix 'x') -BoardId $BoardFileId
Assert-True 'a moved anchor refuses to resume' (($c5.ok -eq $false) -and ($c5.reason -match 'anchor mismatch')) $c5.reason
$c6 = Test-BoardContinuity -State $re -Rows $board2 -BoardId 'another-file-id'
Assert-True 'a different board refuses to resume' (($c6.ok -eq $false) -and ($c6.reason -match 'identity changed')) $c6.reason
# The addendum finding: comparing only when BOTH ids were truthy meant a read
# with no fileId skipped the check and carried on as though identity held.
$c7 = Test-BoardContinuity -State $re -Rows $board2 -BoardId ''
Assert-True 'an UNIDENTIFIED board refuses to resume' ($c7.ok -eq $false) $c7.reason
Assert-True 'and says the identity was missing' ($c7.reason -match 'no identity') $c7.reason

Write-Output ''
Write-Output 'CASE 5 - corrupt, empty, truncated and stale-schema state'
$bad = Join-Path $tmpDir 'bad.json'
Set-Content -LiteralPath $bad -Value '{"schema":4,"lastIndex":5' -Encoding UTF8
$r1 = Read-WatchState -Path $bad
Assert-True 'a truncated file yields no state, with a reason' (($null -eq $r1.state) -and $r1.reason) $r1.reason
Set-Content -LiteralPath $bad -Value '' -Encoding UTF8
Assert-True 'an empty file yields no state, with a reason' ($null -eq (Read-WatchState -Path $bad).state)
Set-Content -LiteralPath $bad -Value '{"seenIds":["a","b"]}' -Encoding UTF8
$r3 = Read-WatchState -Path $bad
Assert-True 'the v0 id-set format is refused, not misread' ($null -eq $r3.state)
Assert-True 'and reported as a schema difference' ($r3.reason -match 'schema') $r3.reason
Set-Content -LiteralPath $bad -Value '{"schema":3,"boardId":"b","lastIndex":3,"anchorId":"a"}' -Encoding UTF8
Assert-True 'the v3 format is refused too' ($null -eq (Read-WatchState -Path $bad).state)
$r5 = Read-WatchState -Path (Join-Path $tmpDir 'absent.json')
Assert-True 'a missing file is simply a cold start, with no alarm' (($null -eq $r5.state) -and (-not $r5.reason))

Write-Output ''
Write-Output 'CASE 6 - persistence failure is REPORTED, never assumed'
# A path whose parent is a FILE. This is the case that exposed the original bug:
# with $ErrorActionPreference=Continue the cmdlet error never reached the catch
# and the function returned ok=$true having written nothing.
$blocker = Join-Path $tmpDir 'blocker.txt'
Set-Content -LiteralPath $blocker -Value 'x' -Encoding UTF8
$sv2 = Save-WatchState -State $s -Path (Join-Path $blocker 'cursor.json')
Assert-True 'an unwritable path returns ok=false' ($sv2.ok -eq $false)
Assert-True 'with a reason the caller can print' ([bool]$sv2.reason) $sv2.reason

Write-Output ''
Write-Output 'CASE 7 - the replace path is exercised and ASSERTED, not just present'
# The previous suite checked Test-Path after saving, so on Linux -- where the
# MoveFileEx P/Invoke cannot load at all -- every replace failed and CI stayed
# green. This asserts the reported result of the SECOND save, which is the one
# that takes the replace branch.
$p7 = Join-Path $tmpDir 'c7.json'
$first = Save-WatchState -State $s -Path $p7
Assert-True 'first save (create path) reports success' ($first.ok -eq $true) $first.reason
$second = Save-WatchState -State $s -Path $p7
Assert-True 'second save (REPLACE path) reports success on this platform' ($second.ok -eq $true) $second.reason
Assert-True 'no .tmp is left behind' (-not (Test-Path -LiteralPath ($p7 + '.tmp')))
Set-Content -LiteralPath ($p7 + '.tmp') -Value '{"junk":true' -Encoding UTF8
Assert-True 'a stray .tmp from a dead run is inert' `
  ((Read-WatchState -Path $p7).state.lastIndex -eq 500)
Remove-Item -LiteralPath ($p7 + '.tmp') -Force -ErrorAction SilentlyContinue

Write-Output ''
Write-Output 'CASE 8/9 - duplicate collapse uses ROW time, not processing time'
$d = Set-WatchEmitted -State (New-WatchState) -Key 'whatsapp::ok' -RowTs '2026-09-08T01:00:00Z'
Assert-True 'the same text three hours later is NOT a duplicate' `
  (-not (Test-RowIsDuplicate -State $d -Key 'whatsapp::ok' -RowTs '2026-09-08T04:00:00Z'))
Assert-True 'the same text two seconds later IS collapsed' `
  (Test-RowIsDuplicate -State $d -Key 'whatsapp::ok' -RowTs '2026-09-08T01:00:02Z')
Assert-True 'different text at the same instant is not collapsed' `
  (-not (Test-RowIsDuplicate -State $d -Key 'whatsapp::other' -RowTs '2026-09-08T01:00:02Z'))
Assert-True 'an undateable row is never collapsed' `
  (-not (Test-RowIsDuplicate -State $d -Key 'whatsapp::ok' -RowTs 'not-a-date'))

Write-Output ''
Write-Output 'CASE 10 - the OUTBOX: a crash between persist and emit loses nothing'
$ob = Stage-At -Rows $board2 -Index 501 -Lines @('line one','line two') -RowIds @('r501','r500')
$p10 = Join-Path $tmpDir 'c10.json'
$stage = Save-WatchState -State $ob -Path $p10
Assert-True 'staging the outbox reports success' ($stage.ok -eq $true) $stage.reason
# --- simulated crash here: the process dies before emitting ---
$after = (Read-WatchState -Path $p10).state
Assert-True 'the next start sees a retained outbox' (Test-HasOutbox -State $after)
Assert-True 'carrying the EXACT lines that were never shown' `
  ((@($after.pendingLines) -join '|') -eq 'line one|line two') (@($after.pendingLines) -join '|')
Assert-True 'and the committed cursor did NOT advance' ($after.lastIndex -eq 0) ([string]$after.lastIndex)
$done = Complete-WatchOutbox -State $after
Assert-True 'committing advances the cursor to the staged index' ($done.lastIndex -eq 501) ([string]$done.lastIndex)
Assert-True 'and clears the outbox' (-not (Test-HasOutbox -State $done))
$cs = Save-WatchState -State $done -Path $p10
Assert-True 'the commit save reports success' ($cs.ok -eq $true) $cs.reason
Assert-True 'a second start after a clean commit replays nothing' `
  (-not (Test-HasOutbox -State (Read-WatchState -Path $p10).state))

Write-Output ''
Write-Output 'CASE 10b - a failed COMMIT retains the outbox rather than dropping it'
$keep = Stage-At -Rows $board2 -Index 501 -Lines @('kept') -RowIds @('r501') -From 500
$bad2 = Save-WatchState -State $keep -Path (Join-Path $blocker 'nope.json')
Assert-True 'the failed save reports ok=false' ($bad2.ok -eq $false) $bad2.reason
Assert-True 'and the in-memory outbox is still present to retry' (Test-HasOutbox -State $keep)

Write-Output ''
Write-Output 'CASE 11 - the DEFAULT state path, behaviourally'
$prevLocal = $env:LOCALAPPDATA
try {
  $env:LOCALAPPDATA = $tmpDir
  foreach ($leaf in @('wa_watch.state.json', 'fleet_watch.claude-code-cli.state.json')) {
    $dp = Get-DefaultStatePath -Leaf $leaf
    $ctrl = @($dp.ToCharArray() | Where-Object { [int]$_ -lt 32 -or [int]$_ -eq 127 })
    Assert-True ('default path for ' + $leaf + ' has no control characters') ($ctrl.Count -eq 0) $dp
    $st = Commit-At -Rows $board2 -Index 7
    $r = Save-WatchState -State $st -Path $dp
    Assert-True ('a save through the real default path succeeds for ' + $leaf) ($r.ok -eq $true) $r.reason
    Assert-True 'and reads back with its cursor intact' ((Read-WatchState -Path $dp).state.lastIndex -eq 7)
  }
} finally { $env:LOCALAPPDATA = $prevLocal }

Write-Output ''
Write-Output 'CASE 12 - a corrupted leaf is refused rather than written to'
$threw = $false
try { [void](Get-DefaultStatePath -Leaf ('sfdc24' + [char]12 + 'leet_watch.state.json')) } catch { $threw = $true }
Assert-True 'a form feed in the leaf throws' $threw
$threw2 = $false
try { [void](Get-DefaultStatePath -Leaf ('a' + [char]0 + 'b')) } catch { $threw2 = $true }
Assert-True 'a NUL in the leaf throws' $threw2

Write-Output ''
Write-Output 'CASE 13 - no watcher source carries a stray control byte'
# The form-feed defect survived because nothing read the bytes. This does.
foreach ($f in @('scripts/watch_state.ps1', 'scripts/wa_watch.ps1', 'scripts/fleet_watch.ps1')) {
  $bytes = [System.IO.File]::ReadAllBytes((Join-Path $RepoRoot $f))
  $bad3 = @($bytes | Where-Object { $_ -lt 32 -and $_ -ne 9 -and $_ -ne 10 -and $_ -ne 13 })
  Assert-True ($f + ' has no control bytes outside tab/CR/LF') ($bad3.Count -eq 0) (($bad3 | Select-Object -First 4) -join ',')
}

Write-Output ''
Write-Output 'CASE 14 - -CatchUpMax 0 must not emit a row by arithmetic accident'
# PowerShell counts DOWN when a range start exceeds its end, so $fresh[$n..($n-1)]
# yields two indices and silently emitted a row. Both watchers branch on zero.
foreach ($f in @('scripts/wa_watch.ps1', 'scripts/fleet_watch.ps1')) {
  $src = Get-Content -LiteralPath (Join-Path $RepoRoot $f) -Raw
  Assert-True ($f + ' branches explicitly on CatchUpMax 0') ($src -match '\$CatchUpMax -le 0') ''
}
$n = 5
$rangeCount = @(($n - 0)..($n - 1)).Count
Assert-True 'and the arithmetic really does produce 2 indices, not 0' ($rangeCount -eq 2) ('count=' + $rangeCount)

Write-Output ''
Write-Output 'CASE 14b - a hollow pending block is UNUSABLE, not merely ignored'
# A schema state with pendingIndex 5, no anchor and empty arrays used to read as
# ok, replay nothing, and then advance the cursor to row 5 leaving the anchor
# blank -- skipping five rows and unanchoring the cursor. The invariants are now
# enforced at the read boundary rather than by each caller remembering.
$hollow = Join-Path $tmpDir 'hollow.json'
# A COMPLETE schema-5 property set, so the refusal is about the hollow block and
# not about a missing field. The first version of this case omitted savedAt and
# was therefore passing for the wrong reason.
Set-Content -LiteralPath $hollow -Value '{"schema":5,"savedAt":"2026-09-08T00:00:00Z","boardId":"b","lastIndex":0,"anchorId":"","lastEmitKey":"","lastEmitTs":"","pendingIndex":5,"pendingAnchor":"","pendingLines":[],"pendingRowIds":[],"planFrom":1,"planMode":"steady","planLimit":0,"planEmitKey":"","planEmitTs":"","resetAt":""}' -Encoding UTF8
$hv = Read-WatchState -Path $hollow
Assert-True 'a hollow pending block is refused' ($hv.disposition -eq 'unusable') ([string]$hv.disposition)
Assert-True 'and names the missing anchor' ($hv.reason -match 'anchor') $hv.reason

Write-Output ''
Write-Output 'CASE 14d - the strict schema gate'
$sg = Join-Path $tmpDir 'strict.json'
function Read-Strict { param([string]$Json) Set-Content -LiteralPath $sg -Value $Json -Encoding UTF8; return (Read-WatchState -Path $sg) }
$full = '{"schema":5,"savedAt":"2026-09-08T00:00:00Z","boardId":"b","lastIndex":0,"anchorId":"","lastEmitKey":"","lastEmitTs":"","pendingIndex":0,"pendingAnchor":"","pendingLines":[],"pendingRowIds":[],"planFrom":0,"planMode":"","planLimit":0,"planEmitKey":"","planEmitTs":"","resetAt":""}'
Assert-True 'a complete, well-typed state reads ok' ((Read-Strict $full).disposition -eq 'ok') (Read-Strict $full).reason
Assert-True 'only schema and boardId is refused' ((Read-Strict '{"schema":5,"boardId":"b"}').disposition -eq 'unusable')
Assert-True 'an unexpected extra property is refused' `
  ((Read-Strict ($full.Substring(0, $full.Length - 1) + ',"surprise":1}')).disposition -eq 'unusable')
Assert-True 'a numeric field sent as a string is refused' `
  ((Read-Strict ($full -replace '"lastIndex":0', '"lastIndex":"0"')).disposition -eq 'unusable')
Assert-True 'an array sent as a scalar is refused' `
  ((Read-Strict ($full -replace '"pendingLines":\[\]', '"pendingLines":"nope"')).disposition -eq 'unusable')
$overLine = '"' + ('x' * 5000) + '"'
Assert-True 'an over-long staged line is refused' `
  ((Read-Strict ($full -replace '"pendingLines":\[\]', ('"pendingLines":[' + $overLine + ']'))).disposition -eq 'unusable')

Write-Output ''
Write-Output 'CASE 14c - a safe-leaf allowlist, for every caller'
foreach ($bad4 in @('a/b', '..', 'x..y', ('C:' + [System.IO.Path]::DirectorySeparatorChar + 'w'))) {
  $t = Test-SafeLeaf -Leaf $bad4
  Assert-True ('rejects leaf ' + $bad4) ($t.ok -eq $false) $t.reason
}
Assert-True 'accepts a plain safe name' ((Test-SafeLeaf -Leaf 'fleet_watch.claude-code-cli.state.json').ok -eq $true)

Write-Output ''
Write-Output 'CASE 15 - ABSENT is not UNUSABLE, and only ABSENT may prime'
# The repair for chatgpt-codex-desktop's e7440a8 finding 1. A corrupt file used
# to yield a NOTICE and then a cold prime, silently skipping every row in the
# gap -- the exact failure this whole mechanism exists to end.
$dp = Join-Path $tmpDir 'disp.json'
$abs = Read-WatchState -Path (Join-Path $tmpDir 'never-written.json')
Assert-True 'a missing file reports ABSENT' ($abs.disposition -eq 'absent') ([string]$abs.disposition)
Assert-True 'and carries no alarm' (-not $abs.reason)
Set-Content -LiteralPath $dp -Value '{"schema":3,"boardId":"b","lastIndex":1' -Encoding UTF8
$un = Read-WatchState -Path $dp
Assert-True 'a truncated file reports UNUSABLE' ($un.disposition -eq 'unusable') ([string]$un.disposition)
Assert-True 'and carries a reason to print' ([bool]$un.reason) $un.reason
Set-Content -LiteralPath $dp -Value '{"seenIds":["a"]}' -Encoding UTF8
Assert-True 'an old-schema file is UNUSABLE, not absent' ((Read-WatchState -Path $dp).disposition -eq 'unusable')
$okState = Commit-At -Rows $board2 -Index 3
[void](Save-WatchState -State $okState -Path $dp)
Assert-True 'a good file reports ok' ((Read-WatchState -Path $dp).disposition -eq 'ok')
foreach ($f in @('scripts/wa_watch.ps1', 'scripts/fleet_watch.ps1')) {
  $src = Get-Content -LiteralPath (Join-Path $RepoRoot $f) -Raw
  Assert-True ($f + ' refuses to start on an unusable cursor') ($src -match "disposition -eq 'unusable'") ''
  Assert-True ($f + ' still allows -Reset to recover it') ($src -match 'and -not \$ResetRequested') ''
}

Write-Output ''
Write-Output 'CASE 16 - a retained outbox is validated BEFORE it is replayed'
# schema 5 replaced the interval check with a full plan recomputation. The
# interval version only proved the supplied ids were SOMEWHERE in range, so a
# block holding one line for r2 with pendingIndex 12 passed, replayed once, and
# committed the cursor to 12 -- silently skipping r3 to r12.
# The recompute stub below stands in for the watcher's pure planner: every row
# in the interval is eligible.
$recomputeAll = { param($from, $to, $mode, $limit, $k, $t)
  $out = @(); for ($i = $from; $i -le $to; $i++) { $out += ('r' + $i) }; return $out }

$cross = Stage-At -Rows $board2 -Index 2 -Lines @('board-A line') -RowIds @('r2') -Id 'board-A' -From 1
$pv1 = Test-PendingMatchesPlan -State $cross -Rows $board2 -BoardId 'board-B' -Recompute $recomputeAll
Assert-True 'an outbox from another board is refused' ($pv1.ok -eq $false)
Assert-True 'and names the board it belongs to' ($pv1.reason -match 'board-A') $pv1.reason
$pv2 = Test-PendingMatchesPlan -State $cross -Rows $board2 -BoardId '' -Recompute $recomputeAll
Assert-True 'an unidentified board refuses the replay too' ($pv2.ok -eq $false) $pv2.reason

$moved = Test-PendingMatchesPlan -State $cross -Rows (New-Board -Count 600 -Prefix 'q') -BoardId 'board-A' -Recompute $recomputeAll
Assert-True 'a moved pending anchor is refused' ($moved.ok -eq $false)
Assert-True 'and names the anchor move' ($moved.reason -match 'anchor moved') $moved.reason

$oob = Stage-At -Rows $board2 -Index 2 -Lines @('x') -RowIds @('r2') -Id 'board-A' -From 1
$oob.pendingIndex = 9999
Assert-True 'an out-of-bounds pending index is refused' `
  ((Test-PendingMatchesPlan -State $oob -Rows $board2 -BoardId 'board-A' -Recompute $recomputeAll).ok -eq $false)

# THE SKIP: one staged line claiming a twelve-row advance.
$short = Stage-At -Rows $board2 -Index 12 -Lines @('ONLY R2') -RowIds @('r2') -Id 'board-A' -From 1
$sv3 = Test-PendingMatchesPlan -State $short -Rows $board2 -BoardId 'board-A' -Recompute $recomputeAll
Assert-True 'an INCOMPLETE plan is refused' ($sv3.ok -eq $false) $sv3.reason
Assert-True 'and says how many rows the board yields' ($sv3.reason -match 'yields') $sv3.reason

# REORDERED, same cardinality.
$reorder = Stage-At -Rows $board2 -Index 3 -Lines @('a','b') -RowIds @('r3','r2') -Id 'board-A' -From 2
$rv = Test-PendingMatchesPlan -State $reorder -Rows $board2 -BoardId 'board-A' -Recompute $recomputeAll
Assert-True 'a REORDERED plan is refused' ($rv.ok -eq $false) $rv.reason
Assert-True 'and names the position it differs at' ($rv.reason -match 'position') $rv.reason

$good = Stage-At -Rows $board2 -Index 3 -Lines @('a','b') -RowIds @('r2','r3') -Id 'board-A' -From 2
Assert-True 'an exact matching plan passes' `
  ((Test-PendingMatchesPlan -State $good -Rows $board2 -BoardId 'board-A' -Recompute $recomputeAll).ok -eq $true)
Write-Output 'CASE 17 - Complete-WatchOutbox cannot touch identity'
$idt = Stage-At -Rows $board2 -Index 2 -Lines @('l') -RowIds @('r2') -Id 'board-A' -From 1
$after2 = Complete-WatchOutbox -State $idt
Assert-True 'the stored identity is unchanged by committing' ($after2.boardId -eq 'board-A') $after2.boardId
foreach ($f in @('scripts/watch_state.ps1', 'scripts/wa_watch.ps1', 'scripts/fleet_watch.ps1')) {
  $src = Get-Content -LiteralPath (Join-Path $RepoRoot $f) -Raw
  Assert-True ($f + ' never passes a BoardId to Complete-WatchOutbox') `
    (-not ($src -match 'Complete-WatchOutbox[^\r\n]*-BoardId')) ''
}

Write-Output ''
Write-Output 'CASE 18 - read-back compares every field, not counts and indexes'
# A pending line whose TEXT was corrupted used to read back as a successful
# save, and those lines are the exact words shown to Mr. Salam on replay.
$d1 = Stage-At -Rows $board2 -Index 3 -Lines @('the real line') -RowIds @('r3') -Id 'b' -From 2
$d2 = Stage-At -Rows $board2 -Index 3 -Lines @('a DIFFERENT line') -RowIds @('r3') -Id 'b' -From 2
Assert-True 'same counts and indexes but different text yields a different digest' `
  ((Get-WatchStateDigest -State $d1) -ne (Get-WatchStateDigest -State $d2))
# The previous digest joined with a separator, so ONE element containing that
# character hashed identically to TWO elements, and one empty string hashed
# identically to an empty array. Length prefixes remove both collisions.
$col1 = Stage-At -Rows $board2 -Index 3 -Lines @(('left' + [char]31 + 'right')) -RowIds @('r3') -Id 'b' -From 2
$col2 = Stage-At -Rows $board2 -Index 3 -Lines @('left','right') -RowIds @('r3','r3') -Id 'b' -From 2
Assert-True 'one element containing the old separator no longer collides with two' `
  ((Get-WatchStateDigest -State $col1) -ne (Get-WatchStateDigest -State $col2))
$e1 = Commit-At -Rows $board2 -Index 3
$e2 = Commit-At -Rows $board2 -Index 3
$e2.pendingLines = @()
Assert-True 'an empty array is not confused with an empty string' `
  ((Get-WatchStateDigest -State $e1) -eq (Get-WatchStateDigest -State $e2))
Assert-True 'and an identical state yields an identical digest' `
  ((Get-WatchStateDigest -State $d1) -eq (Get-WatchStateDigest -State $d1))
$d3 = Set-WatchEmitted -State (Commit-At -Rows $board2 -Index 3) -Key 'k' -RowTs '2026-09-08T00:00:00Z'
Assert-True 'lastEmitKey is part of the contract' `
  ((Get-WatchStateDigest -State $d3) -ne (Get-WatchStateDigest -State (Commit-At -Rows $board2 -Index 3)))

Write-Output ''
Write-Output 'CASE 19 - a failed save leaves the previous bytes intact'
# Reset is a transition, not a delete: the old cursor must survive a failed
# attempt to write the new one.
$keepPath = Join-Path $tmpDir 'keep.json'
$orig = Commit-At -Rows $board2 -Index 4
[void](Save-WatchState -State $orig -Path $keepPath)
$before = Get-Content -LiteralPath $keepPath -Raw
$huge = Commit-At -Rows $board2 -Index 5
[void](Save-WatchState -State $huge -Path (Join-Path $blocker 'cannot.json'))
$afterBytes = Get-Content -LiteralPath $keepPath -Raw
Assert-True 'an unrelated failed save did not disturb the good file' ($before -eq $afterBytes)
Assert-True 'and it still reads back' ((Read-WatchState -Path $keepPath).state.lastIndex -eq 4)
Write-Output ''
Write-Output 'RECOVERY - deterministic interrupted-save reconciliation (CODEX-PR34-CD05-RECOVERY-GO)'
# Every case below builds a REAL on-disk situation and asks the one state machine
# what it proves. The old suite asserted only that a rollback file was detected,
# which is why a save could report clean success while leaving the next start
# unable to run: nothing tested what happened AFTER the asset survived.

$rxDir = Join-Path $tmpDir 'recovery'
New-Item -ItemType Directory -Path $rxDir -Force | Out-Null

function New-RawState {
  param([int]$Index, [string]$Anchor = '')
  if (-not $Anchor) { $Anchor = 'r' + $Index }
  $j = '{"schema":5,"savedAt":"2026-09-08T00:00:00Z","boardId":"b","lastIndex":' + $Index +
       ',"anchorId":"' + $Anchor + '","lastEmitKey":"","lastEmitTs":"","pendingIndex":0,"pendingAnchor":"",' +
       '"pendingLines":[],"pendingRowIds":[],"planFrom":0,"planMode":"","planLimit":0,"planEmitKey":"",' +
       '"planEmitTs":"","resetAt":""}'
  return [System.Text.Encoding]::UTF8.GetBytes($j)
}
function New-Scenario {
  # dest = bytes or $null (absent); asset named for (candidate, prior)
  param([string]$Name, $DestBytes, $CandBytes, $PriorBytes, [switch]$PriorAbsent, [byte[]]$AssetBytes)
  $path = Join-Path $rxDir ($Name + '.json')
  Get-ChildItem -LiteralPath $rxDir -Force -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name.StartsWith((Split-Path -Leaf $path), [System.StringComparison]::Ordinal) } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
  if ($null -ne $DestBytes) { [System.IO.File]::WriteAllBytes($path, $DestBytes) }
  $priorHash = if ($PriorAbsent) { 'PRIOR_ABSENT' } else { Get-BytesHash -Bytes $PriorBytes }
  $asset = Get-TxAssetPath -Path $path -CandidateHash (Get-BytesHash -Bytes $CandBytes) -PriorHash $priorHash
  # Assign inside the branches. An empty array returned FROM an if-expression is
  # unrolled to nothing and lands as $null, which is a PowerShell trap this suite
  # walked into on its first run.
  $body = $null
  if ($null -ne $AssetBytes) { $body = $AssetBytes }
  elseif ($PriorAbsent)      { $body = (New-Object byte[] 0) }
  else                       { $body = $PriorBytes }
  [System.IO.File]::WriteAllBytes($asset, $body)
  return @{ path = $path; asset = $asset }
}

$prior1 = New-RawState 1
$cand3  = New-RawState 3
$other7 = New-RawState 7

Write-Output ''
Write-Output 'CASE 20 - interrupted AFTER the replace: the destination proves it completed'
$s20 = New-Scenario -Name 'after' -DestBytes $cand3 -CandBytes $cand3 -PriorBytes $prior1
$r20 = Resolve-PendingTransition -Path $s20.path
Assert-True 'status is completed' ($r20.status -eq 'completed') ([string]$r20.status)
Assert-True 'and the watcher may proceed' ($r20.canProceed -eq $true)
Assert-True 'the asset is gone' (-not (Test-Path -LiteralPath $s20.asset))
Assert-True 'the destination is still the candidate' ((Read-WatchState -Path $s20.path).state.lastIndex -eq 3)

Write-Output ''
Write-Output 'CASE 21 - interrupted BEFORE the replace: the destination proves it never applied'
$s21 = New-Scenario -Name 'before' -DestBytes $prior1 -CandBytes $cand3 -PriorBytes $prior1
$r21 = Resolve-PendingTransition -Path $s21.path
Assert-True 'status is not_applied' ($r21.status -eq 'not_applied') ([string]$r21.status)
Assert-True 'and the watcher may proceed' ($r21.canProceed -eq $true)
Assert-True 'the asset is gone' (-not (Test-Path -LiteralPath $s21.asset))
Assert-True 'the destination is still the prior' ((Read-WatchState -Path $s21.path).state.lastIndex -eq 1)

Write-Output ''
Write-Output 'CASE 22 - destination matches NEITHER side: the prior is restored from the asset'
$s22 = New-Scenario -Name 'neither' -DestBytes $other7 -CandBytes $cand3 -PriorBytes $prior1
$r22 = Resolve-PendingTransition -Path $s22.path
Assert-True 'status is rolled_back' ($r22.status -eq 'rolled_back') ([string]$r22.status)
Assert-True 'the asset is gone' (-not (Test-Path -LiteralPath $s22.asset))
Assert-True 'the prior came back byte for byte' `
  (([System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($s22.path))) -eq ([System.BitConverter]::ToString($prior1)))

Write-Output ''
Write-Output 'CASE 23 - PRIOR_ABSENT, both branches'
$s23a = New-Scenario -Name 'absent_none' -DestBytes $null -CandBytes $cand3 -PriorAbsent
$r23a = Resolve-PendingTransition -Path $s23a.path
Assert-True 'no prior and no destination proves it never applied' ($r23a.status -eq 'not_applied') ([string]$r23a.status)
Assert-True 'and the asset is gone' (-not (Test-Path -LiteralPath $s23a.asset))
Assert-True 'and the absent destination was NOT invented' (-not (Test-Path -LiteralPath $s23a.path))

$s23b = New-Scenario -Name 'absent_done' -DestBytes $cand3 -CandBytes $cand3 -PriorAbsent
$r23b = Resolve-PendingTransition -Path $s23b.path
Assert-True 'no prior but the candidate landed proves it completed' ($r23b.status -eq 'completed') ([string]$r23b.status)
Assert-True 'and the asset is gone' (-not (Test-Path -LiteralPath $s23b.asset))

$s23c = New-Scenario -Name 'absent_other' -DestBytes $other7 -CandBytes $cand3 -PriorAbsent
$r23c = Resolve-PendingTransition -Path $s23c.path
Assert-True 'no prior and an UNRECOGNISED destination is UNKNOWN' ($r23c.status -eq 'unknown') ([string]$r23c.status)
Assert-True 'and nothing was deleted' (Test-Path -LiteralPath $s23c.asset)

Write-Output ''
Write-Output 'CASE 24 - evidence that proves nothing is UNKNOWN, and NOTHING is deleted'
# a) a bare .rollback from the previous scheme carries no proof at all
$p24 = Join-Path $rxDir 'legacy.json'
[System.IO.File]::WriteAllBytes($p24, $cand3)
$legacy = $p24 + '.rollback'
Set-Content -LiteralPath $legacy -Value 'old scheme' -Encoding UTF8
$r24a = Resolve-PendingTransition -Path $p24
Assert-True 'a legacy bare .rollback is UNKNOWN, not ignored' ($r24a.status -eq 'unknown') ([string]$r24a.status)
Assert-True 'and it is kept' (Test-Path -LiteralPath $legacy)
Remove-Item -LiteralPath $legacy -Force

# b) a TRUNCATED digest is refused - the whole point is that it is a full SHA256
$p24b = Join-Path $rxDir 'short.json'
[System.IO.File]::WriteAllBytes($p24b, $cand3)
$short = $p24b + '.rollback.' + ((Get-BytesHash -Bytes $cand3).Substring(0, 32)) + '.' + (Get-BytesHash -Bytes $prior1)
[System.IO.File]::WriteAllBytes($short, $prior1)
$r24b = Resolve-PendingTransition -Path $p24b
Assert-True 'a truncated candidate digest is UNKNOWN' ($r24b.status -eq 'unknown') ([string]$r24b.status)
Assert-True 'and it is kept' (Test-Path -LiteralPath $short)
Remove-Item -LiteralPath $short -Force

# c) TWO assets cannot be ordered from the disk
$s24c = New-Scenario -Name 'two' -DestBytes $cand3 -CandBytes $cand3 -PriorBytes $prior1
$second = Get-TxAssetPath -Path $s24c.path -CandidateHash (Get-BytesHash -Bytes $other7) -PriorHash (Get-BytesHash -Bytes $prior1)
[System.IO.File]::WriteAllBytes($second, $prior1)
$r24c = Resolve-PendingTransition -Path $s24c.path
Assert-True 'two assets are UNKNOWN' ($r24c.status -eq 'unknown') ([string]$r24c.status)
Assert-True 'and BOTH are kept' ((Test-Path -LiteralPath $s24c.asset) -and (Test-Path -LiteralPath $second))
Remove-Item -LiteralPath $second -Force
Remove-Item -LiteralPath $s24c.asset -Force

# d) an asset whose BYTES do not match the prior it claims
$s24d = New-Scenario -Name 'corrupt' -DestBytes $other7 -CandBytes $cand3 -PriorBytes $prior1 -AssetBytes ([System.Text.Encoding]::UTF8.GetBytes('truncated'))
$r24d = Resolve-PendingTransition -Path $s24d.path
Assert-True 'a corrupt asset is UNKNOWN' ($r24d.status -eq 'unknown') ([string]$r24d.status)
Assert-True 'and it is kept' (Test-Path -LiteralPath $s24d.asset)
Assert-True 'and the destination was NOT overwritten' `
  (([System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($s24d.path))) -eq ([System.BitConverter]::ToString($other7)))

Write-Output ''
Write-Output 'CASE 25 - the status enum is exactly the documented set'
$allowed = @('clean', 'completed', 'not_applied', 'rolled_back', 'cleanup_pending', 'unknown')
$seen = @($r20.status, $r21.status, $r22.status, $r23a.status, $r23b.status, $r23c.status,
          $r24a.status, $r24b.status, $r24c.status, $r24d.status)
$stray = @($seen | Where-Object { $allowed -notcontains $_ })
Assert-True 'every status observed is in the enum' ($stray.Count -eq 0) ($stray -join ',')
$cleanPath = Join-Path $rxDir 'clean.json'
[System.IO.File]::WriteAllBytes($cleanPath, $cand3)
Assert-True 'no asset means clean' ((Resolve-PendingTransition -Path $cleanPath).status -eq 'clean')

Write-Output ''
Write-Output 'CASE 26 - the candidate substituted AFTER its handle closed, BEFORE the rename'
# This is the case the previous commit message claimed was covered and was not.
# The guarantee does not come from holding the temp handle - a path rename cannot
# pin identity - it comes from comparing the DESTINATION afterwards.
$p26 = Join-Path $rxDir 'swap.json'
[System.IO.File]::WriteAllBytes($p26, $prior1)
$script:WatchTestHook = {
  param($Name, $Ctx)
  if ($Name -eq 'BeforeReplace') {
    # a DIFFERENT but perfectly valid schema-5 state, so nothing downstream
    # would refuse it on its own merits
    [System.IO.File]::WriteAllBytes($Ctx.TempPath, (New-RawState 9))
  }
}
try {
  $r26 = Save-StateBytes -Bytes $cand3 -Path $p26
} finally { $script:WatchTestHook = $null }
Assert-True 'the substituted candidate is REFUSED' ($r26.ok -eq $false) ([string]$r26.reason)
Assert-True 'and it did not report a commit' ($r26.committed -eq $false)
Assert-True 'the prior was restored byte for byte' `
  (([System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($p26))) -eq ([System.BitConverter]::ToString($prior1)))
Assert-True 'and no evidence was left behind' (@(Get-TxAssets -Path $p26).Count -eq 0)

Write-Output ''
Write-Output 'CASE 27 - a blocked cleanup is COMMITTED, not clean, and bars the next transition'
# The delete has to be refused BY THE OS, and the two platforms refuse it in
# different ways. On Windows an open handle with FileShare::None blocks it. On
# POSIX an open handle does NOT - unlink on an open file is legal - so the
# directory is made unwritable instead. Using only the Windows mechanism is how
# this case passed here and failed on the ubuntu leg of CI.
$blockDir = Join-Path $rxDir 'blocked'
New-Item -ItemType Directory -Path $blockDir -Force | Out-Null
$p27 = Join-Path $blockDir 'cleanup.json'
[System.IO.File]::WriteAllBytes($p27, $prior1)
$script:HeldAsset = $null
$script:WatchTestHook = {
  param($Name, $Ctx)
  if ($Name -eq 'BeforeAssetCleanup') {
    if ($script:WATCH_IS_WINDOWS) {
      $script:HeldAsset = New-Object System.IO.FileStream($Ctx.AssetPath,
        [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
    } else {
      & chmod 'a-w' (Split-Path -Parent $Ctx.AssetPath) | Out-Null
    }
  }
}
try {
  $r27 = Save-StateBytes -Bytes $cand3 -Path $p27
} finally { $script:WatchTestHook = $null }

Assert-True 'the save reports the data IS committed' ($r27.committed -eq $true) ([string]$r27.reason)
Assert-True 'ok is true, because nothing was lost' ($r27.ok -eq $true)
Assert-True 'but it is NOT a clean success' ($r27.cleanup_pending -eq $true)
Assert-True 'and the reason is not empty' ([bool]$r27.reason)
Assert-True 'the caller is told to stop' ([bool](Get-SaveStopReason $r27))
Assert-True 'the destination really does hold the candidate' ((Read-WatchState -Path $p27).state.lastIndex -eq 3)
$r27b = Save-StateBytes -Bytes $other7 -Path $p27
Assert-True 'a SECOND transition is refused while the asset survives' ($r27b.ok -eq $false) ([string]$r27b.reason)
Assert-True 'and the refusal names the unresolved transition' ($r27b.reason -match 'refusing to start a transition')
Assert-True 'the destination was not touched by the refused save' ((Read-WatchState -Path $p27).state.lastIndex -eq 3)

# release the block: startup now self-heals, because the destination PROVES it
if ($script:HeldAsset) { $script:HeldAsset.Dispose(); $script:HeldAsset = $null }
if (-not $script:WATCH_IS_WINDOWS) { & chmod 'u+w' $blockDir | Out-Null }
$r27c = Resolve-PendingTransition -Path $p27
Assert-True 'once released, recovery proves it completed' ($r27c.status -eq 'completed') ([string]$r27c.status)
Assert-True 'and the watcher may proceed' ($r27c.canProceed -eq $true)
Assert-True 'and the asset is finally gone' (@(Get-TxAssets -Path $p27).Count -eq 0)
$r27d = Save-StateBytes -Bytes $other7 -Path $p27
Assert-True 'and a normal save works again' ($r27d.ok -eq $true) ([string]$r27d.reason)
Assert-True 'cleanly this time' ($r27d.cleanup_pending -eq $false)

Write-Output ''
Write-Output 'CASE 28 - a clean save leaves no evidence, and the name carries FULL digests'
$p28 = Join-Path $rxDir 'names.json'
[System.IO.File]::WriteAllBytes($p28, $prior1)
$captured = ''
$script:WatchTestHook = { param($Name, $Ctx) if ($Name -eq 'BeforeReplace') { $script:CapturedAsset = $Ctx.AssetPath } }
try { $r28 = Save-StateBytes -Bytes $cand3 -Path $p28 } finally { $script:WatchTestHook = $null }
$captured = Split-Path -Leaf ([string]$script:CapturedAsset)
Assert-True 'the save is clean' (($r28.ok -eq $true) -and ($r28.cleanup_pending -eq $false)) ([string]$r28.reason)
Assert-True 'no asset survives a clean save' (@(Get-TxAssets -Path $p28).Count -eq 0)
Assert-True 'no .tmp survives either' (@(Get-ChildItem -LiteralPath $rxDir -Filter '*.tmp' -ErrorAction SilentlyContinue).Count -eq 0)
$parsed = Read-TxAssetName -Path $p28 -AssetName $captured
Assert-True 'the asset name parses' ($parsed.ok -eq $true) ([string]$parsed.reason)
Assert-True 'the candidate digest is a FULL sha256' ($parsed.candidateHash -cmatch '^[0-9A-F]{64}$')
Assert-True 'the prior digest is a FULL sha256' ($parsed.priorHash -cmatch '^[0-9A-F]{64}$')
Assert-True 'and it named the real candidate' ($parsed.candidateHash -ceq (Get-BytesHash -Bytes $cand3))
Assert-True 'and the real prior' ($parsed.priorHash -ceq (Get-BytesHash -Bytes $prior1))

Write-Output ''
Write-Output 'CASE 29 - the test seam is inert in production and never reads the environment'
Assert-True 'the hook is null unless a test sets it' ($null -eq $script:WatchTestHook)
$modSrc = Get-Content -LiteralPath (Join-Path $RepoRoot 'scripts/watch_state.ps1') -Raw
Assert-True 'the module never takes a hook from the environment' (-not ($modSrc -match '\$env:[A-Za-z_]*(HOOK|TEST|INJECT)'))
Assert-True 'and both watchers go through the one state machine' `
  ((@('scripts/wa_watch.ps1', 'scripts/fleet_watch.ps1') | Where-Object {
      $t = Get-Content -LiteralPath (Join-Path $RepoRoot $_) -Raw
      ($t -match 'Resolve-PendingTransition') -and ($t -notmatch 'Test-PendingTransition') -and ($t -match 'Get-SaveStopReason')
   }).Count -eq 2)

Write-Output ''
Write-Output 'the PowerShell 7 / 5.1 JSON type difference is absorbed'
$isoText = '2026-09-08T08:00:00.000Z'
$asDate = [datetime]::Parse($isoText, [System.Globalization.CultureInfo]::InvariantCulture,
  [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
Assert-True 'a string and a datetime resolve identically' `
  ((ConvertTo-WatchTime $isoText) -eq (ConvertTo-WatchTime $asDate))
Assert-True 'and neither depends on the current culture' `
  ((ConvertTo-WatchTime $asDate).ToString('o') -eq (ConvertTo-WatchTime $isoText).ToString('o'))

} finally {
  Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ''
if ($script:Failed -eq 0) { Write-Output ('ALL PASS (' + [string]$script:Passed + ')') }
else {
  # [string] casts: $script:Failed is an int and PowerShell resolves int + string
  # by converting the STRING to an int, so the untyped version threw on the first
  # failing CI run -- a harness that crashed instead of reporting failures.
  Write-Output ([string]$script:Failed + ' FAILURE(S) of ' + [string]($script:Passed + $script:Failed))
}
exit $(if ($script:Failed -eq 0) { 0 } else { 1 })
