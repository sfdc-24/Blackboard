#Requires -Version 5.1
# Pure extraction validation/comparison; no files, credentials or provider calls.
$script:CrosscheckFields = @('tax_year','return_type','documents_received','documents_pending','figures','deadline_claimed','urgent')

function Assert-CrosscheckKeys {
  param($Value, [string[]]$Keys)
  if ($null -eq $Value -or $Value -isnot [pscustomobject]) { throw 'expected an object' }
  $actual = @($Value.PSObject.Properties.Name)
  if ($actual.Count -ne $Keys.Count) { throw 'unexpected object keys' }
  foreach ($key in $Keys) {
    if ($actual -cnotcontains $key) { throw "missing key $key" }
  }
}

function ConvertFrom-CrosscheckAnswer {
  param([string]$Json)
  # PS 5.1 unwraps a root singleton array; reject it before conversion.
  if ($Json.TrimStart() -notmatch '^\{') { throw 'expected a JSON object' }
  $value = ConvertFrom-Json -InputObject $Json -ErrorAction Stop
  Assert-CrosscheckKeys $value $script:CrosscheckFields
  if ($value.tax_year -isnot [string] -or $value.tax_year -cnotmatch '^(?:[0-9]{4}|unclear)$') { throw 'invalid tax_year' }
  if ($value.return_type -isnot [string] -or @('T1','T2','HST','unclear') -cnotcontains $value.return_type) { throw 'invalid return_type' }
  if ($value.deadline_claimed -isnot [string]) { throw 'invalid deadline_claimed' }
  if ($value.urgent -isnot [bool]) { throw 'invalid urgent' }
  foreach ($field in @('documents_received','documents_pending')) {
    if ($value.$field -isnot [Array]) { throw "invalid $field array" }
    foreach ($item in $value.$field) {
      if ($item -isnot [string]) { throw "invalid $field item" }
    }
  }
  if ($value.figures -isnot [Array]) { throw 'invalid figures array' }
  foreach ($figure in $value.figures) {
    Assert-CrosscheckKeys $figure @('label','amount')
    if ($figure.label -isnot [string]) { throw 'invalid figure label' }
    $amount = $figure.amount
    if ($null -eq $amount -or $amount.GetType().FullName -notin @(
      'System.Byte','System.SByte','System.Int16','System.UInt16','System.Int32',
      'System.UInt32','System.Int64','System.UInt64','System.Single','System.Double','System.Decimal'
    )) { throw 'invalid figure amount' }
    if ([double]::IsNaN([double]$amount) -or [double]::IsInfinity([double]$amount)) { throw 'nonfinite figure amount' }
  }
  return $value
}

function Test-CrosscheckText {
  param([string]$Left, [string]$Right)
  return [string]::Equals($Left, $Right, [StringComparison]::OrdinalIgnoreCase)
}

function Test-CrosscheckField {
  param([string]$Field, $Left, $Right)
  if ($Field -eq 'urgent') { return $Left -eq $Right }
  if ($Field -notin @('documents_received','documents_pending','figures')) {
    return Test-CrosscheckText $Left $Right
  }
  # Unordered multisets: preserve element boundaries AND duplicate counts.
  if ($Left.Count -ne $Right.Count) { return $false }
  $used = New-Object 'bool[]' $Right.Count
  foreach ($item in $Left) {
    $found = $false
    for ($i = 0; $i -lt $Right.Count; $i++) {
      if ($used[$i]) { continue }
      if ($Field -eq 'figures') {
        # Check both directions: PowerShell can otherwise round a double to
        # the integer type on the left and label 1 and 1.4 equal.
        $same = (Test-CrosscheckText $item.label $Right[$i].label) -and
          ($item.amount -eq $Right[$i].amount) -and ($Right[$i].amount -eq $item.amount)
      } else { $same = Test-CrosscheckText $item $Right[$i] }
      if ($same) { $used[$i] = $true; $found = $true; break }
    }
    if (-not $found) { return $false }
  }
  return $true
}

function Format-CrosscheckField {
  param($Value)
  # JSON escapes line breaks and delimiters and retains [] / singleton shape.
  return ConvertTo-Json -InputObject $Value -Depth 6 -Compress
}

function Compare-CrosscheckAnswers {
  param($Left, $Right)
  foreach ($field in $script:CrosscheckFields) {
    # Direct property access avoids pipeline unrolling through Get-Field.
    [pscustomobject]@{
      field = $field
      a = Format-CrosscheckField $Left.$field
      b = Format-CrosscheckField $Right.$field
      same = Test-CrosscheckField $field $Left.$field $Right.$field
    }
  }
}
