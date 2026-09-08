#Requires -Version 5.1
<#
SFDC24 - shared watermark state for the board watchers
claude-code-cli, 2026-09-08

WHY THIS EXISTS
  wa_watch.ps1 and fleet_watch.ps1 both kept "the newest 400 row ids I have
  shown" and then rescanned the ENTIRE append-only board on every poll. codex
  found the consequence while reviewing PR34, and it is a time bomb rather than
  a bug you would ever see in testing:

    once more than 400 qualifying rows exist, the oldest ids fall out of the
    saved set, the next restart rescans the whole board, finds those ancient
    rows absent from the set, and replays them as "(missed while offline)".

  The watcher would confidently present Mr. Salam with messages from weeks ago
  as though they had just arrived and nobody had answered them. A watcher that
  cries wolf is worse than no watcher, because the real message is then one line
  in a wall of false ones.

  So the state is a WATERMARK, not a memory of everything seen. It is O(1) in
  the size of the board and cannot be evicted.

WHY NOT A ROW POSITION
  Position would be simpler and is wrong here. The board is read through more
  than one path and at least one of them truncates (the Drive connector stops
  around row 200), so "row 412" does not mean the same thing to every reader.
  A timestamp plus an id survives that.

WHY A SKEW WINDOW
  Timestamps are stamped by whichever instance wrote the row, across machines
  whose clocks are not synchronised. Rows can therefore arrive slightly out of
  timestamp order. Treating "ts > lastTs" as the only test would silently drop a
  row written by a machine running a few seconds behind. So rows within
  SKEW_SECONDS of the watermark are still examined, and de-duplicated by id
  against a bounded tail set. Outside that window, the watermark alone decides.
#>

# Ten minutes. Generous for clock skew between a laptop, a VM and Apps Script,
# and small enough that the tail set stays short.
$script:WATCH_SKEW_SECONDS = 600
$script:WATCH_TAIL_MAX = 200

function ConvertTo-WatchTime {
  # A row timestamp that cannot be parsed must not be silently treated as epoch
  # zero -- that would make it permanently "old" and permanently skipped. Return
  # $null and let the caller decide.
  #
  # NOT [string]$Value on the parameter. PowerShell 7's ConvertFrom-Json eagerly
  # deserialises an ISO-8601 string into a [datetime], while Windows PowerShell
  # 5.1 leaves it a string -- so the same state file yields different types on
  # the laptop and on the Linux CI runner. Casting a [datetime] to string then
  # renders it in the CURRENT CULTURE ("09/08/2026 08:00:00"), which no longer
  # round-trips and compares wrongly. bus.ps1 already carries a comment about
  # this exact trap and I walked into it anyway; CI caught it.
  param($Value)
  if ($Value -is [datetime]) { return ([datetime]$Value).ToUniversalTime() }
  $Value = [string]$Value
  if (-not $Value) { return $null }
  $parsed = [datetime]::MinValue
  $styles = [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor `
            [System.Globalization.DateTimeStyles]::AssumeUniversal
  if ([datetime]::TryParse($Value, [System.Globalization.CultureInfo]::InvariantCulture, $styles, [ref]$parsed)) {
    return $parsed.ToUniversalTime()
  }
  return $null
}

function New-WatchState {
  return [pscustomobject]@{
    lastTs  = ''      # ISO-8601 UTC of the newest row surfaced
    tailIds = @()     # ids at or near lastTs, for tie-breaking within the skew window
  }
}

function Read-WatchState {
  param([string]$Path)
  if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
  try {
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return $null }
    $o = $raw | ConvertFrom-Json
    $ids = @()
    foreach ($k in @($o.tailIds)) { if ($k) { $ids += [string]$k } }
    # Normalise to a canonical ISO-8601 UTC STRING here, whatever the JSON
    # deserialiser handed back, so every consumer downstream sees one type on
    # every platform.
    $when = ConvertTo-WatchTime $o.lastTs
    # A state file with no watermark proves nothing about what has been shown.
    # Treat it as absent rather than as "everything is new".
    if (-not $when) { return $null }
    return [pscustomobject]@{ lastTs = $when.ToString('yyyy-MM-ddTHH:mm:ss.fffZ'); tailIds = $ids }
  } catch {
    # Corrupt state used to become a silent cold start, which looks identical to
    # a first run and quietly loses the catch-up. Still fail closed to cold --
    # replaying the whole board would be worse -- but the caller is told.
    return $null
  }
}

function Save-WatchState {
  param([Parameter(Mandatory = $true)]$State, [Parameter(Mandatory = $true)][string]$Path)
  try {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
      New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $ids = @($State.tailIds)
    if ($ids.Count -gt $script:WATCH_TAIL_MAX) {
      $ids = $ids[($ids.Count - $script:WATCH_TAIL_MAX)..($ids.Count - 1)]
    }
    $obj = [ordered]@{
      savedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
      lastTs  = [string]$State.lastTs
      tailIds = $ids
    }
    # ATOMIC. Set-Content writes in place, so a watcher killed mid-write left a
    # truncated file that parsed as corrupt and silently became a cold start --
    # losing exactly the catch-up this file exists to provide. Write beside it,
    # then replace in one operation.
    $tmp = $Path + '.tmp'
    ($obj | ConvertTo-Json -Depth 4 -Compress) | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $Path -Force
    return $true
  } catch {
    return $false   # a watcher must never die because a cache file could not be written
  }
}

function Test-RowIsNew {
  <#
    Is this row something the watcher has not already surfaced?
      - no watermark yet            -> everything is "new" (the caller primes)
      - ts clearly after            -> new
      - ts inside the skew window   -> new only if the id is not in the tail
      - ts clearly before           -> not new, regardless of the id set
    An unparseable timestamp is treated as new exactly once, by id, rather than
    being dropped: a row we cannot date is still a row somebody wrote.
  #>
  param([Parameter(Mandatory = $true)]$State, [string]$RowId, [string]$RowTs)
  if (-not $State -or -not $State.lastTs) { return $true }
  $mark = ConvertTo-WatchTime $State.lastTs
  $when = ConvertTo-WatchTime $RowTs
  if (-not $when) { return (@($State.tailIds) -notcontains $RowId) }
  if ($when -gt $mark) { return $true }
  if (($mark - $when).TotalSeconds -le $script:WATCH_SKEW_SECONDS) {
    return (@($State.tailIds) -notcontains $RowId)
  }
  return $false
}

function Update-WatchState {
  <#
    Advance the watermark to cover a row that has now been surfaced. The tail
    keeps ids within the skew window of the newest timestamp and drops the rest,
    so it stays short no matter how large the board grows.
  #>
  param([Parameter(Mandatory = $true)]$State, [string]$RowId, [string]$RowTs,
        [hashtable]$TailTimes)
  $when = ConvertTo-WatchTime $RowTs
  $mark = ConvertTo-WatchTime $State.lastTs
  if ($when -and (-not $mark -or $when -gt $mark)) {
    $State.lastTs = $when.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $mark = $when
  }
  $ids = @($State.tailIds)
  if ($RowId -and ($ids -notcontains $RowId)) { $ids += $RowId }
  if ($null -ne $TailTimes) {
    if ($RowId -and $when) { $TailTimes[$RowId] = $when }
    if ($mark) {
      $keep = @()
      foreach ($id in $ids) {
        $t = $TailTimes[$id]
        # Keep ids we cannot date: dropping one would re-surface its row.
        if ($null -eq $t -or ($mark - $t).TotalSeconds -le $script:WATCH_SKEW_SECONDS) { $keep += $id }
        else { $TailTimes.Remove($id) }
      }
      $ids = $keep
    }
  }
  if ($ids.Count -gt $script:WATCH_TAIL_MAX) { $ids = $ids[($ids.Count - $script:WATCH_TAIL_MAX)..($ids.Count - 1)] }
  $State.tailIds = $ids
  return $State
}
