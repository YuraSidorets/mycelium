# Neutral SkillsBench oracle. The evaluation image exposes this same script to
# both the with-skill and no-skill arms; it validates artifacts under /app.
param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet("role", "durable", "conflict", "selection")]
    [string]$Case,

    [string]$Workspace = "/app"
)

$ErrorActionPreference = "Stop"

function Require-File {
    param([string]$RelativePath)
    $Path = Join-Path $Workspace $RelativePath
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "missing evaluation artifact: $RelativePath"
    }
    return $Path
}

function Require-Match {
    param([string]$Text, [string]$Pattern, [string]$Message)
    $Text = $Text.Replace("`r`n", "`n")
    if ($Text -notmatch $Pattern) { throw $Message }
}

function Require-NoMatch {
    param([string]$Text, [string]$Pattern, [string]$Message)
    $Text = $Text.Replace("`r`n", "`n")
    if ($Text -match $Pattern) { throw $Message }
}

function Invoke-ExternalCapVerifier {
    param([string]$CapFile)
    $Verifier = Join-Path $PSScriptRoot "verify-cap.py"
    if (-not (Test-Path -LiteralPath $Verifier -PathType Leaf)) { throw "missing external CAP verifier" }
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) { $Python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $Python) { throw "python is required for external CAP verification" }
    $Result = (& $Python.Source $Verifier $Workspace $CapFile 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "external CAP verification failed: $Result" }
    Require-Match $Result ([regex]::Escape("Verification: PASS")) "external verifier did not emit PASS"
}

switch ($Case) {
    "role" {
        $Apex = Get-Content -Raw -LiteralPath (Require-File "apex-brief.txt")
        $Stem = Get-Content -Raw -LiteralPath (Require-File "stem-brief.txt")
        $Cap = Get-Content -Raw -LiteralPath (Require-File "cap-brief.txt")
        $Workflow = Get-Content -Raw -LiteralPath (Require-File "workflow.json") | ConvertFrom-Json

        Require-Match $Apex ([regex]::Escape("Producing Agent: apex-one")) "APEX brief lost the named producing agent"
        Require-Match $Apex ([regex]::Escape('-ProducingAgent "apex-one"')) "APEX brief lost the producing-agent record argument"
        Require-Match $Stem ([regex]::Escape("Only one STEM")) "STEM brief lost the single-coordinator rule"
        Require-Match $Cap ([regex]::Escape("CAP is mandatory")) "CAP brief lost mandatory finalization"
        if ($Workflow.stem_instances -ne 1 -or $Workflow.cap_completed -ne $true -or $Workflow.producing_agent -ne "apex-one") {
            throw "workflow.json does not record one STEM, completed CAP, and apex-one provenance"
        }
        "MYCELIUM_ROLE_7D3A"
    }

    "durable" {
        $Node = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/node-omega.md")
        $Cap = Get-Content -Raw -LiteralPath (Require-File "cap-durable.txt")

        Require-Match $Node '(?m)^nodeId:[ \t]*node-omega[ \t]*$' "durable node ID is missing"
        Require-Match $Node '(?m)^source_refs:[ \t]*source\.md:7[ \t]*$' "durable node source_refs are missing"
        Require-Match $Node '(?m)^status:[ \t]*complete[ \t]*$' "durable node is not complete"
        Require-Match $Cap ([regex]::Escape("Verification: EXTERNAL_REQUIRED")) "durable CAP did not request external verification"
        Require-NoMatch $Cap ([regex]::Escape("Verification: PASS")) "durable CAP self-certified"
        Require-Match $Cap ([regex]::Escape("[node-omega]")) "durable CAP lost the Node ID"
        Require-Match $Cap ([regex]::Escape(".mycelium/nodes/node-omega.md")) "durable CAP lost the node path"
        Invoke-ExternalCapVerifier "cap-durable.txt"
        "MYCELIUM_DURABLE_91C4"
    }

    "conflict" {
        $Result = Get-Content -Raw -LiteralPath (Require-File "conflict-result.json") | ConvertFrom-Json
        if ($Result.exit_code -ne 1) { throw "conflicted CAP did not exit 1" }
        Require-Match ([string]$Result.output) ([regex]::Escape("Verification: FAIL")) "conflicted CAP did not fail verification"
        Require-Match ([string]$Result.output) ([regex]::Escape("Follow-up: return to STEM")) "conflicted CAP lost the STEM follow-up"
        "MYCELIUM_CONFLICT_2F8B"
    }

    "selection" {
        $Cap = Get-Content -Raw -LiteralPath (Require-File "cap-selection.txt")
        $Relevant = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/node-relevant.md")
        $Invalidated = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/node-invalidated.md")
        $Blocked = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/node-blocked.md")
        $Failed = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/node-failed.md")
        foreach ($Index in 1..6) {
            $Unrelated = Get-Content -Raw -LiteralPath (Require-File ".mycelium/nodes/node-unrelated-$Index.md")
            Require-Match $Unrelated "(?m)^nodeId:[ \t]*node-unrelated-$Index[ \t]*$" "unrelated fixture $Index has the wrong Node ID"
            Require-Match $Unrelated '(?m)^status:[ \t]*complete[ \t]*$' "unrelated fixture $Index is not complete"
            Require-NoMatch $Unrelated '(?mi)^goal:[ \t]*quartz routing[ \t]*$' "unrelated fixture $Index is relevant to the target goal"
            if ($Unrelated.Length -lt 4096) { throw "unrelated fixture $Index is not large" }
        }

        Require-Match $Relevant '(?m)^nodeId:[ \t]*node-relevant[ \t]*$' "selected fixture has the wrong Node ID"
        Require-Match $Relevant '(?m)^goal:[ \t]*quartz routing[ \t]*$' "selected fixture has the wrong goal"
        Require-Match $Relevant '(?m)^status:[ \t]*complete[ \t]*$' "selected fixture is not complete"
        Require-Match $Relevant '(?m)^source_refs:[ \t]*\S+' "selected node has empty source_refs"
        Require-Match $Invalidated '(?m)^nodeId:[ \t]*node-invalidated[ \t]*$' "invalidated fixture has the wrong Node ID"
        Require-Match $Invalidated '(?m)^invalidated:[ \t]*true[ \t]*$' "invalidated fixture is not invalidated"
        Require-Match $Blocked '(?m)^nodeId:[ \t]*node-blocked[ \t]*$' "blocked fixture has the wrong Node ID"
        Require-Match $Blocked '(?m)^status:[ \t]*blocked[ \t]*$' "blocked fixture is not blocked"
        Require-Match $Failed '(?m)^nodeId:[ \t]*node-failed[ \t]*$' "failed fixture has the wrong Node ID"
        Require-Match $Failed '(?m)^status:[ \t]*failed[ \t]*$' "failed fixture is not failed"
        Require-Match $Cap ([regex]::Escape("Verification: EXTERNAL_REQUIRED")) "selection CAP did not request external verification"
        Require-NoMatch $Cap ([regex]::Escape("Verification: PASS")) "selection CAP self-certified"
        Require-Match $Cap ([regex]::Escape("[node-relevant]")) "selection lost the relevant Node ID"
        Require-Match $Cap ([regex]::Escape(".mycelium/nodes/node-relevant.md")) "selection lost the relevant node path"
        Require-NoMatch $Cap "node-invalidated|node-blocked|node-failed|node-unrelated-" "ineligible node reached CAP"
        Invoke-ExternalCapVerifier "cap-selection.txt"
        "MYCELIUM_SELECTION_6E05"
    }
}
