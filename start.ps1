param([switch]$Check)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$appPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$appArguments = @('tools\bootstrap.py')
if ($Check) { $appArguments += '--check' }
if (Test-Path -LiteralPath $appPython) {
    & $appPython @appArguments
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py @appArguments
} else {
    & python @appArguments
}
exit $LASTEXITCODE
