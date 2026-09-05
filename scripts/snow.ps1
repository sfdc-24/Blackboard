#Requires -Version 5.1
<#
SFDC24 ServiceNow rail client for Windows PowerShell 5.1
claude-code-cli, 2026-09-05

STATUS: UNTESTED -- there is no instance yet.
  Not one line of this has run against a live ServiceNow instance, because none
  exists as of 2026-09-05. It is written now so that the unblock, once Mr. Salam
  provisions a PDI, is a three-line .env edit rather than an hour of authoring.
  The same convention as scripts/public_inbox_quarantine.gs, which sat inert
  until it could be deployed AND verified. Delete this banner only after an
  -Action whoami returns a real record -- not after it "looks right".

WHY THIS EXISTS, AND WHY IT LOOKS LIKE bus.ps1
  Same constraint as the Blackboard bus: this laptop's Claude Code permission
  classifier blocks any shell command carrying a secret inline, and DOCTRINE
  D-18 puts credentials in the machine's local env file regardless. So the
  instance URL and credentials are read from ..\.env at run time and NEVER
  appear on a command line, in a board row, or in a chat message.

READ-ONLY BY DEFAULT -- this is deliberate and is not the industry default.
  The community ServiceNow MCP servers ship with destructive writes enabled.
  Our whole product claim is an auditable, read-back-verified trail, so a stray
  create/update from a mis-parsed instruction is precisely the failure we cannot
  afford. create and update refuse to run without an explicit -Write.
  There is no delete action at all. If one is ever needed it should be a
  separate, separately-reviewed script.

D-4 APPLIES HERE TOO: read-back is the only proof of a write. create and update
  therefore re-GET the record by sys_id and print what the instance actually
  stored, not what we sent. A 200 is not proof; the returned record is.

  Corollary learned on ISS-015 and restated in docs/SERVICENOW_SETUP.md: a 200
  carrying {"result":[]} is an ACL silently filtering, which reads as success.
  Check the record COUNT, not the status code. -Action whoami does exactly that.

USAGE (any working directory; paths resolve from this script's location)
  & scripts\snow.ps1 -Action whoami
  & scripts\snow.ps1 -Action query  -Table incident -Query 'active=true' -Limit 5
  & scripts\snow.ps1 -Action get    -Table incident -SysId <32-char sys_id>
  & scripts\snow.ps1 -Action create -Table incident -DataJson '{"short_description":"test"}' -Write
  & scripts\snow.ps1 -Action update -Table incident -SysId <sys_id> -DataJson '{"state":"2"}' -Write

  Call it IN-PROCESS with the call operator (&), never by launching a child
  `powershell -File`. The child shell re-parses argv and strips JSON quoting, so
  -DataJson arrives mangled. That cost a session once already; it is written up
  in the blackboard-local-gotchas memory.

.ENV KEYS (see .env.example)
  SNOW_INSTANCE=https://devNNNNN.service-now.com
  SNOW_USER=<integration user, NOT the admin account>
  SNOW_PASS=<its password>
  SNOW_CLIENT_ID / SNOW_CLIENT_SECRET   optional; presence switches on OAuth
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('whoami', 'query', 'get', 'create', 'update')]
  [string]$Action,

  [string]$Table,
  [string]$Query,
  [string]$Fields,
  [string]$SysId,
  [string]$DataJson,
  [int]$Limit = 10,

  # Gate on every state-changing call. Absent = refuse. See header.
  [switch]$Write,

  [string]$OutFile,
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- load credentials from .env (never from argv) ---------------------------
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "env file not found: $EnvFile  (needs SNOW_INSTANCE / SNOW_USER / SNOW_PASS; see .env.example)"
}
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
    $cfg[$matches[1]] = $matches[2].Trim('"').Trim("'")
  }
}
foreach ($k in 'SNOW_INSTANCE', 'SNOW_USER', 'SNOW_PASS') {
  if (-not $cfg[$k]) {
    throw "$k missing in $EnvFile. No instance is provisioned yet -- see docs/SERVICENOW_SETUP.md, section 'Provisioning'."
  }
}
$base = $cfg.SNOW_INSTANCE.TrimEnd('/')
if ($base -notmatch '^https://') { throw "SNOW_INSTANCE must be https:// -- refusing to send credentials over $base" }

# ---- the write gate, checked BEFORE any network call ------------------------
# Deliberately ahead of Get-AuthHeader: on the OAuth path that function spends a
# real token request, and a call we are going to refuse anyway should not first
# go and authenticate. Refuse on the argv alone.
if ($Action -in @('create', 'update') -and -not $Write) {
  throw "REFUSING TO WRITE. -Action $Action is state-changing; pass -Write to confirm you meant it."
}

# ---- auth -------------------------------------------------------------------
# Basic is the first-proof path only (docs/SERVICENOW_SETUP.md). It needs the
# integration user to hold snc_basic_auth_api_access as well as
# snc_platform_rest_api_access, or every call is 401 no matter how correct the
# ACLs are. If OAuth creds are present in .env we use those instead.
function Get-AuthHeader {
  if ($cfg.SNOW_CLIENT_ID -and $cfg.SNOW_CLIENT_SECRET) {
    $body = @{
      grant_type    = 'password'
      client_id     = $cfg.SNOW_CLIENT_ID
      client_secret = $cfg.SNOW_CLIENT_SECRET
      username      = $cfg.SNOW_USER
      password      = $cfg.SNOW_PASS
    }
    $tok = Invoke-RestMethod -Uri "$base/oauth_token.do" -Method Post -Body $body -TimeoutSec 60
    if (-not $tok.access_token) { throw "OAuth token request returned no access_token" }
    return "Bearer $($tok.access_token)"
  }
  $pair = "$($cfg.SNOW_USER):$($cfg.SNOW_PASS)"
  return 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
}

$headers = @{
  Authorization = (Get-AuthHeader)
  Accept        = 'application/json'
}

function Invoke-Snow {
  param([string]$Method, [string]$Uri, [string]$Body)
  try {
    if ($Body) {
      $bytes = [Text.Encoding]::UTF8.GetBytes($Body)
      return Invoke-RestMethod -Uri $Uri -Method $Method -Headers $headers `
             -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 120
    }
    return Invoke-RestMethod -Uri $Uri -Method $Method -Headers $headers -TimeoutSec 120
  } catch [System.Net.WebException] {
    # ServiceNow returns a JSON error body; surface it instead of the bare
    # "(401) Unauthorized" that tells you nothing about WHICH gate refused.
    $resp = $_.Exception.Response
    $detail = ''
    if ($resp) {
      $sr = New-Object IO.StreamReader($resp.GetResponseStream())
      $detail = $sr.ReadToEnd(); $sr.Close()
      $code = [int]$resp.StatusCode
      if ($code -eq 401) {
        $detail += "`n  HINT: 401 is usually the ROLE gate, not the password. The integration user needs" +
                   "`n  snc_platform_rest_api_access (and snc_basic_auth_api_access for basic auth)." +
                   "`n  See docs/SERVICENOW_SETUP.md, 'Wiring the rail'."
      }
      throw "HTTP $code from $Uri`n$detail"
    }
    throw
  }
}

function Encode([string]$s) { [Uri]::EscapeDataString($s) }

# ---- actions ----------------------------------------------------------------
switch ($Action) {

  # The proof-of-life call. Deliberately counts records rather than trusting the
  # status code -- an ACL that filters everything still answers 200.
  'whoami' {
    $u = "$base/api/now/table/sys_user?sysparm_query=user_name=$(Encode $cfg.SNOW_USER)" +
         "&sysparm_fields=user_name,name,sys_id,active&sysparm_limit=1"
    $r = Invoke-Snow -Method Get -Uri $u
    $n = @($r.result).Count
    if ($n -eq 0) {
      Write-Warning ("AUTH OK BUT ZERO RECORDS. This is the failure that looks like success: the " +
                     "credentials were accepted and an ACL filtered the result. The integration user " +
                     "likely lacks read on sys_user. Do not build on this rail until it returns 1.")
    }
    [pscustomobject]@{
      instance    = $base
      auth        = $(if ($cfg.SNOW_CLIENT_ID) { 'oauth' } else { 'basic' })
      records     = $n
      user        = $(if ($n) { @($r.result)[0].user_name } else { $null })
      name        = $(if ($n) { @($r.result)[0].name } else { $null })
      verdict     = $(if ($n -eq 1) { 'RAIL UP' } else { 'NOT PROVEN' })
    } | Format-List
    return
  }

  'query' {
    if (-not $Table) { throw "-Table is required for query" }
    $u = "$base/api/now/table/$(Encode $Table)?sysparm_limit=$Limit"
    if ($Query)  { $u += "&sysparm_query=$(Encode $Query)" }
    if ($Fields) { $u += "&sysparm_fields=$(Encode $Fields)" }
    $r = Invoke-Snow -Method Get -Uri $u
    $out = $r.result | ConvertTo-Json -Depth 8
    Write-Output ("{0} record(s)" -f @($r.result).Count)
  }

  'get' {
    if (-not $Table -or -not $SysId) { throw "-Table and -SysId are required for get" }
    $u = "$base/api/now/table/$(Encode $Table)/$(Encode $SysId)"
    if ($Fields) { $u += "?sysparm_fields=$(Encode $Fields)" }
    $r = Invoke-Snow -Method Get -Uri $u
    $out = $r.result | ConvertTo-Json -Depth 8
  }

  'create' {
    if (-not $Write)   { throw "REFUSING TO WRITE. -Action create is state-changing; pass -Write to confirm you meant it." }
    if (-not $Table)   { throw "-Table is required for create" }
    if (-not $DataJson){ throw "-DataJson is required for create" }
    # Fail loudly on a malformed payload rather than posting a half-record --
    # ISSUE 010's lesson, which is why bus.ps1 asserts row shape before sending.
    try { $null = $DataJson | ConvertFrom-Json } catch { throw "-DataJson is not valid JSON: $_" }

    $r = Invoke-Snow -Method Post -Uri "$base/api/now/table/$(Encode $Table)" -Body $DataJson
    $newId = $r.result.sys_id
    if (-not $newId) { throw "create returned no sys_id -- treat the write as UNPROVEN and query the table before retrying (D-4)." }

    # D-4: re-read from the instance. Never report what we sent.
    $back = Invoke-Snow -Method Get -Uri "$base/api/now/table/$(Encode $Table)/$(Encode $newId)"
    Write-Output "CREATED $Table $newId -- read back from the instance:"
    $out = $back.result | ConvertTo-Json -Depth 8
  }

  'update' {
    if (-not $Write)    { throw "REFUSING TO WRITE. -Action update is state-changing; pass -Write to confirm you meant it." }
    if (-not $Table -or -not $SysId) { throw "-Table and -SysId are required for update" }
    if (-not $DataJson) { throw "-DataJson is required for update" }
    try { $null = $DataJson | ConvertFrom-Json } catch { throw "-DataJson is not valid JSON: $_" }

    $null = Invoke-Snow -Method Patch -Uri "$base/api/now/table/$(Encode $Table)/$(Encode $SysId)" -Body $DataJson
    $back = Invoke-Snow -Method Get -Uri "$base/api/now/table/$(Encode $Table)/$(Encode $SysId)"
    Write-Output "UPDATED $Table $SysId -- read back from the instance:"
    $out = $back.result | ConvertTo-Json -Depth 8
  }
}

if ($OutFile) {
  $path = if ([IO.Path]::IsPathRooted($OutFile)) { $OutFile } else { Join-Path (Get-Location).Path $OutFile }
  [IO.File]::WriteAllText($path, [string]$out, (New-Object Text.UTF8Encoding($false)))
  Write-Output ("saved {0} chars to {1}" -f ([string]$out).Length, $path)
} else {
  Write-Output $out
}
