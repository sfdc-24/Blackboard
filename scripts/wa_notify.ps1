#Requires -Version 5.1
<#
SFDC24 — WhatsApp notifier: how an instance reaches Mr. Salam when it is stuck
claude-code-cli, 2026-09-06 · closes ISS-003

WHY THIS EXISTS
  Work stalls and Mr. Salam has to come back and say "resume". vm-cli named the
  mechanism in VIEWPORT vseq=011: ORDER 044 opened an unattended window at
  03:48Z and nothing ran, because no instance polls the board — every one of us
  needs a human or a scheduler to wake it. 12h51m of total fleet silence.

  He carries his phone. So the escalation that actually works is a WhatsApp
  message, and until now no instance could send one: the only Graph token lived
  inside Pipedream. He put a system-user token and the phone number id into
  .env on 2026-09-06 and this is the sender built on them.

WHAT THIS IS NOT
  It is NOT the reply path for visitor or gateway traffic. L-58 still stands:
  agent replies to inbound WhatsApp are board rows, and the Pipedream send step
  owns those. This is a one-way operator alert — an instance telling its
  Governor it is blocked — and nothing else should route through it.

THE 24-HOUR WINDOW, which decides whether a send is even legal
  Meta allows free-form text only inside 24 hours of the recipient's last
  inbound message. Outside it, only an APPROVED TEMPLATE delivers, and no
  template is approved for this number yet (LH3, still unowned). So:
    - inside the window  -> this script works
    - outside it         -> Meta returns error 131047 (re-engagement) and this
                            script SAYS SO rather than reporting a false send
  A message that silently fails to arrive is worse than one that never went, so
  the exit code is non-zero on any Graph error and the body is printed verbatim.

  His number is Toronto (+1 647), so the US marketing-template block that
  produced error 131049 does not apply here — that rule is "+1 with a US area
  code" and Canada is not covered. Utility/service traffic to this number is
  fine once a template exists.

CREDENTIALS (DOCTRINE D-18 — never on a command line, never in Drive)
  .env supplies META_TOKEN, WA_PHONE_NUMBER_ID and WA_TO. Nothing is accepted
  from argv.

  GOTCHA, cost 20 minutes on 2026-09-06: the stored META_TOKEN begins with the
  literal text "Bearer ". Sending it as `Bearer $token` produces
  `Bearer Bearer EAA...` and Graph answers 401 with code 190 "Authentication
  Error" — which reads exactly like a dead token and sent me looking for the
  wrong thing. The prefix is stripped below. Never diagnose a 190 as expiry
  until you have looked at the first six characters of the value.

USAGE
  & .\scripts\wa_notify.ps1 -Text "blocked on X, need Y"
  & .\scripts\wa_notify.ps1 -TextFile msg.txt -Tag claude-code-cli
  & .\scripts\wa_notify.ps1 -Text "..." -DryRun     # prints the request, sends nothing
#>
param(
  [string]$Text,
  [string]$TextFile,
  [string]$To,
  [string]$Tag = 'claude-code-cli',
  [ValidateSet('BLOCKED', 'ANDON', 'STATUS', 'DONE')]
  [string]$Kind = 'BLOCKED',
  [switch]$Raw,          # send $Text exactly as given, with no prefix line
  [switch]$DryRun,
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$GRAPH_VERSION = 'v22.0'   # matches the deployed Pipedream reply step. DOCTRINE
                           # V2 says v22.0 and the V3.1 bus doc says v24.0; the
                           # deployed thing wins until that conflict is ruled on.
$MAX_CHARS = 3800          # Meta's text body limit is 4096; leave room for the
                           # prefix line rather than discovering the edge live.

# ---- credentials ------------------------------------------------------------
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "env file not found: $EnvFile" }
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
    $cfg[$matches[1]] = $matches[2].Trim('"').Trim("'")
  }
}
foreach ($k in @('META_TOKEN', 'WA_PHONE_NUMBER_ID')) {
  if (-not $cfg[$k]) { throw "$k missing in $EnvFile" }
}
$token = $cfg.META_TOKEN -replace '^\s*[Bb]earer\s+', ''   # see GOTCHA above
if (-not $To) { $To = $cfg.WA_TO }
if (-not $To) { throw "no recipient: pass -To or set WA_TO in $EnvFile" }
$To = ($To -replace '[^\d]', '')

# ---- the message ------------------------------------------------------------
if ($TextFile) {
  if (-not (Test-Path -LiteralPath $TextFile)) { throw "TextFile not found: $TextFile" }
  $Text = (Get-Content -LiteralPath $TextFile -Raw)
}
if (-not $Text) { throw "nothing to send: pass -Text or -TextFile" }
$Text = $Text.TrimEnd("`r", "`n")

# The prefix exists so he can tell this apart from the gateway's stateless lane,
# which answers his questions with "I don't carry state between messages". If he
# cannot tell which Claude is talking, the channel is worth very little.
$body = if ($Raw) { $Text } else { "[$Kind · $Tag]`n$Text" }
if ($body.Length -gt $MAX_CHARS) { $body = $body.Substring(0, $MAX_CHARS - 3) + '...' }

$payload = @{
  messaging_product = 'whatsapp'
  recipient_type    = 'individual'
  to                = $To
  type              = 'text'
  text              = @{ preview_url = $false; body = $body }
} | ConvertTo-Json -Depth 6 -Compress

$uri = "https://graph.facebook.com/$GRAPH_VERSION/$($cfg.WA_PHONE_NUMBER_ID)/messages"

if ($DryRun) {
  Write-Output "DRY RUN - nothing sent"
  Write-Output "POST $uri"
  Write-Output "to: $To   body chars: $($body.Length)"
  Write-Output $body
  return
}

# ---- send -------------------------------------------------------------------
# D-4: the response is the only evidence. A 200 carrying a message id is the
# proof that Meta ACCEPTED it — not that he read it, and not that it rendered.
try {
  $res = Invoke-WebRequest -Uri $uri -Method Post `
           -Headers @{ Authorization = "Bearer $token" } `
           -ContentType 'application/json' `
           -Body ([Text.Encoding]::UTF8.GetBytes($payload)) `
           -UseBasicParsing -TimeoutSec 60
  Write-Output "HTTP $($res.StatusCode)"
  Write-Output $res.Content
} catch {
  $detail = ''
  if ($_.Exception.Response) {
    $reader = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
    $detail = $reader.ReadToEnd()
  }
  Write-Output "SEND FAILED: $($_.Exception.Message)"
  if ($detail) { Write-Output $detail }
  if ($detail -match '131047') {
    Write-Output ''
    Write-Output 'DIAGNOSIS: the 24-hour window is closed. Free-form text is not'
    Write-Output 'deliverable until he messages the business number again, or until'
    Write-Output 'an approved utility template exists (LH3). This is policy, not a bug.'
  }
  exit 1
}
