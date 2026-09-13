<#
.SYNOPSIS
    Map every Claude browser-extension install on this machine to the deviceId
    the server knows it by, so "which browser is Browser 2?" has an answer.

.WHY THIS EXISTS
    list_connected_browsers returns entries like:

        {"deviceId":"f1315438-...","name":"Browser 1","isLocal":true}

    "Browser 1" is not an identity. When three of them are listed and one
    machine is running one Chrome, there is no way from that list to tell
    which entry can actually serve a tab - so a browser action gets sent to a
    registration that has no window behind it and appears to do nothing.

    The information needed to resolve it is sitting in each profile on disk.
    The extension stores its own `bridgeDeviceId` and a `displayName` in its
    LevelDB settings; the displayName is often meaningful ("Chrome Extension -
    VM") even when the server list shows "Browser N". This reads that, and
    pairs it with whether the browser is actually running.

.WHAT IT PROVES AND WHAT IT DOES NOT
    It proves which deviceIds belong to extension installs ON THIS MACHINE and
    which of those browsers currently has a live process. It says nothing about
    deviceIds registered from another machine - those simply will not appear,
    which is itself the answer for a `isLocal:true` entry that is missing here:
    that registration is stale.

.PARAMETER DeviceId
    Zero or more deviceIds from list_connected_browsers. Each is reported as:

      MATCHED    a complete id was read from a local profile
      MATCHED*   the stored value is broken up by LevelDB compression, and a
                 contiguous run of at least 16 characters identifies it; the
                 run length is printed so the evidence can be weighed
      AMBIGUOUS  more than one candidate cleared the floor in one profile, so
                 the evidence cannot distinguish them and none is reported
      ELSEWHERE  no profile ON THIS MACHINE holds it. That is NOT "stale" -
                 it may be a live browser on another machine

    Exit codes: 0 something matched, 1 nothing matched, 2 could not look
    (installs exist but no bridgeDeviceId key was seen anywhere). 2 is kept
    distinct from 1 because "could not look" and "looked and found nothing"
    call for different actions.

.EXAMPLE
    .\identify_claude_browsers.ps1
    .\identify_claude_browsers.ps1 -DeviceId f1315438-... , 295592ab-...
#>
[CmdletBinding()]
param(
    [string[]]$DeviceId = @(),

    # TESTABILITY, not a user-facing option.
    #
    # Without this the script can only ever be run against whatever browsers
    # this machine happens to have, which means the parsing - the part that has
    # already been wrong three times - is exercised by hand and never the same
    # way twice. With it, tests/test_identify_claude_browsers.ps1 points it at a
    # fixture tree and asserts on the values it pulls out.
    #
    # Production behaviour is unchanged when it is absent.
    [string]$ScanRoot,
    [string]$ScanName = 'Test'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# The Claude extension's stable ID in the Chrome Web Store.
$EXT_ID = 'fcoeoabgfenejglbffodgkkbkcdhcgfn'

# Chromium-family browsers that can host a Chrome extension. Firefox is absent
# deliberately - it cannot load this extension, so listing it would imply a
# place to look that does not exist.
$BROWSERS = @(
    @{ Name = 'Chrome';   Root = "$env:LOCALAPPDATA\Google\Chrome\User Data";          Process = 'chrome'   },
    @{ Name = 'Edge';     Root = "$env:LOCALAPPDATA\Microsoft\Edge\User Data";         Process = 'msedge'   },
    @{ Name = 'Brave';    Root = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data"; Process = 'brave' },
    @{ Name = 'Vivaldi';  Root = "$env:LOCALAPPDATA\Vivaldi\User Data";                Process = 'vivaldi'  },
    @{ Name = 'Opera';    Root = "$env:APPDATA\Opera Software\Opera Stable";           Process = 'opera'    },
    @{ Name = 'Chromium'; Root = "$env:LOCALAPPDATA\Chromium\User Data";               Process = 'chromium' }
)

# Read a file Chrome currently holds open. A plain Get-Content throws on the
# live .log and LOCK files, and swallowing that error would silently skip the
# newest data - which is exactly where a just-changed deviceId would be.
function Read-SharedText {
    param([string]$Path)
    try {
        $fs = New-Object System.IO.FileStream(
            $Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        # LATIN-1, NOT ASCII, AND NOT UTF-8.
        #
        # ASCII folds every byte above 0x7F to '?', which destroys a non-ASCII
        # display name outright. UTF-8 is worse for this job: the file is
        # binary, so invalid sequences become replacement characters and the
        # string indices stop corresponding to byte offsets - the very thing
        # the key-anchored window arithmetic depends on. Latin-1 is the only
        # encoding that maps all 256 byte values one-to-one, so the text is
        # byte-faithful and offsets stay true. The display name is decoded back
        # to UTF-8 from these bytes where it is read, not here.
        $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::GetEncoding(28591))
        $text = $sr.ReadToEnd()
        $sr.Close(); $fs.Close()
        return $text
    } catch {
        return $null
    }
}

# Pull "key" -> "value" out of the extension's LevelDB. The values are stored
# as JSON fragments between length-prefixed binary, so the key name appears
# immediately before its quoted value. Anchoring on the key matters: a bare
# UUID regex also matches anonymousId and would report the wrong identity.
function Get-SettingAfterKey {
    param([string]$Text, [string]$Key)
    if ($null -eq $Text) { return $null }
    $pattern = [regex]::Escape($Key) + '[^"]{0,24}"([^"]{1,120})"'
    $m = [regex]::Match($Text, $pattern)
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

# Every place the id has been seen or might plausibly live. Searching only one
# of these made "not found" a much weaker statement than it read as - the
# laptop's probe had to check the other three by hand to establish anything.
# The list is reported in the refusal so "not found" is always scoped to what
# was actually opened.
function Get-SettingsDirs {
    param([string]$ProfilePath)
    # Labelled explicitly rather than by Split-Path -Leaf: "Local Extension
    # Settings\<ext>" and "Sync Extension Settings\<ext>" share a leaf, so a
    # derived label printed the same extension id for both and the report could
    # not say which of them had been read.
    return @(
        @{ Label = 'Local Extension Settings'
           Path  = (Join-Path $ProfilePath "Local Extension Settings\$EXT_ID") },
        @{ Label = 'Sync Extension Settings'
           Path  = (Join-Path $ProfilePath "Sync Extension Settings\$EXT_ID") },
        @{ Label = 'IndexedDB (extension origin)'
           Path  = (Join-Path $ProfilePath "IndexedDB\chrome-extension_${EXT_ID}_0.indexeddb.leveldb") },
        # Shared across origins and full of unrelated UUIDs - 194 of them on the
        # laptop, including claude.ai's own web analytics `anonymous_id`. Safe to
        # include ONLY because extraction is anchored on the key name; a bare
        # UUID regex pointed here would return a confident wrong answer.
        @{ Label = 'Local Storage\leveldb (all origins)'
           Path  = (Join-Path $ProfilePath "Local Storage\leveldb") }
    )
}

# Is this specific id recorded here? Asked as a QUESTION rather than answered by
# extracting the value, because the stored value cannot be read back intact.
#
# LevelDB compresses its blocks, so framing bytes land INSIDE the value, not only
# before the key. The laptop's real records read:
#
#   bridgeDeviceId....@\"295592ab-1617-486d-b1b4.?df2e7b40ac"
#   bridgeDeviceId....@."7e788.??>677b-43ea-98ae-a0b29ef8ee6f"
#
# so a contiguous UUID regex matches NEITHER - which is why a probe there found
# 194 UUIDs and none of the ones being looked for. Every one of those 194 was an
# unrelated id sitting in an uncompressed blob; the wanted ones were the only
# ones broken up. Characters are lost, not merely inserted, so the value also
# cannot be reassembled: `-434f` arrives as `.?df`.
#
# Reconstruction is therefore impossible, but the question that actually matters
# is answerable. Anchor on the key, look only at the window after it, and ask
# whether a candidate's leading run appears there. 13 characters (8 hex, a dash,
# 4 hex) is 48 bits of prefix against a handful of candidates.
# The floor for calling a broken-up value a match. 16 characters of a UUID is
# 64 bits of content; the real records yield 23 and 27. Paired with the
# uniqueness rule below - one candidate per profile or none - a coincidental
# hit would have to be constructed rather than encountered.
$MIN_RUN = 16

function Get-LongestRunInWindow {
    param([string]$Text, [string]$Candidate)
    $best = 0
    if ([string]::IsNullOrEmpty($Text)) { return 0 }

    # Refuse a candidate that is not a UUID. Substring matching is only safe on
    # a value of known shape: a caller that accidentally passes two ids as one
    # comma-joined string would otherwise "match" on a run of the first and be
    # told a browser holds an id that does not exist. That is exactly what a
    # quoting bug in the test harness did.
    if ($Candidate -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
        Write-Warning "ignoring malformed deviceId '$Candidate' - expected a UUID"
        return 0
    }

    # 'ridgeDeviceId': the leading character is eaten by the length byte often
    # enough that anchoring on the full key misses real records.
    foreach ($m in [regex]::Matches($Text, 'ridgeDeviceId')) {
        $start = $m.Index + $m.Length
        $len = [Math]::Min(160, $Text.Length - $start)
        if ($len -le 0) { continue }
        $window = $Text.Substring($start, $len)

        # LONGEST CONTIGUOUS RUN ANYWHERE IN THE CANDIDATE - NOT A PREFIX.
        #
        # The merged version tested the candidate's first 13 characters, which
        # assumed the corruption lands LATE. It does on the laptop's Chrome
        # record (295592ab-1617-486d-b1b4 then junk) and NOT on its Edge
        # record, whose real bytes are:
        #
        #     bridgeDeviceId....@."7e788.??>677b-43ea-98ae-a0b29ef8ee6f"
        #
        # There the intact portion is the TAIL. `7e788713-677b` never appears,
        # so the tool reported ELSEWHERE for the very browser it was built to
        # identify - and 26 green assertions missed it because the fixture only
        # reproduced the Chrome shape. A test that passes for the wrong reason.
        #
        # Scanning every offset finds 27 contiguous characters for that Edge
        # record and 23 for the Chrome one, either of which is far stronger
        # evidence than a 13-character prefix ever was.
        for ($i = 0; $i -lt $Candidate.Length; $i++) {
            for ($len = $Candidate.Length - $i; $len -gt $best; $len--) {
                if ($window.Contains($Candidate.Substring($i, $len))) { $best = $len; break }
            }
        }
    }
    return $best
}

function Get-ProfileIdentity {
    param([string]$ProfilePath, [string[]]$Candidates = @())
    $result = @{ DeviceId = $null; DisplayName = $null; Unreadable = 0;
                 Searched = @(); Absent = @(); Empty = @(); MatchedIds = @();
                 KeySeen = $false; Runs = @{}; Ambiguous = @(); Exact = @(); AllIds = @() }

    $files = @()
    foreach ($d in (Get-SettingsDirs $ProfilePath)) {
        if (-not (Test-Path $d.Path)) { $result.Absent += $d.Label; continue }
        $found = @(Get-ChildItem $d.Path -File -ErrorAction SilentlyContinue)
        if ($found.Count -eq 0) {
            # EXISTS IS NOT SEARCHED. A directory that is present but holds no
            # files was being counted as searched, so the report claimed to
            # have read a location where nothing was opened. That is the same
            # overclaim as the empty-set reconciliation, one level down: the
            # scope of "not found" quietly grew to cover a place never read.
            $result.Empty += $d.Label
            continue
        }
        $result.Searched += $d.Label
        $files += $found
    }

    # Newest first: the current value lives in the most recently written table,
    # and an older compacted .ldb can still carry a superseded deviceId.
    $files = @($files | Sort-Object LastWriteTime -Descending)
    foreach ($f in $files) {
        $txt = Read-SharedText $f.FullName
        if ($null -eq $txt) { $result.Unreadable++; continue }
        # Was the key here AT ALL? This, not a parsed value, is what makes a
        # non-match meaningful: key present and candidate absent is evidence;
        # key never seen is an open question.
        if ($txt.Contains('ridgeDeviceId')) { $result.KeySeen = $true }

        # COLLECT, DO NOT LET THE FIRST FILE WIN.
        #
        # Files are walked newest-mtime-first, and that ordering was being used
        # as though it were LevelDB's. It is not: LevelDB orders by sequence
        # number, and a compaction can give an older record a newer mtime. So
        # "the first complete UUID I see" was resolving a conflict by an
        # irrelevant clock. Gather every distinct value and let the caller
        # refuse if they disagree.
        $seen = Get-SettingAfterKey $txt 'bridgeDeviceId'
        if ($seen -match '^[0-9a-fA-F-]{36}$' -and $result.AllIds -notcontains $seen) {
            $result.AllIds += $seen
        }

        if (-not $result.DeviceId) {
            # Best effort, for DISPLAY only. It succeeds where the block
            # happened to be stored uncompressed and fails silently otherwise,
            # so it must never be what reconciliation depends on.
            $v = Get-SettingAfterKey $txt 'bridgeDeviceId'
            if ($v -match '^[0-9a-fA-F-]{36}$') { $result.DeviceId = $v }
        }

        # RECONCILIATION USES THIS, NOT THE EXTRACTED VALUE.
        # Asking "is this id here" survives a value the compressor has broken
        # up; reading the value back does not.
        foreach ($c in $Candidates) {
            $run = Get-LongestRunInWindow $txt $c
            if ($run -gt 0) {
                if (-not $result.Runs.ContainsKey($c)) { $result.Runs[$c] = 0 }
                if ($run -gt $result.Runs[$c]) { $result.Runs[$c] = $run }
            }
        }
        if (-not $result.DisplayName) {
            # 'isplayName', not 'displayName', ON PURPOSE.
            #
            # LevelDB writes a length byte immediately before each key, and it
            # lands on top of the first character when the record is read as
            # text - the raw bytes here read `.isplayName.I...<?"Chrome
            # Extension - VM"`. Searching for the full key silently matched
            # nothing and reported a blank name, which looked like "the
            # extension stores no name" when it stores a good one. The same
            # hazard does not hit bridgeDeviceId because a separator survives
            # in front of it, so only this key is truncated.
            # Validate the shape, do not just take the first quoted run. The
            # key appears in several compacted tables with different binary
            # framing, and an unvalidated capture returned ":" from one of
            # them - a value that is obviously not a name, presented as one.
            $v = Get-SettingAfterKey $txt 'isplayName'
            if ($v -and $v.Length -ge 2 -and $v -match '[A-Za-z]{2}') {
                # The text was read Latin-1 to keep byte offsets true, so the
                # captured run is raw bytes in a string. Turn it back into
                # bytes and decode as UTF-8, which is what Chrome actually
                # stored - otherwise a name with an accent or an emoji renders
                # as mojibake and looks like a corrupt record rather than a
                # correctly stored name.
                try {
                    $bytes = [System.Text.Encoding]::GetEncoding(28591).GetBytes($v)
                    $decoded = [System.Text.Encoding]::UTF8.GetString($bytes)
                    if ($decoded -and ($decoded -notmatch [char]0xFFFD)) { $v = $decoded }
                } catch { }
                $result.DisplayName = $v
            }
        }
    }
    # ONE CANDIDATE PER PROFILE, OR NONE.
    #
    # A profile stores one bridgeDeviceId. If two different candidates both
    # clear the floor here, the evidence cannot distinguish them and reporting
    # either would be a coin flip dressed as a finding - which is the whole
    # failure this tool exists to end. Say ambiguous and let the caller refuse.
    $over = @($result.Runs.Keys | Where-Object { $result.Runs[$_] -ge $MIN_RUN })
    if ($over.Count -eq 1) {
        $result.MatchedIds = @($over[0])
        if ($result.Runs[$over[0]] -eq $over[0].Length) { $result.Exact = @($over[0]) }
    } elseif ($over.Count -gt 1) {
        $result.Ambiguous = $over
    }

    return $result
}

if ($ScanRoot) {
    # Process name deliberately impossible to match, so a fixture run reports
    # Running=False rather than borrowing the state of a real browser that
    # happens to be open on the machine running the tests.
    $BROWSERS = @(@{ Name = $ScanName; Root = $ScanRoot; Process = '__fixture_no_such_process__' })
}

$rows = @()

foreach ($b in $BROWSERS) {
    if (-not (Test-Path $b.Root)) { continue }

    $procs = @(Get-Process -Name $b.Process -ErrorAction SilentlyContinue)
    $titles = @($procs | Where-Object { $_.MainWindowTitle } | ForEach-Object { $_.MainWindowTitle })

    $profiles = @(Get-ChildItem $b.Root -Directory -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -eq 'Default' -or $_.Name -like 'Profile *' })

    foreach ($p in $profiles) {
        # ALL VERSION DIRECTORIES, NEWEST REPORTED - NOT WHICHEVER CAME FIRST.
        #
        # Chrome keeps old versions beside the current one, so this directory
        # routinely holds 1.0.91_0, 1.0.92_0 and 1.0.93_0 together. Reporting
        # `Select-Object -First 1` gave 1.0.91 on a laptop actually running
        # 1.0.93, and that single wrong number sent two sessions chasing a
        # storage-format-changed-between-versions theory that did not exist.
        # A number that invites a theory has to be the right number.
        $extDir = Join-Path $p.FullName "Extensions\$EXT_ID"
        if (-not (Test-Path $extDir)) { continue }   # not installed in this profile

        $versions = @()
        foreach ($vd in @(Get-ChildItem $extDir -Directory -ErrorAction SilentlyContinue)) {
            $mf = Join-Path $vd.FullName 'manifest.json'
            if (-not (Test-Path $mf)) { continue }
            try { $versions += (Get-Content $mf -Raw | ConvertFrom-Json).version }
            catch { $versions += ($vd.Name -replace '_\d+$', '') }
        }
        if ($versions.Count -eq 0) { continue }

        $sorted = @($versions | Sort-Object -Property @{ Expression = {
            $parsed = $null
            if ([version]::TryParse($_, [ref]$parsed)) { $parsed } else { [version]'0.0.0' }
        } })
        $version = $sorted[-1]
        if ($sorted.Count -gt 1) {
            $version = $sorted[-1] + ' (also ' + (($sorted[0..($sorted.Count-2)]) -join ', ') + ')'
        }

        $id = Get-ProfileIdentity -ProfilePath $p.FullName -Candidates $DeviceId

        $rows += [PSCustomObject]@{
            Browser     = $b.Name
            Profile     = $p.Name
            ExtVersion  = $version
            # A profile holding two different complete ids is not a profile
            # whose id is the one in the newest file. It is a profile whose id
            # this tool cannot determine, and saying so is the only honest
            # rendering.
            DeviceId    = $(
                if ($id.AllIds.Count -gt 1) { '(CONFLICTING: ' + ($id.AllIds -join ', ') + ')' }
                elseif ($id.DeviceId) { $id.DeviceId }
                else { '(none stored)' })
            DisplayName = $(if ($id.DisplayName) { $id.DisplayName } else { '' })
            # Surfaced, not just counted. "(none stored)" because the file was
            # locked and "(none stored)" because there is genuinely no id are
            # different facts, and a reader cannot tell them apart otherwise.
            Unreadable  = $id.Unreadable
            # Which storage locations actually existed and were opened. Carried
            # on the row so the refusal can scope "not found" to what was read,
            # rather than implying the whole profile was examined.
            Searched    = $id.Searched
            Absent      = $id.Absent
            Empty       = $id.Empty
            MatchedIds  = $id.MatchedIds
            KeySeen     = $id.KeySeen
            Ambiguous   = $id.Ambiguous
            Exact       = $id.Exact
            # The length of the contiguous run each verdict rests on, so a
            # reader can weigh the evidence instead of taking MATCHED on faith.
            Runs        = $id.Runs
            Running     = ($procs.Count -gt 0)
            Windows     = $titles.Count
            Title       = $(if ($titles.Count -gt 0) { $titles[0] } else { '' })
        }
    }
}

Write-Output ''
Write-Output '=== Claude extension installs on this machine ==='
if ($rows.Count -eq 0) {
    Write-Output '  none found - the extension is not installed in any Chromium profile here.'
} else {
    $rows | Format-List
}

Write-Output "=== Local installs: $($rows.Count) ==="

if ($DeviceId.Count -gt 0) {
    Write-Output ''
    Write-Output '=== Reconciliation against the connected list ==='

    # REFUSE TO RECONCILE AGAINST AN EMPTY SET.
    #
    # Found by the laptop session (VANLAS) running this for real: both of its
    # profiles returned "(none stored)", so every id was reported ELSEWHERE -
    # which is true only in the way a statement about an empty set is true. It
    # read as "these ids are not on that machine" when it meant "this script
    # extracted nothing and therefore compared nothing", and one of those ids
    # was very probably the Edge sitting open on that very desk.
    #
    # An extraction failure must never again present as a clean NOT HERE.
    # Silence has to mean clean, never "did not look".
    # The signal is whether the KEY was seen, not whether a value parsed. The
    # laptop proved those differ: its records hold bridgeDeviceId perfectly well
    # while the value is broken up by compression, so keying the refusal on a
    # parsed value would refuse a machine that can in fact answer.
    $canAnswer = @($rows | Where-Object { $_.KeySeen })
    if ($rows.Count -gt 0 -and $canAnswer.Count -eq 0) {
        Write-Output ''
        Write-Output ("  REFUSING TO RECONCILE: found $($rows.Count) extension install(s) here,")
        Write-Output '  and read a deviceId from NONE of them. Every answer below would be'
        Write-Output '  "not here" regardless of the truth, so no answer is given.'
        Write-Output ''
        $unread = ($rows | Measure-Object -Property Unreadable -Sum).Sum
        if ($unread -gt 0) {
            Write-Output ("  $unread settings file(s) could not be read - the browser may be")
            Write-Output '  holding them. Close it and re-run.'
        } else {
            Write-Output '  All settings files were readable, and no bridgeDeviceId key was'
            Write-Output '  found in any of them. Extension versions found here:'
            foreach ($v in ($rows | Select-Object -ExpandProperty ExtVersion -Unique)) {
                Write-Output ("    v$v")
            }
            Write-Output ''
            Write-Output '  Searched, per profile:'
            foreach ($s in ($rows | Select-Object -ExpandProperty Searched -Unique)) {
                Write-Output ("    present  $s")
            }
            foreach ($s in ($rows | Select-Object -ExpandProperty Empty -Unique)) {
                Write-Output ("    EMPTY    $s  (exists, held no files - nothing was read)")
            }
            foreach ($s in ($rows | Select-Object -ExpandProperty Absent -Unique)) {
                Write-Output ("    absent   $s")
            }
            Write-Output ''
            # STATE THE LIMIT OF THE SCAN, not just its result.
            #
            # The laptop probe reported zero hits and then said the thing this
            # paragraph exists to encode: it read those files as ASCII and
            # matched text. An id stored as raw 16 bytes, compressed, or in any
            # binary encoding produces exactly the same zero. Reporting that as
            # "no id here" would be the same empty-set mistake one level down -
            # a scan that could not see the value, presented as a value that is
            # not there.
            Write-Output '  LIMIT: this reads those files as ASCII and matches the key as'
            Write-Output '  TEXT. An id stored binary or compressed would produce this same'
            Write-Output '  result, so this is "no bridgeDeviceId as readable text in the'
            Write-Output '  paths above" - NOT proof the browser has no id. Measured on'
            Write-Output '  extension 1.0.92 the key is plain text; 1.0.91 appears not to'
            Write-Output '  persist it at all, but that was established the same ASCII way.'
        }
        exit 2
    }

    $stale = 0
    $ambiguous = 0
    foreach ($d in $DeviceId) {
        # MatchedIds first: it survives a compressed value. DeviceId equality is
        # kept as a fallback for the uncompressed case and costs nothing.
        # A profile that could not tell two candidates apart must not answer for
        # EITHER of them. Reported separately from a clean miss, because
        # "ambiguous here" and "not here" call for different next actions.
        $amb = @($rows | Where-Object { $_.Ambiguous -contains $d })
        if ($amb.Count -gt 0) {
            $ambiguous++
            $a = $amb[0]
            Write-Output ("  AMBIGUOUS {0}  -> {1} / {2} matched more than one candidate; refusing to guess" -f $d, $a.Browser, $a.Profile)
            continue
        }

        $hit = @($rows | Where-Object { ($_.MatchedIds -contains $d) -or ($_.DeviceId -eq $d) })
        if ($hit.Count -gt 0) {
            $h = $hit[0]
            $live = $(if ($h.Running -and $h.Windows -gt 0) { 'LIVE' } else { 'installed but not running' })
            # EXACT vs PARTIAL, never conflated. A partial rests on a contiguous
            # run through a value the compressor broke up; a reader deciding
            # whether to act on it deserves to know which, and how long the run
            # was.
            $kind = 'MATCHED'
            $how = ''
            if ($h.Exact -notcontains $d) {
                $kind = 'MATCHED*'
                $runLen = 0
                if ($h.Runs.ContainsKey($d)) { $runLen = $h.Runs[$d] }
                $how = ("  [partial: {0} of {1} chars contiguous]" -f $runLen, $d.Length)
            }
            Write-Output ("  {0} {1}  -> {2} / {3}  [{4}]  {5}{6}" -f $kind, $d, $h.Browser, $h.Profile, $live, $h.DisplayName, $how)
        } else {
            $stale++
            Write-Output ("  ELSEWHERE {0}  -> no profile ON THIS MACHINE holds this id" -f $d)
        }
    }
    Write-Output ''
    Write-Output ("  $stale of $($DeviceId.Count) ids belong to no browser on this machine.")
    if ($stale -gt 0) {
        # DO NOT CALL THESE STALE. This label used to read "STALE", and that one
        # word caused a wrong conclusion the first time the tool was run: two ids
        # were reported as dead registrations and they turned out to be the
        # user's LAPTOP browsers, one of which connected fine moments later.
        #
        # "Not here" and "not anywhere" are different claims and this script can
        # only ever make the first one. It sees one machine's disk.
        Write-Output '  That means NOT HERE - it does NOT mean stale. They may be'
        Write-Output '  live browsers on another machine on the same account. Run'
        Write-Output '  this on that machine to tell the two apart; isLocal in the'
        Write-Output '  API response does not, having been observed true for a'
        Write-Output '  browser on a different host.'
    }
}

# Exit non-zero when asked to reconcile and nothing matched: a caller that
# cannot find ANY live browser should fail rather than pick one at random.
if ($DeviceId.Count -gt 0) {
    # Same basis as the reconciliation above. Keying this on the PARSED value
    # made a run that printed MATCHED exit 1, because the value it matched on
    # was compression-broken and never parsed - the report and the exit code
    # disagreeing, which is the failure PR70 shipped.
    $matched = @($rows | Where-Object {
        (@($_.MatchedIds | Where-Object { $DeviceId -contains $_ }).Count -gt 0) -or
        ($DeviceId -contains $_.DeviceId)
    })
    if ($matched.Count -eq 0) { exit 1 }
}
exit 0
