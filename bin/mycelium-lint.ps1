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

$violations = [Collections.Generic.List[string]]::new()

$nodeDir = Join-Path ".mycelium" "nodes"
if (Test-Path -LiteralPath $nodeDir -PathType Container) {
    foreach ($file in (Get-ChildItem -LiteralPath $nodeDir -Filter "*.md" -File | Sort-Object Name)) {
        $text = Get-Content -Raw -LiteralPath $file.FullName
        $relativePath = ".mycelium/nodes/$($file.Name)"
        try {
            $meta = Get-NodeMetadata $text
        }
        catch {
            $violations.Add("${relativePath}: $($_.Exception.Message)")
            continue
        }
        $missing = @()
        foreach ($pair in @(@("nodeid", "nodeId"), @("role", "role"), @("status", "status"))) {
            if (-not $meta.ContainsKey($pair[0])) { $missing += $pair[1] }
        }
        if ($missing.Count) {
            $violations.Add("${relativePath}: missing required field(s): $($missing -join ', ')")
        }
    }
}

$flowDir = Join-Path ".mycelium" "flows"
if (Test-Path -LiteralPath $flowDir -PathType Container) {
    foreach ($file in (Get-ChildItem -LiteralPath $flowDir -Filter "*.jsonl" -File | Sort-Object Name)) {
        $relativePath = ".mycelium/flows/$($file.Name)"
        $lineNumber = 0
        foreach ($line in Get-Content -LiteralPath $file.FullName) {
            $lineNumber++
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $document = $null
            try {
                $document = [Text.Json.JsonDocument]::Parse($line)
            }
            catch {
                $violations.Add("${relativePath}:${lineNumber}: $($_.Exception.Message)")
                continue
            }
            try {
                if ($document.RootElement.ValueKind -ne [Text.Json.JsonValueKind]::Object) {
                    $violations.Add("${relativePath}:${lineNumber}: flow record must be a JSON object")
                    continue
                }
                $keys = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                foreach ($property in $document.RootElement.EnumerateObject()) {
                    [void]$keys.Add($property.Name)
                }
                $missing = @()
                foreach ($required in @("timestamp", "nodeId", "role", "status")) {
                    if (-not $keys.Contains($required)) { $missing += $required }
                }
                if ($missing.Count) {
                    $violations.Add("${relativePath}:${lineNumber}: missing required field(s): $($missing -join ', ')")
                }
            }
            finally {
                if ($document) { $document.Dispose() }
            }
        }
    }
}

foreach ($violation in $violations) {
    $violation
}
if ($violations.Count) { exit 1 }
exit 0
