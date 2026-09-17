#!/usr/bin/env bash
set -euo pipefail

PYTHON=python3
case "$(command -v python3 2>/dev/null | tr '[:upper:]' '[:lower:]')" in *windowsapps*) PYTHON=python ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cd "$TMP"
git init -q
cat > notes.md <<'EOF'
# Notes

The dashboard listens on port 3300.
The dashboard color is blue.
The unrelated color is blue.
EOF
git add notes.md
git -c user.name=mycelium -c user.email=mycelium@example.invalid commit -qm init

cat > omega.md <<'EOF'
# Omega

omega status is green
EOF

printf '%s\n' 'ignored-marker.md' > .gitignore
git add .gitignore
git -c user.name=mycelium -c user.email=mycelium@example.invalid commit -qm ignore-marker
printf '%s\n' 'The untracked marker is visible to APEX.' > untracked-marker.md
printf '%s\n' 'The untracked marker must stay ignored.' > ignored-marker.md

bash "$ROOT/bin/apex.sh" "untracked marker" > untracked-apex.txt
grep -qF $'apex\tuntracked-marker.md\t' untracked-apex.txt
if grep -qF $'apex\tignored-marker.md\t' untracked-apex.txt; then
  echo "APEX included a gitignored file" >&2
  exit 1
fi

bash "$ROOT/bin/apex.sh" "dashboard port" |
  bash "$ROOT/bin/septum.sh" "dashboard port" |
  bash "$ROOT/bin/hyphae.sh" |
  bash "$ROOT/bin/stem.sh" "dashboard port" |
  bash "$ROOT/bin/cap.sh" "dashboard port" > pipeline.txt

grep -qF "dashboard listens on port 3300" pipeline.txt
grep -qF "Verification: EXTERNAL_REQUIRED" pipeline.txt
! grep -qF "Verification: PASS" pipeline.txt
if grep -qF "dashboard color is blue" pipeline.txt; then
  echo "septum passed a weak one-term match" >&2
  exit 1
fi

bash "$ROOT/bin/subagent-brief.sh" apex "dashboard port" "apex-one" \
  --spawn-tool "collaboration.spawn_agent" > brief.txt
grep -qF "You are mycelium APEX" brief.txt
grep -qF "Producing Agent: apex-one" brief.txt
grep -qF "bin/mycelium-node.sh" brief.txt
grep -qF "Spawn tool: collaboration.spawn_agent" brief.txt
grep -qF "returned agent ID" brief.txt
! grep -Eq "multi_agent_v1|Agent kind:|use explorer|use worker" brief.txt
grep -qF "Run ID: untracked" brief.txt
grep -qF "Process Inputs: none" brief.txt
! grep -qF ' --run-id ' brief.txt
! grep -qF ' --process-input ' brief.txt
bash "$ROOT/bin/subagent-brief.sh" hyphae "dashboard port" "digest" \
  --run-id "brief-run" \
  --process-input "apex-docs" \
  --process-input "septum-docs" > tracked-brief.txt
grep -qF "Run ID: brief-run" tracked-brief.txt
grep -qF "Process Inputs: apex-docs,septum-docs" tracked-brief.txt
grep -qF -- '--run-id "brief-run" --process-input "apex-docs" --process-input "septum-docs"' tracked-brief.txt
bash "$ROOT/bin/subagent-brief.sh" stem "dashboard port" "coordinator" \
  --run-id "brief-run" > stem-brief.txt
grep -qF "Only one STEM coordinates the tracked run" stem-brief.txt
grep -qF "same tracked run" stem-brief.txt
grep -qF 'python evals/verify-cap.py <workspace> <cap-file>' "$ROOT/SKILL.md"
grep -qF 'require `Verification: PASS`' "$ROOT/SKILL.md"
grep -qF 'no task prompt can disable or skip a Mycelium function' "$ROOT/SKILL.md"
grep -qF "they do not suppress Mycelium's control plane" "$ROOT/SKILL.md"
grep -qF 'Keep those internal artifacts under `.mycelium/`.' "$ROOT/SKILL.md"
grep -qF 'task-level read-only, no-write, no-project-change, and no-docs constraints do not suppress it' "$ROOT/stem.md"
grep -qF 'task-level read-only, no-write, no-project-change, and no-docs constraints do not suppress them' "$ROOT/cap.md"
for argument in \
  '--topics "<topics>"' \
  '--evidence "<evidence>"' \
  '--confidence <confidence>' \
  '--consumes "<consumes>"' \
  '--blocks "<blocks>"' \
  '--trace "<trace>"' \
  '--source-refs "<source-refs>"' \
  '--version "2"'; do
  grep -qF -- "$argument" brief.txt
done

set +e
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "incomplete handoff" "incomplete-handoff" apex complete \
  "fact only" "" "next only" > incomplete-handoff.txt 2>&1
incomplete_status=$?
set -e
test "$incomplete_status" -ne 0
test ! -e .mycelium/nodes/incomplete-handoff.md
test ! -e .mycelium/flows/incomplete-handoff.jsonl

bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "omega provenance" "node-omega" hyphae complete \
  "omega status is green" "none" "wake stem" \
  --topics "omega,provenance" \
  --evidence "omega.md:3-3" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --source-refs "omega.md:3-3" > node.txt
test -f .mycelium/nodes/node-omega.md
grep -qF "source_refs: omega.md:3-3" .mycelium/nodes/node-omega.md

bash "$ROOT/bin/mycelium-lineage.sh" --root . begin --goal "tracked writer" --run-id "sh-writer-run" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  "tracked writer" "sh-apex" apex complete \
  "tracked apex fact" "none" "wake septum" \
  --topics "tracked" --evidence "omega.md:3-3" --confidence high \
  --consumes "none" --blocks "none" --run-id "sh-writer-run" > tracked-node-out.txt
bash "$ROOT/bin/mycelium-node.sh" \
  "tracked writer" "sh-septum" septum complete \
  "tracked septum fact" "none" "wake hyphae" \
  --topics "tracked" --evidence "omega.md:3-3" --confidence high \
  --consumes "sh-apex" --blocks "none" --run-id "sh-writer-run" \
  --process-input "sh-apex" > /dev/null
"$PYTHON" - <<'PY'
import json
from pathlib import Path
manifest = json.loads(Path('.mycelium/runs/sh-writer-run.json').read_text(encoding='utf-8'))
assert manifest['nodes']['sh-apex']['status'] == 'committed'
assert manifest['nodes']['sh-septum']['status'] == 'committed'
assert manifest['nodes']['sh-septum']['process_inputs'] == ['sh-apex']
PY
bash "$ROOT/bin/mycelium-lineage.sh" --root . begin --goal "tracked failure" --run-id "sh-failure-run" > /dev/null
set +e
bash "$ROOT/bin/mycelium-node.sh" \
  "tracked failure" "sh-invalid" apex complete \
  "invalid schema fact" "none" "stop" \
  --topics "tracked" --evidence "omega.md:3-3" --confidence high \
  --consumes "none" --blocks "none" --run-id "sh-failure-run" \
  --version "1" > tracked-failure.txt 2>&1
tracked_failure_status=$?
set -e
test "$tracked_failure_status" -ne 0
"$PYTHON" - <<'PY'
import json
from pathlib import Path
manifest = json.loads(Path('.mycelium/runs/sh-failure-run.json').read_text(encoding='utf-8'))
assert manifest['nodes']['sh-invalid']['status'] == 'pending'
PY

bash "$ROOT/bin/mycelium-index.sh" > index.txt
rm .mycelium/index.json
bash "$ROOT/bin/mycelium-search.sh" "omega provenance" "omega" > search.txt
grep -qF $'node-omega\thyphae\tcomplete\thigh' search.txt
if grep -qF ".mycelium/index.json" search.txt; then
  echo "search leaked index rebuild output" >&2
  exit 1
fi

printf '%s\n' 'fresh retrieval survives a stale index' > fresh.md
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "fresh retrieval" "node-fresh" hyphae complete \
  "fresh retrieval survives a stale index" "none" "wake stem" \
  --topics "fresh,retrieval" \
  --evidence "fresh.md:1" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --source-refs "fresh.md:1" > /dev/null
bash "$ROOT/bin/mycelium-search.sh" "fresh retrieval" "fresh" > search-fresh.txt
grep -qF $'node-fresh\thyphae\tcomplete\thigh' search-fresh.txt
printf '%s\n' 'fresh source changed' > fresh.md
bash "$ROOT/bin/mycelium-search.sh" "fresh retrieval" "fresh" > search-fresh-stale.txt 2> search-fresh-trace.txt
test ! -s search-fresh-stale.txt
grep -qF 'node-fresh:stale-source-refs' search-fresh-trace.txt

cat > retrieval.md <<'EOF'
grandparent context
dependency context
rare quartz retired
first generic fact
rare quartz current
EOF
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "retrieval graph" "node-grandparent" hyphae complete \
  "grandparent context" "none" "dispatch cap" \
  --topics "ancestry" --evidence "retrieval.md:1" --confidence high \
  --consumes "none" --blocks "none" --source-refs "retrieval.md:1" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "retrieval graph" "node-root" hyphae complete \
  "dependency context" "none" "dispatch cap" \
  --topics "dependency" --evidence "retrieval.md:2" --confidence high \
  --consumes "none" --blocks "none" --source-refs "retrieval.md:2" \
  --depends-on "node-grandparent" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "retrieval graph" "node-old" hyphae complete \
  "rare quartz retired" "none" "dispatch cap" \
  --topics "beta" --evidence "retrieval.md:3" --confidence high \
  --consumes "none" --blocks "none" --source-refs "retrieval.md:3" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "retrieval graph" "node-new" hyphae complete \
  $'first generic fact\nrare quartz current' "none" "dispatch cap" \
  --topics $'beta\tunsafe' --evidence "evidence-only anchor" --confidence high \
  --consumes "node-old" --blocks "none" --source-refs "retrieval.md:4-5" \
  --depends-on "node-root" --supersedes "node-old" > /dev/null

bash "$ROOT/bin/mycelium-search.sh" "rare quartz" "beta" > search-retrieval.txt
awk -F '\t' 'NR == 1 { exit NF == 16 ? 0 : 1 }' search-retrieval.txt
grep -qF $'node-new\t' search-retrieval.txt
! grep -q $'^node-old\t' search-retrieval.txt
for token in \
  'match=facts:rare,quartz' \
  'query=rare quartz' \
  'source_refs=retrieval.md:4-5' \
  'depends_on=node-root' \
  'consumes=node-old' \
  'supersedes=node-old' \
  'index=v2' \
  'ignored=node-old:superseded'
do
  grep -qF "$token" search-retrieval.txt
done
bash "$ROOT/bin/mycelium-search.sh" "evidence-only anchor" "beta" > search-evidence.txt
grep -qF 'match=evidence:evidence-only,anchor' search-evidence.txt
bash "$ROOT/bin/mycelium-search.sh" "retrieval.md" "beta" > search-source-ref.txt
grep -qF 'match=source_refs:retrieval.md' search-source-ref.txt
bash "$ROOT/bin/mycelium-search.sh" "node-root" "beta" > search-dependency.txt
grep -qF 'match=depends_on:node-root' search-dependency.txt

"$PYTHON" - <<'PY'
import json

rows = json.load(open(".mycelium/index.json", encoding="utf-8"))
indexed = {row["nodeId"]: row for row in rows}
assert "rare quartz current" in indexed["node-new"]["facts"]
assert "evidence" not in indexed["node-new"]
assert "node-new" in indexed["node-root"]["depended_on_by"]
assert "node-new" in indexed["node-old"]["superseded_by"]
PY

cat search-retrieval.txt | bash "$ROOT/bin/stem.sh" "rare quartz" --for-cap > stem-retrieval.txt
grep -qF 'node-new' stem-retrieval.txt
grep -qF 'node-root' stem-retrieval.txt
! grep -Eq 'node-old|node-grandparent' stem-retrieval.txt

set +e
cut -f1-4,6- search-retrieval.txt |
  bash "$ROOT/bin/stem.sh" "rare quartz" --for-cap > /dev/null 2> malformed-candidate.txt
malformed_candidate_status=$?
set -e
test "$malformed_candidate_status" -ne 0
grep -qF 'malformed search candidate' malformed-candidate.txt

printf '%s\n' 'case target fact' > case-target.md
printf '%s\n' 'case child fact' > case-child.md
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "case target" "Node-CaseTarget" hyphae complete \
  "case target fact" "none" "dispatch cap" \
  --topics "case-target" --evidence "case-target.md:1" --confidence high \
  --consumes "none" --blocks "none" --source-refs "case-target.md:1" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "case child" "node-case-child" hyphae complete \
  "case child fact" "none" "dispatch cap" \
  --topics "case-child" --evidence "case-child.md:1" --confidence high \
  --consumes "none" --blocks "none" --source-refs "case-child.md:1" \
  --depends-on "node-casetarget" > /dev/null
bash "$ROOT/bin/mycelium-search.sh" "case child fact" "case-child" > search-case.txt
cat search-case.txt | bash "$ROOT/bin/stem.sh" "case child fact" --for-cap > stem-case.txt
grep -qF 'node-case-child' stem-case.txt
! grep -qF 'Node-CaseTarget' stem-case.txt
"$PYTHON" - <<'PY'
import json

rows = json.load(open(".mycelium/index.json", encoding="utf-8"))
target = next(row for row in rows if row["nodeId"] == "Node-CaseTarget")
assert "node-case-child" not in target["depended_on_by"]
PY

printf '' |
  bash "$ROOT/bin/stem.sh" "omega provenance" --for-cap |
  bash "$ROOT/bin/cap.sh" "omega provenance" > durable-cap.txt
grep -qF "[node-omega]" durable-cap.txt
grep -qF "Verification: EXTERNAL_REQUIRED" durable-cap.txt
! grep -qF "Verification: PASS" durable-cap.txt
"$PYTHON" "$ROOT/evals/verify-cap.py" . durable-cap.txt > durable-verification.txt
grep -qF "Verification: PASS" durable-verification.txt

printf '%s\n' 'green flow fact' > flow-binding.md
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "flow binding" "node-flow-binding" hyphae complete \
  "green flow fact" "none" "dispatch cap" \
  --topics "flow,binding" --evidence "flow-binding.md:1" --confidence high \
  --consumes "none" --blocks "none" --source-refs "flow-binding.md:1" > /dev/null
"$PYTHON" - <<'PY'
import json
from pathlib import Path

path = Path(".mycelium/flows/flow-binding.jsonl")
record = json.loads(path.read_text(encoding="utf-8"))
record["facts"] = "\n".join(record["facts"])
record["questions"] = "\n".join(record["questions"])
record["next"] = "\n".join(record["next"])
path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
PY
"$PYTHON" - <<'PY'
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

path = Path(".mycelium/nodes/node-flow-binding.md")
text = path.read_text(encoding="utf-8")
raw = re.search(r"(?m)^updated: (.+)$", text).group(1)
instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
hours = -14 if instant.astimezone(timezone.utc).hour < 14 else 14
shifted = instant.astimezone(timezone(timedelta(hours=hours))).isoformat(timespec="microseconds")
assert instant.date() != datetime.fromisoformat(shifted).date()
path.write_text(re.sub(r"(?m)^updated: .+$", f"updated: {shifted}", text), encoding="utf-8")
PY
bash "$ROOT/bin/mycelium-index.sh" > /dev/null
"$PYTHON" - <<'PY'
from datetime import datetime, timezone
import json

rows = json.load(open(".mycelium/index.json", encoding="utf-8"))
row = next(row for row in rows if row["nodeId"] == "node-flow-binding")
record = json.load(open(".mycelium/flows/flow-binding.jsonl", encoding="utf-8"))
expected = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
fraction = (record["timestamp"].partition(".")[2].split("Z", 1)[0].split("+", 1)[0] or "").ljust(7, "0")
assert row["updated"] == expected.strftime("%Y-%m-%dT%H:%M:%S.%f") + fraction[6] + "Z"
PY
printf '' | bash "$ROOT/bin/stem.sh" "flow binding" --for-cap |
  bash "$ROOT/bin/cap.sh" "flow binding" > cap-flow-binding.txt
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-flow-binding.txt > legacy-flow-verification.txt
grep -qF "Verification: PASS" legacy-flow-verification.txt
printf '%s\n' '{"nodeId":"first","nodeId":"second","facts":[]}' > .mycelium/flows/duplicate-key.jsonl
set +e
bash "$ROOT/bin/mycelium-index.sh" > duplicate-key-error.txt 2>&1
duplicate_key_status=$?
set -e
rm .mycelium/flows/duplicate-key.jsonl
test "$duplicate_key_status" -ne 0
grep -qF "duplicate JSON key: nodeId" duplicate-key-error.txt
sed -i '/^topics: flow,binding$/a evidence: injected-only-term' .mycelium/nodes/node-flow-binding.md
if printf '' | bash "$ROOT/bin/stem.sh" "injected-only-term" --for-cap | grep -qF "node-flow-binding"; then
  echo "uncommitted evidence metadata changed STEM selection" >&2
  exit 1
fi
sed -i '/^evidence: injected-only-term$/d' .mycelium/nodes/node-flow-binding.md
sed -i 's/green flow fact/red flow fact/g' \
  flow-binding.md .mycelium/nodes/node-flow-binding.md cap-flow-binding.txt
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-flow-binding.txt > flow-binding-verification.txt
flow_binding_status=$?
set -e
test "$flow_binding_status" -eq 1
grep -qF "no matching flow commit" flow-binding-verification.txt

printf '%s\n' 'source contradicts claim' > mismatch.md
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "source mismatch" "node-source-mismatch" hyphae complete \
  "claimed fact" "none" "dispatch cap" \
  --topics "source,mismatch" --evidence "mismatch.md:1" --confidence high \
  --consumes "none" --blocks "none" --source-refs "mismatch.md:1" > /dev/null
set +e
printf '' | bash "$ROOT/bin/stem.sh" "source mismatch" --for-cap |
  bash "$ROOT/bin/cap.sh" "source mismatch" > cap-source-mismatch.txt
source_selection_status=$?
set -e
test "$source_selection_status" -ne 0
! grep -qF 'node-source-mismatch' cap-source-mismatch.txt
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-source-mismatch.txt > mismatch-verification.txt
mismatch_status=$?
set -e
test "$mismatch_status" -eq 1

printf '%s\n' 'first supported fact' 'second supported fact' > multi-source.md
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "supported facts" "node-multi-fact" hyphae complete \
  $'first supported fact\nsecond supported fact' "none" "dispatch cap" \
  --topics "supported,facts" --evidence "multi-source.md:1-2" --confidence high \
  --consumes "none" --blocks "none" --source-refs "multi-source.md:1-2" > /dev/null
printf '' | bash "$ROOT/bin/stem.sh" "supported facts" --for-cap |
  bash "$ROOT/bin/cap.sh" "supported facts" > cap-multi-fact.txt
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-multi-fact.txt > multi-verification.txt
test "$(grep -cF -- '- [node-multi-fact]' cap-multi-fact.txt)" -eq 2
sed -i \
  -e 's/^Trace: *$/Trace: tampered trace/' \
  -e '/^## Questions$/{n;s/^none$/tampered question/;}' \
  -e '/^## Next$/{n;s/^dispatch cap$/tampered next/;}' \
  .mycelium/nodes/node-multi-fact.md
bash "$ROOT/bin/mycelium-index.sh" > /dev/null
"$PYTHON" - <<'PY'
import json

rows = json.load(open(".mycelium/index.json", encoding="utf-8"))
assert not any(row["nodeId"] == "node-multi-fact" for row in rows)
PY
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-multi-fact.txt > body-mutation-verification.txt
body_mutation_status=$?
set -e
test "$body_mutation_status" -eq 1
grep -qF "no matching flow commit" body-mutation-verification.txt
sed -i \
  -e 's/^Trace: tampered trace$/Trace: /' \
  -e '/^## Questions$/{n;s/^tampered question$/none/;}' \
  -e '/^## Next$/{n;s/^tampered next$/dispatch cap/;}' \
  .mycelium/nodes/node-multi-fact.md
sed -i '/^## Questions$/{n;s/^none$/- none/;}' .mycelium/nodes/node-multi-fact.md
bash "$ROOT/bin/mycelium-index.sh" > /dev/null
"$PYTHON" - <<'PY'
import json

rows = json.load(open(".mycelium/index.json", encoding="utf-8"))
assert not any(row["nodeId"] == "node-multi-fact" for row in rows)
PY
sed -i '/^## Questions$/{n;s/^- none$/none/;}' .mycelium/nodes/node-multi-fact.md
grep -vF 'second supported fact' cap-multi-fact.txt > cap-multi-subset.txt
sed -i 's/second supported fact/changed unselected fact/g' \
  multi-source.md .mycelium/nodes/node-multi-fact.md
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-multi-subset.txt > fact-vector-verification.txt
fact_vector_status=$?
set -e
test "$fact_vector_status" -eq 1
grep -qF "no matching flow commit" fact-vector-verification.txt
sed -i 's/changed unselected fact/second supported fact/g' \
  multi-source.md .mycelium/nodes/node-multi-fact.md
sed -i 's/topics: supported,facts/topics: unrelated/' .mycelium/nodes/node-multi-fact.md
bash "$ROOT/bin/mycelium-index.sh" > /dev/null
"$PYTHON" - <<'PY'
import json

rows = json.load(open(".mycelium/index.json", encoding="utf-8"))
assert not any(row["nodeId"] == "node-multi-fact" for row in rows)
PY
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-multi-fact.txt > selection-metadata-verification.txt
selection_metadata_status=$?
set -e
test "$selection_metadata_status" -eq 1
grep -qF "no matching flow commit" selection-metadata-verification.txt
sed -i 's/topics: unrelated/topics: supported,facts/' .mycelium/nodes/node-multi-fact.md
rm .mycelium/flows/supported-facts.jsonl
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-multi-fact.txt > orphan-verification.txt
orphan_status=$?
set -e
test "$orphan_status" -eq 1
cp durable-cap.txt mixed-cap.txt
printf '%s\n' 'Verification: FAIL' >> mixed-cap.txt
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . mixed-cap.txt > mixed-verification.txt
mixed_verification_status=$?
set -e
test "$mixed_verification_status" -eq 1
grep -qF "Verification: FAIL" mixed-verification.txt

bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "external evidence" "node-bad-evidence" hyphae complete \
  "external evidence is missing" "none" "wake stem" \
  --topics "external,evidence" --evidence "missing.md:99" --confidence high \
  --consumes "none" --blocks "none" --source-refs "missing.md:99" > /dev/null
set +e
printf '' |
  bash "$ROOT/bin/stem.sh" "external evidence" --for-cap |
  bash "$ROOT/bin/cap.sh" "external evidence" > cap-bad-evidence.txt
bad_selection_status=$?
set -e
test "$bad_selection_status" -ne 0
set +e
"$PYTHON" "$ROOT/evals/verify-cap.py" . cap-bad-evidence.txt > bad-verification.txt
bad_verification_status=$?
set -e
test "$bad_verification_status" -eq 1
grep -qF "Verification: FAIL" bad-verification.txt

bash "$ROOT/bin/mycelium-reflect.sh" \
  "omega provenance" "cap-final" "linux smoke failed" "fix and rerun" > reflection.txt
grep -qF "reflection-cap-final" reflection.txt
grep -qF "linux smoke failed" .mycelium/nodes/reflection-cap-final.md

printf '%s\n' 'status is green' > green.md
printf '%s\n' 'status is red' > red.md
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "gamma status" "node-green" hyphae complete "status is green" "none" "wake stem" \
  --topics "gamma,status" --evidence "green.md:1" --confidence high --consumes "none" --blocks "none" --source-refs "green.md:1" > /dev/null
bash "$ROOT/bin/mycelium-node.sh" \
  --allow-untracked "smoke fixture" \
  "gamma status" "node-red" hyphae complete "status is red" "none" "wake stem" \
  --topics "gamma,status" --evidence "red.md:1" --confidence high --consumes "none" --blocks "none" --source-refs "red.md:1" > /dev/null

set +e
printf '' |
  bash "$ROOT/bin/stem.sh" "gamma status" --for-cap |
  bash "$ROOT/bin/cap.sh" "gamma status" > conflict-cap.txt
conflict_status=$?
set -e
test "$conflict_status" -eq 1
grep -qF "Verification: FAIL" conflict-cap.txt
grep -qF "return to STEM" conflict-cap.txt

"$PYTHON" "$ROOT/evals/run-retrieval.py" > retrieval-eval.json
"$PYTHON" -c 'import json; result=json.load(open("retrieval-eval.json", encoding="utf-8")); assert 15 <= result["query_count"] <= 25 and result["thresholds_met"]'
"$PYTHON" "$ROOT/tests/test_stage_treatment.py" > /dev/null
"$PYTHON" "$ROOT/tests/test_node_writer.py" > /dev/null
"$PYTHON" -B -m unittest discover -s "$ROOT/benchmarks/swebench_live" -p 'test_*.py' > /dev/null

echo "Linux smoke tests passed"
