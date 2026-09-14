# Linux-host behaviour of the ORDER supervisor helpers.
#
# WHAT IS ON TRIAL
#   The port swaps the scheduler and keeps the worker, but two things the worker
#   does are Windows-shaped and had no Linux path at all:
#
#   1. The engine was Windows PowerShell 5.1 by ABSOLUTE PATH. On Linux that
#      file does not exist, so the worker threw before it ever ran anything.
#   2. Process termination was taskkill.exe /T, which kills the whole tree.
#      Invoke-TaskkillTree returned -1 when taskkill was absent, so on Linux it
#      would have reported "could not kill" and moved on while the adapter and
#      ITS children kept running. Quiet, and worse than a crash.
#
#   The tree kill is the case that needs real processes. A mock cannot tell the
#   difference between killing a tree and killing a parent, and killing only the
#   parent is exactly the bug.
#
# RUN (on the Linux host)
#   pwsh -NoProfile -File tests/test_order_linux_host.ps1
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

$runnerPath = Join-Path $PSScriptRoot '../scripts/order_supervisor.ps1'
$runnerPath = [IO.Path]::GetFullPath($runnerPath)
Assert-True 'runner file exists' (Test-Path -LiteralPath $runnerPath -PathType Leaf) $runnerPath

$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$errors)
Assert-True 'runner parses on this host' (@($errors).Count -eq 0)

foreach ($fn in @('Test-OnWindows', 'Resolve-WorkerEngine', 'Get-PosixChildProcessId',
                  'Invoke-PosixTreeKill', 'Invoke-ProcessTreeKill', 'Invoke-TaskkillTree', 'Test-PosixProcessAlive')) {
    $defs = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $fn
    }, $true))
    Assert-True ('runner defines one ' + $fn) ($defs.Count -eq 1)
    if ($defs.Count -eq 1) { Invoke-Expression $defs[0].Extent.Text }
}

Write-Output ''
Write-Output '== platform detection =='
Assert-True 'Test-OnWindows is False on this host' (-not (Test-OnWindows))
# The 5.1 trap, asserted directly: reading $IsWindows on 5.1 gives $null, which
# is falsey, so a naive check would call every Windows 5.1 host Linux.
Assert-True 'the check reads the variable EXISTENCE, not just its value' (
    (Get-Command Test-OnWindows).Definition -match 'Test-Path\s+Variable:IsWindows')

Write-Output ''
Write-Output '== engine resolution =='
$engine = $null
try { $engine = Resolve-WorkerEngine } catch { $engine = 'THREW: ' + $_.Exception.Message }
Assert-True 'an engine resolves on Linux' ($engine -and (Test-Path -LiteralPath $engine -PathType Leaf)) $engine
Assert-True 'and it is a pwsh, not powershell.exe' ($engine -notmatch 'powershell\.exe$') $engine
Assert-True 'and it is the interpreter actually running this' (
    $engine -eq (Get-Process -Id $PID).Path) ($engine + ' vs ' + (Get-Process -Id $PID).Path)

Write-Output ''
Write-Output '== process tree termination, with real processes =='
# A parent that spawns a child that outlives a naive parent-only kill. If only
# the parent dies, the child writes the marker and the test fails - which is
# precisely the failure a mock could not have shown.
$root = Join-Path ([IO.Path]::GetTempPath()) ('order-linux-treekill-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$marker = Join-Path $root 'child-survived.txt'
$childScript = Join-Path $root 'child.sh'
$parentScript = Join-Path $root 'parent.sh'

Set-Content -LiteralPath $childScript -Value @(
    '#!/bin/bash',
    'sleep 6',
    ('touch ' + $marker)
) -Encoding utf8
Set-Content -LiteralPath $parentScript -Value @(
    '#!/bin/bash',
    ('bash ' + $childScript + ' &'),
    'sleep 30'
) -Encoding utf8
& /bin/chmod +x $childScript $parentScript

$proc = Start-Process -FilePath '/bin/bash' -ArgumentList $parentScript -PassThru
Start-Sleep -Seconds 2

$children = @(Get-PosixChildProcessId -ProcessId $proc.Id)
Assert-True 'the parent has a discoverable child' ($children.Count -ge 1) ($children -join ',')

$code = Invoke-ProcessTreeKill -ProcessId $proc.Id
Assert-True 'the tree kill reports success' ($code -eq 0) ("exit " + $code)

Start-Sleep -Seconds 8
Assert-True 'the parent is gone' (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue))
# THE ONE THAT MATTERS. taskkill /T kills descendants; Stop-Process alone does
# not. If this marker exists, the child outlived the kill and the port would
# have leaked adapter processes on every timeout.
Assert-True 'the CHILD is gone too, not just the parent' (
    -not (Test-Path -LiteralPath $marker)) 'the child survived and wrote its marker'

Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
Write-Output '== a pid with no children is not an error =='
$code2 = Invoke-ProcessTreeKill -ProcessId 999999
Assert-True 'killing a non-existent pid does not throw' ($code2 -is [int]) ("got " + $code2)

Write-Output ''
Write-Output ('RESULT passed=' + $script:Pass + ' failed=' + $script:Fail)
foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
if ($script:Fail -gt 0) { exit 1 }
exit 0
