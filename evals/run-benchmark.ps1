param(
    [string]$JobsDir = ("jobs/skillsbench/v7-" + (Get-Date -Format "yyyyMMdd-HHmmss")),
    [string]$Model = "gpt-5.4[medium]",
    [ValidateRange(1, 16)]
    [int]$Concurrency = 4,
    [string]$CaseId = "",
    [string]$Distro = "Ubuntu-24.04"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$GitStatus = @(& git -C $Root status --porcelain --untracked-files=all 2>&1)
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect benchmark source tree" }
if ($GitStatus) { throw "Benchmark source tree must be clean" }
$SourceCommit = (& git -C $Root rev-parse HEAD 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0) { throw "Unable to resolve benchmark source commit" }
$SourceTree = (& git -C $Root rev-parse 'HEAD^{tree}' 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0) { throw "Unable to resolve benchmark source tree" }
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $Python) { throw "Python is required to stage the benchmark treatment" }
$JobsPath = if ([IO.Path]::IsPathRooted($JobsDir)) {
    [IO.Path]::GetFullPath($JobsDir)
}
else {
    [IO.Path]::GetFullPath((Join-Path $Root $JobsDir))
}

if (Test-Path -LiteralPath $JobsPath) {
    throw "Jobs directory already exists: $JobsPath"
}

$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\")
$Stage = Join-Path $TempRoot "mycelium-skillsbench-$PID"
if (-not $Stage.StartsWith($TempRoot + "\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe benchmark staging path: $Stage"
}

function ConvertTo-WslPath {
    param([string]$Path)

    $FullPath = [IO.Path]::GetFullPath($Path)
    if ($FullPath -notmatch '^([A-Za-z]):\\(.*)$') {
        throw "Benchmark paths must use a Windows drive letter: $FullPath"
    }
    return "/mnt/$($Matches[1].ToLowerInvariant())/" + $Matches[2].Replace("\", "/")
}

function Quote-Bash {
    param([string]$Value)

    return "'" + $Value.Replace("'", "'""'""'") + "'"
}

try {
    New-Item -ItemType Directory -Path $Stage | Out-Null
    $TreatmentJson = (& $Python.Source "$Root/evals/stage-treatment.py" `
        --source $Root `
        --destination $Stage `
        --manifest "$Root/evals/treatment-files.txt" `
        --commit $SourceCommit `
        --exact 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "Treatment staging failed: $TreatmentJson" }
    $Treatment = $TreatmentJson | ConvertFrom-Json
    $BenchmarkJson = (& $Python.Source "$Root/evals/stage-treatment.py" `
        --source $Root `
        --destination $Stage `
        --manifest "$Root/evals/benchmark-files.txt" `
        --commit $SourceCommit 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "Benchmark input staging failed: $BenchmarkJson" }
    $BenchmarkInput = $BenchmarkJson | ConvertFrom-Json

    $ManifestPath = Join-Path $Stage "evals/evals.json"
    $Manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
    if ($CaseId) {
        $Selected = @($Manifest.cases | Where-Object id -eq $CaseId)
        if ($Selected.Count -ne 1) { throw "Unknown or duplicate benchmark case: $CaseId" }
        $Manifest.cases = $Selected
        [IO.File]::WriteAllText(
            $ManifestPath,
            ($Manifest | ConvertTo-Json -Depth 20),
            [Text.UTF8Encoding]::new($false)
        )
    }

    & "$Stage/evals/build-workflow-dockerfile.ps1" `
        -SourceDockerfile "$Stage/evals/Dockerfile" `
        -Oracle "$Stage/evals/workflow-oracle.ps1" `
        -CapVerifier "$Stage/evals/verify-cap.py" `
        -LineageVerifier "$Stage/bin/mycelium_lineage.py" `
        -OutputDockerfile "$Stage/evals/Dockerfile"

    $AttestationPath = "$Stage/evals/treatment-attestation.json"
    [IO.File]::WriteAllText(
        $AttestationPath,
        ([ordered]@{
            source_commit = $SourceCommit.Trim()
            source_tree = $SourceTree.Trim()
            treatment = $Treatment
            benchmark_input = $BenchmarkInput
            final_eval_manifest_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ManifestPath).Hash.ToLowerInvariant()
            generated_dockerfile_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath "$Stage/evals/Dockerfile").Hash.ToLowerInvariant()
        } | ConvertTo-Json -Depth 10),
        [Text.UTF8Encoding]::new($false)
    )

    $StageWsl = Quote-Bash (ConvertTo-WslPath $Stage)
    $JobsWsl = Quote-Bash (ConvertTo-WslPath $JobsPath)
    $ModelArg = Quote-Bash $Model
    $Command = "source /opt/benchflow-venv/bin/activate && cd $StageWsl && " +
        "/usr/bin/time -p bench skills eval . --agent codex-acp --model $ModelArg " +
        "--sandbox docker --jobs-dir $JobsWsl --concurrency $Concurrency"

    & wsl.exe -d $Distro -- bash -lc $Command
    $BenchExitCode = $LASTEXITCODE

    if (Test-Path -LiteralPath $JobsPath -PathType Container) {
        Copy-Item -LiteralPath $AttestationPath -Destination "$JobsPath/treatment-attestation.json"
    }

    & "$Stage/evals/summarize-benchmark.ps1" `
        -JobsDir $JobsPath `
        -Manifest $ManifestPath
    $Summary = Get-Content -Raw -LiteralPath (Join-Path $JobsPath "mycelium-summary.json") |
        ConvertFrom-Json

    if (-not $Summary.pairing_valid) { throw "SkillsBench result is incomplete or failed an integrity check" }
    if ($BenchExitCode -ne 0) {
        Write-Warning (
            "SkillsBench returned exit code $BenchExitCode because one or more agent tasks " +
            "failed or timed out; the complete paired result was retained."
        )
    }
}
finally {
    if (Test-Path -LiteralPath $Stage) {
        Remove-Item -Recurse -Force -LiteralPath $Stage
    }
}
