#Requires -Version 5.1
[CmdletBinding()]
param()

<#
Fixtures for the two defects an exact-head review found in scripts/board_since.ps1.

Neither was hypothetical. Substring addressing was reproduced against three real
tag pairs, and the duplicate-timestamp loss was measured on a 1,648-row board
snapshot that contained 11 exact duplicate-timestamp groups, including groups of
three and four.

These tests call the selection logic directly. No bus, no network, no board, no
credentials, and nothing here writes a cursor file.
#>

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $script:RepoRoot (Join-Path 'scripts' 'board_since.lib.ps1'))

$script:Passed = 0
$script:Failed = 0

function Assert-True {
  param([string]$Name, [bool]$Condition, [string]$Detail = '')
  if ($Condition) {
    $script:Passed++
    Write-Output ('PASS ' + $Name)
  } else {
    $script:Failed++
    Write-Output ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
  }
}

function New-Row {
  param([string]$RowId, [string]$Ts, [string]$Writer, [string]$Payload)
  return , @($RowId, $Ts, $Writer, 'Blackboard Alpha DB', 'APPEND', $Payload, 'OPEN', 'ID', 'gist', 'sub')
}

# ── Addressing: exact tokens, never substrings ───────────────────────────────

Assert-True 'a prefix tag does not consume another instance mail' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=chatgpt-codex-desktop|from=x' -Tag 'codex')
) 'codex must not match chatgpt-codex-desktop'

Assert-True 'a suffix tag does not consume another instance mail' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=vm-claude-code-cli|from=x' -Tag 'claude-code-cli')
) 'claude-code-cli must not match vm-claude-code-cli'

Assert-True 'the reverse collision is also refused' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=claude-code-cli|from=x' -Tag 'vm-claude-code-cli')
)

Assert-True 'an exact recipient still matches' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=claude-code-cli|from=x' -Tag 'claude-code-cli'
)

Assert-True 'a comma list matches on any exact member' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=vm-chrome, claude-code-cli|from=x' -Tag 'claude-code-cli'
)

Assert-True 'case does not matter' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=Claude-Code-CLI|from=x' -Tag 'claude-code-cli'
)

Assert-True 'a broadcast reaches everyone' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=someone-else|cc=ALL|from=x' -Tag 'anyone'
)

Assert-True 'cc is honoured as well as to' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=someone|cc=claude-code-cli|from=x' -Tag 'claude-code-cli'
)

Assert-True 'a row addressed to nobody relevant is skipped' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=vm-chrome|from=x' -Tag 'claude-code-cli')
)

Assert-True 'ALLOCATE is not the ALL broadcast' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=ALLOCATE|from=x' -Tag 'claude-code-cli')
) 'the old \bALL\b regex was word-bounded, but token equality is what is meant'

# ── Addressing also lives in the Target_Surface COLUMN ───────────────────────
#
# Cell 3 is addressing too. A row whose recipients live only there -- a
# plain-prose payload carrying no to=/cc= tokens -- was invisible to every
# reader using this function.
#
# Measured on 2026-09-16: the unattended waker reported "3 rows addressed to
# claude-code-cli" and silently omitted index 2509,
# CODEX-01A09BF0-PROTOTYPE-CONTRACT-REVIEW-20260916, whose Target_Surface names
# claude-code-cli FIRST. It was the only row in that batch actually asking this
# lane for something, and it was found by reading rows by hand.
#
# The column is semicolon-delimited, so these cases also pin that the SAME exact
# -token rule applies there. Widening addressing is precisely where a
# mis-delivery bug would enter, so every collision case above is repeated here
# in column form.

$prose = 'Claude-code-cli this is first lead for sfdc24.com - see description below.'

Assert-True 'a prose row addressed only by the column is visible' (
  Test-BoardAddressed -Payload $prose -Tag 'claude-code-cli' -TargetSurface 'claude-code-cli;vm-claude-code-cli;ALL'
) 'the payload has no to= at all; the column is the only addressing'

Assert-True 'a semicolon list matches on any exact member' (
  Test-BoardAddressed -Payload $prose -Tag 'chat-mobile' -TargetSurface 'claude-code-cli;chat-mobile'
)

Assert-True 'a suffix tag does not consume column mail' (
  -not (Test-BoardAddressed -Payload $prose -Tag 'claude-code-cli' -TargetSurface 'vm-claude-code-cli')
) 'claude-code-cli must not match vm-claude-code-cli in the column either'

Assert-True 'the reverse column collision is also refused' (
  -not (Test-BoardAddressed -Payload $prose -Tag 'vm-claude-code-cli' -TargetSurface 'claude-code-cli')
)

Assert-True 'a prefix tag does not consume column mail' (
  -not (Test-BoardAddressed -Payload $prose -Tag 'codex' -TargetSurface 'chatgpt-codex-desktop')
)

Assert-True 'ALL in the column broadcasts' (
  Test-BoardAddressed -Payload $prose -Tag 'anyone' -TargetSurface 'someone-else;ALL'
)

Assert-True 'ALLOCATE in the column is not the ALL broadcast' (
  -not (Test-BoardAddressed -Payload $prose -Tag 'claude-code-cli' -TargetSurface 'ALLOCATE')
) 'token equality in the column too, not a word-bounded ALL'

Assert-True 'a column naming nobody relevant is still skipped' (
  -not (Test-BoardAddressed -Payload $prose -Tag 'claude-code-cli' -TargetSurface 'vm-chrome;glasses-uploader')
)

Assert-True 'an absent column changes nothing for payload addressing' (
  Test-BoardAddressed -Payload 'BCB|v=1|to=claude-code-cli|from=x' -Tag 'claude-code-cli' -TargetSurface ''
) 'the twelve payload-only assertions above must keep holding'

Assert-True 'a column that names nobody does not rescue an unaddressed payload' (
  -not (Test-BoardAddressed -Payload 'BCB|v=1|to=vm-chrome|from=x' -Tag 'claude-code-cli' -TargetSurface '')
)

# ── The payload comes from the fixed cell, never the longest one ─────────────

# A real row: short BCB payload in cell 5, a long human gist in cell 8. The old
# "first cell over 100 chars" heuristic picked the gist, so addressing was read
# from prose that contains no to= at all -- and the cursor still advanced.
$longGist = 'x' * 250
$shortPayloadRow = @(
  'r-short', '2026-09-09T07:00:00Z', 'other', 'Blackboard Alpha DB', 'APPEND',
  'BCB|v=1|to=claude-code-cli|id=SHORT', 'OPEN', 'ID', $longGist, 'sub'
)

Assert-True 'the payload comes from cell 5, not the longest cell' (
  (Get-BoardRowPayload -Row $shortPayloadRow) -eq 'BCB|v=1|to=claude-code-cli|id=SHORT'
) ("got: " + (Get-BoardRowPayload -Row $shortPayloadRow))

Assert-True 'a row whose gist outweighs its payload is still delivered' (
  Test-BoardAddressed -Payload (Get-BoardRowPayload -Row $shortPayloadRow) -Tag 'claude-code-cli'
) 'this row was invisible to its recipient under the length heuristic'

Assert-True 'the old heuristic really did pick the wrong cell' (
  ([string](($shortPayloadRow | Where-Object { ([string]$_).Length -gt 100 } | Select-Object -First 1))) -eq $longGist
) 'documents the defect: the first cell over 100 chars is the gist, not the payload'

Assert-True 'a short row without a payload cell yields nothing rather than throwing' (
  (Get-BoardRowPayload -Row @('a', 'b')) -eq ''
)

# ── Cursor: anchored by Row_ID, and ties are never dropped ───────────────────

# Three rows share the newest timestamp. The old timestamp cursor stored that
# value and then skipped everything `-le` it, so rows B and C could never be
# seen again by any later run.
$tied = @(
  (New-Row -RowId 'r1' -Ts '2026-09-09T05:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=A'),
  (New-Row -RowId 'r2' -Ts '2026-09-09T06:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=B'),
  (New-Row -RowId 'r3' -Ts '2026-09-09T06:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=C'),
  (New-Row -RowId 'r4' -Ts '2026-09-09T06:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=D')
)

$afterFirst = New-BoardCursor -Rows @($tied[0], $tied[1])
Assert-True 'the cursor records an index and the Row_ID that proves it' (
  $afterFirst.lastIndex -eq 1 -and $afterFirst.lastRowId -eq 'r2'
) ("got index=" + $afterFirst.lastIndex + " rowId=" + $afterFirst.lastRowId)

$resume = Resolve-BoardStartIndex -Rows $tied -Cursor ([pscustomobject]$afterFirst)
Assert-True 'resuming after a tied timestamp starts at the next row, not past the tie' (
  $resume.Anchor -eq 'index' -and $resume.StartIndex -eq 2
) ("anchor=" + $resume.Anchor + " start=" + $resume.StartIndex)

$remaining = @()
for ($i = [int]$resume.StartIndex; $i -lt $tied.Count; $i++) { $remaining += [string]$tied[$i][0] }
Assert-True 'the rows sharing the newest timestamp are still delivered' (
  ($remaining -join ',') -eq 'r3,r4'
) ("got " + ($remaining -join ','))

# The old behaviour, asserted directly so the regression is legible: a
# timestamp cursor at 06:00 with `-le` would have yielded nothing at all.
$tsOnlySkipped = @($tied | Where-Object { ([string]$_[1]) -le '2026-09-09T06:00:00Z' }).Count
Assert-True 'the timestamp-only cursor really did drop the tie' (
  $tsOnlySkipped -eq 4
) 'all four rows are -le the newest timestamp, which is why every tie vanished'

# Rows inserted above the anchor: the anchor must be found by search.
# Built through a list on purpose. `@($row) + $rows` splats the leading row into
# its ten cells, which produced a 12-element "board" of bare strings and a
# start index of 12 the first time this fixture was written.
$shiftedList = New-Object System.Collections.ArrayList
[void]$shiftedList.Add(@('r0', '2026-09-09T04:00:00Z', 'other', 'Blackboard Alpha DB', 'APPEND', 'BCB|v=1|to=x', 'OPEN', 'ID', 'gist', 'sub'))
foreach ($row in $tied) { [void]$shiftedList.Add($row) }
$shifted = $shiftedList.ToArray()
Assert-True 'the shifted fixture is a board of rows, not a bag of cells' (
  $shifted.Count -eq 5 -and $shifted[0].Count -eq 10
) ("count=" + $shifted.Count)
$searched = Resolve-BoardStartIndex -Rows $shifted -Cursor ([pscustomobject]$afterFirst)
Assert-True 'an anchor that moved is found by Row_ID rather than lost' (
  $searched.Anchor -eq 'searched' -and $searched.StartIndex -eq 3
) ("anchor=" + $searched.Anchor + " start=" + $searched.StartIndex)

# Anchor gone entirely: RESTART FROM THE TOP. Repeating is acceptable; skipping
# is the failure this whole change exists to prevent.
$lostCursor = [pscustomobject]@{ lastIndex = 1; lastRowId = 'vanished'; lastTs = '2026-09-09T06:00:00Z' }
$lost = Resolve-BoardStartIndex -Rows $tied -Cursor $lostCursor
Assert-True 'a lost anchor restarts from the top' (
  $lost.Anchor -eq 'anchor-lost' -and $lost.StartIndex -eq 0
) ("anchor=" + $lost.Anchor + " start=" + $lost.StartIndex)

Assert-True 'a lost anchor says so, rather than failing quietly' (
  $lost.Note -match 'RESTARTING FROM THE TOP'
)

# A cursor written by the previous version has no Row_ID at all.
$legacy = Resolve-BoardStartIndex -Rows $tied -Cursor ([pscustomobject]@{ lastTs = '2026-09-09T06:00:00Z' })
Assert-True 'a legacy timestamp cursor reads from the top' (
  $legacy.Anchor -eq 'legacy-ts' -and $legacy.StartIndex -eq 0
) ("anchor=" + $legacy.Anchor + " start=" + $legacy.StartIndex)

# ── The board is NOT sorted by timestamp, and the fallback must not assume it ──
#
# apps-script/blackboard-bus-v1/Code.gs:154-155 appends the CALLER's row
# verbatim:
#
#     const row = body.sheetRow || [nowStamp_(), body.text || ''];
#     sheet.appendRow(row);
#
# so the timestamp cell is whatever the writer put there -- a slow clock, a
# replayed row, a backfill. The old fallback scanned for the first row with
# `ts >= lastTs` and returned the row COUNT when it found none: past the end,
# skipping every row, permanently. Exactly what the file's own header promised
# could never happen.
#
# This board is the shape that triggers it: the anchor is gone and EVERY row
# appended since carries a timestamp older than the cursor's.
$unsorted = @(
  (New-Row -RowId 'u1' -Ts '2026-09-09T01:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=U1'),
  (New-Row -RowId 'u2' -Ts '2026-09-09T02:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=U2'),
  (New-Row -RowId 'u3' -Ts '2026-09-09T03:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=U3')
)
$staleTsCursor = [pscustomobject]@{ lastIndex = 7; lastRowId = 'trimmed-away'; lastTs = '2026-09-09T23:59:00Z' }
$unsortedStart = Resolve-BoardStartIndex -Rows $unsorted -Cursor $staleTsCursor
Assert-True 'rows appended out of timestamp order after an anchor loss are NOT skipped' (
  $unsortedStart.StartIndex -eq 0
) ("start=" + $unsortedStart.StartIndex + " of " + $unsorted.Count + " -- a start at the row count means every row is skipped, forever")

# Stated as the guarantee rather than as an index, so the assertion survives a
# change of strategy: whatever the fallback does, no addressed row may be lost.
$addressedSeen = 0
for ($i = [int]$unsortedStart.StartIndex; $i -lt $unsorted.Count; $i++) {
  if (Test-BoardAddressed -Payload (Get-BoardRowPayload -Row $unsorted[$i]) -Tag 'claude-code-cli') { $addressedSeen++ }
}
Assert-True 'every addressed row on that board is still reachable' (
  $addressedSeen -eq 3
) ("saw " + $addressedSeen + " of 3 addressed rows")

$firstRun = Resolve-BoardStartIndex -Rows $tied -Cursor $null
Assert-True 'a first run reads from the top' (
  $firstRun.Anchor -eq 'first-run' -and $firstRun.StartIndex -eq 0
)

$empty = New-BoardCursor -Rows @()
Assert-True 'an empty board yields a cursor that cannot skip anything' (
  $empty.lastIndex -eq -1 -and $empty.lastRowId -eq ''
)

# ── The cursor belongs to ONE board ─────────────────────────────────────────

# Keyed only by tag, a run against a second sheet or a second bus inherited the
# first board's anchor. If that Row_ID is absent from the second board the
# anchor is lost, and the inclusive timestamp fallback then starts PAST THE END
# of a board whose rows are all older -- skipping every one of them, forever.

$keyDefault  = Get-BoardSourceKey -Title 'Blackboard - Alpha DB' -Bus 'https://script.google.com/a/exec' -BusSource 'bus-url'
$keyOther    = Get-BoardSourceKey -Title 'Blackboard - Beta DB'  -Bus 'https://script.google.com/a/exec' -BusSource 'bus-url'
$keyOtherEnv = Get-BoardSourceKey -Title 'Blackboard - Alpha DB' -Bus 'https://script.google.com/b/exec' -BusSource 'bus-url'

Assert-True 'the same board yields a stable key' (
  $keyDefault -eq (Get-BoardSourceKey -Title 'Blackboard - Alpha DB' -Bus 'https://script.google.com/a/exec' -BusSource 'bus-url')
)
Assert-True 'a different sheet is a different board' ($keyDefault -ne $keyOther)
Assert-True 'a different bus is a different board' ($keyDefault -ne $keyOtherEnv)

# ── The key is the BUS, not the path to the file that names it ──────────────
#
# THE DEFECT: the key hashed the env-file PATHNAME. Redeploy the Apps Script --
# which mints a new /exec URL every single time -- and .env has not moved by one
# character while the board underneath it has been replaced. The old cursor is
# then accepted for a board that has never seen its Row_ID, and the inclusive
# fallback starts past the end of it.

$tmpEnvDir = Join-Path ([IO.Path]::GetTempPath()) ('board-key-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpEnvDir -Force | Out-Null
try {
  $envA = Join-Path $tmpEnvDir '.env'
  Set-Content -LiteralPath $envA -Encoding utf8 -Value @(
    '# comment line',
    'BUS_URL=https://script.google.com/macros/s/AAA/exec',
    'BUS_SECRET=not-a-real-secret'
  )
  $idA = Get-BoardBusIdentity -EnvPath $envA
  Assert-True 'the bus identity is read out of the env file' (
    $idA.Source -eq 'bus-url' -and $idA.Value -eq 'https://script.google.com/macros/s/AAA/exec'
  ) ("source=" + $idA.Source)

  $keyBeforeRedeploy = Get-BoardSourceKey -Title 'Blackboard - Alpha DB' -Bus $idA.Value -BusSource $idA.Source

  # SAME PATH. New deployment. This is the ordinary case, not an exotic one.
  Set-Content -LiteralPath $envA -Encoding utf8 -Value @(
    'BUS_URL=https://script.google.com/macros/s/BBB/exec',
    'BUS_SECRET=not-a-real-secret'
  )
  $idB = Get-BoardBusIdentity -EnvPath $envA
  $keyAfterRedeploy = Get-BoardSourceKey -Title 'Blackboard - Alpha DB' -Bus $idB.Value -BusSource $idB.Source

  Assert-True 'a redeployed bus at the SAME env path is a different board' (
    $keyBeforeRedeploy -ne $keyAfterRedeploy
  ) 'the cursor key must follow BUS_URL, not the pathname'

  Assert-True 'the stale cursor is therefore refused, not adopted' (
    -not (Test-BoardCursorMatches -Cursor ([pscustomobject]@{ lastRowId = 'x'; source = $keyBeforeRedeploy }) -SourceKey $keyAfterRedeploy)
  )

  # Quoted values are the other .env dialect bus.ps1 accepts.
  Set-Content -LiteralPath $envA -Encoding utf8 -Value @('BUS_URL="https://script.google.com/macros/s/BBB/exec"')
  Assert-True 'a quoted BUS_URL resolves to the same identity as an unquoted one' (
    (Get-BoardBusIdentity -EnvPath $envA).Value -eq $idB.Value
  )

  # ── The identity must be the bus bus.ps1 ACTUALLY CONNECTS TO ──────────────
  #
  # bus.ps1:154-158 folds every assignment into a hashtable, so the LAST
  # BUS_URL wins. Reading the FIRST one pins the key to a superseded line: edit
  # the active URL -- the only line that changes which board you are reading --
  # and the key does not move, so a cursor from the old board is accepted for
  # the new one and skips every row on it.
  #
  # This asserts AGREEMENT WITH bus.ps1, not "returns the last one". The same
  # file is parsed here by bus.ps1's own algorithm, so if either side's
  # semantics drift later this fails, which is the property that actually
  # matters. Stating it as "the last value" would be a rule that happens to
  # match today.
  $dupEnv = Join-Path $tmpEnvDir 'dup.env'
  Set-Content -LiteralPath $dupEnv -Encoding utf8 -Value @(
    'BUS_URL=https://script.google.com/macros/s/SUPERSEDED/exec',
    'BUS_SECRET=x',
    'BUS_URL=https://script.google.com/macros/s/ACTIVE/exec'
  )

  # bus.ps1:154-158, verbatim in shape.
  $busCfg = @{}
  foreach ($line in (Get-Content -LiteralPath $dupEnv)) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
      $busCfg[$matches[1]] = $matches[2].Trim('"').Trim("'")
    }
  }
  $idDup = Get-BoardBusIdentity -EnvPath $dupEnv
  Assert-True 'the identity is the bus bus.ps1 would actually connect to' (
    $idDup.Value -eq $busCfg.BUS_URL
  ) ("identity=" + $idDup.Value + " bus.ps1=" + $busCfg.BUS_URL)

  # And the consequence, stated directly: change the ACTIVE line and the key
  # must move. Under the first-match bug it would not, because the first line
  # is untouched.
  $keyActiveA = Get-BoardSourceKey -Title 'T' -Bus $idDup.Value -BusSource $idDup.Source
  Set-Content -LiteralPath $dupEnv -Encoding utf8 -Value @(
    'BUS_URL=https://script.google.com/macros/s/SUPERSEDED/exec',
    'BUS_SECRET=x',
    'BUS_URL=https://script.google.com/macros/s/ACTIVE-2/exec'
  )
  $idDup2 = Get-BoardBusIdentity -EnvPath $dupEnv
  $keyActiveB = Get-BoardSourceKey -Title 'T' -Bus $idDup2.Value -BusSource $idDup2.Source
  Assert-True 'changing the ACTIVE BUS_URL moves the cursor key' (
    $keyActiveA -ne $keyActiveB
  ) 'a superseded first line must not pin the key while the real bus changes'

  # A BOM on the first line -- what Set-Content -Encoding utf8 writes on Windows
  # PowerShell 5.1, and what Notepad writes -- must not hide BUS_URL, because
  # `^\s*BUS_URL` does not match a BOM and a miss here falls silently back to
  # the pathname key this whole function exists to stop using.
  #
  # NO GUARD IN THE LIBRARY BACKS THIS. One was written and then removed: it
  # could not be made to fail. Get-Content detects the BOM and strips it before
  # the regex ever sees the line, so the strip was inert -- a green check
  # standing on nothing, which is the exact shape of defect this suite exists
  # to catch. The assertion stays because the PROPERTY matters and this is the
  # only place it is stated; it does not care who satisfies it. If some reader
  # on some host stops stripping, this goes red and the guard earns its place
  # then, with a mutation that fails.
  [IO.File]::WriteAllText($envA, "BUS_URL=https://script.google.com/macros/s/CCC/exec`nBUS_SECRET=x", (New-Object Text.UTF8Encoding $true))
  Assert-True 'a BOM on the first line does not hide BUS_URL' (
    (Get-BoardBusIdentity -EnvPath $envA).Value -eq 'https://script.google.com/macros/s/CCC/exec'
  ) ("got source=" + (Get-BoardBusIdentity -EnvPath $envA).Source)

  # An unreadable env file must NOT collapse to one shared key.
  $missing = Join-Path $tmpEnvDir 'nope.env'
  $idMissing = Get-BoardBusIdentity -EnvPath $missing
  Assert-True 'an unreadable env file falls back to its own path, not a shared token' (
    $idMissing.Source -eq 'env-path' -and $idMissing.Value -eq $missing
  ) ("source=" + $idMissing.Source)
  Assert-True 'and that fallback still separates two different paths' (
    (Get-BoardSourceKey -Title 'T' -Bus $missing -BusSource 'env-path') -ne
    (Get-BoardSourceKey -Title 'T' -Bus (Join-Path $tmpEnvDir 'other.env') -BusSource 'env-path')
  )
  # The tag keeps the two spaces apart, so a path that reads like a URL is
  # still not that URL's board.
  Assert-True 'a path and a URL with the same text are different boards' (
    (Get-BoardSourceKey -Title 'T' -Bus 'https://x/exec' -BusSource 'env-path') -ne
    (Get-BoardSourceKey -Title 'T' -Bus 'https://x/exec' -BusSource 'bus-url')
  )
} finally {
  Remove-Item -LiteralPath $tmpEnvDir -Recurse -Force -ErrorAction SilentlyContinue
}

# ── Two bus.ps1 candidates imply two DIFFERENT default .env files ───────────
#
# board_since.ps1 picks bus.ps1 from -BusScript, $env:SFDC24_BUS_PS1, its own
# directory, or the repo root. Every one of those used to record the literal
# string '<default>', so two checkouts pointing at two buses shared one cursor.
$envFromA = Resolve-BoardEnvPath -BusScript (Join-Path ([IO.Path]::GetTempPath()) 'checkout-a/scripts/bus.ps1')
$envFromB = Resolve-BoardEnvPath -BusScript (Join-Path ([IO.Path]::GetTempPath()) 'checkout-b/scripts/bus.ps1')
Assert-True 'each bus script resolves to the .env beside its own repo root' (
  $envFromA -ne $envFromB -and $envFromA.EndsWith('.env') -and $envFromB.EndsWith('.env')
) ("a=" + $envFromA + " b=" + $envFromB)
Assert-True 'and the two therefore key to different boards' (
  (Get-BoardSourceKey -Title 'T' -Bus $envFromA -BusSource 'env-path') -ne
  (Get-BoardSourceKey -Title 'T' -Bus $envFromB -BusSource 'env-path')
)
Assert-True 'an explicit -EnvFile still wins over the bus script default' (
  (Resolve-BoardEnvPath -EnvFile (Join-Path ([IO.Path]::GetTempPath()) 'explicit.env') -BusScript $envFromA) -ne $envFromA
)

$foreign = [pscustomobject]@{ lastIndex = 3; lastRowId = 'not-on-this-board'; lastTs = '2027-01-01T00:00:00Z'; source = $keyOther }
Assert-True 'a cursor from another board is refused' (
  -not (Test-BoardCursorMatches -Cursor $foreign -SourceKey $keyDefault)
)
Assert-True 'a cursor with no source at all is refused' (
  -not (Test-BoardCursorMatches -Cursor ([pscustomobject]@{ lastRowId = 'x' }) -SourceKey $keyDefault)
)
Assert-True 'this board own cursor is accepted' (
  Test-BoardCursorMatches -Cursor ([pscustomobject]@{ lastRowId = 'x'; source = $keyDefault }) -SourceKey $keyDefault
)

# THE DAMAGE THIS ASSERTION USED TO DEMONSTRATE IS GONE, AND THE ASSERTION HAS
# BEEN RE-AIMED RATHER THAN RE-NUMBERED.
#
# It read `$adopted.StartIndex -eq $olderBoard.Count` -- adopting a foreign
# cursor starts past the end and skips everything. That was true only because
# the anchor-loss fallback scanned by timestamp. Now that the fallback restarts
# from the top, an adopted cursor whose Row_ID is ABSENT here is harmless, and
# the old assertion could only be kept by asserting a behaviour that no longer
# exists. Changing the expected number to 0 would have kept a green line whose
# name -- "would skip the whole board" -- had become false.
#
# What still bites is a Row_ID COLLISION: board identities are per-writer
# sequences, not globally unique, so the same id can exist on both boards. The
# anchor is then FOUND, at the wrong board's position, and every row up to it is
# consumed unseen. That is why the source key still has to exist.
$olderBoard = @(
  (New-Row -RowId 'b1' -Ts '2026-01-01T00:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=X'),
  (New-Row -RowId 'b2' -Ts '2026-01-02T00:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=Y'),
  (New-Row -RowId 'b3' -Ts '2026-01-03T00:00:00Z' -Writer 'other' -Payload 'BCB|v=1|to=claude-code-cli|id=Z')
)
$collided = [pscustomobject]@{ lastIndex = 9; lastRowId = 'b2'; lastTs = '2027-01-01T00:00:00Z'; source = $keyOther }
$adopted = Resolve-BoardStartIndex -Rows $olderBoard -Cursor $collided
Assert-True 'adopting a foreign cursor consumes rows it never showed' (
  $adopted.StartIndex -gt 0
) ("start=" + $adopted.StartIndex + " -- b1 and b2 are addressed, unread, and now behind the cursor")

# And the fallback's own guarantee, restated where it is easiest to regress:
# an absent anchor can no longer skip anything at all, on any board.
$absent = [pscustomobject]@{ lastIndex = 9; lastRowId = 'not-here'; lastTs = '2027-01-01T00:00:00Z' }
Assert-True 'an absent anchor never skips, whatever the timestamps say' (
  (Resolve-BoardStartIndex -Rows $olderBoard -Cursor $absent).StartIndex -eq 0
)

# Refused, it reads as a first run and nothing is lost.
$refused = Resolve-BoardStartIndex -Rows $olderBoard -Cursor $null
Assert-True 'refusing it reads from the top instead' (
  $refused.Anchor -eq 'first-run' -and $refused.StartIndex -eq 0
)

Assert-True 'a written cursor records the board it came from' (
  (New-BoardCursor -Rows $olderBoard -SourceKey $keyDefault).source -eq $keyDefault
)

Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
