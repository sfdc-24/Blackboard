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
  $s = Set-WatchOutbox -State $s -Rows $Rows -Index $Index -Lines @() -RowIds @()
  return (Complete-WatchOutbox -State $s -BoardId $Id)
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
Set-Content -LiteralPath $bad -Value '{"schema":3,"lastIndex":5' -Encoding UTF8
$r1 = Read-WatchState -Path $bad
Assert-True 'a truncated file yields no state, with a reason' (($null -eq $r1.state) -and $r1.reason) $r1.reason
Set-Content -LiteralPath $bad -Value '' -Encoding UTF8
Assert-True 'an empty file yields no state, with a reason' ($null -eq (Read-WatchState -Path $bad).state)
Set-Content -LiteralPath $bad -Value '{"seenIds":["a","b"]}' -Encoding UTF8
$r3 = Read-WatchState -Path $bad
Assert-True 'the v0 id-set format is refused, not misread' ($null -eq $r3.state)
Assert-True 'and reported as a schema difference' ($r3.reason -match 'schema') $r3.reason
Set-Content -LiteralPath $bad -Value '{"schema":2,"boardId":"b","lastIndex":3,"anchorId":"a"}' -Encoding UTF8
Assert-True 'the v2 format is refused too' ($null -eq (Read-WatchState -Path $bad).state)
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
$ob = New-WatchState
$ob.boardId = $BoardFileId
$ob = Set-WatchOutbox -State $ob -Rows $board2 -Index 501 -Lines @('line one','line two') -RowIds @('r501','late')
$p10 = Join-Path $tmpDir 'c10.json'
$stage = Save-WatchState -State $ob -Path $p10
Assert-True 'staging the outbox reports success' ($stage.ok -eq $true) $stage.reason
# --- simulated crash here: the process dies before emitting ---
$after = (Read-WatchState -Path $p10).state
Assert-True 'the next start sees a retained outbox' (Test-HasOutbox -State $after)
Assert-True 'carrying the EXACT lines that were never shown' `
  ((@($after.pendingLines) -join '|') -eq 'line one|line two') (@($after.pendingLines) -join '|')
Assert-True 'and the committed cursor did NOT advance' ($after.lastIndex -eq 0) ([string]$after.lastIndex)
$done = Complete-WatchOutbox -State $after -BoardId $BoardFileId
Assert-True 'committing advances the cursor to the staged index' ($done.lastIndex -eq 501) ([string]$done.lastIndex)
Assert-True 'and clears the outbox' (-not (Test-HasOutbox -State $done))
$cs = Save-WatchState -State $done -Path $p10
Assert-True 'the commit save reports success' ($cs.ok -eq $true) $cs.reason
Assert-True 'a second start after a clean commit replays nothing' `
  (-not (Test-HasOutbox -State (Read-WatchState -Path $p10).state))

Write-Output ''
Write-Output 'CASE 10b - a failed COMMIT retains the outbox rather than dropping it'
$keep = Set-WatchOutbox -State (Commit-At -Rows $board2 -Index 500) -Rows $board2 -Index 501 -Lines @('kept') -RowIds @('r501')
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
