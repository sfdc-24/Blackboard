# The EXECUTE path on Linux: adapter invocation, the wall timeout, and the
# process-tree kill under a real workload.
#
# WHY A SEPARATE FILE
#   tests/test_order_supervisor.ps1 hardcodes powershell.exe in eleven places
#   and aborts on Linux after 333 passing assertions - so these three paths were
#   never reached there. I tried porting that shared harness inline and broke
#   Windows from 494 to 491, because ten of those sites use the bare name and
#   one uses the full SystemRoot path: they are two values, not one. This file
#   is additive and Linux-only, so getting it wrong cannot cost the Windows
#   guard a second time.
#
# WHAT IT CAUGHT
#   Start-Process in Invoke-ClaudeWorker passed -WindowStyle Hidden
#   unconditionally. That parameter is Windows-only and THROWS on Linux. It sits
#   on the execute path, which no Observe-mode run reaches, so every cross-host
#   comparison so far went straight past it.
#
# NO NETWORK, NO BOARD, NO PROVIDER. The adapter is a local script that sleeps.
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
$modulePath = Join-Path $repoRoot 'scripts/OrderSupervisor.psm1'
Import-Module $modulePath -Force

$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$errors)
Assert-True 'runner parses' (@($errors).Count -eq 0)

# Invoke-ClaudeWorker now captures a process-GROUP identity at launch, so the
# group helpers have to be extracted too. Leaving them out did not silently
# weaken the test - it broke it loudly, which is the extraction list doing its
# job: the runner and the subset this file loads must stay in step.
foreach ($fn in @('Test-OnWindows', 'Resolve-WorkerEngine', 'Get-PosixChildProcessId',
                  'Get-PosixProcessGroupId', 'Invoke-PosixGroupKill',
                  'Resolve-PosixKillBinary', 'Get-PosixProcessGroupMemberId',
                  'Test-PosixProcessAlive', 'Invoke-PosixTreeKill', 'Invoke-ProcessTreeKill',
                  'Invoke-TaskkillTree', 'Quote-ProcessArgument',
                  'Test-RunTreeContainsReparsePoint', 'Remove-OwnedRunDirectory',
                  'Invoke-ClaudeWorker')) {
    $defs = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $fn
    }, $true))
    if ($defs.Count -ne 1) { Assert-True ('runner defines one ' + $fn) $false; continue }
    Invoke-Expression $defs[0].Extent.Text
}

# --- the scaffold Invoke-ClaudeWorker reads from script scope ----------------
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('order-exec-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
$owner = Join-Path $sandbox 'owner'
$workspace = Join-Path $sandbox 'workspace'
New-Item -ItemType Directory -Path $owner -Force | Out-Null
New-Item -ItemType Directory -Path $workspace -Force | Out-Null

$descendantMarker = Join-Path $sandbox 'descendant-survived.txt'
$adapterStarted = Join-Path $sandbox 'adapter-started.txt'

# A FAKE adapter. It records that it ran, spawns a child that would outlive a
# parent-only kill, and then sleeps well past the wall timeout.
$descendantPidFile = Join-Path $sandbox 'descendant.pid'

# The child goes in its own .sh file rather than through -ArgumentList.
# The first version passed '-c', 'sleep 6; touch X' and PowerShell split it on
# spaces, so bash received a bare `sleep` and died with "missing operand" - the
# descendant NEVER EXISTED, and the assertion that it had been killed passed
# vacuously. A test that cannot fail is worse than no test.
$childScript = Join-Path $sandbox 'child.sh'
Set-Content -LiteralPath $childScript -Encoding utf8 -Value @(
    '#!/bin/bash',
    ('echo $$ > ' + $descendantPidFile),
    'sleep 6',
    ('touch ' + $descendantMarker)
)
& /bin/chmod +x $childScript

$fakeAdapter = Join-Path $sandbox 'fake_adapter.ps1'
Set-Content -LiteralPath $fakeAdapter -Encoding utf8 -Value @(
    'param(',
    '  [string]$PromptPath, [string]$SchemaPath, [string]$StdoutPath,',
    '  [string]$StderrPath, [string]$EnvFile, [string]$WorkspacePath,',
    '  [string]$ClaudeCommand, [double]$MaxBudgetUsd',
    ')',
    ('[IO.File]::WriteAllText(' + "'" + $adapterStarted + "'" + ', [string]$PID)'),
    ('$null = Start-Process -FilePath ''/bin/bash'' -ArgumentList ' + "'" + $childScript + "'" + ' -PassThru'),
    'Start-Sleep -Seconds 45'
)

$ClaudeAdapter = $fakeAdapter
$ClaudeSchema = Join-Path $repoRoot 'scripts/order_supervisor_result.schema.json'
$StatePath = Join-Path $owner 'state.json'
$LogPath = Join-Path $owner 'order.log'
$WorkspacePath = $workspace
$RunId = 'f' * 32
$WallTimeoutSeconds = 6
$MaxBudgetUsd = 0.01
$ClaudeCommand = 'claude'
$EnvFile = Join-Path $sandbox 'unused.env'
Set-Content -LiteralPath $EnvFile -Value 'PLACEHOLDER=1' -Encoding utf8

Write-Output ''
Write-Output '== the execute path runs at all on Linux =='
Assert-True 'the schema the adapter is checked against exists' (
    Test-Path -LiteralPath $ClaudeSchema -PathType Leaf) $ClaudeSchema

$threw = ''
$started = [DateTime]::UtcNow
try {
    $null = Invoke-ClaudeWorker -WorkId 'linux-exec-timeout' -Source 'codex' -Task 'sleep past the wall timeout'
} catch {
    $threw = [string]$_.Exception.Message
}
$elapsed = ([DateTime]::UtcNow - $started).TotalSeconds

# THE ONE THAT CAUGHT THE BUG. Before the fix this threw a parameter error from
# -WindowStyle, never reaching the timeout at all - so the message, not just the
# fact that it threw, is what is asserted.
Assert-True 'it did NOT fail on a Windows-only Start-Process parameter' (
    $threw -notmatch 'WindowStyle') $threw
Assert-True 'the adapter was actually invoked' (
    Test-Path -LiteralPath $adapterStarted -PathType Leaf) 'adapter never started'
Assert-True 'it classified as a wall timeout' ($threw -ceq 'claude_wall_timeout') $threw
Assert-True 'it waited about the wall timeout, not 45 seconds' (
    $elapsed -ge 5 -and $elapsed -lt 30) ('elapsed ' + [int]$elapsed + 's')

Write-Output ''
Write-Output '== the tree kill reached the adapter''s CHILD, not just the adapter =='
# FIRST prove the descendant existed. Without this, "it is gone" is satisfied by
# a child that never started, which is exactly how the first version of this
# test passed while proving nothing.
Assert-True 'a descendant was actually spawned' (
    Test-Path -LiteralPath $descendantPidFile -PathType Leaf) 'no descendant pid file'

Start-Sleep -Seconds 8
Assert-True 'the adapter process is gone' (
    -not (Test-Path -LiteralPath $adapterStarted -PathType Leaf) -or
    -not (Test-PosixProcessAlive -ProcessId ([int](Get-Content -LiteralPath $adapterStarted -Raw))))
if (Test-Path -LiteralPath $descendantPidFile -PathType Leaf) {
    $descPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $descendantPidFile -Raw).Trim(), [ref]$descPid)) {
        Assert-True 'the descendant pid is no longer alive' (
            -not (Test-PosixProcessAlive -ProcessId $descPid)) ('pid ' + $descPid)
    }
}
# If the marker exists, a grandchild outlived the timeout kill and the port
# would leak a process on every timeout.
Assert-True 'the DESCENDANT is gone too' (
    -not (Test-Path -LiteralPath $descendantMarker)) 'the descendant survived the tree kill'

Write-Output ''
Write-Output '== the owned run directory is cleaned up after a timeout =='
$leftovers = @(Get-ChildItem -LiteralPath $owner -Directory -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -like 'run-*' })
Assert-True 'no run- directory is left behind' ($leftovers.Count -eq 0) (
    ($leftovers | ForEach-Object { $_.Name }) -join ',')

Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
Write-Output ('RESULT passed=' + $script:Pass + ' failed=' + $script:Fail)
foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
if ($script:Fail -gt 0) { exit 1 }
exit 0
