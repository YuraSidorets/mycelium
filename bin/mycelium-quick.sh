#!/bin/sh
set -eu

usage() {
    printf '%s\n' "Usage: mycelium-quick.sh <goal> <apex-facts> <apex-evidence> <stem-facts> <cap-facts> [run-id] [topics] [confidence]" >&2
}

if [ "$#" -lt 5 ]; then
    usage
    exit 1
fi

goal=$1
apex_facts=$2
apex_evidence=$3
stem_facts=$4
cap_facts=$5
run_id=${6:-}
topics=${7:-quick}
confidence=${8:-high}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lineage_script="$script_dir/mycelium-lineage.sh"
node_script="$script_dir/mycelium-node.sh"

if [ -z "$run_id" ]; then
    slug=$(printf '%s' "$goal" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')
    if [ -z "$slug" ]; then slug="goal"; fi
    suffix=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
    run_id="${slug}-${suffix}"
fi

apex_node_id="${run_id}-apex"
stem_node_id="${run_id}-stem"
cap_node_id="${run_id}-cap"

"$lineage_script" begin --goal "$goal" --run-id "$run_id" --topology-preset apex-stem-cap >/dev/null

"$node_script" "$goal" "$apex_node_id" apex complete "$apex_facts" none none \
    --topics "$topics" --evidence "$apex_evidence" --confidence "$confidence" \
    --consumes none --blocks none --run-id "$run_id" >/dev/null

"$node_script" "$goal" "$stem_node_id" stem complete "$stem_facts" none none \
    --topics "$topics" --evidence "$apex_node_id" --confidence "$confidence" \
    --consumes "$apex_node_id" --blocks none --run-id "$run_id" \
    --process-input "$apex_node_id" >/dev/null

"$node_script" "$goal" "$cap_node_id" cap complete "$cap_facts" none "host: run external verification" \
    --topics "$topics" --evidence "$stem_node_id" --confidence "$confidence" \
    --consumes "$stem_node_id" --blocks none --run-id "$run_id" \
    --process-input "$stem_node_id" >/dev/null

"$lineage_script" seal --run-id "$run_id"
