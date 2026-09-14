#Requires -Version 5.1
<#
Is the committed Apps Script baseline still what is DEPLOYED? One command.

WHY THIS EXISTS, AND IT IS NOT A NEW IDEA
  scripts/gas_baseline_check.py has done the COMPARISON since 2026-09-06, and
  its docstring names this exact landmine: apps-script/<project>/ in the repo is
  a mirror of deployed source, a deploy pipeline pushes that directory, and it
  is stale - so deploying reverts whatever is only live.

  That check is referenced by NO workflow and nothing schedules it. On
  2026-09-13 I rediscovered the same drift by hand while deploying a one-line
  monitor fix, and came within one `clasp push` of pushing ten kilobytes of
  unreviewed Code.gs over the live Governor console and destroying content that
  exists only in the deployed Reception.html. A committed check that nothing
  invokes is not coverage - it is a file that happens to contain the answer.

  So this script is not a new detector. It is the missing FETCH-AND-CLEAN-UP
  around the detector that already works, written so that running it is one
  command instead of the six fiddly steps below, each of which I got wrong once.

WHAT IT DOES
  1. copies the project's .clasp.json into a scratch directory
  2. `clasp pull` the deployed source into that scratch directory
  3. runs gas_baseline_check.py to compare it against the committed baseline
  4. DELETES the scratch directory, always, including on failure
  5. exits 0 in sync, 1 drift, 2 could not determine

THE FOUR TRAPS THIS ENCODES, all measured on 2026-09-13
  * CASE. clasp refuses to write when the path's letter-case differs from the
    on-disk truth, and reports it as "Content directory is a symlink. Possible
    race attack." Nothing is a symlink and there are no reparse points on the
    path; --allow-symlinks does not help. So we resolve the real casing first.
  * PIPES. `cmd | tail; $?` is tail's status, not cmd's. My first push printed
    PUSH_EXIT=0 while clasp had actually refused. Exit codes are captured from
    the command here, never inherited through a pipe.
  * EMPTY READS LIKE CLEAN. A fetch that silently returns nothing makes the
    comparison trivially "no differences". We assert a nonzero file count and
    fail as UNKNOWN rather than reporting agreement we did not observe.
  * PRODUCTION SOURCE ON DISK. The pull brings down live Auth/Code including
    whatever they contain. It is removed in a finally block rather than by a
    step someone has to remember - a safeguard that cannot be forgotten beats a
    cleanup that must be.

WHAT IT DELIBERATELY DOES NOT DO
  It never pushes, never creates a version, never redeploys, and never writes
  inside the repository. It is read-only against Apps Script and read-only
  against the working tree. Re-baselining is a human decision with a diff to
  review, so it stays with gas_baseline_check.py --write.

HEAD IS NOT THE PINNED DEPLOYMENT
  `clasp pull` and `clasp push` address HEAD - the project's saved code. A web
  app serves a PINNED immutable version, and on the Governor project the console
  is pinned at @33 while a second deployment tracks @HEAD. Installable triggers
  run HEAD. So HEAD drift is what a time-driven monitor executes, and is the
  right thing to watch here - but "HEAD matches the repo" is NOT the same claim
  as "the pinned web app matches the repo". For that, export the pinned version
  with scripts/gas_get_version.py and compare against THAT.

USAGE
  .\scripts\gas_drift_watch.ps1
  .\scripts\gas_drift_watch.ps1 -Project governor-page-api
  .\scripts\gas_drift_watch.ps1 -SelfTest        # no clasp, no network
#>
param(
  [string]$Project = 'governor-page-api',
  [string]$RepoRoot,
  [switch]$Quiet,
  # Exercise the pure helpers with fixed inputs and no clasp, no network and no
  # credentials, so a hosted runner can check the parts that are checkable.
  [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# ---- pure helpers, above anything that needs clasp or a network -------------

function Resolve-OnDiskCase {
  <#
    Return the path as the filesystem actually spells it.

    clasp compares the path it is given against the canonical one and treats any
    difference as a symlink attack, so a lowercase spelling of a mixed-case
    directory makes every write fail with a message that names the wrong cause.
    GetDirectories with the path as the search pattern makes Windows hand back
    its own casing, one segment at a time.
  #>
  param([Parameter(Mandatory=$true)][string]$Path)
  $full = [IO.Path]::GetFullPath($Path)
  if (-not (Test-Path -LiteralPath $full)) { return $full }
  $di = New-Object IO.DirectoryInfo($full)
  $parts = New-Object System.Collections.Generic.List[string]
  while ($null -ne $di.Parent) {
    $match = $di.Parent.GetDirectories($di.Name) | Select-Object -First 1
    if ($match) { $parts.Insert(0, $match.Name) } else { $parts.Insert(0, $di.Name) }
    $di = $di.Parent
  }
  $root = $di.Name.ToUpperInvariant()
  if ($parts.Count -eq 0) { return $root }
  return (Join-Path $root ($parts -join [IO.Path]::DirectorySeparatorChar))
}

function Get-DriftVerdict {
  <#
    Turn the detector's exit code and the fetched-file count into one verdict.

    The file count is load-bearing: gas_baseline_check.py compares the files it
    is given, so a fetch that produced NOTHING compares nothing and reports no
    differences. That is the shape of a passing run and the substance of a run
    that never happened, so zero files is UNKNOWN and never IN_SYNC.
  #>
  param(
    [Parameter(Mandatory=$true)][AllowNull()][object]$CheckExitCode,
    [Parameter(Mandatory=$true)][int]$FetchedFileCount,
    [Parameter(Mandatory=$true)][bool]$FetchSucceeded
  )
  if (-not $FetchSucceeded)        { return @{ code = 2; verdict = 'UNKNOWN'; why = 'the fetch from Apps Script failed' } }
  if ($FetchedFileCount -le 0)     { return @{ code = 2; verdict = 'UNKNOWN'; why = 'the fetch returned no files, which compares as agreement and is not' } }
  if ($null -eq $CheckExitCode)    { return @{ code = 2; verdict = 'UNKNOWN'; why = 'the comparison did not report an exit code' } }
  if ($CheckExitCode -eq 0)        { return @{ code = 0; verdict = 'IN_SYNC'; why = "$FetchedFileCount file(s) compared, none differ" } }
  if ($CheckExitCode -eq 1)        { return @{ code = 1; verdict = 'DRIFT';   why = 'the committed baseline differs from deployed source' } }
  return @{ code = 2; verdict = 'UNKNOWN'; why = "the comparison exited $CheckExitCode, which is neither in-sync nor drift" }
}

# ---- self-test ---------------------------------------------------------------
if ($SelfTest) {
  $pass = 0; $fail = 0
  function Check { param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { $script:pass++; Write-Host "  ok   $Name" }
    else     { $script:fail++; Write-Host "  FAIL $Name $Detail" }
  }

  $v = Get-DriftVerdict -CheckExitCode 0 -FetchedFileCount 7 -FetchSucceeded $true
  Check 'a clean comparison over real files is IN_SYNC' ($v.verdict -eq 'IN_SYNC' -and $v.code -eq 0) "got $($v.verdict)/$($v.code)"

  $v = Get-DriftVerdict -CheckExitCode 1 -FetchedFileCount 7 -FetchSucceeded $true
  Check 'a differing comparison is DRIFT and exits 1' ($v.verdict -eq 'DRIFT' -and $v.code -eq 1) "got $($v.verdict)/$($v.code)"

  # THE ASSERTION THIS FILE EXISTS FOR. Zero files makes the detector report no
  # differences; that must never be allowed to read as agreement.
  $v = Get-DriftVerdict -CheckExitCode 0 -FetchedFileCount 0 -FetchSucceeded $true
  Check 'a fetch of ZERO files is UNKNOWN, never IN_SYNC' ($v.verdict -eq 'UNKNOWN' -and $v.code -eq 2) "got $($v.verdict)/$($v.code)"

  $v = Get-DriftVerdict -CheckExitCode 0 -FetchedFileCount 7 -FetchSucceeded $false
  Check 'a failed fetch is UNKNOWN even with a clean comparison' ($v.verdict -eq 'UNKNOWN' -and $v.code -eq 2) "got $($v.verdict)/$($v.code)"

  $v = Get-DriftVerdict -CheckExitCode $null -FetchedFileCount 7 -FetchSucceeded $true
  Check 'a missing exit code is UNKNOWN, not success' ($v.verdict -eq 'UNKNOWN' -and $v.code -eq 2) "got $($v.verdict)/$($v.code)"

  $v = Get-DriftVerdict -CheckExitCode 2 -FetchedFileCount 7 -FetchSucceeded $true
  Check 'an unrecognised exit code is UNKNOWN, not drift' ($v.verdict -eq 'UNKNOWN' -and $v.code -eq 2) "got $($v.verdict)/$($v.code)"

  $v = Get-DriftVerdict -CheckExitCode 137 -FetchedFileCount 7 -FetchSucceeded $true
  Check 'a killed comparison is UNKNOWN, not in-sync' ($v.verdict -eq 'UNKNOWN' -and $v.code -eq 2) "got $($v.verdict)/$($v.code)"

  # Case resolution, WINDOWS ONLY and deliberately so. The trap it defuses is a
  # Windows one: NTFS preserves case but matches case-insensitively, so a
  # lowercased path still opens the directory and clasp still rejects it. On a
  # case-SENSITIVE filesystem the lowercased name is simply a different path
  # that does not exist, so asserting the same thing there would be testing the
  # filesystem rather than this function - and it would fail for a reason that
  # has nothing to do with the defect.
  #
  # $IsWindows does not exist in Windows PowerShell 5.1 and reading a missing
  # variable under Set-StrictMode 2.0 THROWS, so it is probed, never read bare.
  $onWindows = if (Test-Path Variable:IsWindows) { [bool]$IsWindows } else { $true }
  if ($onWindows) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("GasCase_" + [Guid]::NewGuid().ToString('N').Substring(0,8) + "_MiXeD")
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
      $asked  = $tmp.ToLowerInvariant()
      $actual = Resolve-OnDiskCase -Path $asked
      $leafOk = (Split-Path -Leaf $actual) -ceq (Split-Path -Leaf $tmp)
      Check 'the on-disk casing is recovered from a lowercased path' $leafOk "asked [$(Split-Path -Leaf $asked)] got [$(Split-Path -Leaf $actual)]"
      Check 'the resolved path still points at the same directory' (
        (Get-Item -LiteralPath $actual).FullName -eq (Get-Item -LiteralPath $tmp).FullName)
    } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
  } else {
    Write-Host '  skip case-resolution assertions: this filesystem is case-sensitive, so they would test the filesystem, not the function'
  }

  # This one holds on every platform: an existing directory must come back as a
  # path that still opens the same directory, whatever the casing rules are.
  $same = Join-Path ([IO.Path]::GetTempPath()) ("GasSame_" + [Guid]::NewGuid().ToString('N').Substring(0,8))
  New-Item -ItemType Directory -Path $same | Out-Null
  try {
    Check 'an existing directory resolves to itself' (
      (Get-Item -LiteralPath (Resolve-OnDiskCase -Path $same)).FullName -eq (Get-Item -LiteralPath $same).FullName)
  } finally { Remove-Item -LiteralPath $same -Recurse -Force -ErrorAction SilentlyContinue }

  # A path that does not exist must come back absolute rather than throw, so the
  # caller can report a missing project instead of a stack trace.
  $ghost = Join-Path ([IO.Path]::GetTempPath()) ("no_such_dir_" + [Guid]::NewGuid().ToString('N'))
  Check 'a nonexistent path resolves without throwing' ([IO.Path]::IsPathRooted((Resolve-OnDiskCase -Path $ghost)))

  Write-Host ""
  Write-Host "RESULT passed=$pass failed=$fail"
  if ($fail -gt 0) { exit 1 }
  if ($pass -eq 0) { Write-Host 'no assertions ran, which is not a pass'; exit 1 }
  exit 0
}

# ---- the real run ------------------------------------------------------------
function Say { param([string]$m) if (-not $Quiet) { Write-Host $m } }

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$RepoRoot = Resolve-OnDiskCase -Path $RepoRoot

$baseline = Join-Path (Join-Path $RepoRoot 'apps-script') $Project
$claspCfg = Join-Path $RepoRoot '.clasp.json'
$checker  = Join-Path (Join-Path $RepoRoot 'scripts') 'gas_baseline_check.py'

foreach ($required in @($baseline, $claspCfg, $checker)) {
  if (-not (Test-Path -LiteralPath $required)) {
    Write-Host "UNKNOWN - missing: $required"
    Write-Host 'RESULT verdict=UNKNOWN'
    exit 2
  }
}

if (-not (Get-Command clasp -ErrorAction SilentlyContinue)) {
  Write-Host 'UNKNOWN - clasp is not on PATH, so deployed source cannot be read here.'
  Write-Host 'RESULT verdict=UNKNOWN'
  exit 2
}

$scratch = Join-Path ([IO.Path]::GetTempPath()) ("gas_drift_" + [Guid]::NewGuid().ToString('N'))
$fetchOk = $false
$fetched = 0
$checkRc = $null

try {
  New-Item -ItemType Directory -Path $scratch | Out-Null
  $scratch = Resolve-OnDiskCase -Path $scratch
  Copy-Item -LiteralPath $claspCfg -Destination (Join-Path $scratch '.clasp.json')

  Say "reading deployed source for '$Project' ..."
  Push-Location $scratch
  try {
    $null = & clasp pull 2>&1
    $fetchOk = ($LASTEXITCODE -eq 0)
  } finally { Pop-Location }

  $deployed = Join-Path (Join-Path $scratch 'apps-script') $Project
  if (Test-Path -LiteralPath $deployed) {
    $fetched = @(Get-ChildItem -LiteralPath $deployed -File | Where-Object { $_.Name -ne '.clasp.json' }).Count
  }

  if ($fetchOk -and $fetched -gt 0) {
    Say "comparing $fetched deployed file(s) against the committed baseline ..."
    Push-Location $RepoRoot
    try {
      $out = & python $checker --baseline $baseline --deployed-dir $deployed 2>&1
      $checkRc = $LASTEXITCODE
      if (-not $Quiet) { $out | ForEach-Object { Write-Host "  $_" } }
    } finally { Pop-Location }
  }
}
finally {
  # ALWAYS. This directory holds live Auth and Code pulled from production; it
  # must not survive a failure, a throw, or a Ctrl-C.
  if (Test-Path -LiteralPath $scratch) {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$v = Get-DriftVerdict -CheckExitCode $checkRc -FetchedFileCount $fetched -FetchSucceeded $fetchOk
Write-Host ""
Write-Host "$($v.verdict) - $($v.why)"
if ($v.code -eq 1) {
  Write-Host 'Do NOT deploy from this baseline. Review the diff, then re-baseline deliberately:'
  Write-Host "  python scripts/gas_baseline_check.py --baseline apps-script/$Project --version <N> --write"
}
Write-Host "RESULT verdict=$($v.verdict) files=$fetched"
exit $v.code
