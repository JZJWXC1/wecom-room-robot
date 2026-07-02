param(
    [ValidateSet("L1", "L2", "L3", "L4")]
    [string]$Level = "L1",
    [string]$Python = "",
    [int]$FullRepeat = 3,
    [int]$ParityCases = 20,
    [switch]$SkipParity,
    [switch]$AllowMissingRealDialogues,
    [switch]$AllowMissingHistoricalFailures,
    [string]$HistoricalFailuresFixture = "tests/fixtures/qa/historical_failures_synthetic_sanitized.json",
    [string]$HistoricalFailuresArtifact = "",
    [switch]$RunHistoricalFailureGateOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

Set-StrictMode -Version Latest

$ScriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptPath "..")
Set-Location $RepoRoot
$script:ReleaseBlockers = New-Object System.Collections.Generic.List[string]

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

function Resolve-QaGatePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return Join-Path $RepoRoot $Path
}

function Protect-QaSensitiveText {
    param([AllowNull()][object]$Text)

    if ($null -eq $Text) {
        return ""
    }
    $value = "$Text"
    $value = [regex]::Replace($value, "(?<!\d)1[3-9]\d{9}(?!\d)", "[redacted]")
    $value = [regex]::Replace($value, "(?i)\bsk-(proj-)?[A-Za-z0-9_-]{12,}\b", "[redacted]")
    $value = [regex]::Replace($value, "(?i)\bgh[pousr]_[A-Za-z0-9_]{12,}\b", "[redacted]")
    $value = [regex]::Replace($value, "\bAKIA[0-9A-Z]{16}\b", "[redacted]")
    $value = [regex]::Replace(
        $value,
        "(?i)\b(access[_-]?token|token|secret|password|credential|signature)\b[^\r\n,，;；]{0,20}[:=：][^\r\n,，;；}]+",
        "[redacted]"
    )
    $value = [regex]::Replace($value, "(密码|签名)[^\r\n,，;；]{0,20}[:=：][^\r\n,，;；}]+", "[redacted]")
    $value = [regex]::Replace($value, "(?<![A-Za-z0-9])[A-Za-z0-9]{4,}#", "[redacted]")
    $value = [regex]::Replace($value, "\b[a-fA-F0-9]{32,}\b", "[redacted]")
    return $value
}

function ConvertTo-QaSanitizedValue {
    param(
        [AllowNull()][object]$Value,
        [string]$PropertyName = ""
    )

    if ($null -eq $Value) {
        return $null
    }

    $sensitiveName = $PropertyName -match "(?i)(hash|signature|token|secret|password|credential|phone|手机号|密码|签名)"
    if ($Value -is [string]) {
        if ($sensitiveName -and $Value) {
            return "[redacted]"
        }
        return Protect-QaSensitiveText $Value
    }

    if ($Value -is [bool] -or $Value -is [ValueType]) {
        return $Value
    }

    if ($Value -is [System.Collections.IDictionary]) {
        $result = [ordered]@{}
        foreach ($key in $Value.Keys) {
            $keyText = "$key"
            $result[$keyText] = ConvertTo-QaSanitizedValue -Value $Value[$key] -PropertyName $keyText
        }
        return $result
    }

    if ($Value -is [System.Collections.IEnumerable]) {
        $items = @()
        foreach ($item in $Value) {
            $items += ConvertTo-QaSanitizedValue -Value $item
        }
        return @($items)
    }

    $properties = @($Value.PSObject.Properties | Where-Object { $_.MemberType -in @("NoteProperty", "Property") })
    if ($properties.Count -gt 0) {
        $result = [ordered]@{}
        foreach ($property in $properties) {
            $result[$property.Name] = ConvertTo-QaSanitizedValue -Value $property.Value -PropertyName $property.Name
        }
        return $result
    }

    return Protect-QaSensitiveText $Value
}

function Assert-QaTextSanitized {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$GateName
    )

    if (-not (Test-Path $Path -PathType Leaf)) {
        throw "${GateName}: sanitized file not found: $Path"
    }
    $text = Get-Content -Raw -Encoding UTF8 $Path
    $findings = New-Object System.Collections.Generic.List[string]
    if ($text -match "(?<!\d)1[3-9]\d{9}(?!\d)") {
        $findings.Add("phone")
    }
    if ($text -match "(?i)\b(sk-(proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AKIA[0-9A-Z]{16})\b") {
        $findings.Add("token")
    }
    if ($text -match "(?<![A-Za-z0-9])[A-Za-z0-9]{4,}#") {
        $findings.Add("password")
    }
    if ($text -match "\b[a-fA-F0-9]{32,}\b") {
        $findings.Add("long_hash")
    }
    if ($findings.Count -gt 0) {
        throw "${GateName}: unsanitized QA content detected ($($findings -join ', ')): $Path"
    }
}

function Save-SanitizedQaArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)][string]$GateName
    )

    if (-not (Test-Path $ArtifactPath -PathType Leaf)) {
        throw "${GateName}: QA artifact not found: $ArtifactPath"
    }
    $payload = Get-Content -Raw -Encoding UTF8 $ArtifactPath | ConvertFrom-Json
    $sanitized = ConvertTo-QaSanitizedValue -Value $payload
    $json = $sanitized | ConvertTo-Json -Depth 100
    Set-Content -Path $ArtifactPath -Value ($json + "`n") -Encoding UTF8
    Assert-QaTextSanitized -Path $ArtifactPath -GateName $GateName
}

function ConvertTo-NativeProcessArgument {
    param([AllowNull()][string]$Argument)

    if ($null -eq $Argument -or $Argument -eq "") {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }
    $result = '"'
    $backslashes = 0
    foreach ($char in $Argument.ToCharArray()) {
        if ($char -eq '\') {
            $backslashes += 1
            continue
        }
        if ($char -eq '"') {
            $result += ('\' * (($backslashes * 2) + 1))
            $result += '"'
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            $result += ('\' * $backslashes)
            $backslashes = 0
        }
        $result += $char
    }
    if ($backslashes -gt 0) {
        $result += ('\' * ($backslashes * 2))
    }
    $result += '"'
    return $result
}

function Split-ProcessOutputLines {
    param([AllowNull()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) {
        return @()
    }
    return @($Text -split "`r?`n" | Where-Object { $_ -ne "" })
}

function Invoke-ExternalCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$RedactOutput
    )

    $stdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) ("rag-v2-gate-stdout-{0}.log" -f [guid]::NewGuid().ToString("N"))
    $stderrPath = Join-Path ([System.IO.Path]::GetTempPath()) ("rag-v2-gate-stderr-{0}.log" -f [guid]::NewGuid().ToString("N"))
    $argumentString = (($Arguments | ForEach-Object { ConvertTo-NativeProcessArgument $_ }) -join " ")
    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $argumentString `
            -WorkingDirectory ([string]$RepoRoot) `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        $stdout = ""
        $stderr = ""
        if (Test-Path $stdoutPath -PathType Leaf) {
            $stdout = Get-Content -Raw -Encoding UTF8 $stdoutPath
        }
        if (Test-Path $stderrPath -PathType Leaf) {
            $stderr = Get-Content -Raw -Encoding UTF8 $stderrPath
        }
        $output = @()
        $output += Split-ProcessOutputLines $stdout
        $output += Split-ProcessOutputLines $stderr
        $exitCode = $process.ExitCode
    } finally {
        Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
    foreach ($line in $output) {
        $lineText = "$line"
        if ($RedactOutput) {
            $lineText = Protect-QaSensitiveText $lineText
        }
        Write-Host $lineText
    }
    return @{
        ExitCode = [int]$exitCode
        Output = @($output | ForEach-Object { "$_" })
    }
}

function Invoke-PytestFiles {
    param([Parameter(Mandatory = $true)][string[]]$TestFiles)

    $args = @("-m", "pytest", "-q") + $TestFiles
    Invoke-External -FilePath $Python -Arguments $args
}

function Add-ReleaseBlocker {
    param([Parameter(Mandatory = $true)][string]$Message)

    $script:ReleaseBlockers.Add($Message)
    Write-Host "RELEASE BLOCKER: $Message" -ForegroundColor Red
}

function Assert-NoReleaseBlockers {
    if ($script:ReleaseBlockers.Count -eq 0) {
        Write-Host "No release blockers recorded."
        return
    }
    foreach ($blocker in $script:ReleaseBlockers) {
        Write-Host "RELEASE BLOCKER: $blocker" -ForegroundColor Red
    }
    throw "L4 is blocked by $($script:ReleaseBlockers.Count) release blocker(s)."
}

function Get-QaArtifactPathFromOutput {
    param([Parameter(Mandatory = $true)][string[]]$Output)

    $artifact = ""
    foreach ($line in $Output) {
        $match = [regex]::Match($line, "^ARTIFACT\s+(.+)$")
        if ($match.Success) {
            $artifact = $match.Groups[1].Value.Trim()
        }
    }
    if (-not $artifact) {
        return ""
    }
    $path = $artifact
    if (-not [System.IO.Path]::IsPathRooted($path)) {
        $path = Join-Path $RepoRoot $path
    }
    return $path
}

function Assert-QaArtifactReleaseGate {
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)][string]$GateName,
        [switch]$RequireFullSuite
    )

    if (-not (Test-Path $ArtifactPath -PathType Leaf)) {
        throw "${GateName}: QA artifact not found: $ArtifactPath"
    }
    $payload = Get-Content -Raw -Encoding UTF8 $ArtifactPath | ConvertFrom-Json
    $quality = $payload.quality_status
    if ($null -eq $quality) {
        throw "${GateName}: QA artifact missing quality_status."
    }
    $qualityProperties = @($quality.PSObject.Properties.Name)
    foreach ($requiredProperty in @("passed", "high_count", "medium_count")) {
        if ($requiredProperty -notin $qualityProperties) {
            throw "${GateName}: QA artifact quality_status missing ${requiredProperty}: $ArtifactPath"
        }
    }
    $highCount = 0
    $highCount = [int]$quality.high_count
    $mediumCount = 0
    $mediumCount = [int]$quality.medium_count
    $passed = [bool]$quality.passed
    if (-not $passed -or $highCount -ne 0 -or $mediumCount -ne 0) {
        throw (
            "${GateName}: QA artifact blocks release; " +
            "passed=$passed high=$highCount medium=$mediumCount artifact=$ArtifactPath"
        )
    }
    if ($RequireFullSuite) {
        $summary = $payload.summary
        $usable = $false
        if ($null -ne $summary) {
            $usable = [bool]$summary.usable_for_release
        }
        if (-not [bool]$payload.full_suite_completed -or -not $usable) {
            throw "${GateName}: QA artifact is not a complete release transcript: $ArtifactPath"
        }
    }
    Write-Host "${GateName}: QA artifact gate passed high=0 medium=0 artifact=$ArtifactPath"
}

function Invoke-QAArtifactRunner {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$RequireFullSuite
    )

    $result = Invoke-ExternalCapture -FilePath $Python -Arguments $Arguments
    $artifactPath = Get-QaArtifactPathFromOutput -Output $result.Output
    if ($artifactPath) {
        Assert-QaArtifactReleaseGate -ArtifactPath $artifactPath -GateName $Name -RequireFullSuite:$RequireFullSuite
    } else {
        throw "${Name}: runner did not print an ARTIFACT line."
    }
    if ($result.ExitCode -ne 0) {
        throw "Command failed with exit code $($result.ExitCode): $Python $($Arguments -join ' ')"
    }
}

function Invoke-QAArtifactRunnerSanitized {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$RequireFullSuite
    )

    $result = Invoke-ExternalCapture -FilePath $Python -Arguments $Arguments -RedactOutput
    $artifactPath = Get-QaArtifactPathFromOutput -Output $result.Output
    if (-not $artifactPath) {
        throw "${Name}: runner did not print an ARTIFACT line."
    }
    Save-SanitizedQaArtifact -ArtifactPath $artifactPath -GateName $Name
    Assert-QaArtifactReleaseGate -ArtifactPath $artifactPath -GateName $Name -RequireFullSuite:$RequireFullSuite
    if ($result.ExitCode -ne 0) {
        throw "Command failed with exit code $($result.ExitCode): $Python $($Arguments -join ' ')"
    }
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

    $isTestFixture = $normalized.StartsWith("tests/fixtures/")
    if (($isEnvExample -or $isTestFixture) -and ($value -match "^(your|missing|dummy|fake|test|example|placeholder)[_-][A-Za-z0-9_-]+$")) {
        return $true
    }

    if ($value -match "^\{[A-Za-z_][A-Za-z0-9_]*\}(&|$)") {
        return $true
    }

    if ($normalized.StartsWith("tests/") -and ($value -in @("previous_secret", "previous_key"))) {
        return $true
    }

    if ($normalized.StartsWith("tests/") -and ($value -match "^(synthetic|test|fake|dummy|canary|example|placeholder)[_-][A-Za-z0-9_#=&.-]+$")) {
        return $true
    }

    if ($normalized.StartsWith("tests/") -and ($value -match "(?i)(synthetic|test|fake|dummy|canary|example|placeholder)")) {
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
        Add-ReleaseBlocker "SkipParity enabled; 20+ parity QA was skipped, so production cutover is blocked."
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
        Invoke-QAArtifactRunner -Name "parity QA $runner" -Arguments @($runner) -RequireFullSuite
    }
    Write-Host "Parity target satisfied: $ParityCases+ cases via two 10-window runners."
}

function Invoke-ProductionSmoke {
    $previousAppEnv = $env:APP_ENV
    $previousMode = $env:KF_DUAL_LLM_MODE
    try {
        $env:APP_ENV = "test"
        $env:KF_DUAL_LLM_MODE = "production"
        Invoke-External -FilePath $Python -Arguments @("scripts/smoke_dual_llm_production.py")
    } finally {
        if ($null -eq $previousAppEnv) {
            Remove-Item Env:\APP_ENV -ErrorAction SilentlyContinue
        } else {
            $env:APP_ENV = $previousAppEnv
        }
        if ($null -eq $previousMode) {
            Remove-Item Env:\KF_DUAL_LLM_MODE -ErrorAction SilentlyContinue
        } else {
            $env:KF_DUAL_LLM_MODE = $previousMode
        }
    }
}

function Invoke-RealDialogueReplay {
    $fixture = "tests/fixtures/qa/real_server_dialogues_sanitized.json"
    if (-not (Test-Path $fixture -PathType Leaf)) {
        if ($AllowMissingRealDialogues) {
            Add-ReleaseBlocker "AllowMissingRealDialogues enabled; real dialogue replay fixture is missing, so production cutover is blocked."
            return
        }
        throw "L4 requires $fixture. Generate it with scripts/export_real_dialogue_fixture.py or pass -AllowMissingRealDialogues explicitly."
    }

    Invoke-QAArtifactRunner -Name "real dialogue replay QA" -RequireFullSuite -Arguments @(
        "qa_artifacts/run_rag_10windows_10turns_utf8.py",
        "--fixture",
        $fixture,
        "--artifact-prefix",
        "rag_real_dialogue_replay_utf8",
        "--min-window-count",
        "10",
        "--min-turn-count",
        "100"
    )
}

function Invoke-HistoricalFailureReplay {
    $fixture = Resolve-QaGatePath $HistoricalFailuresFixture
    if (-not (Test-Path $fixture -PathType Leaf)) {
        if ($AllowMissingHistoricalFailures) {
            Add-ReleaseBlocker "AllowMissingHistoricalFailures enabled; historical failure replay fixture is missing, so production cutover is blocked."
            return
        }
        throw "L4 requires $fixture. Add a sanitized historical failure fixture or pass -AllowMissingHistoricalFailures explicitly."
    }
    Assert-QaTextSanitized -Path $fixture -GateName "historical failure replay fixture"

    if ($HistoricalFailuresArtifact) {
        $artifact = Resolve-QaGatePath $HistoricalFailuresArtifact
        Save-SanitizedQaArtifact -ArtifactPath $artifact -GateName "historical failure replay QA"
        Assert-QaArtifactReleaseGate -ArtifactPath $artifact -GateName "historical failure replay QA" -RequireFullSuite
        return
    }

    Invoke-QAArtifactRunnerSanitized -Name "historical failure replay QA" -RequireFullSuite -Arguments @(
        "qa_artifacts/run_rag_10windows_10turns_utf8.py",
        "--fixture",
        $fixture,
        "--artifact-prefix",
        "rag_historical_failure_replay_sanitized",
        "--min-window-count",
        "3",
        "--min-turn-count",
        "12"
    )
}

function Invoke-RandomGuard {
    Invoke-QAArtifactRunner -Name "random 10-question QA graph" -Arguments @("qa_artifacts/run_kf_qa_gate_graph_utf8.py") -RequireFullSuite
}

function Invoke-ReleaseRehearsal {
    $rehearsalRoot = Join-Path $RepoRoot ("qa_artifacts\release_rehearsal_l4_" + [guid]::NewGuid().ToString("N"))
    Invoke-External -FilePath $Python -Arguments @(
        "scripts/rehearse_release_pipeline.py",
        "--project-dir",
        [string]$RepoRoot,
        "--rehearsal-root",
        $rehearsalRoot,
        "--version",
        "l4-local-rehearsal",
        "--legacy-rehearsal"
    )
    $reportPath = Join-Path $rehearsalRoot "release_rehearsal_report.json"
    if (-not (Test-Path $reportPath -PathType Leaf)) {
        throw "Release rehearsal report missing: $reportPath"
    }
    $report = Get-Content -Raw -Encoding UTF8 $reportPath | ConvertFrom-Json
    if (-not [bool]$report.ok) {
        throw "Release rehearsal report is not ok: $reportPath"
    }
    if (-not [bool]$report.rollback.ok) {
        throw "Rollback rehearsal failed: $reportPath"
    }
    if ($report.current_pointer.version -ne "previous-good") {
        throw "Rollback rehearsal did not restore current pointer to previous-good."
    }
    if (-not [bool]$report.health_contract.ok) {
        throw "Health contract rehearsal failed: $reportPath"
    }
    if ([bool]$report.unattended_env_summary.secret_values_printed) {
        throw "Release rehearsal attempted to print secret values."
    }
    Write-Host "Release rehearsal report: $reportPath"
}

$L1Tests = @(
    "tests/test_kf_contracts.py",
    "tests/test_kf_send_receipts.py",
    "tests/test_media_manifest.py"
)

$L2Tests = @(
    "tests/test_wecom_kf.py",
    "tests/test_kf_send_receipt_faults.py",
    "tests/test_kf_agentic_rag.py",
    "tests/test_kf_dual_llm_shadow.py",
    "tests/test_kf_dual_llm_production.py",
    "tests/test_llm.py",
    "tests/test_media_store.py",
    "tests/test_media_manifest.py",
    "tests/test_inventory_query.py",
    "tests/test_inventory_read_router.py",
    "tests/test_inventory_sensitive_access.py",
    "tests/test_qa_utf8_inputs.py"
)

$L4RollbackTests = @(
    "tests/test_inventory_snapshot.py",
    "tests/test_inventory_snapshot_m1d2b2.py",
    "tests/test_release_pipeline.py"
)

Write-Host "RAG V2 QA Fast Gates"
Write-Host "Repo: $RepoRoot"
Write-Host "Level: $Level"
Write-Host "Python: $Python"
Write-Host "RUN_ONLINE_QA=$env:RUN_ONLINE_QA"

if ($RunHistoricalFailureGateOnly) {
    Invoke-GateStep "historical failure replay QA" { Invoke-HistoricalFailureReplay }
    Invoke-GateStep "release blocker audit" { Assert-NoReleaseBlockers }
    exit 0
}

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
    Invoke-GateStep "dual LLM production package smoke without send" { Invoke-ProductionSmoke }
    Invoke-GateStep "20+ parity QA" { Invoke-Parity20 }
    Invoke-GateStep "real dialogue replay QA" { Invoke-RealDialogueReplay }
    Invoke-GateStep "historical failure replay QA" { Invoke-HistoricalFailureReplay }
    Invoke-GateStep "random 10-question QA graph" { Invoke-RandomGuard }
    Invoke-GateStep "video upload transcode retry gate" { Invoke-PytestFiles @("tests/test_kf_send_receipt_faults.py") }
    Invoke-GateStep "rollback, cutover and release rehearsal tests" { Invoke-PytestFiles $L4RollbackTests }
    Invoke-GateStep "release/current rehearsal local artifact" { Invoke-ReleaseRehearsal }
    Invoke-GateStep "secret scan" { Invoke-SecretScan }
    Invoke-GateStep "compileall app" { Invoke-External -FilePath $Python -Arguments @("-m", "compileall", "app") }
    Invoke-GateStep "git diff --check" { Invoke-External -FilePath "git" -Arguments @("diff", "--check") }
    Invoke-GateStep "release blocker audit" { Assert-NoReleaseBlockers }
    exit 0
}
