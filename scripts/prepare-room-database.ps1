param(
    [string]$SourcePath = "",
    [string]$OutputZip = "dist\room_database.zip"
)

$ErrorActionPreference = "Stop"

if (-not $SourcePath) {
    $SourcePath = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String("RDpc5oi/5rqQ5pWw5o2u5bqT")
    )
}

if (-not (Test-Path -LiteralPath $SourcePath)) {
    throw "Source path not found: $SourcePath"
}

$outputFullPath = Join-Path (Get-Location) $OutputZip
$outputDir = Split-Path -Parent $outputFullPath
if (-not (Test-Path -LiteralPath $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

if (Test-Path -LiteralPath $outputFullPath) {
    Remove-Item -LiteralPath $outputFullPath -Force
}

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

$script = @"
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

source = Path(r'''$SourcePath''')
output = Path(r'''$outputFullPath''')
if output.exists():
    output.unlink()
with ZipFile(output, 'w', ZIP_DEFLATED) as zip_file:
    for path in source.rglob('*'):
        if path.is_file():
            zip_file.write(path, path.relative_to(source).as_posix())
"@

$script | & $python -

$files = Get-ChildItem -LiteralPath $SourcePath -Recurse -File
$size = ($files | Measure-Object -Property Length -Sum).Sum

[pscustomobject]@{
    Source = $SourcePath
    Output = $outputFullPath
    Files = $files.Count
    SizeMB = [math]::Round($size / 1MB, 2)
}
