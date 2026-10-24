param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ServerArgs)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe = (Get-Command python.exe -ErrorAction Stop).Source }
$priorPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repoRoot 'src'
    & $pythonExe -m raddebugger_mcp @ServerArgs
    exit $LASTEXITCODE
} finally { $env:PYTHONPATH = $priorPath }
