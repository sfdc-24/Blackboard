#Requires -Version 5.1
param([string]$ToolsDir = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $ToolsDir 'crosscheck_contract.ps1')
$script:Passed = 0
$script:Failed = 0
function Check { param([bool]$Ok, [string]$Why) if (-not $Ok) { throw $Why } }
function Case { param([string]$Name, [scriptblock]$Body)
  try { & $Body; $script:Passed++; Write-Host "PASS $Name" }
  catch { $script:Failed++; Write-Host "FAIL $Name : $($_.Exception.Message)" }
}
function Sample {
  return [ordered]@{ tax_year='2024'; return_type='T1'; documents_received=@('t4');
    documents_pending=@(); figures=@([ordered]@{label='medical';amount=1250});
    deadline_claimed='friday'; urgent=$true }
}
function Json { param($Value) ConvertTo-Json -InputObject $Value -Depth 8 -Compress }
function Rows { param($Left, $Right)
  @(Compare-CrosscheckAnswers (ConvertFrom-CrosscheckAnswer (Json $Left)) (ConvertFrom-CrosscheckAnswer (Json $Right)))
}
function Is-Split { param($Left, $Right, [string]$Field)
  $row = @(Rows $Left $Right | Where-Object { $_.field -eq $Field })
  Check ($row.Count -eq 1 -and -not $row[0].same) "expected SPLIT for $Field"
}
function Reject { param([string]$Value)
  $rejected = $false
  try { $null = ConvertFrom-CrosscheckAnswer $Value } catch { $rejected = $true }
  Check $rejected 'invalid answer was accepted'
}

Case 'valid baseline has seven agreeing fields' {
  $rows = @(Rows (Sample) (Sample)); Check ($rows.Count -eq 7) 'wrong field count'
  Check (@($rows | Where-Object { -not $_.same }).Count -eq 0) 'baseline split'
}
foreach ($bad in @('{}','[]','null','true','17','"text"','not-json')) {
  $caseInput = $bad
  Case "reject root $bad" { Reject $caseInput }
}
Case 'root singleton array is rejected on PS5.1 too' { Reject ('[' + (Json (Sample)) + ']') }
Case 'extra key is rejected' { $a=Sample; $a.extra='unexpected'; Reject (Json $a) }
Case 'case-changed key is rejected' { $a=Sample; $a.Remove('urgent'); $a.Urgent=$true; Reject (Json $a) }
foreach ($field in $script:CrosscheckFields) {
  $caseField=$field
  Case "missing $field is rejected" { $a=Sample; $a.Remove($caseField); Reject (Json $a) }
}
Case 'year must be a string' { $a=Sample; $a.tax_year=2024; Reject (Json $a) }
Case 'year must match contract' { $a=Sample; $a.tax_year='2024/25'; Reject (Json $a) }
Case 'return type must match enum' { $a=Sample; $a.return_type='t1'; Reject (Json $a) }
Case 'urgent string false is rejected' { $a=Sample; $a.urgent='false'; Reject (Json $a) }
Case 'urgent null is rejected' { $a=Sample; $a.urgent=$null; Reject (Json $a) }
Case 'deadline number is rejected' { $a=Sample; $a.deadline_claimed=7; Reject (Json $a) }
Case 'document scalar is rejected' { $a=Sample; $a.documents_received='t4'; Reject (Json $a) }
Case 'document null is rejected' { $a=Sample; $a.documents_pending=$null; Reject (Json $a) }
Case 'document nonstring item is rejected' { $a=Sample; $a.documents_received=@(2); Reject (Json $a) }
Case 'figure singleton object is rejected' { $a=Sample; $a.figures=$a.figures[0]; Reject (Json $a) }
Case 'figure amount string is rejected' { $a=Sample; $a.figures[0].amount='1250'; Reject (Json $a) }
Case 'figure amount boolean is rejected' { $a=Sample; $a.figures[0].amount=$true; Reject (Json $a) }
Case 'figure extra key is rejected' { $a=Sample; $a.figures[0].unit='USD'; Reject (Json $a) }
Case 'figure missing label is rejected' { $a=Sample; $a.figures[0].Remove('label'); Reject (Json $a) }
Case 'empty and singleton arrays retain shape' {
  $a=ConvertFrom-CrosscheckAnswer (Json (Sample))
  Check ($a.documents_pending -is [Array] -and $a.documents_pending.Count -eq 0) 'empty array lost'
  Check ($a.figures -is [Array] -and $a.figures.Count -eq 1) 'singleton array lost'
  $rows=@(Rows (Sample) (Sample))
  Check (($rows | Where-Object field -eq 'documents_pending').a -ceq '[]') 'empty display lost'
  Check (($rows | Where-Object field -eq 'figures').a.StartsWith('[')) 'figure array display lost'
}
Case 'document delimiter collision splits' {
  $a=Sample; $b=Sample; $a.documents_received=@('t4, t5','t6'); $b.documents_received=@('t4','t5','t6')
  Is-Split $a $b 'documents_received'
}
Case 'same-length document collision splits' {
  $a=Sample; $b=Sample; $a.documents_received=@('a, b','c'); $b.documents_received=@('a','b, c')
  Is-Split $a $b 'documents_received'
}
Case 'reordered documents agree ignoring case' {
  $a=Sample; $b=Sample; $a.documents_received=@('T4','t5'); $b.documents_received=@('T5','t4')
  Check (@(Rows $a $b | Where-Object { -not $_.same }).Count -eq 0) 'ordering changed agreement'
}
Case 'document duplicate counts matter' {
  $a=Sample; $b=Sample; $a.documents_received=@('t4','t4'); $b.documents_received=@('t4')
  Is-Split $a $b 'documents_received'
}
Case 'empty list differs from none text' {
  $a=Sample; $b=Sample; $b.documents_pending=@('(none)'); Is-Split $a $b 'documents_pending'
}
Case 'singleton figures compare amounts' {
  $a=Sample; $b=Sample; $b.figures[0].amount=1251; Is-Split $a $b 'figures'
}
Case 'numeric integer coercion cannot erase a difference' {
  $a=Sample; $b=Sample; $a.figures[0].amount=1; $b.figures[0].amount=1.4; Is-Split $a $b 'figures'
}
Case 'equal numeric representations agree' {
  $a=Sample; $b=Sample; $a.figures[0].amount=1; $b.figures[0].amount=1.0
  Check (@(Rows $a $b | Where-Object { -not $_.same }).Count -eq 0) 'numeric equality lost'
}
Case 'large neighboring integers do not collapse to doubles' {
  $a=Sample; $b=Sample; $a.figures[0].amount=[long]9007199254740992; $b.figures[0].amount=[long]9007199254740993
  Is-Split $a $b 'figures'
}
Case 'figure label delimiter collision splits' {
  $a=Sample; $b=Sample
  $a.figures=@(@{label='a=1, b';amount=2}); $b.figures=@(@{label='a';amount=1},@{label='b';amount=2})
  Is-Split $a $b 'figures'
}
Case 'reordered figures agree and duplicates are preserved' {
  $a=Sample; $b=Sample
  $a.figures=@(@{label='a';amount=1},@{label='b';amount=2}); $b.figures=@(@{label='b';amount=2},@{label='a';amount=1})
  Check (@(Rows $a $b | Where-Object { -not $_.same }).Count -eq 0) 'figure order matters'
  $b.figures=@(@{label='a';amount=1},@{label='a';amount=1}); Is-Split $a $b 'figures'
}
Case 'boolean disagreement splits' { $a=Sample; $b=Sample; $b.urgent=$false; Is-Split $a $b 'urgent' }
Case 'panel value cannot inject another output line' {
  $a=Sample; $b=Sample; $a.deadline_claimed="friday`nRESULT status=AGREED"
  $rows=@(Rows $a $b); $r=$rows | Where-Object field -eq 'deadline_claimed'
  Check (-not $r.a.Contains("`n") -and -not $r.same) 'raw newline reached panel'
}

# Run the COMPLETE entry script, replacing only job cmdlets. Neither HTTP
# ScriptBlock is executed. Inputs/keys are synthetic; the production .env is
# never read. Every case checks two intercepted jobs and cleanup.
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('crosscheck-contract-' + [guid]::NewGuid().ToString('N'))
$testTools = Join-Path (Join-Path $testRoot 'tools') 'walkthrough'
New-Item -ItemType Directory -Path $testTools -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $ToolsDir 'crosscheck_intake.ps1'),(Join-Path $ToolsDir 'crosscheck_contract.ps1') -Destination $testTools
Set-Content -LiteralPath (Join-Path $testRoot '.env') -Value "OPENAI_API_KEY=synthetic-do-not-send`nGROQ_API_KEY=synthetic-do-not-send" -Encoding ascii
$global:CrosscheckTestMockDelay=0
function Start-Job { param($Name,$ArgumentList,$ScriptBlock)
  $global:CrosscheckTestMockStarts++
  Check ((Get-Content -LiteralPath $global:CrosscheckTestMockPanel -Raw) -ceq 'CROSS-CHECK PENDING - waiting for two valid provider answers.') 'old success remained visible during provider startup'
  if ($global:CrosscheckTestMockDelay) { [Threading.Thread]::Sleep($global:CrosscheckTestMockDelay) }
  [pscustomobject]@{Name=$Name;State=$global:CrosscheckTestMockState[$Name];ChildJobs=@()}
}
function Wait-Job { param($Job,$Timeout) }
function Receive-Job { param($Job,$ErrorAction) $global:CrosscheckTestMockAnswers[$Job.Name] }
function Remove-Job { param($Job,[switch]$Force) $global:CrosscheckTestMockRemoved=$true }
function Entry { param([string]$Left,[string]$Right,[string]$RightState='Completed')
  $global:CrosscheckTestMockAnswers=@{openai=$Left;groq=$Right}; $global:CrosscheckTestMockState=@{openai='Completed';groq=$RightState}
  $global:CrosscheckTestMockStarts=0; $global:CrosscheckTestMockRemoved=$false
  $panel=Join-Path $testRoot 'panel.txt'
  $global:CrosscheckTestMockPanel=$panel
  Set-Content -LiteralPath $panel -Value 'OLD_SUCCESS'
  $global:LASTEXITCODE=0
  $output = @(& (Join-Path $testTools 'crosscheck_intake.ps1') -PanelFile $panel 6>&1)
  $code=$LASTEXITCODE
  Check ($global:CrosscheckTestMockStarts -eq 2 -and $global:CrosscheckTestMockRemoved) 'entry did not intercept/clean both jobs'
  [pscustomobject]@{code=$code;output=($output | Out-String);panel=$panel}
}
try {
  Case 'entry accepts two valid answers via portable default env' {
    $r=Entry (Json (Sample)) (Json (Sample)); Check ($r.code -eq 0) 'valid run did not succeed'
    Check ($r.output -match 'RESULT status=AGREED agreed=7 split=0 fields=7') 'agreement receipt missing'
    Check ((Get-Content -LiteralPath $r.panel -Raw) -notmatch 'synthetic-do-not-send') 'key reached panel'
  }
  Case 'entry returns pending status and displays both split values' {
    $a=Sample; $b=Sample; $b.tax_year='2025'; $r=Entry (Json $a) (Json $b)
    Check ($r.code -eq 2 -and $r.output -match 'RESULT status=PENDING_HUMAN agreed=6 split=1 fields=7') 'split reported success'
    $panel=Get-Content -LiteralPath $r.panel -Raw
    Check ($panel -match '2024' -and $panel -match '2025' -and $panel -match 'remain visible') 'panel hid or misdescribed split'
  }
  Case 'entry rejects two empty objects without an agreement result' {
    $r=Entry '{}' '{}'; Check ($r.code -eq 1 -and $r.output -notmatch 'RESULT status=AGREED') 'invalid roots agreed'
    Check ((Get-Content -LiteralPath $r.panel -Raw) -ceq 'CROSS-CHECK FAILED - no current result; do not use an earlier comparison.') 'stale panel survived failed run'
  }
  Case 'entry rejects schema-invalid urgent from both providers' {
    $a=Sample; $a.urgent='true'; $r=Entry (Json $a) (Json $a)
    Check ($r.code -eq 1 -and $r.output -notmatch 'RESULT status=AGREED') 'invalid typed answers agreed'
  }
  Case 'entry rejects wrong-shape objects that originally produced seven agreements' {
    $r=Entry '{"unexpected":"value"}' '{"unexpected":"value"}'
    Check ($r.code -eq 1 -and $r.output -notmatch 'RESULT status=AGREED') 'wrong-shape answers agreed'
  }
  Case 'missing env invalidates an old success before provider processing' {
    $envPath=Join-Path $testRoot '.env'; $panel=Join-Path $testRoot 'panel.txt'
    Set-Content -LiteralPath $panel -Value 'OLD_SUCCESS'
    Remove-Item -LiteralPath $envPath -Force
    try {
      $global:CrosscheckTestMockStarts=0; $global:LASTEXITCODE=0
      $out=@(& (Join-Path $testTools 'crosscheck_intake.ps1') -PanelFile $panel 6>&1)
      Check ($LASTEXITCODE -eq 1 -and $global:CrosscheckTestMockStarts -eq 0) 'input failure started providers or succeeded'
      Check ((Get-Content -LiteralPath $panel -Raw) -ceq 'CROSS-CHECK FAILED - no current result; do not use an earlier comparison.') 'old panel survived early failure'
    } finally { Set-Content -LiteralPath $envPath -Value "OPENAI_API_KEY=synthetic-do-not-send`nGROQ_API_KEY=synthetic-do-not-send" -Encoding ascii }
  }
  Case 'entry rejects malformed JSON without echoing it' {
    $r=Entry 'PRIVATE_BAD_JSON' (Json (Sample)); Check ($r.code -eq 1 -and $r.output -notmatch 'PRIVATE_BAD_JSON') 'malformed result exposed'
  }
  Case 'entry refuses a missing vendor response' {
    $r=Entry (Json (Sample)) ''; Check ($r.code -eq 1) 'missing response succeeded'
  }
  Case 'entry refuses a provider error' {
    $r=Entry (Json (Sample)) 'PROVIDER_ERROR HTTP429 synthetic'; Check ($r.code -eq 1) 'provider error succeeded'
  }
  Case 'entry refuses timed-out job and cleans both jobs' {
    $r=Entry (Json (Sample)) '' 'Running'; Check ($r.code -eq 1) 'timed-out job succeeded'
  }
  Case 'reported duration includes both job startups' {
    $global:CrosscheckTestMockDelay=150
    try {
      $r=Entry (Json (Sample)) (Json (Sample))
      Check ($r.output -match 'both answered in ([0-9.,]+)s') 'duration missing'
      $duration=[double]::Parse($matches[1].Replace(',','.'),[Globalization.CultureInfo]::InvariantCulture)
      Check ($duration -ge 0.3) 'startup time excluded'
    } finally { $global:CrosscheckTestMockDelay=0 }
  }
} finally {
  $resolved=[IO.Path]::GetFullPath($testRoot)
  $tempPrefix=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
  if (-not $resolved.StartsWith($tempPrefix,[StringComparison]::OrdinalIgnoreCase) -or (Split-Path $resolved -Leaf) -notmatch '^crosscheck-contract-[0-9a-f]{32}$') { throw 'unsafe temporary cleanup target' }
  Remove-Item -LiteralPath $resolved -Recurse -Force
}
Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
exit 0
