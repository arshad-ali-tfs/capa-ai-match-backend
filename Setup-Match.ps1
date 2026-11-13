param([string]$Source = "$PSScriptRoot\data\sources\trackwise_records.csv", [string]$Sheet)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { py -3.11 -m venv .venv; if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required.' } }
& ./.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'MATCH dependencies could not be installed.' }
& ./.venv/Scripts/python.exe -m pip install -e '.[test]' --no-deps --no-build-isolation
if ($LASTEXITCODE -ne 0) { throw 'MATCH package installation failed.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
if ($Source) {
    if (-not (Test-Path -LiteralPath $Source)) { throw 'Source not found. Fetch it with git lfs pull or pass -Source.' }
    if ($Sheet) { & ./.venv/Scripts/python.exe -c 'from capa_match.cli import import_source; import_source()' $Source --sheet $Sheet }
    else { & ./.venv/Scripts/python.exe -c 'from capa_match.cli import import_source; import_source()' $Source }
    if ($LASTEXITCODE -ne 0) { throw 'Source import failed.' }
}
