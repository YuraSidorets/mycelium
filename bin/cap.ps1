param(
    [Parameter(Mandatory=$true, Position=0)][string]$Goal,
    [Parameter(ValueFromPipeline=$true)][string]$Record
)

begin {
    $rows = @()
    $records = @()
}

process {
    $line = "$Record"
    $records += $line
    $parts = $line.Split("`t", 4)
    if ($parts.Count -ge 4) {
        $rows += [pscustomobject]@{ Path = $parts[1]; Line = $parts[2]; Fact = $parts[3] }
    }
}

end {
    $text = $records -join "`n"
    if ($text -match "(?im)^-\s+.+ differs: .+ says .+; .+ says .+") {
        "Goal: $Goal"
        "Spore: blocked by unresolved STEM conflict."
        "Verification: FAIL"
        "Residual risk: selected evidence conflicts."
        "Follow-up: return to STEM with conflicting Node IDs and resolve before final CAP."
        exit 1
    }

    "Goal: $Goal"
    "Spore:"
    "Result:"
    if ($rows.Count -eq 0) {
        "- No relevant facts found."
        "Verification: FAIL"
        "Residual risk: no facts reached CAP."
        "Follow-up: run APEX, SEPTUM, HYPHAE, and STEM again with narrower evidence."
        exit 1
    }

    foreach ($row in $rows) {
        "- $($row.Fact) ($($row.Path):$($row.Line))"
    }
    "Verification: EXTERNAL_REQUIRED"
    "Residual risk: selected facts and source evidence are not yet externally verified."
    "Follow-up: run the external verifier before claiming PASS."
}
