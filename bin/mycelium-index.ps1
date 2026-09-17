#requires -Version 7.5
param()

$ErrorActionPreference = "Stop"

function Get-NodeMetadata {
    param([string]$Text)
    $meta = @{}
    $lines = @($Text -split "`r?`n")
    if (-not $lines.Count -or $lines[0].Trim() -ne "---") { throw "node is missing frontmatter" }
    $closed = $false
    foreach ($line in ($lines | Select-Object -Skip 1)) {
        if ($line.Trim() -eq "---") { $closed = $true; break }
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch "^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$") { throw "invalid frontmatter line: $line" }
        $key = $matches[1].ToLowerInvariant()
        if ($meta.ContainsKey($key)) { throw "duplicate metadata key: $key" }
        $meta[$key] = $matches[2]
    }
    if (-not $closed) { throw "node frontmatter is not closed" }
    $meta
}

function Get-NodeBodyField {
    param([string]$Text, [string]$Name)
    $header = ($Text -split '(?m)^## Facts\s*$', 2)[0]
    $found = [regex]::Matches($header, "(?m)^$([regex]::Escape($Name)):[ \t]*([^\r\n]*)\r?$")
    if ($found.Count -ne 1) { throw "node must contain exactly one $Name field" }
    $found[0].Groups[1].Value
}

function Get-NodeSection {
    param([string]$Text, [string]$Name)
    $found = [regex]::Match($Text, "(?ms)^## $([regex]::Escape($Name))[ \t]*\r?\n(.*?)(?=^## |\z)")
    if ($found.Success) { return $found.Groups[1].Value.Trim() }
    ""
}

function Get-CanonicalLines {
    param($Values, [bool]$StripBullets)
    $normalized = foreach ($value in @($Values)) {
        foreach ($line in ("$value" -split "`r?`n")) {
            $fact = ($line -replace "\s+", " ").Trim()
            if ($StripBullets) { $fact = $fact -replace "^[-*]\s+", "" }
            if ($fact) { $fact }
        }
    }
    (@($normalized) -join [char]31)
}

function Get-CanonicalFacts {
    param($Facts)
    Get-CanonicalLines $Facts $true
}

function Get-CanonicalTimestamp {
    param($Value)
    $text = "$Value"
    if ($text -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00))$') {
        throw "invalid flow timestamp: $text"
    }
    try {
        $parsed = [DateTimeOffset]::Parse($text, [Globalization.CultureInfo]::InvariantCulture)
    }
    catch {
        throw "invalid flow timestamp: $text"
    }
    $parsed.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture)
}

function ConvertFrom-StrictJson {
    param([string]$Json)
    $document = [Text.Json.JsonDocument]::Parse($Json)
    try {
        if ($document.RootElement.ValueKind -ne [Text.Json.JsonValueKind]::Object) {
            throw "flow record must be a JSON object"
        }
        $keys = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach ($property in $document.RootElement.EnumerateObject()) {
            if (-not $keys.Add($property.Name)) { throw "duplicate JSON key: $($property.Name)" }
        }
    }
    finally {
        $document.Dispose()
    }
    $Json | ConvertFrom-Json -AsHashtable -DateKind String
}

function Get-FlowCommitKey {
    param(
        $NodeId, $Goal, $Timestamp, $Role, $Status, $Topics, $Evidence,
        $Confidence, $Consumes, $Blocks, $Trace, $Reflection, $ParentGoalId,
        $ProducingAgent, $Version, $SourceRefs, $DependsOn, $Invalidated,
        $Supersedes, $Facts, $Questions, $Next
    )
    $normalizedTimestamp = Get-CanonicalTimestamp $Timestamp
    (@(
        "$NodeId", "$Goal", $normalizedTimestamp, "$Role", "$Status", "$Topics", "$Evidence",
        "$Confidence", "$Consumes", "$Blocks", "$Trace", "$Reflection", "$ParentGoalId",
        "$ProducingAgent", "$Version", "$SourceRefs", "$DependsOn",
        "$Invalidated".ToLowerInvariant(), "$Supersedes", (Get-CanonicalFacts $Facts),
        (Get-CanonicalLines $Questions $false), (Get-CanonicalLines $Next $false)
    ) -join "`0")
}

$nodeDir = Join-Path ".mycelium" "nodes"
$indexPath = Join-Path ".mycelium" "index.json"
if (-not (Test-Path -LiteralPath ".mycelium")) {
    New-Item -ItemType Directory -Path ".mycelium" -Force | Out-Null
}

$items = @()
$flowCommits = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$flowDir = Join-Path ".mycelium" "flows"
if (Test-Path -LiteralPath $flowDir -PathType Container) {
    foreach ($flowFile in Get-ChildItem -LiteralPath $flowDir -Filter "*.jsonl" -File) {
        foreach ($line in Get-Content -LiteralPath $flowFile.FullName) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $event = ConvertFrom-StrictJson $line
            foreach ($field in @(
                "nodeId", "goal", "timestamp", "role", "status", "topics", "evidence",
                "confidence", "consumes", "blocks", "trace", "reflection", "parentGoalId",
                "producingAgent", "version", "sourceRefs", "dependsOn", "supersedes"
            )) {
                if (-not $event.ContainsKey($field) -or $event[$field] -isnot [string]) {
                    throw "flow $field must be a JSON string"
                }
            }
            if (-not $event.ContainsKey("invalidated") -or $event.invalidated -isnot [bool]) {
                throw "flow invalidated must be a JSON boolean"
            }
            foreach ($field in @("facts", "questions", "next")) {
                if (-not $event.ContainsKey($field)) { throw "missing flow field: $field" }
                $value = $event[$field]
                if ($null -eq $value -and $field -ne "facts") { continue }
                if ($null -eq $value -or
                    ($value -isnot [string] -and $value -isnot [array]) -or
                    @($value | Where-Object { $_ -isnot [string] }).Count) {
                    throw "flow $field must be a JSON string or string array"
                }
            }
            $key = Get-FlowCommitKey `
                $event.nodeId $event.goal $event.timestamp $event.role $event.status `
                $event.topics $event.evidence $event.confidence $event.consumes $event.blocks `
                $event.trace $event.reflection $event.parentGoalId $event.producingAgent `
                $event.version $event.sourceRefs $event.dependsOn $event.invalidated `
                $event.supersedes $event.facts $event.questions $event.next
            [void]$flowCommits.Add($key)
        }
    }
}
$skipped = 0
if (Test-Path -LiteralPath $nodeDir) {
    foreach ($file in Get-ChildItem -LiteralPath $nodeDir -Filter "*.md" -File) {
        $text = Get-Content -Raw -LiteralPath $file.FullName
        try {
            $meta = Get-NodeMetadata $text
        }
        catch {
            Write-Warning "skipped $($file.Name): $_"
            $skipped++
            continue
        }
        $facts = Get-NodeSection $text "Facts"
        $questions = Get-NodeSection $text "Questions"
        $next = Get-NodeSection $text "Next"
        $requiredNodeFields = @(
            "nodeid", "goal", "updated", "role", "status", "topics", "confidence",
            "consumes", "blocks", "parent_goal_id", "producing_agent", "version",
            "source_refs", "depends_on", "invalidated", "supersedes"
        )
        if (@($requiredNodeFields | Where-Object { -not $meta.ContainsKey($_) }).Count -or
            (-not [string]::Equals($meta["invalidated"], "true", [StringComparison]::Ordinal) -and
             -not [string]::Equals($meta["invalidated"], "false", [StringComparison]::Ordinal))) {
            continue
        }
        $normalizedUpdated = $null
        $reflection = ""
        try {
            $normalizedUpdated = Get-CanonicalTimestamp $meta["updated"]
            $reflection = Get-NodeBodyField $text "Reflection"
            $flowKey = Get-FlowCommitKey `
                $meta["nodeid"] $meta["goal"] $meta["updated"] $meta["role"] $meta["status"] `
                $meta["topics"] (Get-NodeBodyField $text "Evidence") $meta["confidence"] `
                $meta["consumes"] $meta["blocks"] (Get-NodeBodyField $text "Trace") `
                $reflection $meta["parent_goal_id"] `
                $meta["producing_agent"] $meta["version"] $meta["source_refs"] `
                $meta["depends_on"] $meta["invalidated"] $meta["supersedes"] `
                $facts $questions $next
        }
        catch { continue }
        if (-not $flowCommits.Contains($flowKey)) { continue }
        $firstFact = (($facts -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -First 1)
        $items += [pscustomobject]@{
            nodeId = if ($meta["nodeid"]) { $meta["nodeid"] } else { $file.BaseName }
            role = $meta["role"]
            status = $meta["status"]
            topics = $meta["topics"]
            confidence = $meta["confidence"]
            updated = $normalizedUpdated
            consumes = $meta["consumes"]
            blocks = $meta["blocks"]
            parent_goal_id = $meta["parent_goal_id"]
            producing_agent = $meta["producing_agent"]
            version = $meta["version"]
            source_refs = $meta["source_refs"]
            depends_on = $meta["depends_on"]
            invalidated = $meta["invalidated"]
            supersedes = $meta["supersedes"]
            # Optional since brief 07: nodes written before the tracked-by-default
            # flip carry no reason, and legacy nodes must stay indexable.
            untracked_reason = if ($meta.ContainsKey("untracked_reason")) { $meta["untracked_reason"] } else { "" }
            reflection = $reflection
            flowCommitted = $true
            firstFact = $firstFact
            facts = $facts
            node_path = ".mycelium/nodes/$($file.Name)"
            depended_on_by = @()
            superseded_by = @()
            indexVersion = 2
        }
    }
}
if ($skipped -gt 0) {
    Write-Warning "$skipped node(s) skipped (run mycelium-lint for detail)"
}

$itemById = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
foreach ($item in $items) {
    $itemById[$item.nodeId] = $item
}
foreach ($item in $items) {
    foreach ($targetId in ("$($item.depends_on)" -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne "none" })) {
        if ($itemById.ContainsKey($targetId) -and $itemById[$targetId].depended_on_by -cnotcontains $item.nodeId) {
            $itemById[$targetId].depended_on_by += $item.nodeId
        }
    }
    foreach ($targetId in ("$($item.supersedes)" -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne "none" })) {
        if ($itemById.ContainsKey($targetId) -and $itemById[$targetId].superseded_by -cnotcontains $item.nodeId) {
            $itemById[$targetId].superseded_by += $item.nodeId
        }
    }
}

ConvertTo-Json -InputObject @($items) -Depth 4 | Set-Content -Encoding utf8 -Path $indexPath
$indexPath.Replace("\", "/")
