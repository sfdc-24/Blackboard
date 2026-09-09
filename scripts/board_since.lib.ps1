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

  Where the anchor cannot be found the fallback is INCLUSIVE — it may show a row
  twice. Repeating a row costs a reader one duplicate; skipping one loses a
  message that no later run will ever surface again.
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
    [Parameter(Mandatory = $true)][string]$Tag
  )
  $want = $Tag.Trim().ToLowerInvariant()
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
      StartIndex = (Get-InclusiveTsStart -Rows $Rows -LastTs $lastTs)
      Anchor     = 'anchor-lost'
      Note       = ("cursor row {0} is no longer on the board -- falling back to an INCLUSIVE timestamp scan; rows may repeat, none are skipped" -f $lastRowId)
    }
  }

  return @{
    StartIndex = (Get-InclusiveTsStart -Rows $Rows -LastTs $lastTs)
    Anchor     = 'legacy-ts'
    Note       = 'cursor predates Row_ID anchoring -- inclusive scan this once'
  }
}

<#
First index whose timestamp is >= LastTs.

INCLUSIVE on purpose. The old code used `-le` to skip, which discarded every
row sharing the newest timestamp. Re-showing one row is a cosmetic cost; the
alternative silently loses messages.
#>
function Get-InclusiveTsStart {
  param(
    [object[]]$Rows,
    [string]$LastTs
  )
  if (-not $LastTs) { return 0 }
  $count = 0
  if ($Rows) { $count = $Rows.Count }
  for ($i = 0; $i -lt $count; $i++) {
    if (([string]$Rows[$i][1]) -ge $LastTs) { return $i }
  }
  return $count
}

<# The cursor to persist after a run: index AND the Row_ID that proves it. #>
function New-BoardCursor {
  param([object[]]$Rows)
  $count = 0
  if ($Rows) { $count = $Rows.Count }
  if ($count -eq 0) {
    return @{ lastIndex = -1; lastRowId = ''; lastTs = ''; updated = (Get-Date).ToUniversalTime().ToString('o') }
  }
  $last = $Rows[$count - 1]
  return @{
    lastIndex = $count - 1
    lastRowId = [string]$last[0]
    lastTs    = [string]$last[1]
    updated   = (Get-Date).ToUniversalTime().ToString('o')
  }
}
