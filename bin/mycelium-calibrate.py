#!/usr/bin/env python3
"""Confidence calibration ledger.

Reads every durable node in the current workspace and answers one question:
how often did nodes that declared a given confidence level later receive a
reflection? The link already exists in the corpus -- `mycelium reflect` writes
a node whose `topics` include `reflection` and whose `consumes` names the node
that failed -- so this is pure read-side aggregation with no schema change.

Writes `.mycelium/calibration.json` and prints its path.
"""

import importlib.util
import json
import sys
from pathlib import Path


CONFIDENCE_BUCKETS = ("high", "medium", "low", "unset")


def load_core():
    module_path = Path(__file__).resolve().with_name("mycelium.py")
    if not module_path.is_file():
        raise ValueError(f"calibration requires {module_path}")
    spec = importlib.util.spec_from_file_location("mycelium_calibrate_core", module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load Mycelium core module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def topic_list(node):
    return [part.strip() for part in str(node.get("topics", "")).lower().split(",")]


def is_reflection(node):
    return "reflection" in topic_list(node)


def confidence_bucket(node):
    value = str(node.get("confidence", "")).strip().lower()
    return value if value in CONFIDENCE_BUCKETS else "unset"


def build_ledger(core, nodes, generated):
    reflections = [node for node in nodes if is_reflection(node)]
    subjects = [node for node in nodes if not is_reflection(node)]

    # A reflection can name more than one consumed node; every named id counts.
    reflections_by_subject = {}
    for reflection in reflections:
        for consumed in core.relation_ids(reflection.get("consumes", "")):
            reflections_by_subject.setdefault(consumed, [])
            if reflection["nodeId"] not in reflections_by_subject[consumed]:
                reflections_by_subject[consumed].append(reflection["nodeId"])

    by_confidence = {
        bucket: {"total": 0, "later_reflected": 0, "rate": 0.0}
        for bucket in CONFIDENCE_BUCKETS
    }
    reflected_nodes = []
    for node in subjects:
        bucket = confidence_bucket(node)
        by_confidence[bucket]["total"] += 1
        consuming = sorted(reflections_by_subject.get(node["nodeId"], []))
        if not consuming:
            continue
        by_confidence[bucket]["later_reflected"] += 1
        reflected_nodes.append(
            {
                "nodeId": node["nodeId"],
                "confidence": bucket,
                "role": node.get("role", ""),
                "reflections": consuming,
            }
        )
    for stats in by_confidence.values():
        if stats["total"]:
            stats["rate"] = round(stats["later_reflected"] / stats["total"], 4)
    reflected_nodes.sort(key=lambda entry: entry["nodeId"])

    return {
        "generated": generated,
        "by_confidence": by_confidence,
        "reflected_nodes": reflected_nodes,
    }


def write_ledger():
    core = load_core()
    skipped = []
    nodes = core.load_nodes(skipped=skipped)
    ledger = build_ledger(core, nodes, core.timestamp())
    ledger_path = Path(".mycelium/calibration.json")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return ledger_path, nodes, skipped


def main():
    try:
        ledger_path, nodes, skipped = write_ledger()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    if skipped:
        print(
            f"{len(nodes)} nodes loaded, {len(skipped)} skipped "
            "(run mycelium-lint for detail)",
            file=sys.stderr,
        )
    print(ledger_path.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
