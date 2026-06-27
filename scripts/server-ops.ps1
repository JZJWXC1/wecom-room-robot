param(
    [Parameter(Position = 0)]
    [ValidateSet("Exec", "Upload", "Download", "Status", "Journal", "RecentDialogues", "Context", "Health", "Test", "Restart", "SyncDryRun", "SyncRun", "RagCacheSync", "Timers", "UnattendedCheck")]
    [string]$Action = "Status",

    [Parameter(Position = 1)]
    [string]$Command = "",

    [string]$HostName = "114.55.168.97",
    [string]$User = "root",
    [string]$ProjectDir = "/opt/wecom-room-robot",
    [string]$ApproveDeploy = $env:ROOM_ROBOT_APPROVE_DEPLOY
)

$ErrorActionPreference = "Stop"

function Require-DeployApproval {
    if ($ApproveDeploy -ne "APPROVE_DEPLOY") {
        throw "Remote server operations require explicit APPROVE_DEPLOY. Set -ApproveDeploy APPROVE_DEPLOY or ROOM_ROBOT_APPROVE_DEPLOY=APPROVE_DEPLOY after user authorization."
    }
}

Require-DeployApproval

$CredentialFile = Join-Path (Get-Location) ".local/server-credentials.ps1"
if (Test-Path $CredentialFile) {
    $credentialText = Get-Content -Path $CredentialFile -Raw
    if (-not $env:ROOM_ROBOT_SSH_PASSWORD -and $credentialText -match 'ROOM_ROBOT_SSH_PASSWORD\s*=\s*["'']([^"'']+)["'']') {
        $env:ROOM_ROBOT_SSH_PASSWORD = $Matches[1]
    }
    if (-not $env:ROOM_ROBOT_SSH_KEY -and $credentialText -match 'ROOM_ROBOT_SSH_KEY\s*=\s*["'']([^"'']+)["'']') {
        $env:ROOM_ROBOT_SSH_KEY = $Matches[1]
    }
    if (-not $env:ROOM_ROBOT_PLINK -and $credentialText -match 'ROOM_ROBOT_PLINK\s*=\s*["'']([^"'']+)["'']') {
        $env:ROOM_ROBOT_PLINK = $Matches[1]
    }
}

function Assert-NativeCommandSucceeded {
    param([Parameter(Mandatory = $true)][string]$Label)

    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Get-SshBaseArgs {
    $key = $env:ROOM_ROBOT_SSH_KEY
    $args = @("-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=12")
    if ($key) {
        $args += @("-i", $key)
    }
    $args += @("$User@$HostName")
    return $args
}

function Invoke-RemoteCommand {
    param([Parameter(Mandatory = $true)][string]$RemoteCommand)

    $plink = $env:ROOM_ROBOT_PLINK
    if ($plink -and (Test-Path $plink)) {
        $plinkArgs = @("-ssh", "-batch", "-no-antispoof")
        if ($env:ROOM_ROBOT_SSH_PASSWORD) {
            $plinkArgs += @("-pw", $env:ROOM_ROBOT_SSH_PASSWORD)
        }
        elseif ($env:ROOM_ROBOT_SSH_KEY) {
            $plinkArgs += @("-i", $env:ROOM_ROBOT_SSH_KEY)
        }
        $plinkArgs += @("$User@$HostName", $RemoteCommand)
        & $plink @plinkArgs
        Assert-NativeCommandSucceeded "plink"
        return
    }

    if ($env:ROOM_ROBOT_SSH_PASSWORD) {
        $python = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        if (-not (Test-Path $python)) {
            $python = "python"
        }
        $serverExec = Join-Path (Get-Location) "scripts/server_exec.py"
        if (Test-Path $serverExec) {
            & $python $serverExec --host $HostName --user $User --command $RemoteCommand
            Assert-NativeCommandSucceeded "server_exec.py"
            return
        }
        throw "ROOM_ROBOT_SSH_PASSWORD is set, but neither plink nor scripts/server_exec.py is available."
    }

    $sshArgs = Get-SshBaseArgs
    & ssh @sshArgs $RemoteCommand
    Assert-NativeCommandSucceeded "ssh"
}

function Invoke-UploadFiles {
    param([Parameter(Mandatory = $true)][string]$Files)

    if (-not $env:ROOM_ROBOT_SSH_PASSWORD -and -not $env:ROOM_ROBOT_SSH_KEY) {
        throw "Upload requires ROOM_ROBOT_SSH_PASSWORD or ROOM_ROBOT_SSH_KEY."
    }

    $python = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (-not (Test-Path $python)) {
        $python = "python"
    }
    $serverUpload = Join-Path (Get-Location) "scripts/server_upload.py"
    if (-not (Test-Path $serverUpload)) {
        throw "missing scripts/server_upload.py"
    }

    $fileArgs = @()
    foreach ($file in ($Files -split ",")) {
        $trimmed = $file.Trim()
        if ($trimmed) {
            $fileArgs += $trimmed
        }
    }
    if ($fileArgs.Count -eq 0) {
        throw "Upload requires comma-separated project-relative files."
    }

    $uploadArgs = @("--host", $HostName, "--user", $User, "--remote-root", $ProjectDir)
    if ($env:ROOM_ROBOT_SSH_KEY) {
        $uploadArgs += @("--key", $env:ROOM_ROBOT_SSH_KEY)
    }
    & $python $serverUpload @uploadArgs @fileArgs
    Assert-NativeCommandSucceeded "server_upload.py"
}

function Invoke-DownloadFiles {
    param([Parameter(Mandatory = $true)][string]$Files)

    $python = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (-not (Test-Path $python)) {
        $python = "python"
    }
    $serverDownload = Join-Path (Get-Location) "scripts/server_download.py"
    if (-not (Test-Path $serverDownload)) {
        throw "missing scripts/server_download.py"
    }

    $fileArgs = @()
    foreach ($file in ($Files -split ",")) {
        $trimmed = $file.Trim()
        if ($trimmed) {
            $fileArgs += $trimmed
        }
    }
    if ($fileArgs.Count -eq 0) {
        throw "Download requires comma-separated project-relative files."
    }

    $downloadArgs = @("--host", $HostName, "--user", $User, "--remote-root", $ProjectDir)
    if ($env:ROOM_ROBOT_SSH_KEY) {
        $downloadArgs += @("--key", $env:ROOM_ROBOT_SSH_KEY)
    }
    & $python $serverDownload @downloadArgs @fileArgs
    Assert-NativeCommandSucceeded "server_download.py"
}

switch ($Action) {
    "Exec" {
        if (-not $Command) {
            throw "Exec requires a remote command."
        }
        Invoke-RemoteCommand $Command
    }
    "Upload" {
        if (-not $Command) {
            throw "Upload requires comma-separated project-relative files."
        }
        Invoke-UploadFiles $Command
    }
    "Download" {
        if (-not $Command) {
            throw "Download requires comma-separated project-relative files."
        }
        Invoke-DownloadFiles $Command
    }
    "Status" {
        Invoke-RemoteCommand "cd $ProjectDir && systemctl status wecom-room-robot --no-pager && systemctl status wecom-room-robot-feishu-region-sync.timer --no-pager && systemctl status wecom-room-robot-rag-cache-sync.timer --no-pager"
    }
    "Journal" {
        Invoke-RemoteCommand "journalctl -u wecom-room-robot -n 300 --no-pager"
    }
    "RecentDialogues" {
        Invoke-RemoteCommand "cd $ProjectDir && if [ -f data/kf_dialogue_events.jsonl ]; then tail -n 120 data/kf_dialogue_events.jsonl; else echo 'missing data/kf_dialogue_events.jsonl'; fi"
    }
    "Context" {
        Invoke-RemoteCommand "cd $ProjectDir && if [ -f data/wecom_kf_context.json ]; then tail -c 60000 data/wecom_kf_context.json; else echo 'missing data/wecom_kf_context.json'; fi"
    }
    "Health" {
        Invoke-RemoteCommand "curl -sS http://127.0.0.1:8000/health"
    }
    "Test" {
        Invoke-RemoteCommand "cd $ProjectDir && .venv/bin/python -m pytest -q"
    }
    "Restart" {
        Invoke-RemoteCommand "systemctl restart wecom-room-robot && sleep 2 && systemctl status wecom-room-robot --no-pager && curl -sS http://127.0.0.1:8000/health"
    }
    "SyncDryRun" {
        Invoke-RemoteCommand "cd $ProjectDir && .venv/bin/python scripts/sync_feishu_region_inventory.py --dry-run"
    }
    "SyncRun" {
        Invoke-RemoteCommand "cd $ProjectDir && .venv/bin/python scripts/sync_feishu_region_inventory.py"
    }
    "RagCacheSync" {
        Invoke-RemoteCommand "cd $ProjectDir && .venv/bin/python scripts/refresh_rag_inventory_cache.py"
    }
    "Timers" {
        Invoke-RemoteCommand "systemctl list-timers --all 'wecom-room-robot-*' --no-pager"
    }
    "UnattendedCheck" {
        $localCredentialState = if ($env:ROOM_ROBOT_SSH_PASSWORD -or $env:ROOM_ROBOT_SSH_KEY -or $env:ROOM_ROBOT_PLINK) { "ok" } else { "missing" }
        Write-Host "local_ssh_credential=$localCredentialState"
        Invoke-RemoteCommand "cd $ProjectDir && .venv/bin/python scripts/check_unattended_runtime.py --project-dir $ProjectDir"
    }
}
