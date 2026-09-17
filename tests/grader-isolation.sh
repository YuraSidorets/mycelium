#!/usr/bin/env bash
set -euo pipefail

ORACLE=/usr/local/bin/mycelium-workflow-oracle
PRIVATE=/opt/mycelium-grader

id -u | grep -qx 20000
grep -q '^CapEff:[[:space:]]*0000000000000000$' /proc/self/status
! id -G | tr ' ' '\n' | grep -qx 20001
test -x "$ORACLE"
test ! -r "$ORACLE"
test ! -r "$PRIVATE/workflow-oracle.ps1"
test ! -r "$PRIVATE/verify-cap.py"
test ! -r "$PRIVATE/mycelium_lineage.py"
! cat "$PRIVATE/workflow-oracle.ps1" >/dev/null 2>&1
! cat "$PRIVATE/mycelium_lineage.py" >/dev/null 2>&1
! python3 -c 'open("/opt/mycelium-grader/workflow-oracle.ps1").read()' >/dev/null 2>&1
! python3 -c 'open("/opt/mycelium-grader/mycelium_lineage.py").read()' >/dev/null 2>&1
! pwsh -NoProfile -Command 'Get-Content /opt/mycelium-grader/workflow-oracle.ps1' >/dev/null 2>&1
! pwsh -NoProfile -Command 'Get-Content /opt/mycelium-grader/mycelium_lineage.py' >/dev/null 2>&1

write_tracked_node() {
  local node_id="$1" role="$2" fact="$3" source_ref="$4" parent="${5:-}"
  local lineage_args=()
  if [[ -n "$parent" ]]; then lineage_args=(--process-input "$parent"); fi
  bash bin/mycelium-node.sh \
    "lineage proof" "$node_id" "$role" complete \
    "$fact" "none" "continue" \
    --topics "lineage proof" \
    --evidence "$source_ref" \
    --confidence high \
    --consumes "${parent:-none}" \
    --blocks "none" \
    --source-refs "$source_ref" \
    --run-id "tracked-eval" \
    "${lineage_args[@]}" >/dev/null
}

printf '%s\n' \
  "apex lineage proof" \
  "septum lineage proof" \
  "hyphae lineage proof" \
  "stem lineage proof" \
  "cap lineage proof" > lineage-source.md
bash bin/mycelium-lineage.sh --root . begin \
  --goal "lineage proof" --run-id "tracked-eval" >/dev/null
write_tracked_node "tracked-a" apex "apex lineage proof" "lineage-source.md:1"
write_tracked_node "tracked-s" septum "septum lineage proof" "lineage-source.md:2" "tracked-a"
write_tracked_node "tracked-h" hyphae "hyphae lineage proof" "lineage-source.md:3" "tracked-s"
write_tracked_node "tracked-stem" stem "stem lineage proof" "lineage-source.md:4" "tracked-h"
write_tracked_node "tracked-cap" cap "cap lineage proof" "lineage-source.md:5" "tracked-stem"
bash bin/mycelium-lineage.sh --root . seal --run-id "tracked-eval" >/dev/null
printf '' |
  bash bin/stem.sh "lineage proof" --for-cap |
  bash bin/cap.sh "lineage proof" > tracked-cap.txt

test "$(stat -c '%a' .mycelium/runs/tracked-eval.lock)" = 644
test "$(stat -c '%a' .mycelium/runs/tracked-eval.json)" = 644
test "$(stat -c '%a' .mycelium/nodes/tracked-cap.md)" = 644
test "$(stat -c '%a' .mycelium/flows/lineage-proof.jsonl)" = 644
test "$(stat -c '%a' .mycelium/runs)" = 755
test "$($ORACLE tracked-lineage-roundtrip)" = 'MYCELIUM_TRACKED_G72A'

mkdir -p .mycelium/flows
printf '%s\n' \
  'The dashboard listens on port 3300.' \
  'The dashboard color is blue.' \
  'The unrelated color is blue.' > facts.md
cat > ephemeral-cap.txt <<'EOF'
Goal: dashboard port
Selected results:
- [ephemeral] The dashboard listens on port 3300. (facts.md:1)
Verification: EXTERNAL_REQUIRED
EOF
cat > .mycelium/flows/stem-ledger-test.json <<'EOF'
{"goal":"dashboard port","selected_nodes":["facts.md:1"],"ignored_nodes":[],"conflicts":[],"cap_dispatch":"verify"}
EOF

test "$("$ORACLE" ephemeral-pipeline)" = 'MYCELIUM_PIPELINE_F6C1'
test "$(env PATH=/tmp HOME=/tmp PSModulePath=/tmp LD_PRELOAD=/tmp/not-real "$ORACLE" ephemeral-pipeline)" = 'MYCELIUM_PIPELINE_F6C1'

assert_denied() {
  set +e
  "$@" >/tmp/oracle.out 2>/tmp/oracle.err
  status=$?
  set -e
  test "$status" -eq 126
  test ! -s /tmp/oracle.out
  grep -qx 'oracle unavailable' /tmp/oracle.err
}

assert_denied "$ORACLE"
assert_denied "$ORACLE" --help
assert_denied "$ORACLE" unknown
assert_denied "$ORACLE" ephemeral-pipeline extra

rm facts.md
ln -s "$PRIVATE/workflow-oracle.ps1" facts.md
set +e
"$ORACLE" ephemeral-pipeline >/tmp/oracle.out 2>/tmp/oracle.err
status=$?
set -e
test "$status" -eq 1
test ! -s /tmp/oracle.out
grep -qx 'workflow validation failed' /tmp/oracle.err

rm facts.md
printf '%s\n' \
  'The dashboard listens on port 3300.' \
  'The dashboard color is blue.' \
  'The unrelated color is blue.' > facts.md
rm .mycelium/flows/stem-ledger-test.json
ln -s "$PRIVATE/workflow-oracle.ps1" .mycelium/flows/stem-ledger-test.json
set +e
"$ORACLE" ephemeral-pipeline >/tmp/oracle.out 2>/tmp/oracle.err
status=$?
set -e
test "$status" -eq 1
test ! -s /tmp/oracle.out
grep -qx 'workflow validation failed' /tmp/oracle.err

echo "grader isolation passed"
