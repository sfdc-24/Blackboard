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

USAGE
  .\crosscheck_intake.ps1                              # the built-in sample message
  .\crosscheck_intake.ps1 -MessageFile msg.txt
  .\crosscheck_intake.ps1 -MessageFile msg.txt -PanelFile out.txt
#>
param(
  [string]$MessageFile,
  [string]$PanelFile,
  [string]$EnvFile = 'C:\Users\salam\Quantum\Blackboard\.env',
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
  [int]$TimeoutSec = 45,
  # Exercise the comparison and the panel writer with fixed inputs and no
  # network, then exit. This exists because the hosted job that was green on
  # this file only ever checked that the .sh tools parse - it never loaded this
  # script at all, so "CI is green" said nothing whatsoever about it.
  [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- comparison and rendering, kept free of network and of .env --------------
# These four sit above the key-loading deliberately. Everything below this block
# needs two vendors and a credential; everything in it is a pure function of its
# arguments, which is the only reason -SelfTest can run on a hosted Linux runner
# that has neither.
$CROSSCHECK_FIELDS = @('tax_year','return_type','documents_received','documents_pending','figures','deadline_claimed','urgent')

function Get-Field { param($obj, [string]$name)
  if ($obj.PSObject.Properties.Name -contains $name) { return $obj.$name }
  return $null
}

function As-Text { param($v)
  if ($null -eq $v) { return '(absent)' }
  if ($v -is [bool]) { return $v.ToString().ToLower() }
  if ($v -is [Array]) {
    if ($v.Count -eq 0) { return '(none)' }
    if ($v[0] -is [psobject] -and $v[0].PSObject.Properties.Name -contains 'label') {
      return (($v | ForEach-Object { "$($_.label)=$($_.amount)" } | Sort-Object) -join ', ')
    }
    return (($v | ForEach-Object { "$_" } | Sort-Object) -join ', ')
  }
  return "$v"
}

function Compare-Extractions {
  param($A, $B, [string[]]$Fields = $CROSSCHECK_FIELDS)
  $rows = @()
  foreach ($f in $Fields) {
    $av = As-Text (Get-Field $A $f)
    $bv = As-Text (Get-Field $B $f)
    $rows += [pscustomobject]@{ field = $f; a = $av; b = $bv; same = ($av -eq $bv) }
  }
  return $rows
}

function Format-CrosscheckPanel {
  param($Rows, [string]$ModelA, [string]$ModelB, [double]$Seconds)
  $agree = @($Rows | Where-Object { $_.same }).Count
  $split = @($Rows | Where-Object { -not $_.same }).Count
  $lines = @()
  $lines += "   the same client message, read by two vendors, separately"
  $lines += "   openai/$ModelA   vs   groq/$ModelB"
  $lines += "   both answered in $([math]::Round($Seconds,1))s"
  $lines += ""
  foreach ($r in $Rows) {
    if ($r.same) {
      $lines += ("   AGREE  {0,-19} {1}" -f $r.field, $r.a)
    } else {
      $lines += ("   SPLIT  {0,-19} A: {1}" -f $r.field, $r.a)
      $lines += ("          {0,-19} B: {1}" -f '', $r.b)
    }
  }
  $lines += ""
  if ($split -eq 0) {
    $lines += "   $agree of $($Rows.Count) agreed. Nothing held back."
  } else {
    # SAY ONLY WHAT IS TRUE OF THE FILE THIS TEXT IS IN. The first version of
    # these two lines claimed the split fields "are not written to the file" -
    # printed INSIDE the file, four lines below the split values themselves. It
    # was self-refuting on its face, and the whole point of this beat is that
    # the panel's promises hold when a tax professional reads them closely.
    # What is actually true is the part that matters: nothing here picks a
    # winner. The self-test below asserts the file never makes that old claim.
    $lines += "   $agree agreed, $split SPLIT. Nothing here picks a winner on a"
    $lines += "   split - both readings stay on screen. A person decides those."
  }
  return ($lines -join "`n")
}

function Write-CrosscheckPanel {
  param([string]$Text, [string]$Path)
  # RESOLVE THE PATH FIRST. .NET resolves a relative path against the process
  # working directory, which is NOT PowerShell's current location - so a
  # relative -PanelFile would silently write somewhere else and a Get-Item on
  # the original string would then report a different file, or throw.
  # GetUnresolvedProviderPathFromPSPath handles a path whose file does not
  # exist yet, which Resolve-Path does not.
  $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
  # NO BOM. Set-Content -Encoding utf8 on PowerShell 5.1 writes UTF-8 WITH a
  # byte-order mark, and this file is copied to a Linux box and rendered on a
  # screen a guest is watching - where the BOM shows up as a stray glyph before
  # the first character of the first line. Measured on the pre-fix file: it
  # began EF BB BF. Out-File -Encoding utf8NoBOM does not exist until 6.0.
  [IO.File]::WriteAllText($resolved, $Text, (New-Object Text.UTF8Encoding $false))
  return $resolved
}

# ---- the self-test -----------------------------------------------------------
if ($SelfTest) {
  $pass = 0; $fail = 0
  function Check { param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { $script:pass++; Write-Host "  ok   $Name" }
    else     { $script:fail++; Write-Host "  FAIL $Name $Detail" }
  }

  # Two extractions that agree on one field and split on two, built here rather
  # than fetched, so the assertions below are about the rendering and nothing else.
  $A = [pscustomobject]@{ tax_year='2024'; return_type='unclear';
                          documents_received=@('t4'); documents_pending=@("wife's t4");
                          figures=@([pscustomobject]@{label='daycare';amount=4800});
                          deadline_claimed='b4 friday'; urgent=$true }
  $B = [pscustomobject]@{ tax_year='2024'; return_type='T1';
                          documents_received=@('t4'); documents_pending=@('rrsp paper','wifes t4');
                          figures=@([pscustomobject]@{label='daycare';amount=4800});
                          deadline_claimed='b4 friday'; urgent=$true }

  $rows = Compare-Extractions -A $A -B $B
  Check 'every field is compared' ($rows.Count -eq $CROSSCHECK_FIELDS.Count) "got $($rows.Count)"
  Check 'a real disagreement is reported as a split' (-not ($rows | Where-Object { $_.field -eq 'return_type' }).same)
  Check 'identical values are reported as agreement' (($rows | Where-Object { $_.field -eq 'tax_year' }).same)
  Check 'both sides of a split are carried' (
    ($rows | Where-Object { $_.field -eq 'return_type' }).a -eq 'unclear' -and
    ($rows | Where-Object { $_.field -eq 'return_type' }).b -eq 'T1')
  # Order must not decide agreement: B lists the pending documents the other way round.
  $pend = ($rows | Where-Object { $_.field -eq 'documents_pending' })
  Check 'list order does not fake a split' ($pend.b -eq 'rrsp paper, wifes t4') "got [$($pend.b)]"

  $text = Format-CrosscheckPanel -Rows $rows -ModelA 'm-a' -ModelB 'm-b' -Seconds 2.5

  # THE DEFECT THIS SUITE EXISTS FOR. The panel used to assert, inside itself,
  # that the split values were not written to the file that contained them.
  Check 'the panel makes no claim about withholding the splits' (
    $text -notmatch 'not written to the file')
  Check 'the panel says what it does instead of what it does not' (
    $text -match 'picks a winner')
  Check 'a split value actually appears in the text' ($text -match 'B: T1')
  Check 'the summary count matches the rendered splits' (
    $text -match '2 SPLIT' -and ([regex]::Matches($text, '(?m)^   SPLIT ')).Count -eq 2)

  # The writer, through a RELATIVE path, from a directory that is not the cwd
  # the process started in - the case that used to land the file elsewhere.
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("cc_selftest_" + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  Push-Location $tmp
  try {
    $written = Write-CrosscheckPanel -Text $text -Path 'panel.txt'
    Check 'a relative path lands in the current location' (
      (Split-Path -Parent $written) -eq (Get-Item -LiteralPath $tmp).FullName) "wrote [$written]"
    Check 'the writer returns a path that exists' (Test-Path -LiteralPath $written)
    $bytes = [IO.File]::ReadAllBytes($written)
    Check 'the panel file carries no BOM' (
      -not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    ) ("first bytes {0:X2} {1:X2} {2:X2}" -f $bytes[0], $bytes[1], $bytes[2])
    # What is ON DISK is what a guest reads - assert against the file, not $text.
    $onDisk = [IO.File]::ReadAllText($written)
    Check 'the file on disk makes no withholding claim' ($onDisk -notmatch 'not written to the file')
    Check 'the file on disk still shows both sides of a split' ($onDisk -match 'B: T1')
  } finally {
    Pop-Location
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }

  Write-Host ""
  Write-Host "RESULT passed=$pass failed=$fail"
  # ASSERT the exit code, never inherit it.
  if ($fail -gt 0) { exit 1 }
  if ($pass -eq 0) { Write-Host "no assertions ran, which is not a pass" ; exit 1 }
  exit 0
}

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

# ---- one HTTP helper, used for both, so neither gets an advantage ------------
function Invoke-Chat {
  param([string]$Url, [string]$Key, [string]$Model, [string]$System, [string]$User)

  $body = @{
    model       = $Model
    temperature = 0
    messages    = @(
      @{ role = 'system'; content = $System },
      @{ role = 'user';   content = $User }
    )
    response_format = @{ type = 'json_object' }
  } | ConvertTo-Json -Depth 8 -Compress

  $req = [Net.HttpWebRequest]::Create($Url)
  $req.Method = 'POST'
  $req.ContentType = 'application/json'
  $req.Headers.Add('Authorization', 'Bearer ' + $Key)
  $req.Timeout = $TimeoutSec * 1000
  $bytes = [Text.Encoding]::UTF8.GetBytes($body)
  $req.ContentLength = $bytes.Length
  $s = $req.GetRequestStream(); try { $s.Write($bytes, 0, $bytes.Length) } finally { $s.Dispose() }

  $resp = $req.GetResponse()
  $sr = New-Object IO.StreamReader($resp.GetResponseStream())
  try { $raw = $sr.ReadToEnd() } finally { $sr.Dispose(); $resp.Dispose() }
  $parsed = $raw | ConvertFrom-Json
  return ($parsed.choices[0].message.content | ConvertFrom-Json)
}

# ---- both at once ------------------------------------------------------------
# Sequentially these are 4-10 seconds. Together they are the slower of the two,
# which is what keeps this inside the time a person will watch without talking.
$jobs = @()
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

$sw = [Diagnostics.Stopwatch]::StartNew()
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
  try { $answers[$j.Name] = $txt | ConvertFrom-Json }
  catch { $failed += "$($j.Name) did not return parseable JSON. First 200 chars: $($txt.Substring(0, [Math]::Min(200, $txt.Length)))" }
}
Remove-Job -Job $jobs -Force

# REFUSE rather than degrade. One model answering is not a cross-check, and
# showing it as though it were is exactly the lie this whole beat is against.
if ($failed.Count -gt 0 -or $answers.Count -ne 2) {
  Write-Host "CROSS-CHECK FAILED - this is NOT a result, it is a missing one"
  foreach ($f in $failed) { Write-Host "  $f" }
  Write-Host "  models that answered: $($answers.Count) of 2"
  exit 1
}

# ---- compare -----------------------------------------------------------------
$a = $answers['openai']; $b = $answers['groq']

$fields = $CROSSCHECK_FIELDS
$rows  = Compare-Extractions -A $a -B $b -Fields $fields
$agree = @($rows | Where-Object { $_.same }).Count
$split = @($rows | Where-Object { -not $_.same }).Count

# ---- report ------------------------------------------------------------------
# Rendering and writing both live in the functions above, so -SelfTest exercises
# the SAME code a meeting does rather than a copy of it that can drift from it.
$text = Format-CrosscheckPanel -Rows $rows -ModelA $OpenAIModel -ModelB $GroqModel `
                               -Seconds $sw.Elapsed.TotalSeconds
Write-Host $text

if ($PanelFile) {
  # No key and no raw message body - but READ THIS BEFORE POINTING IT AT A REAL
  # CLIENT. The comparison necessarily contains the EXTRACTED VALUES: names of
  # documents, amounts, dates. With the invented sample that is nothing. With a
  # real client message it is customer content, and this file is shipped to a
  # disposable box and put on a screen someone else is watching. For a live
  # deployment the panel needs redaction, or the comparison stays on the laptop
  # and only the agreed/split COUNTS go to the screen.
  $panelPath = Write-CrosscheckPanel -Text $text -Path $PanelFile
  Write-Host ""
  # Report the path actually written, not the one asked for, so a relative
  # -PanelFile names a file the reader can go and open.
  Write-Host "panel file written: $panelPath ($((Get-Item -LiteralPath $panelPath).Length) bytes)"
}

Write-Host ""
Write-Host "RESULT agreed=$agree split=$split fields=$($fields.Count)"
exit 0
