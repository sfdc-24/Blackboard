# Group containment: does the kill actually PROVE the group died?
#
# THE THREE DEFECTS THIS COVERS, all of them fail-open:
#
#   1. Invoke-PosixGroupKill ended in `return 0` unconditionally. It reported a
#      clean containment whether the signal was delivered, rejected, or never
#      sent at all because /bin/kill did not exist on the host.
#
#   2. The self-kill guard read our own pgid and, when that read FAILED, skipped
#      the guard and signalled anyway. An unreadable /proc/self/stat became
#      permission to signal a group that might have been our own.
#
#   3. Nothing ever observed the group afterwards. "Sent a signal" was being
#      reported as "the processes are gone".
#
# WHY THE NEGATIVE CONTROL IS THE IMPORTANT TEST HERE
#   Every false green in this repo has been an assertion that something bad was
#   ABSENT, satisfied because execution never reached the code. So this file
#   does not merely assert that a real kill returns 0. It SABOTAGES the kill -
#   points the binary resolver at /bin/true, which signals nothing - and then
#   requires a non-zero return and surviving members. If that control ever goes
#   green, the drain proof has stopped proving anything and this file is lying.
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

if (-not (Test-Path -LiteralPath '/proc')) {
    Write-Output 'SKIP: this suite measures POSIX process groups and needs /proc.'
    # The SKIP line above is what a human reads; this is what a parser reads.
    Write-Output 'RESULT passed=0 failed=0'
    exit 0
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runnerPath = Join-Path $repoRoot 'scripts/order_supervisor.ps1'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$errors)
Assert-True 'runner parses' (@($errors).Count -eq 0)

$wanted = @('Get-PosixProcessGroupId', 'Get-PosixProcessGroupMemberId',
            'Resolve-PosixKillBinary', 'Invoke-PosixGroupKill')
foreach ($fn in $wanted) {
    $defs = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $fn
    }, $true))
    Assert-True ('runner defines exactly one ' + $fn) ($defs.Count -eq 1) ("found " + $defs.Count)
    if ($defs.Count -eq 1) { Invoke-Expression $defs[0].Extent.Text }
}

Write-Output ''
Write-Output '== the group enumerator, on a group we can verify independently =='
# POSITIVE CONTROL. Our own group must contain our own pid. An enumerator that
# returned an empty list for everything would satisfy every "drained" assertion
# below, so it is checked against a known-present member first.
$ownGroup = Get-PosixProcessGroupId -ProcessId $PID
Assert-True 'our own process group resolves' ($ownGroup -gt 0) ("pgid " + $ownGroup)
$ownMembers = @(Get-PosixProcessGroupMemberId -ProcessGroupId $ownGroup)
Assert-True 'our own pid appears in our own group' ($ownMembers -contains $PID) (
    "members " + ($ownMembers -join ','))

# A group id that cannot exist must come back empty rather than throwing.
$ghost = @(Get-PosixProcessGroupMemberId -ProcessGroupId 999999)
Assert-True 'a nonexistent group enumerates to empty' ($ghost.Count -eq 0) (
    "count " + $ghost.Count)

Write-Output ''
Write-Output '== it refuses, rather than guesses, when it cannot identify itself =='
# DEFECT 2. Shadow the pgid reader so it fails the way an unreadable
# /proc/self/stat would, and require a refusal. Under the old `-gt 0 -and`
# form this signalled the group instead.
function Get-PosixProcessGroupId { param([int]$ProcessId) return -1 }
$threw = ''
try { $null = Invoke-PosixGroupKill -ProcessGroupId 424242 } catch { $threw = [string]$_.Exception.Message }
Assert-True 'unknown own pgid refuses to signal' ($threw -match 'without knowing our own') $threw

# Restore the real reader for everything that follows.
$realPgidDef = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Get-PosixProcessGroupId'
}, $true))[0]
Invoke-Expression $realPgidDef.Extent.Text
Assert-True 'the real pgid reader is restored' ((Get-PosixProcessGroupId -ProcessId $PID) -eq $ownGroup)

$threwOwn = ''
try { $null = Invoke-PosixGroupKill -ProcessGroupId $ownGroup } catch { $threwOwn = [string]$_.Exception.Message }
Assert-True 'killing our own group is refused' ($threwOwn -match 'own process group') $threwOwn

foreach ($bad in @(0, 1, -5)) {
    $t = ''
    try { $null = Invoke-PosixGroupKill -ProcessGroupId $bad } catch { $t = [string]$_.Exception.Message }
    Assert-True ("group id $bad is refused") ($t -match 'refusing') $t
}

Write-Output ''
Write-Output '== a host with no kill binary reports failure, not success =='
# DEFECT 1. On a host where the binary is absent the old code still returned 0.
function Resolve-PosixKillBinary { return '' }
$noKill = Invoke-PosixGroupKill -ProcessGroupId 424242
Assert-True 'absent kill binary returns -1' ($noKill -eq -1) ("returned " + $noKill)

Write-Output ''
Write-Output '== a real group, and the drain proof that has to be able to FAIL =='
$root = Join-Path ([IO.Path]::GetTempPath()) ('order-drain-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$holder = Join-Path $root 'holder.sh'
[IO.File]::WriteAllText($holder, @'
#!/bin/bash
# Hold a group open with several processes, one of them forked LATE so the
# group is still changing shape when the kill lands.
sleep 60 &
( sleep 2; sleep 60 ) &
sleep 60
'@.Replace("`r`n", "`n"))
& /bin/chmod +x $holder 2>$null | Out-Null

$setsid = '/usr/bin/setsid'
if (-not (Test-Path -LiteralPath $setsid -PathType Leaf)) { $setsid = '/bin/setsid' }
Assert-True 'setsid is available' (Test-Path -LiteralPath $setsid -PathType Leaf) $setsid

$proc = Start-Process -FilePath $setsid -ArgumentList @('/bin/bash', $holder) -PassThru
Start-Sleep -Seconds 4

$group = Get-PosixProcessGroupId -ProcessId $proc.Id
Assert-True 'the run became its own group leader' ($group -eq $proc.Id) (
    "pid " + $proc.Id + " pgid " + $group)

$before = @(Get-PosixProcessGroupMemberId -ProcessGroupId $group)
Assert-True 'the group has live members before any kill' ($before.Count -ge 2) (
    "count " + $before.Count + " -> " + ($before -join ','))

# THE NEGATIVE CONTROL. /bin/true accepts the arguments, exits 0, and signals
# nothing whatsoever. A drain proof that is real must now report failure.
function Resolve-PosixKillBinary { return '/bin/true' }
$sabotaged = Invoke-PosixGroupKill -ProcessGroupId $group -DrainMilliseconds 1500
Assert-True 'a kill that signals nothing does NOT return success' ($sabotaged -ne 0) (
    "returned " + $sabotaged)
$stillThere = @(Get-PosixProcessGroupMemberId -ProcessGroupId $group)
Assert-True 'and the group is demonstrably still alive' ($stillThere.Count -ge 2) (
    "count " + $stillThere.Count)

# Now the real thing, same group, same moment.
$realKillDef = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Resolve-PosixKillBinary'
}, $true))[0]
Invoke-Expression $realKillDef.Extent.Text
Assert-True 'the real kill binary resolves' ((Resolve-PosixKillBinary) -ne '') (Resolve-PosixKillBinary)

$code = Invoke-PosixGroupKill -ProcessGroupId $group -DrainMilliseconds 8000
Assert-True 'a real group kill returns 0' ($code -eq 0) ("returned " + $code)

$after = @(Get-PosixProcessGroupMemberId -ProcessGroupId $group)
Assert-True 'the group is empty afterwards' ($after.Count -eq 0) (
    "survivors " + ($after -join ','))

Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
# RESULT passed=N failed=N is the repo convention and harnesses parse it.
Write-Output ("RESULT passed=" + $script:Pass + " failed=" + $script:Fail)
if ($script:Fail -gt 0) {
    foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
    exit 1
}
exit 0
