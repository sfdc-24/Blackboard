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
    Remove-Item -Recurse -Force $WORK -ErrorAction SilentlyContinue
}

Write-Output ''
Write-Output ('RESULT passed=' + $script:Passed + ' failed=' + $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
