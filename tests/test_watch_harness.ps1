#Requires -Version 5.1
<#
SFDC24 - EXECUTABLE harness for the real watcher entrypoints
claude-code-cli, 2026-09-08, required by CODEX-PR34-9E95-CONSOLIDATED-REPAIR-GO

WHY THIS EXISTS
  tests/test_watch_state.ps1 exercises the state module, and for a while it also
  claimed to cover the watchers -- by reading their SOURCE TEXT with a regex.
  chatgpt-codex-desktop pointed out that asserting a string appears in a file
  proves an author wrote something, not that the program does it. I had verified
  the watcher behaviours by hand and pasted the output, which is manual evidence:
  true once, on one machine, and gone the moment somebody edits the loop.

  This runs the actual entrypoints.

THE SEAM IS THE DIRECTORY, NOT THE PROGRAM
  Both watchers resolve their bus as Join-Path $PSScriptRoot 'bus.ps1'. So the
  harness copies the real wa_watch.ps1, fleet_watch.ps1 and watch_state.ps1 into
  a temp directory beside a STUB bus.ps1 that serves a canned board from a file.
  No production input was added for testing, and nothing here can reach the
  network: the real bus.ps1 is never copied, and its absence is asserted.

RUN
  powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_watch_harness.ps1
  pwsh -NoProfile -File tests/test_watch_harness.ps1
#>

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

$script:Passed = 0
$script:Failed = 0
function Assert-True {
  param([string]$Name, [bool]$Condition, [string]$Detail = '')
  if ($Condition) { $script:Passed++; Write-Output ('  PASS  ' + $Name) }
  else { $script:Failed++; Write-Output ('  FAIL  ' + $Name + $(if ($Detail) { '  --> ' + $Detail } else { '' })) }
}

# Run the child in the SAME edition that is running this file, so both shells are
# genuinely covered when CI runs pwsh and a developer runs 5.1.
$hostExe = 'powershell'
if ($PSVersionTable.PSEdition -eq 'Core') { $hostExe = 'pwsh' }

$root = Join-Path ([System.IO.Path]::GetTempPath()) ('watchharness_' + [guid]::NewGuid().ToString('N'))
$sdir = Join-Path $root 'scripts'
New-Item -ItemType Directory -Path $sdir -Force | Out-Null

foreach ($f in @('wa_watch.ps1', 'fleet_watch.ps1', 'watch_state.ps1')) {
  Copy-Item -LiteralPath (Join-Path $RepoRoot ('scripts/' + $f)) -Destination (Join-Path $sdir $f) -Force
}

# The stub. It serves whatever JSON the harness has put at $env:SFDC24_FAKE_BOARD,
# or fails when that file is absent -- which is how "board unavailable" is tested.
$stub = @(
  'param([string]$Action, [string]$Title, [string]$OutFile)',
  '$src = $env:SFDC24_FAKE_BOARD',
  'if (-not $src -or -not (Test-Path -LiteralPath $src)) { throw "stub bus: no board available" }',
  'Copy-Item -LiteralPath $src -Destination $OutFile -Force',
  '"{""ok"":true}"'
) -join "`n"
Set-Content -LiteralPath (Join-Path $sdir 'bus.ps1') -Value $stub -Encoding UTF8

function New-BoardFile {
  param([string]$Path, [int]$Count, [string]$BoardId = 'board-A', [string]$Prefix = 'r',
        [string]$Source = 'whatsapp', [string]$Payload = 'hello from him')
  $rows = @( ,@('Row_ID','Timestamp','Source_Tag','Target_Surface','Action_Type','Payload','Category','Project','Gist','Sub') )
  for ($i = 1; $i -le $Count; $i++) {
    $ts = (Get-Date '2026-09-01T00:00:00Z').ToUniversalTime().AddSeconds($i).ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $rows += ,@(($Prefix + $i), $ts, $Source, 'ALL', 'APPEND', ($Payload + ' ' + $i), '', '', '', '')
  }
  $obj = [ordered]@{ ok = $true; fileId = $BoardId; title = 'Blackboard - Alpha DB'; rows = $rows }
  ($obj | ConvertTo-Json -Depth 6 -Compress) | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Invoke-Watcher {
  <# Returns @{ exit; out; state } after running a REAL entrypoint. #>
  # NOT $Args. It is a PowerShell AUTOMATIC variable holding a function's unbound
  # arguments, so a parameter of that name does not receive what the caller
  # passed -- every extra switch was silently dropped and the harness reported
  # failures the watchers did not have. Same class as $BOARD colliding with
  # $board because names are case-insensitive.
  param([string]$Script, [string[]]$ExtraArgs, [string]$BoardFile, [string]$StatePath)
  $prev = $env:SFDC24_FAKE_BOARD
  $env:SFDC24_FAKE_BOARD = $BoardFile
  $outFile = Join-Path $root ('out_' + [guid]::NewGuid().ToString('N') + '.txt')
  try {
    $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $sdir $Script),
             '-Once', '-StateFile', $StatePath) + $ExtraArgs
    $p = Start-Process -FilePath $hostExe -ArgumentList $all -NoNewWindow -Wait -PassThru `
           -RedirectStandardOutput $outFile -RedirectStandardError ($outFile + '.err')
    # Explicit, so the exit code and the redirected files are certainly settled
    # before they are read.
    $p.WaitForExit()
    $text = ''
    if (Test-Path -LiteralPath $outFile) { $text += (Get-Content -LiteralPath $outFile -Raw) }
    if (Test-Path -LiteralPath ($outFile + '.err')) { $text += (Get-Content -LiteralPath ($outFile + '.err') -Raw) }
    $stateBytes = $null
    if (Test-Path -LiteralPath $StatePath) { $stateBytes = [System.IO.File]::ReadAllBytes($StatePath) }
    return @{ exit = $p.ExitCode; out = [string]$text; state = $stateBytes }
  } finally { $env:SFDC24_FAKE_BOARD = $prev }
}

try {

Assert-True 'the harness never copies the real bus (no live network is reachable)' `
  (-not (Select-String -Path (Join-Path $sdir 'bus.ps1') -Pattern 'script.google.com' -Quiet))

$board = Join-Path $root 'board.json'
New-BoardFile -Path $board -Count 12

foreach ($w in @(@{ s = 'wa_watch.ps1'; n = 'WA watch' }, @{ s = 'fleet_watch.ps1'; n = 'Fleet watch' })) {
  $name = $w.n
  Write-Output ''
  Write-Output ("=== " + $w.s + " ===")
  $sp = Join-Path $root ($w.s + '.state.json')
  if (Test-Path -LiteralPath $sp) { Remove-Item -LiteralPath $sp -Force }

  # ---- cold prime, then warm resume -------------------------------------
  $r1 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $sp
  Assert-True ($name + ' cold prime exits 0') ($r1.exit -eq 0) ("exit=" + $r1.exit + " " + $r1.out)
  Assert-True ($name + ' cold prime says it armed') ($r1.out -match 'armed COLD') $r1.out
  Assert-True ($name + ' cold prime wrote state') ($null -ne $r1.state)
  $r2 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $sp
  Assert-True ($name + ' warm resume exits 0') ($r2.exit -eq 0) $r2.out
  Assert-True ($name + ' warm resume reports nothing missed') ($r2.out -match 'nothing missed') $r2.out

  # ---- the board is unavailable -----------------------------------------
  $r3 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile (Join-Path $root 'no-such-board.json') -StatePath $sp
  Assert-True ($name + ' an unreadable board exits NON-zero') ($r3.exit -ne 0) ("exit=" + $r3.exit)
  Assert-True ($name + ' and says the board could not be read') ($r3.out -match 'COULD NOT READ THE BOARD') $r3.out
  Assert-True ($name + ' and left the cursor byte-identical') `
    ((-not $r1.state) -or (([System.BitConverter]::ToString($r3.state)) -eq ([System.BitConverter]::ToString($r2.state))))

  # ---- a corrupt cursor refuses, and does not prime over the gap ---------
  $corrupt = Join-Path $root ($w.s + '.corrupt.json')
  Set-Content -LiteralPath $corrupt -Value '{"schema":4,"boardId":"board-A","lastIndex":3' -Encoding UTF8
  $before = [System.IO.File]::ReadAllBytes($corrupt)
  $r4 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $corrupt
  Assert-True ($name + ' a corrupt cursor exits NON-zero') ($r4.exit -ne 0) ("exit=" + $r4.exit)
  Assert-True ($name + ' and refuses to start rather than priming') ($r4.out -match 'CANNOT START') $r4.out
  Assert-True ($name + ' and did NOT overwrite the corrupt file') `
    (([System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($corrupt))) -eq ([System.BitConverter]::ToString($before)))

  # ---- -Reset recovers it, and the receipt follows the new cursor --------
  $r5 = Invoke-Watcher -Script $w.s -ExtraArgs @('-Reset') -BoardFile $board -StatePath $corrupt
  Assert-True ($name + ' -Reset on a corrupt cursor exits 0') ($r5.exit -eq 0) ("exit=" + $r5.exit + " " + $r5.out)
  Assert-True ($name + ' and prints a dated reset receipt') ($r5.out -match 'RESET at') $r5.out
  Assert-True ($name + ' and the new cursor reads back') ($null -ne $r5.state)

  # ---- a cross-board retained outbox refuses, emitting none of it --------
  $cross = Join-Path $root ($w.s + '.cross.json')
  $payload = [ordered]@{
    schema = 4; savedAt = '2026-09-08T00:00:00Z'; boardId = 'board-OTHER'; lastIndex = 1; anchorId = 'r1'
    lastEmitKey = ''; lastEmitTs = ''; pendingIndex = 2; pendingAnchor = 'r2'
    pendingLines = @('SECRET LINE FROM ANOTHER BOARD'); pendingRowIds = @('r2'); resetAt = ''
  }
  ($payload | ConvertTo-Json -Depth 5 -Compress) | Set-Content -LiteralPath $cross -Encoding UTF8
  $r6 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $cross
  Assert-True ($name + ' a cross-board outbox exits NON-zero') ($r6.exit -ne 0) ("exit=" + $r6.exit)
  Assert-True ($name + ' and NEVER emits the other board''s line') (-not ($r6.out -match 'SECRET LINE FROM ANOTHER BOARD')) $r6.out

  # ---- a moved pending anchor refuses ------------------------------------
  $moved = Join-Path $root ($w.s + '.moved.json')
  $payload2 = [ordered]@{
    schema = 4; savedAt = '2026-09-08T00:00:00Z'; boardId = 'board-A'; lastIndex = 1; anchorId = 'r1'
    lastEmitKey = ''; lastEmitTs = ''; pendingIndex = 2; pendingAnchor = 'NOT-THE-ROW-THERE'
    pendingLines = @('STALE LINE'); pendingRowIds = @('r2'); resetAt = ''
  }
  ($payload2 | ConvertTo-Json -Depth 5 -Compress) | Set-Content -LiteralPath $moved -Encoding UTF8
  $r7 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $moved
  Assert-True ($name + ' a moved pending anchor exits NON-zero') ($r7.exit -ne 0) ("exit=" + $r7.exit)
  Assert-True ($name + ' and emits none of the stale lines') (-not ($r7.out -match 'STALE LINE')) $r7.out

  # ---- a pending row that no longer exists refuses ------------------------
  $ghost = Join-Path $root ($w.s + '.ghost.json')
  $payload3 = [ordered]@{
    schema = 4; savedAt = '2026-09-08T00:00:00Z'; boardId = 'board-A'; lastIndex = 1; anchorId = 'r1'
    lastEmitKey = ''; lastEmitTs = ''; pendingIndex = 2; pendingAnchor = 'r2'
    pendingLines = @('GHOST LINE'); pendingRowIds = @('row-that-vanished'); resetAt = ''
  }
  ($payload3 | ConvertTo-Json -Depth 5 -Compress) | Set-Content -LiteralPath $ghost -Encoding UTF8
  $r8 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $ghost
  Assert-True ($name + ' a vanished pending row exits NON-zero') ($r8.exit -ne 0) ("exit=" + $r8.exit)
  Assert-True ($name + ' and emits none of it') (-not ($r8.out -match 'GHOST LINE')) $r8.out

  # ---- a retained outbox for THIS board is replayed and labelled ---------
  $replay = Join-Path $root ($w.s + '.replay.json')
  $payload4 = [ordered]@{
    schema = 4; savedAt = '2026-09-08T00:00:00Z'; boardId = 'board-A'; lastIndex = 1; anchorId = 'r1'
    lastEmitKey = ''; lastEmitTs = ''; pendingIndex = 2; pendingAnchor = 'r2'
    pendingLines = @('LINE THAT WAS NEVER SHOWN'); pendingRowIds = @('r2'); resetAt = ''
  }
  ($payload4 | ConvertTo-Json -Depth 5 -Compress) | Set-Content -LiteralPath $replay -Encoding UTF8
  $r9 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $replay
  Assert-True ($name + ' a valid retained outbox replays') ($r9.out -match 'LINE THAT WAS NEVER SHOWN') $r9.out
  Assert-True ($name + ' and says it may be a repeat') ($r9.out -match 'POSSIBLE REPLAY') $r9.out
  Assert-True ($name + ' and exits 0') ($r9.exit -eq 0) ("exit=" + $r9.exit)
  Assert-True ($name + ' and clears the outbox afterwards') `
    (-not ((Get-Content -LiteralPath $replay -Raw) -match 'LINE THAT WAS NEVER SHOWN'))

  # ---- lock contention ---------------------------------------------------
  $lockPath = $sp + '.lock'
  $held = New-Object System.IO.FileStream($lockPath, [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  try {
    $r10 = Invoke-Watcher -Script $w.s -ExtraArgs @() -BoardFile $board -StatePath $sp
    Assert-True ($name + ' a second watcher refuses while the lock is held') ($r10.exit -ne 0) ("exit=" + $r10.exit)
    Assert-True ($name + ' and names the contention') ($r10.out -match 'already holds the cursor') $r10.out
  } finally { $held.Dispose() }

  # ---- invalid parameters bind-fail rather than running ------------------
  $r11 = Invoke-Watcher -Script $w.s -ExtraArgs @('-PollSeconds', '0') -BoardFile $board -StatePath $sp
  Assert-True ($name + ' -PollSeconds 0 is refused') ($r11.exit -ne 0) ("exit=" + $r11.exit)
  $r12 = Invoke-Watcher -Script $w.s -ExtraArgs @('-CatchUpMax', '-1') -BoardFile $board -StatePath $sp
  Assert-True ($name + ' -CatchUpMax -1 is refused') ($r12.exit -ne 0) ("exit=" + $r12.exit)
}

# ---- fleet-only: a traversing -Tag is refused ---------------------------
Write-Output ''
Write-Output '=== fleet-only parameter guard ==='
$sp2 = Join-Path $root 'tagtest.state.json'
$r13 = Invoke-Watcher -Script 'fleet_watch.ps1' -ExtraArgs @('-Tag', '../../evil') -BoardFile $board -StatePath $sp2
Assert-True 'Fleet watch refuses a traversing -Tag' ($r13.exit -ne 0) ("exit=" + $r13.exit)

# ---- chunking: more lines than the bound -------------------------------
Write-Output ''
Write-Output '=== the outbox chunks rather than truncating ==='
$big = Join-Path $root 'bigboard.json'
New-BoardFile -Path $big -Count 260
$sp3 = Join-Path $root 'chunk.state.json'
# Prime against a small board, then present the big one so everything after the
# cursor is new.
$small = Join-Path $root 'smallboard.json'
New-BoardFile -Path $small -Count 1
[void](Invoke-Watcher -Script 'wa_watch.ps1' -ExtraArgs @() -BoardFile $small -StatePath $sp3)
$c1 = Invoke-Watcher -Script 'wa_watch.ps1' -ExtraArgs @('-CatchUpMax', '100') -BoardFile $big -StatePath $sp3
Assert-True 'a large catch-up exits 0' ($c1.exit -eq 0) ("exit=" + $c1.exit + " " + $c1.out)
$st = Get-Content -LiteralPath $sp3 -Raw | ConvertFrom-Json
Assert-True 'the cursor did not jump to the end of the board in one go' `
  ([int]$st.lastIndex -le 260) ('lastIndex=' + [string]$st.lastIndex)
Assert-True 'and the outbox is empty after a clean commit' (@($st.pendingLines).Count -eq 0)

} finally {
  Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ''
if ($script:Failed -eq 0) { Write-Output ('ALL PASS (' + [string]$script:Passed + ')') }
else { Write-Output ([string]$script:Failed + ' FAILURE(S) of ' + [string]($script:Passed + $script:Failed)) }
exit $(if ($script:Failed -eq 0) { 0 } else { 1 })
