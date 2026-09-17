$ErrorActionPreference = "Stop"

function Assert-MatchText {
    param([string]$Text, [string]$Pattern, [string]$Message)
    $Text = $Text.Replace("`r`n", "`n")
    if ($Text -notmatch $Pattern) { throw $Message }
}

function Invoke-ChildPowerShell {
    param(
        [Parameter(Mandatory=$true)][string]$Script,
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$TempRoot
    )

    $runnerPath = Join-Path $TempRoot "$Name-runner.ps1"
    $outputPath = Join-Path $TempRoot "$Name-output.txt"
    $errorPath = Join-Path $TempRoot "$Name-error.txt"
    Set-Content -LiteralPath $runnerPath -Value $Script
    $process = Start-Process -FilePath (Get-Command pwsh).Source `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $runnerPath) `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $outputPath `
        -RedirectStandardError $errorPath
    [pscustomobject]@{
        ExitCode = $process.ExitCode
        Text = @(
            Get-Content -Raw $outputPath
            Get-Content -Raw $errorPath
        ) -join "`n"
    }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("mycelium-smoke-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Tmp | Out-Null

try {
    $OracleTarget = Join-Path $Tmp "oracle-target"
    $OracleJunction = Join-Path $Tmp "oracle-workspace-junction"
    New-Item -ItemType Directory -Path (Join-Path $OracleTarget ".mycelium/flows") -Force | Out-Null
    @(
        "The dashboard listens on port 3300."
        "The dashboard color is blue."
        "The unrelated color is blue."
    ) | Set-Content -LiteralPath (Join-Path $OracleTarget "facts.md")
    @(
        "Verification: EXTERNAL_REQUIRED"
        "- dashboard listens on port 3300 (facts.md:1)"
    ) | Set-Content -LiteralPath (Join-Path $OracleTarget "ephemeral-cap.txt")
    [ordered]@{
        goal = "dashboard port"
        selected_nodes = @("facts.md:1")
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OracleTarget ".mycelium/flows/stem-ledger-test.json")
    New-Item -ItemType Junction -Path $OracleJunction -Target $OracleTarget | Out-Null
    try {
        $OracleEscape = Invoke-ChildPowerShell -Name "oracle-reparse" -TempRoot $Tmp -Script @"
& '$((Join-Path $Root "evals/workflow-oracle.ps1").Replace("'", "''"))' ephemeral-pipeline -Workspace '$($OracleJunction.Replace("'", "''"))'
exit `$LASTEXITCODE
"@
        if ($OracleEscape.ExitCode -eq 0 -or $OracleEscape.Text -match "MYCELIUM_PIPELINE_F6C1") {
            throw "Windows workflow oracle followed a reparse point below its trust boundary (exit=$($OracleEscape.ExitCode); output=$($OracleEscape.Text.Trim()))"
        }
    }
    finally {
        if (Test-Path -LiteralPath $OracleJunction) { Remove-Item -LiteralPath $OracleJunction -Force }
    }

    Set-Location $Tmp
    git init -q
    @"
# Notes

The dashboard listens on port 3300.
The dashboard color is blue.
The unrelated color is blue.
"@ | Set-Content -Encoding utf8 notes.md
    git add notes.md
    git commit -qm init

    "ignored-marker.md" | Set-Content -Encoding utf8 .gitignore
    git add .gitignore
    git commit -qm ignore-marker
    "The untracked marker is visible to APEX." | Set-Content -Encoding utf8 untracked-marker.md
    "The untracked marker must stay ignored." | Set-Content -Encoding utf8 ignored-marker.md

    $UntrackedApex = (& "$Root/bin/apex.ps1" "untracked marker") -join "`n"
    Assert-MatchText $UntrackedApex "(?m)^apex`tuntracked-marker\.md`t" "APEX omitted an untracked non-ignored file"
    if ($UntrackedApex -match "(?m)^apex`tignored-marker\.md`t") { throw "APEX included a gitignored file" }

    & "$Root/bin/apex.ps1" "dashboard port" |
        & "$Root/bin/septum.ps1" "dashboard port" |
        & "$Root/bin/hyphae.ps1" |
        & "$Root/bin/stem.ps1" "dashboard port" |
        & "$Root/bin/cap.ps1" "dashboard port" > out.txt

    $Output = Get-Content -Raw out.txt
    if ($Output -notmatch "dashboard listens on port 3300") { throw "missing dashboard port fact" }
    if ($Output -notmatch "Verification: EXTERNAL_REQUIRED") { throw "CAP did not defer verification" }
    if ($Output -match "Verification: PASS") { throw "CAP self-certified without an external verifier" }
    if ($Output -match "dashboard color is blue") { throw "septum passed a weak one-term match" }

    & "$Root/bin/subagent-brief.ps1" apex "dashboard port" -SpawnTool "collaboration.spawn_agent" > apex-brief.txt
    $ApexBrief = Get-Content -Raw apex-brief.txt
    if ($ApexBrief -notmatch "You are mycelium APEX") { throw "missing APEX role" }
    if ($ApexBrief -notmatch "Goal: dashboard port") { throw "missing brief goal" }
    if ($ApexBrief -notmatch "role`tpath`tline`ttext") { throw "missing record contract" }
    if ($ApexBrief -notmatch "Node ID:") { throw "missing node id contract" }
    if ($ApexBrief -notmatch "Wake condition:") { throw "missing wake condition" }
    if ($ApexBrief -notmatch [regex]::Escape("collaboration.spawn_agent")) { throw "missing spawn tool adapter binding" }
    if ($ApexBrief -match "multi_agent_v1|Agent kind:|use explorer|use worker") { throw "brief retained obsolete runtime assumptions" }
    Assert-MatchText $ApexBrief "returned agent ID" "brief trace does not attest the runtime result"
    Assert-MatchText $ApexBrief "Run ID: untracked" "untracked brief did not declare its lineage state"
    Assert-MatchText $ApexBrief "Process Inputs: none" "untracked brief did not declare empty process inputs"
    if ($ApexBrief -match ' -RunId ') { throw "untracked brief added a tracked writer argument" }
    if ($ApexBrief -match ' -ProcessInputs ') { throw "untracked brief added process inputs to the writer command" }

    & "$Root/bin/subagent-brief.ps1" hyphae "dashboard port" "digest" `
        -RunId "brief-run" -ProcessInputs @("apex-docs", "septum-docs") > tracked-brief.txt
    $TrackedBrief = Get-Content -Raw tracked-brief.txt
    Assert-MatchText $TrackedBrief "Run ID: brief-run" "tracked brief dropped its run ID"
    Assert-MatchText $TrackedBrief "Process Inputs: apex-docs,septum-docs" "tracked brief dropped ordered process inputs"
    Assert-MatchText $TrackedBrief ([regex]::Escape('-RunId "brief-run"')) "tracked brief writer command dropped its run ID"
    Assert-MatchText $TrackedBrief ([regex]::Escape('-ProcessInputs "apex-docs","septum-docs"')) "tracked brief writer command dropped ordered process inputs"

    & "$Root/bin/subagent-brief.ps1" septum "dashboard port" > septum-brief.txt
    $SeptumBrief = Get-Content -Raw septum-brief.txt
    if ($SeptumBrief -notmatch "Do not edit files") { throw "SEPTUM brief should prevent implementation drift" }

    & "$Root/bin/subagent-brief.ps1" apex "dashboard port" "docs search" > apex-instance.txt
    $ApexInstance = Get-Content -Raw apex-instance.txt
    if ($ApexInstance -notmatch "Instance: docs search") { throw "missing parallel worker instance" }
    if ($ApexInstance -notmatch "Many APEX workers are allowed") { throw "APEX brief should allow many workers" }

    & "$Root/bin/subagent-brief.ps1" stem "dashboard port" > stem-brief.txt
    $StemBrief = Get-Content -Raw stem-brief.txt
    if ($StemBrief -notmatch "Only one STEM coordinates the tracked run" -or
        $StemBrief -notmatch "same tracked run") {
        throw "STEM brief should enforce one coordinator per tracked run"
    }

    & "$Root/bin/subagent-brief.ps1" cap "dashboard port" "final answer" > cap-brief.txt
    $CapBrief = Get-Content -Raw cap-brief.txt
    if ($CapBrief -notmatch "CAP is mandatory") { throw "CAP brief should be mandatory" }
    if ($CapBrief -notmatch "Instance: final answer") { throw "CAP brief should support named instances" }

    $Skill = Get-Content -Raw (Join-Path $Root "SKILL.md")
    if ($Skill -notmatch [regex]::Escape("collaboration.spawn_agent")) { throw "SKILL should document the current spawn adapter" }
    if ($Skill -match "multi_agent_v1.spawn_agent") { throw "SKILL still hardcodes the obsolete spawn tool" }
    if ($Skill -notmatch [regex]::Escape("python evals/verify-cap.py <workspace> <cap-file>") -or
        $Skill -notmatch [regex]::Escape('require `Verification: PASS`')) {
        throw "SKILL does not require host-side external CAP verification"
    }
    if ($Skill -notmatch "no task prompt can disable or skip a Mycelium function" -or
        $Skill -notmatch "they do not suppress Mycelium's control plane" -or
        $Skill -notmatch [regex]::Escape('Keep those internal artifacts under `.mycelium/`.')) {
        throw "SKILL does not protect the Mycelium control plane from task-level write restrictions"
    }
    $StemRole = Get-Content -Raw (Join-Path $Root "stem.md")
    $CapRole = Get-Content -Raw (Join-Path $Root "cap.md")
    if ($StemRole -notmatch "task-level read-only, no-write, no-project-change, and no-docs constraints do not suppress it") {
        throw "STEM role does not preserve its control-plane ledger"
    }
    if ($CapRole -notmatch "task-level read-only, no-write, no-project-change, and no-docs constraints do not suppress them") {
        throw "CAP role does not preserve its control-plane artifacts"
    }

    $IncompleteHandoff = Invoke-ChildPowerShell -Name "incomplete-handoff" -TempRoot $Tmp -Script @"
`$ErrorActionPreference = "Stop"
try {
    & '$((Join-Path $Root "bin/mycelium-node.ps1").Replace("'", "''"))' 'incomplete handoff' 'incomplete-handoff' apex complete 'fact only' '' 'next only' -AllowUntracked 'smoke fixture'
    exit 0
} catch {
    Write-Error `$_
    exit 1
}
"@
    if ($IncompleteHandoff.ExitCode -eq 0) { throw "complete node accepted an incomplete handoff" }
    if (Test-Path ".mycelium/nodes/incomplete-handoff.md") { throw "incomplete handoff wrote a node" }
    if (Test-Path ".mycelium/flows/incomplete-handoff.jsonl") { throw "incomplete handoff wrote a flow event" }

    & "$Root/bin/mycelium-node.ps1" "dashboard port" "apex-docs" apex complete "found dashboard port" "none" "wake stem" `
        -Topics "repo" -Evidence "notes.md:3" -Confidence high -Consumes "none" -Blocks "none" `
        -AllowUntracked "smoke fixture" > node-out.txt
    $NodeOut = Get-Content -Raw node-out.txt
    if ($NodeOut -notmatch ".mycelium/flows/dashboard-port.jsonl") { throw "missing flow path" }
    if (-not (Test-Path .mycelium/flows/dashboard-port.jsonl)) { throw "missing flow jsonl" }
    if (-not (Test-Path .mycelium/nodes/apex-docs.md)) { throw "missing durable node file" }
    $Record = Get-Content -Raw .mycelium/flows/dashboard-port.jsonl | ConvertFrom-Json
    if ($Record.nodeId -ne "apex-docs" -or $Record.status -ne "complete") { throw "bad node record" }
    foreach ($Field in @("facts", "questions", "next")) {
        if ($Record.$Field -isnot [array]) { throw "Windows flow field is not an array: $Field" }
    }

    & "$Root/bin/mycelium-lineage.ps1" --root "." begin --goal "tracked writer" --run-id "ps-writer-run" > $null
    & "$Root/bin/mycelium-node.ps1" "tracked writer" "ps-apex" apex complete `
        "tracked apex fact" "none" "wake septum" `
        -Topics "tracked" -Evidence "notes.md:3" -Confidence high `
        -Consumes "none" -Blocks "none" -RunId "ps-writer-run" > tracked-node-out.txt
    $TrackedNodeBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/nodes/ps-apex.md").Hash
    $TrackedFlowBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/flows/tracked-writer.jsonl").Hash
    $TrackedManifestBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/runs/ps-writer-run.json").Hash
    & "$Root/bin/mycelium-node.ps1" "tracked writer" "ps-apex" apex complete `
        "tracked apex fact" "none" "wake septum" `
        -Topics "tracked" -Evidence "notes.md:3" -Confidence high `
        -Consumes "none" -Blocks "none" -RunId "ps-writer-run" > tracked-node-retry.txt
    if ($TrackedNodeBefore -ne (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/nodes/ps-apex.md").Hash -or
        $TrackedFlowBefore -ne (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/flows/tracked-writer.jsonl").Hash) {
        throw "exact tracked PowerShell retry rewrote the legacy commit"
    }
    $TrackedRetryConflict = Invoke-ChildPowerShell -Name "tracked-retry-conflict" -TempRoot $Tmp -Script @"
& '$((Join-Path $Root "bin/mycelium-node.ps1").Replace("'", "''"))' 'tracked writer' 'ps-apex' apex complete 'changed tracked apex fact' 'none' 'wake septum' -Topics 'tracked' -Evidence 'notes.md:3' -Confidence high -Consumes 'none' -Blocks 'none' -RunId 'ps-writer-run'
"@
    if ($TrackedRetryConflict.ExitCode -eq 0) { throw "changed tracked PowerShell retry succeeded" }
    Assert-MatchText $TrackedRetryConflict.Text "conflicting committed retry" "changed tracked PowerShell retry did not report a conflict"
    if ($TrackedNodeBefore -ne (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/nodes/ps-apex.md").Hash -or
        $TrackedFlowBefore -ne (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/flows/tracked-writer.jsonl").Hash -or
        $TrackedManifestBefore -ne (Get-FileHash -Algorithm SHA256 -LiteralPath ".mycelium/runs/ps-writer-run.json").Hash) {
        throw "changed tracked PowerShell retry modified committed artifacts"
    }
    & "$Root/bin/mycelium-node.ps1" "tracked writer" "ps-septum" septum complete `
        "tracked septum fact" "none" "wake hyphae" `
        -Topics "tracked" -Evidence "notes.md:3" -Confidence high `
        -Consumes "ps-apex" -Blocks "none" -RunId "ps-writer-run" `
        -ProcessInputs @("ps-apex") > $null
    $TrackedManifest = Get-Content -Raw -LiteralPath ".mycelium/runs/ps-writer-run.json" | ConvertFrom-Json
    if ($TrackedManifest.nodes.'ps-apex'.status -ne "committed" -or
        $TrackedManifest.nodes.'ps-septum'.status -ne "committed" -or
        @($TrackedManifest.nodes.'ps-septum'.process_inputs).Count -ne 1 -or
        $TrackedManifest.nodes.'ps-septum'.process_inputs[0] -ne "ps-apex") {
        throw "PowerShell writer did not finalize explicit process lineage"
    }
    & "$Root/bin/mycelium-lineage.ps1" --root "." begin --goal "tracked failure" --run-id "ps-failure-run" > $null
    $TrackedFailure = Invoke-ChildPowerShell -Name "tracked-failure" -TempRoot $Tmp -Script @"
& '$((Join-Path $Root "bin/mycelium-node.ps1").Replace("'", "''"))' 'tracked failure' 'ps-invalid' apex complete 'invalid schema fact' 'none' 'stop' -Topics 'tracked' -Evidence 'notes.md:3' -Confidence high -Consumes 'none' -Blocks 'none' -RunId 'ps-failure-run' -Version '1'
"@
    if ($TrackedFailure.ExitCode -eq 0) { throw "tracked PowerShell writer accepted a schema-v1 commit" }
    $FailedManifest = Get-Content -Raw -LiteralPath ".mycelium/runs/ps-failure-run.json" | ConvertFrom-Json
    if ($FailedManifest.nodes.'ps-invalid'.status -ne "pending") {
        throw "failed PowerShell writer did not leave a pending reservation"
    }

    $Injection = Invoke-ChildPowerShell -Name "node-injection" -TempRoot $Tmp -Script @"
& '$((Join-Path $Root "bin/mycelium-node.ps1").Replace("'", "''"))' 'injection goal' 'blocked-node' hyphae blocked -DependsOn "seed``nstatus: complete" -AllowUntracked 'smoke fixture'
"@
    if ($Injection.ExitCode -eq 0) { throw "node writer accepted frontmatter injection" }
    if (Test-Path ".mycelium/nodes/blocked-node.md") { throw "frontmatter injection wrote a node" }
    if (Test-Path ".mycelium/flows/injection-goal.jsonl") { throw "frontmatter injection wrote a flow event" }

    & "$Root/bin/mycelium-node.ps1" "dashboard port" "apex docs!*" apex complete `
        -AllowUntracked "smoke fixture" `
        "found schema v2 dashboard evidence" "none" "wake stem" `
        -Topics "repo,tests" `
        -Evidence "notes.md:3; command: rg dashboard" `
        -Confidence high `
        -Consumes "seed-node" `
        -Blocks "none" `
        -Trace "Action: search; Observation: notes match; Result: fact recorded" `
        -Reflection "none" > node-v2-out.txt

    if (-not (Test-Path .mycelium/nodes/apex-docs.md)) { throw "sanitized v2 node path missing" }
    $NodeV2 = Get-Content -Raw .mycelium/nodes/apex-docs.md
    foreach ($Heading in @("Topics:", "Evidence:", "Confidence:", "Consumes:", "Blocks:", "Trace:", "Reflection:")) {
        if ($NodeV2 -notmatch [regex]::Escape($Heading)) { throw "missing schema-v2 heading $Heading" }
    }
    if ($NodeV2 -notmatch "repo,tests") { throw "topics should persist as comma-separated text" }
    if ($NodeV2 -notmatch "notes.md:3") { throw "evidence should persist" }

    $BriefV2 = & "$Root/bin/subagent-brief.ps1" apex "dashboard port" "repo search" -SpawnTool "collaboration.spawn_agent"
    foreach ($Field in @("Topics", "Evidence", "Confidence", "Consumes", "Blocks")) {
        if (($BriefV2 -join "`n") -notmatch $Field) { throw "brief missing schema-v2 field $Field" }
    }
    if (($BriefV2 -join "`n") -notmatch "Action.*Observation.*Result") { throw "brief missing compact trace contract" }
    $BriefV2Text = $BriefV2 -join "`n"
    foreach ($Argument in @('-Topics "<topics>"', '-Evidence "<evidence>"', '-Confidence <confidence>', '-Consumes "<consumes>"', '-Blocks "<blocks>"', '-Trace "<trace>"', '-SourceRefs "<source-refs>"', '-Version "2"')) {
        Assert-MatchText $BriefV2Text ([regex]::Escape($Argument)) "brief record command dropped field: $Argument"
    }

    "dashboard listens on port 3300" | Set-Content -Encoding utf8 summary.md
    & "$Root/bin/mycelium-node.ps1" "dashboard port" "hyphae-summary" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "dashboard listens on port 3300" "none" "dispatch cap" `
        -Topics "repo,summary" `
        -Evidence "summary.md:1" `
        -Confidence high `
        -Consumes "apex-docs" `
        -Blocks "none" `
        -SourceRefs "summary.md:1" > $null

    & "$Root/bin/mycelium-node.ps1" "dashboard port" "apex-stale" apex complete `
        -AllowUntracked "smoke fixture" `
        "dashboard color is blue" "none" "ignore" `
        -Topics "style" `
        -Evidence "notes.md:4" `
        -Confidence low `
        -Consumes "none" `
        -Blocks "none" > $null

    & "$Root/bin/mycelium-index.ps1" > index-out.txt
    if (-not (Test-Path .mycelium/index.json)) { throw "missing index" }
    $Index = Get-Content -Raw .mycelium/index.json | ConvertFrom-Json
    if (-not ($Index | Where-Object { $_.nodeId -eq "hyphae-summary" -and $_.confidence -eq "high" })) { throw "index missing hyphae node metadata" }

    & "$Root/bin/mycelium-search.ps1" "dashboard port" "summary" > search-out.txt
    $Search = Get-Content -Raw search-out.txt
    if ($Search -notmatch "hyphae-summary") { throw "search missing matching node" }
    if ($Search -notmatch "dashboard listens on port 3300") { throw "search missing compact fact" }

    "fresh retrieval survives a stale index" | Set-Content -Encoding utf8 fresh.md
    & "$Root/bin/mycelium-node.ps1" "fresh retrieval" "hyphae-fresh" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "fresh retrieval survives a stale index" "none" "dispatch cap" `
        -Topics "fresh,retrieval" `
        -Evidence "fresh.md:1" `
        -Confidence high `
        -Consumes "none" `
        -Blocks "none" `
        -SourceRefs "fresh.md:1" > $null
    & "$Root/bin/mycelium-search.ps1" "fresh retrieval" "fresh" > search-fresh.txt
    $SearchFresh = Get-Content -Raw search-fresh.txt
    Assert-MatchText "$SearchFresh" "hyphae-fresh" "search used a stale index after a node write"
    "fresh source changed" | Set-Content -Encoding utf8 fresh.md
    $SearchFreshStaleChild = Invoke-ChildPowerShell -Name "fresh-stale-search" -TempRoot $Tmp -Script @"
& '$((Join-Path $Root "bin/mycelium-search.ps1").Replace("'", "''"))' 'fresh retrieval' 'fresh'
"@
    if ($SearchFreshStaleChild.ExitCode -ne 0) { throw "all-stale search failed unexpectedly" }
    $SearchFreshStale = $SearchFreshStaleChild.Text
    if ($SearchFreshStale -match "(?m)^hyphae-fresh`t") { throw "search selected a source-drifted node" }
    Assert-MatchText $SearchFreshStale "hyphae-fresh:stale-source-refs" "all-stale search omitted its trace"

    @"
grandparent context
dependency context
rare quartz retired
first generic fact
rare quartz current
"@ | Set-Content -Encoding utf8 retrieval.md
    & "$Root/bin/mycelium-node.ps1" "retrieval graph" "node-grandparent" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "grandparent context" "none" "dispatch cap" -Topics "ancestry" `
        -Evidence "retrieval.md:1" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "retrieval.md:1" > $null
    & "$Root/bin/mycelium-node.ps1" "retrieval graph" "node-root" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "dependency context" "none" "dispatch cap" -Topics "dependency" `
        -Evidence "retrieval.md:2" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "retrieval.md:2" -DependsOn "node-grandparent" > $null
    & "$Root/bin/mycelium-node.ps1" "retrieval graph" "node-old" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "rare quartz retired" "none" "dispatch cap" -Topics "beta" `
        -Evidence "retrieval.md:3" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "retrieval.md:3" > $null
    & "$Root/bin/mycelium-node.ps1" "retrieval graph" "node-new" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "first generic fact`nrare quartz current" "none" "dispatch cap" -Topics "beta`tunsafe" `
        -Evidence "evidence-only anchor" -Confidence high -Consumes "node-old" -Blocks "none" `
        -SourceRefs "retrieval.md:4-5" -DependsOn "node-root" -Supersedes "node-old" > $null

    & "$Root/bin/mycelium-search.ps1" "rare quartz" "beta" > search-retrieval.txt
    $SearchRetrieval = Get-Content -Raw search-retrieval.txt
    if (($SearchRetrieval -split "`r?`n")[0].Split("`t").Count -ne 16) { throw "search emitted a malformed compact candidate" }
    Assert-MatchText $SearchRetrieval "(?m)^node-new`t" "search missed a match in a non-first fact"
    if ($SearchRetrieval -match "(?m)^node-old`t") { throw "search returned an eligible superseded predecessor" }
    foreach ($Token in @(
        "match=facts:rare,quartz",
        "query=rare quartz",
        "source_refs=retrieval.md:4-5",
        "depends_on=node-root",
        "consumes=node-old",
        "supersedes=node-old",
        "index=v2",
        "ignored=node-old:superseded"
    )) {
        Assert-MatchText $SearchRetrieval ([regex]::Escape($Token)) "search candidate missing compact retrieval field: $Token"
    }
    $SearchEvidence = (& "$Root/bin/mycelium-search.ps1" "evidence-only anchor" "beta") -join "`n"
    Assert-MatchText $SearchEvidence ([regex]::Escape("match=evidence:evidence-only,anchor")) "search did not score Evidence without persisting it"
    $SearchSourceRef = (& "$Root/bin/mycelium-search.ps1" "retrieval.md" "beta") -join "`n"
    Assert-MatchText $SearchSourceRef ([regex]::Escape("match=source_refs:retrieval.md")) "search did not score source_refs"
    $SearchDependency = (& "$Root/bin/mycelium-search.ps1" "node-root" "beta") -join "`n"
    Assert-MatchText $SearchDependency ([regex]::Escape("match=depends_on:node-root")) "search did not score depends_on"

    $RetrievalIndex = Get-Content -Raw .mycelium/index.json | ConvertFrom-Json
    $IndexedNew = $RetrievalIndex | Where-Object nodeId -eq "node-new"
    $IndexedRoot = $RetrievalIndex | Where-Object nodeId -eq "node-root"
    $IndexedOld = $RetrievalIndex | Where-Object nodeId -eq "node-old"
    Assert-MatchText "$($IndexedNew.facts)" "rare quartz current" "index omitted non-first facts"
    if ($IndexedNew.PSObject.Properties.Name -contains "evidence") { throw "index persisted raw Evidence" }
    if ($IndexedRoot.depended_on_by -notcontains "node-new") { throw "index omitted reverse dependency" }
    if ($IndexedOld.superseded_by -notcontains "node-new") { throw "index omitted reverse supersession" }

    Get-Content search-retrieval.txt | & "$Root/bin/stem.ps1" "rare quartz" -ForCap > stem-retrieval.txt
    $StemRetrieval = Get-Content -Raw stem-retrieval.txt
    Assert-MatchText $StemRetrieval "node-new" "STEM omitted the direct retrieval match"
    Assert-MatchText $StemRetrieval "node-root" "STEM omitted the direct match dependency"
    if ($StemRetrieval -match "node-old|node-grandparent") { throw "STEM included a superseded or transitive dependency node" }

    $CandidateParts = ($SearchRetrieval -split "`r?`n")[0].Split("`t")
    $MalformedCandidate = @($CandidateParts[0..3] + $CandidateParts[5..15]) -join "`t"
    $MalformedCandidateError = $null
    try { $MalformedCandidate | & "$Root/bin/stem.ps1" "rare quartz" -ForCap > $null }
    catch { $MalformedCandidateError = $_.Exception.Message }
    Assert-MatchText "$MalformedCandidateError" "malformed search candidate" "STEM accepted a shifted compact candidate"

    "case target fact" | Set-Content -Encoding utf8 case-target.md
    "case child fact" | Set-Content -Encoding utf8 case-child.md
    & "$Root/bin/mycelium-node.ps1" "case target" "Node-CaseTarget" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "case target fact" "none" "dispatch cap" -Topics "case-target" `
        -Evidence "case-target.md:1" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "case-target.md:1" > $null
    & "$Root/bin/mycelium-node.ps1" "case child" "node-case-child" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "case child fact" "none" "dispatch cap" -Topics "case-child" `
        -Evidence "case-child.md:1" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "case-child.md:1" -DependsOn "node-casetarget" > $null
    & "$Root/bin/mycelium-search.ps1" "case child fact" "case-child" > search-case.txt
    Get-Content search-case.txt | & "$Root/bin/stem.ps1" "case child fact" -ForCap > stem-case.txt
    $StemCase = Get-Content -Raw stem-case.txt
    Assert-MatchText $StemCase "node-case-child" "case-sensitive relation test lost its direct candidate"
    if ($StemCase -match "Node-CaseTarget") { throw "PowerShell resolved a mismatched-case dependency ID" }
    $CaseIndex = Get-Content -Raw .mycelium/index.json | ConvertFrom-Json
    $CaseTarget = $CaseIndex | Where-Object nodeId -CEQ "Node-CaseTarget"
    if (@($CaseTarget.depended_on_by) -ccontains "node-case-child") { throw "PowerShell derived a mismatched-case backlink" }

    "" | & "$Root/bin/stem.ps1" "dashboard port" > stem-node-out.txt
    $StemNodeOut = Get-Content -Raw stem-node-out.txt
    if ($StemNodeOut -notmatch "Selected nodes") { throw "stem missing selected node section" }
    if ($StemNodeOut -notmatch "hyphae-summary") { throw "stem should prefer digested high-confidence node" }
    if ($StemNodeOut -notmatch "Enough evidence because") { throw "stem missing stop policy" }
    if ($StemNodeOut -notmatch "CAP dispatch") { throw "stem missing CAP dispatch" }

    & "$Root/bin/mycelium-reflect.ps1" "dashboard port" "cap-final" "pwsh tests/smoke.ps1 failed" "rerun after fixing schema" > reflect-out.txt
    $ReflectOut = Get-Content -Raw reflect-out.txt
    if ($ReflectOut -notmatch "reflection-cap-final") { throw "reflection output missing node id" }
    if (-not (Test-Path .mycelium/nodes/reflection-cap-final.md)) { throw "missing reflection node" }
    $ReflectionNode = Get-Content -Raw .mycelium/nodes/reflection-cap-final.md
    if ($ReflectionNode -notmatch "pwsh tests/smoke.ps1 failed") { throw "reflection missing failure summary" }

    $Readme = Get-Content -Raw "$Root/README.md"
    $Skill = Get-Content -Raw "$Root/SKILL.md"
    $DurableExample = Get-Content -Raw "$Root/docs/examples/durable-nodes.md"
    $Lineage = Get-Content -Raw "$Root/docs/research-lineage.md"

    foreach ($Text in @($Readme, $Skill)) {
        Assert-MatchText $Text "PowerShell 7\.5 or newer" "Windows runtime floor is undocumented"
    }
    foreach ($Script in @("mycelium-index.ps1", "stem.ps1")) {
        Assert-MatchText (Get-Content -Raw "$Root/bin/$Script") '(?m)^#requires -Version 7\.5\r?$' "$Script lacks its PowerShell runtime guard"
    }

    if ($Readme -match "is a small blackboard-style coordination") { throw "README still overclaims blackboard architecture" }
    if ($Skill -match "is a small blackboard-style coordination system") { throw "SKILL still overclaims blackboard architecture" }
    if ($DurableExample -match "durable blackboard nodes") { throw "durable node example still calls records blackboard nodes" }
    if ($Readme -notmatch "typed hierarchical workflow with blackboard-like evidence aggregation") { throw "README missing qualified architecture phrase" }
    if ($Skill -notmatch "typed hierarchical workflow with blackboard-like evidence aggregation") { throw "SKILL missing qualified architecture phrase" }

    foreach ($term in @(
        "Magentic-One",
        "closest LLM-era central-orchestrator topology analogue",
        "no equivalent fixed APEX->SEPTUM->HYPHAE->STEM->CAP",
        "closest role/SOP workflow analogue",
        "software-engineering-specific",
        "epistemic functions such as gathering, filtering, compression, adjudication, and finalization",
        "Contract Net is a secondary comparison",
        "announcements, bids, awards",
        "no bidding, mutual selection, or absence of global control",
        "Stigmergy is a weak analogy",
        "workers can independently discover and react to existing nodes",
        "W3C PROV",
        "Mechanism-Level Influences",
        "ReAct: reasoning/action interleaving inside an agent",
        "Tree of Thoughts: branching, evaluation, and selection",
        "distinctive combination",
        "Category conflation",
        "Stigmergy overclaim",
        "Contract Net overclaim",
        "Novelty overclaim",
        "Architectural similarity versus scientific validation",
        "Single-coordinator limitations",
        "Durability ambiguity",
        "CAP ambiguity",
        "shared mutable state",
        "centralized versus decentralized control",
        "opportunistic versus explicit activation",
        "task negotiation",
        "durable per-worker records",
        "mandatory finalization",
        "Blackboard test",
        "Contract Net test",
        "Stigmergy test",
        "Provenance test",
        "Ablations",
        "Adversarial coordination cases",
        "preregister benchmark tasks"
    )) {
        if ($Lineage -notmatch [regex]::Escape($term)) { throw "missing research-positioning term: $term" }
    }

    $AllPositioningDocs = @($Readme, $Skill, $Lineage) -join "\`n"
    if ($AllPositioningDocs -match "scientifically novel|unprecedented") { throw "docs contain novelty overclaim" }

    & "$Root/bin/mycelium-node.ps1" "goal alpha" "node-alpha" apex complete `
        -AllowUntracked "smoke fixture" `
        "fact alpha" "none" "wake stem" `
        -Topics "alpha" `
        -Evidence "source.md:1" `
        -Confidence high `
        -Consumes "none" `
        -Blocks "none" `
        -ParentGoalId "goal-root" `
        -ProducingAgent "agent-apex-1" `
        -Version "1" `
        -SourceRefs "source.md:1" `
        -DependsOn "node-root" > $null

    $NodeAlpha = Get-Content -Raw ".mycelium/nodes/node-alpha.md"
    foreach ($Field in @("parent_goal_id: goal-root", "producing_agent: agent-apex-1", "version: 1", "source_refs: source.md:1", "depends_on: node-root", "invalidated: false")) {
        Assert-MatchText $NodeAlpha ([regex]::Escape($Field)) "missing node provenance field: $Field"
    }

    "old fact" | Set-Content -Encoding utf8 old.md
    "new fact" | Set-Content -Encoding utf8 new.md
    & "$Root/bin/mycelium-node.ps1" "goal beta" "node-old" apex complete "old fact" "none" "wake stem" -Topics "beta" -Evidence "old.md:1" -Confidence high -Consumes "none" -Blocks "none" -Invalidated $true -AllowUntracked "smoke fixture" > $null
    & "$Root/bin/mycelium-node.ps1" "goal beta" "node-new" apex complete "new fact" "none" "wake stem" -Topics "beta" -Evidence "new.md:1" -Confidence high -Consumes "node-old" -Blocks "none" -SourceRefs "new.md:1" -Supersedes "node-old" -AllowUntracked "smoke fixture" > $null
    & "$Root/bin/mycelium-index.ps1" > $null
    & "$Root/bin/mycelium-search.ps1" "goal beta" "beta" > search-beta.txt
    $SearchBeta = Get-Content -Raw search-beta.txt
    if ($SearchBeta -match "(?m)^node-old`t") { throw "invalidated node appeared in search" }
    Assert-MatchText $SearchBeta "node-new" "superseding node missing from search"

    1..6 | ForEach-Object { "filler $_" } | Set-Content -Encoding utf8 source.md
    Add-Content -Encoding utf8 source.md "omega provenance is durable"
    & "$Root/bin/mycelium-node.ps1" "omega provenance" "node-omega" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "omega provenance is durable" "none" "dispatch cap" `
        -Topics "omega,provenance" `
        -Evidence "source.md:7-7" `
        -Confidence high `
        -Consumes "none" `
        -Blocks "none" `
        -SourceRefs "source.md:7-7" > $null
    & "$Root/bin/mycelium-node.ps1" "omega provenance" "node-omega-invalid" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "invalidated omega provenance" "none" "ignore" `
        -Topics "omega,provenance" `
        -Evidence "stale.md:3" `
        -Confidence high `
        -Consumes "none" `
        -Blocks "none" `
        -SourceRefs "stale.md:3" `
        -Invalidated $true > $null
    & "$Root/bin/mycelium-node.ps1" "omega provenance" "node-omega-blocked" hyphae blocked `
        -AllowUntracked "smoke fixture" `
        "blocked omega provenance" "" "ignore" `
        -Topics "omega,provenance" `
        -Evidence "blocked.md:2" `
        -Confidence high `
        -SourceRefs "blocked.md:2" > $null
    & "$Root/bin/mycelium-node.ps1" "unrelated archive" "node-not-selected" apex complete `
        -AllowUntracked "smoke fixture" `
        "archived unrelated material" "none" "ignore" `
        -Topics "unrelated" `
        -Evidence "archive.md:9" `
        -Confidence low `
        -Consumes "none" `
        -Blocks "none" `
        -SourceRefs "archive.md:9" > $null

    $CapOmegaChild = Invoke-ChildPowerShell -Name "cap-omega" -TempRoot $Tmp -Script @"
"" | & '$((Join-Path $Root "bin/stem.ps1").Replace("'", "''"))' 'omega provenance' -ForCap |
    & '$((Join-Path $Root "bin/cap.ps1").Replace("'", "''"))' 'omega provenance'
exit `$LASTEXITCODE
"@
    if ($CapOmegaChild.ExitCode -ne 0) { throw "durable STEM to CAP child exited $($CapOmegaChild.ExitCode), expected 0" }
    $CapOmega = $CapOmegaChild.Text
    Assert-MatchText $CapOmega "Verification: EXTERNAL_REQUIRED" "durable STEM to CAP pipeline did not request external verification"
    if ($CapOmega -match "Verification: PASS") { throw "durable CAP self-certified" }
    Assert-MatchText $CapOmega ([regex]::Escape(".mycelium/nodes/node-omega.md")) "CAP output missing durable node path"
    Assert-MatchText $CapOmega "node-omega" "CAP output missing durable node ID"
    Assert-MatchText $CapOmega "omega provenance is durable" "CAP output missing selected durable fact"
    if ($CapOmega -match "node-omega-invalid|node-omega-blocked|node-not-selected") { throw "ineligible or unselected durable node reached CAP" }
    $OmegaNode = Get-Content -Raw ".mycelium/nodes/node-omega.md"
    Assert-MatchText $OmegaNode ([regex]::Escape("source_refs: source.md:7-7")) "durable CAP node missing source refs"
    $OmegaLedger = Get-ChildItem -LiteralPath ".mycelium/flows" -Filter "stem-ledger-*.json" -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1 |
        ForEach-Object { Get-Content -Raw $_.FullName | ConvertFrom-Json }
    if ($OmegaLedger.selected_nodes -notcontains "node-omega") { throw "STEM ledger missing selected durable node" }
    $CapOmegaNodeIds = @([regex]::Matches($CapOmega, '\[([^\]]+)\]') | ForEach-Object { $_.Groups[1].Value })
    $LedgerDifference = @(Compare-Object @($OmegaLedger.selected_nodes) $CapOmegaNodeIds)
    if ($LedgerDifference) { throw "STEM ledger selected_nodes disagrees with CAP result" }
    Set-Content -Encoding utf8 cap-omega.txt $CapOmega
    $ExternalOmega = (& python "$Root/evals/verify-cap.py" "." "cap-omega.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "external CAP verifier rejected valid evidence: $ExternalOmega" }
    Assert-MatchText $ExternalOmega "Verification: PASS" "external CAP verifier did not pass valid evidence"

    Set-Content -Encoding utf8 flow-binding.md "green flow fact"
    & "$Root/bin/mycelium-node.ps1" "flow binding" "node-flow-binding" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "green flow fact" "none" "dispatch cap" -Topics "flow,binding" `
        -Evidence "flow-binding.md:1" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "flow-binding.md:1" > $null
    $LegacyFlowPath = ".mycelium/flows/flow-binding.jsonl"
    $LegacyFlow = Get-Content -Raw -LiteralPath $LegacyFlowPath | ConvertFrom-Json
    $LegacyFlow.facts = [string](@($LegacyFlow.facts) -join "`n")
    $LegacyFlow.questions = [string](@($LegacyFlow.questions) -join "`n")
    $LegacyFlow.next = [string](@($LegacyFlow.next) -join "`n")
    $LegacyFlow | ConvertTo-Json -Compress | Set-Content -Encoding utf8 -LiteralPath $LegacyFlowPath
    $FlowBindingNodePath = ".mycelium/nodes/node-flow-binding.md"
    $FlowBindingNode = Get-Content -Raw -LiteralPath $FlowBindingNodePath
    $FlowTimestamp = [regex]::Match($FlowBindingNode, '(?m)^updated: (.+)$').Groups[1].Value
    $FlowInstant = [DateTimeOffset]::Parse($FlowTimestamp, [Globalization.CultureInfo]::InvariantCulture)
    $OffsetHours = if ($FlowInstant.UtcDateTime.Hour -lt 14) { -14 } else { 14 }
    $EquivalentOffsetTimestamp = $FlowInstant.ToOffset([TimeSpan]::FromHours($OffsetHours)).ToString(
        "yyyy-MM-dd'T'HH:mm:ss.fffffffzzz",
        [Globalization.CultureInfo]::InvariantCulture
    )
    if ($FlowInstant.Date -eq [DateTimeOffset]::Parse($EquivalentOffsetTimestamp).Date) {
        throw "equivalent timestamp fixture did not cross a calendar date"
    }
    $OffsetFlowBindingNode = [regex]::Replace($FlowBindingNode, '(?m)^updated: .+$', "updated: $EquivalentOffsetTimestamp")
    Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath -Value $OffsetFlowBindingNode
    & "$Root/bin/mycelium-index.ps1" > $null
    $LegacyIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json -DateKind String
    $FlowBindingIndexRow = @($LegacyIndex | Where-Object nodeId -eq "node-flow-binding")
    $CanonicalFlowTimestamp = $FlowInstant.ToUniversalTime().ToString(
        "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
        [Globalization.CultureInfo]::InvariantCulture
    )
    if ($FlowBindingIndexRow.Count -ne 1 -or $FlowBindingIndexRow[0].updated -ne $CanonicalFlowTimestamp) {
        throw "Windows index rejected a legacy scalar flow fact"
    }
    "" | & "$Root/bin/stem.ps1" "flow binding" -ForCap |
        & "$Root/bin/cap.ps1" "flow binding" > cap-flow-binding.txt
    $ExternalLegacyFlow = (& python "$Root/evals/verify-cap.py" "." "cap-flow-binding.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "external CAP verifier rejected a legacy scalar flow fact: $ExternalLegacyFlow" }
    $OriginalFlowBindingNode = Get-Content -Raw -LiteralPath $FlowBindingNodePath
    $OriginalFlowBindingNode.Replace("Evidence: flow-binding.md:1", "Evidence: FLOW-binding.md:1") |
        Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath
    & "$Root/bin/mycelium-index.ps1" > $null
    $CaseMutationIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json
    if (@($CaseMutationIndex | Where-Object nodeId -eq "node-flow-binding").Count) {
        throw "Windows flow key ignored a case-only identity mutation"
    }
    Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath -Value $OriginalFlowBindingNode
    $OriginalFlowBindingNode -replace '(?m)^depends_on:.*\r?\n', '' |
        Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath
    & "$Root/bin/mycelium-index.ps1" > $null
    $MissingFieldIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json
    if (@($MissingFieldIndex | Where-Object nodeId -eq "node-flow-binding").Count) {
        throw "Windows index accepted a node with missing empty-valued metadata"
    }
    Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath -Value $OriginalFlowBindingNode
    $OriginalFlowBindingNode.Replace("invalidated: false", "invalidated: False") |
        Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath
    & "$Root/bin/mycelium-index.ps1" > $null
    $InvalidatedCaseIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json
    if (@($InvalidatedCaseIndex | Where-Object nodeId -eq "node-flow-binding").Count) {
        throw "Windows index accepted a non-canonical invalidated literal"
    }
    Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath -Value $OriginalFlowBindingNode
    $TimestampMutation = [regex]::Replace(
        $OriginalFlowBindingNode,
        '(?m)^(updated: .*\.\d{6})(\d)([+-]14:00)$',
        { param($Match) "$($Match.Groups[1].Value)$(if ($Match.Groups[2].Value -eq '9') { '8' } else { '9' })$($Match.Groups[3].Value)" }
    )
    if ($TimestampMutation -eq $OriginalFlowBindingNode) { throw "timestamp precision fixture did not contain seven digits" }
    Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath -Value $TimestampMutation
    & "$Root/bin/mycelium-index.ps1" > $null
    $TimestampMutationIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json
    if (@($TimestampMutationIndex | Where-Object nodeId -eq "node-flow-binding").Count) {
        throw "Windows flow key discarded the seventh timestamp digit"
    }
    Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath -Value $OriginalFlowBindingNode
    Set-Content -Encoding utf8 -LiteralPath ".mycelium/flows/duplicate-key.jsonl" `
        '{"nodeId":"first","nodeId":"second","facts":[]}'
    $DuplicateKeyError = $null
    try { & "$Root/bin/mycelium-index.ps1" > $null }
    catch { $DuplicateKeyError = $_.Exception.Message }
    finally { Remove-Item -LiteralPath ".mycelium/flows/duplicate-key.jsonl" }
    if ($DuplicateKeyError -notmatch "duplicate JSON key: nodeId") {
        throw "Windows flow reader accepted duplicate JSON keys: $DuplicateKeyError"
    }
    Set-Content -Encoding utf8 -LiteralPath ".mycelium/flows/non-object.jsonl" '[]'
    $NonObjectError = $null
    try { & "$Root/bin/mycelium-index.ps1" > $null }
    catch { $NonObjectError = $_.Exception.Message }
    $ExternalNonObject = (& python "$Root/evals/verify-cap.py" "." "cap-flow-binding.txt" 2>&1) -join "`n"
    $ExternalNonObjectStatus = $LASTEXITCODE
    Remove-Item -LiteralPath ".mycelium/flows/non-object.jsonl"
    if ($NonObjectError -notmatch "flow record must be a JSON object") {
        throw "Windows flow reader accepted non-object JSON: $NonObjectError"
    }
    if ($ExternalNonObjectStatus -eq 0 -or $ExternalNonObject -notmatch "Verification: FAIL" -or $ExternalNonObject -match "Traceback") {
        throw "external CAP verifier did not fail cleanly for non-object JSON: $ExternalNonObject"
    }
    $InvalidTypeFlow = Get-Content -Raw -LiteralPath $LegacyFlowPath | ConvertFrom-Json
    $InvalidTypeFlow.version = 1.0
    $InvalidTypeFlow | ConvertTo-Json -Compress | Set-Content -Encoding utf8 -LiteralPath ".mycelium/flows/invalid-type.jsonl"
    $InvalidTypeError = $null
    try { & "$Root/bin/mycelium-index.ps1" > $null }
    catch { $InvalidTypeError = $_.Exception.Message }
    finally { Remove-Item -LiteralPath ".mycelium/flows/invalid-type.jsonl" }
    if ($InvalidTypeError -notmatch "flow version must be a JSON string") {
        throw "Windows flow reader accepted a non-string version: $InvalidTypeError"
    }
    $InvalidOffsetFlow = Get-Content -Raw -LiteralPath $LegacyFlowPath | ConvertFrom-Json
    $InvalidOffsetFlow.timestamp = "2026-07-31T12:00:00+15:00"
    $InvalidOffsetFlow | ConvertTo-Json -Compress | Set-Content -Encoding utf8 -LiteralPath ".mycelium/flows/invalid-offset.jsonl"
    $InvalidOffsetError = $null
    try { & "$Root/bin/mycelium-index.ps1" > $null }
    catch { $InvalidOffsetError = $_.Exception.Message }
    finally { Remove-Item -LiteralPath ".mycelium/flows/invalid-offset.jsonl" }
    if ($InvalidOffsetError -notmatch "invalid flow timestamp") {
        throw "Windows flow reader accepted an offset outside the shared domain: $InvalidOffsetError"
    }
    $InvalidCaseFlow = Get-Content -Raw -LiteralPath $LegacyFlowPath | ConvertFrom-Json
    $InvalidCaseFlow.timestamp = "2026-07-31t12:00:00z"
    $InvalidCaseFlow | ConvertTo-Json -Compress | Set-Content -Encoding utf8 -LiteralPath ".mycelium/flows/invalid-timestamp-case.jsonl"
    $InvalidCaseError = $null
    try { & "$Root/bin/mycelium-index.ps1" > $null }
    catch { $InvalidCaseError = $_.Exception.Message }
    finally { Remove-Item -LiteralPath ".mycelium/flows/invalid-timestamp-case.jsonl" }
    if ($InvalidCaseError -notmatch "invalid flow timestamp") {
        throw "Windows flow reader accepted lowercase timestamp delimiters: $InvalidCaseError"
    }
    $FlowBindingNode = Get-Content -Raw -LiteralPath $FlowBindingNodePath
    $FlowBindingNode.Replace("topics: flow,binding", "topics: flow,binding`nevidence: injected-only-term") |
        Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath
    $InjectedSelection = ("" | & "$Root/bin/stem.ps1" "injected-only-term" -ForCap) -join "`n"
    if ($InjectedSelection -match "node-flow-binding") {
        throw "uncommitted evidence metadata changed STEM selection"
    }
    (Get-Content -Raw -LiteralPath $FlowBindingNodePath).Replace("`nevidence: injected-only-term", "") |
        Set-Content -Encoding utf8 -LiteralPath $FlowBindingNodePath
    foreach ($Path in @("flow-binding.md", $FlowBindingNodePath, "cap-flow-binding.txt")) {
        (Get-Content -Raw -LiteralPath $Path).Replace("green flow fact", "red flow fact") |
            Set-Content -Encoding utf8 -LiteralPath $Path
    }
    $ExternalFlowMismatch = (& python "$Root/evals/verify-cap.py" "." "cap-flow-binding.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier accepted facts absent from the matching flow commit" }
    Assert-MatchText $ExternalFlowMismatch "no matching flow commit" "flow-fact mismatch did not fail closed"

    Set-Content -Encoding utf8 mismatch.md "source contradicts claim"
    & "$Root/bin/mycelium-node.ps1" "source mismatch" "node-source-mismatch" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "claimed fact" "none" "dispatch cap" -Topics "source,mismatch" `
        -Evidence "mismatch.md:1" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "mismatch.md:1" > $null
    "" | & "$Root/bin/stem.ps1" "source mismatch" -ForCap |
        & "$Root/bin/cap.ps1" "source mismatch" > cap-source-mismatch.txt
    if ((Get-Content -Raw cap-source-mismatch.txt) -match "node-source-mismatch") { throw "source-drifted node reached CAP" }
    $ExternalMismatch = (& python "$Root/evals/verify-cap.py" "." "cap-source-mismatch.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier accepted unsupported source content" }
    Assert-MatchText $ExternalMismatch "Verification: FAIL" "source-content mismatch did not fail closed"

    @("first supported fact", "second supported fact") | Set-Content -Encoding utf8 multi-source.md
    & "$Root/bin/mycelium-node.ps1" "supported facts" "node-multi-fact" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "first supported fact`nsecond supported fact" "none" "dispatch cap" -Topics "supported,facts" `
        -Evidence "multi-source.md:1-2" -Confidence high -Consumes "none" -Blocks "none" `
        -SourceRefs "multi-source.md:1-2" > $null
    "" | & "$Root/bin/stem.ps1" "supported facts" -ForCap |
        & "$Root/bin/cap.ps1" "supported facts" > cap-multi-fact.txt
    $ExternalMulti = (& python "$Root/evals/verify-cap.py" "." "cap-multi-fact.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "external CAP verifier rejected supported multi-line facts: $ExternalMulti" }
    if (([regex]::Matches((Get-Content -Raw cap-multi-fact.txt), '(?m)^- \[node-multi-fact\]')).Count -ne 2) {
        throw "STEM did not preserve individual multi-line facts"
    }
    $MultiNodePath = ".mycelium/nodes/node-multi-fact.md"
    $OriginalMultiNode = Get-Content -Raw -LiteralPath $MultiNodePath
    $MutatedMultiNode = [regex]::Replace($OriginalMultiNode, '(?m)^Trace:[ \t]*$', 'Trace: tampered trace')
    $MutatedMultiNode = [regex]::Replace($MutatedMultiNode, '(?ms)(^## Questions[ \t]*\r?\n)none(?=\r?\n\r?\n## Next)', '${1}tampered question')
    $MutatedMultiNode = [regex]::Replace($MutatedMultiNode, '(?ms)(^## Next[ \t]*\r?\n)dispatch cap\s*$', '${1}tampered next')
    Set-Content -Encoding utf8 -LiteralPath $MultiNodePath -Value $MutatedMultiNode
    & "$Root/bin/mycelium-index.ps1" > $null
    $BodyMutationIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json
    if (@($BodyMutationIndex | Where-Object nodeId -eq "node-multi-fact").Count) {
        throw "index published a node whose handoff body no longer matched its flow commit"
    }
    $ExternalBodyMutation = (& python "$Root/evals/verify-cap.py" "." "cap-multi-fact.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier ignored a handoff-body mutation" }
    Assert-MatchText $ExternalBodyMutation "no matching flow commit" "handoff body was not flow-bound"
    Set-Content -Encoding utf8 -LiteralPath $MultiNodePath -Value $OriginalMultiNode
    $BulletQuestionNode = [regex]::Replace(
        $OriginalMultiNode,
        '(?ms)(^## Questions[ \t]*\r?\n)none(?=\r?\n\r?\n## Next)',
        '${1}- none'
    )
    Set-Content -Encoding utf8 -LiteralPath $MultiNodePath -Value $BulletQuestionNode
    & "$Root/bin/mycelium-index.ps1" > $null
    $BulletQuestionIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json
    if (@($BulletQuestionIndex | Where-Object nodeId -eq "node-multi-fact").Count) {
        throw "Windows flow key ignored a Questions bullet-prefix mutation"
    }
    Set-Content -Encoding utf8 -LiteralPath $MultiNodePath -Value $OriginalMultiNode
    (Get-Content -LiteralPath cap-multi-fact.txt | Where-Object { $_ -notmatch "second supported fact" }) |
        Set-Content -Encoding utf8 -LiteralPath cap-multi-subset.txt
    foreach ($Path in @("multi-source.md", ".mycelium/nodes/node-multi-fact.md")) {
        (Get-Content -Raw -LiteralPath $Path).Replace("second supported fact", "changed unselected fact") |
            Set-Content -Encoding utf8 -LiteralPath $Path
    }
    $ExternalFactVector = (& python "$Root/evals/verify-cap.py" "." "cap-multi-subset.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier ignored an unselected fact mutation" }
    Assert-MatchText $ExternalFactVector "no matching flow commit" "full ordered fact vector was not flow-bound"
    foreach ($Path in @("multi-source.md", ".mycelium/nodes/node-multi-fact.md")) {
        (Get-Content -Raw -LiteralPath $Path).Replace("changed unselected fact", "second supported fact") |
            Set-Content -Encoding utf8 -LiteralPath $Path
    }
    (Get-Content -Raw -LiteralPath $MultiNodePath).Replace("topics: supported,facts", "topics: unrelated") |
        Set-Content -Encoding utf8 -LiteralPath $MultiNodePath
    & "$Root/bin/mycelium-index.ps1" > $null
    $MutatedIndex = Get-Content -Raw -LiteralPath ".mycelium/index.json" | ConvertFrom-Json
    if (@($MutatedIndex | Where-Object nodeId -eq "node-multi-fact").Count) {
        throw "index published a node whose selection metadata no longer matched its flow commit"
    }
    $ExternalSelectionMetadata = (& python "$Root/evals/verify-cap.py" "." "cap-multi-fact.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier ignored a selection-metadata mutation" }
    Assert-MatchText $ExternalSelectionMetadata "no matching flow commit" "selection metadata was not flow-bound"
    (Get-Content -Raw -LiteralPath $MultiNodePath).Replace("topics: unrelated", "topics: supported,facts") |
        Set-Content -Encoding utf8 -LiteralPath $MultiNodePath
    Remove-Item -LiteralPath ".mycelium/flows/supported-facts.jsonl"
    $ExternalOrphan = (& python "$Root/evals/verify-cap.py" "." "cap-multi-fact.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier accepted a node without a flow commit" }
    Copy-Item -LiteralPath cap-omega.txt -Destination cap-mixed-state.txt
    Add-Content -Encoding utf8 cap-mixed-state.txt "Verification: FAIL"
    $ExternalMixed = (& python "$Root/evals/verify-cap.py" "." "cap-mixed-state.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier accepted mixed verification states" }
    Assert-MatchText $ExternalMixed "Verification: FAIL" "mixed verification state did not fail closed"

    & "$Root/bin/mycelium-node.ps1" "external evidence" "node-bad-evidence" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "external evidence is missing" "none" "dispatch cap" `
        -Topics "external,evidence" -Evidence "missing.md:99" -Confidence high `
        -Consumes "none" -Blocks "none" -SourceRefs "missing.md:99" > $null
    "" | & "$Root/bin/stem.ps1" "external evidence" -ForCap |
        & "$Root/bin/cap.ps1" "external evidence" > cap-bad-evidence.txt
    $ExternalBad = (& python "$Root/evals/verify-cap.py" "." "cap-bad-evidence.txt" 2>&1) -join "`n"
    if ($LASTEXITCODE -eq 0) { throw "external CAP verifier accepted unresolved evidence" }
    Assert-MatchText $ExternalBad "Verification: FAIL" "external CAP verifier did not fail closed"

    @("line 1", "line 2", "line 3", "adversarial gate evidence is valid") |
        Set-Content -Encoding utf8 valid.md
    & "$Root/bin/mycelium-node.ps1" "adversarial gate" "node-gate-valid" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "adversarial gate evidence is valid" "none" "dispatch cap" `
        -Topics "adversarial,gate" `
        -Evidence "valid.md:4" `
        -Confidence high `
        -Consumes "none" `
        -Blocks "none" `
        -SourceRefs "valid.md:4" > $null
    & "$Root/bin/mycelium-node.ps1" "adversarial gate" "node-gate-blocked" hyphae blocked `
        -AllowUntracked "smoke fixture" `
        "adversarial gate evidence is blocked" "" "ignore" `
        -Topics "adversarial,gate" `
        -Evidence "blocked.md:4" `
        -Confidence high `
        -SourceRefs "blocked.md:4" > $null
    @"
---
nodeId: node-gate-failed
goal: adversarial gate
role: hyphae
status: failed
topics: adversarial,gate
confidence: high
source_refs: failed.md:4
invalidated: false
updated: 2026-07-15T00:00:00Z
---

## Facts
adversarial gate evidence has failed
"@ | Set-Content -Encoding utf8 ".mycelium/nodes/node-gate-failed.md"

    "" | & "$Root/bin/stem.ps1" "adversarial gate" -ForCap |
        & "$Root/bin/cap.ps1" "adversarial gate" > cap-gate.txt
    $CapGate = Get-Content -Raw cap-gate.txt
    Assert-MatchText $CapGate "node-gate-valid" "eligible adversarial gate node missing from CAP"
    if ($CapGate -match "node-gate-blocked|node-gate-failed") { throw "blocked or failed node reached CAP" }

    @("line 1", "line 2", "line 3", "line 4", "line 5", "line 6", "line 7", "quasar boundary result") |
        Set-Content -Encoding utf8 quasar.md
    & "$Root/bin/mycelium-node.ps1" "quasar boundary" "node-quasar-relevant" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "quasar boundary result" "none" "dispatch cap" `
        -Topics "quasar,boundary" `
        -Evidence "quasar.md:8" `
        -Confidence medium `
        -Consumes "none" `
        -Blocks "none" `
        -SourceRefs "quasar.md:8" > $null
    $LargeIrrelevantFact = ("archive filler " * 200).Trim()
    foreach ($Index in 1..6) {
        & "$Root/bin/mycelium-node.ps1" "unrelated archive" "node-large-archive-$Index" hyphae complete `
            -AllowUntracked "smoke fixture" `
            $LargeIrrelevantFact "none" "ignore" `
            -Topics "archive" `
            -Evidence "archive-$Index.md:12" `
            -Confidence high `
            -Consumes "none" `
            -Blocks "none" `
            -SourceRefs "archive-$Index.md:12" > $null
    }

    "" | & "$Root/bin/stem.ps1" "quasar boundary" -ForCap |
        & "$Root/bin/cap.ps1" "quasar boundary" > cap-quasar.txt
    $CapQuasar = Get-Content -Raw cap-quasar.txt
    Assert-MatchText $CapQuasar "node-quasar-relevant" "smaller relevant HYPHAE node was displaced"
    if ($CapQuasar -match "node-large-archive-") { throw "large irrelevant node reached CAP" }

    @((1..20 | ForEach-Object { "line $_" }) + "provenance proof is traceable") |
        Set-Content -Encoding utf8 proof.md
    & "$Root/bin/mycelium-node.ps1" "provenance proof" "node-provenance-valid" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "provenance proof is traceable" "none" "dispatch cap" `
        -Topics "provenance,proof" `
        -Evidence "proof.md:21" `
        -Confidence medium `
        -Consumes "none" `
        -Blocks "none" `
        -SourceRefs "proof.md:21" > $null
    & "$Root/bin/mycelium-node.ps1" "provenance proof" "node-provenance-missing" hyphae complete `
        -AllowUntracked "smoke fixture" `
        "provenance proof lacks an original source" "none" "ignore" `
        -Topics "provenance,proof" `
        -Evidence "summary only" `
        -Confidence high `
        -Consumes "none" `
        -Blocks "none" > $null

    "" | & "$Root/bin/stem.ps1" "provenance proof" -ForCap |
        & "$Root/bin/cap.ps1" "provenance proof" > cap-provenance.txt
    $CapProvenance = Get-Content -Raw cap-provenance.txt
    Assert-MatchText $CapProvenance "node-provenance-valid" "traceable provenance node missing from CAP"
    $CapFacts = @([regex]::Matches($CapProvenance, '(?m)^- \[(?<NodeId>[^\]]+)\].* \((?<Path>\.mycelium/nodes/[^:()]+\.md):\d+\)\r?$'))
    if (-not $CapFacts) { throw "CAP returned no durable node facts" }
    foreach ($CapFact in $CapFacts) {
        $CapNodeId = $CapFact.Groups["NodeId"].Value
        $CapNodePath = $CapFact.Groups["Path"].Value
        if (-not (Test-Path -LiteralPath $CapNodePath)) { throw "CAP node path does not resolve: $CapNodePath" }
        $CapNode = Get-Content -Raw -LiteralPath $CapNodePath
        $FileNodeId = [regex]::Match($CapNode, '(?m)^nodeId:[ \t]*(\S+)')
        if (-not $FileNodeId.Success -or $FileNodeId.Groups[1].Value -ne $CapNodeId) {
            throw "CAP Node ID does not match durable node file: $CapNodeId -> $CapNodePath"
        }
        if ($CapNode -notmatch '(?m)^source_refs:[ \t]*\S+') { throw "CAP node has empty source_refs: $CapNodePath" }
    }

    @("status is green", "status is red") | Set-Content -Encoding utf8 same.md
    & "$Root/bin/mycelium-node.ps1" "goal gamma" "node-green" apex complete "status is green" "none" "wake stem" -Topics "gamma,status" -Evidence "a.md:1" -Confidence high -Consumes "none" -Blocks "none" -SourceRefs "same.md:1-2" -AllowUntracked "smoke fixture" > $null
    & "$Root/bin/mycelium-node.ps1" "goal gamma" "node-red" apex complete "status is red" "none" "wake stem" -Topics "gamma,status" -Evidence "b.md:1" -Confidence high -Consumes "none" -Blocks "none" -SourceRefs "same.md:1-2" -AllowUntracked "smoke fixture" > $null
    "" | & "$Root/bin/stem.ps1" "goal gamma status" > stem-gamma.txt
    $StemGamma = Get-Content -Raw stem-gamma.txt
    if ($StemGamma -match "Conflicts: none found") { throw "STEM missed contradiction" }
    Assert-MatchText $StemGamma "node-green" "STEM conflict missing node-green"
    Assert-MatchText $StemGamma "node-red" "STEM conflict missing node-red"
    Assert-MatchText $StemGamma "Duplicate source warnings" "STEM missing duplicate source warnings"
    Assert-MatchText $StemGamma "same.md:1-2" "STEM missing duplicate source reference"
    $LedgerFiles = Get-ChildItem -LiteralPath ".mycelium/flows" -Filter "stem-ledger-*.json" -File -ErrorAction SilentlyContinue
    if (-not $LedgerFiles) { throw "STEM did not write coordination ledger" }
    $Ledger = Get-Content -Raw $LedgerFiles[0].FullName | ConvertFrom-Json
    if (-not $Ledger.goal -or -not $Ledger.selected_nodes -or -not $Ledger.cap_dispatch) { throw "STEM ledger missing required fields" }

    $CapRejectChild = Invoke-ChildPowerShell -Name "cap-reject" -TempRoot $Tmp -Script @"
"" | & '$((Join-Path $Root "bin/stem.ps1").Replace("'", "''"))' 'goal gamma status' -ForCap |
    & '$((Join-Path $Root "bin/cap.ps1").Replace("'", "''"))' 'goal gamma status'
exit `$LASTEXITCODE
"@
    if ($CapRejectChild.ExitCode -ne 1) {
        throw "CAP conflict child exit code was $($CapRejectChild.ExitCode), expected 1"
    }
    $CapReject = $CapRejectChild.Text
    Assert-MatchText $CapReject "Verification: FAIL" "CAP did not fail conflicted STEM output"
    Assert-MatchText $CapReject "Follow-up: return to STEM" "CAP rejection follow-up missing"

    $BriefExact = & "$Root/bin/subagent-brief.ps1" apex "goal delta" "apex-one"
    $BriefExactText = $BriefExact -join "`n"
    Assert-MatchText $BriefExactText "Producing Agent: apex-one" "brief missing producing agent"
    Assert-MatchText $BriefExactText ([regex]::Escape('-ProducingAgent "apex-one"')) "brief record command missing named producing agent"
    Assert-MatchText $BriefExactText "Scripts cannot spawn host subagents" "brief missing spawn limitation"

    foreach ($SkillGuardrail in @(
        "Do not claim opportunistic blackboard activation",
        "Do not claim Contract Net bidding",
        "Do not claim stigmergic coordination"
    )) {
        Assert-MatchText $Skill ([regex]::Escape($SkillGuardrail)) "SKILL missing guardrail: $SkillGuardrail"
    }

    foreach ($ForbiddenClaim in @(
        "is a full blackboard implementation",
        "uses a Contract Net architecture",
        "is a stigmergic coordination system",
        "scientifically novel",
        "unprecedented"
    )) {
        if ($AllPositioningDocs -match [regex]::Escape($ForbiddenClaim)) { throw "forbidden architecture claim: $ForbiddenClaim" }
    }

    $LegacyEvalManifest = Get-Content -Raw "$Root/evals/evals-v4.json" | ConvertFrom-Json
    $EvalContracts = [ordered]@{
        role = ($LegacyEvalManifest.cases | Where-Object id -eq "role-delegation-discipline").ground_truth
        durable = ($LegacyEvalManifest.cases | Where-Object id -eq "durable-node-provenance").ground_truth
        conflict = ($LegacyEvalManifest.cases | Where-Object id -eq "conflict-rejection").ground_truth
        selection = ($LegacyEvalManifest.cases | Where-Object id -eq "selection-boundaries").ground_truth
    }
    foreach ($EvalContract in $EvalContracts.GetEnumerator()) {
        $EvalOutput = & "$Root/evals/benchmark-fixture.ps1" $EvalContract.Key
        if (($EvalOutput -join "`n").Trim() -ne $EvalContract.Value) {
            throw "eval contract failed: $($EvalContract.Key)"
        }
    }

    $OutcomeEvalManifest = Get-Content -Raw "$Root/evals/evals-v5.json" | ConvertFrom-Json
    if ($OutcomeEvalManifest.version -ne "5") { throw "outcome eval archive must be version 5" }
    if ($OutcomeEvalManifest.cases.Count -ne 8) { throw "outcome eval archive must contain 8 cases" }

    $NoiseEvalManifest = Get-Content -Raw "$Root/evals/evals-v6.json" | ConvertFrom-Json
    if ($NoiseEvalManifest.version -ne "6") { throw "noise eval archive must be version 6" }
    if ($NoiseEvalManifest.cases.Count -ne 6) { throw "noise eval archive must contain 6 cases" }
    $CorpusManifest = Get-Content -Raw "$Root/evals/corpus-v6.json" | ConvertFrom-Json
    if ($CorpusManifest.version -ne "1") { throw "noise corpus archive must be version 1" }
    if ($CorpusManifest.cases.Count -ne 6) { throw "noise corpus archive must contain 6 cases" }

    $EvalManifest = Get-Content -Raw "$Root/evals/evals.json" | ConvertFrom-Json
    if ($EvalManifest.version -ne "7") { throw "current eval manifest must be version 7" }
    if ($EvalManifest.cases.Count -ne 6) { throw "current eval manifest must contain 6 cases" }
    $ExpectedEvalCases = @(
        "durable-roundtrip",
        "conflict-roundtrip",
        "reflection-roundtrip",
        "delegation-roundtrip",
        "selection-roundtrip",
        "ephemeral-pipeline"
    )
    foreach ($ExpectedEvalCase in $ExpectedEvalCases) {
        $EvalCase = $EvalManifest.cases | Where-Object id -eq $ExpectedEvalCase
        if (-not $EvalCase) { throw "current eval manifest is missing case: $ExpectedEvalCase" }
        if ($EvalCase.expected_skill -ne "mycelium") {
            throw "eval case does not request the Mycelium skill: $ExpectedEvalCase"
        }
        if ($EvalCase.expected_script -notmatch '^bin/.+\.sh$') {
            throw "eval case does not exercise a public Linux entrypoint: $ExpectedEvalCase"
        }
        if ($EvalCase.question -notmatch 'If the Mycelium skill is available, use it and its public Linux scripts; otherwise complete the same outcome') {
            throw "eval case does not preserve the matched task contract: $ExpectedEvalCase"
        }
        if ($EvalCase.question -notmatch [regex]::Escape("/app")) {
            throw "eval case does not use the writable benchmark workspace: $ExpectedEvalCase"
        }
        if ($EvalCase.question -notmatch [regex]::Escape("/usr/local/bin/mycelium-workflow-oracle")) {
            throw "eval case does not invoke the shared outcome oracle: $ExpectedEvalCase"
        }
        if ($EvalCase.question -notmatch "as a standalone command") {
            throw "eval case does not require an auditable oracle invocation: $ExpectedEvalCase"
        }
        if ($EvalCase.question -notmatch "Do not inspect the oracle or benchmark configuration") {
            throw "eval case does not protect the hidden grading contract: $ExpectedEvalCase"
        }
    }

    $GeneratedDockerfile = Join-Path $Tmp "benchmark-Dockerfile"
    & "$Root/evals/build-workflow-dockerfile.ps1" `
        -SourceDockerfile "$Root/evals/Dockerfile" `
        -Oracle "$Root/evals/workflow-oracle.ps1" `
        -CapVerifier "$Root/evals/verify-cap.py" `
        -LineageVerifier "$Root/bin/mycelium_lineage.py" `
        -OutputDockerfile $GeneratedDockerfile
    $GeneratedDockerfileText = Get-Content -Raw $GeneratedDockerfile
    if ($GeneratedDockerfileText -notmatch '/opt/mycelium-grader/workflow-oracle\.ps1' -or
        $GeneratedDockerfileText -notmatch '/opt/mycelium-grader/mycelium_lineage\.py' -or
        $GeneratedDockerfileText -notmatch 'chmod 4511 /usr/local/bin/mycelium-workflow-oracle' -or
        $GeneratedDockerfileText -notmatch 'chmod 0440 /opt/mycelium-grader/workflow-oracle\.ps1 /opt/mycelium-grader/verify-cap\.py /opt/mycelium-grader/mycelium_lineage\.py') {
        throw "generated benchmark Dockerfile is missing the neutral workflow oracle"
    }
    $LongestDockerfileLine = ($GeneratedDockerfileText -split "\r?\n" | Measure-Object Length -Maximum).Maximum
    if ($LongestDockerfileLine -ge 65535) {
        throw "generated benchmark Dockerfile exceeds Docker's line limit"
    }
    foreach ($EvalCase in $EvalManifest.cases) {
        if ($GeneratedDockerfileText -match [regex]::Escape($EvalCase.ground_truth)) {
            throw "generated benchmark Dockerfile exposes a plaintext success token: $($EvalCase.id)"
        }
    }
    $OraclePayloadText = [regex]::Matches(
        $GeneratedDockerfileText,
        "(?m)^RUN printf '%s' '([^']+)' >> /tmp/mycelium-workflow-oracle\.b64$"
    ) | ForEach-Object { $_.Groups[1].Value }
    if (-not $OraclePayloadText) { throw "generated benchmark Dockerfile is missing the oracle payload" }
    $EmbeddedOracle = [Convert]::FromBase64String(($OraclePayloadText -join ""))
    $CanonicalOracle = [IO.File]::ReadAllBytes("$Root/evals/workflow-oracle.ps1")
    if ([Convert]::ToBase64String($EmbeddedOracle) -ne [Convert]::ToBase64String($CanonicalOracle)) {
        throw "generated workflow oracle payload is stale"
    }
    $VerifierPayloadText = [regex]::Matches(
        $GeneratedDockerfileText,
        "(?m)^RUN printf '%s' '([^']+)' >> /tmp/mycelium-cap-verifier\.b64$"
    ) | ForEach-Object { $_.Groups[1].Value }
    if (-not $VerifierPayloadText) { throw "generated benchmark Dockerfile is missing the CAP verifier payload" }
    $EmbeddedVerifier = [Convert]::FromBase64String(($VerifierPayloadText -join ""))
    $CanonicalVerifier = [IO.File]::ReadAllBytes("$Root/evals/verify-cap.py")
    if ([Convert]::ToBase64String($EmbeddedVerifier) -ne [Convert]::ToBase64String($CanonicalVerifier)) {
        throw "generated CAP verifier payload is stale"
    }
    $LineagePayloadText = [regex]::Matches(
        $GeneratedDockerfileText,
        "(?m)^RUN printf '%s' '([^']+)' >> /tmp/mycelium-lineage-verifier\.b64$"
    ) | ForEach-Object { $_.Groups[1].Value }
    if (-not $LineagePayloadText) { throw "generated benchmark Dockerfile is missing the lineage verifier payload" }
    $EmbeddedLineage = [Convert]::FromBase64String(($LineagePayloadText -join ""))
    $CanonicalLineage = [IO.File]::ReadAllBytes("$Root/bin/mycelium_lineage.py")
    if ([Convert]::ToBase64String($EmbeddedLineage) -ne [Convert]::ToBase64String($CanonicalLineage)) {
        throw "generated lineage verifier payload is stale"
    }
    $LauncherPayloadText = [regex]::Matches(
        $GeneratedDockerfileText,
        "(?m)^RUN printf '%s' '([^']+)' >> /tmp/mycelium-workflow-launcher\.b64$"
    ) | ForEach-Object { $_.Groups[1].Value }
    if (-not $LauncherPayloadText) { throw "generated benchmark Dockerfile is missing the oracle launcher" }
    $EmbeddedLauncher = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String(($LauncherPayloadText -join ""))
    )
    if ($EmbeddedLauncher -notmatch 'setgroups\(0, NULL\)' -or
        $EmbeddedLauncher -notmatch 'setresgid\(GRADER_ID, GRADER_ID, GRADER_ID\)' -or
        $EmbeddedLauncher -notmatch 'setresuid\(GRADER_ID, GRADER_ID, GRADER_ID\)' -or
        $EmbeddedLauncher -notmatch 'PR_SET_NO_NEW_PRIVS' -or
        $EmbeddedLauncher -notmatch '"tracked-lineage-roundtrip"' -or
        $EmbeddedLauncher -notmatch 'execve\(child_argv\[0\], child_argv, child_env\)') {
        throw "generated oracle launcher does not enforce the grader identity boundary"
    }

    $EvalDockerfile = Get-Content -Raw "$Root/evals/Dockerfile"
    if ($EvalDockerfile -match 'MYCELIUM_DURABLE_A17C|workflow-oracle\.ps1') {
        throw "source eval Dockerfile contains an embedded grading payload"
    }

    $SummaryFixture = Join-Path $Tmp "summary-integrity"
    $SummaryManifest = Join-Path $SummaryFixture "evals.json"
    New-Item -ItemType Directory -Path $SummaryFixture -Force | Out-Null
    [ordered]@{
        version = 7
        cases = @([ordered]@{ id = "integrity-case"; expected_script = "bin/mycelium-node.sh" })
    } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath $SummaryManifest
    foreach ($Arm in @("with-skill", "no-skill")) {
        $ResultDirectory = Join-Path $SummaryFixture $Arm
        $TrajectoryDirectory = Join-Path $ResultDirectory "trajectory"
        New-Item -ItemType Directory -Path $TrajectoryDirectory -Force | Out-Null
        [ordered]@{
            task_name = "integrity-case"
            skill_mode = $Arm
            rewards = [ordered]@{ reward = 1 }
            n_tool_calls = if ($Arm -eq "with-skill") { 3 } else { 2 }
            timing = [ordered]@{ agent_execution = 1 }
            error = ""
            error_category = ""
        } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $ResultDirectory "result.json")
        $Trajectory = if ($Arm -eq "with-skill") {
            @(
                [ordered]@{ type = "tool_call"; kind = "execute"; status = "completed"; title = "pwd && sed -n '1,220p' /opt/skills/mycelium/SKILL.md" },
                [ordered]@{ type = "tool_call"; kind = "execute"; status = "completed"; title = "bash /opt/skills/mycelium/bin/mycelium-node.sh" },
                [ordered]@{ type = "tool_call"; kind = "execute"; status = "completed"; title = "/usr/local/bin/mycelium-workflow-oracle integrity-case" }
            )
        }
        else {
            @(
                [ordered]@{ type = "prompt"; title = "Read /opt/skills/mycelium/SKILL.md and run /opt/skills/mycelium/bin/mycelium-node.sh" },
                [ordered]@{ type = "tool_call"; kind = "read"; status = "failed"; title = "/opt/skills/mycelium/SKILL.md" },
                [ordered]@{ type = "tool_call"; kind = "read"; status = "completed"; title = "/opt/skills/mycelium/bin/mycelium-node.sh" },
                [ordered]@{ type = "tool_call"; kind = "execute"; status = "failed"; title = "bash /opt/skills/mycelium/bin/mycelium-node.sh" },
                [ordered]@{ type = "tool_call"; kind = "execute"; status = "completed"; title = "true || bash /opt/skills/mycelium/bin/mycelium-node.sh" },
                [ordered]@{ type = "prompt"; title = "/usr/local/bin/mycelium-workflow-oracle integrity-case" },
                [ordered]@{ type = "tool_call"; kind = "execute"; status = "completed"; title = "/usr/local/bin/mycelium-workflow-oracle wrong-case" },
                [ordered]@{ type = "tool_call"; kind = "execute"; status = "completed"; title = 'python3 -c "print(open(''/opt/mycelium-grader/workflow-oracle.ps1'').read())"' }
            )
        }
        $Trajectory | ForEach-Object { $_ | ConvertTo-Json -Compress } |
            Set-Content -Encoding utf8 -LiteralPath (Join-Path $TrajectoryDirectory "acp_trajectory.jsonl")
    }
    & "$Root/evals/summarize-benchmark.ps1" -JobsDir $SummaryFixture -Manifest $SummaryManifest > $null
    $IntegritySummary = Get-Content -Raw -LiteralPath (Join-Path $SummaryFixture "mycelium-summary.json") | ConvertFrom-Json
    $BaselineIntegrityRow = $IntegritySummary.rows | Where-Object arm -eq "no-skill"
    if ($IntegritySummary.pairing_valid -or $null -ne $IntegritySummary.lift -or
        $IntegritySummary.with_skill.integrity_failures -ne 0 -or
        $IntegritySummary.no_skill.integrity_failures -ne 1 -or
        $BaselineIntegrityRow.skill_read -or $BaselineIntegrityRow.expected_script_used -or
        $BaselineIntegrityRow.oracle_invoked -or -not $BaselineIntegrityRow.grader_inspection) {
        throw "benchmark summary accepted baseline grader inspection"
    }

    $LegacyEvalDockerfile = Get-Content -Raw "$Root/evals/Dockerfile-v4"
    $OraclePayload = [regex]::Match($LegacyEvalDockerfile, '(?m)^ARG MYCELIUM_ORACLE_BASE64=(\S+)\r?$')
    if (-not $OraclePayload.Success) { throw "legacy eval Dockerfile is missing the embedded neutral oracle" }
    $EmbeddedOracle = [Convert]::FromBase64String($OraclePayload.Groups[1].Value)
    $CanonicalOracle = [IO.File]::ReadAllBytes("$Root/evals/benchmark-oracle.ps1")
    if ([Convert]::ToBase64String($EmbeddedOracle) -ne [Convert]::ToBase64String($CanonicalOracle)) {
        throw "eval Dockerfile neutral oracle payload is stale"
    }
    $LegacyVerifierPayload = [regex]::Match($LegacyEvalDockerfile, '(?m)^ARG MYCELIUM_CAP_VERIFIER_BASE64=(\S+)\r?$')
    if (-not $LegacyVerifierPayload.Success) { throw "legacy eval Dockerfile is missing the external CAP verifier" }
    $EmbeddedVerifier = [Convert]::FromBase64String($LegacyVerifierPayload.Groups[1].Value)
    $CanonicalVerifier = [IO.File]::ReadAllBytes("$Root/evals/verify-cap.py")
    if ([Convert]::ToBase64String($EmbeddedVerifier) -ne [Convert]::ToBase64String($CanonicalVerifier)) {
        throw "legacy eval Dockerfile CAP verifier payload is stale"
    }
    $LegacyLineagePayload = [regex]::Match($LegacyEvalDockerfile, '(?m)^ARG MYCELIUM_LINEAGE_VERIFIER_BASE64=(\S+)\r?$')
    if (-not $LegacyLineagePayload.Success) { throw "legacy eval Dockerfile is missing the evaluator-owned lineage verifier" }
    $EmbeddedLineage = [Convert]::FromBase64String($LegacyLineagePayload.Groups[1].Value)
    $CanonicalLineage = [IO.File]::ReadAllBytes("$Root/bin/mycelium_lineage.py")
    if ([Convert]::ToBase64String($EmbeddedLineage) -ne [Convert]::ToBase64String($CanonicalLineage)) {
        throw "legacy eval Dockerfile lineage verifier payload is stale"
    }
    if ($LegacyEvalDockerfile -notmatch '/opt/mycelium-eval/mycelium_lineage\.py' -or
        $LegacyEvalDockerfile -notmatch 'chmod 0444 /opt/mycelium-eval/benchmark-oracle\.ps1 /opt/mycelium-eval/verify-cap\.py /opt/mycelium-eval/mycelium_lineage\.py' -or
        $LegacyEvalDockerfile -notmatch '(?s)chmod 0444 .*?USER evaluator:evaluator\s+WORKDIR /app') {
        throw "legacy eval Dockerfile does not protect evaluator-owned verifier files"
    }

    $RetrievalEvalText = (& python "$Root/evals/run-retrieval.py") -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "retrieval evaluation thresholds failed" }
    $RetrievalEval = $RetrievalEvalText | ConvertFrom-Json
    if (-not $RetrievalEval.thresholds_met -or $RetrievalEval.query_count -lt 15 -or $RetrievalEval.query_count -gt 25) {
        throw "retrieval evaluation is not a frozen 15-25 query gate"
    }

    & python "$Root/tests/test_stage_treatment.py" > $null
    if ($LASTEXITCODE -ne 0) { throw "treatment staging tests failed" }
    & python "$Root/tests/test_node_writer.py" > $null
    if ($LASTEXITCODE -ne 0) { throw "node writer tests failed" }
    & python -B -m unittest discover -s "$Root/benchmarks/swebench_live" -p "test_*.py" > $null
    if ($LASTEXITCODE -ne 0) { throw "SWE-bench-Live tests failed" }

    Write-Output "Smoke tests passed"
}
finally {
    Set-Location $Root
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
