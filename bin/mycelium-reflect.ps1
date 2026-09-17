param(
    [Parameter(Mandatory=$true, Position=0)][string]$Goal,
    [Parameter(Mandatory=$true, Position=1)][string]$FailedNodeId,
    [Parameter(Mandatory=$true, Position=2)][string]$FailureSummary,
    [Parameter(Mandatory=$true, Position=3)][string]$NextAttempt
)

$ErrorActionPreference = "Stop"

$safeFailed = ($FailedNodeId -replace "[^A-Za-z0-9_.-]+", "-").Trim("-")
if (-not $safeFailed) { $safeFailed = "node" }
$reflectionId = "reflection-$safeFailed"

& (Join-Path $PSScriptRoot "mycelium-node.ps1") `
    $Goal `
    $reflectionId `
    cap `
    blocked `
    $FailureSummary `
    "" `
    $NextAttempt `
    -Topics "reflection,verification" `
    -Evidence $FailureSummary `
    -Confidence medium `
    -Consumes $safeFailed `
    -Blocks $FailureSummary `
    -Reflection $NextAttempt `
    -AllowUntracked "reflection" | Out-Null

& (Join-Path $PSScriptRoot "mycelium-digest.ps1") | Out-Null

$reflectionId
