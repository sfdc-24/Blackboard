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
Start-Sleep -Milliseconds 300
# CAPTURED WHILE ALIVE, exactly as the supervisor now does. Reading it later
# returns -1, because this launcher exits within a second - which is how the
# first version of this fix silently fell through to the racy walk while
# reporting success.
$group = Get-PosixProcessGroupId -ProcessId $proc.Id
Assert-True 'the run is its own process-group leader' ($group -eq $proc.Id) (
    "pgid $group vs pid " + $proc.Id)
Assert-True 'and that group is NOT ours' ($group -ne $ownGroup)

# The first scan happens here - and only now do we let the parent fork.
$null = Get-PosixChildProcessId -ProcessId $proc.Id
Set-Content -LiteralPath $scanned -Value 'go' -Encoding utf8
Start-Sleep -Seconds 2
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

Start-Sleep -Seconds 10
# THE ASSERTION THE OLD WALK FAILED. The orphaned late child is not reachable
# from the root by any tree search, but it is still in the process group.
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
