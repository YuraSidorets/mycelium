param(
    [Parameter(Mandatory=$true, Position=0)][string]$Goal,
    [Parameter(Position=1)][string]$Topic = "",
    [switch]$Calibrated
)

$ErrorActionPreference = "Stop"

$indexPath = Join-Path ".mycelium" "index.json"
# ponytail: rebuild on every search; add write-time invalidation if node volume makes this measurable.
& (Join-Path $PSScriptRoot "mycelium-index.ps1") | Out-Null

if (-not (Test-Path -LiteralPath $indexPath)) { exit 0 }

$items = @(Get-Content -Raw -LiteralPath $indexPath | ConvertFrom-Json)
$terms = $Goal.ToLowerInvariant().Split(" ", [StringSplitOptions]::RemoveEmptyEntries)

function Get-NormalizedFact {
    param($Value)
    (("$Value" -replace "\s+", " ").Trim() -replace "^[-*]\s+", "")
}

function Resolve-FinalPath {
    param([string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    $current = $root
    foreach ($part in ($fullPath.Substring($root.Length) -split '[\\/]')) {
        if (-not $part) { continue }
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) { return $null }
        $item = Get-Item -Force -LiteralPath $current
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            $target = $item.ResolveLinkTarget($true)
            if (-not $target) { return $null }
            $current = $target.FullName
        }
    }
    [IO.Path]::GetFullPath($current)
}

function Test-SourceRefsCurrent {
    param($Item)
    $workspace = (Get-Location).Path
    $references = @("$($Item.source_refs)" -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne "none" })
    if (-not $references.Count) { return $false }
    $sourceCandidates = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($reference in $references) {
        if ($reference -notmatch '^(.+):(\d+)(?:-(\d+))?$') { return $false }
        $path = Resolve-FinalPath (Join-Path $workspace $matches[1])
        if (-not $path) { return $false }
        $relative = [IO.Path]::GetRelativePath($workspace, $path)
        if ([IO.Path]::IsPathRooted($relative) -or $relative -eq ".." -or $relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) { return $false }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
        $sourceLines = @(Get-Content -LiteralPath $path)
        $start = [int]$matches[2]
        $end = if ($matches[3]) { [int]$matches[3] } else { $start }
        if ($start -lt 1 -or $end -lt $start -or $end -gt $sourceLines.Count) { return $false }
        $sourceRange = @($sourceLines[($start - 1)..($end - 1)])
        foreach ($line in $sourceRange) {
            if (-not [string]::IsNullOrWhiteSpace($line)) { [void]$sourceCandidates.Add((Get-NormalizedFact $line)) }
        }
        [void]$sourceCandidates.Add((Get-NormalizedFact ($sourceRange -join " ")))
    }
    foreach ($fact in ("$($Item.facts)" -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        if (-not $sourceCandidates.Contains((Get-NormalizedFact $fact))) { return $false }
    }
    return $true
}

function ConvertTo-CompactField {
    param($Value)
    ("$Value" -replace "[\t\r\n]+", " ").Trim()
}

function Get-CalibrationPenalties {
    # A missing, unreadable, or malformed ledger yields no penalties, so
    # -Calibrated degrades to the default ranking instead of failing.
    $penalties = @{}
    $ledgerPath = Join-Path ".mycelium" "calibration.json"
    if (-not (Test-Path -LiteralPath $ledgerPath -PathType Leaf)) { return $penalties }
    try { $ledger = Get-Content -Raw -LiteralPath $ledgerPath | ConvertFrom-Json }
    catch { return $penalties }
    if ($null -eq $ledger -or $null -eq $ledger.by_confidence) { return $penalties }
    foreach ($bucket in $ledger.by_confidence.PSObject.Properties) {
        $rate = $bucket.Value.rate
        if ($null -eq $rate -or $rate -isnot [double] -and $rate -isnot [int] -and $rate -isnot [long] -and $rate -isnot [decimal]) { continue }
        $value = [double]$rate
        if ($value -lt 0) { $value = 0.0 }
        if ($value -gt 1) { $value = 1.0 }
        $penalties[$bucket.Name] = $value
    }
    return $penalties
}

function Get-CalibratedConfidenceRank {
    param([double]$Rank, [string]$Confidence, [hashtable]$Penalties)
    if (-not $Penalties.Count) { return $Rank }
    $bucket = "$Confidence".Trim().ToLowerInvariant()
    if (-not $bucket) { $bucket = "unset" }
    if (-not $Penalties.ContainsKey($bucket)) { return $Rank }
    return $Rank * (1.0 - $Penalties[$bucket])
}

$calibrationPenalties = if ($Calibrated) { Get-CalibrationPenalties } else { @{} }

$superseded = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($item in $items) {
    $sourceRefs = "$($item.source_refs)".Trim()
    if ($item.invalidated -eq "true" -or
        -not $item.flowCommitted -or
        $item.status -ne "complete" -or
        -not $sourceRefs -or
        $sourceRefs -eq "none") {
        continue
    }
    foreach ($targetId in ("$($item.supersedes)" -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne "none" })) {
        if ($targetId -cne $item.nodeId) { [void]$superseded.Add($targetId) }
    }
}

$ignored = @()
$ranked = foreach ($item in $items) {
    if ($Topic -and -not ("$($item.topics)".ToLowerInvariant().Contains($Topic.ToLowerInvariant()))) { continue }
    $evidence = ""
    if ($item.node_path -and (Test-Path -LiteralPath $item.node_path -PathType Leaf)) {
        $nodeText = Get-Content -Raw -LiteralPath $item.node_path
        $evidenceMatch = [regex]::Match($nodeText, '(?m)^Evidence:[ \t]*([^\r\n]*)\r?$')
        if ($evidenceMatch.Success) { $evidence = $evidenceMatch.Groups[1].Value }
    }
    $fields = [ordered]@{
        nodeId = "$($item.nodeId)"
        topics = "$($item.topics)"
        facts = "$($item.facts)"
        evidence = $evidence
        source_refs = "$($item.source_refs)"
        depends_on = "$($item.depends_on)"
        supersedes = "$($item.supersedes)"
        reflection = "$($item.reflection)"
    }
    $matchedTerms = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $matches = @()
    foreach ($field in $fields.Keys) {
        $value = $fields[$field].ToLowerInvariant()
        $fieldTerms = @($terms | Where-Object { $value.Contains($_) })
        if ($fieldTerms.Count) {
            $matches += "$($field):$($fieldTerms -join ',')"
            foreach ($term in $fieldTerms) { [void]$matchedTerms.Add($term) }
        }
    }
    if (-not $matchedTerms.Count) { continue }
    if ($item.invalidated -eq "true") {
        $ignored += "$($item.nodeId):invalidated"
        continue
    }
    if ($superseded.Contains("$($item.nodeId)")) {
        $ignored += "$($item.nodeId):superseded"
        continue
    }
    $isReflection = @("$($item.topics)".ToLowerInvariant() -split "," | ForEach-Object { $_.Trim() }) -contains "reflection"
    # A reflection records why an attempt failed, so it cites no source range
    # and mycelium-reflect never sets SourceRefs. Holding it to the currency
    # gate would drop every real reflection before ranking, which is exactly
    # what made the reflection-first tie-break dead code. The exemption is
    # scoped to search; stem.ps1 keeps the gate, and reflections are
    # status: blocked so they are already outside STEM's pool.
    if (-not $isReflection -and -not (Test-SourceRefsCurrent $item)) {
        $ignored += "$($item.nodeId):stale-source-refs"
        continue
    }
    $confidenceRank = switch ($item.confidence) { "high" { 3 } "medium" { 2 } "low" { 1 } default { 0 } }
    $confidenceRank = Get-CalibratedConfidenceRank $confidenceRank "$($item.confidence)" $calibrationPenalties
    [pscustomobject]@{
        IsReflection = $isReflection
        Score = $matchedTerms.Count
        ConfidenceRank = $confidenceRank
        MatchReason = $matches -join ";"
        Item = $item
    }
}

$ignoredTrace = if ($ignored.Count) { $ignored -join "," } else { "none" }
if ($ignored.Count) {
    [Console]::Error.WriteLine("trace`tquery=$(ConvertTo-CompactField $Goal)`tindex=v2`tignored=$ignoredTrace")
}
$ranked |
    Sort-Object -Property @{Expression="IsReflection";Descending=$true}, @{Expression="Score";Descending=$true}, @{Expression="ConfidenceRank";Descending=$true}, @{Expression={$_.Item.updated};Descending=$true} |
    Select-Object -First 10 |
    ForEach-Object {
        $i = $_.Item
        $dependedOnBy = @($i.depended_on_by) -join ","
        $supersededBy = @($i.superseded_by) -join ","
        @(
            (ConvertTo-CompactField $i.nodeId),
            (ConvertTo-CompactField $i.role),
            (ConvertTo-CompactField $i.status),
            (ConvertTo-CompactField $i.confidence),
            (ConvertTo-CompactField $i.topics),
            (ConvertTo-CompactField $i.firstFact),
            "match=$(ConvertTo-CompactField $_.MatchReason)",
            "query=$(ConvertTo-CompactField $Goal)",
            "source_refs=$(ConvertTo-CompactField $i.source_refs)",
            "depends_on=$(ConvertTo-CompactField $i.depends_on)",
            "consumes=$(ConvertTo-CompactField $i.consumes)",
            "supersedes=$(ConvertTo-CompactField $i.supersedes)",
            "depended_on_by=$(ConvertTo-CompactField $dependedOnBy)",
            "superseded_by=$(ConvertTo-CompactField $supersededBy)",
            "index=v$(ConvertTo-CompactField $i.indexVersion)",
            "ignored=$(ConvertTo-CompactField $ignoredTrace)"
        ) -join "`t"
    }
