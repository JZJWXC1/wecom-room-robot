param(
    [ValidateSet("L1", "L2", "L3", "L4")]
    [string]$Level = "L1",
    [string]$Python = "",
    [int]$FullRepeat = 3,
    [int]$ParityCases = 20,
    [switch]$SkipParity
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

Set-StrictMode -Version Latest

$ScriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptPath "..")
Set-Location $RepoRoot

if (-not $Python) {
    $BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path $BundledPython) {
        $Python = $BundledPython
    } else {
        $Python = "python"
    }
}

$DefaultDeps = Join-Path $env:TEMP "wecom-room-robot-local-test-deps"
$PythonPathParts = @([string]$RepoRoot)
if ((Test-Path $DefaultDeps) -and ($DefaultDeps -ne [string]$RepoRoot)) {
    $PythonPathParts += $DefaultDeps
}
if ($env:PYTHONPATH) {
    $PythonPathParts += ($env:PYTHONPATH -split [System.IO.Path]::PathSeparator)
}
$env:PYTHONPATH = ($PythonPathParts | Where-Object { $_ } | Select-Object -Unique) -join [System.IO.Path]::PathSeparator

$env:RUN_ONLINE_QA = "0"

function Invoke-GateStep {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    Write-Host ""
    Write-Host "==> $Name"
    $started = Get-Date
    try {
        & $Action
        $elapsed = (Get-Date) - $started
        Write-Host ("PASS {0} ({1:n1}s)" -f $Name, $elapsed.TotalSeconds)
    } catch {
        $elapsed = (Get-Date) - $started
        Write-Host ("FAIL {0} ({1:n1}s)" -f $Name, $elapsed.TotalSeconds) -ForegroundColor Red
        throw
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-PytestFiles {
    param([Parameter(Mandatory = $true)][string[]]$TestFiles)

    $args = @("-m", "pytest", "-q") + $TestFiles
    Invoke-External -FilePath $Python -Arguments $args
}

function Get-SecretScanValue {
    param([Parameter(Mandatory = $true)][string]$MatchText)

    $parts = $MatchText -split "[:=]", 2
    if ($parts.Count -lt 2) {
        return ""
    }
    $value = $parts[1].Trim()
    if ($value.StartsWith("'") -or $value.StartsWith('"')) {
        $value = $value.Substring(1)
    }
    return $value.Trim()
}

function Test-AllowedSecretScanFinding {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string]$PatternName,
        [Parameter(Mandatory = $true)][string]$MatchText
    )

    if ($PatternName -ne "assigned runtime secret") {
        return $false
    }

    $normalized = $File -replace "\\", "/"
    $value = Get-SecretScanValue -MatchText $MatchText
    if (-not $value) {
        return $false
    }

    $isEnvExample = $normalized.EndsWith(".env.example") -or $normalized.EndsWith("/safe_runtime.env.example")
    if ($isEnvExample -and ($value -match "^(your[_-]|missing[_-])[A-Za-z0-9_-]+$")) {
        return $true
    }

    if ($value -match "^\{[A-Za-z_][A-Za-z0-9_]*\}(&|$)") {
        return $true
    }

    if ($normalized.StartsWith("tests/") -and ($value -in @("previous_secret", "previous_key"))) {
        return $true
    }

    return $false
}

function Invoke-SecretScan {
    $patterns = @(
        @{ Name = "private key"; Regex = "-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----" },
        @{ Name = "OpenAI-style key"; Regex = "\bsk-(proj-)?[A-Za-z0-9_-]{20,}\b" },
        @{ Name = "GitHub token"; Regex = "\bgh[pousr]_[A-Za-z0-9_]{20,}\b" },
        @{ Name = "AWS access key"; Regex = "\bAKIA[0-9A-Z]{16}\b" },
        @{ Name = "assigned runtime secret"; Regex = "\b(FEISHU_APP_SECRET|WECOM_(KF_)?(SECRET|TOKEN|AES_KEY|ENCODING_AES_KEY)|DASHSCOPE_API_KEY|OPENAI_API_KEY|ACCESS_TOKEN)[^\S\r\n]*[:=][^\S\r\n]*['""]?[^'""\s#]{12,}" }
    )
    $excludePrefixes = @(
        ".git/",
        ".venv/",
        "venv/",
        ".codex-test-deps/",
        ".codex-pytest-deps/",
        "room_database/",
        "data/",
        "server_snapshots/"
    )
    $findings = New-Object System.Collections.Generic.List[string]
    $files = & git ls-files
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-files failed"
    }

    foreach ($file in $files) {
        $normalized = $file -replace "\\", "/"
        $excluded = $false
        foreach ($prefix in $excludePrefixes) {
            if ($normalized.StartsWith($prefix)) {
                $excluded = $true
                break
            }
        }
        if ($excluded -or -not (Test-Path $file -PathType Leaf)) {
            continue
        }
        $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $file))
        if ($bytes -contains 0) {
            continue
        }
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        foreach ($pattern in $patterns) {
            $matches = [regex]::Matches($text, $pattern.Regex, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
            foreach ($match in $matches) {
                if (Test-AllowedSecretScanFinding -File $file -PatternName $pattern.Name -MatchText $match.Value) {
                    continue
                }
                $line = ($text.Substring(0, $match.Index) -split "`n").Count
                $findings.Add("${file}:$line $($pattern.Name)")
            }
        }
    }

    if ($findings.Count -gt 0) {
        $findings | ForEach-Object { Write-Host $_ -ForegroundColor Red }
        throw "Secret scan found $($findings.Count) potential sensitive value(s)."
    }
}

function Invoke-Parity20 {
    if ($SkipParity) {
        Write-Host "SkipParity enabled; parity runner skipped."
        return
    }

    $runners = @(
        "qa_artifacts/run_rag_10windows_10turns_utf8.py",
        "qa_artifacts/run_rag_holdout_10windows_10turns_utf8.py"
    )
    $available = @()
    foreach ($runner in $runners) {
        if (Test-Path $runner) {
            $available += $runner
        }
    }
    if ($available.Count -lt 2 -and (Test-Path "qa_artifacts/run_rag_10similar_10turns_utf8.py")) {
        $available += "qa_artifacts/run_rag_10similar_10turns_utf8.py"
    }
    if ($available.Count -lt 2) {
        throw "Need at least two 10-window parity runners to cover 20+ cases."
    }

    foreach ($runner in $available[0..1]) {
        Invoke-External -FilePath $Python -Arguments @($runner)
    }
    Write-Host "Parity target satisfied: $ParityCases+ cases via two 10-window runners."
}

$L1Tests = @(
    "tests/test_kf_contracts.py",
    "tests/test_media_manifest.py"
)

$L2Tests = @(
    "tests/test_wecom_kf.py",
    "tests/test_kf_agentic_rag.py",
    "tests/test_kf_dual_llm_shadow.py",
    "tests/test_llm.py",
    "tests/test_media_store.py",
    "tests/test_media_manifest.py",
    "tests/test_inventory_query.py",
    "tests/test_inventory_read_router.py",
    "tests/test_inventory_sensitive_access.py"
)

$L4RollbackTests = @(
    "tests/test_inventory_snapshot.py",
    "tests/test_inventory_snapshot_m1d2b2.py"
)

Write-Host "RAG V2 QA Fast Gates"
Write-Host "Repo: $RepoRoot"
Write-Host "Level: $Level"
Write-Host "Python: $Python"
Write-Host "RUN_ONLINE_QA=$env:RUN_ONLINE_QA"

if ($Level -eq "L1") {
    Invoke-GateStep "contracts and media manifest tests" { Invoke-PytestFiles $L1Tests }
    Invoke-GateStep "compileall app" { Invoke-External -FilePath $Python -Arguments @("-m", "compileall", "app") }
    Invoke-GateStep "git diff --check" { Invoke-External -FilePath "git" -Arguments @("diff", "--check") }
    exit 0
}

if ($Level -eq "L2") {
    Invoke-GateStep "L1 contracts/media/compile/diff" {
        Invoke-PytestFiles $L1Tests
        Invoke-External -FilePath $Python -Arguments @("-m", "compileall", "app")
        Invoke-External -FilePath "git" -Arguments @("diff", "--check")
    }
    Invoke-GateStep "agentic RAG, LLM, candidate and media binding tests" { Invoke-PytestFiles $L2Tests }
    exit 0
}

if ($Level -eq "L3") {
    Invoke-GateStep "single full pytest" { Invoke-External -FilePath $Python -Arguments @("-m", "pytest", "-q") }
    Invoke-GateStep "compileall app" { Invoke-External -FilePath $Python -Arguments @("-m", "compileall", "app") }
    Invoke-GateStep "git diff --check" { Invoke-External -FilePath "git" -Arguments @("diff", "--check") }
    exit 0
}

if ($Level -eq "L4") {
    if ($FullRepeat -lt 3) {
        throw "L4 requires FullRepeat >= 3."
    }
    for ($i = 1; $i -le $FullRepeat; $i++) {
        Invoke-GateStep "full pytest repeat $i/$FullRepeat" {
            Invoke-External -FilePath $Python -Arguments @("-m", "pytest", "-q")
        }
    }
    Invoke-GateStep "20+ parity QA" { Invoke-Parity20 }
    Invoke-GateStep "rollback and cutover safety tests" { Invoke-PytestFiles $L4RollbackTests }
    Invoke-GateStep "secret scan" { Invoke-SecretScan }
    Invoke-GateStep "compileall app" { Invoke-External -FilePath $Python -Arguments @("-m", "compileall", "app") }
    Invoke-GateStep "git diff --check" { Invoke-External -FilePath "git" -Arguments @("diff", "--check") }
    exit 0
}
