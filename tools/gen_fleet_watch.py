"""Generate scripts/fleet_watch.ps1 from scripts/wa_watch.ps1.

Run from the repository root:  python tools/gen_fleet_watch.py

fleet_watch.ps1 is GENERATED, not hand-edited. Edit wa_watch.ps1 and re-run this.

It lived in a scratch directory until 2026-09-08, which meant the generator for a
committed file could vanish with a temp sweep. The assertions at the bottom exist
because a previous regeneration silently dropped the parameter guards while the
commit message said they were present -- so the generator now refuses to write a
file that has lost them.
"""
import io, re, sys

src = io.open('scripts/wa_watch.ps1', encoding='utf-8').read()
old = io.open('scripts/fleet_watch.ps1', encoding='utf-8').read()

def grab(text, name):
    i = text.index('function ' + name + ' {')
    depth = 0; j = i
    while True:
        if text[j] == '{': depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0: return text[i:j+1]
        j += 1

field = grab(old, 'Field')
addr  = grab(old, 'Addressed')
fmt   = grab(old, 'Format-Line')
head  = old[:old.index('param(')]

params = "\n".join([
"param(",
"  # Guards restored and ASSERTED by the generator. A previous regeneration",
"  # dropped these while a commit message claimed they were present.",
"  [ValidatePattern('^[a-z0-9][a-z0-9._-]{0,39}$')]",
"  [string]$Tag = 'claude-code-cli',",
"  [ValidateRange(5, 3600)]",
"  [int]$PollSeconds = 90,",
"  [ValidateRange(0, 100)]",
"  [int]$CatchUpMax = 6,",
"  [switch]$IncludeCc,",
"  [string]$StateFile,",
"  [switch]$Reset,",
"  [switch]$Once",
")",
])

body = src[src.index("$ErrorActionPreference = 'Continue'"):]
body = body.replace("Get-DefaultStatePath -Leaf 'wa_watch.state.json'",
                    "Get-DefaultStatePath -Leaf ('fleet_watch.' + $Tag + '.state.json')")
body = body.replace('"wa_watch_"', '"fleet_watch_"')
body = body.replace('WA watch', 'Fleet watch')
body = body.replace('" - cursor set at row " + $lastData + ", polling',
                    '" - cursor set at row " + $lastData + " for " + $Tag + ", polling')
body = body.replace('" - nothing missed, polling every "', '" for " + $Tag + " - nothing missed, polling every "')
body = body.replace('" messages arrived while nothing was listening', '" addressed rows arrived while nothing was listening')
body = body.replace('" message(s) arrived while nothing was listening', '" addressed row(s) arrived while nothing was listening')

# fleet has no -Backfill: cold mode selects nothing.
body = body.replace("      $mode = 'cold'; $limit = $Backfill; $from = 1",
                    "      $mode = 'cold'; $limit = 0; $from = 1")

qualifies = "\n".join([
"function Test-RowQualifies {",
"  # Rows somebody chose to address to this tag. NOT cc=ALL, which is most of the",
"  # board and would bury the addressed rows the way a notification stream must",
"  # never do; -IncludeCc opts into that. Rows written BY this tag are skipped,",
"  # because a watcher that reports our own writes back is the session talking to",
"  # itself -- wa_watch.ps1 shipped with that bug and it looked like real traffic.",
"  param($Row)",
"  $pay = [string]$Row[5]",
"  if ($pay -notlike 'BCB|*') { return $false }",
"  if (([string]$Row[2]) -eq $script:WatchTag) { return $false }",
"  if ((Field $pay 'from') -eq $script:WatchTag) { return $false }",
"  return (Addressed $pay $script:WatchTag $script:WatchWithCc)",
"}",
])

body = (body[:body.index('function Test-RowQualifies {')]
        + field + "\n\n" + addr + "\n\n" + qualifies + "\n\n" + fmt + "\n"
        + body[body.index('function Get-ModePrefix {'):])

# Tag/IncludeCc reach the qualifier through script scope, so Get-PlanEntries stays
# identical in both watchers and cannot drift.
body = body.replace("$bus = Join-Path $PSScriptRoot 'bus.ps1'",
                    "$script:WatchTag = $Tag\n$script:WatchWithCc = [bool]$IncludeCc\n\n$bus = Join-Path $PSScriptRoot 'bus.ps1'")

out = head + params + "\n\n" + body
io.open('scripts/fleet_watch.ps1', 'w', encoding='utf-8', newline='\n').write(out)

problems = []
if out.count('ValidateRange') != 2: problems.append('ValidateRange count ' + str(out.count('ValidateRange')))
if 'ValidatePattern' not in out: problems.append('ValidatePattern missing')
if 'Enter-WatchLock' not in out: problems.append('writer lock missing')
if 'COULD NOT READ THE BOARD' not in out: problems.append('fail-loud -Once missing')
if 'Test-PendingMatchesPlan' not in out: problems.append('plan recompute missing')
if 'Get-PlanEntries' not in out: problems.append('pure planner missing')
if '$Backfill' in out: problems.append('fleet still references -Backfill')
# recovery invariants, to CODEX-PR34-CD05-RECOVERY-GO
if 'Resolve-PendingTransition' not in out: problems.append('single recovery state machine missing')
if 'Test-PendingTransition' in out: problems.append('the old refuse-on-presence check came back')
if out.count('Get-SaveStopReason') != 5: problems.append('save results unsurfaced: Get-SaveStopReason x' + str(out.count('Get-SaveStopReason')))
if 'WriteAllBytes($StateFile' in out: problems.append('a second recovery writer appeared in the watcher')
d = io.open('scripts/fleet_watch.ps1', 'rb').read()
ctrl = [b for b in set(d) if b < 32 and b not in (9, 10, 13)]
if ctrl: problems.append('control bytes ' + str(ctrl))
if problems:
    print('GENERATOR ASSERTIONS FAILED:')
    for p in problems: print('  -', p)
    sys.exit(1)
print('fleet_watch.ps1 generated; generator assertions passed')
