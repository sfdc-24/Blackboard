#Requires -Version 5.1
<#
Internal child-process adapter for order_supervisor.ps1.

The parent launches this script as a process so it can enforce a wall-clock
timeout over Claude and its descendants. The board payload is read from a
temporary file and sent on stdin; it is never placed on the process command
line. Do not call this adapter directly.
#>
param(
    [Parameter(Mandatory = $true)][string]$PromptPath,
    [Parameter(Mandatory = $true)][string]$SchemaPath,
    [Parameter(Mandatory = $true)][string]$StdoutPath,
    [Parameter(Mandatory = $true)][string]$StderrPath,
    [string]$ClaudeCommand = 'claude',
    [ValidateRange(0.01, 20.0)][double]$MaxBudgetUsd = 2.0
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if (-not (Test-Path -LiteralPath $PromptPath)) { throw 'prompt_file_missing' }
if (-not (Test-Path -LiteralPath $SchemaPath)) { throw 'schema_file_missing' }
$resolved = Get-Command $ClaudeCommand -ErrorAction Stop
$promptText = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $PromptPath).Path, [Text.Encoding]::UTF8)
$schemaText = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $SchemaPath).Path, [Text.Encoding]::UTF8)
# Windows PowerShell 5.1's native argv marshaller otherwise removes the JSON
# quotation marks. Backslash-escaped quotes arrive intact at the Node CLI.
$schemaArgument = if ($PSVersionTable.PSEdition -eq 'Desktop') {
    ($schemaText -replace '"', '\"')
} else {
    $schemaText
}
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

$arguments = @(
    '--print',
    '--output-format', 'json',
    '--json-schema', $schemaArgument,
    '--permission-mode', 'auto',
    '--permission-prompts', 'none',
    '--max-budget-usd', ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:0.00}', $MaxBudgetUsd)),
    '--safe-mode',
    '--no-session-persistence',
    '--disable-slash-commands'
)

# Native output is isolated in files. The parent validates structured_output and
# never copies stderr or raw model output into its state or JSONL log.
$promptText | & $resolved.Source @arguments 1> $StdoutPath 2> $StderrPath
exit $LASTEXITCODE
