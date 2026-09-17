# This brief is for hosts without a native subagent primitive. Hosts with one
# (Claude Code `Agent`, plugin agents) dispatch directly and skip this script.
# This script prints a role brief; it has never dispatched anything.
param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet("apex", "septum", "hyphae", "stem", "cap")]
    [string]$Role,

    [Parameter(Mandatory=$true, Position=1)]
    [string]$Goal,

    [Parameter(Position=2)]
    [string]$Instance = "",

    [ValidateNotNullOrEmpty()]
    [string]$SpawnTool = "collaboration.spawn_agent",

    [string]$RunId = "",

    [string[]]$ProcessInputs = @()
)

$upper = $Role.ToUpperInvariant()
$nodeId = if ($Instance) { ($Role + "-" + ($Instance -replace "[^A-Za-z0-9_.-]+", "-").Trim("-")) } else { $Role }
$normalizedProcessInputs = @(
    $ProcessInputs |
        ForEach-Object { $_ -split "[,;]" } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)
$contracts = @{
    apex = "Many APEX workers are allowed when each has a separate search path. Gather only local evidence relevant to the goal. Evidence must name commands, files, URLs, or line references used."
    septum = "Many SEPTUM workers are allowed when each owns a separate filter boundary. Do not edit files. Pass only records that help the goal and name the kept/dropped boundary."
    hyphae = "Many HYPHAE workers are allowed when each digests a separate evidence bundle. Compress evidence, keep source paths and line numbers, and preserve uncertainty."
    stem = "Only one STEM coordinates the tracked run. Merge worker outputs, select the smallest useful fact set, surface conflicts, and decide when evidence is enough."
    cap = "CAP is mandatory. Many CAP workers are allowed when producing separate spores such as final answer, review, or verification. Include verification evidence or an explicit no-verification reason."
}

$instanceLine = if ($Instance) { "Instance: $Instance`nProducing Agent: $Instance`n" } else { "" }
$runLine = if ($RunId) { "Run ID: $RunId`n" } else { "Run ID: untracked`n" }
$processInputText = if ($normalizedProcessInputs.Count) { $normalizedProcessInputs -join "," } else { "none" }
$recordProcessInputs = ($normalizedProcessInputs | ForEach-Object { '"' + ($_ -replace '"', '`"') + '"' }) -join ","
$recordLineageArgs = if ($RunId) {
    $inputArgs = if ($recordProcessInputs) { " -ProcessInputs $recordProcessInputs" } else { "" }
    " -RunId `"$RunId`"$inputArgs"
} else {
    # Node writes are tracked by default, so an untracked brief has to print a
    # record command that names its own reason.
    " -AllowUntracked `"no run id supplied`""
}

@"
This brief is for hosts without a native subagent primitive. Hosts with one (Claude Code ``Agent``, plugin agents) dispatch directly and skip this script.

You are mycelium $upper.

Goal: $Goal
$instanceLine
$runLine
Process Inputs: $processInputText
Node ID: $nodeId
Parent Goal: $Goal
Scope: $Instance
Input command: follow the coordinator prompt for this role
Output contract:
- Node ID
- Run ID when supplied
- Process Inputs
- Status
- Topics
- Facts
- Evidence
- Source Refs
- Confidence
- Consumes
- Questions
- Blocks
- Next
- Trace when tools were used: Action; Observation; Result
Trace must name the invoked spawn tool and returned agent ID.
Wake condition: coordinator references this Node ID again
Spawn tool: $SpawnTool
Spawn rule: STEM invokes this capability with the brief using the tool's declared schema; scripts cannot spawn host subagents. Never spawn another STEM for the same tracked run.

$($contracts[$Role])

Record command:
bin/mycelium-node.ps1 "$Goal" "<node-id>" $Role <status> "<facts>" "<questions>" "<next>" -Topics "<topics>" -Evidence "<evidence>" -Confidence <confidence> -Consumes "<consumes>" -Blocks "<blocks>" -Trace "<trace>" -ProducingAgent "$Instance" -ParentGoalId "$Goal" -SourceRefs "<source-refs>" -Version "2"$recordLineageArgs

Record contract: role`tpath`tline`ttext

Return only your result. Do not include unrelated context.
"@
