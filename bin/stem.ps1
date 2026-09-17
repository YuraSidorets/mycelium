#requires -Version 7.5
param(
    [Parameter(Mandatory=$true, Position=0)][string]$Goal,
    [Parameter(ValueFromPipeline=$true)][string]$Record,
    [switch]$ForCap
)

begin {
    $terms = $Goal.ToLowerInvariant().Split(" ", [StringSplitOptions]::RemoveEmptyEntries)
    $requiredTermMatches = if ($terms.Count -gt 1) { 2 } else { 1 }
    $seen = @{}
    $rows = @()
    $candidateIds = [Collections.Generic.List[string]]::new()

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

    function Get-IsClaims {
        param($Node)
        foreach ($line in ("$($Node.Facts)" -split "`r?`n")) {
            if ($line -match "^\s*[-*]?\s*(.+?)\s+is\s+(.+?)\.?\s*$") {
                [pscustomobject]@{
                    Subject = $matches[1].Trim().ToLowerInvariant()
                    Value = $matches[2].Trim()
                    NodeId = $Node.NodeId
                }
            }
        }
    }

    function Get-RelationIds {
        param($Value)
        "$Value" -split "[,;]" |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and $_ -ne "none" }
    }

    function Test-CurrentNode {
        param($Node)
        $sourceRefs = "$($Node.SourceRefs)".Trim()
        return (
            -not $Node.Invalidated -and
            $Node.FlowCommitted -and
            $Node.Status -eq "complete" -and
            $sourceRefs -and
            $sourceRefs -ne "none"
        )
    }

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
        param($Node)
        $workspace = (Get-Location).Path
        $references = @(Get-RelationIds $Node.SourceRefs)
        if (-not $references.Count) { return $false }
        $sourceCandidates = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach ($reference in $references) {
            if ($reference -notmatch '^(.+):(\d+)(?:-(\d+))?$') { return $false }
            $path = Resolve-FinalPath (Join-Path $workspace $matches[1])
            if (-not $path) { return $false }
            $relative = [IO.Path]::GetRelativePath($workspace, $path)
            if ([IO.Path]::IsPathRooted($relative) -or $relative -eq ".." -or $relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) {
                return $false
            }
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
            $sourceLines = @(Get-Content -LiteralPath $path)
            $start = [int]$matches[2]
            $end = if ($matches[3]) { [int]$matches[3] } else { $start }
            if ($start -lt 1 -or $end -lt $start -or $end -gt $sourceLines.Count) { return $false }
            $sourceRange = @($sourceLines[($start - 1)..($end - 1)])
            foreach ($line in $sourceRange) {
                if (-not [string]::IsNullOrWhiteSpace($line)) {
                    [void]$sourceCandidates.Add((Get-NormalizedFact $line))
                }
            }
            [void]$sourceCandidates.Add((Get-NormalizedFact ($sourceRange -join " ")))
        }
        foreach ($fact in $Node.FactRows) {
            if (-not $sourceCandidates.Contains((Get-NormalizedFact $fact.Text))) { return $false }
        }
        return $true
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

    function Write-StemLedger {
        param($SelectedNodes, $IgnoredNodes, $Conflicts, [string]$CapDispatch)
        $ledgerDir = Join-Path ".mycelium" "flows"
        New-Item -ItemType Directory -Path $ledgerDir -Force | Out-Null
        $ledgerPath = Join-Path $ledgerDir ("stem-ledger-{0}.json" -f (Get-Date -Format "yyyyMMddHHmmssfff"))
        [ordered]@{
            goal = $Goal
            selected_nodes = @($SelectedNodes)
            ignored_nodes = @($IgnoredNodes)
            conflicts = @($Conflicts)
            cap_dispatch = $CapDispatch
            created = (Get-Date).ToUniversalTime().ToString("o")
        } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -Path $ledgerPath
    }
}

process {
    $candidate = "$Record".Split("`t")
    $candidatePrefixes = @(
        "match=", "query=", "source_refs=", "depends_on=", "consumes=",
        "supersedes=", "depended_on_by=", "superseded_by=", "index=", "ignored="
    )
    $traceColumns = @($candidate | Where-Object {
        $value = "$_"
        @($candidatePrefixes | Where-Object { $value.StartsWith($_) }).Count
    })
    if ($traceColumns.Count) {
        $validCandidate = $candidate.Count -eq 16
        foreach ($index in 0..($candidatePrefixes.Count - 1)) {
            $validCandidate = $validCandidate -and $candidate[$index + 6].StartsWith($candidatePrefixes[$index])
        }
        if (-not $validCandidate) { throw "malformed search candidate" }
        if (-not $candidateIds.Contains($candidate[0])) { $candidateIds.Add($candidate[0]) }
    } else {
        $parts = "$Record".Split("`t", 4)
        if ($parts.Count -ge 4) {
            $fact = $parts[3]
            if (-not $seen.ContainsKey($fact)) {
                $seen[$fact] = $true
                $score = 0
                $lower = $fact.ToLowerInvariant()
                foreach ($term in $terms) {
                    if ($lower.Contains($term)) { $score++ }
                }
                $rows += [pscustomobject]@{ Score = $score; Path = $parts[1]; Line = $parts[2]; Fact = $fact }
            }
        }
    }
}

end {
    if ($rows.Count -gt 0) {
        $selectedRows = @($rows |
            Sort-Object -Property @{Expression="Score";Descending=$true}, Path, Line |
            Select-Object -First 10)
        Write-StemLedger -SelectedNodes @($selectedRows | ForEach-Object { "$($_.Path):$($_.Line)" }) -IgnoredNodes @() -Conflicts @() -CapDispatch "use selected pipeline facts for the spore and verification."
        $selectedRows | ForEach-Object { "stem`t$($_.Path)`t$($_.Line)`t$($_.Fact)" }
        return
    }

    $nodeDir = Join-Path ".mycelium" "nodes"
    if (-not (Test-Path -LiteralPath $nodeDir)) { return }

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

    $indexedSupersededNodeIds = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $candidatePaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    if ($candidateIds.Count) {
        $indexPath = Join-Path ".mycelium" "index.json"
        if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
            throw "candidate input requires .mycelium/index.json"
        }
        $indexItems = @(Get-Content -Raw -LiteralPath $indexPath | ConvertFrom-Json)
        $indexById = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
        foreach ($item in $indexItems) {
            $indexById[$item.nodeId] = $item
            $sourceRefs = "$($item.source_refs)".Trim()
            if ($item.invalidated -eq "true" -or
                -not $item.flowCommitted -or
                $item.status -ne "complete" -or
                -not $sourceRefs -or
                $sourceRefs -eq "none") {
                continue
            }
            foreach ($targetId in (Get-RelationIds $item.supersedes)) {
                if ($targetId -cne $item.nodeId) { [void]$indexedSupersededNodeIds.Add($targetId) }
            }
        }
        $directCandidateIds = @($candidateIds | Select-Object -First 5)
        $requestedIds = [Collections.Generic.List[string]]::new()
        foreach ($candidateId in $directCandidateIds) { $requestedIds.Add($candidateId) }
        foreach ($candidateId in $directCandidateIds) {
            if (-not $indexById.ContainsKey($candidateId)) { continue }
            foreach ($dependencyId in (Get-RelationIds $indexById[$candidateId].depends_on)) {
                if ($requestedIds.Count -ge 10) { break }
                if (-not $requestedIds.Contains($dependencyId)) { $requestedIds.Add($dependencyId) }
            }
        }
        foreach ($nodeId in $requestedIds) {
            if ($indexById.ContainsKey($nodeId)) { [void]$candidatePaths.Add("$($indexById[$nodeId].node_path)") }
        }
    }

    $roleRank = @{ hyphae = 3; septum = 2; apex = 1; stem = 0; cap = 0 }
    $confidenceRank = @{ high = 3; medium = 2; low = 1; "" = 0 }
    $nodeFiles = @(Get-ChildItem -LiteralPath $nodeDir -Filter "*.md" -File)
    if ($candidateIds.Count) {
        $nodeFiles = @($nodeFiles | Where-Object { $candidatePaths.Contains(".mycelium/nodes/$($_.Name)") })
    }
    $skipped = 0
    $nodes = foreach ($file in $nodeFiles) {
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
        $factRows = @()
        $inFacts = $false
        $lineNumber = 0
        foreach ($line in ($text -split "`r?`n")) {
            $lineNumber++
            if ($line.Trim() -eq "## Facts") { $inFacts = $true; continue }
            if ($inFacts -and $line -match "^## ") { break }
            if ($inFacts -and -not [string]::IsNullOrWhiteSpace($line)) {
                $factRows += [pscustomobject]@{ Line = $lineNumber; Text = $line }
            }
        }
        $haystack = (($meta["nodeid"], $meta["topics"], $facts) -join " ").ToLowerInvariant()
        $flowKey = $null
        $normalizedUpdated = $null
        try {
            $normalizedUpdated = Get-CanonicalTimestamp $meta["updated"]
            $flowKey = Get-FlowCommitKey `
                $meta["nodeid"] $meta["goal"] $meta["updated"] $meta["role"] $meta["status"] `
                $meta["topics"] (Get-NodeBodyField $text "Evidence") $meta["confidence"] `
                $meta["consumes"] $meta["blocks"] (Get-NodeBodyField $text "Trace") `
                (Get-NodeBodyField $text "Reflection") $meta["parent_goal_id"] `
                $meta["producing_agent"] $meta["version"] $meta["source_refs"] `
                $meta["depends_on"] $meta["invalidated"] $meta["supersedes"] `
                $facts $questions $next
        }
        catch { }
        $score = 0
        foreach ($term in $terms) {
            if ($haystack.Contains($term)) { $score++ }
        }
        [pscustomobject]@{
            NodeId = if ($meta["nodeid"]) { $meta["nodeid"] } else { $file.BaseName }
            Role = $meta["role"]
            Status = $meta["status"]
            Topics = $meta["topics"]
            Confidence = $meta["confidence"]
            SourceRefs = $meta["source_refs"]
            DependsOn = $meta["depends_on"]
            Supersedes = $meta["supersedes"]
            Invalidated = "$($meta["invalidated"])".ToLowerInvariant() -eq "true"
            Updated = $normalizedUpdated
            Facts = $facts
            FactRows = @($factRows)
            FlowCommitted = $null -ne $flowKey -and $flowCommits.Contains($flowKey)
            NodePath = ".mycelium/nodes/$($file.Name)"
            SourceCurrent = $false
            Score = $score
            ConfidenceRank = if ($confidenceRank.ContainsKey($meta["confidence"])) { $confidenceRank[$meta["confidence"]] } else { 0 }
            RoleRank = if ($roleRank.ContainsKey($meta["role"])) { $roleRank[$meta["role"]] } else { 0 }
        }
    }
    if ($skipped -gt 0) {
        Write-Warning "$skipped node(s) skipped (run mycelium-lint for detail)"
    }

    $supersededNodeIds = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($nodeId in $indexedSupersededNodeIds) { [void]$supersededNodeIds.Add($nodeId) }
    foreach ($node in $nodes) {
        $node.SourceCurrent = Test-SourceRefsCurrent $node
        if (-not (Test-CurrentNode $node)) { continue }
        foreach ($targetId in (Get-RelationIds $node.Supersedes)) {
            if ($targetId -cne $node.NodeId) { [void]$supersededNodeIds.Add($targetId) }
        }
    }

    $nodesById = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    foreach ($node in $nodes) { $nodesById[$node.NodeId] = $node }
    if ($candidateIds.Count) {
        $ranked = @()
        foreach ($nodeId in ($candidateIds | Select-Object -First 5)) {
            if (-not $nodesById.ContainsKey($nodeId)) { continue }
            $node = $nodesById[$nodeId]
            if ((Test-CurrentNode $node) -and
                -not $supersededNodeIds.Contains($nodeId) -and
                (-not $ForCap -or $node.SourceCurrent)) {
                $ranked += $node
            }
        }
    } else {
        $ranked = @($nodes |
            Where-Object {
                (Test-CurrentNode $_) -and
                -not $supersededNodeIds.Contains($_.NodeId) -and
                $_.Score -ge $requiredTermMatches -and
                (-not $ForCap -or $_.SourceCurrent)
            } |
            Sort-Object -Property @{Expression="Score";Descending=$true}, @{Expression="ConfidenceRank";Descending=$true}, @{Expression="RoleRank";Descending=$true}, @{Expression="Updated";Descending=$true}, NodeId)
    }
    $directSelected = @($ranked | Select-Object -First 5)
    $selected = @($directSelected)
    $selectedIds = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($node in $selected) {
        [void]$selectedIds.Add($node.NodeId)
    }
    foreach ($node in $directSelected) {
        foreach ($dependencyId in (Get-RelationIds $node.DependsOn)) {
            if ($selected.Count -ge 10) { break }
            if (-not $nodesById.ContainsKey($dependencyId) -or
                $selectedIds.Contains($dependencyId) -or
                $supersededNodeIds.Contains($dependencyId)) {
                continue
            }
            $dependency = $nodesById[$dependencyId]
            if ((Test-CurrentNode $dependency) -and (-not $ForCap -or $dependency.SourceCurrent)) {
                $selected += $dependency
                [void]$selectedIds.Add($dependencyId)
            }
        }
    }
    $ignored = @($nodes | Where-Object { -not $selectedIds.Contains($_.NodeId) } | Select-Object -First 5)

    $claims = @{}
    $conflicts = @()
    foreach ($claim in ($selected | ForEach-Object { Get-IsClaims $_ })) {
        if ($claims.ContainsKey($claim.Subject) -and $claims[$claim.Subject].Value -ne $claim.Value) {
            $conflicts += "$($claim.Subject) differs: $($claims[$claim.Subject].NodeId) says $($claims[$claim.Subject].Value); $($claim.NodeId) says $($claim.Value)"
        } else {
            $claims[$claim.Subject] = $claim
        }
    }

    $sourceRefs = @{}
    $duplicateSources = @()
    foreach ($node in $selected) {
        foreach ($sourceRef in ("$($node.SourceRefs)" -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
            if (-not $sourceRefs.ContainsKey($sourceRef)) { $sourceRefs[$sourceRef] = @() }
            $sourceRefs[$sourceRef] += $node.NodeId
        }
    }
    foreach ($sourceRef in $sourceRefs.Keys) {
        $nodeIds = @($sourceRefs[$sourceRef] | Select-Object -Unique)
        if ($nodeIds.Count -gt 1) {
            $duplicateSources += "$sourceRef cited by $($nodeIds -join ', ')"
        }
    }

    $capDispatch = "use selected node IDs and node-file paths for the spore and verification."
    Write-StemLedger -SelectedNodes @($selected | ForEach-Object { $_.NodeId }) -IgnoredNodes @($ignored | ForEach-Object { $_.NodeId }) -Conflicts $conflicts -CapDispatch $capDispatch

    if ($ForCap) {
        if ($conflicts.Count) {
            "Conflicts:"
            $conflicts | ForEach-Object { "- $_" }
            return
        }
        foreach ($node in $selected) {
            foreach ($fact in $node.FactRows) {
                "stem`t$($node.NodePath)`t$($fact.Line)`t[$($node.NodeId)] $($fact.Text)"
            }
        }
        return
    }

    "Goal: $Goal"
    "Selected nodes:"
    foreach ($node in $selected) {
        "- $($node.NodeId) [$($node.Role), $($node.Confidence)] $($node.Facts)"
    }
    "Ignored nodes:"
    foreach ($node in $ignored) {
        $reason = if ($node.Invalidated) {
            "invalidated"
        } elseif ($supersededNodeIds.Contains($node.NodeId)) {
            "superseded"
        } elseif (-not $node.FlowCommitted) {
            "missing flow commit"
        } elseif ($node.Status -ne "complete") {
            "status $($node.Status)"
        } elseif ($ForCap -and -not $node.SourceCurrent) {
            "stale source refs"
        } elseif ($node.Score -lt $requiredTermMatches) {
            "insufficient goal match"
        } elseif ([string]::IsNullOrWhiteSpace("$($node.SourceRefs)")) {
            "missing source refs"
        } else {
            "lower ranked"
        }
        "- $($node.NodeId) [$($node.Role), $($node.Confidence)] $reason"
    }
    if ($conflicts.Count) {
        "Conflicts:"
        $conflicts | ForEach-Object { "- $_" }
    } else {
        "Conflicts: none found in selected nodes"
    }
    if ($duplicateSources.Count) {
        "Duplicate source warnings:"
        $duplicateSources | ForEach-Object { "- $_" }
    } else {
        "Duplicate source warnings: none"
    }
    "Enough evidence because: selected nodes match the goal terms and include durable evidence for CAP."
    "Missing but not needed: no further discovery required before CAP unless CAP verification fails."
    "Next role: CAP"
    "CAP dispatch: $capDispatch"
}

