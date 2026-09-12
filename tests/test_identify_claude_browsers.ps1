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
        [System.IO.File]::WriteAllText((Join-Path $sdir $t.Name), $t.Body, [System.Text.Encoding]::ASCII)
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
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $SUT,
                     '-ScanRoot', $Root, '-ScanName', 'Fixture')
        # Only pass -DeviceId when there is something to pass: a bare switch
        # with an empty array makes powershell.exe reject the command line,
        # which is a harness failure that reads like a tool failure.
        if ($Ids.Count -gt 0) { $argList += @('-DeviceId', ($Ids -join ',')) }

        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList `
             -NoNewWindow -Wait -PassThru -RedirectStandardOutput $o -RedirectStandardError $e
        $text = (Get-Content $o -Raw -ErrorAction SilentlyContinue)
        $err  = (Get-Content $e -Raw -ErrorAction SilentlyContinue)
        if ($null -eq $text) { $text = '' }
        if ($null -eq $err)  { $err  = '' }
        return [PSCustomObject]@{ Text = ($text + $err); Code = $p.ExitCode }
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
