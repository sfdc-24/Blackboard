#Requires -Version 5.1
<#
Blackboard - Alpha DB (V2 POC gateway) client for Windows PowerShell 5.1
claude-code-cli, 2026-08-28

WHY THIS EXISTS
  Same reasons as scripts/bus.ps1: DOCTRINE D-18 keeps credentials in this
  machine's local .env, never on a command line, never in Drive. This reads
  ALPHA_URL and ALPHA_SECRET from ..\.env at run time.

  The v1 bus stays the working system and the fallback (ORDER 017). This talks
  to the SEPARATE V2 gateway in front of the Google Sheet "Blackboard - Alpha DB".
  Nothing here touches the v1 bus or any Doc.

REDIRECT HANDLING -- the two shapes differ and it matters
  READ  is a GET with no body, so plain -L is safe and correct.
  WRITE is a POST with a body: curl -L re-issues the POST to the redirect target
        without a Content-Length and Google answers 411. So writes use the
        no-follow two-hop pattern REQ-PR4EXZ settled as standing law -- POST with
        redirects suppressed, then a single GET on the Location header.
        The Location key is ONE-SHOT: never probe it before reading it.

RULES THIS SCRIPT DOES NOT RELAX
  D-4: read-back is the only proof of a write. -Action append prints the response;
       it verifies nothing. Verify with -Action read and match on row_id.
       Never blind-retry an append.

USAGE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\alpha.ps1 -Action read
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\alpha.ps1 -Action append -SourceTag claude-code-cli -TargetSurface "V2 Sandbox" -Payload "text"
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\alpha.ps1 -Action raw -BodyFile probe.json   (adversarial probes: sends the file verbatim)
#>
param(
  [Parameter(Mandatory = $true, ParameterSetName = 'Run')]
  [ValidateSet('read', 'append', 'raw')]
  [string]$Action,
  [string]$SourceTag,
  [string]$TargetSurface,
  [string]$ActionType = 'APPEND',
  [string]$Payload,
  [string]$BodyFile,
  [switch]$NoSecret,
  [ValidateRange(1, 4)][int]$Retries = 4,
  # Exercise the secret-handling helper with fixed inputs, no .env and no
  # network. A separate parameter set so -Action stays mandatory for a real run
  # without -SelfTest having to supply it.
  [Parameter(Mandatory = $true, ParameterSetName = 'SelfTest')]
  [switch]$SelfTest,
  # Internal child used only by -SelfTest on Windows. It needs no .env and no
  # network, and deliberately exits nonzero after exercising a retained config.
  [Parameter(Mandatory = $true, ParameterSetName = 'CleanupLockProbe')]
  [switch]$CleanupLockProbe
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$script:AlphaCleanupFailureToken = 'ALPHA_SECRET_CONFIG_CLEANUP_FAILED'
$script:AlphaPrivacyFailureToken = 'ALPHA_SECRET_CONFIG_PRIVACY_FAILED'
$script:AlphaCreateFailureToken = 'ALPHA_SECRET_CONFIG_CREATE_FAILED'
$script:AlphaCleanupAttempts = 3
$script:AlphaCleanupRetryDelayMilliseconds = 50
$isWindowsVariable = Get-Variable -Name IsWindows -ErrorAction SilentlyContinue
$script:IsWindowsPlatform = if ($null -ne $isWindowsVariable) {
  [bool]$isWindowsVariable.Value
} else {
  $env:OS -ceq 'Windows_NT'
}

# Fixed, non-production canaries shared by the parent self-test and its lock
# child. Keeping them here lets the parent scour every child stream without
# ever passing a credential or URL on the child's command line.
$script:AlphaLockProbeSecret = 'LOCK_S3CR3T&with#hash+plus=eq/slash?q'
$script:AlphaLockProbeBase = 'https://lock-probe.example.invalid/exec'

function Remove-AlphaCredentialConfig {
  <#
    Delete one credential-bearing curl config and its owned private directory,
    retry a bounded number of times, and verify both paths are absent. No caught
    exception, path, URL, or file content is written to any stream. The only
    failure is one fixed token.

    Throwing from a caller's finally block is intentional: an otherwise valid
    response must not be returned when the credential file is still present.
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [ValidateRange(1, 16)][int]$Attempts = $script:AlphaCleanupAttempts,
    [ValidateRange(0, 1000)][int]$RetryDelayMilliseconds = $script:AlphaCleanupRetryDelayMilliseconds
  )

  # New-CurlSecretConfig is the only producer of this exact shape. Derive the
  # directory only when every component resolves to the expected TEMP child;
  # cleanup never recursively removes a directory or follows a reparse point.
  $privateDirectory = $null
  try {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $candidateDirectory = [IO.Path]::GetDirectoryName($fullPath)
    $candidateName = [IO.Path]::GetFileName($candidateDirectory)
    $candidateParent = [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($candidateDirectory)).TrimEnd('\', '/')
    $temporaryParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
    if ([IO.Path]::GetFileName($fullPath) -ceq 'request.conf' -and
        $candidateName -cmatch '^alpha_private_[0-9a-f]{32}$' -and
        $candidateParent -ceq $temporaryParent) {
      $privateDirectory = $candidateDirectory
    }
  } catch {
    $privateDirectory = $null
  }

  for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    try {
      if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction Stop # ALPHA_CREDENTIAL_CONFIG_DELETE_MUTATION_ANCHOR
      }
    } catch {
      # A live handle, scanner, or filesystem race may be transient. Never emit
      # the exception: even diagnostic streams for this helper stay secret-free.
    }

    if ($null -ne $privateDirectory) {
      try {
        if (Test-Path -LiteralPath $privateDirectory -PathType Container) {
          $directoryItem = Get-Item -LiteralPath $privateDirectory -Force -ErrorAction Stop
          if (($directoryItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
            [IO.Directory]::Delete($privateDirectory, $false)
          }
        }
      } catch {
        # A retained file or unexpected child keeps the directory nonempty. It
        # must be reported as cleanup failure, never recursively removed.
      }
    }

    $retained = $true
    try {
      $null = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
      $retained = $false
    } catch {
      # Inability to prove absence is not absence.
      $retained = $true
    }

    if (-not $retained -and $null -ne $privateDirectory) {
      try {
        $null = Get-Item -LiteralPath $privateDirectory -Force -ErrorAction Stop
        $retained = $true
      } catch [System.Management.Automation.ItemNotFoundException] {
        $retained = $false
      } catch {
        $retained = $true
      }
    }

    if (-not $retained) { return }
    if ($attempt -lt $Attempts -and $RetryDelayMilliseconds -gt 0) {
      # Default delays are 50 ms then 100 ms: bounded, but enough for a scanner
      # or just-exited curl process to release a transient handle.
      Start-Sleep -Milliseconds ($RetryDelayMilliseconds * $attempt)
    }
  }

  $cleanupFailure = [InvalidOperationException]::new($script:AlphaCleanupFailureToken)
  $cleanupFailure.Data['attempts'] = $Attempts
  throw $cleanupFailure
}

function Set-AlphaWindowsOwnerOnlyAcl {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [switch]$Directory
  )

  try {
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $security = if ($Directory) {
      New-Object Security.AccessControl.DirectorySecurity
    } else {
      New-Object Security.AccessControl.FileSecurity
    }
    $security.SetOwner($currentSid)
    $security.SetAccessRuleProtection($true, $false) # ALPHA_WINDOWS_PRIVATE_ACL_MUTATION_ANCHOR
    $inheritance = if ($Directory) {
      [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
      [Security.AccessControl.InheritanceFlags]::None
    }
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
      $currentSid,
      [Security.AccessControl.FileSystemRights]::FullControl,
      $inheritance,
      [Security.AccessControl.PropagationFlags]::None,
      [Security.AccessControl.AccessControlType]::Allow
    )
    $security.SetAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $security -ErrorAction Stop
  } catch {
    throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
  }
}

function Assert-AlphaPrivatePath {
  <#
    Verify the effective access state without emitting ACLs, identities, modes,
    or paths. Windows requires a protected DACL with exactly one explicit
    current-SID FullControl ACE. Unix requires exact owner-only mode bits.
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [switch]$Directory
  )

  try {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $expectedContainer = [bool]$Directory
    if ($expectedContainer -ne [bool]$item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
    }

    if ($script:IsWindowsPlatform) {
      $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
      $security = Get-Acl -LiteralPath $Path -ErrorAction Stop
      $ownerSid = $security.GetOwner([Security.Principal.SecurityIdentifier])
      $rules = @($security.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
      ))
      $expectedInheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
          [Security.AccessControl.InheritanceFlags]::ObjectInherit
      } else {
        [Security.AccessControl.InheritanceFlags]::None
      }
      $private = $security.AreAccessRulesProtected -and
        $ownerSid.Value -ceq $currentSid.Value -and
        $rules.Count -eq 1 -and
        $rules[0].IdentityReference.Value -ceq $currentSid.Value -and
        $rules[0].AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        [int64]$rules[0].FileSystemRights -eq [int64][Security.AccessControl.FileSystemRights]::FullControl -and
        $rules[0].InheritanceFlags -eq $expectedInheritance -and
        $rules[0].PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None -and
        -not $rules[0].IsInherited
    } else {
      $expectedMode = if ($Directory) { 448 } else { 384 } # 0700 / 0600
      $private = [int][IO.File]::GetUnixFileMode($Path) -eq $expectedMode
    }

    if (-not $private) {
      throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
    }
  } catch {
    throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
  }
}

function Assert-AlphaCredentialConfigPrivate {
  [CmdletBinding()]
  param([Parameter(Mandatory = $true)][string]$Path)

  $privateDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
  Assert-AlphaPrivatePath -Path $privateDirectory -Directory
  Assert-AlphaPrivatePath -Path $Path
}

# ---- keeping the read secret off the command line ---------------------------
# THE DEFECT THIS REPLACES, and it was in this file for a fortnight. The header
# of this script says, in its own words, that D-18 keeps credentials "never on a
# command line". The read path then built
#
#     $url = $cfg.ALPHA_URL + '?secret=' + $cfg.ALPHA_SECRET
#
# and passed $url as an ARGUMENT to curl.exe, where any local user can read it
# out of the process table while the transfer is in flight. The WRITE path in
# the same file already did the right thing - the secret goes in a JSON body
# written to a temp file and handed over as --data-binary @file - so the file
# contained both the rule, the correct pattern, and the violation at once.
#
# scripts/hear_in_zoom.ps1 and scripts/wa_inbound_digest.ps1 already solved this
# for header credentials with a curl config file and -K. This is the same fix
# for a query-string credential, so the fleet now applies one pattern
# everywhere rather than two thirds of the time.
#
# PERCENT-ENCODING IS NOT COSMETIC HERE. The old concatenation dropped the
# secret into a query string raw, so a '&' would have started a new parameter,
# a '#' would have truncated the URL at the fragment, and a '+' would have been
# read by the server as a space. That is a correctness bug independent of the
# exposure, and it fails as a mangled request rather than an obvious error.
function New-CurlSecretConfig {
  <#
    Write a curl config naming the full URL, and return its path. The caller
    passes it as `-K <path>` so the URL - and the credential inside it - is
    never an argv element. The caller must delete it with the strict helper.
  #>
  param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Secret
  )
  if ([string]::IsNullOrWhiteSpace($BaseUrl) -or $BaseUrl -match '\s') {
    throw [InvalidOperationException]::new('ALPHA_BASE_URL_INVALID')
  }
  $url = $BaseUrl + '?secret=' + [uri]::EscapeDataString($Secret)
  $privateDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    'alpha_private_' + [guid]::NewGuid().ToString('N'))
  $path = Join-Path $privateDirectory 'request.conf'
  # curl's unquoted config value ends at whitespace; a percent-encoded URL has
  # none. A quoted value would re-interpret backslash escapes, so it is
  # deliberately NOT quoted.
  $stream = $null
  $ownsPrivateDirectory = $false
  try {
    if (Test-Path -LiteralPath $privateDirectory) {
      throw [InvalidOperationException]::new($script:AlphaCreateFailureToken)
    }

    if ($script:IsWindowsPlatform) {
      $null = [IO.Directory]::CreateDirectory($privateDirectory)
      $ownsPrivateDirectory = $true
      Set-AlphaWindowsOwnerOnlyAcl -Path $privateDirectory -Directory
    } else {
      $unixModeType = [Type]::GetType('System.IO.UnixFileMode')
      if ($null -eq $unixModeType) {
        throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
      }
      $directoryMode = [Enum]::ToObject($unixModeType, 448) # 0700
      $null = [IO.Directory]::CreateDirectory($privateDirectory, $directoryMode)
      $ownsPrivateDirectory = $true
    }

    Assert-AlphaPrivatePath -Path $privateDirectory -Directory
    if ([IO.Directory]::GetFileSystemEntries($privateDirectory).Length -ne 0) {
      throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
    }

    if ($script:IsWindowsPlatform) {
      $stream = [IO.File]::Open(
        $path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
      )
      Set-AlphaWindowsOwnerOnlyAcl -Path $path
    } else {
      $optionsType = [Type]::GetType('System.IO.FileStreamOptions')
      $unixModeType = [Type]::GetType('System.IO.UnixFileMode')
      if ($null -eq $optionsType -or $null -eq $unixModeType) {
        throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
      }
      $fileOptions = [Activator]::CreateInstance($optionsType)
      $fileOptions.Mode = [IO.FileMode]::CreateNew
      $fileOptions.Access = [IO.FileAccess]::Write
      $fileOptions.Share = [IO.FileShare]::None
      $fileOptions.UnixCreateMode = [Enum]::ToObject($unixModeType, 384) # ALPHA_UNIX_PRIVATE_MODE_MUTATION_ANCHOR
      $stream = [IO.File]::Open($path, $fileOptions)
    }

    if ($stream.Length -ne 0) {
      throw [InvalidOperationException]::new($script:AlphaPrivacyFailureToken)
    }
    # The same assertion runs here while the file is still empty and again in
    # Invoke-AlphaReadRequest immediately before curl consumes the config.
    Assert-AlphaCredentialConfigPrivate -Path $path
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes("url = $url`n")
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
    Assert-AlphaCredentialConfigPrivate -Path $path
    return $path
  } catch {
    $failureToken = if ($_.Exception.Message -ceq $script:AlphaPrivacyFailureToken) {
      $script:AlphaPrivacyFailureToken
    } else {
      $script:AlphaCreateFailureToken
    }
    if ($null -ne $stream) {
      $stream.Dispose()
      $stream = $null
    }
    if ($ownsPrivateDirectory) {
      Remove-AlphaCredentialConfig -Path $path
    }
    throw [InvalidOperationException]::new($failureToken)
  } finally {
    if ($null -ne $stream) {
      $stream.Dispose()
    }
  }
}

function Get-AlphaReadCurlArgs {
  <#
    The exact argument vector the read uses. It exists as a function so the
    self-test exercises the SAME construction a real run does, rather than a
    copy of it that can drift.

    Reverting the old `$url` concatenation would have to change this function to
    pass a URL positionally, and the assertion below - that no element carries
    the credential - is what fails when someone does.
  #>
  param([Parameter(Mandatory = $true)][string]$ConfigPath)
  # curl only honors --disable as its first argument. This prevents an ambient
  # curlrc from enabling verbose/proxy behavior before the secret config loads.
  return @('--disable', '-s', '-S', '-L', '--max-time', '90', '-K', $ConfigPath)
}

function Invoke-AlphaReadRequest {
  <#
    Run the real read control flow. Transport is injectable only so -SelfTest
    can exercise these exact branches without a network or an .env file.

    The response is staged in memory. Nothing reaches the success stream until
    Remove-AlphaCredentialConfig has returned and therefore proved absence.
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Secret,
    [Parameter(Mandatory = $true)][string]$CurlPath,
    [Parameter(Mandatory = $true)][ValidateRange(1, 4)][int]$RetryCount,
    [scriptblock]$Transport,
    [ValidateRange(1, 16)][int]$CleanupAttempts = $script:AlphaCleanupAttempts,
    [ValidateRange(0, 1000)][int]$CleanupRetryDelayMilliseconds = $script:AlphaCleanupRetryDelayMilliseconds
  )

  $raw = ''
  $hasJson = $false
  for ($n = 1; $n -le $RetryCount; $n++) {
    # One config per request attempt keeps the credential's on-disk lifetime no
    # longer than the native process that consumes it. Even the next retry does
    # not begin until strict cleanup has proved this attempt's path absent.
    $configPath = New-CurlSecretConfig -BaseUrl $BaseUrl -Secret $Secret
    try {
      Assert-AlphaCredentialConfigPrivate -Path $configPath
      $curlArguments = @(Get-AlphaReadCurlArgs -ConfigPath $configPath)
      if ($null -eq $Transport) {
        $raw = (& $CurlPath @curlArguments) -join "`n"
      } else {
        $raw = (& $Transport $CurlPath $curlArguments) -join "`n"
      }
      if ($raw.TrimStart().StartsWith('{')) {
        $hasJson = $true
      }
    } finally {
      Remove-AlphaCredentialConfig -Path $configPath -Attempts $CleanupAttempts `
        -RetryDelayMilliseconds $CleanupRetryDelayMilliseconds
    }
    if ($hasJson) { break }
  }

  if ($hasJson) {
    Write-Output $raw
    return
  }
  Write-Warning "read returned non-JSON $RetryCount times (redirect artifact). Last response follows."
  Write-Output $raw
}

if ($CleanupLockProbe) {
  if (-not $script:IsWindowsPlatform) {
    throw [InvalidOperationException]::new('ALPHA_LOCK_NEGATIVE_CONTROL_WINDOWS_ONLY')
  }

  $script:AlphaLockProbePath = $null
  $script:AlphaLockProbeStream = $null
  $caughtCleanupFailure = $null
  try {
    try {
      Invoke-AlphaReadRequest -BaseUrl $script:AlphaLockProbeBase `
        -Secret $script:AlphaLockProbeSecret -CurlPath 'selftest-curl' `
        -RetryCount 1 -CleanupAttempts 3 -CleanupRetryDelayMilliseconds 10 `
        -Transport {
          param($IgnoredCurlPath, $CurlArguments)
          $script:AlphaLockProbePath = [string]$CurlArguments[-1]
          $script:AlphaLockProbeStream = [IO.File]::Open(
            $script:AlphaLockProbePath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
          )
          return '{"ok":true}'
        }
    } catch {
      $caughtCleanupFailure = $_
    }

    if ($null -eq $caughtCleanupFailure -or
        $caughtCleanupFailure.Exception.Message -cne $script:AlphaCleanupFailureToken -or
        [int]$caughtCleanupFailure.Exception.Data['attempts'] -ne 3) {
      throw [InvalidOperationException]::new('ALPHA_LOCK_NEGATIVE_CONTROL_INVALID')
    }
    if ([string]::IsNullOrWhiteSpace($script:AlphaLockProbePath) -or
        -not (Test-Path -LiteralPath $script:AlphaLockProbePath -PathType Leaf)) {
      throw [InvalidOperationException]::new('ALPHA_LOCK_NEGATIVE_CONTROL_INVALID')
    }
    Write-Output 'LOCK_PROBE retained=true cleanup_failure=true attempts=3'
  } finally {
    if ($null -ne $script:AlphaLockProbeStream) {
      $script:AlphaLockProbeStream.Dispose()
      $script:AlphaLockProbeStream = $null
    }
    if (-not [string]::IsNullOrWhiteSpace($script:AlphaLockProbePath)) {
      Remove-AlphaCredentialConfig -Path $script:AlphaLockProbePath
    }
  }

  if (Test-Path -LiteralPath $script:AlphaLockProbePath) {
    throw [InvalidOperationException]::new('ALPHA_LOCK_NEGATIVE_CONTROL_INVALID')
  }
  Write-Output 'LOCK_PROBE teardown_absent=true'
  throw [InvalidOperationException]::new($script:AlphaCleanupFailureToken)
}

if ($SelfTest) {
  $pass = 0; $fail = 0; $skipped = 0
  function Check { param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { $script:pass++; Write-Host "  ok   $Name" }
    else     { $script:fail++; Write-Host "  FAIL $Name $Detail" }
  }

  $platformName = if ($script:IsWindowsPlatform) { 'windows' } else { 'linux' }
  Write-Host "ENGINE edition=$($PSVersionTable.PSEdition) version=$($PSVersionTable.PSVersion) platform=$platformName"

  # A secret containing every character that breaks a raw query string.
  $canary = 'S3CR3T&with#hash+plus=eq/slash?q'
  $base   = 'https://example.invalid/exec'
  $cfgPath = New-CurlSecretConfig -BaseUrl $base -Secret $canary
  try {
    $privacyAssertionOutput = @(Assert-AlphaCredentialConfigPrivate -Path $cfgPath)
    Check 'config is owner-only and privacy assertion emits nothing before consumption' (
      $privacyAssertionOutput.Count -eq 0)
    $body = [IO.File]::ReadAllText($cfgPath)

    Check 'the config names the base URL' ($body -like "*$base*")
    # The assertion this whole change exists for, run against the SAME argv
    # builder the read path calls - not a copy of it written here.
    $argv = Get-AlphaReadCurlArgs -ConfigPath $cfgPath
    # BOTH FORMS, and the second is the one that matters. Mutation-testing this
    # suite showed the raw-substring check PASSING against the pre-fix defect,
    # because by then the secret in the URL is percent-encoded and the raw
    # canary no longer appears anywhere in it. A search for the plaintext is
    # blind to exactly the leak it was written to catch.
    $encoded = [uri]::EscapeDataString($canary)
    Check 'the secret is absent from every argv element, raw' (
      -not (@($argv | Where-Object { $_ -like "*$canary*" }).Count)) "argv=$($argv -join ' ')"
    Check 'the secret is absent from every argv element, percent-encoded' (
      -not (@($argv | Where-Object { $_ -like "*$encoded*" }).Count)) "argv=$($argv -join ' ')"
    Check 'curl --disable is argv index zero' ($argv[0] -ceq '--disable')
    Check 'the argv passes a config file rather than a URL' (
      $argv -contains '-K' -and $argv[-1] -eq $cfgPath) "argv=$($argv -join ' ')"
    Check 'no argv element looks like a URL at all' (
      -not (@($argv | Where-Object { $_ -like 'http*://*' }).Count)) "argv=$($argv -join ' ')"
    Check 'the raw secret never appears verbatim in the config either' ($body -notlike "*$canary*")
    Check 'the secret IS present, percent-encoded, so the request still works' (
      $body -like ("*" + [uri]::EscapeDataString($canary) + "*"))

    # Each character that would have corrupted the old concatenated URL.
    Check 'an ampersand is encoded and cannot start a new parameter' ($body -notlike '*&with*')
    Check 'a hash is encoded and cannot truncate the URL' ($body -notlike '*#hash*')
    Check 'the config is a single url line curl can read' (
      ($body -split "`n" | Where-Object { $_.Trim() } | Measure-Object).Count -eq 1)
    Check 'the config carries no quoting for curl to re-escape' ($body -notlike '*"*')
  } finally {
    Remove-AlphaCredentialConfig -Path $cfgPath
  }
  Check 'the config file and private directory are removed after use' (
    -not (Test-Path -LiteralPath $cfgPath) -and
    -not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($cfgPath))))

  # Two different calls must not collide, or a concurrent run reads or deletes
  # the other's credential.
  $p1 = New-CurlSecretConfig -BaseUrl $base -Secret 'a'
  $p2 = New-CurlSecretConfig -BaseUrl $base -Secret 'b'
  try { Check 'each call gets its own config path' ($p1 -ne $p2) }
  finally {
    Remove-AlphaCredentialConfig -Path $p1
    Remove-AlphaCredentialConfig -Path $p2
  }
  Check 'both unique config paths and private directories are removed' (
    -not (Test-Path -LiteralPath $p1) -and
    -not (Test-Path -LiteralPath $p2) -and
    -not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($p1))) -and
    -not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($p2))))

  $alreadyAbsentPath = Join-Path ([IO.Path]::GetTempPath()) (
    'alpha_absent_' + [Guid]::NewGuid().ToString('N') + '.conf')
  $absentCleanupOutput = @(Remove-AlphaCredentialConfig -Path $alreadyAbsentPath)
  Check 'strict cleanup is idempotent for an already absent path' (
    -not (Test-Path -LiteralPath $alreadyAbsentPath))
  Check 'successful strict cleanup emits nothing' ($absentCleanupOutput.Count -eq 0)

  $badBaseRejected = $false
  try {
    $null = New-CurlSecretConfig -BaseUrl ("https://example.invalid/exec`nurl = injected") -Secret $canary
  } catch {
    $badBaseRejected = $_.Exception.Message -ceq 'ALPHA_BASE_URL_INVALID'
  }
  Check 'base URL whitespace is rejected with a fixed diagnostic before writing' $badBaseRejected

  # Same-code normal success: the transport sees only the config path, the
  # response is preserved, and the path is absent before the function returns.
  $script:AlphaNormalCalls = 0
  $script:AlphaNormalPath = $null
  $normalOutput = @(Invoke-AlphaReadRequest -BaseUrl $base -Secret $canary `
    -CurlPath 'selftest-curl' -RetryCount 2 -Transport {
      param($IgnoredCurlPath, $CurlArguments)
      $script:AlphaNormalCalls++
      $script:AlphaNormalPath = [string]$CurlArguments[-1]
      return '{"ok":true,"case":"normal"}'
    })
  Check 'normal JSON response is preserved after cleanup' (
    $normalOutput.Count -eq 1 -and $normalOutput[0] -ceq '{"ok":true,"case":"normal"}')
  Check 'normal response stops after one transport call' ($script:AlphaNormalCalls -eq 1)
  Check 'normal response config is absent before return' (
    -not [string]::IsNullOrWhiteSpace($script:AlphaNormalPath) -and
    -not (Test-Path -LiteralPath $script:AlphaNormalPath) -and
    -not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($script:AlphaNormalPath))))

  # Same-code transport throw: the original fixed test error propagates only
  # after the strict helper has proved the credential file absent.
  $script:AlphaThrowPath = $null
  $throwWasPreserved = $false
  try {
    $null = Invoke-AlphaReadRequest -BaseUrl $base -Secret $canary `
      -CurlPath 'selftest-curl' -RetryCount 1 -Transport {
        param($IgnoredCurlPath, $CurlArguments)
        $script:AlphaThrowPath = [string]$CurlArguments[-1]
        throw [InvalidOperationException]::new('SELFTEST_TRANSPORT_FAILURE')
      }
  } catch {
    $throwWasPreserved = $_.Exception.Message -ceq 'SELFTEST_TRANSPORT_FAILURE'
  }
  Check 'transport failure is preserved' $throwWasPreserved
  Check 'transport failure config is absent before catch returns' (
    -not [string]::IsNullOrWhiteSpace($script:AlphaThrowPath) -and
    -not (Test-Path -LiteralPath $script:AlphaThrowPath) -and
    -not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($script:AlphaThrowPath))))

  # Same-code retry exhaustion: cleanup precedes the final non-JSON output too.
  $script:AlphaExhaustedCalls = 0
  $script:AlphaExhaustedPaths = @()
  $exhaustedOutput = @(Invoke-AlphaReadRequest -BaseUrl $base -Secret $canary `
    -CurlPath 'selftest-curl' -RetryCount 2 -WarningAction SilentlyContinue `
    -Transport {
      param($IgnoredCurlPath, $CurlArguments)
      $script:AlphaExhaustedCalls++
      $script:AlphaExhaustedPaths += [string]$CurlArguments[-1]
      return ('not-json-' + $script:AlphaExhaustedCalls)
    })
  Check 'retry exhaustion runs the exact requested count' ($script:AlphaExhaustedCalls -eq 2)
  Check 'retry exhaustion preserves only the final response' (
    $exhaustedOutput.Count -eq 1 -and $exhaustedOutput[0] -ceq 'not-json-2')
  Check 'retry exhaustion config is absent before return' (
    $script:AlphaExhaustedPaths.Count -eq 2 -and
    @($script:AlphaExhaustedPaths | Where-Object {
      (Test-Path -LiteralPath $_) -or
        (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($_)))
    }).Count -eq 0)

  if ($script:IsWindowsPlatform) {
    # A timer releases this real FileShare.None handle after the first cleanup
    # attempt. The staged operation must then succeed within the fixed bound,
    # emit nothing of its own, and leave no path behind.
    $transientPath = New-CurlSecretConfig -BaseUrl $base -Secret $canary
    $transientStream = [IO.File]::Open(
      $transientPath,
      [IO.FileMode]::Open,
      [IO.FileAccess]::ReadWrite,
      [IO.FileShare]::None
    )
    $transientTimer = New-Object Timers.Timer
    $transientTimer.Interval = 75
    $transientTimer.AutoReset = $false
    $transientSubscription = Register-ObjectEvent -InputObject $transientTimer -EventName Elapsed `
      -MessageData $transientStream -Action { $event.MessageData.Dispose() }
    $transientTimer.Start()
    $transientOk = $false
    try {
      $transientCleanupOutput = @(Remove-AlphaCredentialConfig -Path $transientPath)
      $transientOk = $transientCleanupOutput.Count -eq 0 -and
        -not (Test-Path -LiteralPath $transientPath)
    } finally {
      $transientTimer.Stop()
      $transientTimer.Dispose()
      Unregister-Event -SubscriptionId $transientSubscription.Id -ErrorAction SilentlyContinue
      Remove-Job -Id $transientSubscription.Id -Force -ErrorAction SilentlyContinue
      $transientStream.Dispose()
      if (Test-Path -LiteralPath $transientPath) {
        Remove-AlphaCredentialConfig -Path $transientPath
      }
    }
    if ($transientOk) { Write-Host 'LOCK_TRANSIENT PASS platform=windows' }
    else { Write-Host 'LOCK_TRANSIENT FAIL platform=windows' }
    Check 'transient lock recovers within the retry bound without output' $transientOk
  } else {
    $script:skipped++
    Write-Host 'LOCK_TRANSIENT SKIP platform=linux reason=open-file-unlink-semantics'
  }

  # Windows deletion semantics make FileShare.None a deterministic persistent
  # lock. Run it in a child so the suite proves a real nonzero command exit and
  # can scour stdout+stderr without leaking any canary while the child still
  # releases the handle and verifies teardown.
  if ($script:IsWindowsPlatform) {
    $childShell = if ($PSVersionTable.PSEdition -ceq 'Desktop') {
      Join-Path $PSHOME 'powershell.exe'
    } else {
      Join-Path $PSHOME 'pwsh.exe'
    }
    $processInfo = New-Object Diagnostics.ProcessStartInfo
    $processInfo.FileName = $childShell
    $processInfo.Arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      $PSCommandPath.Replace('"', '\"') + '" -CleanupLockProbe'
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true

    $child = [Diagnostics.Process]::Start($processInfo)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $completed = $child.WaitForExit(10000)
    if (-not $completed) {
      try { $child.Kill() } catch { }
      $child.WaitForExit()
    }
    $clock.Stop()
    $childStdout = $child.StandardOutput.ReadToEnd()
    $childStderr = $child.StandardError.ReadToEnd()
    $childExit = if ($completed) { $child.ExitCode } else { $null }
    $child.Dispose()

    $combinedChild = $childStdout + "`n" + $childStderr
    $lockEncoded = [uri]::EscapeDataString($script:AlphaLockProbeSecret)
    $lockQuery = '?secret=' + $lockEncoded
    $lockFullUrl = $script:AlphaLockProbeBase + $lockQuery
    $lockCanaries = @(
      $script:AlphaLockProbeSecret,
      $lockEncoded,
      $script:AlphaLockProbeBase,
      $lockQuery,
      $lockFullUrl
    )
    $lockLeakCount = @($lockCanaries | Where-Object { $combinedChild.Contains($_) }).Count
    $lockOk = $completed -and $clock.Elapsed.TotalSeconds -lt 10 -and
      $childExit -ne 0 -and
      $combinedChild.Contains($script:AlphaCleanupFailureToken) -and
      -not $combinedChild.Contains('ALPHA_LOCK_NEGATIVE_CONTROL_INVALID') -and
      -not $combinedChild.Contains('{"ok":true}') -and
      @($childStdout -split "`r?`n" | Where-Object { $_ -ceq 'LOCK_PROBE retained=true cleanup_failure=true attempts=3' }).Count -eq 1 -and
      @($childStdout -split "`r?`n" | Where-Object { $_ -ceq 'LOCK_PROBE teardown_absent=true' }).Count -eq 1 -and
      $combinedChild -cnotmatch 'alpha_private_[0-9a-f]{32}' -and
      $lockLeakCount -eq 0
    if ($lockOk) { Write-Host 'LOCK_NEGATIVE PASS platform=windows' }
    else { Write-Host 'LOCK_NEGATIVE FAIL platform=windows' }
    Check 'persistent lock fails bounded and secret-free before teardown' $lockOk
  } else {
    # POSIX permits unlinking an open file, so FileShare.None is not a retained-
    # path negative control on Linux. The normal/throw/exhaustion cleanup paths
    # still run above; the Windows-only retained-path case is explicitly counted.
    $script:skipped++
    Write-Host 'LOCK_NEGATIVE SKIP platform=linux reason=open-file-unlink-semantics'
  }

  Write-Host ""
  $cases = $pass + $fail + $skipped
  Write-Host "CASES total=$cases"
  Write-Host "RESULT passed=$pass failed=$fail skipped=$skipped"
  if ($fail -gt 0) { exit 1 }
  if ($pass -eq 0) { Write-Host 'no assertions ran, which is not a pass'; exit 1 }
  exit 0
}

$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
if (-not (Test-Path -LiteralPath $envFile)) { throw "env file not found: $envFile" }
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $envFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
if (-not $cfg.ALPHA_URL -or -not $cfg.ALPHA_SECRET) { throw "ALPHA_URL / ALPHA_SECRET missing in $envFile" }

$curlPath = (Get-Command curl.exe -ErrorAction Stop).Source

# ---- READ: GET, body-less, so following redirects is safe -------------------
if ($Action -eq 'read') {
  # The URL - and the secret in it - goes in a curl config, never in argv.
  Invoke-AlphaReadRequest -BaseUrl $cfg.ALPHA_URL -Secret $cfg.ALPHA_SECRET `
    -CurlPath $curlPath -RetryCount $Retries
  return
}

# ---- WRITE: build the body -------------------------------------------------
if ($Action -eq 'raw') {
  if (-not $BodyFile) { throw "-Action raw needs -BodyFile pointing at a JSON file to send verbatim." }
  $json = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $BodyFile).Path, [Text.Encoding]::UTF8)
} else {
  $body = @{
    source_tag     = $SourceTag
    target_surface = $TargetSurface
    action_type    = $ActionType
    payload        = $Payload
  }
  if (-not $NoSecret) { $body.secret = $cfg.ALPHA_SECRET }
  $json = $body | ConvertTo-Json -Depth 8 -Compress
}

$tmpBody = [IO.Path]::GetTempFileName()
$tmpHead = [IO.Path]::GetTempFileName()
$content = $null
try {
  [IO.File]::WriteAllText($tmpBody, $json, (New-Object Text.UTF8Encoding($false)))
  $out = & $curlPath -s -S -D $tmpHead --max-time 90 -X POST $cfg.ALPHA_URL `
           -H 'Content-Type: application/json' --data-binary "@$tmpBody"
  $head = Get-Content -LiteralPath $tmpHead -ErrorAction SilentlyContinue
  $locLine = @($head | Where-Object { $_ -like 'Location:*' -or $_ -like 'location:*' }) | Select-Object -First 1
  if ($locLine) {
    $target = $locLine.Substring($locLine.IndexOf(':') + 1).Trim()
    $content = (& $curlPath -s -S --max-time 90 $target) -join "`n"
  } else {
    $content = ($out) -join "`n"
  }
} finally {
  if (Test-Path -LiteralPath $tmpBody) { Remove-Item -LiteralPath $tmpBody -Force }
  if (Test-Path -LiteralPath $tmpHead) { Remove-Item -LiteralPath $tmpHead -Force }
}

if ($null -eq $content) { $content = '' }
if (-not $content.TrimStart().StartsWith('{')) {
  Write-Warning "response is not JSON. Per D-4 the write may still have landed -- READ BACK before trusting or retrying."
}
Write-Output $content
