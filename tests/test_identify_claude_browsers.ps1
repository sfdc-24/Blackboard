<#
    Drive identify_claude_browsers.ps1 against fixture profile trees.

    WHY FIXTURES AND NOT "RUN IT AND LOOK"
        The parsing in this tool has been wrong three times, each time quietly:
        it skipped the files Chrome holds open, it missed displayName because
        LevelDB overwrites the key's first byte, and it once returned ":" as a
        browser name. None of those threw. All three produced a confident,
        wrong answer, and the only reason they were caught is that a human
        happened to know what the value should have been.

        A fixture pins what the bytes actually look like, so the next change to
        the regex has to keep agreeing with them.

    THE ONE THAT MATTERS MOST
        In the real file, `anonymousId` and its UUID appear IMMEDIATELY BEFORE
        `bridgeDeviceId`. A bare UUID regex - the obvious implementation -
        returns the anonymousId and is wrong in a way that looks perfectly
        plausible: still a UUID, still stable, just not the one the server
        knows the browser by. Fixture 1 reproduces that ordering on purpose.
#>
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$SUT = Join-Path $PSScriptRoot '..\tools\browser\identify_claude_browsers.ps1'
if (-not (Test-Path $SUT)) { throw "cannot find $SUT" }
$SUT = (Resolve-Path $SUT).Path

$EXT_ID = 'fcoeoabgfenejglbffodgkkbkcdhcgfn'
$script:Passed = 0
$script:Failed = 0

function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) {
        $script:Passed++
        Write-Output "PASS $Name"
    } else {
        $script:Failed++
        if ($Detail) { Write-Output "FAIL $Name : $Detail" } else { Write-Output "FAIL $Name" }
    }
}

$NUL = [string][char]0

# Build a profile that looks like a real one to the tool: the extension
# directory with a manifest, plus a settings directory holding LevelDB tables.
function New-FixtureProfile {
    param(
        [string]$Root,
        [string]$Profile = 'Default',
        [string]$Version = '1.0.92',
        [hashtable[]]$Tables = @(),
        [switch]$NoExtension
    )
    $pdir = Join-Path $Root $Profile
    if (-not $NoExtension) {
        $edir = Join-Path $pdir "Extensions\$EXT_ID\$($Version)_0"
        New-Item -ItemType Directory -Force -Path $edir | Out-Null
        Set-Content -Path (Join-Path $edir 'manifest.json') -Encoding utf8 `
            -Value ('{"name":"Claude","version":"' + $Version + '","manifest_version":3}')
    }
    $sdir = Join-Path $pdir "Local Extension Settings\$EXT_ID"
    New-Item -ItemType Directory -Force -Path $sdir | Out-Null
    foreach ($t in $Tables) {
        # LATIN-1, matching how the tool reads. Writing fixtures as ASCII folds
        # every byte above 0x7F to '?', so a fixture built to carry a non-ASCII
        # display name never contained one - and the suite reported the TOOL as
        # mangling a name the FIXTURE had already destroyed. The test writer and
        # the code under test have to agree on bytes or the test is fiction.
        [System.IO.File]::WriteAllText((Join-Path $sdir $t.Name), $t.Body,
                                       [System.Text.Encoding]::GetEncoding(28591))
        # Age the files so "newest first" ordering inside the tool is
        # deterministic rather than dependent on how fast the disk was.
        if ($t.ContainsKey('AgeMinutes')) {
            (Get-Item (Join-Path $sdir $t.Name)).LastWriteTime = (Get-Date).AddMinutes(-1 * $t.AgeMinutes)
        }
    }
    return $pdir
}

# The real framing, reproduced: anonymousId FIRST, then bridgeDeviceId, then a
# displayName whose leading 'd' has been eaten by LevelDB's length byte.
function New-RealisticBody {
    param([string]$AnonId, [string]$DeviceId, [string]$DisplayName)
    return ($NUL + '&nonymousId' + $NUL + $NUL + ':?' + $NUL + '"' + $AnonId + '"' +
            $NUL + $NUL + '&bridgeDeviceId' + $NUL + $NUL + $NUL + $NUL + '@?"' + $DeviceId + '"' +
            $NUL + $NUL + $NUL + 'isplayName' + $NUL + 'I' + $NUL + $NUL + $NUL + '<?"' + $DisplayName + '"')
}

function Invoke-Sut {
    param([string]$Root, [string[]]$Ids = @())

    # NO `2>&1` ON A NATIVE EXE.
    #   Windows PowerShell 5.1 wraps every stderr line from a native command in
    #   an ErrorRecord (NativeCommandError) and sets $? to $false even when the
    #   process exited 0. That turns "the script printed a warning" into what
    #   looks like a test failure. Redirect both streams to files instead and
    #   read them back, so the exit code is the only verdict.
    $o = [System.IO.Path]::GetTempFileName()
    $e = [System.IO.Path]::GetTempFileName()
    try {
        # THE IDS MUST ARRIVE AS AN ARRAY, AND THE COMMAND LINE CANNOT DO IT.
        #
        # Two attempts failed differently and both produced WRONG TEST RESULTS
        # rather than errors:
        #   Start-Process quoted ('-DeviceId','a,b') into -DeviceId "a,b";
        #   cmd.exe passed it through unquoted and `powershell -File` STILL
        #   bound it as one string, because -File does not split a comma the
        #   way -Command does.
        # Either way the script saw a single malformed candidate. The first
        # version then "matched" it on a 13-character prefix and printed
        # MATCHED for an id that does not exist - a harness bug manufacturing a
        # false positive, which is worse than a broken tool because it reads as
        # evidence. (The script now refuses a non-UUID candidate outright,
        # which is how the second attempt showed up as a clean ELSEWHERE
        # instead of a phantom match.)
        #
        # A generated wrapper sidesteps the quoting entirely: the array is
        # built in PowerShell source, where an array is unambiguous.
        $w = [System.IO.Path]::GetTempFileName() + '.ps1'
        $idLiteral = '@()'
        if ($Ids.Count -gt 0) {
            $idLiteral = '@(' + (($Ids | ForEach-Object { "'" + $_ + "'" }) -join ',') + ')'
        }
        # THE WRAPPER WRITES THE FILE ITSELF, WITH AN EXPLICIT ENCODING.
        #
        # Letting the shell redirect stdout captures raw bytes encoded with the
        # console code page, and setting [Console]::OutputEncoding inside the
        # child does not retake an already-redirected handle. A correctly
        # decoded display name therefore came back mangled and the suite failed
        # a tool that was right. That is the mirror of the earlier harness bug:
        # one manufactured a false positive, this one a false negative. Both
        # are the harness lying about the thing it exists to measure.
        #
        # Capturing in PowerShell and writing UTF-8 explicitly removes the
        # console code page from the path entirely.
        $wrapper = @(
            '$ErrorActionPreference = ''Continue''',
            "`$out = & '$SUT' -ScanRoot '$Root' -ScanName 'Fixture' -DeviceId $idLiteral 2>&1 | Out-String",
            '$code = $LASTEXITCODE',
            "[System.IO.File]::WriteAllText('$o', `$out, [System.Text.Encoding]::UTF8)",
            'if ($null -eq $code) { $code = 0 }',
            'exit $code'
        ) -join [Environment]::NewLine
        Set-Content -Path $w -Value $wrapper -Encoding utf8

        $p = Start-Process -FilePath 'powershell.exe' `
             -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $w) `
             -NoNewWindow -Wait -PassThru
        $code = $p.ExitCode
        Remove-Item $w -Force -ErrorAction SilentlyContinue
        $text = (Get-Content $o -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)
        $err  = ''
        if ($null -eq $text) { $text = '' }
        if ($null -eq $err)  { $err  = '' }
        return [PSCustomObject]@{ Text = ($text + $err); Code = $code }
    } finally {
        Remove-Item $o, $e -Force -ErrorAction SilentlyContinue
    }
}

$WORK = Join-Path $env:TEMP ("identify-browsers-test-" + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Force -Path $WORK | Out-Null

try {
    $LIVE_ID = 'f1315438-e826-4a57-a576-0f169bd13666'
    $ANON_ID = '33201699-13c0-4701-b4bf-06cfc6a06147'
    $OTHER   = '295592ab-1617-486d-b1b4-434f2e7b40ac'

    # ---- fixture 1: the realistic shape -----------------------------------
    $r1 = Join-Path $WORK 'r1'
    New-FixtureProfile -Root $r1 -Tables @(
        @{ Name = '000041.ldb'; Body = (New-RealisticBody $ANON_ID $LIVE_ID 'Chrome Extension - VM'); AgeMinutes = 5 }
    ) | Out-Null

    $res = Invoke-Sut -Root $r1 -Ids @($LIVE_ID)
    Assert-True 'the installed profile is found at all' ($res.Text -match 'Fixture')
    Assert-True 'extension version is read from the manifest' ($res.Text -match '1\.0\.92')

    # THE ANCHORING TEST.
    Assert-True 'bridgeDeviceId is returned, NOT the anonymousId that precedes it' `
        (($res.Text -match [regex]::Escape($LIVE_ID)) -and -not ($res.Text -match [regex]::Escape($ANON_ID))) `
        'a bare UUID regex would have grabbed anonymousId here'

    Assert-True 'displayName survives the eaten leading byte' ($res.Text -match 'Chrome Extension - VM')
    Assert-True 'a known id reconciles as MATCHED' ($res.Text -match 'MATCHED')
    Assert-True 'a matching run exits 0' ($res.Code -eq 0)

    # ---- fixture 2: junk value in a newer table ---------------------------
    # The failure that shipped: an unvalidated capture took ":" from whichever
    # table sorted first and presented it as the browser's name.
    $r2 = Join-Path $WORK 'r2'
    New-FixtureProfile -Root $r2 -Tables @(
        @{ Name = '000053.ldb'; Body = ($NUL + 'isplayName' + $NUL + '"' + ':' + '"'); AgeMinutes = 1 },
        @{ Name = '000041.ldb'; Body = (New-RealisticBody $ANON_ID $LIVE_ID 'Edge on Laptop'); AgeMinutes = 9 }
    ) | Out-Null

    $res2 = Invoke-Sut -Root $r2 -Ids @($LIVE_ID)
    Assert-True 'a punctuation-only displayName is rejected, not reported as a name' `
        (-not ($res2.Text -match 'DisplayName\s*:\s*:\s*$'))
    Assert-True 'the real name is still found in an older table' ($res2.Text -match 'Edge on Laptop')

    # ---- fixture 3: an id that is not here --------------------------------
    $res3 = Invoke-Sut -Root $r1 -Ids @($OTHER)
    Assert-True 'an absent id reports ELSEWHERE' ($res3.Text -match 'ELSEWHERE')
    # -cmatch, case SENSITIVE and on purpose: the explanatory paragraph
    # legitimately contains the lowercase word "stale", and a case-insensitive
    # match here fails on the tool's own correct output.
    Assert-True 'ELSEWHERE is NOT reported under a STALE label' (-not ($res3.Text -cmatch 'STALE')) `
        'calling it stale is the wording that caused a wrong conclusion'
    Assert-True 'the output says not-here does not mean stale' ($res3.Text -match 'does NOT mean stale')
    Assert-True 'reconciling with no match exits non-zero' ($res3.Code -ne 0) `
        'a caller that finds no live browser must fail rather than pick one'

    # ---- fixture 4: profile without the extension -------------------------
    $r4 = Join-Path $WORK 'r4'
    New-FixtureProfile -Root $r4 -NoExtension -Tables @(
        @{ Name = '000041.ldb'; Body = (New-RealisticBody $ANON_ID $LIVE_ID 'Should Not Appear'); AgeMinutes = 5 }
    ) | Out-Null
    $res4 = Invoke-Sut -Root $r4 -Ids @($LIVE_ID)
    Assert-True 'a profile without the extension installed is skipped' `
        (-not ($res4.Text -match 'Should Not Appear')) `
        'settings without an install are leftovers, not a usable browser'

    # ---- fixture 6: the laptop's situation, reproduced ---------------------
    # VANLAS ran this for real with two installed profiles from which no id
    # could be read, and got three confident ELSEWHERE lines. That verdict was
    # a statement about an empty set. This pins the refusal.
    $r6 = Join-Path $WORK 'r6'
    New-FixtureProfile -Root $r6 -Profile 'Profile 9' -Version '1.0.91' -Tables @(
        @{ Name = '000041.ldb'; Body = ($NUL + 'someOtherKey' + $NUL + '"irrelevant"'); AgeMinutes = 5 }
    ) | Out-Null
    New-FixtureProfile -Root $r6 -Profile 'Profile 2' -Version '1.0.91' -Tables @(
        @{ Name = '000041.ldb'; Body = ($NUL + 'someOtherKey' + $NUL + '"irrelevant"'); AgeMinutes = 5 }
    ) | Out-Null

    $res6 = Invoke-Sut -Root $r6 -Ids @($LIVE_ID, $OTHER)
    Assert-True 'installs with no readable id do NOT produce ELSEWHERE verdicts' `
        (-not ($res6.Text -cmatch 'ELSEWHERE')) `
        'an extraction failure must not present as a clean not-here'
    Assert-True 'it refuses to reconcile and says why' ($res6.Text -match 'REFUSING TO RECONCILE')
    Assert-True 'the refusal names the extension versions it saw' ($res6.Text -match 'v1\.0\.91')
    Assert-True 'refusing to reconcile exits 2, distinct from 0 and from 1' ($res6.Code -eq 2) `
        "got exit $($res6.Code); 2 means could-not-look and must not be flattened into no-match"

    # ---- fixture 8: the laptop's COMPRESSED value -------------------------
    # The real records on VANLAS, transcribed from its probe:
    #
    #   bridgeDeviceId....@\"295592ab-1617-486d-b1b4.?df2e7b40ac"
    #   bridgeDeviceId....@."7e788.??>677b-43ea-98ae-a0b29ef8ee6f"
    #
    # LevelDB compresses the block, so framing bytes land INSIDE the value and
    # characters are LOST, not merely inserted - `-434f` arrives as `.?df`. No
    # contiguous UUID regex can match these, and the value cannot be rebuilt.
    # Reconciliation must still answer, by anchoring on the key and asking
    # whether the candidate's leading run is in the window after it.
    $LAPTOP_CHROME = '295592ab-1617-486d-b1b4-434f2e7b40ac'
    $LAPTOP_EDGE   = '7e788713-677b-43ea-98ae-a0b29ef8ee6f'

    $r8 = Join-Path $WORK 'r8'
    $chromeBody = $NUL + '&bridgeDeviceId' + $NUL + $NUL + $NUL + $NUL + '@' + $NUL +
                  '"295592ab-1617-486d-b1b4' + $NUL + '?df' + '2e7b40ac"'
    New-FixtureProfile -Root $r8 -Profile 'Profile 9' -Version '1.0.93' -Tables @(
        @{ Name = '000079.ldb'; Body = $chromeBody; AgeMinutes = 3 }
    ) | Out-Null

    $res8 = Invoke-Sut -Root $r8 -Ids @($LAPTOP_CHROME, $LAPTOP_EDGE)
    Assert-True 'a compression-broken value still reconciles as MATCHED' `
        ($res8.Text -cmatch 'MATCHED') `
        'the stored UUID is split by framing bytes; matching must anchor on the key'
    Assert-True 'the id that is NOT there is still reported ELSEWHERE' `
        ($res8.Text -cmatch 'ELSEWHERE') `
        'a key-anchored window must not match every candidate indiscriminately'
    Assert-True 'a compression-broken value does not trigger the refusal path' `
        (-not ($res8.Text -match 'REFUSING TO RECONCILE')) `
        'the key is present, so this machine CAN answer even though no value parses'
    Assert-True 'a broken value that still matches exits 0' ($res8.Code -eq 0) `
        "got exit $($res8.Code)"

    # ---- fixture 9: THE EDGE SHAPE - corruption near the START ------------
    # The regression that shipped in the merged PR87. Its matcher tested the
    # candidate's first 13 characters, which assumes the corruption lands late.
    # The laptop's real Edge record breaks right after `7e788`, so the intact
    # portion is the TAIL and the tool reported ELSEWHERE for the very browser
    # it was built to identify. 26 green assertions missed it because the only
    # compression fixture reproduced the Chrome shape.
    $r9 = Join-Path $WORK 'r9'
    $edgeBody = $NUL + '&bridgeDeviceId' + $NUL + $NUL + $NUL + $NUL + '@' + $NUL +
                '."7e788' + $NUL + '?' + $NUL + '>' + '677b-43ea-98ae-a0b29ef8ee6f"'
    New-FixtureProfile -Root $r9 -Profile 'Profile 2' -Version '1.0.93' -Tables @(
        @{ Name = '000005.ldb'; Body = $edgeBody; AgeMinutes = 3 }
    ) | Out-Null

    $res9 = Invoke-Sut -Root $r9 -Ids @($LAPTOP_EDGE, $LIVE_ID)
    Assert-True 'corruption near the START of the value still matches (tail intact)' `
        ($res9.Text -cmatch 'MATCHED') `
        'the merged version reported ELSEWHERE here - prefix matching assumed late corruption'
    Assert-True 'the tail-intact match is labelled partial with its run length' `
        ($res9.Text -match 'partial: 27 of 36') `
        'a partial match must never be presented as an exact one'
    Assert-True 'the unrelated id is still ELSEWHERE in the same run' `
        ($res9.Text -cmatch 'ELSEWHERE')

    # ---- fixture 10: a run BELOW the floor must not match ------------------
    # codex constructed prefix collisions as its objection to the old matcher.
    # With a 16-character floor, a window carrying only 13 characters of a
    # candidate must be refused - otherwise the floor is decorative.
    $r10 = Join-Path $WORK 'r10'
    $shortBody = $NUL + '&bridgeDeviceId' + $NUL + $NUL + '@' + $NUL + '"7e788713-677' + $NUL + 'XXXX"'
    New-FixtureProfile -Root $r10 -Profile 'Default' -Version '1.0.93' -Tables @(
        @{ Name = '000001.ldb'; Body = $shortBody; AgeMinutes = 3 }
    ) | Out-Null
    $res10 = Invoke-Sut -Root $r10 -Ids @($LAPTOP_EDGE)
    Assert-True 'a contiguous run shorter than the floor does NOT match' `
        (-not ($res10.Text -cmatch 'MATCHED')) `
        '13 characters was the old threshold and is exactly what codex objected to'

    # ---- fixture 11: two candidates in one profile is AMBIGUOUS ------------
    # A profile stores ONE id. If the evidence cannot separate two candidates,
    # reporting either is a coin flip dressed as a finding.
    $r11 = Join-Path $WORK 'r11'
    $bothBody = $NUL + '&bridgeDeviceId' + $NUL + '@' + '"295592ab-1617-486d-b1b4' + $NUL + 'x"' +
                $NUL + '&bridgeDeviceId' + $NUL + '@' + '"7e788713-677b-43ea-98ae' + $NUL + 'y"'
    New-FixtureProfile -Root $r11 -Profile 'Default' -Version '1.0.93' -Tables @(
        @{ Name = '000001.ldb'; Body = $bothBody; AgeMinutes = 3 }
    ) | Out-Null
    $res11 = Invoke-Sut -Root $r11 -Ids @($LAPTOP_CHROME, $LAPTOP_EDGE)
    Assert-True 'two candidates matching one profile report AMBIGUOUS' `
        ($res11.Text -cmatch 'AMBIGUOUS') `
        'one profile holds one id; refusing to pick is the only honest answer'
    Assert-True 'an ambiguous profile reports MATCHED for neither candidate' `
        (-not ($res11.Text -cmatch 'MATCHED')) `
        'reporting either would be a guess presented as evidence'

    # ---- fixture 11b: ambiguity must not hide a definite match ------------
    # chatgpt-codex-connector, reviewing PR89. Ambiguity was checked BEFORE
    # matches, so a profile that could not separate two candidates suppressed a
    # definite unique match for the same candidate in a DIFFERENT profile. That
    # is global suppression by another name - the exact thing I had just argued
    # against in codex's own proposal, reintroduced one scope down.
    $r11b = Join-Path $WORK 'r11b'
    # Chrome: clean, unique, definite evidence for LAPTOP_CHROME.
    New-FixtureProfile -Root $r11b -Profile 'Profile 9' -Version '1.0.93' -Tables @(
        @{ Name = '000001.ldb'; Body = (New-RealisticBody $ANON_ID $LAPTOP_CHROME 'Clean Chrome'); AgeMinutes = 5 }
    ) | Out-Null
    # Edge: muddled - both candidates clear the floor here.
    $muddled = $NUL + '&bridgeDeviceId' + $NUL + '@' + '"295592ab-1617-486d-b1b4' + $NUL + 'x"' +
               $NUL + '&bridgeDeviceId' + $NUL + '@' + '"7e788713-677b-43ea-98ae' + $NUL + 'y"'
    New-FixtureProfile -Root $r11b -Profile 'Profile 2' -Version '1.0.93' -Tables @(
        @{ Name = '000002.ldb'; Body = $muddled; AgeMinutes = 5 }
    ) | Out-Null

    $res11b = Invoke-Sut -Root $r11b -Ids @($LAPTOP_CHROME, $LAPTOP_EDGE)
    Assert-True 'a definite match wins over ambiguity in another profile' `
        ($res11b.Text -cmatch ('MATCHED\*?\s+' + [regex]::Escape($LAPTOP_CHROME))) `
        'ambiguity elsewhere must not suppress unique positive evidence here'
    Assert-True 'the candidate with only ambiguous evidence stays AMBIGUOUS' `
        ($res11b.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LAPTOP_EDGE))) `
        'ambiguity is profile-local, but it still applies where nothing definite exists'

    # ---- fixture 8b: the per-profile block must not contradict itself -----
    # Reported by the laptop session against a real run. The block read:
    #     DeviceId   : (none stored)
    #     MatchedIds : {295592ab-...}
    # Both true, nonsense together. "(none stored)" claimed the id is absent
    # when it is present and merely unextractable, and a reader skimming
    # profile blocks concluded the browser had no id while the next line
    # named it.
    $res8b = Invoke-Sut -Root $r8 -Ids @($LAPTOP_CHROME)
    Assert-True 'a compression-broken value is not reported as "(none stored)"' `
        (-not ($res8b.Text -match '\(none stored\)')) `
        'the id is present; only the extraction failed, and the label said otherwise'
    Assert-True 'it says the value is present but unextractable' `
        ($res8b.Text -match 'present but unextractable') `
        'the exit codes already drew this distinction; the display had not'

    # ---- fixture 12: conflicting complete ids, misleading mtimes ----------
    # Files are walked newest-mtime-first, and that ordering was being treated
    # as LevelDB's. It is not - LevelDB orders by sequence number, and a
    # compaction can give an older record a newer mtime. So the conflict was
    # being resolved by an irrelevant clock.
    $r12 = Join-Path $WORK 'r12'
    New-FixtureProfile -Root $r12 -Profile 'Default' -Version '1.0.93' -Tables @(
        @{ Name = '000090.ldb'; Body = (New-RealisticBody $ANON_ID $LIVE_ID 'Newest By Clock'); AgeMinutes = 1 },
        @{ Name = '000010.ldb'; Body = (New-RealisticBody $ANON_ID $LAPTOP_CHROME 'Older By Clock'); AgeMinutes = 90 }
    ) | Out-Null
    $res12 = Invoke-Sut -Root $r12
    Assert-True 'two different complete ids are reported as CONFLICTING' `
        ($res12.Text -cmatch 'CONFLICTING') `
        'picking the newest mtime resolves a LevelDB conflict by the wrong clock'

    # ---- fixture 13: a non-ASCII display name survives --------------------
    # ASCII decoding folds every byte above 0x7F to '?', so an accented or
    # emoji name arrived as mojibake and read like a corrupt record.
    $r13 = Join-Path $WORK 'r13'
    $utf8Name = 'Chrome Berlin Buro'
    $nameBytes = [System.Text.Encoding]::UTF8.GetBytes('Chrome Berlin B' + [char]0x00FC + 'ro')
    $latin = [System.Text.Encoding]::GetEncoding(28591).GetString($nameBytes)
    $uniBody = $NUL + '&bridgeDeviceId' + $NUL + $NUL + '@?"' + $LIVE_ID + '"' +
               $NUL + 'isplayName' + $NUL + '<?"' + $latin + '"'
    New-FixtureProfile -Root $r13 -Profile 'Default' -Version '1.0.93' -Tables @(
        @{ Name = '000001.ldb'; Body = $uniBody; AgeMinutes = 3 }
    ) | Out-Null
    $res13 = Invoke-Sut -Root $r13 -Ids @($LIVE_ID)
    Assert-True 'a non-ASCII display name is decoded, not mangled' `
        ($res13.Text -match ([char]0x00FC)) `
        'ASCII decoding turned every byte above 0x7F into a question mark'

    # ---- review regressions: evidence must belong to the bridge value -----
    $r14 = Join-Path $WORK 'r14'
    $neighbor = $NUL + 'bridgeDeviceId' + $NUL + '@"' + $LIVE_ID + '"' +
                $NUL + 'anonymousId' + $NUL + '@"' + $ANON_ID + '"'
    New-FixtureProfile -Root $r14 -Tables @(@{ Name = '000001.ldb'; Body = $neighbor }) | Out-Null
    $res14 = Invoke-Sut -Root $r14 -Ids @($ANON_ID)
    Assert-True 'a neighboring anonymousId is never a bridge match' (-not ($res14.Text -cmatch 'MATCHED'))
    Assert-True 'neighbor-only evidence does not exit successfully' ($res14.Code -eq 1)
    $res14live = Invoke-Sut -Root $r14 -Ids @($LIVE_ID)
    Assert-True 'the actual complete bridge value remains an exact match' `
        ($res14live.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID)))

    $r14b = Join-Path $WORK 'r14b'
    $brokenNeighbor = $chromeBody + $NUL + 'anonymousId' + $NUL + '@"' + $ANON_ID + '"'
    New-FixtureProfile -Root $r14b -Tables @(@{ Name = '000001.ldb'; Body = $brokenNeighbor }) | Out-Null
    $res14b = Invoke-Sut -Root $r14b -Ids @($ANON_ID, $LAPTOP_CHROME)
    Assert-True 'a broken bridge value cannot borrow a neighboring candidate' `
        (-not ($res14b.Text -cmatch ('MATCHED\*?\s+' + [regex]::Escape($ANON_ID))))
    Assert-True 'a broken bridge value still supplies its own partial match' `
        ($res14b.Text -cmatch ('MATCHED\*\s+' + [regex]::Escape($LAPTOP_CHROME)))

    # Listing a conflict is insufficient: reconciliation and exit must agree.
    $res15 = Invoke-Sut -Root $r12 -Ids @($LIVE_ID)
    Assert-True 'a complete conflict outside the candidate list stays ambiguous' `
        ($res15.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LIVE_ID)))
    Assert-True 'a conflicted profile cannot supply a match or successful exit' `
        ((-not ($res15.Text -cmatch 'MATCHED')) -and $res15.Code -eq 1)

    $r16 = Join-Path $WORK 'r16'
    $twoValues = $NUL + 'bridgeDeviceId' + $NUL + '@"' + $LIVE_ID + '"' +
                 $NUL + 'bridgeDeviceId' + $NUL + '@"' + $LAPTOP_CHROME + '"'
    New-FixtureProfile -Root $r16 -Tables @(@{ Name = '000001.ldb'; Body = $twoValues }) | Out-Null
    $res16 = Invoke-Sut -Root $r16 -Ids @($LIVE_ID, $LAPTOP_CHROME)
    Assert-True 'all complete values in one file contribute to conflict display' ($res16.Text -cmatch 'CONFLICTING')
    Assert-True 'neither complete value bypasses within-file ambiguity' `
        (($res16.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LIVE_ID))) -and
         ($res16.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LAPTOP_CHROME))))
    Assert-True 'within-file ambiguity yields no match and exits nonzero' `
        ((-not ($res16.Text -cmatch 'MATCHED')) -and $res16.Code -eq 1)

    $res17 = Invoke-Sut -Root $r9 -Ids @($LAPTOP_EDGE.ToUpperInvariant())
    Assert-True 'uppercase UUID input matches the same lowercase compressed value' `
        (($res17.Text -cmatch 'MATCHED\*') -and $res17.Code -eq 0)
    Assert-True 'case normalization preserves the measured partial run length' ($res17.Text -match 'partial: 27 of 36')

    $r18 = Join-Path $WORK 'r18'
    New-FixtureProfile -Root $r18 -Tables @(@{ Name = '000001.ldb'; Body = $twoValues }) | Out-Null
    New-FixtureProfile -Root $r18 -Profile 'Profile 9' -Tables @(
        @{ Name = '000002.ldb'; Body = (New-RealisticBody $ANON_ID $LIVE_ID 'Independent Clean Profile') }
    ) | Out-Null
    $res18 = Invoke-Sut -Root $r18 -Ids @($LIVE_ID, $LAPTOP_CHROME)
    Assert-True 'a clean profile supplies the exact attribution despite another conflict' `
        (($res18.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID) + '\s+-> Fixture / Profile 9')) -and $res18.Code -eq 0)
    Assert-True 'the candidate with only conflicting evidence is still ambiguous' `
        ($res18.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LAPTOP_CHROME)))

    $r19 = Join-Path $WORK 'r19'
    $embedded = $NUL + 'bridgeDeviceId' + $NUL + '@"x' + $LIVE_ID + 'x"'
    New-FixtureProfile -Root $r19 -Tables @(@{ Name = '000001.ldb'; Body = $embedded }) | Out-Null
    $res19 = Invoke-Sut -Root $r19 -Ids @($LIVE_ID)
    Assert-True 'a full run inside a broken value is partial, not an exact extraction' `
        (($res19.Text -cmatch ('MATCHED\*\s+' + [regex]::Escape($LIVE_ID))) -and
         (-not ($res19.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID)))))

    $similar = $LIVE_ID.Substring(0, 35) + '7'
    $res20 = Invoke-Sut -Root $r1 -Ids @($similar)
    Assert-True 'a different complete UUID cannot match by a shared 35-character run' `
        ((-not ($res20.Text -cmatch 'MATCHED')) -and $res20.Code -eq 1)

    $r21 = Join-Path $WORK 'r21'
    $mixed = $NUL + 'bridgeDeviceId' + $NUL + '@"' + $LIVE_ID + '"' + $chromeBody
    New-FixtureProfile -Root $r21 -Tables @(@{ Name = '000001.ldb'; Body = $mixed }) | Out-Null
    $res21 = Invoke-Sut -Root $r21 -Ids @($LAPTOP_CHROME)
    Assert-True 'an unrequested complete ID still conflicts with a different partial candidate' `
        (($res21.Text -cmatch 'AMBIGUOUS') -and (-not ($res21.Text -cmatch 'MATCHED')) -and $res21.Code -eq 1)

    $r22 = Join-Path $WORK 'r22'
    $missing = $NUL + 'bridgeDeviceId' + $NUL + 'anonymousId' + $NUL + '@"' + $ANON_ID + '"'
    New-FixtureProfile -Root $r22 -Tables @(@{ Name = '000001.ldb'; Body = $missing }) | Out-Null
    $res22 = Invoke-Sut -Root $r22 -Ids @($ANON_ID)
    Assert-True 'a missing bridge value cannot consume the following field' `
        ((-not ($res22.Text -cmatch 'MATCHED')) -and $res22.Code -ne 0)

    $r23 = Join-Path $WORK 'r23'
    $lookalike = $NUL + 'notbridgeDeviceId' + $NUL + '@"' + $ANON_ID + '"'
    New-FixtureProfile -Root $r23 -Tables @(@{ Name = '000001.ldb'; Body = $lookalike }) | Out-Null
    $res23 = Invoke-Sut -Root $r23 -Ids @($ANON_ID)
    Assert-True 'a lookalike property name is not a bridge identity key' `
        ((-not ($res23.Text -cmatch 'MATCHED|ELSEWHERE')) -and $res23.Code -eq 2)
    Assert-True 'lookalike-only records refuse reconciliation without claiming a key was found' `
        (($res23.Text -cmatch 'REFUSING TO RECONCILE') -and
         (-not ($res23.Text -match 'present but unextractable')))

    # Readable punctuation belongs to the property token, not binary framing.
    foreach ($prefix in @('other.', 'other-', 'other/', 'other:', 'other&')) {
        $r24 = Join-Path $WORK ('r24-' + [guid]::NewGuid().ToString('N'))
        $punctuationKey = $NUL + $prefix + 'bridgeDeviceId' + $NUL + '@"' + $LIVE_ID + '"'
        New-FixtureProfile -Root $r24 -Tables @(@{ Name = '000001.ldb'; Body = $punctuationKey }) | Out-Null
        $res24 = Invoke-Sut -Root $r24 -Ids @($LIVE_ID)
        Assert-True "readable prefix '$prefix' cannot supply a key, match or elsewhere verdict" `
            (($res24.Text -cmatch 'REFUSING TO RECONCILE') -and
             (-not ($res24.Text -cmatch 'MATCHED|ELSEWHERE')) -and $res24.Code -eq 2)
    }

    foreach ($suffix in @('Backup', '_backup', '.backup', '-backup', '/backup')) {
        $r24b = Join-Path $WORK ('r24b-' + [guid]::NewGuid().ToString('N'))
        $suffixKey = $NUL + 'bridgeDeviceId' + $suffix + $NUL + '@"' + $LIVE_ID + '"'
        New-FixtureProfile -Root $r24b -Tables @(@{ Name = '000001.ldb'; Body = $suffixKey }) | Out-Null
        $res24b = Invoke-Sut -Root $r24b -Ids @($LIVE_ID)
        Assert-True "readable suffix '$suffix' cannot supply a key, match or elsewhere verdict" `
            (($res24b.Text -cmatch 'REFUSING TO RECONCILE') -and
             (-not ($res24b.Text -cmatch 'MATCHED|ELSEWHERE')) -and $res24b.Code -eq 2)
    }

    # Keep every supported key boundary while tightening the lookalike guard.
    foreach ($keyPrefix in @('', '&', $NUL, ($NUL + '&'))) {
        foreach ($keyName in @('bridgeDeviceId', 'ridgeDeviceId')) {
            $r25 = Join-Path $WORK ('r25-' + [guid]::NewGuid().ToString('N'))
            $supportedKey = $keyPrefix + $keyName + $NUL + '@"' + $LIVE_ID + '"'
            New-FixtureProfile -Root $r25 -Tables @(@{ Name = '000001.ldb'; Body = $supportedKey }) | Out-Null
            $res25 = Invoke-Sut -Root $r25 -Ids @($LIVE_ID)
            Assert-True "supported boundary length $($keyPrefix.Length) for $keyName remains exact" `
                (($res25.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID))) -and $res25.Code -eq 0)
        }
    }

    $r25b = Join-Path $WORK 'r25b'
    New-FixtureProfile -Root $r25b -Tables @(
        @{ Name = '000001.ldb'; Body = ('bridgeDeviceId"' + $LIVE_ID + '"') }
    ) | Out-Null
    $res25b = Invoke-Sut -Root $r25b -Ids @($LIVE_ID)
    Assert-True 'a direct value quote after the key remains an exact match' `
        (($res25b.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID))) -and $res25b.Code -eq 0)

    # A unique UUID within each profile is not a unique location across profiles.
    foreach ($shape in @('exact', 'partial', 'mixed')) {
        $r26 = Join-Path $WORK ('r26-' + $shape)
        $exactBody = New-RealisticBody $ANON_ID $LAPTOP_CHROME 'Duplicate Identity'
        $firstBody = $(if ($shape -eq 'partial') { $chromeBody } else { $exactBody })
        $secondBody = $(if ($shape -eq 'exact') { $exactBody } else { $chromeBody })
        New-FixtureProfile -Root $r26 -Profile 'Default' -Tables @(@{ Name = '000001.ldb'; Body = $firstBody }) | Out-Null
        New-FixtureProfile -Root $r26 -Profile 'Profile 9' -Tables @(@{ Name = '000002.ldb'; Body = $secondBody }) | Out-Null
        $res26 = Invoke-Sut -Root $r26 -Ids @($LAPTOP_CHROME)
        Assert-True "$shape duplicate-profile evidence is ambiguous and exits 1" `
            (($res26.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LAPTOP_CHROME))) -and
             (-not ($res26.Text -cmatch 'MATCHED')) -and $res26.Code -eq 1)
        Assert-True "$shape duplicate-profile verdict names both locations" `
            ($res26.Text -match 'multiple profiles:(?=[^\r\n]*Fixture / Default)(?=[^\r\n]*Fixture / Profile 9)')
    }

    # Ambiguous duplicate locations for one candidate do not hide another unique one.
    New-FixtureProfile -Root $r26 -Profile 'Profile 2' -Tables @(
        @{ Name = '000003.ldb'; Body = (New-RealisticBody $ANON_ID $LIVE_ID 'Unique Identity') }
    ) | Out-Null
    $res27 = Invoke-Sut -Root $r26 -Ids @($LAPTOP_CHROME, $LIVE_ID)
    Assert-True 'a different uniquely located candidate remains matched with exit 0' `
        (($res27.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID) + '\s+-> Fixture / Profile 2')) -and
         ($res27.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LAPTOP_CHROME))) -and $res27.Code -eq 0)
    Assert-True 'a successful mixed-result exit does not promote the duplicate candidate' `
        (-not ($res27.Text -cmatch ('MATCHED\*?\s+' + [regex]::Escape($LAPTOP_CHROME))))

    $r28 = Join-Path $WORK 'r28'
    $multipleEmbedded = $NUL + 'bridgeDeviceId' + $NUL + '@"x' + $LIVE_ID + ' / ' + $LAPTOP_CHROME + 'x"'
    New-FixtureProfile -Root $r28 -Tables @(@{ Name = '000001.ldb'; Body = $multipleEmbedded }) | Out-Null
    $res28 = Invoke-Sut -Root $r28 -Ids @($LIVE_ID)
    Assert-True 'multiple full IDs in one malformed quoted value conflict even with one requested ID' `
        (($res28.Text -cmatch 'CONFLICTING') -and
         ($res28.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LIVE_ID))))
    Assert-True 'a malformed multi-ID value cannot provide a match or successful exit' `
        ((-not ($res28.Text -cmatch 'MATCHED')) -and $res28.Code -eq 1)

    # Every intrinsic full ID matters, even when only another partial ID is requested.
    $r29 = Join-Path $WORK 'r29'
    $oneFullPlusPartial = $NUL + 'bridgeDeviceId' + $NUL + '@"x' + $LIVE_ID + 'y' + $LAPTOP_CHROME.Substring(0, 20) + 'z"'
    New-FixtureProfile -Root $r29 -Tables @(@{ Name = '000001.ldb'; Body = $oneFullPlusPartial }) | Out-Null
    foreach ($requested in @(@($LAPTOP_CHROME), @($LAPTOP_CHROME, $LIVE_ID))) {
        $res29 = Invoke-Sut -Root $r29 -Ids $requested
        Assert-True 'one full competing ID defeats another requested partial regardless of candidate list' `
            (($res29.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LAPTOP_CHROME))) -and
             (-not ($res29.Text -cmatch 'MATCHED')) -and $res29.Code -eq 1)
    }
    $res29positive = Invoke-Sut -Root $r19 -Ids @($LIVE_ID)
    Assert-True 'intrinsic collection keeps a lone embedded requested ID partial, never exact' `
        (($res29positive.Text -cmatch ('MATCHED\*\s+' + [regex]::Escape($LIVE_ID))) -and
         (-not ($res29positive.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID)))) -and $res29positive.Code -eq 0)
    $res28both = Invoke-Sut -Root $r28 -Ids @($LIVE_ID, $LAPTOP_CHROME)
    Assert-True 'two embedded full IDs remain ambiguous when both are requested' `
        (($res28both.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LIVE_ID))) -and
         ($res28both.Text -cmatch ('AMBIGUOUS ' + [regex]::Escape($LAPTOP_CHROME))) -and
         (-not ($res28both.Text -cmatch 'MATCHED')) -and $res28both.Code -eq 1)

    # Latin-1 reading must not mistake UTF-8 bytes for binary key delimiters.
    $utf8Accent = [Text.Encoding]::GetEncoding(28591).GetString([Text.Encoding]::UTF8.GetBytes([string][char]0x00E9))
    foreach ($adjacent in @($utf8Accent, [string][char]0xE9, [string][char]0x80)) {
        foreach ($side in @('prefix', 'suffix', 'framing')) {
            $r30 = Join-Path $WORK ('r30-' + [guid]::NewGuid().ToString('N'))
            $unicodeKey = switch ($side) {
                'prefix' { $NUL + $adjacent + 'bridgeDeviceId' + $NUL + '@"' + $LIVE_ID + '"' }
                'suffix' { $NUL + 'bridgeDeviceId' + $adjacent + $NUL + '@"' + $LIVE_ID + '"' }
                'framing' { $NUL + 'bridgeDeviceId' + $NUL + $adjacent + '@"' + $LIVE_ID + '"' }
            }
            New-FixtureProfile -Root $r30 -Tables @(@{ Name = '000001.ldb'; Body = $unicodeKey }) | Out-Null
            $res30 = Invoke-Sut -Root $r30 -Ids @($LIVE_ID)
            Assert-True "high-byte $side length $($adjacent.Length) stays unknown with refusal2" `
                (($res30.Text -cmatch 'REFUSING TO RECONCILE') -and
                 (-not ($res30.Text -cmatch 'MATCHED|ELSEWHERE')) -and $res30.Code -eq 2)
        }
    }

    # Pin the supported boundary, not a perpetually expanding scan window.
    foreach ($length in @(24, 25)) {
        $r31 = Join-Path $WORK ('r31-' + $length)
        $framed = $NUL + 'bridgeDeviceId' + ($NUL * $length) + '"' + $LIVE_ID + '"'
        New-FixtureProfile -Root $r31 -Tables @(@{ Name = '000001.ldb'; Body = $framed }) | Out-Null
        $res31 = Invoke-Sut -Root $r31 -Ids @($LIVE_ID)
        if ($length -eq 24) {
            Assert-True '24 framing bytes remain supported and exact' `
                (($res31.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID))) -and $res31.Code -eq 0)
        } else {
            Assert-True '25 framing bytes refuse rather than return a negative location' `
                (($res31.Text -cmatch 'REFUSING TO RECONCILE') -and
                 (-not ($res31.Text -cmatch 'MATCHED|ELSEWHERE')) -and $res31.Code -eq 2)
            Assert-True 'recognized unsupported key retains KeySeen but not ValueSeen' `
                (($res31.Text -match 'KeySeen\s+: True') -and ($res31.Text -match 'ValueSeen\s+: False'))
        }
    }

    foreach ($unsupported in @('""', '"unterminated', ('"' + ('x' * 121) + '"'))) {
        $r32 = Join-Path $WORK ('r32-' + [guid]::NewGuid().ToString('N'))
        New-FixtureProfile -Root $r32 -Tables @(
            @{ Name = '000001.ldb'; Body = ($NUL + 'bridgeDeviceId' + $NUL + $unsupported) }
        ) | Out-Null
        $res32 = Invoke-Sut -Root $r32 -Ids @($LIVE_ID)
        Assert-True 'empty, unterminated and overlong windows cannot give a negative location' `
            (($res32.Text -cmatch 'REFUSING TO RECONCILE') -and
             (-not ($res32.Text -cmatch 'MATCHED|ELSEWHERE')) -and $res32.Code -eq 2)
    }

    # A supported record elsewhere must not hide unresolved evidence for a nonmatch.
    $r33 = Join-Path $WORK 'r33'
    New-FixtureProfile -Root $r33 -Tables @(
        @{ Name = '000001.ldb'; Body = (New-RealisticBody $ANON_ID $LIVE_ID 'Known Profile') }
    ) | Out-Null
    New-FixtureProfile -Root $r33 -Profile 'Profile 9' -Tables @(
        @{ Name = '000002.ldb'; Body = ($NUL + 'bridgeDeviceId' + ($NUL * 25) + '"' + $LAPTOP_CHROME + '"') }
    ) | Out-Null
    $res33 = Invoke-Sut -Root $r33 -Ids @($LAPTOP_CHROME)
    Assert-True 'an unmatched candidate stays UNKNOWN when another profile is unsupported' `
        (($res33.Text -cmatch ('UNKNOWN ' + [regex]::Escape($LAPTOP_CHROME))) -and
         (-not ($res33.Text -cmatch 'MATCHED|ELSEWHERE')) -and $res33.Code -eq 2)
    $res33mixed = Invoke-Sut -Root $r33 -Ids @($LIVE_ID, $LAPTOP_CHROME)
    Assert-True 'a unique supported match survives alongside a different UNKNOWN candidate' `
        (($res33mixed.Text -cmatch ('MATCHED\s+' + [regex]::Escape($LIVE_ID))) -and
         ($res33mixed.Text -cmatch ('UNKNOWN ' + [regex]::Escape($LAPTOP_CHROME))) -and $res33mixed.Code -eq 0)

    # ---- fixture 7: a settings dir that exists but is empty ---------------
    # "Exists" was being counted as "searched", so the report claimed to have
    # read a location where nothing was opened - the scope of "not found"
    # quietly growing to cover a place never read.
    $r7 = Join-Path $WORK 'r7'
    New-FixtureProfile -Root $r7 -Profile 'Default' -Version '1.0.91' -Tables @() | Out-Null

    $res7 = Invoke-Sut -Root $r7 -Ids @($LIVE_ID)
    Assert-True 'an existing but empty settings dir is reported EMPTY, not searched' `
        ($res7.Text -cmatch 'EMPTY') `
        'a directory holding no files was counted as one that had been read'
    Assert-True 'an empty settings dir still refuses to reconcile' `
        ($res7.Text -match 'REFUSING TO RECONCILE')
    Assert-True 'the empty-dir case also exits 2' ($res7.Code -eq 2) `
        "got exit $($res7.Code)"

    # ---- fixture 5: no reconciliation requested ---------------------------
    $res5 = Invoke-Sut -Root $r1
    Assert-True 'listing without -DeviceId still exits 0' ($res5.Code -eq 0)
    Assert-True 'listing without -DeviceId prints no reconciliation section' `
        (-not ($res5.Text -match 'Reconciliation'))

} finally {
    $resolvedWork = [IO.Path]::GetFullPath($WORK)
    $resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if (-not $resolvedWork.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedWork) -notlike 'identify-browsers-test-*') {
        throw 'Refusing cleanup outside the generated test workspace'
    }
    Remove-Item -LiteralPath $resolvedWork -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ''
Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
