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
    Zero or more deviceIds from list_connected_browsers. Each is reported as
    MATCHED (found in a local profile) or STALE (no local install holds it).

.EXAMPLE
    .\identify_claude_browsers.ps1
    .\identify_claude_browsers.ps1 -DeviceId f1315438-... , 295592ab-...
#>
[CmdletBinding()]
param(
    [string[]]$DeviceId = @()
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
        $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::ASCII)
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

function Get-ProfileIdentity {
    param([string]$ProfilePath)
    $settings = Join-Path $ProfilePath "Local Extension Settings\$EXT_ID"
    $result = @{ DeviceId = $null; DisplayName = $null; Unreadable = 0 }
    if (-not (Test-Path $settings)) { return $result }

    # Newest first: the current value lives in the most recently written table,
    # and an older compacted .ldb can still carry a superseded deviceId.
    $files = Get-ChildItem $settings -File -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime -Descending
    foreach ($f in $files) {
        $txt = Read-SharedText $f.FullName
        if ($null -eq $txt) { $result.Unreadable++; continue }
        if (-not $result.DeviceId) {
            $v = Get-SettingAfterKey $txt 'bridgeDeviceId'
            if ($v -match '^[0-9a-fA-F-]{36}$') { $result.DeviceId = $v }
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
                $result.DisplayName = $v
            }
        }
    }
    return $result
}

$rows = @()

foreach ($b in $BROWSERS) {
    if (-not (Test-Path $b.Root)) { continue }

    $procs = @(Get-Process -Name $b.Process -ErrorAction SilentlyContinue)
    $titles = @($procs | Where-Object { $_.MainWindowTitle } | ForEach-Object { $_.MainWindowTitle })

    $profiles = @(Get-ChildItem $b.Root -Directory -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -eq 'Default' -or $_.Name -like 'Profile *' })

    foreach ($p in $profiles) {
        $manifest = $null
        $extDir = Join-Path $p.FullName "Extensions\$EXT_ID"
        if (Test-Path $extDir) {
            $manifest = Get-ChildItem $extDir -Recurse -Filter 'manifest.json' -ErrorAction SilentlyContinue |
                        Select-Object -First 1
        }
        if (-not $manifest) { continue }   # extension not installed in this profile

        $version = '?'
        try { $version = (Get-Content $manifest.FullName -Raw | ConvertFrom-Json).version } catch {}

        $id = Get-ProfileIdentity $p.FullName

        $rows += [PSCustomObject]@{
            Browser     = $b.Name
            Profile     = $p.Name
            ExtVersion  = $version
            DeviceId    = $(if ($id.DeviceId) { $id.DeviceId } else { '(none stored)' })
            DisplayName = $(if ($id.DisplayName) { $id.DisplayName } else { '' })
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
    $stale = 0
    foreach ($d in $DeviceId) {
        $hit = @($rows | Where-Object { $_.DeviceId -eq $d })
        if ($hit.Count -gt 0) {
            $h = $hit[0]
            $live = $(if ($h.Running -and $h.Windows -gt 0) { 'LIVE' } else { 'installed but not running' })
            Write-Output ("  MATCHED  {0}  -> {1} / {2}  [{3}]  {4}" -f $d, $h.Browser, $h.Profile, $live, $h.DisplayName)
        } else {
            $stale++
            Write-Output ("  STALE    {0}  -> no local profile holds this id" -f $d)
        }
    }
    Write-Output ''
    Write-Output ("  $stale of $($DeviceId.Count) registrations have no browser behind them on this machine.")
    if ($stale -gt 0) {
        Write-Output '  A browser action sent to one of those will appear to do nothing.'
    }
}

# Exit non-zero when asked to reconcile and nothing matched: a caller that
# cannot find ANY live browser should fail rather than pick one at random.
if ($DeviceId.Count -gt 0) {
    $matched = @($rows | Where-Object { $DeviceId -contains $_.DeviceId })
    if ($matched.Count -eq 0) { exit 1 }
}
exit 0
