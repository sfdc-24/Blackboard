#Requires -Version 5.1
<#
SFDC24 - watermark state for the board watchers
claude-code-cli, 2026-09-08

WHAT IS ON TRIAL
  codex, reviewing PR34, found that both watchers kept "the newest 400 ids seen"
  and rescanned the entire append-only board every poll. Past 400 qualifying
  rows, evicted ancient rows read as unseen and a restart replayed them as
  "(missed while offline)" -- presenting Mr. Salam with weeks-old messages as
  though nobody had answered them. A watcher that cries wolf is worse than none,
  because the real message becomes one line in a wall of false ones.

  So these assertions are about the property that replaces it: the state is a
  WATERMARK, bounded and un-evictable, and

    - a row older than the watermark is never new, however large the board grows;
    - a row inside the clock-skew window is still de-duplicated by id, because
      instances on different machines stamp their own timestamps;
    - a truncated or corrupt state file fails to a cold start rather than
      replaying history, and cannot be produced by killing a write.

RUN
  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_watch_state.ps1
#>

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
# Forward slash: this resolved on the Linux runner by luck rather than contract,
# and a path separator is not worth relying on luck for.
. (Join-Path $RepoRoot 'scripts/watch_state.ps1')

$script:Passed = 0
$script:Failed = 0
function Assert-True {
  param([string]$Name, [bool]$Condition, [string]$Detail = '')
  if ($Condition) { $script:Passed++; Write-Output ('  PASS  ' + $Name) }
  else { $script:Failed++; Write-Output ('  FAIL  ' + $Name + $(if ($Detail) { '  --> ' + $Detail } else { '' })) }
}

# NOT $env:TEMP. CI runs this on ubuntu-24.04 under pwsh, where that variable does
# not exist, and Join-Path threw "Cannot bind argument to parameter 'Path'
# because it is null" on the very first CI run. The watchers themselves are
# Windows operator helpers, but their tests must run wherever CI runs.
$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ('watchstate_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

try {

Write-Output ''
Write-Output 'a fresh watcher treats everything as new, and primes'
$s = New-WatchState
Assert-True 'no watermark means new' (Test-RowIsNew -State $s -RowId 'a' -RowTs '2026-01-01T00:00:00Z')
Assert-True 'state starts empty' ([string]$s.lastTs -eq '')

Write-Output ''
Write-Output 'THE BUG: an ancient row is never new again, whatever the id set holds'
$s = New-WatchState
$times = @{}
$s = Update-WatchState -State $s -RowId 'recent' -RowTs '2026-09-08T08:00:00Z' -TailTimes $times
# The old design would have held 400 ids and, once this row aged out, called it
# fresh. Under a watermark its id is irrelevant -- the timestamp decides.
Assert-True 'a row from a week ago is not new' `
  (-not (Test-RowIsNew -State $s -RowId 'ancient-never-in-the-tail' -RowTs '2026-09-01T00:00:00Z'))
Assert-True 'a row from a year ago is not new' `
  (-not (Test-RowIsNew -State $s -RowId 'older-still' -RowTs '2025-09-01T00:00:00Z'))
Assert-True 'a newer row is new' `
  (Test-RowIsNew -State $s -RowId 'later' -RowTs '2026-09-08T09:00:00Z')

Write-Output ''
Write-Output 'the tail stays bounded no matter how many rows go past'
$s = New-WatchState
$times = @{}
for ($i = 0; $i -lt 1000; $i++) {
  $ts = (Get-Date '2026-09-08T00:00:00Z').ToUniversalTime().AddSeconds($i).ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
  $s = Update-WatchState -State $s -RowId ('row' + $i) -RowTs $ts -TailTimes $times
}
Assert-True 'tail did not grow with the board' (@($s.tailIds).Count -le 200) ('tail=' + @($s.tailIds).Count)
Assert-True 'watermark advanced to the newest row' ([string]$s.lastTs -gt '2026-09-08T00:16:00')
Assert-True 'row0, long evicted from the tail, is still not new' `
  (-not (Test-RowIsNew -State $s -RowId 'row0' -RowTs '2026-09-08T00:00:00Z'))

Write-Output ''
Write-Output 'clock skew between instances does not lose a row'
$s = New-WatchState
$times = @{}
$s = Update-WatchState -State $s -RowId 'fast-machine' -RowTs '2026-09-08T08:00:30Z' -TailTimes $times
Assert-True 'a row stamped 20s earlier by another machine is still new' `
  (Test-RowIsNew -State $s -RowId 'slow-machine' -RowTs '2026-09-08T08:00:10Z')
Assert-True 'but the same row is not new twice' `
  ((Update-WatchState -State $s -RowId 'slow-machine' -RowTs '2026-09-08T08:00:10Z' -TailTimes $times) -and
   (-not (Test-RowIsNew -State $s -RowId 'slow-machine' -RowTs '2026-09-08T08:00:10Z')))
Assert-True 'a row outside the skew window is not reconsidered' `
  (-not (Test-RowIsNew -State $s -RowId 'unseen-but-old' -RowTs '2026-09-08T07:30:00Z'))

Write-Output ''
Write-Output 'an undateable row is surfaced once, not dropped and not repeated'
$s = New-WatchState
$times = @{}
$s = Update-WatchState -State $s -RowId 'dated' -RowTs '2026-09-08T08:00:00Z' -TailTimes $times
Assert-True 'a row with a junk timestamp is new the first time' `
  (Test-RowIsNew -State $s -RowId 'junk' -RowTs 'not-a-date')
$s = Update-WatchState -State $s -RowId 'junk' -RowTs 'not-a-date' -TailTimes $times
Assert-True 'and not the second time' `
  (-not (Test-RowIsNew -State $s -RowId 'junk' -RowTs 'not-a-date'))
Assert-True 'an undateable id is kept in the tail rather than pruned' `
  (@($s.tailIds) -contains 'junk')

Write-Output ''
Write-Output 'state survives a round trip, and a truncated file does not lie'
$path = Join-Path $tmpDir 'state.json'
$s = New-WatchState
$times = @{}
$s = Update-WatchState -State $s -RowId 'x1' -RowTs '2026-09-08T08:00:00Z' -TailTimes $times
Assert-True 'save reports success' (Save-WatchState -State $s -Path $path)
$loaded = Read-WatchState -Path $path
Assert-True 'watermark round-trips' ($loaded -and $loaded.lastTs -eq $s.lastTs) ([string]$loaded.lastTs)
Assert-True 'a row before the reloaded watermark is not new' `
  (-not (Test-RowIsNew -State $loaded -RowId 'y' -RowTs '2026-09-07T00:00:00Z'))

Set-Content -LiteralPath $path -Value '{"lastTs":"2026-09-08T08:0' -Encoding UTF8
Assert-True 'a truncated file loads as absent, not as a bad watermark' `
  ($null -eq (Read-WatchState -Path $path))
Set-Content -LiteralPath $path -Value '{"tailIds":["a","b"]}' -Encoding UTF8
Assert-True 'a file with ids but no watermark is treated as absent' `
  ($null -eq (Read-WatchState -Path $path))
Set-Content -LiteralPath $path -Value '{"lastTs":"never","tailIds":[]}' -Encoding UTF8
Assert-True 'an unparseable watermark is treated as absent' `
  ($null -eq (Read-WatchState -Path $path))
Assert-True 'a missing file is absent' ($null -eq (Read-WatchState -Path (Join-Path $tmpDir 'nope.json')))

Write-Output ''
Write-Output 'the PowerShell 7 / 5.1 JSON type difference is absorbed'
# PowerShell 7's ConvertFrom-Json deserialises an ISO-8601 string into a
# [datetime]; 5.1 leaves it a string. The same state file therefore yields
# different TYPES on the laptop and on the Linux CI runner, and a naive [string]
# cast of the datetime renders in the current culture -- CI reported exactly
# "09/08/2026 08:00:00" as the failure detail. This assertion runs on 5.1 too, so
# the difference stays covered wherever the suite is executed.
$isoText = '2026-09-08T08:00:00.000Z'
$asDate = [datetime]::Parse($isoText, [System.Globalization.CultureInfo]::InvariantCulture,
  [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
Assert-True 'a string and a datetime watermark resolve identically' `
  ((ConvertTo-WatchTime $isoText) -eq (ConvertTo-WatchTime $asDate)) `
  ((ConvertTo-WatchTime $asDate).ToString('o'))
Assert-True 'and neither depends on the current culture' `
  ((ConvertTo-WatchTime $asDate).ToString('o') -eq (ConvertTo-WatchTime $isoText).ToString('o'))

$dtPath = Join-Path $tmpDir 'dt.json'
Set-Content -LiteralPath $dtPath -Value ('{"lastTs":"' + $isoText + '","tailIds":["k"]}') -Encoding UTF8
$dtLoaded = Read-WatchState -Path $dtPath
Assert-True 'a loaded watermark is always a canonical ISO string' `
  ($dtLoaded -and ($dtLoaded.lastTs -is [string]) -and $dtLoaded.lastTs.EndsWith('Z')) ([string]$dtLoaded.lastTs)
Assert-True 'and it still decides freshness correctly after the round trip' `
  ((-not (Test-RowIsNew -State $dtLoaded -RowId 'old' -RowTs '2026-09-07T00:00:00Z')) -and
   (Test-RowIsNew -State $dtLoaded -RowId 'new' -RowTs '2026-09-08T09:00:00Z'))

Write-Output ''
Write-Output 'the write is atomic'
$s = New-WatchState
$times = @{}
$s = Update-WatchState -State $s -RowId 'z' -RowTs '2026-09-08T08:00:00Z' -TailTimes $times
[void](Save-WatchState -State $s -Path $path)
Assert-True 'no .tmp file is left behind' (-not (Test-Path -LiteralPath ($path + '.tmp')))
# The point of writing beside and moving: the destination is never a partial file.
# If a previous run died mid-write, its leftover .tmp must not be mistaken for state.
Set-Content -LiteralPath ($path + '.tmp') -Value '{"lastTs":"junk-from-a-dead-run"' -Encoding UTF8
$again = Read-WatchState -Path $path
Assert-True 'a stray .tmp does not affect the real state' `
  ($again -and $again.lastTs -eq $s.lastTs)
Remove-Item -LiteralPath ($path + '.tmp') -Force -ErrorAction SilentlyContinue

} finally {
  Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ''
if ($script:Failed -eq 0) { Write-Output ('ALL PASS (' + $script:Passed + ')') }
else {
  # [string] casts, because $script:Failed is an int and PowerShell resolves
  # int + string by trying to convert the STRING to an int. On the first failing
  # CI run this threw instead of reporting -- a test harness that crashes when a
  # test fails hides the failure it exists to show.
  Write-Output ([string]$script:Failed + ' FAILURE(S) of ' + [string]($script:Passed + $script:Failed))
}
exit $(if ($script:Failed -eq 0) { 0 } else { 1 })
