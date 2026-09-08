#Requires -Version 5.1
<#
SFDC24 - durable cursor, outbox and single-writer state for the board watchers
claude-code-cli, 2026-09-08, to CODEX-PR34-9E95-CONSOLIDATED-REPAIR-GO

WHY THIS FILE KEEPS BEING REWRITTEN
  At 2026-09-07T03:39:45Z Mr. Salam sent "Show me what you can do" from the
  governor console. Nothing was watching. When a watcher armed 21 hours later it
  counted his message among "214 existing messages ignored" and stayed silent.

  Four designs since then have been wrong, and every one passed its own tests:

    v0  newest-400 id set          -> evicted ids replayed ancient rows as new
    v1  timestamp watermark        -> a late row stamped behind the mark was
                                      skipped forever
    v2  positional cursor          -> emitted before persisting, then warned and
                                      carried on when the save failed
    v3  outbox                     -> truncated silently at the cap, accepted a
                                      hollow pending block, verified after
                                      replacing, and had no writer lock

  The common shape is not carelessness about the happy path. It is that each
  version could not tell a failure from a success, so it reported success.

WHAT THIS VERSION HOLDS TO
  - ABSENT and UNUSABLE are different events. Only absent may prime.
  - Nothing is emitted that was not first persisted, and the cursor never
    advances past output that was not staged.
  - ok=$true from Save-WatchState means these exact bytes are on disk and were
    read back. It is proven by a length-prefixed digest, not by the absence of
    an exception and not by a delimiter join that two different arrays can share.
  - One writer per state file, enforced by a lock held for the process lifetime.
  - Every refusal names itself. Silence is never a result.
#>

$script:WATCH_SCHEMA = 4
# Duplicate collapse compares ROW timestamps, never processing time: two
# identical messages sent hours apart are two messages, and comparing wall clock
# turned de-duplication into message loss.
$script:WATCH_DUPE_SECONDS = 90
# The outbox is bounded, and the bound CHUNKS rather than truncates. Staging 200
# of 250 lines while emitting all 250 loses the first 50 on a crash and commits
# past rows whose output was never durable.
$script:WATCH_OUTBOX_MAX = 200

# $IsWindows exists in PowerShell 6+; on Windows PowerShell 5.1 it is undefined,
# and 5.1 only runs on Windows, so undefined means Windows.
$script:WATCH_IS_WINDOWS = $true
if (Test-Path variable:global:IsWindows) { $script:WATCH_IS_WINDOWS = [bool]$IsWindows }

# MoveFileEx is the Win32 call underneath both Move-Item and File.Replace, with
# MOVEFILE_REPLACE_EXISTING. On NTFS, same volume, a genuine atomic replace.
# [IO.File]::Replace does not work on this build at all ("The path is not of a
# legal form", reproduced with a normalised 42-character path). On POSIX,
# rename(2) is atomic and File.Move(src,dst,overwrite) is that call.
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
  # ISO-8601 string into a [datetime] while 5.1 leaves it a string; casting the
  # datetime to string then renders it in the CURRENT CULTURE. CI printed
  # "09/08/2026 08:00:00" when this was wrong.
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

function Test-SafeLeaf {
  <#
    An ALLOWLIST, for every caller, not a blocklist for one.

    Two defects came through this door. A literal 0x0C form feed in a leaf made
    WinPS reject the path so every save failed silently. And an unvalidated
    fleet -Tag was concatenated into a leaf, where three levels of dot-dot reach
    AppData\Local and five reach the profile root -- measured, not supposed.

    Rejecting only control characters, or only the Tag, leaves the hole open for
    the next caller who builds a leaf out of input.
  #>
  param([string]$Leaf)
  if (-not $Leaf) { return @{ ok = $false; reason = 'state file name is empty' } }
  if ($Leaf.Length -gt 120) { return @{ ok = $false; reason = 'state file name is too long' } }
  if ($Leaf -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    return @{ ok = $false; reason = ('state file name ' + $Leaf + ' is not a plain safe name') }
  }
  if ($Leaf -match '\.\.') { return @{ ok = $false; reason = 'state file name contains a traversal segment' } }
  return @{ ok = $true; reason = '' }
}

function Get-DefaultStatePath {
  param([Parameter(Mandatory = $true)][string]$Leaf)
  $safe = Test-SafeLeaf -Leaf $Leaf
  if (-not $safe.ok) { throw $safe.reason }
  $base = $env:LOCALAPPDATA
  if (-not $base) { $base = $env:XDG_STATE_HOME }
  if (-not $base) { $base = $env:HOME }
  if (-not $base) { $base = [System.IO.Path]::GetTempPath() }
  return (Join-Path (Join-Path $base 'sfdc24') $Leaf)
}

function Resolve-StatePath {
  param([string]$Path)
  try {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { $Path = Join-Path (Get-Location).ProviderPath $Path }
    return [System.IO.Path]::GetFullPath($Path)
  } catch { return $Path }
}

function New-WatchState {
  return [pscustomobject]@{
    schema        = $script:WATCH_SCHEMA
    boardId       = ''
    lastIndex     = 0
    anchorId      = ''
    lastEmitKey   = ''
    lastEmitTs    = ''
    pendingIndex  = 0
    pendingAnchor = ''
    pendingLines  = @()
    pendingRowIds = @()
    resetAt       = ''
  }
}

function Get-WatchStateDigest {
  <#
    LENGTH-PREFIXED, then SHA256.

    The previous digest joined arrays with a separator character, so one element
    containing that character hashed identically to two elements, and a single
    empty string hashed identically to an empty array. Two materially different
    outboxes therefore verified as equal -- defeating the exact property the
    digest was added to prove. Board text is arbitrary and may contain anything,
    so no delimiter is safe; a length prefix needs none.
  #>
  param([Parameter(Mandatory = $true)]$State)
  $sb = New-Object System.Text.StringBuilder
  function Add-Field { param($v)
    $t = [string]$v
    [void]$sb.Append($t.Length); [void]$sb.Append(':'); [void]$sb.Append($t); [void]$sb.Append(';')
  }
  function Add-Array { param($a)
    $arr = @($a)
    [void]$sb.Append($arr.Count); [void]$sb.Append('#')
    foreach ($e in $arr) { Add-Field $e }
  }
  Add-Field ([string]$State.boardId)
  Add-Field ([string][int]$State.lastIndex)
  Add-Field ([string]$State.anchorId)
  Add-Field ([string]$State.lastEmitKey)
  Add-Field ([string]$State.lastEmitTs)
  Add-Field ([string][int]$State.pendingIndex)
  Add-Field ([string]$State.pendingAnchor)
  Add-Array $State.pendingRowIds
  Add-Array $State.pendingLines
  Add-Field ([string]$State.resetAt)
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try { return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '') }
  finally { $sha.Dispose() }
}

function Test-StateInvariants {
  <#
    Structural invariants, enforced at the READ boundary so an invalid shape is
    UNUSABLE rather than something every caller must remember to reject.

    A schema-3 file with pendingIndex 5, an empty anchor and empty arrays used to
    read as ok, validate, replay nothing, and then advance the cursor to row 5
    leaving the anchor blank -- skipping five rows and unanchoring the cursor.
  #>
  param($State)
  if ([int]$State.lastIndex -lt 0) { return 'lastIndex is negative' }
  if ([int]$State.pendingIndex -lt 0) { return 'pendingIndex is negative' }
  if (-not $State.boardId) { return 'state has no board identity' }
  if ([int]$State.lastIndex -ge 1 -and -not $State.anchorId) { return 'state has a cursor but no anchor' }
  $lines = @($State.pendingLines); $ids = @($State.pendingRowIds)
  if ([int]$State.pendingIndex -ge 1) {
    if (-not $State.pendingAnchor) { return 'staged outbox has no anchor' }
    if ($lines.Count -lt 1) { return 'staged outbox has no lines' }
    if ($ids.Count -lt 1) { return 'staged outbox has no row identities' }
    if ([int]$State.pendingIndex -le [int]$State.lastIndex) { return 'staged outbox does not advance the cursor' }
  } else {
    if ($lines.Count -gt 0 -or $ids.Count -gt 0) { return 'lines are staged with no cursor to commit them to' }
    if ($State.pendingAnchor) { return 'a staged anchor with no staged cursor' }
  }
  if ($lines.Count -gt $script:WATCH_OUTBOX_MAX) { return 'staged outbox exceeds its bound' }
  return ''
}

function Read-WatchState {
  <# @{ state; disposition = absent|ok|unusable; reason } #>
  param([string]$Path)
  if (-not $Path) { return @{ state = $null; disposition = 'unusable'; reason = 'no state path' } }
  $Path = Resolve-StatePath $Path
  # ABSENT is not UNUSABLE. A missing file means this watcher never ran here and
  # priming is honest. A file that exists and cannot be used means we HAD a
  # cursor and lost it, and priming past the gap hides what this exists to show.
  if (-not (Test-Path -LiteralPath $Path)) { return @{ state = $null; disposition = 'absent'; reason = '' } }
  try {
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return @{ state = $null; disposition = 'unusable'; reason = 'state file was empty' } }
    $o = $raw | ConvertFrom-Json
    $schema = 0
    if ($o.PSObject.Properties.Name -contains 'schema') { $schema = [int]$o.schema }
    if ($schema -ne $script:WATCH_SCHEMA) {
      return @{ state = $null; disposition = 'unusable'; reason = ('state schema ' + $schema + ' is not ' + $script:WATCH_SCHEMA) }
    }
    $s = New-WatchState
    $s.boardId = [string]$o.boardId
    $s.lastIndex = [int]$o.lastIndex
    $s.anchorId = [string]$o.anchorId
    $s.lastEmitKey = [string]$o.lastEmitKey
    $t = ConvertTo-WatchTime $o.lastEmitTs
    $s.lastEmitTs = $(if ($t) { $t.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') } else { '' })
    # NORMALISED EXACTLY LIKE lastEmitTs, and for exactly the same reason.
    # pwsh's ConvertFrom-Json hands an ISO string back as a [datetime]; a bare
    # cast then renders it in the current culture ("09/08/2026 10:30:00"), the
    # digest differs from what was written, and every save carrying a resetAt
    # fails its own verification. In the previous design that check ran AFTER the
    # destination had been replaced, so a pwsh -Reset destroyed the old cursor
    # and then reported "the previous cursor was NOT removed and is intact" --
    # a false receipt over lost bytes. I normalised lastEmitTs and missed the
    # field beside it; chatgpt-codex-desktop found it on the real watcher.
    $rt = ConvertTo-WatchTime $o.resetAt
    $s.resetAt = $(if ($rt) { $rt.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') } else { '' })
    $s.pendingIndex = [int]$o.pendingIndex
    $s.pendingAnchor = [string]$o.pendingAnchor
    $lines = @(); foreach ($l in @($o.pendingLines)) { if ($null -ne $l) { $lines += [string]$l } }
    $ids = @();   foreach ($l in @($o.pendingRowIds)) { if ($null -ne $l) { $ids += [string]$l } }
    $s.pendingLines = $lines
    $s.pendingRowIds = $ids
    $bad = Test-StateInvariants -State $s
    if ($bad) { return @{ state = $null; disposition = 'unusable'; reason = $bad } }
    return @{ state = $s; disposition = 'ok'; reason = '' }
  } catch {
    return @{ state = $null; disposition = 'unusable'; reason = 'state file could not be parsed' }
  }
}

function Enter-WatchLock {
  <#
    ONE WRITER PER STATE FILE, held for the process lifetime.

    Two Save-WatchState calls against one path both returned ok=true while only
    the second survived, so "ok" meant "my bytes were the last ones I looked at"
    rather than "my state survives a restart". A lock states the invariant
    directly; CAS would only detect its violation afterwards.
  #>
  param([Parameter(Mandatory = $true)][string]$Path)
  $Path = Resolve-StatePath $Path
  $dir = Split-Path -Parent $Path
  try {
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null }
    $lockPath = $Path + '.lock'
    $fs = New-Object System.IO.FileStream($lockPath, [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    return @{ ok = $true; handle = $fs; reason = '' }
  } catch {
    return @{ ok = $false; handle = $null
              reason = ('another watcher already holds the cursor at ' + $Path + ' (' + $_.Exception.Message + ')') }
  }
}

function Exit-WatchLock {
  param($Lock)
  if ($Lock -and $Lock.handle) { try { $Lock.handle.Dispose() } catch { } }
}

function Save-WatchState {
  <#
    ok=$true means these exact bytes are on disk and were read back.

    Order matters and got it wrong twice: the temp is now verified BEFORE the
    destination is replaced, so a mismatch can never destroy good bytes; and the
    destination is read back afterwards, because the replace itself can fail in
    ways the write did not.
  #>
  param([Parameter(Mandatory = $true)]$State, [Parameter(Mandatory = $true)][string]$Path)
  $Path = Resolve-StatePath $Path
  # A UNIQUE temp, created new. A fixed sibling .tmp is shared by every writer
  # and a leftover is unattributable.
  $tmp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
  # LOCAL. The watchers run with $ErrorActionPreference='Continue', so a
  # non-terminating cmdlet error never reached the catch: Move-Item failed,
  # wrote nothing, and this returned ok=$true.
  $ErrorActionPreference = 'Stop'
  $bad = Test-StateInvariants -State $State
  if ($bad) { return @{ ok = $false; reason = ('refusing to persist an invalid state: ' + $bad) } }
  try {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null }
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
    $json = $obj | ConvertTo-Json -Depth 5 -Compress
    $stream = New-Object System.IO.FileStream($tmp, [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
      $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
      $stream.Write($bytes, 0, $bytes.Length)
      $stream.Flush($true)
    } finally { $stream.Dispose() }

    # VERIFY THE TEMP FIRST. Replacing and then discovering a mismatch destroys
    # the previous cursor and leaves nothing to fall back to.
    $pre = Read-WatchState -Path $tmp
    $want = Get-WatchStateDigest -State $State
    if (-not $pre.state -or (Get-WatchStateDigest -State $pre.state) -ne $want) {
      Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
      return @{ ok = $false; reason = 'the written state did not verify; the previous cursor is untouched' }
    }

    if (Test-Path -LiteralPath $Path) {
      if ($script:WATCH_IS_WINDOWS) { [Sfdc24.AtomicFile]::ReplaceAtomic($tmp, $Path) }
      else { [System.IO.File]::Move($tmp, $Path, $true) }
    } else {
      Move-Item -LiteralPath $tmp -Destination $Path -Force -ErrorAction Stop
    }

    $post = Read-WatchState -Path $Path
    if (-not $post.state) { return @{ ok = $false; reason = ('state did not read back at ' + $Path + ': ' + $post.reason) } }
    if ((Get-WatchStateDigest -State $post.state) -ne $want) {
      return @{ ok = $false; reason = ('state read back differently at ' + $Path) }
    }
    return @{ ok = $true; reason = '' }
  } catch {
    try { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } } catch { }
    return @{ ok = $false; reason = ('could not persist state: ' + $_.Exception.Message) }
  }
}

function Test-BoardContinuity {
  <# @{ ok; prime; startIndex; reason } -- ok=$false means the caller must print
     the reason and EXIT. Automatic re-priming is banned: it is how a watcher
     skips a gap while looking healthy. #>
  param($State, $Rows, [string]$BoardId)
  $count = 0
  if ($Rows) { $count = @($Rows).Count }
  if ($count -lt 1) { return @{ ok = $false; prime = $false; startIndex = 0; reason = 'board read returned no rows' } }
  $lastData = $count - 1
  if (-not $BoardId) {
    return @{ ok = $false; prime = $false; startIndex = 0
              reason = 'board read carried no identity; refusing to resume against an unidentified board' }
  }
  if (-not $State) { return @{ ok = $true; prime = $true; startIndex = ($lastData + 1); reason = '' } }
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

function Test-PendingIsValid {
  # Called BEFORE anything is replayed. The previous order emitted first and
  # validated afterwards, so a stale outbox from another board reached Mr. Salam
  # as though it were current.
  param($State, $Rows, [string]$BoardId)
  if (-not (Test-HasOutbox -State $State)) { return @{ ok = $true; reason = '' } }
  if (-not $BoardId) { return @{ ok = $false; reason = 'board read carried no identity' } }
  if ($State.boardId -and ($State.boardId -ne $BoardId)) {
    return @{ ok = $false; reason = ('retained outbox belongs to board ' + $State.boardId + ' but this is ' + $BoardId) }
  }
  $lastData = @($Rows).Count - 1
  $pi = [int]$State.pendingIndex
  if ($pi -lt 1 -or $pi -gt $lastData) {
    return @{ ok = $false; reason = ('retained outbox points at row ' + $pi + ', outside the ' + $lastData + ' rows now present') }
  }
  if ([string]@($Rows)[$pi][0] -ne $State.pendingAnchor) {
    return @{ ok = $false; reason = ('retained outbox anchor moved at row ' + $pi + ': expected ' + $State.pendingAnchor + ', found ' + [string]@($Rows)[$pi][0]) }
  }
  $have = @{}
  $from = [Math]::Max(1, [int]$State.lastIndex + 1)
  for ($i = $from; $i -le $pi; $i++) { $have[[string]@($Rows)[$i][0]] = $true }
  foreach ($id in @($State.pendingRowIds)) {
    if ($id -and -not $have.ContainsKey($id)) {
      return @{ ok = $false; reason = ('retained outbox refers to row ' + $id + ' which is no longer between ' + $from + ' and ' + $pi) }
    }
  }
  return @{ ok = $true; reason = '' }
}

function Set-WatchOutbox {
  <#
    Stage a CHUNK. $Plan is an ordered array of @{ line; rowIndex; rowId }.

    The bound used to truncate: 250 planned lines staged the last 200 while the
    watcher emitted all 250 and committed past all of them, so a crash lost the
    first 50 and the cursor had moved beyond rows whose output was never durable.
    Now the first N are staged, only those are emitted, and the cursor advances
    only through the last FULLY represented source row. The remainder is the next
    tick's work.

    Returns @{ state; staged; nextFrom } where staged is the exact set to emit.
  #>
  param([Parameter(Mandatory = $true)]$State, $Rows, $Plan, [int]$FallbackIndex)
  $plan = @($Plan)
  $max = $script:WATCH_OUTBOX_MAX
  $take = [Math]::Min($plan.Count, $max)
  $chunk = @()
  if ($take -gt 0) { $chunk = $plan[0..($take - 1)] }

  # If the chunk stops mid-way, do not commit through a row whose later lines are
  # still unstaged.
  $commitIndex = $FallbackIndex
  if ($take -lt $plan.Count) {
    $lastStagedRow = 0
    foreach ($e in $chunk) { if ([int]$e.rowIndex -gt $lastStagedRow) { $lastStagedRow = [int]$e.rowIndex } }
    $nextRow = [int]$plan[$take].rowIndex
    if ($nextRow -eq $lastStagedRow) { $lastStagedRow = $lastStagedRow - 1 }
    $commitIndex = $lastStagedRow
  }

  $lines = @(); $ids = @()
  foreach ($e in $chunk) { $lines += [string]$e.line; if ($e.rowId) { $ids += [string]$e.rowId } }

  $State.pendingLines = $lines
  $State.pendingRowIds = $ids
  if ($lines.Count -gt 0) {
    $State.pendingIndex = $commitIndex
    if ($Rows -and $commitIndex -ge 1 -and $commitIndex -lt @($Rows).Count) {
      $State.pendingAnchor = [string]@($Rows)[$commitIndex][0]
    }
  } else {
    $State.pendingIndex = 0
    $State.pendingAnchor = ''
  }
  return @{ state = $State; staged = $chunk; remaining = ($plan.Count - $take) }
}

function Complete-WatchOutbox {
  <# THIS MAY NOT TOUCH IDENTITY, and it used to: it took a -BoardId and assigned
     it, so replaying a retained outbox from board-A while pointed at board-B
     rewrote the stored id to board-B, after which the identity check returned
     ok. The replay disarmed the guard meant to stop it. #>
  param([Parameter(Mandatory = $true)]$State)
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

function Set-CommittedCursor {
  # A transition with NO external effect: no lines were produced, so there is
  # nothing to stage and the cursor commits directly.
  param([Parameter(Mandatory = $true)]$State, $Rows, [int]$Index)
  if ($Index -ge 1 -and $Rows -and $Index -lt @($Rows).Count) {
    $State.lastIndex = $Index
    $State.anchorId = [string]@($Rows)[$Index][0]
  }
  return $State
}

function Test-HasOutbox {
  param($State)
  return ($State -and (@($State.pendingLines).Count -gt 0 -or [int]$State.pendingIndex -ge 1))
}

function Test-RowIsDuplicate {
  # Compared on the ROWS' OWN timestamps. Comparing processing time meant two
  # identical messages sent hours apart were replayed together after a restart
  # and the second was dropped.
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
