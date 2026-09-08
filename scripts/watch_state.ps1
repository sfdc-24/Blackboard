#Requires -Version 5.1
<#
SFDC24 - durable cursor, outbox and single-writer state for the board watchers
claude-code-cli, 2026-09-08, schema 5, to CODEX-PR34-868-REPAIR-GO-A

WHY THIS FILE KEEPS BEING REWRITTEN
  At 2026-09-07T03:39:45Z Mr. Salam sent "Show me what you can do" from the
  governor console. Nothing was watching, and when a watcher armed 21 hours later
  it counted his message among "214 existing messages ignored" and stayed silent.

  Five designs since then have been wrong, and every one passed its own tests:

    v0  newest-400 id set     -> evicted ids replayed ancient rows as new
    v1  timestamp watermark   -> a late row stamped behind the mark was skipped
    v2  positional cursor     -> emitted before persisting, then warned and
                                 carried on when the save failed
    v3  outbox                -> truncated at the cap, accepted a hollow pending
                                 block, verified after replacing, no writer lock
    v4  chunked outbox        -> the replay path could commit past rows it never
                                 accounted for; the dedupe marker advanced by
                                 PLANNING rather than by emitting; and the staged
                                 lines were swallowed by a function return value
                                 so nothing reached stdout at all

  The shape is always the same: the code could not tell a failure from a success,
  so it reported success.

WHAT SCHEMA 5 ADDS
  A staged chunk now carries the EVIDENCE needed to recompute the exact plan it
  came from: where the scan started, which selection was in force, and the dedupe
  marker that was in effect at plan time. Before a single line is replayed, the
  plan is recomputed against the board in hand and must match exactly - ordered,
  complete, no omission, no reorder, no duplicate, no extra, and the same lines.
  A cursor may never advance beyond a plan that has been proven that way.

  The state file is also validated strictly rather than projected: exact property
  set, real JSON types, and every size bounded BEFORE the content is materialised.
#>

$script:WATCH_SCHEMA = 5
$script:WATCH_DUPE_SECONDS = 90
$script:WATCH_OUTBOX_MAX   = 200
# Bounds are checked before parsing, not after. An unbounded line is emitted
# verbatim to him and an unbounded id array is iterated during validation.
$script:WATCH_MAX_BYTES    = 262144
$script:WATCH_MAX_LINE     = 4000
$script:WATCH_MAX_FIELD    = 400

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
  # pwsh's ConvertFrom-Json returns an ISO string as a [datetime] while 5.1
  # leaves it a string; a bare cast then renders it in the CURRENT CULTURE.
  # That difference silently broke a -Reset on pwsh once already.
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
function Format-WatchTime { param($V) $t = ConvertTo-WatchTime $V; if ($t) { return $t.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') } return '' }

function Test-SafeLeaf {
  # An ALLOWLIST for every caller, not a blocklist for one. A literal form feed
  # in a leaf made every save fail silently, and an unvalidated -Tag reached
  # AppData\Local at three levels of dot-dot and the profile root at five.
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
    # ---- plan evidence: enough to recompute the exact plan before replay ----
    planFrom      = 0    # first board row index the scan considered
    planMode      = ''   # cold | warm | steady
    planLimit     = 0    # -Backfill for cold, -CatchUpMax for warm, 0 otherwise
    planEmitKey   = ''   # dedupe marker IN FORCE when the plan was built
    planEmitTs    = ''
    resetAt       = ''
  }
}

# Exactly these, no more and no fewer. The reader used to PROJECT the fields it
# wanted, so a file of {schema, boardId} alone read as usable.
$script:WATCH_REQUIRED = @(
  'schema','savedAt','boardId','lastIndex','anchorId','lastEmitKey','lastEmitTs',
  'pendingIndex','pendingAnchor','pendingLines','pendingRowIds',
  'planFrom','planMode','planLimit','planEmitKey','planEmitTs','resetAt'
)

function Get-WatchStateDigest {
  # LENGTH-PREFIXED, then SHA256. A delimiter join let one element containing the
  # separator hash identically to two elements, and one empty string identically
  # to an empty array -- defeating the property the digest exists to prove.
  param([Parameter(Mandatory = $true)]$State)
  $sb = New-Object System.Text.StringBuilder
  function Add-F { param($v) $t = [string]$v; [void]$sb.Append($t.Length); [void]$sb.Append(':'); [void]$sb.Append($t); [void]$sb.Append(';') }
  function Add-A { param($a) $arr = @($a); [void]$sb.Append($arr.Count); [void]$sb.Append('#'); foreach ($e in $arr) { Add-F $e } }
  Add-F $State.boardId;      Add-F ([int]$State.lastIndex);  Add-F $State.anchorId
  Add-F $State.lastEmitKey;  Add-F $State.lastEmitTs
  Add-F ([int]$State.pendingIndex); Add-F $State.pendingAnchor
  Add-A $State.pendingRowIds; Add-A $State.pendingLines
  Add-F ([int]$State.planFrom); Add-F $State.planMode; Add-F ([int]$State.planLimit)
  Add-F $State.planEmitKey;  Add-F $State.planEmitTs;  Add-F $State.resetAt
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try { return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '') }
  finally { $sha.Dispose() }
}

function Test-StateInvariants {
  param($State)
  if ([int]$State.lastIndex -lt 0) { return 'lastIndex is negative' }
  if ([int]$State.pendingIndex -lt 0) { return 'pendingIndex is negative' }
  if (-not $State.boardId) { return 'state has no board identity' }
  # Parentheses matter: [string]$x.Length casts the LENGTH, not $x, and then
  # compares a string to an int -- "7" -gt 400 is TRUE as a string comparison, so
  # every ordinary board id failed this check. Caught by the harness, not by
  # reading it.
  if (([string]$State.boardId).Length -gt $script:WATCH_MAX_FIELD) { return 'board identity is too long' }
  if ([int]$State.lastIndex -ge 1 -and -not $State.anchorId) { return 'state has a cursor but no anchor' }
  $lines = @($State.pendingLines); $ids = @($State.pendingRowIds)
  if ([int]$State.pendingIndex -ge 1) {
    if (-not $State.pendingAnchor) { return 'staged outbox has no anchor' }
    if ($lines.Count -lt 1) { return 'staged outbox has no lines' }
    if ($ids.Count -ne $lines.Count) { return 'staged line and row-id counts differ' }
    if ([int]$State.pendingIndex -le [int]$State.lastIndex) { return 'staged outbox does not advance the cursor' }
    if ([int]$State.planFrom -lt 1) { return 'staged outbox has no plan start' }
    if ([int]$State.planFrom -gt [int]$State.pendingIndex) { return 'plan start is beyond the staged cursor' }
    if ('cold','warm','steady' -notcontains [string]$State.planMode) { return 'staged outbox has no valid plan mode' }
    $seen = @{}
    foreach ($id in $ids) {
      if (-not $id) { return 'staged outbox has an empty row id' }
      if (([string]$id).Length -gt $script:WATCH_MAX_FIELD) { return 'a staged row id is too long' }
      if ($seen.ContainsKey($id)) { return ('staged outbox repeats row id ' + $id) }
      $seen[$id] = $true
    }
  } else {
    if ($lines.Count -gt 0 -or $ids.Count -gt 0) { return 'lines are staged with no cursor to commit them to' }
    if ($State.pendingAnchor) { return 'a staged anchor with no staged cursor' }
    if ([int]$State.planFrom -ne 0 -or [string]$State.planMode -ne '') { return 'plan evidence with no staged chunk' }
  }
  if ($lines.Count -gt $script:WATCH_OUTBOX_MAX) { return 'staged outbox exceeds its bound' }
  foreach ($l in $lines) { if (([string]$l).Length -gt $script:WATCH_MAX_LINE) { return 'a staged line exceeds its bound' } }
  return ''
}

function Read-WatchState {
  <# @{ state; disposition = absent|ok|unusable; reason } #>
  param([string]$Path)
  if (-not $Path) { return @{ state = $null; disposition = 'unusable'; reason = 'no state path' } }
  $Path = Resolve-StatePath $Path
  if (-not (Test-Path -LiteralPath $Path)) { return @{ state = $null; disposition = 'absent'; reason = '' } }
  try {
    # BOUND BEFORE MATERIALISING. Checking sizes after parsing is checking them
    # after the cost has already been paid.
    $fi = New-Object System.IO.FileInfo($Path)
    if ($fi.Length -gt $script:WATCH_MAX_BYTES) {
      return @{ state = $null; disposition = 'unusable'; reason = ('state file is ' + $fi.Length + ' bytes, over the ' + $script:WATCH_MAX_BYTES + ' bound') }
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return @{ state = $null; disposition = 'unusable'; reason = 'state file was empty' } }
    $o = $raw | ConvertFrom-Json

    # EXACT property set, no extras and no omissions.
    $have = @($o.PSObject.Properties.Name)
    foreach ($k in $script:WATCH_REQUIRED) { if ($have -notcontains $k) { return @{ state = $null; disposition = 'unusable'; reason = ('state file is missing ' + $k) } } }
    foreach ($k in $have) { if ($script:WATCH_REQUIRED -notcontains $k) { return @{ state = $null; disposition = 'unusable'; reason = ('state file carries an unexpected property ' + $k) } } }

    if (([int]$o.schema) -ne $script:WATCH_SCHEMA) {
      return @{ state = $null; disposition = 'unusable'; reason = ('state schema ' + [string]$o.schema + ' is not ' + $script:WATCH_SCHEMA) }
    }
    # TYPES, not coercions. "3" is not 3.
    foreach ($k in @('lastIndex','pendingIndex','planFrom','planLimit','schema')) {
      if (-not ($o.$k -is [int] -or $o.$k -is [long] -or $o.$k -is [double])) {
        return @{ state = $null; disposition = 'unusable'; reason = ($k + ' is not a JSON number') }
      }
    }
    foreach ($k in @('boardId','anchorId','lastEmitKey','lastEmitTs','pendingAnchor','planMode','planEmitKey','planEmitTs','resetAt','savedAt')) {
      if ($null -ne $o.$k -and -not ($o.$k -is [string] -or $o.$k -is [datetime])) {
        return @{ state = $null; disposition = 'unusable'; reason = ($k + ' is not a JSON string') }
      }
      if (($o.$k -is [string]) -and ([string]$o.$k).Length -gt $script:WATCH_MAX_FIELD) {
        return @{ state = $null; disposition = 'unusable'; reason = ($k + ' exceeds its length bound') }
      }
    }
    foreach ($k in @('pendingLines','pendingRowIds')) {
      $v = $o.$k
      if ($null -eq $v) { return @{ state = $null; disposition = 'unusable'; reason = ($k + ' is missing') } }
      if (-not ($v -is [System.Array] -or $v -is [System.Collections.IList])) {
        return @{ state = $null; disposition = 'unusable'; reason = ($k + ' is not a JSON array') }
      }
      if (@($v).Count -gt $script:WATCH_OUTBOX_MAX) {
        return @{ state = $null; disposition = 'unusable'; reason = ($k + ' exceeds the ' + $script:WATCH_OUTBOX_MAX + ' bound') }
      }
      foreach ($e in @($v)) {
        if (-not ($e -is [string])) { return @{ state = $null; disposition = 'unusable'; reason = ($k + ' holds a non-string element') } }
        if (([string]$e).Length -gt $script:WATCH_MAX_LINE) { return @{ state = $null; disposition = 'unusable'; reason = ($k + ' holds an element over its length bound') } }
      }
    }

    $s = New-WatchState
    $s.boardId = [string]$o.boardId
    $s.lastIndex = [int]$o.lastIndex
    $s.anchorId = [string]$o.anchorId
    $s.lastEmitKey = [string]$o.lastEmitKey
    $s.lastEmitTs = Format-WatchTime $o.lastEmitTs
    $s.pendingIndex = [int]$o.pendingIndex
    $s.pendingAnchor = [string]$o.pendingAnchor
    $s.pendingLines = @(@($o.pendingLines) | ForEach-Object { [string]$_ })
    $s.pendingRowIds = @(@($o.pendingRowIds) | ForEach-Object { [string]$_ })
    $s.planFrom = [int]$o.planFrom
    $s.planMode = [string]$o.planMode
    $s.planLimit = [int]$o.planLimit
    $s.planEmitKey = [string]$o.planEmitKey
    $s.planEmitTs = Format-WatchTime $o.planEmitTs
    $s.resetAt = Format-WatchTime $o.resetAt

    $bad = Test-StateInvariants -State $s
    if ($bad) { return @{ state = $null; disposition = 'unusable'; reason = $bad } }
    return @{ state = $s; disposition = 'ok'; reason = '' }
  } catch {
    return @{ state = $null; disposition = 'unusable'; reason = 'state file could not be parsed' }
  }
}

function Enter-WatchLock {
  # ONE WRITER per state file, held for the process lifetime. Two saves against
  # one path both used to report success while only the second survived.
  param([Parameter(Mandatory = $true)][string]$Path)
  $Path = Resolve-StatePath $Path
  $dir = Split-Path -Parent $Path
  try {
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null }
    $fs = New-Object System.IO.FileStream(($Path + '.lock'), [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    return @{ ok = $true; handle = $fs; reason = '' }
  } catch {
    return @{ ok = $false; handle = $null; reason = ('another watcher already holds the cursor at ' + $Path) }
  }
}
function Exit-WatchLock { param($Lock) if ($Lock -and $Lock.handle) { try { $Lock.handle.Dispose() } catch { } } }

function Save-WatchState {
  # ok=$true means these exact bytes are on disk and were read back. The temp is
  # verified BEFORE the replace so a mismatch can never destroy good bytes.
  param([Parameter(Mandatory = $true)]$State, [Parameter(Mandatory = $true)][string]$Path)
  $Path = Resolve-StatePath $Path
  $tmp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
  $ErrorActionPreference = 'Stop'
  $bad = Test-StateInvariants -State $State
  if ($bad) { return @{ ok = $false; reason = ('refusing to persist an invalid state: ' + $bad) } }
  try {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null }
    $obj = [ordered]@{
      schema = $script:WATCH_SCHEMA
      savedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
      boardId = [string]$State.boardId
      lastIndex = [int]$State.lastIndex
      anchorId = [string]$State.anchorId
      lastEmitKey = [string]$State.lastEmitKey
      lastEmitTs = [string]$State.lastEmitTs
      pendingIndex = [int]$State.pendingIndex
      pendingAnchor = [string]$State.pendingAnchor
      pendingLines = @($State.pendingLines)
      pendingRowIds = @($State.pendingRowIds)
      planFrom = [int]$State.planFrom
      planMode = [string]$State.planMode
      planLimit = [int]$State.planLimit
      planEmitKey = [string]$State.planEmitKey
      planEmitTs = [string]$State.planEmitTs
      resetAt = [string]$State.resetAt
    }
    $json = $obj | ConvertTo-Json -Depth 5 -Compress
    $stream = New-Object System.IO.FileStream($tmp, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try { $b = [System.Text.Encoding]::UTF8.GetBytes($json); $stream.Write($b, 0, $b.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }

    $pre = Read-WatchState -Path $tmp
    $want = Get-WatchStateDigest -State $State
    if (-not $pre.state -or (Get-WatchStateDigest -State $pre.state) -ne $want) {
      Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
      return @{ ok = $false; reason = ('the written state did not verify (' + $pre.reason + '); the previous cursor is untouched') }
    }
    if (Test-Path -LiteralPath $Path) {
      if ($script:WATCH_IS_WINDOWS) { [Sfdc24.AtomicFile]::ReplaceAtomic($tmp, $Path) }
      else { [System.IO.File]::Move($tmp, $Path, $true) }
    } else { Move-Item -LiteralPath $tmp -Destination $Path -Force -ErrorAction Stop }

    $post = Read-WatchState -Path $Path
    if (-not $post.state) { return @{ ok = $false; reason = ('state did not read back: ' + $post.reason) } }
    if ((Get-WatchStateDigest -State $post.state) -ne $want) { return @{ ok = $false; reason = 'state read back differently' }  }
    return @{ ok = $true; reason = '' }
  } catch {
    try { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } } catch { }
    return @{ ok = $false; reason = ('could not persist state: ' + $_.Exception.Message) }
  }
}

function Test-BoardContinuity {
  param($State, $Rows, [string]$BoardId)
  $count = 0
  if ($Rows) { $count = @($Rows).Count }
  if ($count -lt 1) { return @{ ok = $false; prime = $false; startIndex = 0; reason = 'board read returned no rows' } }
  $lastData = $count - 1
  if (-not $BoardId) { return @{ ok = $false; prime = $false; startIndex = 0; reason = 'board read carried no identity; refusing to resume against an unidentified board' } }
  if (-not $State) { return @{ ok = $true; prime = $true; startIndex = ($lastData + 1); reason = '' } }
  if ($State.boardId -and ($State.boardId -ne $BoardId)) {
    return @{ ok = $false; prime = $false; startIndex = 0; reason = ('board identity changed (' + $State.boardId + ' -> ' + $BoardId + ')') }
  }
  if ($State.lastIndex -gt $lastData) {
    return @{ ok = $false; prime = $false; startIndex = 0; reason = ('board shrank: cursor at row ' + $State.lastIndex + ' but only ' + $lastData + ' data rows remain') }
  }
  if ($State.lastIndex -ge 1 -and ([string]@($Rows)[$State.lastIndex][0]) -ne $State.anchorId) {
    return @{ ok = $false; prime = $false; startIndex = 0
              reason = ('anchor mismatch at row ' + $State.lastIndex + ': expected ' + $State.anchorId + ', found ' + [string]@($Rows)[$State.lastIndex][0]) }
  }
  return @{ ok = $true; prime = $false; startIndex = ($State.lastIndex + 1); reason = '' }
}

function Test-HasOutbox { param($State) return ($State -and (@($State.pendingLines).Count -gt 0 -or [int]$State.pendingIndex -ge 1)) }

function Test-PendingMatchesPlan {
  <#
    The staged chunk must be EXACTLY the plan it claims to be.

    The interval check this replaces only proved the supplied ids were SOMEWHERE
    inside [lastIndex+1..pendingIndex]. A block holding one line for r2 with
    pendingIndex 12 therefore passed, replayed one line, and committed the cursor
    to 12 -- silently skipping r3 to r12. So the caller now recomputes the exact
    ordered eligible plan from the board in hand, using the persisted plan
    evidence, and any omission, reorder, duplicate or extra fails BEFORE output.

    $Recompute is a scriptblock: param($fromIndex, $toIndex, $mode, $limit,
    $emitKey, $emitTs) -> ordered array of row ids the plan would have produced.
  #>
  param($State, $Rows, [string]$BoardId, [scriptblock]$Recompute)
  if (-not (Test-HasOutbox -State $State)) { return @{ ok = $true; reason = '' } }
  if (-not $BoardId) { return @{ ok = $false; reason = 'board read carried no identity' } }
  if ($State.boardId -ne $BoardId) {
    return @{ ok = $false; reason = ('retained outbox belongs to board ' + $State.boardId + ' but this is ' + $BoardId) }
  }
  $lastData = @($Rows).Count - 1
  $pi = [int]$State.pendingIndex
  if ($pi -lt 1 -or $pi -gt $lastData) {
    return @{ ok = $false; reason = ('retained outbox points at row ' + $pi + ', outside the ' + $lastData + ' rows now present') }
  }
  if (([string]@($Rows)[$pi][0]) -ne $State.pendingAnchor) {
    return @{ ok = $false; reason = ('retained outbox anchor moved at row ' + $pi) }
  }
  $expect = @(& $Recompute ([int]$State.planFrom) $pi ([string]$State.planMode) ([int]$State.planLimit) ([string]$State.planEmitKey) ([string]$State.planEmitTs))
  $have = @($State.pendingRowIds)
  if ($expect.Count -ne $have.Count) {
    return @{ ok = $false; reason = ('retained outbox holds ' + $have.Count + ' rows but the board now yields ' + $expect.Count + ' for the same plan') }
  }
  for ($i = 0; $i -lt $expect.Count; $i++) {
    if ([string]$expect[$i] -ne [string]$have[$i]) {
      return @{ ok = $false; reason = ('retained outbox differs from the recomputed plan at position ' + $i + ': expected ' + $expect[$i] + ', found ' + $have[$i]) }
    }
  }
  return @{ ok = $true; reason = '' }
}

function Set-WatchOutbox {
  <#
    Stage a bounded CHUNK and return a structured object ONLY.

    This function writes nothing and commits nothing. The previous version wrote
    the staged lines to the success stream AND returned a count, so the caller's
    assignment captured both and NOTHING reached stdout: the lines were staged,
    swallowed, and then cleared by the commit. A PowerShell function has one
    output stream and it may not be used for two purposes.

    $Plan is ordered @{ line; rowIndex; rowId }.
  #>
  param([Parameter(Mandatory = $true)]$State, $Rows, $Plan, [int]$FallbackIndex,
        [string]$Mode = 'steady', [int]$Limit = 0, [int]$From = 0,
        [string]$PlanEmitKey = '', [string]$PlanEmitTs = '')
  $plan = @($Plan)
  $take = [Math]::Min($plan.Count, $script:WATCH_OUTBOX_MAX)
  $chunk = @()
  if ($take -gt 0) { $chunk = $plan[0..($take - 1)] }

  $commitIndex = $FallbackIndex
  if ($take -lt $plan.Count) {
    $lastStagedRow = 0
    foreach ($e in $chunk) { if ([int]$e.rowIndex -gt $lastStagedRow) { $lastStagedRow = [int]$e.rowIndex } }
    if ([int]$plan[$take].rowIndex -eq $lastStagedRow) { $lastStagedRow = $lastStagedRow - 1 }
    $commitIndex = $lastStagedRow
  }

  $lines = @(); $ids = @()
  foreach ($e in $chunk) { $lines += [string]$e.line; $ids += [string]$e.rowId }

  $State.pendingLines = $lines
  $State.pendingRowIds = $ids
  if ($lines.Count -gt 0) {
    $State.pendingIndex = $commitIndex
    if ($Rows -and $commitIndex -ge 1 -and $commitIndex -lt @($Rows).Count) { $State.pendingAnchor = [string]@($Rows)[$commitIndex][0] }
    $State.planFrom = $(if ($From -ge 1) { $From } else { 1 })
    $State.planMode = $Mode
    $State.planLimit = $Limit
    $State.planEmitKey = $PlanEmitKey
    $State.planEmitTs = Format-WatchTime $PlanEmitTs
  } else {
    $State.pendingIndex = 0; $State.pendingAnchor = ''
    $State.planFrom = 0; $State.planMode = ''; $State.planLimit = 0
    $State.planEmitKey = ''; $State.planEmitTs = ''
  }
  return @{ state = $State; staged = $chunk; lines = $lines; remaining = ($plan.Count - $take) }
}

function Complete-WatchOutbox {
  <# May not touch identity: it once took a -BoardId and assigned it, so a replay
     from another board rewrote the stored id and disarmed the check meant to
     refuse it. #>
  param([Parameter(Mandatory = $true)]$State, [string]$EmitKey = $null, [string]$EmitTs = $null)
  if ($State.pendingIndex -ge 1) {
    $State.lastIndex = $State.pendingIndex
    if ($State.pendingAnchor) { $State.anchorId = $State.pendingAnchor }
  }
  # The dedupe marker advances HERE, with the chunk, and only to the last row
  # actually staged. Advancing it while PLANNING left the marker describing a row
  # that was never staged, so the next scan dropped a real message against it.
  if ($null -ne $EmitKey) { $State.lastEmitKey = $EmitKey; $State.lastEmitTs = Format-WatchTime $EmitTs }
  $State.pendingIndex = 0; $State.pendingAnchor = ''
  $State.pendingLines = @(); $State.pendingRowIds = @()
  $State.planFrom = 0; $State.planMode = ''; $State.planLimit = 0
  $State.planEmitKey = ''; $State.planEmitTs = ''
  return $State
}

function Set-CommittedCursor {
  # A transition with NO external effect: nothing was produced, so nothing needs
  # staging and the cursor commits directly.
  param([Parameter(Mandatory = $true)]$State, $Rows, [int]$Index)
  if ($Index -ge 1 -and $Rows -and $Index -lt @($Rows).Count) {
    $State.lastIndex = $Index
    $State.anchorId = [string]@($Rows)[$Index][0]
  }
  return $State
}

function Test-KeyIsDuplicate {
  # PURE. Takes the marker as arguments rather than reading state, so planning
  # can evaluate a prospective marker without mutating anything.
  param([string]$PrevKey, [string]$PrevTs, [string]$Key, [string]$RowTs)
  if (-not $PrevKey -or $PrevKey -ne $Key) { return $false }
  $a = ConvertTo-WatchTime $PrevTs; $b = ConvertTo-WatchTime $RowTs
  if (-not $a -or -not $b) { return $false }
  return ([math]::Abs(($b - $a).TotalSeconds) -lt $script:WATCH_DUPE_SECONDS)
}

function Test-RowIsDuplicate {
  param($State, [string]$Key, [string]$RowTs)
  if (-not $State) { return $false }
  return (Test-KeyIsDuplicate -PrevKey $State.lastEmitKey -PrevTs $State.lastEmitTs -Key $Key -RowTs $RowTs)
}

function Set-WatchEmitted {
  param([Parameter(Mandatory = $true)]$State, [string]$Key, [string]$RowTs)
  $State.lastEmitKey = $Key
  $State.lastEmitTs = Format-WatchTime $RowTs
  return $State
}
