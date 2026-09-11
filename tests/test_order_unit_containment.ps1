# The unit file is where the setsid escape is actually bounded. Guard it.
#
# WHAT THE ESCAPE TEST SHOWED
#   A child that calls setsid leaves our process GROUP, so kill(-pgid) cannot
#   reach it and Invoke-PosixGroupKill returns 0 with that descendant running.
#   Proven on two hosts.
#
# WHAT I THEN FOUND, AND IT CHANGED THE FIX
#   The remedy I probed - systemd-run --user --scope - is NOT available to this
#   service. The unit runs User=blackboard as a SYSTEM service, so there is no
#   user session bus for --user; and it sets ProtectControlGroups=yes, which
#   makes /sys/fs/cgroup read-only, so the service cannot create a sub-cgroup
#   either. Implementing that remedy would have failed on the real host. This
#   is why the unit gets read before the code gets written.
#
#   But the escape is already bounded, one level up. A setsid child leaves the
#   process GROUP and STAYS IN THE UNIT'S CGROUP. With Type=oneshot the unit
#   stops when the pass exits, and KillMode=mixed makes systemd SIGKILL whatever
#   is still in that cgroup. So an escapee outlives the supervisor's own kill
#   but not the pass.
#
#   That containment is a property of THIS FILE. Change KillMode to process or
#   none and the escapee outlives the unit with nothing to reap it. Nothing else
#   in the repo would notice. Hence these assertions.
#
# Pure file parsing - no processes, no platform requirement, runs anywhere.
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
$unitPath = Join-Path $repoRoot 'packaging/systemd/blackboard-order.service'
Assert-True 'the ORDER unit file exists' (Test-Path -LiteralPath $unitPath) $unitPath
if (-not (Test-Path -LiteralPath $unitPath)) {
    Write-Output ("" + $script:Pass + " passed, " + $script:Fail + " failed")
    exit 1
}

# Directives only: strip comments so a directive named inside a comment can
# never satisfy an assertion about the directive itself.
$directives = @{}
foreach ($raw in [IO.File]::ReadAllLines($unitPath)) {
    $line = $raw.Trim()
    if (-not $line -or $line.StartsWith('#') -or $line.StartsWith(';')) { continue }
    if ($line.StartsWith('[')) { continue }
    $eq = $line.IndexOf('=')
    if ($eq -lt 1) { continue }
    $key = $line.Substring(0, $eq).Trim()
    $value = $line.Substring($eq + 1).Trim()
    $directives[$key] = $value
}
Assert-True 'the unit parsed into directives' ($directives.Count -ge 10) (
    "count " + $directives.Count)

Write-Output ''
Write-Output '== the two directives that bound a setsid escapee =='

# KillMode decides whether systemd reaps the whole cgroup. process and none
# leave the escapee running with nothing to collect it.
$killMode = [string]$directives['KillMode']
Assert-True 'KillMode is set at all' (-not [string]::IsNullOrWhiteSpace($killMode)) (
    "value '" + $killMode + "'")
Assert-True 'KillMode reaps the whole cgroup, not just the main process' (
    $killMode -ceq 'mixed' -or $killMode -ceq 'control-group') (
    "KillMode=" + $killMode + " would leave a setsid escapee unreaped")

# Type=oneshot is what makes the pass END, which is what triggers the reap.
# A long-running Type=simple daemon would hold the cgroup open indefinitely and
# an escapee would live for as long as the daemon did.
$type = [string]$directives['Type']
Assert-True 'Type is oneshot, so the pass ends and the cgroup is torn down' (
    $type -ceq 'oneshot') ("Type=" + $type)

# Without a stop timeout systemd could wait indefinitely before escalating.
$stopTimeout = [string]$directives['TimeoutStopSec']
Assert-True 'a stop timeout is set, so escalation to SIGKILL is bounded' (
    -not [string]::IsNullOrWhiteSpace($stopTimeout)) ("TimeoutStopSec=" + $stopTimeout)

Write-Output ''
Write-Output '== the reason the in-process cgroup remedy is unavailable =='
# Recorded as an assertion so that if someone ever relaxes it, the comment above
# stops being true and this test says so rather than going quietly stale.
$protect = [string]$directives['ProtectControlGroups']
# The + must END the line. PowerShell completes the expression at a line break,
# so a continuation line beginning with + is a parse error, not a continuation.
Assert-True 'ProtectControlGroups is still yes' ($protect -ceq 'yes') (
    "ProtectControlGroups=" + $protect +
    " - if this is now no, the service CAN create its own cgroup and the " +
    "setsid escape can be closed in-process")

$user = [string]$directives['User']
Assert-True 'the service still runs as a named non-root user' (
    -not [string]::IsNullOrWhiteSpace($user) -and $user -cne 'root') ("User=" + $user)

Write-Output ''
Write-Output '== credentials stay out of the unit (D-18) =='
foreach ($forbidden in @('BUS_SECRET', 'ANTHROPIC_API_KEY', 'CLASPRC_JSON', 'password')) {
    $hit = (Select-String -LiteralPath $unitPath -Pattern $forbidden -SimpleMatch -Quiet) -eq $true
    Assert-True ("the unit contains no literal " + $forbidden) (-not $hit)
}
Assert-True 'credentials arrive by EnvironmentFile, not Environment=' (
    $directives.ContainsKey('EnvironmentFile') -and -not $directives.ContainsKey('Environment')) (
    "EnvironmentFile=" + $directives['EnvironmentFile'])

Write-Output ''
Write-Output ("" + $script:Pass + " passed, " + $script:Fail + " failed")
if ($script:Fail -gt 0) {
    foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
    exit 1
}
exit 0
