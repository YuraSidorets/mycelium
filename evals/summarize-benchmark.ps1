param(
    [Parameter(Mandatory)]
    [string]$JobsDir,
    [Parameter(Mandatory)]
    [string]$Manifest
)

$ErrorActionPreference = "Stop"
$JobsPath = [IO.Path]::GetFullPath($JobsDir)
$EvalManifest = Get-Content -Raw -LiteralPath $Manifest | ConvertFrom-Json
$Cases = @($EvalManifest.cases)
$ExpectedResults = $Cases.Count * 2
$ExpectedScripts = @{}
foreach ($Case in $Cases) {
    $ExpectedScripts[$Case.id] = [string]$Case.expected_script
}

function Get-TrajectoryFacts {
    param(
        [string]$ResultPath,
        [string]$CaseId,
        [string]$Arm
    )

    $TrajectoryPath = Join-Path (Split-Path -Parent $ResultPath) "trajectory/acp_trajectory.jsonl"
    $Raw = if (Test-Path -LiteralPath $TrajectoryPath) {
        Get-Content -Raw -LiteralPath $TrajectoryPath
    }
    else {
        ""
    }
    $GraderInspection = $false
    $OracleInvoked = $false
    $SkillRead = $false
    $ExpectedScriptUsed = $false
    $ExpectedScript = $ExpectedScripts[$CaseId]
    if ($Raw) {
        foreach ($Line in Get-Content -LiteralPath $TrajectoryPath) {
            try {
                $Event = $Line | ConvertFrom-Json
            }
            catch {
                continue
            }
            if ($Event.type -ne "tool_call") { continue }
            $Title = [string]$Event.title
            if ($Event.status -eq "completed") {
                $SkillPath = [regex]::Escape('/opt/skills/mycelium/SKILL.md')
                $CompletedRead = $Event.kind -eq "read" -and
                    $Title -match ('(?i)(?:^|[''"\s])' + $SkillPath + '(?:[''"\s]|$)')
                $CompletedShellRead = $Event.kind -eq "execute" -and
                    $Title -match ('(?i)(?:^|&&\s*)(?:cat|head|tail|sed|Get-Content)\b[^;&|\r\n]*' +
                        $SkillPath + '[''"]?\s*$')
                if ($CompletedRead -or $CompletedShellRead) { $SkillRead = $true }
            }
            if ($ExpectedScript -and $Event.status -eq "completed" -and $Event.kind -eq "execute") {
                $ScriptPath = [regex]::Escape("/opt/skills/mycelium/$ExpectedScript")
                $ScriptCall = '(?i)^\s*[''"]?(?:cd\s+/app\s*&&\s*)?(?:(?:bash|sh|pwsh(?:\.exe)?|powershell(?:\.exe)?)\s+)?[''"]?' +
                    $ScriptPath + '[''"]?(?:\s|$)'
                if ($Title -match $ScriptCall) { $ExpectedScriptUsed = $true }
            }
            if ($Title -match '(?i)(?:/opt/mycelium-grader|workflow-oracle\.ps1|verify-cap\.py|/evals/evals\.json)') {
                $GraderInspection = $true
            }
            if ($Title -match '(?i)/usr/local/bin/mycelium-workflow-oracle(?:\s|$)') {
                $ExpectedCall = '^\s*/usr/local/bin/mycelium-workflow-oracle\s+' +
                    [regex]::Escape($CaseId) + '\s*$'
                if ($Event.status -eq "completed" -and $Event.kind -eq "execute" -and $Title -match $ExpectedCall) {
                    $OracleInvoked = $true
                }
                else {
                    $GraderInspection = $true
                }
            }
        }
    }

    return [pscustomobject]@{
        skill_read = $SkillRead
        expected_script_used = $ExpectedScriptUsed
        oracle_invoked = $OracleInvoked
        grader_inspection = $GraderInspection
        trajectory_path = $TrajectoryPath
    }
}

$Rows = @(
    Get-ChildItem -Recurse -Filter result.json -LiteralPath $JobsPath |
        ForEach-Object {
            $Result = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
            $Facts = Get-TrajectoryFacts `
                -ResultPath $_.FullName `
                -CaseId $Result.task_name `
                -Arm $Result.skill_mode
            [pscustomobject]@{
                case = $Result.task_name
                arm = $Result.skill_mode
                reward = [double]$Result.rewards.reward
                tool_calls = [int]$Result.n_tool_calls
                agent_seconds = [double]$Result.timing.agent_execution
                error = [string]$Result.error
                error_category = [string]$Result.error_category
                skill_read = $Facts.skill_read
                expected_script_used = $Facts.expected_script_used
                oracle_invoked = $Facts.oracle_invoked
                grader_inspection = $Facts.grader_inspection
                result_path = $_.FullName
                trajectory_path = $Facts.trajectory_path
            }
        }
)

$SkillRows = @($Rows | Where-Object arm -eq "with-skill")
$BaselineRows = @($Rows | Where-Object arm -eq "no-skill")
$SkillScore = if ($Cases.Count) {
    (@($SkillRows | Where-Object reward -eq 1).Count / $Cases.Count)
}
else { 0 }
$BaselineScore = if ($Cases.Count) {
    (@($BaselineRows | Where-Object reward -eq 1).Count / $Cases.Count)
}
else { 0 }

$PairingValid = $Rows.Count -eq $ExpectedResults
foreach ($Case in $Cases) {
    if (@($Rows | Where-Object {
        $_.case -eq $Case.id -and $_.arm -eq "with-skill"
    }).Count -ne 1 -or
        @($Rows | Where-Object {
        $_.case -eq $Case.id -and $_.arm -eq "no-skill"
    }).Count -ne 1) {
        $PairingValid = $false
    }
}

$SkillIntegrity = @($SkillRows | Where-Object {
    -not $_.skill_read -or
    -not $_.expected_script_used -or
    -not $_.oracle_invoked -or
    $_.grader_inspection
})
$BaselineIntegrity = @($BaselineRows | Where-Object {
    $_.skill_read -or
    $_.expected_script_used -or
    -not $_.oracle_invoked -or
    $_.grader_inspection
})
if ($SkillIntegrity.Count -or $BaselineIntegrity.Count) { $PairingValid = $false }

$Summary = [ordered]@{
    suite_version = [string]$EvalManifest.version
    case_count = $Cases.Count
    result_count = $Rows.Count
    pairing_valid = $PairingValid
    with_skill = [ordered]@{
        passed = @($SkillRows | Where-Object reward -eq 1).Count
        score = $SkillScore
        tool_calls = ($SkillRows | Measure-Object tool_calls -Sum).Sum
        mean_tool_calls = if ($SkillRows.Count) {
            ($SkillRows | Measure-Object tool_calls -Average).Average
        }
        else { 0 }
        mean_agent_seconds = if ($SkillRows.Count) {
            ($SkillRows | Measure-Object agent_seconds -Average).Average
        }
        else { 0 }
        integrity_failures = $SkillIntegrity.Count
    }
    no_skill = [ordered]@{
        passed = @($BaselineRows | Where-Object reward -eq 1).Count
        score = $BaselineScore
        tool_calls = ($BaselineRows | Measure-Object tool_calls -Sum).Sum
        mean_tool_calls = if ($BaselineRows.Count) {
            ($BaselineRows | Measure-Object tool_calls -Average).Average
        }
        else { 0 }
        mean_agent_seconds = if ($BaselineRows.Count) {
            ($BaselineRows | Measure-Object agent_seconds -Average).Average
        }
        else { 0 }
        timeouts = @($BaselineRows | Where-Object error_category -eq "timeout").Count
        integrity_failures = $BaselineIntegrity.Count
    }
    lift = if ($PairingValid) { $SkillScore - $BaselineScore } else { $null }
    rows = @($Rows | Sort-Object arm, case)
}

$SummaryPath = Join-Path $JobsPath "mycelium-summary.json"
[IO.File]::WriteAllText(
    $SummaryPath,
    ($Summary | ConvertTo-Json -Depth 8) + "`n",
    [Text.UTF8Encoding]::new($false)
)

$LiftText = if ($Summary.pairing_valid) { "{0:+0.00;-0.00;0.00}" -f $Summary.lift } else { "INVALID" }
$SummaryLine = "Mycelium v{0}: skill {1}/{2}, baseline {3}/{2}, lift {4}, tools {5} vs {6}, integrity failures {7}"
Write-Output ($SummaryLine -f
    $EvalManifest.version,
    $Summary.with_skill.passed,
    $Cases.Count,
    $Summary.no_skill.passed,
    $LiftText,
    $Summary.with_skill.tool_calls,
    $Summary.no_skill.tool_calls,
    ($Summary.with_skill.integrity_failures + $Summary.no_skill.integrity_failures)
)
Write-Output $SummaryPath
