# The limit of containment by process group, stated as a test rather than a caveat.
#
# THE GAP (codex carry-forward 5)
#   A process group is durable against reparenting, which is why kill(-pgid)
#   beats a /proc tree walk. It is NOT durable against a child that calls
#   setsid: that child becomes the leader of a NEW group and leaves ours. Our
#   group then drains perfectly, Invoke-PosixGroupKill observes it empty, and
#   returns 0 - while the escaped descendant is still running.
#
#   That is a success return with a survivor, which is the exact shape this
#   whole lane exists to eliminate. It must be measured, not described in a
#   comment, or it will be forgotten and later mistaken for full containment.
#
# WHAT THIS FILE ASSERTS
#   1. The escape is real: after a successful group kill, the escapee is ALIVE.
#   2. The success return is real: the call returns 0 while that survivor runs.
#      Assertion 2 is the one that matters. It is not an absence check - it
#      requires a specific returned value alongside a specific live process.
#   3. A delegated cgroup DOES contain it, so the remedy is proven and not
#      merely proposed.
#
#   If assertion 1 or 2 ever fails, containment has been extended and this file
#   should be rewritten to match - a red result here is news, not a defect.
$ErrorActionPreference = 'Continue'
$script:Pass = 0
$script:Fail = 0
$script:Failures = @()
$script:Notes = @()

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

if (-not (Test-Path -LiteralPath '/proc')) {
    Write-Output 'SKIP: POSIX process groups require /proc.'
    Write-Output '0 passed, 0 failed (skipped)'
    exit 0
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runnerPath = Join-Path $repoRoot 'scripts/order_supervisor.ps1'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$errors)
Assert-True 'runner parses' (@($errors).Count -eq 0)
foreach ($fn in @('Get-PosixProcessGroupId', 'Get-PosixProcessGroupMemberId',
                  'Resolve-PosixKillBinary', 'Invoke-PosixGroupKill', 'Test-PosixProcessAlive')) {
    $defs = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $fn
    }, $true))
    if ($defs.Count -ne 1) { Assert-True ('runner defines one ' + $fn) $false; continue }
    Invoke-Expression $defs[0].Extent.Text
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('order-escape-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$escapeePidFile = Join-Path $root 'escapee.pid'

$setsid = '/usr/bin/setsid'
if (-not (Test-Path -LiteralPath $setsid -PathType Leaf)) { $setsid = '/bin/setsid' }
Assert-True 'setsid is available' (Test-Path -LiteralPath $setsid -PathType Leaf) $setsid

Write-Output ''
Write-Output '== a child that calls setsid leaves the group =='

# The outer process is our group. The inner one calls setsid, so it becomes its
# own group leader and is no longer reachable by our pgid.
#
# THE SCRIPT GOES IN A FILE, NOT IN -ArgumentList. Start-Process joins its
# argument list and lets the platform re-parse it, so a -c payload containing
# spaces and quotes arrives mangled: the first attempt exited instantly and the
# pgid read came back -1. The same trap produced "sleep: missing operand"
# earlier in this port. A file has no quoting to lose, which is why the
# group-drain suite launches its holder the same way.
$holder = Join-Path $root 'escape_holder.sh'
[IO.File]::WriteAllText($holder, @'
#!/bin/bash
# Fork a child that calls setsid, so it LEAVES this process group, then hold
# this group open. Redirection is inside the script: PowerShell refuses
# -RedirectStandardOutput and -RedirectStandardError when both name /dev/null.
setsid bash -c 'echo $$ > "__PIDFILE__"; exec sleep 300' >/dev/null 2>&1 &
exec sleep 300
'@.Replace("`r`n", "`n").Replace('__PIDFILE__', $escapeePidFile))
& /bin/chmod +x $holder 2>$null | Out-Null

$proc = Start-Process -FilePath $setsid -ArgumentList @('/bin/bash', $holder) -PassThru
Start-Sleep -Seconds 5

$group = Get-PosixProcessGroupId -ProcessId $proc.Id
Assert-True 'the run is its own group leader' ($group -eq $proc.Id) (
    "pid " + $proc.Id + " pgid " + $group)

$escapee = 0
if (Test-Path -LiteralPath $escapeePidFile) {
    $null = [int]::TryParse((Get-Content -LiteralPath $escapeePidFile -Raw).Trim(), [ref]$escapee)
}
Assert-True 'the escapee reported its pid' ($escapee -gt 0) ("pid " + $escapee)
Assert-True 'the escapee is alive before any kill' (Test-PosixProcessAlive -ProcessId $escapee)

$escapeeGroup = Get-PosixProcessGroupId -ProcessId $escapee
Assert-True 'the escapee left our group' ($escapeeGroup -ne $group) (
    "ours " + $group + " theirs " + $escapeeGroup)
Assert-True 'the escapee leads its own group' ($escapeeGroup -eq $escapee) (
    "pgid " + $escapeeGroup)

Write-Output ''
Write-Output '== the group kill succeeds, and the survivor is still there =='
$code = Invoke-PosixGroupKill -ProcessGroupId $group -DrainMilliseconds 6000
$members = @(Get-PosixProcessGroupMemberId -ProcessGroupId $group)
Assert-True 'our group drained completely' ($members.Count -eq 0) (
    "survivors in group: " + ($members -join ','))

# THE TWO ASSERTIONS THAT DEFINE THE GAP.
$stillAlive = Test-PosixProcessAlive -ProcessId $escapee
Assert-True 'the escaped descendant SURVIVED the group kill' $stillAlive
Assert-True 'and the call returned 0 while it was running' ($code -eq 0) ("returned " + $code)

Write-Output ''
Write-Output '== the remedy: a delegated cgroup does contain it =='
$unit = 'order-escape-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$scopePidFile = Join-Path $root 'scoped.pid'
# Same reason as above: the payload lives in a file so Start-Process has no
# quoting to lose.
$scopeHolder = Join-Path $root 'scope_holder.sh'
[IO.File]::WriteAllText($scopeHolder, @'
#!/bin/bash
setsid bash -c 'echo $$ > "__PIDFILE__"; exec sleep 300' >/dev/null 2>&1 &
exec sleep 300
'@.Replace("`r`n", "`n").Replace('__PIDFILE__', $scopePidFile))
& /bin/chmod +x $scopeHolder 2>$null | Out-Null

$scopeStarted = $false
try {
    $null = Start-Process -FilePath 'systemd-run' `
        -ArgumentList @('--user', '--scope', '--quiet', "--unit=$unit", '/bin/bash', $scopeHolder) `
        -PassThru
    Start-Sleep -Seconds 5
    $scopeStarted = Test-Path -LiteralPath $scopePidFile
} catch {
    $scopeStarted = $false
}

if (-not $scopeStarted) {
    # Reported loudly rather than skipped silently: a remedy that was never
    # exercised must not read as a remedy that passed.
    $script:Notes += 'REMEDY NOT EXERCISED: no systemd --user session on this host, so the cgroup containment case did not run.'
    Write-Output '  REMEDY NOT EXERCISED on this host (no systemd --user session).'
} else {
    $scoped = 0
    $null = [int]::TryParse((Get-Content -LiteralPath $scopePidFile -Raw).Trim(), [ref]$scoped)
    Assert-True 'the scoped escapee reported its pid' ($scoped -gt 0) ("pid " + $scoped)
    Assert-True 'it also left its process group' (
        (Get-PosixProcessGroupId -ProcessId $scoped) -eq $scoped)
    Assert-True 'it is alive before the scope is killed' (Test-PosixProcessAlive -ProcessId $scoped)

    & systemctl --user kill --signal=SIGKILL ($unit + '.scope') 2>$null | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Test-PosixProcessAlive -ProcessId $scoped) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
    Assert-True 'the cgroup kill reached the escapee that pgid could not' (
        -not (Test-PosixProcessAlive -ProcessId $scoped))
    & systemctl --user stop ($unit + '.scope') 2>$null | Out-Null
}

# Leave nothing running.
foreach ($p in @($escapee)) {
    if ($p -gt 0) { & /bin/kill -s KILL $p 2>$null | Out-Null }
}
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
foreach ($n in $script:Notes) { Write-Output ('NOTE ' + $n) }
Write-Output ("" + $script:Pass + " passed, " + $script:Fail + " failed")
if ($script:Fail -gt 0) {
    foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
    exit 1
}
exit 0
