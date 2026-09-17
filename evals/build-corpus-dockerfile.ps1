param(
    [Parameter(Mandatory)]
    [string]$SourceDockerfile,
    [Parameter(Mandatory)]
    [string]$CorpusManifest,
    [Parameter(Mandatory)]
    [string]$OutputDockerfile,
    [string]$CaseId = ""
)

$ErrorActionPreference = "Stop"
$Manifest = Get-Content -Raw -LiteralPath $CorpusManifest | ConvertFrom-Json
$Cases = @($Manifest.cases)
if ($CaseId) {
    $Cases = @($Cases | Where-Object id -eq $CaseId)
    if ($Cases.Count -ne 1) { throw "Unknown or duplicate corpus case: $CaseId" }
}

$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\")
$CorpusRoot = Join-Path $TempRoot "mycelium-benchmark-corpus-$PID"
if (-not $CorpusRoot.StartsWith($TempRoot + "\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe corpus staging path: $CorpusRoot"
}

function Write-Node {
    param(
        [string]$Path,
        [object]$Node
    )

    $Role = if ($Node.role) { $Node.role } else { "hyphae" }
    $Status = if ($Node.status) { $Node.status } else { "complete" }
    $Confidence = if ($Node.confidence) { $Node.confidence } else { "medium" }
    $Updated = if ($Node.updated) { $Node.updated } else { "2026-07-16T12:00:00Z" }
    $SourceRefs = if ($null -ne $Node.source_refs) { [string]$Node.source_refs } else { "" }
    $Invalidated = if ($Node.invalidated) { "true" } else { "false" }
    $Supersedes = if ($Node.supersedes) { $Node.supersedes } else { "" }
    $Version = if ($Node.version) { $Node.version } else { "1" }
    $Evidence = if ($Node.evidence) { $Node.evidence } else { $SourceRefs }
    $Text = @"
nodeId: $($Node.node_id)
role: $Role
status: $Status
topics: $($Node.topics)
confidence: $Confidence
updated: $Updated
parent_goal_id: $($Node.goal)
producing_agent: benchmark-fixture
version: $Version
source_refs: $SourceRefs
invalidated: $Invalidated
supersedes: $Supersedes
evidence: $Evidence

## Facts
- $($Node.fact)
"@
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

try {
    New-Item -ItemType Directory -Path $CorpusRoot | Out-Null
    foreach ($Case in $Cases) {
        $NodeDir = Join-Path $CorpusRoot "$($Case.id)/.mycelium/nodes"
        New-Item -ItemType Directory -Path $NodeDir | Out-Null
        foreach ($Node in @($Case.nodes)) {
            Write-Node -Path (Join-Path $NodeDir $Node.file) -Node $Node
        }
        for ($Index = 1; $Index -le $Case.noise_count; $Index++) {
            $Noise = [pscustomobject]@{
                node_id = "noise-$($Case.id)-$Index"
                role = @("apex", "septum", "hyphae")[$Index % 3]
                status = "complete"
                topics = "archive telemetry batch-$Index"
                confidence = @("low", "medium", "high")[$Index % 3]
                updated = "2026-07-15T$($Index.ToString('00')):00:00Z"
                goal = "unrelated archive telemetry"
                source_refs = "noise/$($Case.id)-$Index.log:1"
                invalidated = $false
                supersedes = ""
                version = "1"
                evidence = "noise/$($Case.id)-$Index.log:1"
                fact = "archive telemetry batch $Index is nominal"
            }
            Write-Node -Path (Join-Path $NodeDir "noise-$($Index.ToString('00')).md") -Node $Noise
        }
    }

    $Archive = Join-Path $TempRoot "mycelium-benchmark-corpus-$PID.zip"
    [IO.Compression.ZipFile]::CreateFromDirectory($CorpusRoot, $Archive)
    $Payload = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Archive))
    $Dockerfile = [IO.File]::ReadAllText($SourceDockerfile)
    $Marker = "WORKDIR /app"
    if (($Dockerfile.Split($Marker).Count - 1) -ne 1) {
        throw "Benchmark Dockerfile must contain one WORKDIR /app marker"
    }
    $InjectionLines = @(
        "RUN mkdir -p /opt/mycelium-eval/cases",
        "RUN : > /tmp/mycelium-corpus.b64"
    )
    for ($Offset = 0; $Offset -lt $Payload.Length; $Offset += 32000) {
        $Length = [Math]::Min(32000, $Payload.Length - $Offset)
        $Chunk = $Payload.Substring($Offset, $Length)
        $InjectionLines += "RUN printf '%s' '$Chunk' >> /tmp/mycelium-corpus.b64"
    }
    $InjectionLines += @(
        "RUN base64 -d /tmp/mycelium-corpus.b64 > /tmp/mycelium-corpus.zip \",
        "    && python3 -c `"import zipfile; zipfile.ZipFile('/tmp/mycelium-corpus.zip').extractall('/opt/mycelium-eval/cases')`" \",
        "    && rm /tmp/mycelium-corpus.b64 /tmp/mycelium-corpus.zip"
    )
    $Injection = ($InjectionLines -join "`n") + "`n`n"
    [IO.File]::WriteAllText(
        $OutputDockerfile,
        $Dockerfile.Replace($Marker, $Injection + $Marker),
        [Text.UTF8Encoding]::new($false)
    )
}
finally {
    if (Test-Path -LiteralPath $CorpusRoot) {
        Remove-Item -Recurse -Force -LiteralPath $CorpusRoot
    }
    if ($Archive -and (Test-Path -LiteralPath $Archive)) {
        Remove-Item -Force -LiteralPath $Archive
    }
}
