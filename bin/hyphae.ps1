# normalize whitespace in piped candidate lines
param([Parameter(ValueFromPipeline=$true)][string]$Record)

process {
    $parts = "$Record".Split("`t", 4)
    if ($parts.Count -lt 4) { continue }
    $fact = $parts[3] -replace "\s+", " "
    "hyphae`t$($parts[1])`t$($parts[2])`t$fact"
}
