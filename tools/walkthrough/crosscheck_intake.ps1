#Requires -Version 5.1
<#
Two vendors read the same client message, separately. Then the disagreement.

WHAT THIS IS FOR
  Beat 2 of a prospect meeting. The work panel has been promising that "two
  agents take the same question and answer it separately, and where they
  disagree it stops and waits for a human". This is the thing that makes that
  sentence true, and it has to complete while someone is watching - so the two
  calls go out together, not one after the other.

WHY TWO VENDORS AND NOT ONE MODEL TWICE
  One model asked twice is not a cross-check, it is the same prior sampled
  twice, and it agrees with itself on wrong answers. OpenAI and Groq are
  different companies, different weights, different training. When THOSE two
  land on the same number it means something; when they split, that is the
  finding, and the whole point is to leave the split on screen rather than
  quietly picking one.

WHY EXTRACTION AND NOT A TAX QUESTION
  The audience is a tax professional. Two language models arguing about tax law
  in front of him is a coin flip on being corrected in his own field, and the
  tool is not supposed to replace his expertise anyway. Extraction from a messy
  client message is his actual daily pain - his clients reach him on WhatsApp
  and he re-keys numbers off their photographs - and a disagreement about a
  figure or a tax year is both meaningful and safe.

D-18. Keys are read from .env at run time. Neither reaches a command line, a log
line, or the panel file that gets shipped to the presenter box.

EXIT STATUS
  0 = two valid extractions agree (not independently verified truth)
  1 = missing/invalid provider result or execution failure
  2 = PENDING_HUMAN; split values remain visible, no downstream approval

USAGE
  .\crosscheck_intake.ps1                              # the built-in sample message
  .\crosscheck_intake.ps1 -MessageFile msg.txt
  .\crosscheck_intake.ps1 -MessageFile msg.txt -PanelFile out.txt
#>
param(
  [string]$MessageFile,
  [string]$PanelFile,
  [string]$EnvFile = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.env'),
  [string]$OpenAIModel = 'gpt-4o-mini',
  # QWEN, DELIBERATELY. The first draft named llama-3.3-70b-versatile from
  # memory and got a 404 - Groq had retired it. Asking the models endpoint what
  # it actually serves left four plausible options, and the choice among them is
  # not arbitrary: openai/gpt-oss-120b is OpenAI's own open-weights model, so
  # checking gpt-4o-mini against it would be the same lineage twice wearing two
  # names. Qwen is Alibaba - a different company, different weights, different
  # training data. That is what makes an agreement worth anything.
  #
  # 3.6 rather than 3.8: 3.8 answers 429, and the body is worth reading rather
  # than retrying - "output tokens per minute (OTPM): Limit 1000, Requested
  # 1557". That is not congestion, it is a free-tier cap measured on EXPECTED
  # output, so it would have failed identically every time. Hence MaxTokens
  # below, which keeps two runs inside the cap; a 429 mid-meeting because I ran
  # the demo twice would be a self-inflicted wound.
  [string]$GroqModel   = 'qwen/qwen3.6-27b',
  # The extraction is a small JSON object, 150-250 tokens. 400 is headroom, and
  # two runs still sit under the 1000/minute ceiling.
  [int]$MaxTokens = 400,
  [int]$TimeoutSec = 45
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'crosscheck_contract.ps1')
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-CrosscheckPanel { param([string]$Content)
  if (-not $PanelFile) { return }
  [IO.File]::WriteAllText($PanelFile, $Content, (New-Object Text.UTF8Encoding $false))
  if ([IO.File]::ReadAllText($PanelFile) -cne $Content) { throw 'panel file readback mismatch' }
}
$jobs = @()
try {
Write-CrosscheckPanel 'CROSS-CHECK PENDING - waiting for two valid provider answers.'

# ---- the message -------------------------------------------------------------
# Deliberately ambiguous in three places, because a cross-check that only ever
# runs on clean input proves nothing. "last year 2024" does not say whether the
# tax year is 2024 or 2025; the RRSP paper is asked about rather than sent; and
# the wife's T4 is promised rather than attached.
$SAMPLE = @'
hi its me again sorry - for last year 2024 i think? i sent u the t4 already
but my wifes one is coming. also i paid 4800 for daycare and around 1250 in
medical. do i need the rrsp paper? my accountant last yr said no. need this
done b4 friday pls
'@

$message = if ($MessageFile) {
  if (-not (Test-Path -LiteralPath $MessageFile)) { throw "message file not found: $MessageFile" }
  (Get-Content -LiteralPath $MessageFile -Raw)
} else { $SAMPLE }

# ---- keys --------------------------------------------------------------------
$cfg = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    $cfg[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
  }
}
foreach ($k in @('OPENAI_API_KEY', 'GROQ_API_KEY')) {
  if (-not $cfg.ContainsKey($k) -or -not $cfg[$k]) { throw "$k missing in $EnvFile - this needs BOTH vendors or it is not a cross-check" }
}

$INSTRUCTION = @'
You are reading one message a client sent to their tax preparer. Extract only
what the message actually says. Do not infer, do not fill gaps, do not be
helpful. If the message is ambiguous about a field, choose the single most
literal reading.

Return ONLY a JSON object, no prose, no code fence, with exactly these keys:
  "tax_year"          string, the four-digit year the work is FOR, or "unclear"
  "return_type"       one of "T1","T2","HST","unclear"
  "documents_received" array of strings: documents the client says they ALREADY sent
  "documents_pending"  array of strings: documents mentioned but NOT yet sent
  "figures"           array of objects {"label":string,"amount":number}
  "deadline_claimed"  string, any deadline the client states, or "none"
  "urgent"            boolean
'@

# ---- both at once ------------------------------------------------------------
# Sequentially these are 4-10 seconds. Together they are the slower of the two,
# which is what keeps this inside the time a person will watch without talking.
$sw = [Diagnostics.Stopwatch]::StartNew()
$jobs += Start-Job -Name 'openai' -ArgumentList $cfg['OPENAI_API_KEY'], $OpenAIModel, $INSTRUCTION, $message, $TimeoutSec, $MaxTokens -ScriptBlock {
  param($key, $model, $sys, $usr, $t, $maxTok)
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $body = @{ model=$model; temperature=0; max_tokens=$maxTok; response_format=@{type='json_object'};
             messages=@(@{role='system';content=$sys},@{role='user';content=$usr}) } | ConvertTo-Json -Depth 8 -Compress
  $r = [Net.HttpWebRequest]::Create('https://api.openai.com/v1/chat/completions')
  $r.Method='POST'; $r.ContentType='application/json'; $r.Headers.Add('Authorization','Bearer '+$key); $r.Timeout=$t*1000
  $b=[Text.Encoding]::UTF8.GetBytes($body); $r.ContentLength=$b.Length
  # RETURN THE PROVIDER'S OWN WORDS ON FAILURE. Without this the parent sees
  # only "(400) Bad Request" and I have to go and write a separate probe script
  # to find out why - which I did, twice, for a 404 and a 429 whose bodies both
  # named the exact fix. The body is the diagnosis; throwing it away and keeping
  # the status code is throwing away the answer and keeping the complaint.
  try {
    $st=$r.GetRequestStream(); try{$st.Write($b,0,$b.Length)}finally{$st.Dispose()}
    $rs=$r.GetResponse(); $sr=New-Object IO.StreamReader($rs.GetResponseStream())
    try{$raw=$sr.ReadToEnd()}finally{$sr.Dispose();$rs.Dispose()}
    ($raw | ConvertFrom-Json).choices[0].message.content
  } catch [Net.WebException] {
    $er = $_.Exception.Response
    if ($er) {
      $code = [int]$er.StatusCode
      $esr = New-Object IO.StreamReader($er.GetResponseStream())
      try { $eb = $esr.ReadToEnd() } finally { $esr.Dispose() }
      $emsg = try { ($eb | ConvertFrom-Json).error.message } catch { $eb }
      "PROVIDER_ERROR HTTP$code $emsg"
    } else {
      "PROVIDER_ERROR no-response $($_.Exception.Message)"
    }
  }
}
$jobs += Start-Job -Name 'groq' -ArgumentList $cfg['GROQ_API_KEY'], $GroqModel, $INSTRUCTION, $message, $TimeoutSec, $MaxTokens -ScriptBlock {
  param($key, $model, $sys, $usr, $t, $maxTok)
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  # reasoning_effort='none' IS LOAD BEARING. Qwen is a reasoning model: left to
  # itself it spends its output budget thinking, the JSON is cut off mid-object,
  # and Groq answers 400 "Failed to validate JSON" - which reads like a bad
  # prompt and is actually a truncated one. With reasoning off the same prompt
  # returns valid JSON in ~520ms using 128 output tokens.
  $body = @{ model=$model; temperature=0; max_tokens=$maxTok; reasoning_effort='none'; response_format=@{type='json_object'};
             messages=@(@{role='system';content=$sys},@{role='user';content=$usr}) } | ConvertTo-Json -Depth 8 -Compress
  $r = [Net.HttpWebRequest]::Create('https://api.groq.com/openai/v1/chat/completions')
  $r.Method='POST'; $r.ContentType='application/json'; $r.Headers.Add('Authorization','Bearer '+$key); $r.Timeout=$t*1000
  $b=[Text.Encoding]::UTF8.GetBytes($body); $r.ContentLength=$b.Length
  # RETURN THE PROVIDER'S OWN WORDS ON FAILURE. Without this the parent sees
  # only "(400) Bad Request" and I have to go and write a separate probe script
  # to find out why - which I did, twice, for a 404 and a 429 whose bodies both
  # named the exact fix. The body is the diagnosis; throwing it away and keeping
  # the status code is throwing away the answer and keeping the complaint.
  try {
    $st=$r.GetRequestStream(); try{$st.Write($b,0,$b.Length)}finally{$st.Dispose()}
    $rs=$r.GetResponse(); $sr=New-Object IO.StreamReader($rs.GetResponseStream())
    try{$raw=$sr.ReadToEnd()}finally{$sr.Dispose();$rs.Dispose()}
    ($raw | ConvertFrom-Json).choices[0].message.content
  } catch [Net.WebException] {
    $er = $_.Exception.Response
    if ($er) {
      $code = [int]$er.StatusCode
      $esr = New-Object IO.StreamReader($er.GetResponseStream())
      try { $eb = $esr.ReadToEnd() } finally { $esr.Dispose() }
      $emsg = try { ($eb | ConvertFrom-Json).error.message } catch { $eb }
      "PROVIDER_ERROR HTTP$code $emsg"
    } else {
      "PROVIDER_ERROR no-response $($_.Exception.Message)"
    }
  }
}

$null = Wait-Job -Job $jobs -Timeout ($TimeoutSec + 5)
$sw.Stop()

# Why a job failed, WITHOUT the reporting itself throwing. The first version read
# $j.ChildJobs[0].JobStateInfo.Reason.Message directly; Reason is null when the
# child failed some other way, and under Set-StrictMode 2.0 reading .Message off
# null is a terminating error. So the diagnostic crashed while trying to explain
# the failure, and printed a PropertyNotFoundStrict trace instead of the cause.
# An error path that can itself error is an error path that has never been run.
function Get-JobReason {
  param($job)
  $bits = @()
  try {
    if ($job.ChildJobs -and $job.ChildJobs.Count -gt 0) {
      $info = $job.ChildJobs[0].JobStateInfo
      if ($info -and $info.Reason) { $bits += $info.Reason.Message }
      $errs = $job.ChildJobs[0].Error
      if ($errs -and $errs.Count -gt 0) {
        $bits += ($errs | ForEach-Object { $_.ToString() }) -join ' | '
      }
    }
  } catch { $bits += "could not read the job's error record" }
  if ($bits.Count -eq 0) { return 'no reason reported by the job' }
  return ($bits -join ' | ')
}

$answers = @{}
$failed = @()
foreach ($j in $jobs) {
  if ($j.State -ne 'Completed') { $failed += "$($j.Name) did not finish (state $($j.State)): $(Get-JobReason $j)"; continue }
  $out = Receive-Job -Job $j -ErrorAction SilentlyContinue
  if (-not $out) { $failed += "$($j.Name) returned nothing: $(Get-JobReason $j)"; continue }
  $txt = ($out | Out-String).Trim()
  if ($txt -like 'PROVIDER_ERROR*') { $failed += "$($j.Name): $txt"; continue }
  try { $answers[$j.Name] = ConvertFrom-CrosscheckAnswer $txt }
  catch { $failed += "$($j.Name) returned an invalid extraction; required keys and types were not satisfied" }
}
Remove-Job -Job $jobs -Force
$jobs = @()

# REFUSE rather than degrade. One model answering is not a cross-check, and
# showing it as though it were is exactly the lie this whole beat is against.
if ($failed.Count -gt 0 -or $answers.Count -ne 2) {
  # The presenter tails this path; leaving a previous agreement there would
  # display an old successful run during a current failed one.
  Write-CrosscheckPanel 'CROSS-CHECK FAILED - no current result; do not use an earlier comparison.'
  Write-Host "CROSS-CHECK FAILED - this is NOT a result, it is a missing one"
  foreach ($f in $failed) { Write-Host "  $f" }
  Write-Host "  models that answered: $($answers.Count) of 2"
  exit 1
}

# ---- compare -----------------------------------------------------------------
$rows = @(Compare-CrosscheckAnswers $answers['openai'] $answers['groq'])
$agree = @($rows | Where-Object { $_.same }).Count
$split = $rows.Count - $agree

# ---- report ------------------------------------------------------------------
$lines = @()
$lines += "   the same client message, read by two vendors, separately"
$lines += "   openai/$OpenAIModel   vs   groq/$GroqModel"
$lines += "   both answered in $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
$lines += ""
foreach ($r in $rows) {
  if ($r.same) {
    $lines += ("   AGREE  {0,-19} {1}" -f $r.field, $r.a)
  } else {
    $lines += ("   SPLIT  {0,-19} A: {1}" -f $r.field, $r.a)
    $lines += ("          {0,-19} B: {1}" -f '', $r.b)
  }
}
$lines += ""
if ($split -eq 0) {
  $lines += "   $agree of $($rows.Count) agreed. Both vendors agree; this is not proof the extraction is correct."
} else {
  $lines += "   $agree agreed, $split SPLIT. The split fields are not resolved here"
  $lines += "   and remain visible in this panel for a person to decide. No approval is recorded."
}

$text = ($lines -join "`n")
Write-Host $text

if ($PanelFile) {
  # No key and no raw message body - but READ THIS BEFORE POINTING IT AT A REAL
  # CLIENT. The comparison necessarily contains the EXTRACTED VALUES: names of
  # documents, amounts, dates. With the invented sample that is nothing. With a
  # real client message it is customer content, and this file is shipped to a
  # disposable box and put on a screen someone else is watching. For a live
  # deployment the panel needs redaction, or the comparison stays on the laptop
  # and only the agreed/split COUNTS go to the screen.
  Write-CrosscheckPanel $text
  Write-Host ""
  Write-Host "panel file written: $PanelFile ($((Get-Item $PanelFile).Length) bytes)"
}

Write-Host ""
if ($split -gt 0) {
  Write-Host "RESULT status=PENDING_HUMAN agreed=$agree split=$split fields=$($rows.Count)"
  exit 2
}
Write-Host "RESULT status=AGREED agreed=$agree split=0 fields=$($rows.Count)"
exit 0
} catch {
  # Do not expose a raw provider response or client message through exceptions.
  try { Write-CrosscheckPanel 'CROSS-CHECK FAILED - no current result; do not use an earlier comparison.' } catch {}
  Write-Host 'CROSS-CHECK FAILED - input, job startup, or panel output could not complete.'
  exit 1
} finally {
  if ($jobs.Count -gt 0) { Remove-Job -Job $jobs -Force -ErrorAction SilentlyContinue }
}
