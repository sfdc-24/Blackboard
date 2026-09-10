# Parse every PowerShell file in the repo under PowerShell 7 on Linux, then ask
# which of the cmdlets they call do not exist here.
#
# A parse error is proof of a portability problem. A clean parse is NOT proof of
# portability - it says the language accepted the text, nothing about whether
# Get-Service exists or a path resolves. Reported that way.
#
# THE FIRST VERSION OF THIS FILE LIED. It ran Get-Command over every
# Verb-Noun call and reported 106 "missing" cmdlets - but the scripts define
# most of those functions THEMSELVES, and nothing had loaded them. A number
# that large should have been the tell. Self-defined functions are collected
# from the ASTs first and subtracted.
$ErrorActionPreference = 'Continue'
$root = '/opt/blackboard/scripts'
if (-not (Test-Path -LiteralPath $root)) { Write-Output "no $root"; exit 1 }

$files = Get-ChildItem -LiteralPath $root -File |
  Where-Object { $_.Extension -in '.ps1', '.psm1' } | Sort-Object Name

$ok = 0; $bad = 0
$asts = @{}
foreach ($f in $files) {
  $tokens = $null; $errors = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $f.FullName, [ref]$tokens, [ref]$errors)
  $asts[$f.Name] = $ast
  $n = @($errors).Count
  if ($n -eq 0) {
    $ok++
    Write-Output ('{0,-34} PARSES   ({1} tokens)' -f $f.Name, @($tokens).Count)
  } else {
    $bad++
    Write-Output ('{0,-34} {1} PARSE ERROR(S)' -f $f.Name, $n)
    foreach ($e in ($errors | Select-Object -First 3)) {
      Write-Output ('      line {0}: {1}' -f $e.Extent.StartLineNumber, $e.Message)
    }
  }
}
Write-Output ''
Write-Output ("{0} parsed clean, {1} failed to parse, out of {2} files" -f $ok, $bad, $files.Count)

# Every function these files DEFINE. Subtracting these is the difference
# between a real finding and 106 lines of noise.
$defined = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($name in $asts.Keys) {
  if (-not $asts[$name]) { continue }
  foreach ($d in $asts[$name].FindAll({ param($n)
      $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    $null = $defined.Add($d.Name)
  }
}
Write-Output ''
Write-Output ("these files define {0} of their own functions" -f $defined.Count)

$referenced = @{}
foreach ($name in $asts.Keys) {
  if (-not $asts[$name]) { continue }
  foreach ($c in $asts[$name].FindAll({ param($n)
      $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $cmd = $c.GetCommandName()
    if (-not $cmd -or $cmd -notmatch '^[A-Za-z]+-[A-Za-z]+$') { continue }
    if ($defined.Contains($cmd)) { continue }        # their own function
    if (-not $referenced.ContainsKey($cmd)) {
      $referenced[$cmd] = New-Object 'System.Collections.Generic.HashSet[string]'
    }
    $null = $referenced[$cmd].Add($name)
  }
}

Write-Output ''
Write-Output '--- EXTERNAL cmdlets called that do NOT exist on this Linux host ---'
$missing = 0
foreach ($cmd in ($referenced.Keys | Sort-Object)) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    $missing++
    Write-Output ('  {0,-30} used by {1}' -f $cmd, (($referenced[$cmd] | Sort-Object) -join ', '))
  }
}
if ($missing -eq 0) { Write-Output '  none' }
Write-Output ''
Write-Output ("{0} external cmdlet(s) missing, out of {1} external cmdlets referenced" -f
  $missing, $referenced.Count)
