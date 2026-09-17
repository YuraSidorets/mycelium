param(
    [Parameter(Mandatory=$true, Position=0)][string]$Goal,
    [Parameter(Mandatory=$true, Position=1)][string]$NodeId,
    [Parameter(Mandatory=$true, Position=2)]
    [ValidateSet("apex", "septum", "hyphae", "stem", "cap")]
    [string]$Role,
    [Parameter(Mandatory=$true, Position=3)]
    [ValidateSet("alive", "blocked", "complete")]
    [string]$Status,
    [Parameter(Position=4)][string]$Facts = "",
    [Parameter(Position=5)][string]$Questions = "",
    [Parameter(Position=6)][string]$Next = "",
    [string]$Topics = "",
    [string]$Evidence = "",
    [ValidateSet("", "low", "medium", "high")]
    [string]$Confidence = "",
    [string]$Consumes = "",
    [string]$Blocks = "",
    [string]$Trace = "",
    [string]$Reflection = "",
    [string]$ParentGoalId = "",
    [string]$ProducingAgent = "",
    [string]$Version = "1",
    [string]$SourceRefs = "",
    [string]$DependsOn = "",
    [bool]$Invalidated = $false,
    [string]$Supersedes = "",
    [string]$Command = "",
    [string]$Expect = "",
    [string]$RunId = "",
    [string]$AllowUntracked = "",
    [string[]]$ProcessInputs = @()
)

function Resolve-LineagePython {
    $Candidates = @()
    foreach ($Name in @("python", "python3")) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            $Candidates += [pscustomobject]@{ Executable = $Command.Source; Prefix = @() }
        }
    }
    $Py = Get-Command py -ErrorAction SilentlyContinue
    if ($Py) {
        $Candidates += [pscustomobject]@{ Executable = $Py.Source; Prefix = @("-3") }
    }

    foreach ($Candidate in $Candidates) {
        $PreviousPreference = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        try {
            & $Candidate.Executable @($Candidate.Prefix) -c `
                'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) { return $Candidate }
        }
        finally {
            $ErrorActionPreference = $PreviousPreference
        }
    }
    throw "Tracked node writes require Python 3.9 or newer (python, python3, or py -3); no node or flow was written."
}

function Invoke-LineageJson {
    param(
        [Parameter(Mandatory=$true)]$Python,
        [Parameter(Mandatory=$true)][string]$LineageScript,
        [Parameter(Mandatory=$true)][string[]]$Arguments
    )
    $PreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
    $env:PYTHONDONTWRITEBYTECODE = "1"
    try {
        $Output = (& $Python.Executable @($Python.Prefix) $LineageScript @Arguments 2>&1) -join "`n"
        if ($LASTEXITCODE -ne 0) {
            if ($Output) { throw $Output }
            throw "Mycelium lineage command failed."
        }
        return $Output | ConvertFrom-Json
    }
    finally {
        if ($null -eq $PreviousBytecode) {
            Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
        }
        else {
            $env:PYTHONDONTWRITEBYTECODE = $PreviousBytecode
        }
    }
}

# Tracked is the default. The untracked path still exists, but it has to be
# asked for by name and the stated reason lands in the node itself, so a bypass
# is greppable instead of silent.
if (-not $RunId -and [string]::IsNullOrWhiteSpace($AllowUntracked)) {
    [Console]::Error.WriteLine('node writes are tracked by default; pass -RunId <id> or -AllowUntracked "<reason>"')
    exit 2
}
if ($RunId -and -not [string]::IsNullOrWhiteSpace($AllowUntracked)) {
    [Console]::Error.WriteLine("-RunId and -AllowUntracked are mutually exclusive; a tracked write needs no reason")
    exit 2
}

if ($RunId -and -not $PSBoundParameters.ContainsKey("Version")) {
    $Version = "2"
}

$slug = ($Goal.ToLowerInvariant() -replace "[^a-z0-9]+", "-").Trim("-")
if (-not $slug) { $slug = "goal" }
$safeNode = ($NodeId -replace "[^A-Za-z0-9_.-]+", "-").Trim("-")
if (-not $safeNode) { throw "NodeId must contain at least one safe character" }

if ($Status -eq "complete") {
    $required = [ordered]@{
        Facts = $Facts
        Topics = $Topics
        Evidence = $Evidence
        Confidence = $Confidence
        Consumes = $Consumes
        Questions = $Questions
        Blocks = $Blocks
        Next = $Next
    }
    $missing = @($required.GetEnumerator() | Where-Object { [string]::IsNullOrWhiteSpace("$($_.Value)") } | ForEach-Object Key)
    if ($missing) { throw "Complete node missing required fields: $($missing -join ', '). Use 'none' when a field is intentionally empty." }
}

# A Trace that claims a spawn must cost the same fields a real one carries. This
# still cannot prove an agent ran; it stops a spawn claim that carries nothing.
if (-not [string]::IsNullOrWhiteSpace($Trace) -and
    ($Trace -cmatch "collaboration\.spawn_agent" -or $Trace -cmatch "\bAgent\b")) {
    $spawnIncomplete = [string]::IsNullOrWhiteSpace($ProducingAgent)
    foreach ($segment in @("Action:", "Observation:", "Result:")) {
        if ($Trace -cnotmatch [regex]::Escape($segment)) { $spawnIncomplete = $true }
    }
    if ($spawnIncomplete) {
        [Console]::Error.WriteLine("Trace names a spawn tool; ProducingAgent and Action/Observation/Result are required.")
        exit 2
    }
}

$frontmatter = [ordered]@{
    Goal = $Goal
    Topics = $Topics
    Evidence = $Evidence
    Consumes = $Consumes
    Blocks = $Blocks
    Trace = $Trace
    Reflection = $Reflection
    ParentGoalId = $ParentGoalId
    ProducingAgent = $ProducingAgent
    Version = $Version
    SourceRefs = $SourceRefs
    DependsOn = $DependsOn
    Supersedes = $Supersedes
    Command = $Command
    Expect = $Expect
    UntrackedReason = $AllowUntracked
}
$multiline = @($frontmatter.GetEnumerator() | Where-Object { "$($_.Value)" -match "[`r`n]" } | ForEach-Object Key)
if ($multiline) { throw "Frontmatter fields must be a single line: $($multiline -join ', ')" }

$flowProjection = [ordered]@{
    goal = $Goal
    nodeId = $safeNode
    role = $Role
    status = $Status
    topics = $Topics
    evidence = $Evidence
    confidence = $Confidence
    consumes = $Consumes
    blocks = $Blocks
    trace = $Trace
    reflection = $Reflection
    parentGoalId = $ParentGoalId
    producingAgent = $ProducingAgent
    version = $Version
    sourceRefs = $SourceRefs
    dependsOn = $DependsOn
    invalidated = $Invalidated
    supersedes = $Supersedes
    command = $Command
    expect = $Expect
    untracked_reason = $AllowUntracked
    facts = @($Facts | Where-Object { $_ })
    questions = @($Questions | Where-Object { $_ })
    next = @($Next | Where-Object { $_ })
}

$workspace = (Get-Location).Path
$lineageReservation = $null
$lineagePython = $null
$lineageScript = Join-Path $PSScriptRoot "mycelium_lineage.py"
if ($RunId) {
    if (-not (Test-Path -LiteralPath $lineageScript -PathType Leaf)) {
        throw "Tracked node writes require $lineageScript; no node or flow was written."
    }
    $lineagePython = Resolve-LineagePython
    Invoke-LineageJson $lineagePython $lineageScript @("--root", $workspace, "preflight") | Out-Null
    $reserveArguments = @(
        "--root", $workspace, "reserve",
        "--run-id", $RunId,
        "--node-id", $safeNode,
        "--role", $Role
    )
    foreach ($ProcessInput in $ProcessInputs) {
        $reserveArguments += @("--process-input", $ProcessInput)
    }
    $reserveArguments += @("--projection-json", ($flowProjection | ConvertTo-Json -Compress))
    $lineageReservation = Invoke-LineageJson $lineagePython $lineageScript $reserveArguments
    if ($lineageReservation.status -eq "committed") {
        $lineageReservation.commit.flow.path
        return
    }
}

$flowDir = Join-Path $workspace ".mycelium/flows"
$nodeDir = Join-Path $workspace ".mycelium/nodes"
New-Item -ItemType Directory -Path $flowDir, $nodeDir -Force | Out-Null

$record = [ordered]@{ timestamp = (Get-Date).ToUniversalTime().ToString("o") }
foreach ($Field in $flowProjection.GetEnumerator()) {
    $record[$Field.Key] = $Field.Value
}

$flowRelativePath = ".mycelium/flows/$slug.jsonl"
$flowPath = Join-Path $flowDir ($slug + ".jsonl")
$nodePath = Join-Path $nodeDir ($safeNode + ".md")
$nodeText = @"
---
nodeId: $safeNode
goal: $Goal
role: $Role
status: $Status
topics: $Topics
confidence: $Confidence
consumes: $Consumes
blocks: $Blocks
parent_goal_id: $ParentGoalId
producing_agent: $ProducingAgent
version: $Version
source_refs: $SourceRefs
depends_on: $DependsOn
invalidated: $($Invalidated.ToString().ToLowerInvariant())
supersedes: $Supersedes
command: $Command
expect: $Expect
untracked_reason: $AllowUntracked
updated: $($record.timestamp)
---

# $safeNode

Goal: $Goal
Role: $Role
Status: $Status
Topics: $Topics
Evidence: $Evidence
Confidence: $Confidence
Consumes: $Consumes
Blocks: $Blocks
Trace: $Trace
Reflection: $Reflection
Parent Goal ID: $ParentGoalId
Producing Agent: $ProducingAgent
Version: $Version
Source Refs: $SourceRefs
Depends On: $DependsOn
Invalidated: $($Invalidated.ToString().ToLowerInvariant())
Supersedes: $Supersedes
Command: $Command
Expect: $Expect
Untracked Reason: $AllowUntracked

## Facts
$Facts

## Questions
$Questions

## Next
$Next
"@
$temporaryNodePath = Join-Path $nodeDir (".$safeNode.$PID.tmp")
try {
    [IO.File]::WriteAllText($temporaryNodePath, $nodeText, [Text.UTF8Encoding]::new($false))
    # Readers publish a node only when this flow timestamp matches its atomic node projection.
    $recordJson = $record | ConvertTo-Json -Compress
    $recordJson | Add-Content -Encoding utf8 -Path $flowPath
    [IO.File]::Move($temporaryNodePath, $nodePath, $true)

    if ($RunId) {
        $flowLines = @(Get-Content -LiteralPath $flowPath)
        $flowLine = 0
        for ($Index = 0; $Index -lt $flowLines.Count; $Index++) {
            if ($flowLines[$Index] -eq $recordJson) {
                $flowLine = $Index + 1
            }
        }
        if (-not $flowLine) { throw "Written flow commit cannot be found: $flowRelativePath" }
        Invoke-LineageJson $lineagePython $lineageScript @(
            "--root", $workspace, "finalize",
            "--run-id", $RunId,
            "--reservation-token", [string]$lineageReservation.reservation_token,
            "--node-path", ".mycelium/nodes/$safeNode.md",
            "--flow-path", $flowRelativePath,
            "--flow-line", [string]$flowLine
        ) | Out-Null
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryNodePath) { Remove-Item -LiteralPath $temporaryNodePath -Force }
}

# Only mycelium-reflect regenerates the reflections digest, so a node that
# retires a reflection with -Supersedes left the digest advertising a failure
# mode that no longer applies until the next reflection was written. Regenerate
# it here too. This writes no stdout, so node output stays byte-identical with
# bin/mycelium.py. A superseded node whose file is absent is skipped silently.
$supersedesAReflection = $false
foreach ($targetId in ("$Supersedes" -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne "none" })) {
    $safeTarget = ($targetId -replace "[^A-Za-z0-9_.-]+", "-").Trim("-")
    if (-not $safeTarget) { continue }
    $targetPath = Join-Path $nodeDir ($safeTarget + ".md")
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) { continue }
    $targetMatch = [regex]::Match((Get-Content -Raw -LiteralPath $targetPath), '(?m)^topics:[ \t]*([^\r\n]*)\r?$')
    if (-not $targetMatch.Success) { continue }
    if (@($targetMatch.Groups[1].Value.ToLowerInvariant() -split "," | ForEach-Object { $_.Trim() }) -contains "reflection") {
        $supersedesAReflection = $true
        break
    }
}
if ($supersedesAReflection) {
    $digestScript = Join-Path $PSScriptRoot "mycelium-digest.ps1"
    if (Test-Path -LiteralPath $digestScript -PathType Leaf) { & $digestScript | Out-Null }
}

$flowRelativePath
