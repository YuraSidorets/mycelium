# Neutral SkillsBench workflow oracle. The generated evaluation image exposes
# this same script to both benchmark arms. It validates only outcomes under
# /app; the skill remains the sole paired difference.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet(
        "durable-roundtrip",
        "conflict-roundtrip",
        "reflection-roundtrip",
        "delegation-roundtrip",
        "selection-roundtrip",
        "tracked-lineage-roundtrip",
        "ephemeral-pipeline"
    )]
    [string]$Case,

    [string]$Workspace = "/app"
)

$ErrorActionPreference = "Stop"

function Resolve-CanonicalPath {
    param([string]$Path)
    if (-not $IsWindows) {
        $Resolved = ((& /usr/bin/realpath -- $Path 2>$null) -join "").TrimEnd("/")
        if ($LASTEXITCODE -ne 0 -or -not $Resolved) { throw "workflow validation failed" }
        return $Resolved
    }

    $ResolvedPath = @(Resolve-Path -LiteralPath $Path -ErrorAction Stop)
    if ($ResolvedPath.Count -ne 1) { throw "workflow validation failed" }
    return [IO.Path]::GetFullPath($ResolvedPath[0].ProviderPath).TrimEnd([char[]]"\/")
}

$WorkspaceRoot = Resolve-CanonicalPath $Workspace
$PathComparison = if ($IsWindows) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
$PathSeparator = [IO.Path]::DirectorySeparatorChar

function Resolve-ContainedPath {
    param(
        [string]$RelativePath,
        [ValidateSet("Leaf", "Container")]
        [string]$PathType
    )
    $Candidate = Join-Path $WorkspaceRoot $RelativePath
    $Resolved = Resolve-CanonicalPath $Candidate
    if (-not $Resolved.StartsWith($WorkspaceRoot + $PathSeparator, $PathComparison) -or
        -not (Test-Path -LiteralPath $Resolved -PathType $PathType)) {
        throw "workflow validation failed"
    }
    if ($IsWindows) {
        $Current = Get-Item -LiteralPath $Resolved -Force
        while ($Current) {
            if (($Current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "workflow validation failed"
            }
            $CurrentPath = [IO.Path]::GetFullPath($Current.FullName).TrimEnd([char[]]"\/")
            if ($CurrentPath.Equals($WorkspaceRoot, $PathComparison)) { break }
            $Current = if ($Current -is [IO.FileInfo]) { $Current.Directory } else { $Current.Parent }
        }
        if (-not $Current) { throw "workflow validation failed" }
    }
    return $Resolved
}

function Require-File {
    param([string]$RelativePath)
    return Resolve-ContainedPath $RelativePath "Leaf"
}

function Require-Match {
    param([string]$Text, [string]$Pattern)
    $Text = $Text.Replace("`r`n", "`n")
    if ($Text -notmatch $Pattern) { throw "workflow validation failed" }
}

function Require-NoMatch {
    param([string]$Text, [string]$Pattern)
    $Text = $Text.Replace("`r`n", "`n")
    if ($Text -match $Pattern) { throw "workflow validation failed" }
}

function Invoke-ExternalCapVerifier {
    param([string]$CapFile, [string]$RunId = "")
    $Verifier = Join-Path $PSScriptRoot "verify-cap.py"
    if (-not (Test-Path -LiteralPath $Verifier -PathType Leaf)) { throw "workflow validation failed" }
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) { $Python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $Python) { throw "workflow validation failed" }
    $VerifierArguments = @($Verifier, $Workspace, $CapFile)
    if ($RunId) { $VerifierArguments += @("--run-id", $RunId) }
    $Result = (& $Python.Source @VerifierArguments 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "workflow validation failed" }
    Require-Match $Result ([regex]::Escape("Verification: PASS"))
}

function Read-Json {
    param([string]$RelativePath)
    return Get-Content -Raw -LiteralPath (Require-File $RelativePath) | ConvertFrom-Json
}

function Read-JsonLines {
    param([string]$RelativePath)
    return @(
        Get-Content -LiteralPath (Require-File $RelativePath) |
            Where-Object { $_.Trim() } |
            ForEach-Object { $_ | ConvertFrom-Json }
    )
}

function Get-LatestLedger {
    $FlowDir = Resolve-ContainedPath ".mycelium/flows" "Container"
    $LedgerPath = Get-ChildItem -LiteralPath $FlowDir -Filter "stem-ledger-*.json" -File |
        Sort-Object LastWriteTimeUtc, Name |
        Select-Object -Last 1
    if (-not $LedgerPath) { throw "workflow validation failed" }
    $LedgerRelativePath = [IO.Path]::GetRelativePath($WorkspaceRoot, $LedgerPath.FullName)
    $ResolvedLedger = Resolve-ContainedPath $LedgerRelativePath "Leaf"
    return Get-Content -Raw -LiteralPath $ResolvedLedger | ConvertFrom-Json
}

function Require-NodeField {
    param([string]$Text, [string]$Name, [string]$Value)
    Require-Match $Text ("(?m)^" + [regex]::Escape($Name) + ":[ \t]*" + [regex]::Escape($Value) + "[ \t]*$")
}

try {
switch ($Case) {
    "durable-roundtrip" {
        $Node = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/release-proof.md")
        Require-NodeField $Node "nodeId" "release-proof"
        Require-NodeField $Node "goal" "release provenance"
        Require-NodeField $Node "role" "hyphae"
        Require-NodeField $Node "status" "complete"
        Require-NodeField $Node "topics" "release provenance"
        Require-NodeField $Node "confidence" "high"
        Require-NodeField $Node "parent_goal_id" "release provenance"
        Require-NodeField $Node "producing_agent" "apex-release"
        Require-NodeField $Node "source_refs" "evidence/release.json:12"
        Require-Match $Node '(?m)^release candidate is green[ \t]*$'

        $Flow = Read-JsonLines ".mycelium/flows/release-provenance.jsonl"
        $FlowNode = @($Flow | Where-Object nodeId -eq "release-proof")
        if ($FlowNode.Count -ne 1 -or $FlowNode[0].role -ne "hyphae" -or
            $FlowNode[0].status -ne "complete" -or
            $FlowNode[0].sourceRefs -ne "evidence/release.json:12" -or
            $FlowNode[0].producingAgent -ne "apex-release") {
            throw "workflow validation failed"
        }

        $Index = @(Read-Json ".mycelium/index.json")
        $IndexNode = @($Index | Where-Object nodeId -eq "release-proof")
        if ($IndexNode.Count -ne 1 -or
            $IndexNode[0].source_refs -ne "evidence/release.json:12" -or
            $IndexNode[0].firstFact -notmatch "release candidate is green") {
            throw "workflow validation failed"
        }

        $Search = Get-Content -Raw -LiteralPath (Require-File "search.txt")
        Require-Match $Search '(?m)^release-proof\thyphae\tcomplete\thigh\trelease provenance\trelease candidate is green(?:\t[^\r\n]*)?[ \t]*$'

        $Ledger = Get-LatestLedger
        if ($Ledger.goal -ne "release provenance" -or
            @($Ledger.selected_nodes).Count -ne 1 -or
            $Ledger.selected_nodes[0] -ne "release-proof" -or
            @($Ledger.conflicts).Count -ne 0 -or
            -not $Ledger.cap_dispatch) {
            throw "workflow validation failed"
        }

        $Cap = Get-Content -Raw -LiteralPath (Require-File "cap.txt")
        Require-Match $Cap ([regex]::Escape("Verification: EXTERNAL_REQUIRED"))
        Require-NoMatch $Cap ([regex]::Escape("Verification: PASS"))
        Require-Match $Cap ([regex]::Escape("[release-proof]"))
        Require-Match $Cap ([regex]::Escape(".mycelium/nodes/release-proof.md"))
        Invoke-ExternalCapVerifier (Require-File "cap.txt")
        "MYCELIUM_DURABLE_A17C"
    }

    "conflict-roundtrip" {
        foreach ($NodeId in @("endpoint-public", "endpoint-internal")) {
            $Node = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/$NodeId.md")
            Require-NodeField $Node "nodeId" $NodeId
            Require-NodeField $Node "goal" "service endpoint"
            Require-NodeField $Node "role" "hyphae"
            Require-NodeField $Node "status" "complete"
            Require-NodeField $Node "topics" "service endpoint"
            Require-NodeField $Node "confidence" "high"
        }
        $Public = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/endpoint-public.md")
        $Internal = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/endpoint-internal.md")
        Require-NodeField $Public "source_refs" "owners/public.md:8"
        Require-NodeField $Internal "source_refs" "owners/internal.md:11"
        Require-Match $Public '(?m)^service endpoint is 443[ \t]*$'
        Require-Match $Internal '(?m)^service endpoint is 8443[ \t]*$'

        $Flow = Read-JsonLines ".mycelium/flows/service-endpoint.jsonl"
        if (@($Flow | Where-Object nodeId -in @("endpoint-public", "endpoint-internal")).Count -ne 2) {
            throw "workflow validation failed"
        }

        $Ledger = Get-LatestLedger
        if ($Ledger.goal -ne "service endpoint" -or
            @($Ledger.selected_nodes).Count -ne 2 -or
            @($Ledger.conflicts).Count -ne 1) {
            throw "workflow validation failed"
        }
        $Conflict = [string]$Ledger.conflicts[0]
        Require-Match $Conflict "endpoint-public"
        Require-Match $Conflict "endpoint-internal"

        $Result = Read-Json "conflict-result.json"
        if ($Result.exit_code -ne 1) { throw "workflow validation failed" }
        Require-Match ([string]$Result.output) ([regex]::Escape("Verification: FAIL"))
        Require-Match ([string]$Result.output) ([regex]::Escape("Follow-up: return to STEM"))
        "MYCELIUM_CONFLICT_B28D"
    }

    "reflection-roundtrip" {
        $Original = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/cap-final.md")
        Require-NodeField $Original "nodeId" "cap-final"
        Require-NodeField $Original "goal" "release verification"
        Require-NodeField $Original "role" "cap"
        Require-NodeField $Original "status" "complete"
        Require-NodeField $Original "source_refs" "checks/integration.log:4"

        $Reflection = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/reflection-cap-final.md")
        Require-NodeField $Reflection "nodeId" "reflection-cap-final"
        Require-NodeField $Reflection "goal" "release verification"
        Require-NodeField $Reflection "role" "cap"
        Require-NodeField $Reflection "status" "blocked"
        Require-NodeField $Reflection "consumes" "cap-final"
        Require-NodeField $Reflection "blocks" "integration verification failed"
        Require-Match $Reflection '(?m)^Reflection:[ \t]*fix fixture and rerun verification[ \t]*$'
        Require-Match $Reflection '(?m)^integration verification failed[ \t]*$'
        Require-Match $Reflection '(?m)^fix fixture and rerun verification[ \t]*$'

        $Flow = Read-JsonLines ".mycelium/flows/release-verification.jsonl"
        $OriginalFlow = @($Flow | Where-Object nodeId -eq "cap-final")
        $ReflectionFlow = @($Flow | Where-Object nodeId -eq "reflection-cap-final")
        if ($OriginalFlow.Count -ne 1 -or $ReflectionFlow.Count -ne 1 -or
            $ReflectionFlow[0].consumes -ne "cap-final" -or
            $ReflectionFlow[0].blocks -ne "integration verification failed" -or
            $ReflectionFlow[0].reflection -ne "fix fixture and rerun verification") {
            throw "workflow validation failed"
        }
        "MYCELIUM_REFLECTION_C39E"
    }

    "delegation-roundtrip" {
        $Apex = Get-Content -Raw -LiteralPath (Require-File "apex-brief.txt")
        $Stem = Get-Content -Raw -LiteralPath (Require-File "stem-brief.txt")
        $Cap = Get-Content -Raw -LiteralPath (Require-File "cap-brief.txt")

        Require-Match $Apex ([regex]::Escape("You are mycelium APEX."))
        Require-Match $Apex ([regex]::Escape("Node ID: apex-apex-one"))
        Require-Match $Apex ([regex]::Escape("Producing Agent: apex-one"))
        Require-Match $Apex ([regex]::Escape('--producing-agent "apex-one"'))
        Require-Match $Apex ([regex]::Escape("Scripts cannot spawn host subagents."))

        Require-Match $Stem ([regex]::Escape("You are mycelium STEM."))
        Require-Match $Stem ([regex]::Escape("Node ID: stem-stem-main"))
        Require-Match $Stem ([regex]::Escape("Producing Agent: stem-main"))
        Require-Match $Stem ([regex]::Escape("Only one STEM coordinates the tracked run."))
        Require-Match $Stem ([regex]::Escape("same tracked run"))

        Require-Match $Cap ([regex]::Escape("You are mycelium CAP."))
        Require-Match $Cap ([regex]::Escape("Node ID: cap-cap-review"))
        Require-Match $Cap ([regex]::Escape("Producing Agent: cap-review"))
        Require-Match $Cap ([regex]::Escape("CAP is mandatory."))
        "MYCELIUM_DELEGATION_D4AF"
    }

    "selection-roundtrip" {
        $Expected = [ordered]@{
            "target-current" = @{ status = "complete"; invalidated = "false"; source = "platform/quartz-live.md:22" }
            "target-preview" = @{ status = "complete"; invalidated = "true"; source = "previews/quartz.md:3" }
            "target-blocked" = @{ status = "blocked"; invalidated = "false"; source = "issues/quartz.md:6" }
            "target-no-source" = @{ status = "complete"; invalidated = "false"; source = "" }
            "archive-noise" = @{ status = "complete"; invalidated = "false"; source = "archive/telemetry.log:1" }
        }
        foreach ($Entry in $Expected.GetEnumerator()) {
            $Node = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/$($Entry.Key).md")
            Require-NodeField $Node "nodeId" $Entry.Key
            Require-NodeField $Node "status" $Entry.Value.status
            Require-NodeField $Node "invalidated" $Entry.Value.invalidated
            Require-NodeField $Node "source_refs" $Entry.Value.source
        }

        $Index = @(Read-Json ".mycelium/index.json")
        foreach ($NodeId in $Expected.Keys) {
            if (@($Index | Where-Object nodeId -eq $NodeId).Count -ne 1) {
                throw "workflow validation failed"
            }
        }

        $Ledger = Get-LatestLedger
        if ($Ledger.goal -ne "quartz routing port" -or
            @($Ledger.selected_nodes).Count -ne 1 -or
            $Ledger.selected_nodes[0] -ne "target-current" -or
            @($Ledger.conflicts).Count -ne 0) {
            throw "workflow validation failed"
        }

        $Cap = Get-Content -Raw -LiteralPath (Require-File "selection-cap.txt")
        Require-Match $Cap ([regex]::Escape("Verification: EXTERNAL_REQUIRED"))
        Require-NoMatch $Cap ([regex]::Escape("Verification: PASS"))
        Require-Match $Cap ([regex]::Escape("[target-current]"))
        Require-Match $Cap ([regex]::Escape(".mycelium/nodes/target-current.md"))
        Require-NoMatch $Cap "target-preview|target-blocked|target-no-source|archive-noise"
        Invoke-ExternalCapVerifier (Require-File "selection-cap.txt")
        "MYCELIUM_SELECTION_E5B0"
    }

    "tracked-lineage-roundtrip" {
        $Manifest = Read-Json ".mycelium/runs/tracked-eval.json"
        if ($Manifest.schema -ne 1 -or
            $Manifest.run_id -ne "tracked-eval" -or
            $Manifest.goal -ne "lineage proof" -or
            $Manifest.state -ne "sealed") {
            throw "workflow validation failed"
        }
        $RoleStates = @($Manifest.roles)
        if ($RoleStates.Count -ne 5 -or @($RoleStates | Where-Object state -ne "performed").Count) {
            throw "workflow validation failed"
        }
        $ExpectedParents = [ordered]@{
            "tracked-a" = @()
            "tracked-s" = @("tracked-a")
            "tracked-h" = @("tracked-s")
            "tracked-stem" = @("tracked-h")
            "tracked-cap" = @("tracked-stem")
        }
        foreach ($Entry in $ExpectedParents.GetEnumerator()) {
            $Node = $Manifest.nodes.($Entry.Key)
            if (-not $Node -or $Node.status -ne "committed" -or
                (@($Node.process_inputs) -join ",") -ne (@($Entry.Value) -join ",")) {
                throw "workflow validation failed"
            }
        }

        $Cap = Get-Content -Raw -LiteralPath (Require-File "tracked-cap.txt")
        Require-Match $Cap ([regex]::Escape("Verification: EXTERNAL_REQUIRED"))
        Require-NoMatch $Cap ([regex]::Escape("Verification: PASS"))
        Require-Match $Cap ([regex]::Escape("[tracked-cap]"))
        Invoke-ExternalCapVerifier (Require-File "tracked-cap.txt") "tracked-eval"
        "MYCELIUM_TRACKED_G72A"
    }

    "ephemeral-pipeline" {
        $Facts = Get-Content -Raw -LiteralPath (Require-File "facts.md")
        $ExpectedFacts = @(
            "The dashboard listens on port 3300.",
            "The dashboard color is blue.",
            "The unrelated color is blue."
        )
        foreach ($ExpectedFact in $ExpectedFacts) {
            Require-Match $Facts ([regex]::Escape($ExpectedFact))
        }

        $Cap = Get-Content -Raw -LiteralPath (Require-File "ephemeral-cap.txt")
        Require-Match $Cap ([regex]::Escape("Verification: EXTERNAL_REQUIRED"))
        Require-NoMatch $Cap ([regex]::Escape("Verification: PASS"))
        Require-Match $Cap ([regex]::Escape("dashboard listens on port 3300"))
        Require-Match $Cap "facts\.md:\d+"
        Require-NoMatch $Cap "dashboard color is blue|unrelated color is blue"

        $Ledger = Get-LatestLedger
        if ($Ledger.goal -ne "dashboard port" -or @($Ledger.selected_nodes).Count -lt 1) {
            throw "workflow validation failed"
        }
        Require-Match ([string]$Ledger.selected_nodes[0]) "facts\.md:\d+"
        "MYCELIUM_PIPELINE_F6C1"
    }
}
}
catch {
    [Console]::Error.WriteLine("workflow validation failed")
    exit 1
}
