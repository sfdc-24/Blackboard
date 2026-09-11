# Run the ORDER supervisor against a FIXTURE board and emit a normalised
# artifact for cross-host comparison.
#
# WHY THIS EXISTS
#   Step 3 of docs/ORDER-PORT-PLAN.md. "It parses on Linux" and "the cmdlets
#   exist" are both true and neither says the supervisor DECIDES the same
#   things. The only evidence that means anything is equal output on equal
#   input, produced on both hosts and diffed.
#
# WHY OBSERVE MODE AND A FIXTURE
#   The supervisor refuses to append with a fixture (fixture_append_forbidden)
#   and refuses Execute with a fixture (fixture_execute_forbidden). Those rails
#   are its own, not mine, so this cannot touch the live board or run a provider
#   even by mistake.
#
# WHAT IS NORMALISED, AND WHY THAT IS THE RISKY PART
#   A normaliser that strips too much turns a real difference into a match. So
#   only provably environmental values are replaced, each with a NAMED token so
#   the diff shows what was removed rather than hiding it:
#     - run ids and work ids (random per run)
#     - timestamps (wall clock)
#     - absolute paths, which differ by host BY DESIGN
#     - pids, hostname, username
#   Anything else - statuses, codes, row ids, counts, ordering, decisions -
#   is compared verbatim. If the two hosts disagree about a decision, this
#   shows it.
#
# RUN
#   pwsh -NoProfile -File tests/order_fixture_crosscheck.ps1 -OutFile out.json
param(
    [string]$FixturePath,
    [string]$OutFile
)
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $FixturePath) {
    $FixturePath = Join-Path $repoRoot 'tests/fixtures/order_supervisor_board.json'
}
$FixturePath = [IO.Path]::GetFullPath($FixturePath)
if (-not (Test-Path -LiteralPath $FixturePath -PathType Leaf)) {
    Write-Output ('fixture missing: ' + $FixturePath); exit 1
}
if (-not $OutFile) { $OutFile = Join-Path ([IO.Path]::GetTempPath()) 'order-crosscheck.json' }

$runner = Join-Path $repoRoot 'scripts/order_supervisor.ps1'
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('order-crosscheck-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

# TWO SCENARIOS, because one of them proves almost nothing.
#
#   seeded  - default behaviour. Pass one seeds the cursor at the board tail,
#             pass two finds nothing after it. This is the real cold-start path
#             and it must match, but it would look identical on both hosts even
#             if every admission rule were broken.
#   replay  - -ReplayHistorical on fresh state. Skips the tail seed AND the age
#             gate, so the supervisor actually evaluates every row and emits its
#             admission diagnostics. THIS is the comparison that has teeth.
$scenarios = @(
    @{ name = 'seeded'; replay = $false; passes = 2 },
    @{ name = 'replay'; replay = $true;  passes = 2 }
)

$passes = @()
foreach ($scenario in $scenarios) {
    $scenarioRoot = Join-Path $sandbox $scenario.name
    New-Item -ItemType Directory -Path $scenarioRoot -Force | Out-Null
    $statePath = Join-Path $scenarioRoot 'state.json'
    $logPath = Join-Path $scenarioRoot 'order.log'
    $workspace = Join-Path $scenarioRoot 'workspace'
    New-Item -ItemType Directory -Path $workspace -Force | Out-Null

    foreach ($n in 1..$scenario.passes) {
        $stdout = ''
        $threw = ''
        $runnerExit = $null
        $common = @{
            Mode = 'Observe'
            BoardFixturePath = $FixturePath
            StatePath = $statePath
            LogPath = $logPath
            WorkspacePath = $workspace
            MaxOrderAgeMinutes = 10080
        }
        try {
            $global:LASTEXITCODE = $null
            if ($scenario.replay) {
                $stdout = (& $runner @common -ReplayHistorical *>&1 | Out-String)
            } else {
                $stdout = (& $runner @common *>&1 | Out-String)
            }
            $runnerExit = $LASTEXITCODE
        } catch {
            $runnerExit = $LASTEXITCODE
            $threw = [string]$_.Exception.Message
        }
        $passes += [pscustomobject][ordered]@{
            scenario = $scenario.name
            pass = $n
            stdout = $stdout.Trim()
            threw = $threw
            exit_code = $runnerExit
        }
    }
    # Each scenario keeps its own log, read back below by scenario name.
    Set-Variable -Name ('log_' + $scenario.name) -Value $logPath -Scope Script
    Set-Variable -Name ('state_' + $scenario.name) -Value $statePath -Scope Script
}

function ConvertTo-Normalised {
    param([string]$Text, [switch]$StateDocument)
    if (-not $Text) { return '' }
    if ($StateDocument) {
        # Preserve fixture-derived timestamps and IDs. Applying the broad text
        # substitutions below would erase differences in cursor.timestamp.
        $parse = @{ InputObject = $Text; ErrorAction = 'Stop' }
        if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
            $parse.DateKind = 'String'
        } elseif ($PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
            throw 'State comparison requires Windows PowerShell 5.1 or PowerShell 7.5+'
        }
        $stateObject = ConvertFrom-Json @parse
        foreach ($section in @('last_poll', 'seen', 'success', 'error')) {
            $entry = $stateObject.$section
            if ($null -ne $entry -and $null -ne $entry.PSObject.Properties['at'] -and $entry.at) {
                $entry.at = '<TS>'
            }
        }
        if ($null -ne $stateObject.last_poll) {
            foreach ($field in @('run_id', 'identity', 'user_profile')) {
                if ($null -ne $stateObject.last_poll.PSObject.Properties[$field]) {
                    $stateObject.last_poll.$field = $(switch ($field) {
                        'run_id' { '<RUNID32>' }
                        'identity' { '<EXECUTION_IDENTITY>' }
                        'user_profile' { '<USER_PROFILE>' }
                    })
                }
            }
        }
        return ($stateObject | ConvertTo-Json -Depth 64)
    }
    $s = $Text

    # Absolute paths differ by host by design. Replaced FIRST, because a path
    # can contain a 32-hex directory name that the run-id rule would otherwise
    # eat, producing a diff that blames the wrong thing.
    $s = $s -replace [regex]::Escape($sandbox), '<SANDBOX>'
    $s = $s -replace [regex]::Escape($repoRoot), '<REPO>'
    $s = $s -replace '(?i)[a-z]:\\[^"'',\s]+', '<ABSPATH>'
    $s = $s -replace '(?<![\w/])/(?:tmp|home|opt|usr|var|etc)/[^"'',\s]*', '<ABSPATH>'

    # Identifiers and clocks.
    $s = $s -replace '\b[0-9a-fA-F]{32}\b', '<RUNID32>'
    $s = $s -replace '\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b', '<GUID>'
    $s = $s -replace '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z', '<TS>'
    $s = $s -replace '"(pid|process_id)"\s*:\s*\d+', '"$1":<PID>'

    # Host identity.
    $s = $s -replace [regex]::Escape([Environment]::MachineName), '<HOST>'
    if ($env:USER) { $s = $s -replace [regex]::Escape($env:USER), '<USER>' }
    if ($env:USERNAME) { $s = $s -replace [regex]::Escape($env:USERNAME), '<USER>' }

    # Line endings are not a behavioural difference.
    $s = $s -replace "`r`n", "`n"
    return $s.Trim()
}

# The log carries a lot of environmental detail. The DECISIONS in it are the
# event names, codes and row ids, in order - that is what has to match, and
# comparing only that keeps a formatting change from masquerading as a
# behaviour change. Row ids are NOT normalised: they come from the fixture, so
# two hosts disagreeing about which row they acted on is exactly the finding
# this harness exists to surface.
function Get-Decisions {
    param([string]$LogFile)
    $out = @()
    if (-not (Test-Path -LiteralPath $LogFile)) { return $out }
    foreach ($line in ([IO.File]::ReadAllText($LogFile) -split "`n")) {
        if (-not $line.Trim()) { continue }
        try {
            $obj = $line | ConvertFrom-Json -ErrorAction Stop
            $out += [pscustomobject][ordered]@{
                event = $obj.event
                level = $obj.level
                code  = $obj.code
                row   = $obj.row_id
            }
        } catch {
            $out += [pscustomobject][ordered]@{ event = '<UNPARSEABLE>'; level = ''; code = ''; row = '' }
        }
    }
    return $out
}

$decisions = [ordered]@{}
$states = [ordered]@{}
foreach ($scenario in $scenarios) {
    $decisions[$scenario.name] = @(Get-Decisions -LogFile (Get-Variable -Name ('log_' + $scenario.name) -ValueOnly))
    $sp = Get-Variable -Name ('state_' + $scenario.name) -ValueOnly
    $states[$scenario.name] = $(if (Test-Path -LiteralPath $sp) { ConvertTo-Normalised ([IO.File]::ReadAllText($sp)) -StateDocument } else { '' })
}
$events = @()
foreach ($k in $decisions.Keys) { $events += $decisions[$k] }

$report = [pscustomobject][ordered]@{
    platform      = $(if (Test-Path Variable:IsWindows) { if ($IsWindows) { 'Windows' } else { 'Linux' } } else { 'Windows' })
    ps_version    = $PSVersionTable.PSVersion.ToString()
    ps_edition    = $PSVersionTable.PSEdition
    fixture_sha   = (Get-FileHash -LiteralPath $FixturePath -Algorithm SHA256).Hash
    passes        = @($passes | ForEach-Object {
                        [pscustomobject][ordered]@{
                            scenario = $_.scenario
                            pass   = $_.pass
                            stdout = ConvertTo-Normalised $_.stdout
                            threw  = ConvertTo-Normalised $_.threw
                            exit_code = $_.exit_code
                        } })
    state         = $states
    log_decisions = $decisions
}

$json = $report | ConvertTo-Json -Depth 8
$destination = [IO.Path]::GetFullPath($OutFile)
$temporary = $destination + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
$published = $false
try {
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::ReadAllText($temporary) -cne $json) { throw 'artifact temporary readback differs' }
    if ([IO.File]::Exists($destination)) {
        # 5.1 binds $null to an empty string here; NullString is a CLR null.
        [IO.File]::Replace($temporary, $destination, [System.Management.Automation.Language.NullString]::Value)
    } else {
        [IO.File]::Move($temporary, $destination)
    }
    if ([IO.File]::ReadAllText($destination) -cne $json) { throw 'artifact destination readback differs' }
    $published = $true
} catch {
    Write-Output ('ARTIFACT_WRITE_FAILED: ' + $_.Exception.Message)
} finally {
    if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    # Only remove the unique scratch directory created by this invocation.
    $scratchRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $resolvedSandbox = [IO.Path]::GetFullPath($sandbox)
    if ($resolvedSandbox.StartsWith($scratchRoot, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolvedSandbox) -match '^order-crosscheck-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolvedSandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if (-not $published) { exit 1 }

Write-Output ('platform      : ' + $report.platform + ' / ' + $report.ps_version + ' (' + $report.ps_edition + ')')
Write-Output ('fixture sha   : ' + $report.fixture_sha)
foreach ($k in $decisions.Keys) {
    Write-Output ('scenario ' + $k + ' : ' + @($decisions[$k]).Count + ' decisions')
    foreach ($e in $decisions[$k]) {
        Write-Output ('   ' + $e.event + $(if ($e.code) { ' [' + $e.code + ']' } else { '' }) + $(if ($e.row) { ' row=' + $e.row } else { '' }))
    }
}
Write-Output ('wrote ' + $OutFile)

# THE HARNESS MUST NOT EXIT 0 OVER A RUN THAT DID NOT HAPPEN.
#
# It used to produce an artifact and exit 0 whatever the passes did, leaving the
# differ as the only thing that could notice - and the differ had no failure
# oracle either, so on PowerShell 7.4.6 every pass errored and the whole chain
# reported success. Two places that each assumed the other was checking.
$failed = @()
foreach ($p in $passes) {
    $label = $p.scenario + ' pass ' + $p.pass
    if ($p.threw) { $failed += ($label + ': threw'); continue }
    if ($null -eq $p.exit_code -or $p.exit_code -isnot [int] -or $p.exit_code -ne 0) {
        $failed += ($label + ': runner exit_code is not integer zero')
    }
    $last = ($p.stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
    if (-not $last) { $failed += ($label + ': no stdout'); continue }
    try {
        $verdict = $last | ConvertFrom-Json -ErrorAction Stop
        if ($verdict.ok -isnot [bool] -or $verdict.ok -ne $true) { $failed += ($label + ': ok is not Boolean true') }
    } catch {
        $failed += ($label + ': stdout is not the JSON verdict')
    }
}
foreach ($k in $decisions.Keys) {
    if (@($decisions[$k] | Where-Object { $_.event -eq 'poll_started' }).Count -ne 2) {
        $failed += ($k + ': expected two poll_started events')
    }
    try {
        $parsedState = $states[$k] | ConvertFrom-Json -ErrorAction Stop
        if ($parsedState -isnot [pscustomobject] -or @($parsedState.PSObject.Properties).Count -eq 0) {
            throw 'missing persisted state'
        }
    } catch { $failed += ($k + ': missing or invalid persisted state') }
    foreach ($e in $decisions[$k]) {
        if ($e.event -eq 'run_error' -or $e.event -eq '<UNPARSEABLE>') {
            $failed += ($k + ': logged ' + $e.event + ' [' + $e.code + ']')
        }
    }
}
if ($failed.Count -gt 0) {
    Write-Output ''
    Write-Output 'THIS RUN DID NOT PRODUCE A USABLE COMPARISON:'
    foreach ($f in $failed) { Write-Output ('  - ' + $f) }
    exit 1
}
exit 0
