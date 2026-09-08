#Requires -Version 5.1
<#
SFDC24 - positional append-cursor state for the board watchers
claude-code-cli, 2026-09-08, to codex's design (CODEX-PR34-WATERMARK-GO-20260908T085300Z)

WHY THIS SHAPE, AND WHAT IT REPLACES
  Two previous designs both lost messages, and both looked correct in their own
  tests.

  v0 kept "the newest 400 row ids I have shown" and rescanned the whole board.
  Past 400 qualifying rows the oldest ids fell out of the set and a restart
  replayed ancient rows as "(missed while offline)" -- handing Mr. Salam
  weeks-old messages as if nobody had answered them.

  v1 replaced that with a timestamp watermark plus a ten-minute skew window.
  codex found the hole: A TIMESTAMP IS NOT AN APPEND CURSOR. A row appended
  later but stamped more than ten minutes behind the mark is skipped forever.
  Demonstrated with the mark at 08:00:00Z: a row stamped 07:49 returns "not new"
  no matter how late it arrives. That is the original defect in a new costume --
  the row exists, and nothing wakes anyone.

  I had also justified rejecting a row position with the wrong fact: that the
  Drive connector truncates the board near row 200. Both watchers read the FULL
  board through bus.ps1, so that constraint belongs to a different reader and I
  imported it. codex was right to push back.

  v2, this file, is a real append cursor: the index of the last processed data
  row, plus the Row_ID sitting at that index as an anchor, plus the board's own
  file id. Before processing anything, the board must still be the same board,
  must not have shrunk, and must still carry the anchor at exactly that index.
  Only rows AFTER it are considered. Position is what "already processed" means
  in an append-only log; a clock is not.

FAIL VISIBLY, NEVER SILENTLY
  Every way this can lose its place -- board replaced, board compacted or
  shrunk, anchor moved, state file corrupt, state file unwritable -- returns a
  reason for the caller to PRINT. The one thing forbidden is carrying on quietly
  as though catch-up were intact, because that is indistinguishable from working
  and is how both earlier versions hid their defects.
#>

$script:WATCH_SCHEMA = 2
# Duplicate collapse compares ROW timestamps, not processing time. The previous
# version used [datetime]::UtcNow, so two identical messages sent hours apart
# were replayed back-to-back after a restart and the second was silently dropped
# -- a de-duplication that became message loss.
$script:WATCH_DUPE_SECONDS = 90

# An ACTUALLY atomic replace, which codex asked for and which took three tries.
#
#   Move-Item -Force      what the first version used. PowerShell implements the
#                         overwrite as delete-then-move, so there is a window in
#                         which the destination does not exist. Assumed atomic
#                         rather than checked.
#   [IO.File]::Replace    the documented .NET primitive, and it does not work
#                         here at all: "The path is not of a legal form" on this
#                         Windows PowerShell 5.1 / CLR 4.0.30319 build, tested
#                         with a fully normalised 42-character path, so it is not
#                         a long-path problem. Measured, not assumed.
#   MoveFileEx            the Win32 call underneath both, with
#                         MOVEFILE_REPLACE_EXISTING. On NTFS, same volume, this
#                         is a genuine atomic replace: the destination is either
#                         the old file or the new one, never absent and never
#                         half-written.
if (-not ('Sfdc24.AtomicFile' -as [type])) {
  Add-Type -Namespace 'Sfdc24' -Name 'AtomicFile' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
[return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
private static extern bool MoveFileExW(string lpExistingFileName, string lpNewFileName, uint dwFlags);

public static void ReplaceAtomic(string source, string destination) {
    const uint MOVEFILE_REPLACE_EXISTING = 0x1;
    const uint MOVEFILE_WRITE_THROUGH    = 0x8;
    if (!MoveFileExW(source, destination, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error());
    }
}
'@ -ErrorAction SilentlyContinue
}

function ConvertTo-WatchTime {
  # Accepts either a string or a [datetime]. PowerShell 7's ConvertFrom-Json
  # deserialises an ISO-8601 string into a [datetime] while 5.1 leaves it a
  # string, so the same state file yields different types per shell; casting the
  # datetime to string then renders it in the CURRENT CULTURE. CI printed
  # "09/08/2026 08:00:00" when this was got wrong. bus.ps1 documents the same
  # trap for the same reason.
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
    schema      = $script:WATCH_SCHEMA
    boardId     = ''    # the board's own file id: identity, not its title
    lastIndex   = 0     # index into rows[] of the last processed DATA row; 0 = none
    anchorId    = ''    # Row_ID expected at lastIndex
    lastEmitKey = ''    # source+text of the last line emitted, for cross-restart dedupe
    lastEmitTs  = ''    # that row's OWN timestamp, never a processing time
  }
}

function Read-WatchState {
  <# Returns @{ state = <state|$null>; reason = <string> }. A reason is present
     whenever the caller must say something out loud. #>
  param([string]$Path)
  if (-not $Path) { return @{ state = $null; reason = 'no state path' } }
  if (-not (Test-Path -LiteralPath $Path)) { return @{ state = $null; reason = '' } }
  try {
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return @{ state = $null; reason = 'state file was empty' } }
    $o = $raw | ConvertFrom-Json
    $schema = 0
    if ($o.PSObject.Properties.Name -contains 'schema') { $schema = [int]$o.schema }
    if ($schema -ne $script:WATCH_SCHEMA) {
      # An older format is not corruption and must not be read as one, but it
      # also cannot be trusted to mean what this version means.
      return @{ state = $null; reason = ('state schema ' + $schema + ' is not ' + $script:WATCH_SCHEMA + '; re-priming') }
    }
    $idx = 0
    if ($o.PSObject.Properties.Name -contains 'lastIndex') { $idx = [int]$o.lastIndex }
    $anchor = [string]$o.anchorId
    $board = [string]$o.boardId
    if ($idx -lt 0 -or (-not $anchor) -or (-not $board)) {
      return @{ state = $null; reason = 'state file is missing its cursor; re-priming' }
    }
    $s = New-WatchState
    $s.boardId = $board
    $s.lastIndex = $idx
    $s.anchorId = $anchor
    $s.lastEmitKey = [string]$o.lastEmitKey
    $ts = ConvertTo-WatchTime $o.lastEmitTs
    $s.lastEmitTs = $(if ($ts) { $ts.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') } else { '' })
    return @{ state = $s; reason = '' }
  } catch {
    return @{ state = $null; reason = 'state file could not be parsed; re-priming' }
  }
}

function Save-WatchState {
  <# Returns @{ ok = <bool>; reason = <string> }. Callers MUST surface a failure:
     a watcher that cannot persist has no restart coverage and must not print a
     line implying it has. #>
  param([Parameter(Mandatory = $true)]$State, [Parameter(Mandatory = $true)][string]$Path)
  # Normalise to a real filesystem path. The Win32 call understands nothing
  # about PowerShell's location stack or forward slashes. Join-Path must NOT be
  # used unconditionally: given an already-rooted path it happily produces
  # C:\repo\C:\Users... which is exactly the
  # "path is not of a legal form" error this first produced.
  try {
    if (-not [System.IO.Path]::IsPathRooted($Path)) {
      $Path = Join-Path (Get-Location).ProviderPath $Path
    }
    $Path = [System.IO.Path]::GetFullPath($Path)
  } catch { }
  $tmp = $Path + '.tmp'
  try {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
      New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $obj = [ordered]@{
      schema      = $script:WATCH_SCHEMA
      savedAt     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
      boardId     = [string]$State.boardId
      lastIndex   = [int]$State.lastIndex
      anchorId    = [string]$State.anchorId
      lastEmitKey = [string]$State.lastEmitKey
      lastEmitTs  = [string]$State.lastEmitTs
    }
    ($obj | ConvertTo-Json -Depth 4 -Compress) | Set-Content -LiteralPath $tmp -Encoding UTF8
    if (Test-Path -LiteralPath $Path) {
      [Sfdc24.AtomicFile]::ReplaceAtomic($tmp, $Path)
    } else {
      # Nothing to replace: a plain move is already all-or-nothing here.
      Move-Item -LiteralPath $tmp -Destination $Path -Force
    }
    return @{ ok = $true; reason = '' }
  } catch {
    try { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } } catch { }
    return @{ ok = $false; reason = ('could not persist state: ' + $_.Exception.Message) }
  }
}

function Test-BoardContinuity {
  <#
    Decide where to resume in this read of the board.

    Returns @{ ok; prime; startIndex; reason }
      prime = $true    nothing to resume from; caller establishes the cursor and
                       emits nothing but its own armed line
      ok = $false      the cursor cannot be trusted. The caller MUST say so and
                       re-prime explicitly. Never treat this as "nothing missed".
  #>
  param($State, $Rows, [string]$BoardId)
  $count = 0
  if ($Rows) { $count = @($Rows).Count }
  if ($count -lt 1) { return @{ ok = $false; prime = $false; startIndex = 0; reason = 'board read returned no rows' } }
  $lastData = $count - 1      # rows[0] is the header

  if (-not $State) { return @{ ok = $true; prime = $true; startIndex = $lastData + 1; reason = '' } }

  if ($BoardId -and $State.boardId -and ($State.boardId -ne $BoardId)) {
    return @{ ok = $false; prime = $false; startIndex = 0
              reason = ('board identity changed (' + $State.boardId + ' -> ' + $BoardId + ')') }
  }
  if ($State.lastIndex -gt $lastData) {
    return @{ ok = $false; prime = $false; startIndex = 0
              reason = ('board shrank: cursor at row ' + $State.lastIndex + ' but only ' + $lastData + ' data rows remain') }
  }
  if ($State.lastIndex -ge 1) {
    $anchor = [string]@($Rows)[$State.lastIndex][0]
    if ($anchor -ne $State.anchorId) {
      return @{ ok = $false; prime = $false; startIndex = 0
                reason = ('anchor mismatch at row ' + $State.lastIndex + ': expected ' + $State.anchorId + ', found ' + $anchor) }
    }
  }
  return @{ ok = $true; prime = $false; startIndex = ($State.lastIndex + 1); reason = '' }
}

function Set-WatchPosition {
  # Advance the cursor to a row index that has now been dealt with. Called for
  # every row CONSIDERED, not only those emitted: a row suppressed by the
  # catch-up cap or the duplicate window has still been handled and must not
  # return as new after a restart.
  param([Parameter(Mandatory = $true)]$State, $Rows, [int]$Index, [string]$BoardId)
  if ($BoardId) { $State.boardId = $BoardId }
  if ($Index -ge 1 -and $Rows -and $Index -lt @($Rows).Count) {
    $State.lastIndex = $Index
    $State.anchorId = [string]@($Rows)[$Index][0]
  }
  return $State
}

function Test-RowIsDuplicate {
  <#
    Is this row a repeat of the one just emitted? Compared on the ROWS' OWN
    timestamps. The console double-posted identical notes one to two seconds
    apart on 2026-09-02 and 2026-09-03, and answering a man twice because his
    browser sent twice is a bad look -- but two identical messages sent HOURS
    apart are two messages, and the previous processing-time comparison threw
    the second away when a restart replayed both together.
  #>
  param($State, [string]$Key, [string]$RowTs)
  if (-not $State -or -not $State.lastEmitKey) { return $false }
  if ($State.lastEmitKey -ne $Key) { return $false }
  $prev = ConvertTo-WatchTime $State.lastEmitTs
  $now = ConvertTo-WatchTime $RowTs
  if (-not $prev -or -not $now) { return $false }   # undateable rows are never collapsed
  $gap = [math]::Abs(($now - $prev).TotalSeconds)
  return ($gap -lt $script:WATCH_DUPE_SECONDS)
}

function Set-WatchEmitted {
  param([Parameter(Mandatory = $true)]$State, [string]$Key, [string]$RowTs)
  $State.lastEmitKey = $Key
  $t = ConvertTo-WatchTime $RowTs
  $State.lastEmitTs = $(if ($t) { $t.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') } else { '' })
  return $State
}
