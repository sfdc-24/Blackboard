# ConvertTo-ClaudeEnvDenyPath, asserted on its OUTPUT.
#
# WHY THIS REPLACES THE PREVIOUS TEST
#   My first attempt drove the whole adapter and asserted that two error strings
#   were ABSENT. The adapter threw anthropic_api_key_missing long before it
#   reached the changed line, both assertions were trivially satisfied, and the
#   suite reported 9/0 while the code under test never executed. Codex caught it.
#
#   That was the THIRD assertion of that shape I wrote in one day:
#     - a descendant "is gone" - satisfied by a child that never started
#     - two runs "have no differences" - satisfied by two identical failures
#     - an error string "is absent" - satisfied by an earlier, different error
#
#   All three pass when execution never reaches the thing being tested. An
#   assertion about a returned VALUE cannot be satisfied that way, which is why
#   the formatter was extracted rather than tested through the adapter.
#
# Runs on either platform. No adapter invocation, no provider, no network.
$ErrorActionPreference = 'Continue'
$script:Pass = 0
$script:Fail = 0
$script:Failures = @()

function Assert-Equal {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -ceq $Actual) {
        $script:Pass++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Fail++
        $script:Failures += $Name
        Write-Output ('FAIL ' + $Name + ": expected '" + $Expected + "' got '" + $Actual + "'")
    }
}
function Assert-Throws {
    param([string]$Name, [string]$Expected, [scriptblock]$Action)
    $msg = ''
    try { $null = & $Action } catch { $msg = [string]$_.Exception.Message }
    if ($msg -ceq $Expected) {
        $script:Pass++
        Write-Output ('PASS ' + $Name)
    } else {
        $script:Fail++
        $script:Failures += $Name
        Write-Output ('FAIL ' + $Name + ": expected throw '" + $Expected + "' got '" + $msg + "'")
    }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$adapter = Join-Path $repoRoot 'scripts/invoke_order_claude.ps1'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($adapter, [ref]$tokens, [ref]$errors)
if (@($errors).Count -ne 0) { Write-Output 'FAIL adapter does not parse'; exit 1 }
$defs = @($ast.FindAll({
    param($n)
    $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -ceq 'ConvertTo-ClaudeEnvDenyPath'
}, $true))
if ($defs.Count -ne 1) { Write-Output 'FAIL adapter defines one ConvertTo-ClaudeEnvDenyPath'; exit 1 }
Invoke-Expression $defs[0].Extent.Text
Write-Output 'PASS the formatter was extracted from the real adapter'
$script:Pass++

Write-Output ''
Write-Output '== POSIX: the case that was broken =='
Assert-Equal 'a POSIX absolute path is returned unchanged' `
    '/etc/blackboard-order/env' (ConvertTo-ClaudeEnvDenyPath -EnvFile '/etc/blackboard-order/env' -OnWindows $false)
Assert-Equal 'a trailing slash is trimmed' `
    '/etc/x' (ConvertTo-ClaudeEnvDenyPath -EnvFile '/etc/x/' -OnWindows $false)
Assert-Equal 'a path with spaces survives' `
    '/home/a b/.env' (ConvertTo-ClaudeEnvDenyPath -EnvFile '/home/a b/.env' -OnWindows $false)
Assert-Equal 'a dotfile at root works' `
    '/.env' (ConvertTo-ClaudeEnvDenyPath -EnvFile '/.env' -OnWindows $false)

Write-Output ''
Write-Output '== Windows: byte-identical to what it always produced =='
Assert-Equal 'a drive path becomes the //c form' `
    '//c/Users/salam/Quantum/Blackboard/.env' `
    (ConvertTo-ClaudeEnvDenyPath -EnvFile 'C:\Users\salam\Quantum\Blackboard\.env' -OnWindows $true)
Assert-Equal 'the drive letter is lowercased' `
    '//d/a/b' (ConvertTo-ClaudeEnvDenyPath -EnvFile 'D:\a\b' -OnWindows $true)
Assert-Equal 'forward slashes are accepted too' `
    '//c/a/b' (ConvertTo-ClaudeEnvDenyPath -EnvFile 'C:/a/b' -OnWindows $true)
Assert-Equal 'a trailing slash is trimmed on Windows as well' `
    '//c/a' (ConvertTo-ClaudeEnvDenyPath -EnvFile 'C:\a\' -OnWindows $true)
# The Windows branch is reachable REGARDLESS of platform flag, because a
# drive-letter path is unambiguous. This pins that the POSIX branch did not
# swallow it.
Assert-Equal 'a drive path still works with OnWindows false' `
    '//c/a/b' (ConvertTo-ClaudeEnvDenyPath -EnvFile 'C:\a\b' -OnWindows $false)

Write-Output ''
Write-Output '== what must still be refused =='
Assert-Throws 'a relative path' 'claude_env_file_drive_root_required' `
    { ConvertTo-ClaudeEnvDenyPath -EnvFile 'some/relative/.env' -OnWindows $false }
Assert-Throws 'a bare filename' 'claude_env_file_drive_root_required' `
    { ConvertTo-ClaudeEnvDenyPath -EnvFile '.env' -OnWindows $false }
Assert-Throws 'an empty path' 'claude_env_file_drive_root_required' `
    { ConvertTo-ClaudeEnvDenyPath -EnvFile '' -OnWindows $false }
# A POSIX path presented on WINDOWS is refused: there is no such file there, and
# accepting it would emit a deny rule that matches nothing.
Assert-Throws 'a POSIX path while on Windows' 'claude_env_file_drive_root_required' `
    { ConvertTo-ClaudeEnvDenyPath -EnvFile '/etc/x' -OnWindows $true }
Assert-Throws 'root alone collapses to empty and is refused' 'claude_env_file_absolute_path_required' `
    { ConvertTo-ClaudeEnvDenyPath -EnvFile '/' -OnWindows $false }

Write-Output ''
Write-Output ('RESULT passed=' + $script:Pass + ' failed=' + $script:Fail)
foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
if ($script:Fail -gt 0) { exit 1 }
exit 0
