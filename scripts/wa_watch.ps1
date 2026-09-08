#Requires -Version 5.1
<#
SFDC24 - watch the board for Mr. Salam's messages and surface them
claude-code-cli, 2026-09-06; amendment A 2026-09-08 (CODEX-PR34-868-REPAIR-GO-A)

WHY
  The gateway writes one board row per inbound WhatsApp message and the governor
  console writes one row per note he sends. The messages were never unreachable.
  Nothing WAKES an instance, and every stall in this project has that shape.

  MEASURED: at 2026-09-07T03:39:45Z he sent "Show me what you can do" from the
  governor console. Nothing was watching. When a watcher armed at
  2026-09-08T00:36:32Z it counted his message among "214 existing messages
  ignored" and stayed silent. He waited 22 hours.

STAGE, EMIT, COMMIT - three explicit boundaries
  Set-WatchOutbox persists and returns DATA ONLY. The caller writes the exact
  persisted lines to stdout. Only then is the cursor committed. The previous
  version had the stage function write to the success stream and also return a
  count, so the caller's assignment captured both and NOTHING reached stdout: the
  lines were staged, swallowed, and cleared by the commit.

PLANNING IS PURE
  Get-PlanEntries is deterministic and mutates nothing. It is used for the live
  plan AND to recompute a retained outbox before replay, so the two cannot drift.
  The dedupe marker advances only at commit, and only to the last row actually
  staged; advancing it while planning left the marker describing a row that was
  never staged, and the next scan dropped a real message against it.
#>
param(
  [ValidateRange(5, 3600)]
  [int]$PollSeconds = 60,
  [ValidateRange(0, 100)]
  [int]$Backfill = 0,
  [ValidateRange(0, 100)]
  [int]$CatchUpMax = 10,
  [string]$StateFile,
  [switch]$Reset,
  [switch]$Once
)

$ErrorActionPreference = 'Continue'
$WarningPreference     = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

$bus = Join-Path $PSScriptRoot 'bus.ps1'
if (-not (Test-Path -LiteralPath $bus)) { throw "bus.ps1 not found beside this script" }
. (Join-Path $PSScriptRoot 'watch_state.ps1')

if (-not $StateFile) { $StateFile = Get-DefaultStatePath -Leaf 'wa_watch.state.json' }
$StateFile = Resolve-StatePath $StateFile

$lock = Enter-WatchLock -Path $StateFile
if (-not $lock.ok) { throw ("WA watch CANNOT START - " + $lock.reason) }

$ResetRequested = [bool]$Reset
$ResetDone = $false

$loaded = Read-WatchState -Path $StateFile
if ($loaded.disposition -eq 'unusable' -and -not $ResetRequested) {
  Exit-WatchLock $lock
  throw ("WA watch CANNOT START - " + $loaded.reason +
         ". A cursor existed and cannot be read, so priming would skip whatever arrived since it was written. Nothing has been reported and nothing has been overwritten. Re-prime deliberately with -Reset.")
}
$state = $loaded.state
$warm = ($loaded.disposition -eq 'ok')

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("wa_watch_" + [guid]::NewGuid().ToString('N') + '.json')

function Read-Board {
  try {
    & $bus -Action read -Title "Blackboard - Alpha DB" -OutFile $tmp 2>$null 3>$null 4>$null 5>$null 6>$null | Out-Null
    return (Get-Content -LiteralPath $tmp -Raw -Encoding UTF8 | ConvertFrom-Json)
  } catch { return $null }
}

function Test-RowQualifies {
  # The source tag separates his words from ours -- NOT the tag= field inside the
  # payload, which is free text any writer can set to GOVERNOR and which three
  # vm-chrome rows on this board already do.
  param($Row)
  $tag = [string]$Row[2]
  $pay = [string]$Row[5]
  if ($tag -eq 'whatsapp') { return $true }
  return (($pay -like 'GOV|kind=feed*') -and ($tag -eq 'governor-page'))
}

function Format-Line {
  param($Row, [string]$Prefix)
  $t = ([string]$Row[5]) -replace '\s+', ' '
  if (([string]$Row[5]) -like 'GOV|kind=feed*') {
    $t = $t -replace '^GOV\|kind=feed\|', ''
    $t = $t -replace '^(project=[^|]*\|)?tag=[^|]*\|text=', ''
    $t = $t -replace '\|project=[^|]*$', ''
    return ($Prefix + "GOVERNOR PAGE - MR SALAM [" + $Row[1] + "] " + $t.Trim())
  }
  return ($Prefix + "WA FROM MR SALAM [" + $Row[1] + "] " + $t.Trim())
}

function Get-ModePrefix { param([string]$Mode)
  if ($Mode -eq 'cold') { return '(recent) ' }
  if ($Mode -eq 'warm') { return '(missed while offline) ' }
  return ''
}

function Get-PlanEntries {
  <#
    PURE and DETERMINISTIC. Same board and same evidence, same ordered plan.
    Used for the live plan and to recompute a retained outbox before replay, so
    the two can never drift apart.
  #>
  param($Rows, [int]$From, [int]$To, [string]$Mode, [int]$Limit, [string]$EmitKey, [string]$EmitTs)
  $elig = @()
  for ($i = $From; $i -le $To; $i++) {
    if ($i -ge 1 -and $i -lt @($Rows).Count -and (Test-RowQualifies -Row $Rows[$i])) { $elig += ,@{ i = $i; row = $Rows[$i] } }
  }
  if ($Mode -eq 'cold' -or $Mode -eq 'warm') {
    if ($Limit -le 0) { $elig = @() }
    elseif ($elig.Count -gt $Limit) { $elig = $elig[($elig.Count - $Limit)..($elig.Count - 1)] }
  }
  $prefix = Get-ModePrefix -Mode $Mode
  $k = $EmitKey; $t = $EmitTs
  $plan = @()
  foreach ($e in $elig) {
    $r = $e.row
    $key = ([string]$r[2]) + '::' + (([string]$r[5]) -replace '\s+', ' ')
    if (Test-KeyIsDuplicate -PrevKey $k -PrevTs $t -Key $key -RowTs ([string]$r[1])) { continue }
    $k = $key; $t = [string]$r[1]
    $plan += ,@{ line = (Format-Line -Row $r -Prefix $prefix); rowIndex = [int]$e.i; rowId = [string]$r[0]; key = $key; ts = [string]$r[1] }
  }
  return $plan
}

$recompute = {
  param($from, $to, $mode, $limit, $emitKey, $emitTs)
  $p = Get-PlanEntries -Rows $script:currentRows -From $from -To $to -Mode $mode -Limit $limit -EmitKey $emitKey -EmitTs $emitTs
  return @(@($p) | ForEach-Object { $_.rowId })
}

try {
  while ($true) {
    $board = Read-Board
    if (-not $board -or -not $board.rows) {
      # FAIL LOUD. -Once once exited 0 with no output and no state, so a failed
      # poll was indistinguishable from a clean one.
      if ($Once) { throw "WA watch COULD NOT READ THE BOARD - no rows returned. Nothing was reported and the cursor did not move." }
      Start-Sleep -Seconds $PollSeconds
      continue
    }

    $rows = $board.rows
    $script:currentRows = $rows
    $boardId = [string]$board.fileId
    $lastData = @($rows).Count - 1

    if (-not $boardId) {
      throw "WA watch CANNOT PROCEED - the board read carried no identity, so there is no way to know this is the same board the cursor belongs to. Nothing was reported."
    }

    if ($ResetRequested -and -not $ResetDone) {
      # A TRANSITION. Prior bytes are kept until the new cursor lands, and the
      # restore outcome is computed as DATA. The previous version used thrown
      # exceptions as branch control inside the recovery block, and a .NET IO
      # failure is wrapped in RuntimeException -- so the rethrow branch swallowed
      # it and the UNKNOWN receipt was unreachable.
      $prior = $null
      try { if (Test-Path -LiteralPath $StateFile) { $prior = [System.IO.File]::ReadAllBytes($StateFile) } } catch { $prior = $null }
      $fresh = New-WatchState
      $fresh.boardId = $boardId
      $fresh.resetAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
      $fresh = Set-CommittedCursor -State $fresh -Rows $rows -Index $lastData
      $rs = Save-WatchState -State $fresh -Path $StateFile
      if (-not $rs.ok) {
        $verdict = 'there was no previous cursor to lose'
        if ($null -ne $prior) {
          $verdict = 'the previous cursor could NOT be verified after restore: state UNKNOWN'
          try {
            [System.IO.File]::WriteAllBytes($StateFile, $prior)
            $check = [System.IO.File]::ReadAllBytes($StateFile)
            $same = ($check.Length -eq $prior.Length)
            if ($same) { for ($k = 0; $k -lt $check.Length; $k++) { if ($check[$k] -ne $prior[$k]) { $same = $false; break } } }
            if ($same) { $verdict = 'the previous cursor was restored and verified byte for byte' }
          } catch { $verdict = 'restoring the previous cursor also failed: state UNKNOWN' }
        }
        throw ("WA watch RESET FAILED - " + $rs.reason + ". " + $verdict + ".")
      }
      $state = $fresh
      $ResetDone = $true
      $warm = $false
      Write-Output ("WA watch RESET at " + $fresh.resetAt + " - cursor re-primed at row " + $lastData +
                    " for board " + $boardId + ". Anything that arrived before this point will NOT be replayed.")
      if ($Once) { break }
      Start-Sleep -Seconds $PollSeconds
      continue
    }

    # ---- validate a retained outbox against a RECOMPUTED plan, before output --
    $pv = Test-PendingMatchesPlan -State $state -Rows $rows -BoardId $boardId -Recompute $recompute
    if (-not $pv.ok) {
      throw ("WA watch RETAINED OUTBOX IS STALE - " + $pv.reason +
             ". Nothing was replayed and nothing was overwritten, so those lines are still on disk. Decide what to do about them, then -Reset.")
    }
    $cont = Test-BoardContinuity -State $state -Rows $rows -BoardId $boardId
    if (-not $cont.ok) {
      throw ("WA watch CANNOT RESUME - " + $cont.reason +
             ". Nothing was reported and the cursor was not moved, so nothing has been skipped yet. Re-prime deliberately with -Reset.")
    }

    if (Test-HasOutbox -State $state) {
      Write-Output ("WA watch POSSIBLE REPLAY - the previous run persisted " + @($state.pendingLines).Count +
                    " line(s) and may have exited before showing them. Repeating them now; anything you have already seen is a duplicate, not a new message.")
      foreach ($line in @($state.pendingLines)) { Write-Output $line }
      # The marker advances with the chunk it belongs to.
      $lastIdx = @($state.pendingRowIds).Count - 1
      $lastId = $(if ($lastIdx -ge 0) { [string]@($state.pendingRowIds)[$lastIdx] } else { '' })
      $mk = $state.lastEmitKey; $mt = $state.lastEmitTs
      for ($i = 1; $i -le $lastData; $i++) {
        if (([string]$rows[$i][0]) -eq $lastId) {
          $mk = ([string]$rows[$i][2]) + '::' + (([string]$rows[$i][5]) -replace '\s+', ' ')
          $mt = [string]$rows[$i][1]
          break
        }
      }
      $state = Complete-WatchOutbox -State $state -EmitKey $mk -EmitTs $mt
      $rc = Save-WatchState -State $state -Path $StateFile
      if (-not $rc.ok) {
        throw ("WA watch COULD NOT COMMIT AFTER REPLAY - " + $rc.reason +
               ". The outbox is retained, so the next start replays the same lines rather than skipping them.")
      }
      $cont = Test-BoardContinuity -State $state -Rows $rows -BoardId $boardId
      if (-not $cont.ok) { throw ("WA watch CANNOT RESUME AFTER REPLAY - " + $cont.reason + ".") }
    }

    # ---- plan ---------------------------------------------------------------
    $mode = 'steady'; $limit = 0; $from = $cont.startIndex
    if ($cont.prime) {
      if (-not $state) { $state = New-WatchState; $state.boardId = $boardId }
      $mode = 'cold'; $limit = $Backfill; $from = 1
    } elseif ($warm) {
      $mode = 'warm'; $limit = $CatchUpMax
    }
    $plan = @(Get-PlanEntries -Rows $rows -From $from -To $lastData -Mode $mode -Limit $limit `
                -EmitKey $state.lastEmitKey -EmitTs $state.lastEmitTs)

    if ($warm) {
      $warm = $false
      $eligible = 0
      for ($i = $from; $i -le $lastData; $i++) { if (Test-RowQualifies -Row $rows[$i]) { $eligible++ } }
      if ($eligible -gt 0) {
        if ($CatchUpMax -le 0) {
          Write-Output ("WA watch resumed - " + $eligible + " message(s) arrived while nothing was listening; -CatchUpMax 0 so none are replayed")
        } elseif ($eligible -gt $CatchUpMax) {
          Write-Output ("WA watch resumed - " + $eligible + " messages arrived while nothing was listening, showing the newest " + $CatchUpMax)
        } else {
          Write-Output ("WA watch resumed - " + $eligible + " message(s) arrived while nothing was listening")
        }
      } else {
        Write-Output ("WA watch resumed at " + (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') +
                      " - nothing missed, polling every " + $PollSeconds + "s")
      }
    }

    # ---- STAGE (data only) --------------------------------------------------
    $r = Set-WatchOutbox -State $state -Rows $rows -Plan $plan -FallbackIndex $lastData `
           -Mode $mode -Limit $limit -From $from -PlanEmitKey $state.lastEmitKey -PlanEmitTs $state.lastEmitTs
    $state = $r.state
    $staged = @($r.staged)

    if ($staged.Count -eq 0) {
      $state = Set-CommittedCursor -State $state -Rows $rows -Index $lastData
      $sv = Save-WatchState -State $state -Path $StateFile
      if (-not $sv.ok) { throw ("WA watch COULD NOT COMMIT - " + $sv.reason + ". Nothing was reported.") }
    } else {
      $ps = Save-WatchState -State $state -Path $StateFile
      if (-not $ps.ok) {
        throw ("WA watch COULD NOT STAGE ITS OUTBOX - " + $ps.reason +
               ". Nothing was reported and the cursor did not move, so the next start re-reads exactly this.")
      }
      # ---- EMIT, from the caller, after the durable save --------------------
      foreach ($line in @($r.lines)) { Write-Output $line }
      # ---- COMMIT, advancing the marker only to the last STAGED row ---------
      $last = $staged[$staged.Count - 1]
      $state = Complete-WatchOutbox -State $state -EmitKey ([string]$last.key) -EmitTs ([string]$last.ts)
      $cs = Save-WatchState -State $state -Path $StateFile
      if (-not $cs.ok) {
        throw ("WA watch COULD NOT COMMIT - " + $cs.reason +
               ". The outbox is retained, so the next start replays those lines rather than skipping them.")
      }
      if ($r.remaining -gt 0) {
        Write-Output ("WA watch - " + $r.remaining + " more line(s) held for the next poll; the cursor advanced only through what was made durable.")
      }
    }

    if ($cont.prime) {
      Write-Output ("WA watch armed COLD at " + (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') +
                    " - cursor set at row " + $lastData + ", polling every " + $PollSeconds + "s")
    }

    if ($Once) { break }
    Start-Sleep -Seconds $PollSeconds
  }
} finally {
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
  Exit-WatchLock $lock
}
