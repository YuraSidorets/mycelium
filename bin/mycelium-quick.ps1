param(
    [Parameter(Mandatory)][string]$Goal,
    [string]$RunId = "",
    [Parameter(Mandatory)][string]$ApexFacts,
    [Parameter(Mandatory)][string]$ApexEvidence,
    [Parameter(Mandatory)][string]$StemFacts,
    [Parameter(Mandatory)][string]$CapFacts,
    [string]$Topics = "quick",
    [ValidateSet("low","medium","high")][string]$Confidence = "high"
)

$ErrorActionPreference = "Stop"

$lineageScript = Join-Path $PSScriptRoot "mycelium-lineage.ps1"
$nodeScript = Join-Path $PSScriptRoot "mycelium-node.ps1"

if (-not $RunId) {
    $slug = ($Goal.ToLowerInvariant() -replace "[^a-z0-9]+", "-").Trim("-")
    if (-not $slug) { $slug = "goal" }
    $suffix = [System.Guid]::NewGuid().ToString("N").Substring(0, 6)
    $RunId = "$slug-$suffix"
}

$apexNodeId = "$RunId-apex"
$stemNodeId = "$RunId-stem"
$capNodeId = "$RunId-cap"

& $lineageScript begin --goal $Goal --run-id $RunId --topology-preset apex-stem-cap | Out-Null

& $nodeScript $Goal $apexNodeId "apex" "complete" $ApexFacts "none" "none" `
    -Topics $Topics -Evidence $ApexEvidence -Confidence $Confidence `
    -Consumes "none" -Blocks "none" -RunId $RunId | Out-Null

& $nodeScript $Goal $stemNodeId "stem" "complete" $StemFacts "none" "none" `
    -Topics $Topics -Evidence $apexNodeId -Confidence $Confidence `
    -Consumes $apexNodeId -Blocks "none" -RunId $RunId `
    -ProcessInputs @($apexNodeId) | Out-Null

& $nodeScript $Goal $capNodeId "cap" "complete" $CapFacts "none" "host: run external verification" `
    -Topics $Topics -Evidence $stemNodeId -Confidence $Confidence `
    -Consumes $stemNodeId -Blocks "none" -RunId $RunId `
    -ProcessInputs @($stemNodeId) | Out-Null

& $lineageScript seal --run-id $RunId
