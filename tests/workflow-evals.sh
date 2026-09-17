#!/usr/bin/env bash
set -euo pipefail

PYTHON=python3
case "$(command -v python3 2>/dev/null | tr '[:upper:]' '[:lower:]')" in *windowsapps*) PYTHON=python ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

run_oracle() {
  local case_id="$1"
  local workspace="$2"
  pwsh -NoProfile -File "$ROOT/evals/workflow-oracle.ps1" "$case_id" -Workspace "$workspace"
}

write_evidence() {
  local path="$1" line="$2" fact="$3"
  mkdir -p "$(dirname "$path")"
  for ((i = 1; i < line; i++)); do printf 'fixture\n'; done > "$path"
  printf '%s\n' "$fact" >> "$path"
}

write_tracked_node() {
  local run_id="$1" node_id="$2" role="$3" fact="$4" source_ref="$5" parent="${6:-}"
  local lineage_args=()
  if [[ -n "$parent" ]]; then lineage_args=(--process-input "$parent"); fi
  bash "$ROOT/bin/mycelium-node.sh" \
    "lineage proof" "$node_id" "$role" complete \
    "$fact" "none" "continue" \
    --topics "lineage proof" \
    --evidence "$source_ref" \
    --confidence high \
    --consumes "${parent:-none}" \
    --blocks "none" \
    --source-refs "$source_ref" \
    --run-id "$run_id" \
    "${lineage_args[@]}" > /dev/null
}

mkdir -p "$TMP/tracked-lineage"
cd "$TMP/tracked-lineage"
mkdir -p bin
cp "$ROOT/bin/mycelium_lineage.py" bin/mycelium_lineage.py
printf '%s\n' \
  "apex lineage proof" \
  "septum lineage proof" \
  "hyphae lineage proof" \
  "stem lineage proof" \
  "cap lineage proof" > lineage-source.md
bash "$ROOT/bin/mycelium-lineage.sh" --root . begin \
  --goal "lineage proof" --run-id "tracked-eval" > /dev/null
write_tracked_node "tracked-eval" "tracked-a" apex "apex lineage proof" "lineage-source.md:1"
write_tracked_node "tracked-eval" "tracked-s" septum "septum lineage proof" "lineage-source.md:2" "tracked-a"
write_tracked_node "tracked-eval" "tracked-h" hyphae "hyphae lineage proof" "lineage-source.md:3" "tracked-s"
write_tracked_node "tracked-eval" "tracked-stem" stem "stem lineage proof" "lineage-source.md:4" "tracked-h"
write_tracked_node "tracked-eval" "tracked-cap" cap "cap lineage proof" "lineage-source.md:5" "tracked-stem"
bash "$ROOT/bin/mycelium-lineage.sh" --root . seal --run-id "tracked-eval" > /dev/null
printf '' |
  bash "$ROOT/bin/stem.sh" "lineage proof" --for-cap |
  bash "$ROOT/bin/cap.sh" "lineage proof" > tracked-cap.txt
test "$(run_oracle tracked-lineage-roundtrip "$PWD")" = "MYCELIUM_TRACKED_G72A"

mkdir -p "$TMP/durable"
cd "$TMP/durable"
write_evidence "evidence/release.json" 12 "release candidate is green"
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "release provenance" "release-proof" hyphae complete \
  "release candidate is green" "none" "wake stem" \
  --topics "release provenance" \
  --evidence "evidence/release.json:12" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --parent-goal-id "release provenance" \
  --producing-agent "apex-release" \
  --source-refs "evidence/release.json:12" > /dev/null
bash "$ROOT/bin/mycelium-index.sh" > /dev/null
bash "$ROOT/bin/mycelium-search.sh" "release provenance" > search.txt
printf '' |
  bash "$ROOT/bin/stem.sh" "release provenance" --for-cap |
  bash "$ROOT/bin/cap.sh" "release provenance" > cap.txt
test "$(run_oracle durable-roundtrip "$PWD")" = "MYCELIUM_DURABLE_A17C"

mkdir -p "$TMP/conflict"
cd "$TMP/conflict"
write_evidence "owners/public.md" 8 "service endpoint is 443"
write_evidence "owners/internal.md" 11 "service endpoint is 8443"
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "service endpoint" "endpoint-public" hyphae complete \
  "service endpoint is 443" "none" "wake stem" \
  --topics "service endpoint" \
  --evidence "owners/public.md:8" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --producing-agent "apex-public" \
  --source-refs "owners/public.md:8" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "service endpoint" "endpoint-internal" hyphae complete \
  "service endpoint is 8443" "none" "wake stem" \
  --topics "service endpoint" \
  --evidence "owners/internal.md:11" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --producing-agent "apex-internal" \
  --source-refs "owners/internal.md:11" > /dev/null
set +e
conflict_output="$(
  printf '' |
    bash "$ROOT/bin/stem.sh" "service endpoint" --for-cap |
    bash "$ROOT/bin/cap.sh" "service endpoint"
)"
conflict_status=$?
set -e
CONFLICT_EXIT="$conflict_status" CONFLICT_OUTPUT="$conflict_output" pwsh -NoProfile -Command '
  [pscustomobject]@{
    exit_code = [int]$env:CONFLICT_EXIT
    output = $env:CONFLICT_OUTPUT
  } | ConvertTo-Json | Set-Content -LiteralPath conflict-result.json
'
test "$(run_oracle conflict-roundtrip "$PWD")" = "MYCELIUM_CONFLICT_B28D"

mkdir -p "$TMP/reflection"
cd "$TMP/reflection"
write_evidence "checks/integration.log" 4 "release verification attempted"
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "release verification" "cap-final" cap complete \
  "release verification attempted" "none" "verify" \
  --topics "release verification" \
  --evidence "checks/integration.log:4" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --source-refs "checks/integration.log:4" > /dev/null
bash "$ROOT/bin/mycelium-reflect.sh" \
  "release verification" \
  "cap-final" \
  "integration verification failed" \
  "fix fixture and rerun verification" > /dev/null
test "$(run_oracle reflection-roundtrip "$PWD")" = "MYCELIUM_REFLECTION_C39E"

mkdir -p "$TMP/delegation"
cd "$TMP/delegation"
bash "$ROOT/bin/subagent-brief.sh" \
  apex "audit dashboard API boundaries" "apex-one" > apex-brief.txt
bash "$ROOT/bin/subagent-brief.sh" \
  stem "audit dashboard API boundaries" "stem-main" > stem-brief.txt
bash "$ROOT/bin/subagent-brief.sh" \
  cap "audit dashboard API boundaries" "cap-review" > cap-brief.txt
test "$(run_oracle delegation-roundtrip "$PWD")" = "MYCELIUM_DELEGATION_D4AF"

mkdir -p "$TMP/selection"
cd "$TMP/selection"
write_evidence "platform/quartz-live.md" 22 "quartz routing port is 7443"
write_evidence "previews/quartz.md" 3 "quartz routing port is 7999"
write_evidence "issues/quartz.md" 6 "quartz routing port is 7555"
write_evidence "archive/telemetry.log" 1 "archive telemetry is nominal"
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "quartz routing port" "target-current" hyphae complete \
  "quartz routing port is 7443" "none" "wake stem" \
  --topics "quartz routing port" \
  --evidence "platform/quartz-live.md:22" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --source-refs "platform/quartz-live.md:22" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "quartz routing port" "target-preview" hyphae complete \
  "quartz routing port is 7999" "none" "ignore" \
  --topics "quartz routing port" \
  --evidence "previews/quartz.md:3" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --source-refs "previews/quartz.md:3" \
  --invalidated > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "quartz routing port" "target-blocked" hyphae blocked \
  "quartz routing port is 7555" "" "wait" \
  --topics "quartz routing port" \
  --confidence high \
  --source-refs "issues/quartz.md:6" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "quartz routing port" "target-no-source" hyphae complete \
  "quartz routing port is 7666" "none" "ignore" \
  --topics "quartz routing port" \
  --evidence "summary only" \
  --confidence high \
  --consumes "none" \
  --blocks "none" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "workflow eval fixture" \
  "archive telemetry" "archive-noise" hyphae complete \
  "archive telemetry is nominal" "none" "archive" \
  --topics "archive telemetry" \
  --evidence "archive/telemetry.log:1" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --source-refs "archive/telemetry.log:1" > /dev/null
bash "$ROOT/bin/mycelium-index.sh" > /dev/null
printf '' |
  bash "$ROOT/bin/stem.sh" "quartz routing port" --for-cap |
  bash "$ROOT/bin/cap.sh" "quartz routing port" > selection-cap.txt
test "$(run_oracle selection-roundtrip "$PWD")" = "MYCELIUM_SELECTION_E5B0"

mkdir -p "$TMP/pipeline"
cd "$TMP/pipeline"
git init -q
pwsh -NoProfile -Command '
  @(
    "The dashboard listens on port 3300."
    "The dashboard color is blue."
    "The unrelated color is blue."
  ) | Set-Content -LiteralPath facts.md
'
git add facts.md
git -c user.name=mycelium -c user.email=mycelium@example.invalid commit -qm init
bash "$ROOT/bin/apex.sh" "dashboard port" |
  bash "$ROOT/bin/septum.sh" "dashboard port" |
  bash "$ROOT/bin/hyphae.sh" |
  bash "$ROOT/bin/stem.sh" "dashboard port" |
  bash "$ROOT/bin/cap.sh" "dashboard port" > ephemeral-cap.txt
test "$(run_oracle ephemeral-pipeline "$PWD")" = "MYCELIUM_PIPELINE_F6C1"

echo "Workflow eval tests passed"
