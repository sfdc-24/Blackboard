# The REAL adapter on Linux, not a fake one.
#
# WHY THIS FILE EXISTS
#   tests/test_order_linux_execute.ps1 proves the supervisor's timeout and
#   containment paths, but it drives a FAKE adapter - so it structurally cannot
#   see anything about the real one. Codex found that the real
#   invoke_order_claude.ps1 rejected every POSIX EnvFile with
#   claude_env_file_drive_root_required, dying before the provider was ever
#   reached, and no amount of green in the fake-adapter suite would have shown
#   it.
#
#   A fake collaborator tests the caller. It tests nothing about the collaborator.
#
# NO PROVIDER IS INVOKED. The adapter is driven only as far as its argument and
# path validation, with a ClaudeCommand that does not exist - so a failure past
# validation is expected and is not what is being asserted. What is asserted is
# WHICH failure comes back.
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
$adapter = Join-Path $repoRoot 'scripts/invoke_order_claude.ps1'
$schema = Join-Path $repoRoot 'scripts/order_supervisor_result.schema.json'
Assert-True 'the real adapter is present' (Test-Path -LiteralPath $adapter -PathType Leaf) $adapter
Assert-True 'the schema is present' (Test-Path -LiteralPath $schema -PathType Leaf) $schema

$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('order-adapter-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
$workspace = Join-Path $sandbox 'workspace'
New-Item -ItemType Directory -Path $workspace -Force | Out-Null

$prompt = Join-Path $sandbox 'prompt.txt'
Set-Content -LiteralPath $prompt -Value 'do nothing' -Encoding utf8
$stdout = Join-Path $sandbox 'stdout.json'
$stderr = Join-Path $sandbox 'stderr.txt'

function Invoke-RealAdapter {
    param([string]$EnvFilePath)
    $err = ''
    try {
        $null = & $adapter `
            -PromptPath $prompt -SchemaPath $schema `
            -StdoutPath $stdout -StderrPath $stderr `
            -EnvFile $EnvFilePath -WorkspacePath $workspace `
            -ClaudeCommand '/nonexistent/claude-binary-for-this-test' `
            -MaxBudgetUsd 0.01 *>&1
    } catch {
        $err = [string]$_.Exception.Message
    }
    return $err
}

Write-Output ''
Write-Output '== THE BUG CODEX FOUND: a POSIX absolute env file =='
$posixEnv = Join-Path $sandbox 'real.env'
Set-Content -LiteralPath $posixEnv -Value 'PLACEHOLDER=1' -Encoding utf8
$err = Invoke-RealAdapter -EnvFilePath $posixEnv
Assert-True 'it no longer dies with claude_env_file_drive_root_required' (
    $err -notmatch 'drive_root_required') $err
# It must get PAST path validation. It will still fail later because the
# provider binary does not exist, and that is the point: a DIFFERENT failure
# means validation was cleared.
Assert-True 'it got past env-path validation' (
    $err -notmatch 'env_file.*required') $err

Write-Output ''
Write-Output '== but a RELATIVE path is still refused =='
# The deny rule names one exact file. A relative path does not name one, so
# accepting it would silently weaken the protection this validation exists for.
# Refused by claude_env_file_path_must_be_absolute, an EARLIER guard than the
# one this change touches. Worth asserting anyway: the POSIX branch accepts
# anything starting with '/', so if that earlier guard were ever removed this
# test is what would notice.
$err = Invoke-RealAdapter -EnvFilePath 'some/relative/.env'
Assert-True 'a relative env path is refused' (
    $err -match 'must_be_absolute|required') $err

$err = Invoke-RealAdapter -EnvFilePath '.env'
Assert-True 'a bare filename is refused' (
    $err -match 'must_be_absolute|required') $err

Write-Output ''
Write-Output '== the Windows shape is still understood on this host =='
# A drive-letter path is not valid on Linux, but the branch that handles it
# must remain reachable and must not be swallowed by the POSIX branch.
$src = [IO.File]::ReadAllText($adapter)
Assert-True 'the drive-letter branch survives in the source' (
    $src -match "A-Za-z\]\)\:\/" -or $src -match 'drive') 'drive branch missing'
Assert-True 'the POSIX branch is gated on not-Windows' (
    $src -match 'onWindows') 'no platform gate'
Assert-True 'both failure identifiers exist' (
    ($src -match 'claude_env_file_drive_root_required') -and
    ($src -match 'claude_env_file_absolute_path_required'))

Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
Write-Output ('RESULT passed=' + $script:Pass + ' failed=' + $script:Fail)
foreach ($f in $script:Failures) { Write-Output ('  - ' + $f) }
if ($script:Fail -gt 0) { exit 1 }
exit 0
