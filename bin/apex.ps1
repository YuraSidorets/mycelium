# print candidate lines that contain at least one goal term
param([Parameter(Mandatory=$true)][string]$Goal)
$ErrorActionPreference = "Stop"

$terms = $Goal.ToLowerInvariant().Split(" ", [StringSplitOptions]::RemoveEmptyEntries)
$files = @(git ls-files --cached --others --exclude-standard 2>$null | Sort-Object -Unique)
if (-not $files) {
    $files = Get-ChildItem -Recurse -File | Where-Object { $_.FullName -notmatch "\\.git\\" } | ForEach-Object { $_.FullName }
}

foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
    $lineNo = 0
    foreach ($line in Get-Content -LiteralPath $file -ErrorAction SilentlyContinue) {
        $lineNo++
        $lower = $line.ToLowerInvariant()
        $score = 0
        foreach ($term in $terms) {
            if ($lower.Contains($term)) { $score++ }
        }
        if ($score -gt 0) {
            "apex`t$file`t$lineNo`t$($line.Trim())"
        }
    }
}
