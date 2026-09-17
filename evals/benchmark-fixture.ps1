# Local fixture builder for exercising the neutral SkillsBench oracle through
# Mycelium's public seams. This file is excluded from the mounted skill.
param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet("role", "durable", "conflict", "selection")]
    [string]$Case
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Oracle = Join-Path $PSScriptRoot "benchmark-oracle.ps1"
$PreviousLocation = Get-Location
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("mycelium-eval-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Tmp | Out-Null

try {
    Set-Location $Tmp

    switch ($Case) {
        "role" {
            $Apex = (& "$Root/bin/subagent-brief.ps1" apex "audit dashboard API boundaries" "apex-one") -join "`n"
            $Stem = (& "$Root/bin/subagent-brief.ps1" stem "audit dashboard API boundaries") -join "`n"
            $Cap = (& "$Root/bin/subagent-brief.ps1" cap "audit dashboard API boundaries") -join "`n"
            Set-Content -LiteralPath "apex-brief.txt" -Value $Apex
            Set-Content -LiteralPath "stem-brief.txt" -Value $Stem
            Set-Content -LiteralPath "cap-brief.txt" -Value $Cap
            [ordered]@{
                stem_instances = 1
                cap_completed = $true
                producing_agent = "apex-one"
            } | ConvertTo-Json | Set-Content -LiteralPath "workflow.json"
            & $Oracle role -Workspace $Tmp
        }

        "durable" {
            1..6 | ForEach-Object { "filler $_" } | Set-Content -Encoding utf8 source.md
            Add-Content -Encoding utf8 source.md "the omega adapter is enabled"
            & "$Root/bin/mycelium-node.ps1" "omega provenance" "node-omega" hyphae complete `
                -AllowUntracked "benchmark fixture" `
                "the omega adapter is enabled" "none" "dispatch cap" `
                -Topics "omega,provenance" `
                -Evidence "source.md:7" `
                -Confidence high `
                -Consumes "none" `
                -Blocks "none" `
                -SourceRefs "source.md:7" > $null

            $Cap = ("" | & "$Root/bin/stem.ps1" "omega provenance" -ForCap |
                & "$Root/bin/cap.ps1" "omega provenance") -join "`n"
            Set-Content -LiteralPath "cap-durable.txt" -Value $Cap
            & $Oracle durable -Workspace $Tmp
        }

        "conflict" {
            $InputPath = Join-Path $Tmp "conflict-input.txt"
            $RunnerPath = Join-Path $Tmp "conflict-runner.ps1"
            $OutputPath = Join-Path $Tmp "conflict-output.txt"
            $ErrorPath = Join-Path $Tmp "conflict-error.txt"
            @(
                "hyphae`ta.md`t1`tdashboard listens on port 3300",
                "hyphae`tb.md`t1`tdashboard does not listen on port 3300",
                "- topic differs: node-a says value-a; node-b says value-b"
            ) | Set-Content -LiteralPath $InputPath
            @"
Get-Content -LiteralPath '$($InputPath.Replace("'", "''"))' |
    & '$((Join-Path $Root "bin/cap.ps1").Replace("'", "''"))' 'dashboard port'
exit `$LASTEXITCODE
"@ | Set-Content -LiteralPath $RunnerPath
            $Process = Start-Process -FilePath (Get-Command pwsh).Source `
                -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $RunnerPath) `
                -NoNewWindow -Wait -PassThru `
                -RedirectStandardOutput $OutputPath `
                -RedirectStandardError $ErrorPath
            $Output = @(
                Get-Content -Raw $OutputPath
                Get-Content -Raw $ErrorPath
            ) -join "`n"
            [ordered]@{
                exit_code = $Process.ExitCode
                output = $Output
            } | ConvertTo-Json | Set-Content -LiteralPath "conflict-result.json"
            & $Oracle conflict -Workspace $Tmp
        }

        "selection" {
            1..6 | ForEach-Object { "filler $_" } | Set-Content -Encoding utf8 source.md
            Add-Content -Encoding utf8 source.md "quartz routing is enabled"
            & "$Root/bin/mycelium-node.ps1" "quartz routing" "node-relevant" hyphae complete `
                -AllowUntracked "benchmark fixture" `
                "quartz routing is enabled" "none" "dispatch cap" `
                -Topics "quartz,routing" `
                -Evidence "source.md:7" `
                -Confidence high `
                -Consumes "none" `
                -Blocks "none" `
                -SourceRefs "source.md:7" > $null
            & "$Root/bin/mycelium-node.ps1" "quartz routing" "node-invalidated" hyphae complete `
                -AllowUntracked "benchmark fixture" `
                "invalidated quartz routing" "none" "ignore" `
                -Topics "quartz,routing" `
                -Evidence "stale.md:2" `
                -Confidence high `
                -Consumes "none" `
                -Blocks "none" `
                -SourceRefs "stale.md:2" `
                -Invalidated $true > $null
            & "$Root/bin/mycelium-node.ps1" "quartz routing" "node-blocked" hyphae blocked `
                -AllowUntracked "benchmark fixture" `
                "blocked quartz routing" "" "ignore" `
                -Topics "quartz,routing" `
                -Evidence "blocked.md:4" `
                -Confidence high `
                -SourceRefs "blocked.md:4" > $null
            @"
---
nodeId: node-failed
goal: quartz routing
role: hyphae
status: failed
topics: quartz,routing
confidence: high
source_refs: failed.md:9
invalidated: false
updated: 2026-07-15T00:00:00Z
---

## Facts
failed quartz routing
"@ | Set-Content -Encoding utf8 ".mycelium/nodes/node-failed.md"
            $LargeFact = ("unrelated basalt storage record " * 200).Trim()
            foreach ($Index in 1..6) {
                & "$Root/bin/mycelium-node.ps1" "basalt storage" "node-unrelated-$Index" hyphae complete `
                    -AllowUntracked "benchmark fixture" `
                    $LargeFact "none" "ignore" `
                    -Topics "basalt,storage" `
                    -Evidence "inventory-$Index.md:1" `
                    -Confidence high `
                    -Consumes "none" `
                    -Blocks "none" `
                    -SourceRefs "inventory-$Index.md:1" > $null
            }

            $Cap = ("" | & "$Root/bin/stem.ps1" "quartz routing" -ForCap |
                & "$Root/bin/cap.ps1" "quartz routing") -join "`n"
            Set-Content -LiteralPath "cap-selection.txt" -Value $Cap
            & $Oracle selection -Workspace $Tmp
        }
    }
}
finally {
    Set-Location $PreviousLocation
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
