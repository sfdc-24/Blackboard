#Requires -Version 5.1
<#
Offline acceptance driver for scripts/alpha.ps1.

The driver launches -SelfTest in a child of the SAME PowerShell engine so it
can assert the real exit code and scour stdout plus stderr. It then copies the
script, mutates exactly one anchored delete into a no-op, and requires the same
self-test to fail closed. No production credential, .env file, or network is
used. Child transcripts are never rendered by this driver.
#>
param(
  [string]$AlphaPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts/alpha.ps1')
)

$ErrorActionPreference = 'Stop'
$script:Passed = 0
$script:Failed = 0

function Assert-AlphaDriver {
  param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][bool]$Condition)
  if ($Condition) {
    $script:Passed++
    Write-Host "  ok   $Name"
  } else {
    $script:Failed++
    Write-Host "  FAIL $Name"
  }
}

function ConvertTo-AlphaProcessArgument {
  param([Parameter(Mandatory = $true)][string]$Value)
  if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
  # The paths created by this driver cannot contain a quote or end in a path
  # separator. Quoting the whitespace case is therefore identical on Desktop
  # PowerShell's .NET Framework and PowerShell Core's .NET runtime.
  return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-AlphaChild {
  param(
    [Parameter(Mandatory = $true)][string]$EnginePath,
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [Parameter(Mandatory = $true)][string[]]$ScriptArguments,
    [Parameter(Mandatory = $true)][string]$TemporaryRoot,
    [int]$TimeoutMilliseconds = 15000
  )

  $arguments = @('-NoLogo', '-NoProfile', '-NonInteractive')
  if ($script:IsWindowsPlatform) { $arguments += @('-ExecutionPolicy', 'Bypass') }
  $arguments += @('-File', $ScriptPath)
  $arguments += $ScriptArguments

  $info = New-Object Diagnostics.ProcessStartInfo
  $info.FileName = $EnginePath
  $info.Arguments = (@($arguments | ForEach-Object { ConvertTo-AlphaProcessArgument -Value $_ }) -join ' ')
  $info.UseShellExecute = $false
  $info.CreateNoWindow = $true
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  $info.EnvironmentVariables['TEMP'] = $TemporaryRoot
  $info.EnvironmentVariables['TMP'] = $TemporaryRoot
  $info.EnvironmentVariables['TMPDIR'] = $TemporaryRoot
  # The child modes are offline and do not read these values. Clearing them is
  # an extra negative boundary against accidental future diagnostic exposure.
  $info.EnvironmentVariables['ALPHA_URL'] = ''
  $info.EnvironmentVariables['ALPHA_SECRET'] = ''

  $process = [Diagnostics.Process]::Start($info)
  $stdoutTask = $process.StandardOutput.ReadToEndAsync()
  $stderrTask = $process.StandardError.ReadToEndAsync()
  $clock = [Diagnostics.Stopwatch]::StartNew()
  $completed = $process.WaitForExit($TimeoutMilliseconds)
  if (-not $completed) {
    try { $process.Kill() } catch { }
  }
  $process.WaitForExit()
  $clock.Stop()
  $stdout = $stdoutTask.GetAwaiter().GetResult()
  $stderr = $stderrTask.GetAwaiter().GetResult()
  $exitCode = if ($completed) { $process.ExitCode } else { $null }
  $process.Dispose()

  return [pscustomobject]@{
    completed = $completed
    elapsed_ms = [int][Math]::Ceiling($clock.Elapsed.TotalMilliseconds)
    exit_code = $exitCode
    stdout = $stdout
    stderr = $stderr
  }
}

function Get-ExactLineCount {
  param([string[]]$Lines, [string]$Expected)
  return @($Lines | Where-Object { $_ -ceq $Expected }).Count
}

$isWindowsVariable = Get-Variable -Name IsWindows -ErrorAction SilentlyContinue
$script:IsWindowsPlatform = if ($null -ne $isWindowsVariable) {
  [bool]$isWindowsVariable.Value
} else {
  $env:OS -ceq 'Windows_NT'
}
$platformName = if ($script:IsWindowsPlatform) { 'windows' } else { 'linux' }
$engineName = if ($PSVersionTable.PSEdition -ceq 'Desktop') {
  'powershell.exe'
} elseif ($script:IsWindowsPlatform) {
  'pwsh.exe'
} else {
  'pwsh'
}
$enginePath = Join-Path $PSHOME $engineName
$resolvedAlphaPath = (Resolve-Path -LiteralPath $AlphaPath -ErrorAction Stop).Path
$expectedEngine = "ENGINE edition=$($PSVersionTable.PSEdition) version=$($PSVersionTable.PSVersion) platform=$platformName"
$expectedCases = 'CASES total=29'
$expectedResult = if ($script:IsWindowsPlatform) {
  'RESULT passed=29 failed=0 skipped=0'
} else {
  'RESULT passed=27 failed=0 skipped=2'
}
$expectedPassLines = if ($script:IsWindowsPlatform) { 29 } else { 27 }

$primarySecret = 'S3CR3T&with#hash+plus=eq/slash?q'
$primaryEncoded = [uri]::EscapeDataString($primarySecret)
$primaryBase = 'https://example.invalid/exec'
$lockSecret = 'LOCK_S3CR3T&with#hash+plus=eq/slash?q'
$lockEncoded = [uri]::EscapeDataString($lockSecret)
$lockBase = 'https://lock-probe.example.invalid/exec'
$canaries = @(
  $primarySecret,
  $primaryEncoded,
  $primaryBase,
  ('?secret=' + $primaryEncoded),
  ($primaryBase + '?secret=' + $primaryEncoded),
  $lockSecret,
  $lockEncoded,
  $lockBase,
  ('?secret=' + $lockEncoded),
  ($lockBase + '?secret=' + $lockEncoded)
)

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('alpha-security-driver-' + [Guid]::NewGuid().ToString('N'))
$cleanupFailed = $false
New-Item -ItemType Directory -Path $testRoot -ErrorAction Stop | Out-Null
Write-Host "DRIVER_ENGINE edition=$($PSVersionTable.PSEdition) version=$($PSVersionTable.PSVersion) platform=$platformName"

try {
  $normalRoot = Join-Path $testRoot 'normal'
  New-Item -ItemType Directory -Path $normalRoot -ErrorAction Stop | Out-Null
  $normal = Invoke-AlphaChild -EnginePath $enginePath -ScriptPath $resolvedAlphaPath `
    -ScriptArguments @('-SelfTest') -TemporaryRoot $normalRoot
  $normalCombined = $normal.stdout + "`n" + $normal.stderr
  $normalLines = @($normal.stdout -split "`r?`n" | Where-Object { $_.Length -gt 0 })
  $normalLeakCount = @($canaries | Where-Object { $normalCombined.Contains($_) }).Count
  $normalResidue = @(Get-ChildItem -LiteralPath $normalRoot -Recurse -Force -ErrorAction Stop |
    Where-Object { $_.Name -ceq 'request.conf' -or $_.Name -cmatch '^alpha_private_[0-9a-f]{32}$' })

  Assert-AlphaDriver 'self-test child completes with exact zero exit' (
    $normal.completed -and $normal.exit_code -eq 0 -and $normal.elapsed_ms -lt 15000)
  Assert-AlphaDriver 'self-test emits one exact engine identity' (
    (Get-ExactLineCount -Lines $normalLines -Expected $expectedEngine) -eq 1)
  Assert-AlphaDriver 'self-test emits one exact case count' (
    (Get-ExactLineCount -Lines $normalLines -Expected $expectedCases) -eq 1)
  Assert-AlphaDriver 'self-test result is exact, unique, and terminal' (
    (Get-ExactLineCount -Lines $normalLines -Expected $expectedResult) -eq 1 -and
    $normalLines[-1] -ceq $expectedResult)
  Assert-AlphaDriver 'self-test assertion count and failure surface are exact' (
    @($normalLines | Where-Object { $_ -like '  ok   *' }).Count -eq $expectedPassLines -and
    @($normalLines | Where-Object { $_ -like '  FAIL *' }).Count -eq 0)
  if ($script:IsWindowsPlatform) {
    Assert-AlphaDriver 'Windows lock negative is explicit and not skipped' (
      (Get-ExactLineCount -Lines $normalLines -Expected 'LOCK_NEGATIVE PASS platform=windows') -eq 1 -and
      @($normalLines | Where-Object { $_ -like 'LOCK_NEGATIVE SKIP*' }).Count -eq 0 -and
      (Get-ExactLineCount -Lines $normalLines -Expected 'LOCK_TRANSIENT PASS platform=windows') -eq 1 -and
      @($normalLines | Where-Object { $_ -like 'LOCK_TRANSIENT SKIP*' }).Count -eq 0)
  } else {
    Assert-AlphaDriver 'Linux lock semantics are explicitly skipped and counted' (
      (Get-ExactLineCount -Lines $normalLines -Expected 'LOCK_NEGATIVE SKIP platform=linux reason=open-file-unlink-semantics') -eq 1 -and
      @($normalLines | Where-Object { $_ -like 'LOCK_NEGATIVE PASS*' }).Count -eq 0 -and
      (Get-ExactLineCount -Lines $normalLines -Expected 'LOCK_TRANSIENT SKIP platform=linux reason=open-file-unlink-semantics') -eq 1 -and
      @($normalLines | Where-Object { $_ -like 'LOCK_TRANSIENT PASS*' }).Count -eq 0)
  }
  Assert-AlphaDriver 'self-test stderr is empty and no config residue remains' (
    [string]::IsNullOrEmpty($normal.stderr) -and $normalResidue.Count -eq 0)
  Assert-AlphaDriver 'combined self-test streams contain no raw, encoded, base, query, or full URL canary' (
    $normalLeakCount -eq 0)

  $source = [IO.File]::ReadAllText($resolvedAlphaPath, [Text.Encoding]::UTF8)
  $privacyLine = if ($script:IsWindowsPlatform) {
    '$security.SetAccessRuleProtection($true, $false) # ALPHA_WINDOWS_PRIVATE_ACL_MUTATION_ANCHOR'
  } else {
    '$fileOptions.UnixCreateMode = [Enum]::ToObject($unixModeType, 384) # ALPHA_UNIX_PRIVATE_MODE_MUTATION_ANCHOR'
  }
  $privacyReplacement = if ($script:IsWindowsPlatform) {
    '$security.SetAccessRuleProtection($false, $true) # ALPHA_WINDOWS_PRIVATE_ACL_MUTATION_ANCHOR'
  } else {
    '$fileOptions.UnixCreateMode = [Enum]::ToObject($unixModeType, 0) # ALPHA_UNIX_PRIVATE_MODE_MUTATION_ANCHOR'
  }
  $firstPrivacyAnchor = $source.IndexOf($privacyLine, [StringComparison]::Ordinal)
  $secondPrivacyAnchor = if ($firstPrivacyAnchor -ge 0) {
    $source.IndexOf($privacyLine, $firstPrivacyAnchor + $privacyLine.Length, [StringComparison]::Ordinal)
  } else {
    -1
  }
  $privacyAnchorIsUnique = $firstPrivacyAnchor -ge 0 -and $secondPrivacyAnchor -lt 0
  Assert-AlphaDriver 'platform privacy mutation anchor is unique' $privacyAnchorIsUnique

  if ($privacyAnchorIsUnique) {
    $privacyRoot = Join-Path $testRoot 'privacy-mutation'
    New-Item -ItemType Directory -Path $privacyRoot -ErrorAction Stop | Out-Null
    $privacyMutatedPath = Join-Path $privacyRoot 'alpha-privacy-mutated.ps1'
    $privacyMutatedSource = $source.Replace($privacyLine, $privacyReplacement)
    [IO.File]::WriteAllText($privacyMutatedPath, $privacyMutatedSource, (New-Object Text.UTF8Encoding($false)))

    $privacyMutation = Invoke-AlphaChild -EnginePath $enginePath -ScriptPath $privacyMutatedPath `
      -ScriptArguments @('-SelfTest') -TemporaryRoot $privacyRoot
    $privacyCombined = $privacyMutation.stdout + "`n" + $privacyMutation.stderr
    $privacyLines = @($privacyMutation.stdout -split "`r?`n" | Where-Object { $_.Length -gt 0 })
    $privacyLeakCount = @($canaries | Where-Object { $privacyCombined.Contains($_) }).Count
    $privacyResidue = @(Get-ChildItem -LiteralPath $privacyRoot -Recurse -Force -ErrorAction Stop |
      Where-Object { $_.Name -ceq 'request.conf' -or $_.Name -cmatch '^alpha_private_[0-9a-f]{32}$' })

    Assert-AlphaDriver 'privacy mutation fails nonzero and bounded before consumption' (
      $privacyMutation.completed -and $privacyMutation.exit_code -eq 1 -and
      $privacyMutation.elapsed_ms -lt 15000)
    Assert-AlphaDriver 'privacy mutation reaches the fixed privacy token without a green result or path' (
      $privacyCombined.Contains('ALPHA_SECRET_CONFIG_PRIVACY_FAILED') -and
      (Get-ExactLineCount -Lines $privacyLines -Expected $expectedResult) -eq 0 -and
      $privacyCombined -cnotmatch 'alpha_private_[0-9a-f]{32}')
    Assert-AlphaDriver 'privacy mutation cleans its empty private objects before returning failure' (
      $privacyResidue.Count -eq 0)
    Assert-AlphaDriver 'privacy mutation combined streams contain no raw, encoded, base, query, or full URL canary' (
      $privacyLeakCount -eq 0)
  }

  $deleteLine = 'Remove-Item -LiteralPath $Path -Force -ErrorAction Stop # ALPHA_CREDENTIAL_CONFIG_DELETE_MUTATION_ANCHOR'
  $firstAnchor = $source.IndexOf($deleteLine, [StringComparison]::Ordinal)
  $secondAnchor = if ($firstAnchor -ge 0) {
    $source.IndexOf($deleteLine, $firstAnchor + $deleteLine.Length, [StringComparison]::Ordinal)
  } else {
    -1
  }
  $anchorIsUnique = $firstAnchor -ge 0 -and $secondAnchor -lt 0
  Assert-AlphaDriver 'cleanup delete mutation anchor is unique' $anchorIsUnique

  if ($anchorIsUnique) {
    $mutationRoot = Join-Path $testRoot 'mutation'
    New-Item -ItemType Directory -Path $mutationRoot -ErrorAction Stop | Out-Null
    $mutatedPath = Join-Path $mutationRoot 'alpha-mutated.ps1'
    $replacement = '$null = $Path # ALPHA_CREDENTIAL_CONFIG_DELETE_MUTATION_NOOP'
    $mutatedSource = $source.Replace($deleteLine, $replacement)
    [IO.File]::WriteAllText($mutatedPath, $mutatedSource, (New-Object Text.UTF8Encoding($false)))

    $mutation = Invoke-AlphaChild -EnginePath $enginePath -ScriptPath $mutatedPath `
      -ScriptArguments @('-SelfTest') -TemporaryRoot $mutationRoot
    $mutationCombined = $mutation.stdout + "`n" + $mutation.stderr
    $mutationLines = @($mutation.stdout -split "`r?`n" | Where-Object { $_.Length -gt 0 })
    $mutationLeakCount = @($canaries | Where-Object { $mutationCombined.Contains($_) }).Count
    $mutationResidue = @(Get-ChildItem -LiteralPath $mutationRoot -Recurse -Force -ErrorAction Stop |
      Where-Object { $_.Name -ceq 'request.conf' -or $_.Name -cmatch '^alpha_private_[0-9a-f]{32}$' })

    Assert-AlphaDriver 'no-op cleanup mutation fails nonzero and bounded' (
      $mutation.completed -and $mutation.exit_code -eq 1 -and $mutation.elapsed_ms -lt 15000)
    Assert-AlphaDriver 'no-op cleanup mutation reaches the fixed cleanup token without a green result' (
      $mutationCombined.Contains('ALPHA_SECRET_CONFIG_CLEANUP_FAILED') -and
      (Get-ExactLineCount -Lines $mutationLines -Expected $expectedResult) -eq 0 -and
      $mutationCombined -cnotmatch 'alpha_private_[0-9a-f]{32}')
    Assert-AlphaDriver 'no-op cleanup mutation leaves detectable isolated residue before harness teardown' (
      $mutationResidue.Count -ge 1)
    Assert-AlphaDriver 'no-op mutation combined streams contain no raw, encoded, base, query, or full URL canary' (
      $mutationLeakCount -eq 0)
  }
} finally {
  try {
    if (Test-Path -LiteralPath $testRoot) {
      Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction Stop
    }
  } catch {
    $cleanupFailed = $true
  }
}

Assert-AlphaDriver 'driver removes all isolated normal and mutation artifacts' (
  -not $cleanupFailed -and -not (Test-Path -LiteralPath $testRoot))

Write-Host ''
$driverCases = $script:Passed + $script:Failed
Write-Host "DRIVER_CASES total=$driverCases"
Write-Host "DRIVER_RESULT passed=$($script:Passed) failed=$($script:Failed)"
if ($script:Failed -gt 0) { exit 1 }
if ($script:Passed -eq 0) { exit 1 }
exit 0
