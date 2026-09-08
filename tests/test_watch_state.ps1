#Requires -Version 5.1
<#
SFDC24 - positional append cursor for the board watchers
claude-code-cli, 2026-09-08. Cases specified by codex in
CODEX-PR34-WATERMARK-GO-20260908T085300Z.

WHAT IS ON TRIAL
  Two earlier designs for this state both LOST MESSAGES, and both passed their
  own tests:

    v0  kept the newest 400 row ids and rescanned the whole board. Past 400
        qualifying rows the oldest fell out and a restart replayed ancient rows
        as "(missed while offline)".
    v1  replaced that with a timestamp watermark. A row appended later but
        stamped more than ten minutes behind was skipped FOREVER.

  Both failures share a shape: the watcher looked healthy while quietly dropping
  or inventing messages, and no assertion existed that could tell the difference.
  So these cases are written against the FAILURES, not the happy path.

RUN
  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_watch_state.ps1
  pwsh -NoProfile -File tests/test_watch_state.ps1
#>

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
# Forward slash: this must run under pwsh on Linux in CI as well as WinPS here.
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
# Named $BoardFileId rather than $BOARD on purpose. PowerShell variable names are
# case-insensitive, so $BOARD and the $board row array below were the SAME
# variable: the id was silently overwritten by a row set, and Set-WatchPosition
# received an array where it wanted a string.
$BoardFileId = 'board-file-id-1'

try {

Write-Output ''
Write-Output 'CASE 1 - restart with more than 400 rows behind the cursor (the v0 bug)'
$board = New-Board -Count 500
$s = New-WatchState
$c = Test-BoardContinuity -State $null -Rows $board -BoardId $BoardFileId
Assert-True 'a fresh watcher primes rather than replaying' ($c.prime -eq $true)
$s = Set-WatchPosition -State $s -Rows $board -Index 500 -BoardId $BoardFileId
$path = Join-Path $tmpDir 'c1.json'
$sv = Save-WatchState -State $s -Path $path
Assert-True 'state persists' ($sv.ok -eq $true) $sv.reason
$re = Read-WatchState -Path $path
$board2 = $board + ,(New-Row -Id 'r501' -Ts '2026-09-02T00:00:00Z')
$c2 = Test-BoardContinuity -State $re.state -Rows $board2 -BoardId $BoardFileId
Assert-True 'resume is possible' ($c2.ok -eq $true -and $c2.prime -eq $false) $c2.reason
Assert-True 'resumes at exactly the next row, not row 1' ($c2.startIndex -eq 501) ('startIndex=' + $c2.startIndex)
Assert-True 'so 500 ancient rows are NOT replayed as missed' (($board2.Count - 1) - $c2.startIndex + 1 -eq 1)

Write-Output ''
Write-Output 'CASE 2 - a row appended late carrying an OLD valid timestamp (the v1 bug)'
# v1 compared timestamps, so this row was invisible forever. Position does not
# care what clock the writer had.
$board3 = $board2 + ,(New-Row -Id 'late' -Ts '2026-08-01T00:00:00Z')
$c3 = Test-BoardContinuity -State $re.state -Rows $board3 -BoardId $BoardFileId
$considered = @()
for ($i = $c3.startIndex; $i -le ($board3.Count - 1); $i++) { $considered += [string]$board3[$i][0] }
Assert-True 'the late-but-old-stamped row IS considered' ($considered -contains 'late') ($considered -join ',')

Write-Output ''
Write-Output 'CASE 3 - the board shrank'
$short = New-Board -Count 10
$c4 = Test-BoardContinuity -State $re.state -Rows $short -BoardId $BoardFileId
Assert-True 'refuses to resume' ($c4.ok -eq $false)
Assert-True 'and says why, naming the shrink' ($c4.reason -match 'shrank') $c4.reason
Assert-True 'and does not silently prime' ($c4.prime -eq $false)

Write-Output ''
Write-Output 'CASE 4 - the anchor moved (compaction or rewrite)'
$moved = New-Board -Count 501 -Prefix 'x'
$c5 = Test-BoardContinuity -State $re.state -Rows $moved -BoardId $BoardFileId
Assert-True 'refuses to resume' ($c5.ok -eq $false)
Assert-True 'and names the anchor mismatch' ($c5.reason -match 'anchor mismatch') $c5.reason

Write-Output ''
Write-Output 'CASE 4b - a different board entirely'
$c6 = Test-BoardContinuity -State $re.state -Rows $board2 -BoardId 'a-different-file-id'
Assert-True 'refuses to resume' ($c6.ok -eq $false)
Assert-True 'and names the identity change' ($c6.reason -match 'identity changed') $c6.reason

Write-Output ''
Write-Output 'CASE 5 - corrupt, empty, truncated and stale-schema state'
$bad = Join-Path $tmpDir 'bad.json'
Set-Content -LiteralPath $bad -Value '{"schema":2,"lastIndex":5' -Encoding UTF8
$r1 = Read-WatchState -Path $bad
Assert-True 'a truncated file yields no state' ($null -eq $r1.state)
Assert-True 'and a reason to print' ([bool]$r1.reason) $r1.reason
Set-Content -LiteralPath $bad -Value '' -Encoding UTF8
$r2 = Read-WatchState -Path $bad
Assert-True 'an empty file yields no state, with a reason' (($null -eq $r2.state) -and $r2.reason)
Set-Content -LiteralPath $bad -Value '{"seenIds":["a","b"]}' -Encoding UTF8
$r3 = Read-WatchState -Path $bad
Assert-True 'the OLD v0 id-set format is refused, not misread' ($null -eq $r3.state)
Assert-True 'and is reported as a schema difference, not corruption' ($r3.reason -match 'schema') $r3.reason
$r4 = Read-WatchState -Path (Join-Path $tmpDir 'absent.json')
Assert-True 'a missing file is simply a cold start' (($null -eq $r4.state) -and (-not $r4.reason))

Write-Output ''
Write-Output 'CASE 6 - persistence failure is reported, never assumed'
$sv2 = Save-WatchState -State $s -Path (Join-Path $tmpDir "no`0such/dir/x.json")
Assert-True 'an unwritable path returns ok=false' ($sv2.ok -eq $false)
Assert-True 'with a reason the caller can print' ([bool]$sv2.reason) $sv2.reason

Write-Output ''
Write-Output 'CASE 7 - no temp file is left behind, and a stray one is inert'
$p7 = Join-Path $tmpDir 'c7.json'
[void](Save-WatchState -State $s -Path $p7)
[void](Save-WatchState -State $s -Path $p7)     # second write exercises the replace path
Assert-True 'no .tmp remains after a successful save' (-not (Test-Path -LiteralPath ($p7 + '.tmp')))
Set-Content -LiteralPath ($p7 + '.tmp') -Value '{"junk":true' -Encoding UTF8
$r7 = Read-WatchState -Path $p7
Assert-True 'a stray .tmp from a dead run does not affect real state' `
  ($r7.state -and $r7.state.lastIndex -eq 500) ([string]$r7.state.lastIndex)
Remove-Item -LiteralPath ($p7 + '.tmp') -Force -ErrorAction SilentlyContinue
[void](Save-WatchState -State $s -Path $p7)
Assert-True 'the replace path is genuinely atomic (destination never absent)' (Test-Path -LiteralPath $p7)

Write-Output ''
Write-Output 'CASE 8 - identical text sent HOURS apart while offline (the dedupe bug)'
# The previous window compared processing time, so a restart that replayed both
# back-to-back threw the second away. These are two messages, not one.
$d = New-WatchState
$d = Set-WatchEmitted -State $d -Key 'whatsapp::ok' -RowTs '2026-09-08T01:00:00Z'
Assert-True 'the same text three hours later is NOT a duplicate' `
  (-not (Test-RowIsDuplicate -State $d -Key 'whatsapp::ok' -RowTs '2026-09-08T04:00:00Z'))
Assert-True 'nor two minutes later' `
  (-not (Test-RowIsDuplicate -State $d -Key 'whatsapp::ok' -RowTs '2026-09-08T01:02:00Z'))

Write-Output ''
Write-Output 'CASE 9 - the console double-post, seconds apart'
Assert-True 'the same text two seconds later IS collapsed' `
  (Test-RowIsDuplicate -State $d -Key 'whatsapp::ok' -RowTs '2026-09-08T01:00:02Z')
Assert-True 'different text at the same instant is not collapsed' `
  (-not (Test-RowIsDuplicate -State $d -Key 'whatsapp::different' -RowTs '2026-09-08T01:00:02Z'))
Assert-True 'an undateable row is never collapsed' `
  (-not (Test-RowIsDuplicate -State $d -Key 'whatsapp::ok' -RowTs 'not-a-date'))

Write-Output ''
Write-Output 'the PowerShell 7 / 5.1 JSON type difference is absorbed'
# pwsh deserialises an ISO string into a [datetime]; 5.1 leaves it a string. A
# naive cast then renders in the current culture -- CI printed "09/08/2026
# 08:00:00" when this was wrong. This runs on both shells.
$isoText = '2026-09-08T08:00:00.000Z'
$asDate = [datetime]::Parse($isoText, [System.Globalization.CultureInfo]::InvariantCulture,
  [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
Assert-True 'a string and a datetime resolve identically' `
  ((ConvertTo-WatchTime $isoText) -eq (ConvertTo-WatchTime $asDate))
Assert-True 'and neither depends on the current culture' `
  ((ConvertTo-WatchTime $asDate).ToString('o') -eq (ConvertTo-WatchTime $isoText).ToString('o'))
$dt = New-WatchState
$dt = Set-WatchEmitted -State $dt -Key 'k' -RowTs $asDate
Assert-True 'a datetime stored as lastEmitTs round-trips to a canonical string' `
  (($dt.lastEmitTs -is [string]) -and $dt.lastEmitTs.EndsWith('Z')) ([string]$dt.lastEmitTs)

} finally {
  Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ''
if ($script:Failed -eq 0) { Write-Output ('ALL PASS (' + [string]$script:Passed + ')') }
else {
  # [string] casts: $script:Failed is an int and PowerShell resolves int + string
  # by converting the STRING to an int, so the untyped version threw on the first
  # failing CI run -- a harness that crashed instead of reporting the failures it
  # exists to show.
  Write-Output ([string]$script:Failed + ' FAILURE(S) of ' + [string]($script:Passed + $script:Failed))
}
exit $(if ($script:Failed -eq 0) { 0 } else { 1 })
