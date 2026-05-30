param(
    [Parameter(Mandatory = $true)]
    [string]$HostName,

    [string]$User = "root",

    [string]$RemoteProjectDir = "/opt/wecom-room-robot",

    [string]$SourcePath = "",

    [string]$KeyPath = ""
)

$ErrorActionPreference = "Stop"

if (-not $SourcePath) {
    $SourcePath = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String("RDpc5oi/5rqQ5pWw5o2u5bqT")
    )
}

$zipResult = & "$PSScriptRoot\prepare-room-database.ps1" -SourcePath $SourcePath
$zipPath = $zipResult.Output
$remote = "$User@$HostName"
$remoteZip = "/tmp/room_database.zip"
$sshArgs = @()

if ($KeyPath) {
    $sshArgs += @("-i", $KeyPath)
}

& scp @sshArgs $zipPath "${remote}:${remoteZip}"
& ssh @sshArgs $remote "rm -rf '$RemoteProjectDir/room_database' && mkdir -p '$RemoteProjectDir/room_database' && python3 - <<'PY'
from pathlib import Path
from zipfile import ZipFile
zip_path = Path('/tmp/room_database.zip')
target = Path('$RemoteProjectDir/room_database')
with ZipFile(zip_path) as zf:
    for member in zf.infolist():
        name = member.filename.replace('\\\\', '/')
        if not name or name.endswith('/'):
            continue
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, dest.open('wb') as dst:
            dst.write(src.read())
zip_path.unlink(missing_ok=True)
for p in sorted(target.rglob('*')):
    if p.is_file():
        print(f'{p}\t{p.stat().st_size}')
PY"

Write-Host "Room database uploaded to ${remote}:${RemoteProjectDir}/room_database"
