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
  [Parameter(Mandatory = $true, ParameterSetName = 'WriteSelfTest')]
  [switch]$WriteSelfTest,
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

  # New-AlphaPrivateFile is the only producer of this exact shape. Derive the
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
# the same file kept the secret out of argv with --data-binary @file. Its body
# and redirect headers now also use private storage with verified cleanup.
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
function New-AlphaPrivateFile {
  <#
    Create one owner-only file before writing any content. Each file owns a
    separate private directory so the existing strict, nonrecursive cleanup
    contract applies independently to bodies, headers, diagnostics and configs.
  #>
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
  )
  $privateDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    'alpha_private_' + [guid]::NewGuid().ToString('N'))
  $path = Join-Path $privateDirectory 'request.conf'
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
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Content)
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

function New-CurlSecretConfig {
  param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Secret
  )
  if ([string]::IsNullOrWhiteSpace($BaseUrl) -or $BaseUrl -match '\s') {
    throw [InvalidOperationException]::new('ALPHA_BASE_URL_INVALID')
  }
  $url = $BaseUrl + '?secret=' + [uri]::EscapeDataString($Secret)
  # Unquoted curl values end at whitespace; the encoded secret has none.
  New-AlphaPrivateFile -Content "url = $url`n"
}

function New-CurlUrlConfig {
  param([Parameter(Mandatory = $true)][string]$Url)
  $parsed = $null
  if ([string]::IsNullOrWhiteSpace($Url) -or $Url -match '\s' -or
      -not [uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$parsed) -or
      $parsed.Scheme -cne 'https') {
    throw [InvalidOperationException]::new('ALPHA_WRITE_URL_INVALID')
  }
  # In particular, a one-shot Location is credential material, never argv.
  New-AlphaPrivateFile -Content "url = $Url`n"
}

function Invoke-AlphaWriteTransport {
  param(
    [Parameter(Mandatory = $true)][string]$CurlPath,
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [Parameter(Mandatory = $true)][string]$DiagnosticsPath,
    [scriptblock]$Transport
  )
  if ($null -ne $Transport) {
    return (& $Transport $CurlPath $Arguments $DiagnosticsPath)
  }
  # Diagnostics can contain a secret URL or private filename. Redirect them to
  # an already-private owned file, never to any caller stream, even on exit0.
  $priorPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $output = @(& $CurlPath @Arguments 2> $DiagnosticsPath)
    $nativeExit = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $priorPreference
  }
  [pscustomobject]@{ ExitCode = $nativeExit; Output = $output }
}

function Invoke-AlphaWriteRequest {
  <#
    One POST, followed by at most one bodyless GET. Never retry a write here.
    Stage responses until every owned file and directory is verified absent.
    An error after dispatch is an unconfirmed outcome, not proof of no write.
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Json,
    [Parameter(Mandatory = $true)][string]$CurlPath,
    [scriptblock]$Transport,
    [ValidateRange(1, 16)][int]$CleanupAttempts = $script:AlphaCleanupAttempts,
    [ValidateRange(0, 1000)][int]$CleanupRetryDelayMilliseconds = $script:AlphaCleanupRetryDelayMilliseconds
  )
  $ownedPaths = New-Object System.Collections.Generic.List[string]
  $postStarted = $false
  $failureToken = $null
  $cleanupFailed = $false
  $content = ''
  try {
    $bodyPath = New-AlphaPrivateFile -Content $Json
    $ownedPaths.Add($bodyPath)
    $headerPath = New-AlphaPrivateFile -Content ''
    $ownedPaths.Add($headerPath)
    $baseConfig = New-CurlUrlConfig -Url $BaseUrl
    $ownedPaths.Add($baseConfig)
    $diagnosticsPath = New-AlphaPrivateFile -Content ''
    $ownedPaths.Add($diagnosticsPath)
    foreach ($path in $ownedPaths) { Assert-AlphaCredentialConfigPrivate -Path $path }

    $postArguments = @('--disable', '-s', '-S', '--max-time', '90',
      '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '0',
      '-D', $headerPath, '-X', 'POST', '-H', 'Content-Type: application/json',
      '--data-binary', ('@' + $bodyPath), '-K', $baseConfig)
    $postStarted = $true
    $response = Invoke-AlphaWriteTransport -CurlPath $CurlPath -Arguments $postArguments `
      -DiagnosticsPath $diagnosticsPath -Transport $Transport
    if ($null -eq $response -or $response.ExitCode -isnot [int] -or $response.ExitCode -ne 0) {
      throw [InvalidOperationException]::new('ALPHA_WRITE_TRANSPORT_FAILED')
    }
    foreach ($path in $ownedPaths) { Assert-AlphaCredentialConfigPrivate -Path $path }
    $content = (@($response.Output) | ForEach-Object { [string]$_ }) -join "`n"
    $headers = [IO.File]::ReadAllLines($headerPath)
    $locationLine = @($headers | Where-Object { $_ -match '^(?i:Location):' } | Select-Object -First 1)
    if ($locationLine.Count -eq 1) {
      $target = $locationLine[0].Substring($locationLine[0].IndexOf(':') + 1).Trim()
      $targetConfig = New-CurlUrlConfig -Url $target
      $ownedPaths.Add($targetConfig)
      foreach ($path in $ownedPaths) { Assert-AlphaCredentialConfigPrivate -Path $path }
      # No -L, POST, body, automatic retry or positional URL on the second hop.
      $getArguments = @('--disable', '-s', '-S', '--max-time', '90',
        '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '0',
        '-D', $headerPath, '-K', $targetConfig)
      $response = Invoke-AlphaWriteTransport -CurlPath $CurlPath -Arguments $getArguments `
        -DiagnosticsPath $diagnosticsPath -Transport $Transport
      if ($null -eq $response -or $response.ExitCode -isnot [int] -or $response.ExitCode -ne 0) {
        throw [InvalidOperationException]::new('ALPHA_WRITE_TRANSPORT_FAILED')
      }
      foreach ($path in $ownedPaths) { Assert-AlphaCredentialConfigPrivate -Path $path }
      $content = (@($response.Output) | ForEach-Object { [string]$_ }) -join "`n"
      if (@([IO.File]::ReadAllLines($headerPath) | Where-Object { $_ -match '^(?i:Location):' }).Count -gt 0) {
        throw [InvalidOperationException]::new('ALPHA_WRITE_REDIRECT_LIMIT')
      }
    }
    if (-not $content.TrimStart().StartsWith('{')) {
      # HTML and transport error pages can echo a one-shot URL. Do not expose
      # them as response output. A write may already have landed; never retry.
      throw [InvalidOperationException]::new('ALPHA_WRITE_RESPONSE_UNCONFIRMED')
    }
    # A leading brace alone does not make an error page valid JSON. Validate
    # without reserializing: callers keep the original valid response text.
    $null = ConvertFrom-Json -InputObject $content -ErrorAction Stop
  } catch {
    # The original native/script exception and its inner chain can contain the
    # payload, URL or filenames. Only fixed diagnostics cross this boundary.
    if ($_.Exception.Message -ceq $script:AlphaCleanupFailureToken) { $cleanupFailed = $true }
    $failureToken = if ($postStarted) {
      'ALPHA_WRITE_OUTCOME_UNCONFIRMED: READ BACK before retrying.'
    } else {
      'ALPHA_WRITE_PREPARATION_FAILED'
    }
  } finally {
    # Each path owns a distinct private directory. A locked first body must not
    # prevent cleanup of headers, diagnostics, or either credential URL config.
    foreach ($path in $ownedPaths) {
      try {
        Remove-AlphaCredentialConfig -Path $path -Attempts $CleanupAttempts `
          -RetryDelayMilliseconds $CleanupRetryDelayMilliseconds
      } catch {
        $cleanupFailed = $true
      }
    }
  }
  if ($cleanupFailed) {
    $failureToken = 'ALPHA_WRITE_CLEANUP_FAILED: READ BACK before retrying; any dispatched write may have landed.'
  }
  if ($null -ne $failureToken) {
    $failure = [InvalidOperationException]::new($failureToken)
    $failure.Data['post_started'] = $postStarted
    throw $failure
  }
  Write-Output $content
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

if ($WriteSelfTest) {
  # Offline write acceptance uses the production helper and private-file maker.
  # Only this mode wraps creation to record ownership and inject a partial failure.
  $script:AlphaWritePass = 0; $script:AlphaWriteFail = 0; $script:AlphaWriteSkip = 0
  $script:AlphaWriteAllPaths = @()
  $script:AlphaWriteOriginalPrivateFile = (Get-Command New-AlphaPrivateFile).ScriptBlock
  $script:AlphaWriteTestSecret = 'WRITE_S3CR3T&hash#plus+eq=slash/question?'
  $script:AlphaWriteTestBase = 'https://write-base.example.invalid/exec?token=WRITE_BASE_CANARY'
  $script:AlphaWriteTestRedirect = 'https://write-hop.example.invalid/once?token=WRITE_ONE_SHOT_CANARY'
  $script:AlphaWriteTestJson = '{"secret":"' + $script:AlphaWriteTestSecret + '","operation":"append","text":"synthetic"}'
  $script:AlphaWriteSuccess = "{`r`n  `"ok`": true, `"case`": `"WRITE_SUCCESS_SENTINEL`", `"spacing`": [1,  2]`r`n}  "
  $script:AlphaWriteLockStream = $null
  $writePlatform = if ($script:IsWindowsPlatform) { 'windows' } else { 'linux' }
  Write-Host "WRITE_ENGINE edition=$($PSVersionTable.PSEdition) version=$($PSVersionTable.PSVersion) platform=$writePlatform"
  Write-Host 'WRITE_MODE active=true'

  function Check-AlphaWrite {
    param([string]$Name, [bool]$Ok)
    if ($Ok) { $script:AlphaWritePass++; Write-Host "  write-ok   $Name" }
    else { $script:AlphaWriteFail++; Write-Host "  WRITE_FAIL $Name" }
  }
  function New-AlphaPrivateFile {
    param([AllowEmptyString()][string]$Content)
    $script:AlphaWriteCreateCalls++
    if ($script:AlphaWriteCreateFailAt -gt 0 -and
        $script:AlphaWriteCreateCalls -eq $script:AlphaWriteCreateFailAt) {
      throw [InvalidOperationException]::new('WRITE_CREATION_EXCEPTION_CANARY ' + $script:AlphaWriteTestSecret)
    }
    $made = & $script:AlphaWriteOriginalPrivateFile -Content $Content
    $script:AlphaWritePaths += [string]$made
    $script:AlphaWriteAllPaths += [string]$made
    return $made
  }
  function Get-AlphaWriteTestArgument {
    param([string[]]$Arguments, [string[]]$Names)
    for ($i = 0; $i -lt ($Arguments.Count - 1); $i++) {
      if ($Arguments[$i] -cin $Names) { return [string]$Arguments[$i + 1] }
    }
    return $null
  }
  function Test-AlphaWritePathsAbsent {
    param([string[]]$Paths)
    foreach ($path in $Paths) {
      if ((Test-Path -LiteralPath $path) -or
          (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($path)))) { return $false }
    }
    return $true
  }
  function Invoke-AlphaWriteSelfCase {
    param([string]$Name, [int]$CreateFailAt = 0)
    $script:AlphaWriteScenario = $Name
    $script:AlphaWritePaths = @(); $script:AlphaWriteCalls = @()
    $script:AlphaWriteCreateCalls = 0; $script:AlphaWriteCreateFailAt = $CreateFailAt
    $script:AlphaWriteOutputBeforeCleanup = $false
    $script:AlphaWritePrivacyDriftApplied = $false
    $output = New-Object 'System.Collections.Generic.List[object]'
    $caught = $null
    try {
      Invoke-AlphaWriteRequest -BaseUrl $script:AlphaWriteTestBase -Json $script:AlphaWriteTestJson `
        -CurlPath 'offline-write-curl' -CleanupAttempts 2 -CleanupRetryDelayMilliseconds 0 `
        -Transport {
          param($IgnoredCurlPath, [string[]]$Arguments, $DiagnosticsPath)
          $method = Get-AlphaWriteTestArgument -Arguments $Arguments -Names @('-X', '--request')
          if ([string]::IsNullOrEmpty($method)) { $method = 'GET' }
          $headerPath = Get-AlphaWriteTestArgument -Arguments $Arguments -Names @('-D', '--dump-header')
          $bodyArgument = Get-AlphaWriteTestArgument -Arguments $Arguments -Names @('--data-binary')
          $urlPath = Get-AlphaWriteTestArgument -Arguments $Arguments -Names @('-K', '--config')
          $bodyPath = if ($bodyArgument -and $bodyArgument.StartsWith('@')) { $bodyArgument.Substring(1) } else { $null }
          $private = $true
          try {
            foreach ($path in $script:AlphaWritePaths) { Assert-AlphaCredentialConfigPrivate -Path $path }
            Assert-AlphaCredentialConfigPrivate -Path $headerPath
            Assert-AlphaCredentialConfigPrivate -Path $DiagnosticsPath
            Assert-AlphaCredentialConfigPrivate -Path $urlPath
            if ($method -ceq 'POST') { Assert-AlphaCredentialConfigPrivate -Path $bodyPath }
          } catch { $private = $false }
          $bodyExact = $method -cne 'POST' -or
            ($bodyPath -and [IO.File]::ReadAllText($bodyPath) -ceq $script:AlphaWriteTestJson)
          $expectedUrl = if ($method -ceq 'POST') { $script:AlphaWriteTestBase } else { $script:AlphaWriteTestRedirect }
          $urlExact = $urlPath -and [IO.File]::ReadAllText($urlPath).Contains($expectedUrl)
          $argvSafe = $Arguments.Count -gt 0 -and $Arguments[0] -ceq '--disable'
          foreach ($arg in $Arguments) {
            if ($arg -cin @('-L', '--location', '--location-trusted') -or $arg -clike '--retry*') { $argvSafe = $false }
            foreach ($canary in @($script:AlphaWriteTestSecret, [uri]::EscapeDataString($script:AlphaWriteTestSecret),
                $script:AlphaWriteTestBase, $script:AlphaWriteTestRedirect, 'WRITE_BASE_CANARY', 'WRITE_ONE_SHOT_CANARY')) {
              if ($arg.Contains($canary)) { $argvSafe = $false }
            }
          }
          $script:AlphaWriteCalls += [pscustomobject]@{
            method = $method; private = $private; body_exact = $bodyExact; url_exact = $urlExact
            argv_safe = $argvSafe; body_present = [bool]$bodyArgument
            header_path = $headerPath; body_path = $bodyPath; diagnostics_path = $DiagnosticsPath
          }
          $redirectScenario = $script:AlphaWriteScenario -cin @('redirect-success', 'hop2-throw', 'second-redirect', 'privacy-drift')
          $headers = if ($method -ceq 'POST' -and $redirectScenario) {
            "HTTP/1.1 302 Found`r`nLocation: $($script:AlphaWriteTestRedirect)`r`n`r`n"
          } elseif ($method -ceq 'GET' -and $script:AlphaWriteScenario -ceq 'second-redirect') {
            "HTTP/1.1 302 Found`r`nLocation: https://write-third.example.invalid/WRITE_THIRD_CANARY`r`n`r`n"
          } else { "HTTP/1.1 200 OK`r`nContent-Type: application/json`r`n`r`n" }
          [IO.File]::WriteAllText($headerPath, $headers, (New-Object Text.UTF8Encoding($false)))
          if ($method -ceq 'POST' -and $script:AlphaWriteScenario -ceq 'privacy-drift') {
            # Mutation occurs after the pre-dispatch privacy assertion, while
            # valid JSON and a usable Location would otherwise permit GET.
            if ($script:IsWindowsPlatform) {
              # A fresh protected descriptor avoids privileged audit/SACL
              # changes. Only this synthetic header gains Everyone-read.
              $changedAcl = New-Object Security.AccessControl.FileSecurity
              $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
              $everyoneSid = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
              $changedAcl.SetOwner($currentSid)
              $changedAcl.SetAccessRuleProtection($true, $false)
              $ownerRule = New-Object Security.AccessControl.FileSystemAccessRule(
                $currentSid, [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow)
              $everyoneRule = New-Object Security.AccessControl.FileSystemAccessRule(
                $everyoneSid, [Security.AccessControl.FileSystemRights]::Read,
                [Security.AccessControl.AccessControlType]::Allow)
              $changedAcl.SetAccessRule($ownerRule)
              $changedAcl.AddAccessRule($everyoneRule)
              # Set-Acl can request SeSecurityPrivilege when replacing this
              # existing descriptor; apply only the owned fixture DACL directly.
              if ($PSVersionTable.PSEdition -ceq 'Desktop') {
                [IO.File]::SetAccessControl($headerPath, $changedAcl)
              } else {
                [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($headerPath), $changedAcl)
              }
              $readbackAcl = Get-Acl -LiteralPath $headerPath -ErrorAction Stop
              $readbackRules = @($readbackAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
              $everyoneRules = @($readbackRules | Where-Object { $_.IdentityReference.Value -ceq 'S-1-1-0' })
              $script:AlphaWritePrivacyDriftApplied = $readbackAcl.AreAccessRulesProtected -and
                $readbackAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ceq $currentSid.Value -and
                $readbackRules.Count -eq 2 -and $everyoneRules.Count -eq 1 -and
                -not $everyoneRules[0].IsInherited -and
                $everyoneRules[0].AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                [int64]$everyoneRules[0].FileSystemRights -eq [int64]$everyoneRule.FileSystemRights
            } else {
              $unixMode = [Type]::GetType('System.IO.UnixFileMode')
              [IO.File]::SetUnixFileMode($headerPath, [Enum]::ToObject($unixMode, 420)) # 0644, owned fixture only
              $script:AlphaWritePrivacyDriftApplied = [int][IO.File]::GetUnixFileMode($headerPath) -eq 420
            }
            $privacyRejected = $false
            try { Assert-AlphaCredentialConfigPrivate -Path $headerPath }
            catch { $privacyRejected = $_.Exception.Message -ceq 'ALPHA_SECRET_CONFIG_PRIVACY_FAILED' }
            $script:AlphaWritePrivacyDriftApplied = $script:AlphaWritePrivacyDriftApplied -and $privacyRejected
          }
          if ($script:AlphaWriteScenario -ceq 'transport-exit') {
            [IO.File]::WriteAllText($DiagnosticsPath, ('WRITE_STDERR_CANARY ' + $script:AlphaWriteTestSecret), (New-Object Text.UTF8Encoding($false)))
            return [pscustomobject]@{ ExitCode = 7; Output = @($script:AlphaWriteSuccess) }
          }
          if ($script:AlphaWriteScenario -ceq 'transport-throw' -or
              ($method -ceq 'GET' -and $script:AlphaWriteScenario -ceq 'hop2-throw')) {
            throw [InvalidOperationException]::new(
              ('WRITE_THROW_OUTER_CANARY ' + $script:AlphaWriteTestSecret),
              [Exception]::new('WRITE_THROW_INNER_CANARY ' + $script:AlphaWriteTestRedirect))
          }
          if ($script:AlphaWriteScenario -cin @('non-json', 'brace-non-json')) {
            $prefix = if ($script:AlphaWriteScenario -ceq 'brace-non-json') { '{' } else { '' }
            return [pscustomobject]@{ ExitCode = 0; Output = @($prefix + 'WRITE_NONJSON_CANARY ' + $script:AlphaWriteTestSecret) }
          }
          if ($script:AlphaWriteScenario -cin @('lock-body', 'lock-header')) {
            $lockPath = if ($script:AlphaWriteScenario -ceq 'lock-body') { $bodyPath } else { $headerPath }
            $script:AlphaWriteLockPath = $lockPath
            $script:AlphaWriteLockStream = [IO.File]::Open($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
          }
          return [pscustomobject]@{ ExitCode = 0; Output = @($script:AlphaWriteSuccess) }
        } | ForEach-Object {
          if (-not (Test-AlphaWritePathsAbsent -Paths $script:AlphaWritePaths)) { $script:AlphaWriteOutputBeforeCleanup = $true }
          $output.Add($_)
        }
    } catch {
      $caught = $_
      $safeToken = if ($_.Exception.Message -clike 'ALPHA_WRITE_PREPARATION_FAILED*') { 'ALPHA_WRITE_PREPARATION_FAILED' }
        elseif ($_.Exception.Message -clike 'ALPHA_WRITE_OUTCOME_UNCONFIRMED*') { 'ALPHA_WRITE_OUTCOME_UNCONFIRMED' }
        elseif ($_.Exception.Message -clike 'ALPHA_WRITE_CLEANUP_FAILED*') { 'ALPHA_WRITE_CLEANUP_FAILED' }
        else { 'ALPHA_WRITE_SELFTEST_UNEXPECTED_FAILURE' }
      Write-Host "WRITE_FAILURE token=$safeToken"
    }
    return [pscustomobject]@{
      output = $output.ToArray(); error = $caught; paths = @($script:AlphaWritePaths)
      calls = @($script:AlphaWriteCalls); staged = -not $script:AlphaWriteOutputBeforeCleanup
      create_calls = $script:AlphaWriteCreateCalls
      privacy_drift_applied = $script:AlphaWritePrivacyDriftApplied
    }
  }
  function Test-AlphaWriteFailure {
    param($Run, [string]$Token, [bool]$PostStarted)
    $expectedMessage = switch ($Token) {
      'ALPHA_WRITE_PREPARATION_FAILED' { 'ALPHA_WRITE_PREPARATION_FAILED' }
      'ALPHA_WRITE_OUTCOME_UNCONFIRMED' { 'ALPHA_WRITE_OUTCOME_UNCONFIRMED: READ BACK before retrying.' }
      'ALPHA_WRITE_CLEANUP_FAILED' { 'ALPHA_WRITE_CLEANUP_FAILED: READ BACK before retrying; any dispatched write may have landed.' }
      default { 'ALPHA_WRITE_SELFTEST_UNKNOWN_EXPECTATION' }
    }
    return $null -ne $Run.error -and $Run.error.Exception.GetType() -eq [InvalidOperationException] -and
      $Run.error.Exception.Message -ceq $expectedMessage -and
      $null -eq $Run.error.Exception.InnerException -and
      $Run.error.Exception.Data['post_started'] -is [bool] -and
      [bool]$Run.error.Exception.Data['post_started'] -eq $PostStarted -and $Run.output.Count -eq 0
  }
  try {
    $normal = Invoke-AlphaWriteSelfCase -Name 'normal'
    Check-AlphaWrite 'normal raw JSON text is preserved exactly' ($null -eq $normal.error -and $normal.output.Count -eq 1 -and $normal.output[0] -ceq $script:AlphaWriteSuccess)
    Check-AlphaWrite 'normal write dispatches exactly one POST' ($normal.calls.Count -eq 1 -and $normal.calls[0].method -ceq 'POST')
    Check-AlphaWrite 'body headers URL config and diagnostics are private before POST' ($normal.calls.Count -eq 1 -and $normal.calls[0].private -and $normal.calls[0].body_exact -and $normal.calls[0].url_exact)
    Check-AlphaWrite 'POST disables curl defaults and exposes no URL or credential argv and no retry' ($normal.calls.Count -eq 1 -and $normal.calls[0].argv_safe)
    Check-AlphaWrite 'success is emitted only after every owned path is absent' ($normal.paths.Count -ge 4 -and $normal.staged -and (Test-AlphaWritePathsAbsent -Paths $normal.paths))

    $redirect = Invoke-AlphaWriteSelfCase -Name 'redirect-success'
    Check-AlphaWrite 'one-hop response preserves raw JSON text' ($null -eq $redirect.error -and $redirect.output.Count -eq 1 -and $redirect.output[0] -ceq $script:AlphaWriteSuccess)
    Check-AlphaWrite 'redirect dispatches POST then one bodyless GET' ($redirect.calls.Count -eq 2 -and $redirect.calls[0].method -ceq 'POST' -and $redirect.calls[1].method -ceq 'GET' -and -not $redirect.calls[1].body_present)
    Check-AlphaWrite 'both hops keep private files and URL configs with no retry or argv leakage' ($redirect.calls.Count -eq 2 -and @($redirect.calls | Where-Object { -not $_.private -or -not $_.url_exact -or -not $_.argv_safe }).Count -eq 0)
    Check-AlphaWrite 'redirect success waits for cleanup of every owned file' ($redirect.paths.Count -ge 5 -and $redirect.staged -and (Test-AlphaWritePathsAbsent -Paths $redirect.paths))

    foreach ($scenario in @('non-json', 'brace-non-json', 'transport-exit', 'transport-throw', 'hop2-throw', 'second-redirect', 'privacy-drift')) {
      $run = Invoke-AlphaWriteSelfCase -Name $scenario
      $expectedCalls = if ($scenario -cin @('hop2-throw', 'second-redirect')) { 2 } else { 1 }
      Check-AlphaWrite "$scenario exposes only a fixed inner-free unconfirmed outcome and no response" (Test-AlphaWriteFailure -Run $run -Token 'ALPHA_WRITE_OUTCOME_UNCONFIRMED' -PostStarted $true)
      Check-AlphaWrite "$scenario never retries or dispatches a third hop" ($run.calls.Count -eq $expectedCalls -and @($run.calls | Where-Object { -not $_.argv_safe }).Count -eq 0 -and
        ($scenario -cne 'privacy-drift' -or ($run.privacy_drift_applied -and $run.calls[0].method -ceq 'POST')))
      Check-AlphaWrite "$scenario removes all owned objects before failure returns" ($run.paths.Count -ge 4 -and (Test-AlphaWritePathsAbsent -Paths $run.paths))
    }
    $partial = Invoke-AlphaWriteSelfCase -Name 'partial-creation' -CreateFailAt 2
    Check-AlphaWrite 'partial creation exposes only a preparation failure with post_started false' (Test-AlphaWriteFailure -Run $partial -Token 'ALPHA_WRITE_PREPARATION_FAILED' -PostStarted $false)
    Check-AlphaWrite 'partial creation stops before transport after owning one private file' ($partial.calls.Count -eq 0 -and $partial.create_calls -eq 2 -and $partial.paths.Count -eq 1)
    Check-AlphaWrite 'partial creation removes the already-created file and directory' (Test-AlphaWritePathsAbsent -Paths $partial.paths)

    foreach ($kind in @('body', 'header')) {
      $marker = 'WRITE_LOCK_' + $kind.ToUpperInvariant()
      if ($script:IsWindowsPlatform) {
        try {
          $locked = Invoke-AlphaWriteSelfCase -Name ('lock-' + $kind)
          $others = @($locked.paths | Where-Object { $_ -cne $script:AlphaWriteLockPath })
          $ok = (Test-AlphaWriteFailure -Run $locked -Token 'ALPHA_WRITE_CLEANUP_FAILED' -PostStarted $true) -and
            $locked.calls.Count -eq 1 -and $null -ne $script:AlphaWriteLockStream -and
            (Test-Path -LiteralPath $script:AlphaWriteLockPath) -and $others.Count -ge 3 -and
            (Test-AlphaWritePathsAbsent -Paths $others)
          Check-AlphaWrite "$kind lock overrides success and cleanup still reaches every other file" $ok
          if ($ok) { Write-Host "$marker PASS platform=windows" }
        } finally {
          if ($null -ne $script:AlphaWriteLockStream) { $script:AlphaWriteLockStream.Dispose(); $script:AlphaWriteLockStream = $null }
          foreach ($path in $locked.paths) {
            try { Remove-AlphaCredentialConfig -Path $path -Attempts 1 -RetryDelayMilliseconds 0 } catch { }
          }
        }
      } else {
        $script:AlphaWriteSkip++
        Write-Host "$marker SKIP platform=linux reason=open-file-unlink-semantics"
      }
    }
    Check-AlphaWrite 'all write fixtures leave no owned file or directory after release' (Test-AlphaWritePathsAbsent -Paths $script:AlphaWriteAllPaths)
  } finally {
    if ($null -ne $script:AlphaWriteLockStream) { $script:AlphaWriteLockStream.Dispose(); $script:AlphaWriteLockStream = $null }
    Set-Item -Path Function:\New-AlphaPrivateFile -Value $script:AlphaWriteOriginalPrivateFile
  }
  $writeCases = $script:AlphaWritePass + $script:AlphaWriteFail + $script:AlphaWriteSkip
  Write-Host "WRITE_CASES total=$writeCases"
  Write-Host "WRITE_RESULT passed=$($script:AlphaWritePass) failed=$($script:AlphaWriteFail) skipped=$($script:AlphaWriteSkip)"
  if ($script:AlphaWriteFail -gt 0 -or $script:AlphaWritePass -eq 0) { exit 1 }
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

Invoke-AlphaWriteRequest -BaseUrl $cfg.ALPHA_URL -Json $json -CurlPath $curlPath
