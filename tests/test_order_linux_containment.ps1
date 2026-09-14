# Containment: the fork-after-scan case, which a /proc walk cannot cover.
#
# WHAT CODEX REPRODUCED against 373f9da
#   A child forked AFTER the first /proc scan, whose parent then died, is
#   reparented away from the root. Re-walking from the root can never find it,
#   however many sweeps are added - so Invoke-PosixTreeKill returned 0 while
#   that child stayed alive and wrote its post-timeout marker. Returning success
#   while a process survives is the same false-green shape as a differ that
#   compares two failures and calls them a match.
#
# WHY A PROCESS GROUP FIXES IT AND A BETTER WALK CANNOT
#   A child inherits the pgid at fork and KEEPS it when its parent dies. So
#   kill(-pgid) reaches the whole group atomically, regardless of who reparented
#   to whom. There is no snapshot to race against. A walk is a search over a
#   structure that changes while you search it.
#
# THE TEST THAT MATTERS is the last one: it forks its late child only AFTER the
# first scan has happened, then orphans it. Under the old walk that child
# survived. If this file ever passes with the walk restored, the test is wrong.
$ErrorActionPreference = 'Continue'
$script:Pass = 0
$script:Fail = 0
$script:Failures = @()

function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) {
        $script:Pass++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Fail++
        $script:Failures += $Name
        Write-Output ('FAIL ' + $Name + $(if ($Detail) { ': ' + $Detail } else { '' }))
    }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runnerPath = Join-Path $repoRoot 'scripts/order_supervisor.ps1'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$errors)
Assert-True 'runner parses' (@($errors).Count -eq 0)
# THIS LIST IS A DEPENDENCY MANIFEST, not a wish list. Each function is lifted
# out of the runner in isolation, so anything a listed function CALLS must be
# listed too or the call dies with "not recognized" mid-test. Adding a helper to
# Invoke-PosixGroupKill without adding it here turned this suite red while the
# runner itself was correct - the failure looked like a containment regression
# and was a loading one.
foreach ($fn in @('Test-OnWindows', 'Get-PosixProcessGroupId', 'Invoke-PosixGroupKill',
                  'Resolve-PosixKillBinary', 'Get-PosixProcessGroupMemberId',
                  'Get-PosixChildProcessId', 'Test-PosixProcessAlive',
                  'Invoke-PosixTreeKill', 'Invoke-ProcessTreeKill', 'Invoke-TaskkillTree')) {
    $defs = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $fn
    }, $true))
    if ($defs.Count -ne 1) { Assert-True ('runner defines one ' + $fn) $false; continue }
    Invoke-Expression $defs[0].Extent.Text
}

Write-Output ''
Write-Output '== reading a process group id correctly =='
$ownGroup = Get-PosixProcessGroupId -ProcessId $PID
Assert-True 'our own process group resolves' ($ownGroup -gt 0) ("pgid " + $ownGroup)
Assert-True 'a nonexistent pid yields -1 rather than a guess' (
    (Get-PosixProcessGroupId -ProcessId 999999) -eq -1)

Write-Output ''
Write-Output '== it refuses to kill its own group =='
# If setsid ever fails, the adapter shares OUR group and kill(-pgid) would take
# down the supervisor. This must refuse rather than tidy up by suicide.
$threw = ''
try { $null = Invoke-PosixGroupKill -ProcessGroupId $ownGroup } catch { $threw = [string]$_.Exception.Message }
Assert-True 'killing our own group is refused' ($threw -match 'own process group') $threw
foreach ($bad in @(0, 1, -5)) {
    $t = ''
    try { $null = Invoke-PosixGroupKill -ProcessGroupId $bad } catch { $t = [string]$_.Exception.Message }
    Assert-True ("group id $bad is refused") ($t -match 'refusing') $t
}

Write-Output ''
Write-Output '== THE FORK-AFTER-SCAN CASE =='
$root = Join-Path ([IO.Path]::GetTempPath()) ('order-contain-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$lateMarker = Join-Path $root 'late-child-survived.txt'
$latePid = Join-Path $root 'late.pid'
$scanned = Join-Path $root 'scanned.txt'

# The parent waits for this test to say it has scanned, THEN forks a child and
# exits immediately - orphaning it. That child is in the group but is no longer
# a descendant of anything the walk started from.
$lateChild = Join-Path $root 'late_child.sh'
Set-Content -LiteralPath $lateChild -Encoding utf8 -Value @(
    '#!/bin/bash',
    ('echo $$ > ' + $latePid),
    'sleep 8',
    ('touch ' + $lateMarker)
)
$parent = Join-Path $root 'parent.sh'
Set-Content -LiteralPath $parent -Encoding utf8 -Value @(
    '#!/bin/bash',
    ('while [ ! -f ' + $scanned + ' ]; do sleep 0.1; done'),
    # A PLAIN fork. The first version of this test used `setsid` here, which
    # gives the late child its OWN new group - deliberately escaping the group
    # under test. That is a different scenario, and one no process-group kill
    # can cover: a process that calls setsid on itself leaves the group by
    # design, and only a cgroup would still contain it. Codex's case is the
    # ordinary one: a child forked normally, which INHERITS the pgid and keeps
    # it after its parent dies.
    ('bash ' + $lateChild + ' &'),
    'sleep 0.3',
    'exit 0'
)
& /bin/chmod +x $lateChild $parent

# Launch under setsid so the run owns a group, exactly as the supervisor does.
$proc = Start-Process -FilePath '/usr/bin/setsid' -ArgumentList ('/bin/bash ' + $parent) -PassThru

# WAIT FOR THE CONDITION, DO NOT SLEEP AT IT.
#
# This was a fixed 300ms pause, which is an assumption about how quickly the
# kernel schedules a new process and setsid re-parents it into its own group.
# On an unloaded box that assumption holds and the test is green; on a loaded
# one /proc/<pid>/stat may not be readable yet, the capture returns -1, and the
# suite fails for a reason that has nothing to do with containment. That is
# measured, not theoretical: on this rig at load average 6.7 the racy tree-walk
# suite failed three runs in four while the group path passed four in four.
#
# A test whose result depends on machine load reports load, not behaviour. So
# poll until the group is READABLE, bounded, and let the assertions below judge
# whether it is the RIGHT group. A timeout still fails - it just fails having
# actually waited.
$deadline = [DateTime]::UtcNow.AddSeconds(10)
$group = Get-PosixProcessGroupId -ProcessId $proc.Id
while ($group -le 0 -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 50
    $group = Get-PosixProcessGroupId -ProcessId $proc.Id
}
# CAPTURED WHILE ALIVE, exactly as the supervisor now does. Reading it after the
# launcher exits returns -1 - which is how the first version of this fix
# silently fell through to the racy walk while reporting success.
Assert-True 'the run is its own process-group leader' ($group -eq $proc.Id) (
    "pgid $group vs pid " + $proc.Id)
Assert-True 'and that group is NOT ours' ($group -ne $ownGroup)

# The first scan happens here - and only now do we let the parent fork.
$null = Get-PosixChildProcessId -ProcessId $proc.Id
Set-Content -LiteralPath $scanned -Value 'go' -Encoding utf8

# Same reasoning as the capture above: wait for the two facts this scenario
# needs - the late child exists, and its parent has gone - rather than sleeping
# two seconds and hoping both happened. Under load they may not have, and the
# failure would read as "late child never started" when the truth is "the box
# was busy".
$deadline = [DateTime]::UtcNow.AddSeconds(20)
$lateSeenAt = [DateTime]::MaxValue
while ([DateTime]::UtcNow -lt $deadline) {
    if ($lateSeenAt -eq [DateTime]::MaxValue -and (Test-Path -LiteralPath $latePid -PathType Leaf)) {
        # Observed AFTER the child started, so it is a safe lower bound on its
        # start time: waiting N seconds from here is at least N from the start.
        $lateSeenAt = [DateTime]::UtcNow
    }
    if ($lateSeenAt -ne [DateTime]::MaxValue -and
        -not (Test-PosixProcessAlive -ProcessId $proc.Id)) { break }
    Start-Sleep -Milliseconds 100
}
Assert-True 'a late child was forked after the scan' (
    Test-Path -LiteralPath $latePid -PathType Leaf) 'late child never started'
Assert-True 'and its parent has already exited, orphaning it' (
    -not (Test-PosixProcessAlive -ProcessId $proc.Id)) 'parent still alive'

$code = Invoke-ProcessTreeKill -ProcessId $proc.Id -ProcessGroupId $group
Assert-True 'the kill reports success' ($code -eq 0) ("exit " + $code)

# The control that would have caught the lookup-too-late bug: with NO group id
# passed, this falls back to the walk, which cannot reach an orphan. Asserting
# the fallback is genuinely weaker keeps anyone from "simplifying" the captured
# identity away later.
Assert-True 'looking the group up now would return -1, not the real group' (
    (Get-PosixProcessGroupId -ProcessId $proc.Id) -ne $group) (
    'lookup ' + (Get-PosixProcessGroupId -ProcessId $proc.Id) + ' vs captured ' + $group)

# THE MARKER-ABSENCE ASSERTION IS ONLY MEANINGFUL PAST THE CHILD'S OWN TIMER.
#
# The late child does `sleep 8` and then touches the marker. Checking for the
# marker before that moment proves nothing: it would be absent whether the kill
# worked or the child was simply still sleeping - an absence satisfied by not
# having waited, which is the shape of every false green in this port.
#
# The old code slept a flat 10 seconds, leaving a 2-second margin over the
# child's 8. Under load that margin is not real. Anchor on when the child was
# observed instead, and wait past 8 plus a margin, while also requiring the pid
# to actually be gone.
# If the child was never observed, $lateSeenAt is DateTime.MaxValue and adding
# to it throws. The assertion above has already failed in that case, so fall
# back to a deadline that is simply now - there is no child timer to outlast.
$markerDeadline = $(if ($lateSeenAt -eq [DateTime]::MaxValue) {
    [DateTime]::UtcNow
} else {
    $lateSeenAt.AddSeconds(14)
})
$pidDeadline = [DateTime]::UtcNow.AddSeconds(20)
$latePidValue = 0
if (Test-Path -LiteralPath $latePid -PathType Leaf) {
    $null = [int]::TryParse((Get-Content -LiteralPath $latePid -Raw).Trim(), [ref]$latePidValue)
}
while ([DateTime]::UtcNow -lt $pidDeadline) {
    if ($latePidValue -gt 0 -and -not (Test-PosixProcessAlive -ProcessId $latePidValue)) { break }
    Start-Sleep -Milliseconds 200
}
while ([DateTime]::UtcNow -lt $markerDeadline) { Start-Sleep -Milliseconds 200 }

# THE ASSERTION THE OLD WALK FAILED. The orphaned late child is not reachable
# from the root by any tree search, but it is still in the process group.
Assert-True 'we waited past the moment the child would have written its marker' (
    [DateTime]::UtcNow -ge $markerDeadline) 'the absence check below would be vacuous'
Assert-True 'the ORPHANED late child is dead' (
    -not (Test-Path -LiteralPath $lateMarker)) 'it survived and wrote its marker'
if (Test-Path -LiteralPath $latePid -PathType Leaf) {
    $lp = 0
    if ([int]::TryParse((Get-Content -LiteralPath $latePid -Raw).Trim(), [ref]$lp)) {
        Assert-True 'and its pid is no longer alive' (-not (Test-PosixProcessAlive -ProcessId $lp)) ("pid " + $lp)
    }
}

Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
Write-Output ('RESULT passed=' + $script:Pass + ' failed=' + $script:Fail)
foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
if ($script:Fail -gt 0) { exit 1 }
exit 0
