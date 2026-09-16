#Requires -Version 5.1
<#
Selection and cursor logic for board_since.ps1, in functions so it can be tested.

WHY THIS IS A SEPARATE FILE
  The logic below decides which board rows an instance is allowed to never see
  again. Two defects in it were found by review rather than by use, because the
  script did its own reading, printing and cursor-writing in one pass and there
  was nothing a test could call. Now there is.

THE TWO DEFECTS THIS FILE EXISTS TO FIX

  1. SUBSTRING ADDRESSING. `to=` was matched with -match on an escaped tag, so
     `-Tag codex` accepted rows addressed `to=chatgpt-codex-desktop`, and
     `claude-code-cli` accepted `to=vm-claude-code-cli`. An instance was reading
     — and then skipping past — mail addressed to a different instance. That is
     worse than noise: the cursor advanced, so the real recipient's row was
     consumed by the wrong reader.

  2. A TIMESTAMP-ONLY CURSOR THAT DROPPED TIES. Rows at `ts -le lastTs` were
     skipped, and the board is not unique by timestamp: the 1,648-row snapshot
     reviewed on 2026-09-09 contained 11 exact duplicate-timestamp groups,
     including groups of three and four. Every row sharing the newest
     timestamp was silently and permanently skipped. The cursor is now anchored
     to a Row_ID at a known index, which is unique and stable.

  Where the anchor cannot be found, the scan RESTARTS FROM THE TOP. It may show
  many rows twice. Repeating a row costs a reader one duplicate; skipping one
  loses a message that no later run will ever surface again.

  3. A TIMESTAMP FALLBACK ON A BOARD THAT IS NOT SORTED BY TIMESTAMP. The
     anchor-loss recovery used to scan for the first row with `ts >= lastTs`,
     which silently assumes the board is in timestamp order. It is not:
     `apps-script/blackboard-bus-v1/Code.gs:154-155` is

         const row = body.sheetRow || [nowStamp_(), body.text || ''];
         sheet.appendRow(row);

     — the CALLER's row, timestamp cell and all, appended verbatim. A writer
     with a slow clock, a replayed row, or any backfill puts an older timestamp
     after a newer one. So rows appended after the anchor vanished could all
     carry timestamps below `lastTs`, the scan would find nothing at or after
     it, and it returned the row COUNT: past the end of the board, skipping
     every one of them, permanently — while the comment three lines above
     promised that could never happen.

     The fallback now returns 0. Index order is append order, which is the one
     ordering the board actually guarantees; timestamps are data the writer
     chose.
#>

Set-StrictMode -Version 2.0

<#
Split a BCB address field into exact tokens.
`to=claude-code-cli, vm-chrome` -> @('claude-code-cli','vm-chrome')
#>
function Get-BoardFieldTokens {
  param(
    [string]$Payload,
    [Parameter(Mandatory = $true)][string]$Field
  )
  if ([string]::IsNullOrEmpty($Payload)) { return @() }
  # BCB fields are pipe-delimited: field=value|next=...
  $pattern = '(?:^|\|)' + [regex]::Escape($Field) + '=([^|]*)'
  $m = [regex]::Match($Payload, $pattern)
  if (-not $m.Success) { return @() }
  $raw = $m.Groups[1].Value
  return @($raw -split '[,;\s]+' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

<#
The payload cell of a board row.

FIXED at index 5 by the board schema. board_since.ps1 previously chose "the
first cell longer than 100 characters", which picks a long gist or sub-gist
whenever the BCB payload itself is short -- so the row was then addressed from
the wrong cell, failed the addressing test, and the cursor advanced past it
anyway. That hides a row from its recipient permanently, which is the same
failure the exact-token addressing above exists to prevent.
#>
function Get-BoardRowPayload {
  param([object[]]$Row)
  if (-not $Row) { return '' }
  if ($Row.Count -le 5) { return '' }
  return [string]$Row[5]
}

<#
Is this payload addressed to $Tag?

EXACT token match, case-insensitive, plus the ALL broadcast. A tag that is a
prefix, suffix or substring of another tag is NOT a match — that was the bug.
#>
function Test-BoardAddressed {
  param(
    [string]$Payload,
    [Parameter(Mandatory = $true)][string]$Tag,
    # THE SHEET COLUMN IS ALSO ADDRESSING, AND IT WAS INVISIBLE.
    #
    #   This function only ever saw cell 5. A row whose recipients live in the
    #   Target_Surface COLUMN (cell 3) and whose payload is plain prose carries
    #   no to=/cc= tokens at all, so it was never addressed to anyone.
    #
    #   Measured on 2026-09-16: the unattended waker reported "3 rows addressed
    #   to claude-code-cli" and silently omitted index 2509,
    #   CODEX-01A09BF0-PROTOTYPE-CONTRACT-REVIEW-20260916, whose Target_Surface
    #   names claude-code-cli FIRST — the primary recipient, and the only row in
    #   that batch actually asking this lane for anything. It was found by
    #   reading rows by hand, not by the detector.
    #
    #   That is the bad kind of bug: it fails SILENT and it fails CONFIDENT. The
    #   run does not error, it reports a number, and the number looks like
    #   coverage.
    #
    #   Optional, not a changed contract: twelve existing tests call this with
    #   -Payload/-Tag only, and they encode the collision rules below.
    [string]$TargetSurface = ''
  )
  $want = $Tag.Trim().ToLowerInvariant()

  # EXACT tokens here too, for the same reason as the payload. The column is
  # semicolon-delimited (a;b;c) and Get-BoardFieldTokens already splits on
  # [,;\s]+, so the splitter is reused rather than re-derived — a second idea of
  # what a delimiter is, is how a reader and a writer come to disagree.
  # Substring matching would make vm-claude-code-cli consume claude-code-cli's
  # mail, which is the defect the payload path was already hardened against.
  if (-not [string]::IsNullOrWhiteSpace($TargetSurface)) {
    foreach ($token in ($TargetSurface -split '[,;\s]+')) {
      $t = $token.Trim().ToLowerInvariant()
      if (-not $t) { continue }
      if ($t -eq $want) { return $true }
      if ($t -eq 'all') { return $true }
    }
  }

  foreach ($field in @('to', 'cc')) {
    foreach ($token in (Get-BoardFieldTokens -Payload $Payload -Field $field)) {
      $t = $token.ToLowerInvariant()
      if ($t -eq $want) { return $true }
      if ($t -eq 'all') { return $true }
    }
  }
  return $false
}

<#
Where to start reading, given the stored cursor.

Returns a hashtable: @{ StartIndex = <int>; Anchor = <string>; Note = <string> }

  first-run    no cursor: everything addressed is new to this reader
  index        the anchor Row_ID was exactly where the cursor said it was
  searched     the anchor moved (rows inserted/removed above it) but was found
  anchor-lost  the anchor is gone — board replaced or trimmed. INCLUSIVE
               timestamp fallback, which may repeat rows but cannot skip them.
  legacy-ts    a cursor written by the pre-Row_ID version. Same inclusive
               fallback, once, until the next write upgrades it.
#>
function Resolve-BoardStartIndex {
  param(
    [object[]]$Rows,
    [object]$Cursor
  )
  $count = 0
  if ($Rows) { $count = $Rows.Count }

  if (-not $Cursor) {
    return @{ StartIndex = 0; Anchor = 'first-run'; Note = 'no cursor yet -- this is a first run' }
  }

  $lastRowId = ''
  if ($Cursor.PSObject.Properties.Match('lastRowId').Count -gt 0 -and $Cursor.lastRowId) {
    $lastRowId = [string]$Cursor.lastRowId
  }
  $lastTs = ''
  if ($Cursor.PSObject.Properties.Match('lastTs').Count -gt 0 -and $Cursor.lastTs) {
    $lastTs = [string]$Cursor.lastTs
  }
  $lastIndex = -1
  if ($Cursor.PSObject.Properties.Match('lastIndex').Count -gt 0 -and $null -ne $Cursor.lastIndex) {
    $lastIndex = [int]$Cursor.lastIndex
  }

  if ($lastRowId) {
    if ($lastIndex -ge 0 -and $lastIndex -lt $count -and ([string]$Rows[$lastIndex][0]) -eq $lastRowId) {
      return @{ StartIndex = $lastIndex + 1; Anchor = 'index'; Note = '' }
    }
    for ($i = 0; $i -lt $count; $i++) {
      if (([string]$Rows[$i][0]) -eq $lastRowId) {
        return @{ StartIndex = $i + 1; Anchor = 'searched'; Note = ("anchor row moved to index {0}" -f $i) }
      }
    }
    return @{
      StartIndex = 0
      Anchor     = 'anchor-lost'
      Note       = ("cursor row {0} is no longer on the board -- RESTARTING FROM THE TOP; rows may repeat, none are skipped" -f $lastRowId)
    }
  }

  return @{
    StartIndex = 0
    Anchor     = 'legacy-ts'
    Note       = 'cursor predates Row_ID anchoring -- reading from the top this once'
  }
}

<#
Get-InclusiveTsStart IS DELETED, NOT FIXED.

It answered "the first index whose timestamp is >= LastTs", and there is no
version of that question worth asking here. Any answer it gives is a claim
about POSITION derived from a value the writer supplies and the bus never
validates or sorts. Making it inclusive rather than exclusive fixed the tie
bug and left the assumption underneath it -- that later in the board means
later in time -- completely intact.

Its last caller now returns 0. Two functions with the same seductive shape are
how this defect keeps coming back, so the shape is gone rather than left
lying around for the next reader to reach for.
#>

<#
Which .env file will bus.ps1 actually read?

bus.ps1 line 150 defaults it to the .env one level ABOVE its own directory:

    if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }

board_since.ps1 chooses its bus.ps1 from four candidates (-BusScript, the
SFDC24_BUS_PS1 environment variable, beside itself, under the repo root), so
"the default env file" is not one path -- it is one path PER BUS SCRIPT. The
first version of the cursor key recorded the literal string '<default>' for all
of them, which is exactly the collision the key exists to prevent: two
checkouts, two buses, two boards, one cursor.
#>
function Resolve-BoardEnvPath {
  param([string]$EnvFile, [string]$BusScript)
  if ($EnvFile) {
    try { return [IO.Path]::GetFullPath($EnvFile) } catch { return $EnvFile }
  }
  if (-not $BusScript) { return '' }
  try {
    $busDir = Split-Path -Parent ([IO.Path]::GetFullPath($BusScript))
    if (-not $busDir) { return '' }
    return (Join-Path (Split-Path -Parent $busDir) '.env')
  } catch { return '' }
}

<#
The BUS the cursor was taken from, not the path to the file that names it.

WHY THE PATH IS THE WRONG THING TO HASH

  The env file is configuration; BUS_URL is the identity. Point the same
  .env at a different deployment -- a re-deployed Apps Script gets a NEW /exec
  URL every time, so this is the ordinary case, not an exotic one -- and the
  path has not changed by one character while the board underneath it has been
  replaced entirely. The cursor then carries a Row_ID from the old board, the
  anchor is lost on the new one, and the INCLUSIVE timestamp fallback starts
  from a timestamp newer than every row there is. Every addressed row on the new
  board is skipped, permanently, and the run reports "nothing new addressed to
  you" -- which is indistinguishable from a quiet board.

  Hashing the path checked the LABEL on the configuration instead of the claim
  it makes. That is the same mistake, in a fourth place.

RETURNS  @{ Value = <string>; Source = 'bus-url' | 'env-path' | 'unresolved' }

  The value is a hash INPUT and nothing else. It is never printed, logged or
  written: BUS_URL is a bearer-ish endpoint that lives only in .env (D-18), and
  the cursor filename carries six bytes of SHA-256, not the URL.

  Each Source is tagged into the hashed string so the spaces cannot collide --
  an env path that happens to read like a URL is still a different board from
  that URL. A file we cannot read falls back to its own path: that yields a
  DIFFERENT key from the readable case, so the cursor is treated as foreign and
  the run repeats rows. Repeating is the correct direction to fail; the whole
  point of this key is that skipping is not.
#>
function Get-BoardBusIdentity {
  param([string]$EnvPath)
  if (-not $EnvPath) { return @{ Value = ''; Source = 'unresolved' } }
  try {
    if (Test-Path -LiteralPath $EnvPath) {
      # THE WHOLE FILE, LAST VALUE WINS -- because that is what bus.ps1 does.
      #
      # bus.ps1:154-158 assigns every matching line into a hashtable:
      #
      #     $cfg = @{}
      #     foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
      #       if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
      #         $cfg[$matches[1]] = $matches[2].Trim('"').Trim("'")
      #
      # so a later BUS_URL overwrites an earlier one and THAT is the bus it
      # connects to. Returning on the first match read a superseded line: the
      # key would then be pinned to a URL nobody talks to, and editing the
      # active one -- the line that actually changes the board -- would move
      # the bus while leaving the key exactly where it was. A cursor from the
      # old board is then accepted for the new one and skips every row on it.
      #
      # Which is this defect for the third time: not the URL, but the WRONG
      # URL. Identity has to be read the way the thing being identified reads
      # it, or it is describing something else.
      $found = ''
      foreach ($line in (Get-Content -LiteralPath $EnvPath -ErrorAction Stop)) {
        if ($line -match '^\s*BUS_URL\s*=\s*(.*?)\s*$') {
          $found = $matches[1].Trim('"').Trim("'")
        }
      }
      if ($found) { return @{ Value = $found; Source = 'bus-url' } }
    }
  } catch {
    # Unreadable for any reason -- permissions, a locked file, a directory.
    # Fall through to the path, which is still better than one shared key.
  }
  return @{ Value = $EnvPath; Source = 'env-path' }
}

<#
A stable identity for the board a cursor was taken from.

WHY THE CURSOR CANNOT BE KEYED BY TAG ALONE

  -Title selects a different sheet and -EnvFile can select a different bus
  entirely, but the cursor file was named only for the tag. So a run against a
  second board reused the first board's cursor. If that saved Row_ID is not on
  the second board the anchor is lost, and the INCLUSIVE timestamp fallback then
  starts from the newest saved timestamp -- which, on a board whose rows are all
  older, is past the end. Every existing addressed row on that board is skipped,
  permanently.

  That is the same "permanently hides mail" failure as the substring addressing
  and the tied-timestamp cursor above, arriving by a third route: the cursor was
  right about a board nobody was reading.

  -Bus is what Get-BoardBusIdentity resolved: the BUS_URL where it could be
  read, the env path where it could not. See there for why the URL and not the
  path, and for why the two are tagged apart.
#>
function Get-BoardSourceKey {
  param(
    [string]$Title,
    [string]$Bus,
    [string]$BusSource = 'unresolved'
  )
  $raw = ("{0}|{1}|{2}" -f $Title, $BusSource, $Bus).ToLowerInvariant()
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($raw))
  } finally {
    $sha.Dispose()
  }
  return -join ($bytes[0..5] | ForEach-Object { $_.ToString('x2') })
}

<#
Does this cursor belong to the board we are about to read?

A mismatch is treated as NO cursor, never as a usable one. Showing rows twice
costs a reader a duplicate; adopting another board's cursor skips real messages.
#>
function Test-BoardCursorMatches {
  param([object]$Cursor, [string]$SourceKey)
  if (-not $Cursor) { return $false }
  if ($Cursor.PSObject.Properties.Match('source').Count -eq 0) { return $false }
  return ([string]$Cursor.source) -eq $SourceKey
}

<# The cursor to persist after a run: index AND the Row_ID that proves it. #>
function New-BoardCursor {
  param([object[]]$Rows, [string]$SourceKey = '')
  $count = 0
  if ($Rows) { $count = $Rows.Count }
  if ($count -eq 0) {
    return @{ lastIndex = -1; lastRowId = ''; lastTs = ''; source = $SourceKey; updated = (Get-Date).ToUniversalTime().ToString('o') }
  }
  $last = $Rows[$count - 1]
  return @{
    lastIndex = $count - 1
    lastRowId = [string]$last[0]
    lastTs    = [string]$last[1]
    source    = $SourceKey
    updated   = (Get-Date).ToUniversalTime().ToString('o')
  }
}
