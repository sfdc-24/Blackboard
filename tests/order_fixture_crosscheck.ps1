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
$ErrorActionPreference = 'Continue'

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
        $common = @{
            Mode = 'Observe'
            BoardFixturePath = $FixturePath
            StatePath = $statePath
            LogPath = $logPath
            WorkspacePath = $workspace
            MaxOrderAgeMinutes = 10080
        }
        try {
            if ($scenario.replay) {
                $stdout = (& $runner @common -ReplayHistorical *>&1 | Out-String)
            } else {
                $stdout = (& $runner @common *>&1 | Out-String)
            }
        } catch {
            $threw = [string]$_.Exception.Message
        }
        $passes += [pscustomobject][ordered]@{
            scenario = $scenario.name
            pass = $n
            stdout = $stdout.Trim()
            threw = $threw
        }
    }
    # Each scenario keeps its own log, read back below by scenario name.
    Set-Variable -Name ('log_' + $scenario.name) -Value $logPath -Scope Script
    Set-Variable -Name ('state_' + $scenario.name) -Value $statePath -Scope Script
}

function ConvertTo-Normalised {
    param([string]$Text)
    if (-not $Text) { return '' }
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
    $states[$scenario.name] = $(if (Test-Path -LiteralPath $sp) { ConvertTo-Normalised ([IO.File]::ReadAllText($sp)) } else { '' })
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
                        } })
    state         = $states
    log_decisions = $decisions
}

$json = $report | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($OutFile, $json, (New-Object Text.UTF8Encoding($false)))
Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ('platform      : ' + $report.platform + ' / ' + $report.ps_version + ' (' + $report.ps_edition + ')')
Write-Output ('fixture sha   : ' + $report.fixture_sha)
foreach ($k in $decisions.Keys) {
    Write-Output ('scenario ' + $k + ' : ' + @($decisions[$k]).Count + ' decisions')
    foreach ($e in $decisions[$k]) {
        Write-Output ('   ' + $e.event + $(if ($e.code) { ' [' + $e.code + ']' } else { '' }) + $(if ($e.row) { ' row=' + $e.row } else { '' }))
    }
}
Write-Output ('wrote ' + $OutFile)
