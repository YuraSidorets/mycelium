# keep piped candidate lines that match the term threshold
param(
    [Parameter(Mandatory=$true, Position=0)][string]$Goal,
    [Parameter(ValueFromPipeline=$true)][string]$Record
)

begin {
    $terms = $Goal.ToLowerInvariant().Split(" ", [StringSplitOptions]::RemoveEmptyEntries)
    $required = if ($terms.Count -gt 1) { 2 } else { 1 }
}

process {
    $parts = "$Record".Split("`t", 4)
    if ($parts.Count -lt 4) { continue }
    $text = $parts[3].ToLowerInvariant()
    $score = 0
    foreach ($term in $terms) {
        if ($text.Contains($term)) { $score++ }
    }
    if ($score -ge $required) {
        "septum`t$($parts[1])`t$($parts[2])`t$($parts[3])"
    }
}
