#Requires -Version 5.1
<#
SFDC24 - durable cursor + outbox for the board watchers
claude-code-cli, 2026-09-08, to the design in
CODEX-PR34-OUTBOX-DESIGN-20260908T092600Z (chatgpt-codex-desktop)

WHY THIS EXISTS AT ALL
  At 2026-09-07T03:39:45Z Mr. Salam sent "Show me what you can do" from the
  governor console. Nothing was watching. When a watcher armed 21 hours later it
  counted his message among "214 existing messages ignored" and stayed silent.
  Every version of this file is an attempt to make that impossible.

FOUR DESIGNS, THREE OF THEM WRONG
  v0  newest 400 row ids, whole-board rescan. Past 400 qualifying rows the
      oldest were evicted and a restart replayed ancient rows as "(missed while
      offline)" -- crying wolf at him with weeks-old messages.
  v1  timestamp watermark plus a skew window. A TIMESTAMP IS NOT AN APPEND
      CURSOR: a row appended later but stamped more than the window behind is
      skipped forever. The original defect in a new costume.
  v2  positional cursor, which is right, but it emitted BEFORE persisting and
      then warned-and-continued when the save failed. External effect and
      durable state disagree, so a restart either replays from a stale cursor or
      cold-primes over the gap. Silent again.
  v3  this file.

THE FACT THE DESIGN STARTS FROM
  Exactly-once external notification is impossible without a consumer ACK. There
  is no ACK here: stdout goes to a Monitor task and then to a person. So the
  honest choice is explicit AT-LEAST-ONCE with no silent loss, which means a
  durable outbox:

    1. persist the exact lines to be emitted, atomically, BEFORE emitting;
    2. emit them;
    3. atomically commit the cursor and clear the outbox.

  A crash anywhere leaves the outbox on disk. The next start says plainly that
  what follows may be a repeat, replays the EXACT persisted lines, and commits.
  A repeat that announces itself is recoverable; a silent gap is not.

FAIL CLOSED, AND NEVER AUTO-REPRIME
  - outbox save fails  -> nothing is emitted, exit non-zero. No external effect
                          happened, so there is nothing ambiguous to reconcile.
  - commit save fails  -> exit non-zero with the outbox RETAINED, so the next
                          start replays rather than skips.
  - corrupt state, board reset, shrink, anchor mismatch -> refuse and exit. The
    operator re-primes deliberately with -Reset, which is itself persisted and
    receipted. Automatic re-priming is how a watcher silently skips a gap while
    looking healthy, and it is banned here.
#>

$script:WATCH_SCHEMA = 3
# Duplicate collapse compares ROW timestamps, never processing time. Comparing
# processing time meant two identical messages sent hours apart were replayed
# back-to-back after a restart and the second was dropped -- a de-duplication
# that had become message loss.
$script:WATCH_DUPE_SECONDS = 90
# The outbox is bounded. A watcher that has been down for a week must not try to
# persist thousands of lines in one atomic write.
$script:WATCH_OUTBOX_MAX = 200

# An ACTUALLY atomic replace, which took three attempts, all measured:
#   Move-Item -Force      PowerShell overwrites by delete-then-move, so the
#                         destination briefly does not exist. Assumed atomic.
#   [IO.File]::Replace    does not work on this build at all -- "The path is not
#                         of a legal form" on WinPS 5.1 / CLR 4.0.30319,
#                         reproduced with a normalised 42-character path, so not
#                         a long-path problem.
#   MoveFileEx            the Win32 call under both, with
#                         MOVEFILE_REPLACE_EXISTING. On NTFS, same volume, a
#                         genuine atomic replace.
# $IsWindows exists in PowerShell 6+; on Windows PowerShell 5.1 it is undefined,
# and 5.1 only runs on Windows, so undefined means Windows.
$script:WATCH_IS_WINDOWS = $true
if (Test-Path variable:global:IsWindows) { $script:WATCH_IS_WINDOWS = [bool]$IsWindows }

if ($script:WATCH_IS_WINDOWS -and -not ('Sfdc24.AtomicFile' -as [type])) {
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
  # Accepts a string or a [datetime]. PowerShell 7's ConvertFrom-Json turns an
  # ISO-8601 string into a [datetime] while 5.1 leaves it a string, so the same
  # file yields different types per shell; casting the datetime to string then
  # renders it in the CURRENT CULTURE. CI printed "09/08/2026 08:00:00" when this
  # was wrong. bus.ps1 documents the same trap.
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

function Get-DefaultStatePath {
  <#
    Build the default state path.

    This exists because of a real defect: fleet_watch.ps1 carried a literal 0x0C
    FORM FEED in its default leaf -- an escape that survived a source transform
    and ate a character. WinPS 5.1 rejected the path, so every save failed and
    every restart was cold. Every test passed an explicit -StateFile, so nothing
    ever exercised the default. chatgpt-codex-desktop found it by reading bytes.

    The leaf is validated, not trusted.
  #>
  param([Parameter(Mandatory = $true)][string]$Leaf)
  foreach ($ch in $Leaf.ToCharArray()) {
    if ([int]$ch -lt 32 -or [int]$ch -eq 127) {
      throw ('state file name contains control character 0x' + ('{0:X2}' -f [int]$ch) + '; refusing to build a path from it')
    }
  }
  $base = $env:LOCALAPPDATA
  if (-not $base) { $base = $env:XDG_STATE_HOME }
  if (-not $base) { $base = $env:HOME }
  if (-not $base) { $base = [System.IO.Path]::GetTempPath() }
  return (Join-Path (Join-Path $base 'sfdc24') $Leaf)
}

function New-WatchState {
  return [pscustomobject]@{
    schema        = $script:WATCH_SCHEMA
    boardId       = ''
    lastIndex     = 0      # committed cursor: index of the last row fully dealt with
    anchorId      = ''     # Row_ID expected at lastIndex
    lastEmitKey   = ''
    lastEmitTs    = ''
    pendingIndex  = 0      # outbox: the cursor these lines WOULD commit
    pendingAnchor = ''
    pendingLines  = @()    # the exact strings to emit
    pendingRowIds = @()
    resetAt       = ''     # receipt for a deliberate -Reset
  }
}

function Resolve-StatePath {
  # The Win32 call understands nothing about PowerShell's location stack or
  # forward slashes. Join-Path must NOT be used unconditionally: given an
  # already-rooted path it produces "C:\repo\C:\Users\..." which is exactly the
  # "path is not of a legal form" error this first produced.
  param([string]$Path)
  try {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { $Path = Join-Path (Get-Location).ProviderPath $Path }
    return [System.IO.Path]::GetFullPath($Path)
  } catch { return $Path }
}

function Read-WatchState {
  <# @{ state; reason }. A reason is present whenever the caller must speak. #>
  param([string]$Path)
  if (-not $Path) { return @{ state = $null; reason = 'no state path' } }
  $Path = Resolve-StatePath $Path
  if (-not (Test-Path -LiteralPath $Path)) { return @{ state = $null; reason = '' } }
  try {
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return @{ state = $null; reason = 'state file was empty' } }
    $o = $raw | ConvertFrom-Json
    $schema = 0
    if ($o.PSObject.Properties.Name -contains 'schema') { $schema = [int]$o.schema }
    if ($schema -ne $script:WATCH_SCHEMA) {
      return @{ state = $null; reason = ('state schema ' + $schema + ' is not ' + $script:WATCH_SCHEMA) }
    }
    $s = New-WatchState
    $s.boardId = [string]$o.boardId
    $s.lastIndex = [int]$o.lastIndex
    $s.anchorId = [string]$o.anchorId
    $s.lastEmitKey = [string]$o.lastEmitKey
    $t = ConvertTo-WatchTime $o.lastEmitTs
    $s.lastEmitTs = $(if ($t) { $t.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') } else { '' })
    $s.resetAt = [string]$o.resetAt
    if ($o.PSObject.Properties.Name -contains 'pendingIndex') { $s.pendingIndex = [int]$o.pendingIndex }
    $s.pendingAnchor = [string]$o.pendingAnchor
    $lines = @(); foreach ($l in @($o.pendingLines)) { if ($null -ne $l) { $lines += [string]$l } }
    $ids = @();   foreach ($l in @($o.pendingRowIds)) { if ($null -ne $l) { $ids += [string]$l } }
    $s.pendingLines = $lines
    $s.pendingRowIds = $ids
    # An anchor is only meaningful once something has been COMMITTED. A state
    # carrying only a staged outbox has no committed row yet and is still valid;
    # requiring an anchor there made the first save of a fresh watcher fail its
    # own read-back verification.
    if (-not $s.boardId) { return @{ state = $null; reason = 'state file is missing its board identity' } }
    if ($s.lastIndex -ge 1 -and -not $s.anchorId) {
      return @{ state = $null; reason = 'state file has a cursor but no anchor' }
    }
    return @{ state = $s; reason = '' }
  } catch {
    return @{ state = $null; reason = 'state file could not be parsed' }
  }
}

function Save-WatchState {
  <# @{ ok; reason }. ok=$true means the state survives a restart, and that is
     proven by reading it back, not inferred from the absence of an exception. #>
  param([Parameter(Mandatory = $true)]$State, [Parameter(Mandatory = $true)][string]$Path)
  $Path = Resolve-StatePath $Path
  $tmp = $Path + '.tmp'
  # LOCAL, and it matters. The watchers run with $ErrorActionPreference =
  # 'Continue', so a non-terminating cmdlet error inside this try never reached
  # the catch: Move-Item failed, wrote nothing, and this returned ok=$true. A
  # persistence guard that reports success when it silently failed is worse than
  # no guard. Found by pointing the cursor at a path whose parent is a file.
  $ErrorActionPreference = 'Stop'
  try {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
      New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
    }
    $obj = [ordered]@{
      schema        = $script:WATCH_SCHEMA
      savedAt       = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
      boardId       = [string]$State.boardId
      lastIndex     = [int]$State.lastIndex
      anchorId      = [string]$State.anchorId
      lastEmitKey   = [string]$State.lastEmitKey
      lastEmitTs    = [string]$State.lastEmitTs
      pendingIndex  = [int]$State.pendingIndex
      pendingAnchor = [string]$State.pendingAnchor
      pendingLines  = @($State.pendingLines)
      pendingRowIds = @($State.pendingRowIds)
      resetAt       = [string]$State.resetAt
    }
    ($obj | ConvertTo-Json -Depth 5 -Compress) | Set-Content -LiteralPath $tmp -Encoding UTF8 -ErrorAction Stop
    if (Test-Path -LiteralPath $Path) {
      # PLATFORM-AWARE. MoveFileEx is a kernel32 P/Invoke, so on pwsh/Linux every
      # replace throws DllNotFoundException -- and the first version of the test
      # asserted only that the destination existed, so CI would have stayed green
      # while no save ever succeeded there. chatgpt-codex-desktop found that.
      #
      # On POSIX, rename(2) is itself atomic and replaces, and .NET Core's
      # File.Move(src, dst, overwrite:true) is that call. On Windows PowerShell
      # 5.1 the three-argument overload does not exist, hence the split.
      if ($script:WATCH_IS_WINDOWS) {
        [Sfdc24.AtomicFile]::ReplaceAtomic($tmp, $Path)
      } else {
        [System.IO.File]::Move($tmp, $Path, $true)
      }
    }
    else { Move-Item -LiteralPath $tmp -Destination $Path -Force -ErrorAction Stop }

    # READ IT BACK. D-4: read-back is the only proof of a write, and this
    # function's entire contract is that ok=$true means the state survives.
    $v = Read-WatchState -Path $Path
    if (-not $v.state) { return @{ ok = $false; reason = ('state did not read back at ' + $Path + ': ' + $v.reason) } }
    if ([int]$v.state.lastIndex -ne [int]$State.lastIndex -or
        [int]$v.state.pendingIndex -ne [int]$State.pendingIndex -or
        @($v.state.pendingLines).Count -ne @($State.pendingLines).Count) {
      return @{ ok = $false; reason = ('state read back differently at ' + $Path) }
    }
    return @{ ok = $true; reason = '' }
  } catch {
    try { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } } catch { }
    return @{ ok = $false; reason = ('could not persist state: ' + $_.Exception.Message) }
  }
}

function Test-BoardContinuity {
  <#
    @{ ok; prime; startIndex; reason }
      prime      nothing to resume from; the caller establishes the cursor
      ok=$false  the cursor cannot be trusted. The caller MUST print the reason
                 and EXIT. Automatic re-priming is banned: it is how a watcher
                 skips a gap while looking healthy.
  #>
  param($State, $Rows, [string]$BoardId)
  $count = 0
  if ($Rows) { $count = @($Rows).Count }
  if ($count -lt 1) { return @{ ok = $false; prime = $false; startIndex = 0; reason = 'board read returned no rows' } }
  $lastData = $count - 1
  if (-not $State) { return @{ ok = $true; prime = $true; startIndex = ($lastData + 1); reason = '' } }
  # FAIL CLOSED ON AN UNKNOWN BOARD. The first version only compared when BOTH
  # ids were truthy, so a read that returned no fileId skipped the check entirely
  # and the watcher carried on as though identity had been confirmed. Not
  # knowing which board this is, is exactly the case that must stop.
  if (-not $BoardId) {
    return @{ ok = $false; prime = $false; startIndex = 0
              reason = 'board read carried no identity; refusing to resume against an unidentified board' }
  }
  if ($State.boardId -and ($State.boardId -ne $BoardId)) {
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

function Set-WatchOutbox {
  # Stage the exact lines that are about to be emitted, with the cursor they
  # would commit. Persisted BEFORE anything reaches stdout.
  param([Parameter(Mandatory = $true)]$State, $Rows, [int]$Index, [string[]]$Lines, [string[]]$RowIds)
  $l = @($Lines); $r = @($RowIds)
  if ($l.Count -gt $script:WATCH_OUTBOX_MAX) { $l = $l[($l.Count - $script:WATCH_OUTBOX_MAX)..($l.Count - 1)] }
  if ($r.Count -gt $script:WATCH_OUTBOX_MAX) { $r = $r[($r.Count - $script:WATCH_OUTBOX_MAX)..($r.Count - 1)] }
  $State.pendingLines = $l
  $State.pendingRowIds = $r
  $State.pendingIndex = $Index
  if ($Rows -and $Index -ge 1 -and $Index -lt @($Rows).Count) { $State.pendingAnchor = [string]@($Rows)[$Index][0] }
  return $State
}

function Complete-WatchOutbox {
  # The lines are out. Move the staged cursor to committed and clear the outbox.
  param([Parameter(Mandatory = $true)]$State, [string]$BoardId)
  if ($BoardId) { $State.boardId = $BoardId }
  if ($State.pendingIndex -ge 1) {
    $State.lastIndex = $State.pendingIndex
    if ($State.pendingAnchor) { $State.anchorId = $State.pendingAnchor }
  }
  $State.pendingIndex = 0
  $State.pendingAnchor = ''
  $State.pendingLines = @()
  $State.pendingRowIds = @()
  return $State
}

function Test-HasOutbox {
  param($State)
  return ($State -and (@($State.pendingLines).Count -gt 0 -or [int]$State.pendingIndex -ge 1))
}

function Test-RowIsDuplicate {
  # Compared on the ROWS' OWN timestamps. The console double-posted identical
  # notes one to two seconds apart, and answering a man twice because his browser
  # sent twice is a bad look -- but two identical messages sent HOURS apart are
  # two messages, and a processing-time comparison threw the second away.
  param($State, [string]$Key, [string]$RowTs)
  if (-not $State -or -not $State.lastEmitKey) { return $false }
  if ($State.lastEmitKey -ne $Key) { return $false }
  $prev = ConvertTo-WatchTime $State.lastEmitTs
  $now = ConvertTo-WatchTime $RowTs
  if (-not $prev -or -not $now) { return $false }
  return ([math]::Abs(($now - $prev).TotalSeconds) -lt $script:WATCH_DUPE_SECONDS)
}

function Set-WatchEmitted {
  param([Parameter(Mandatory = $true)]$State, [string]$Key, [string]$RowTs)
  $State.lastEmitKey = $Key
  $t = ConvertTo-WatchTime $RowTs
  $State.lastEmitTs = $(if ($t) { $t.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') } else { '' })
  return $State
}
