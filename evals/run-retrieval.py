#!/usr/bin/env python3
"""Run the frozen Mycelium fielded-retrieval experiment."""

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MYCELIUM = ROOT / "bin" / "mycelium.py"
VERIFY_CAP = ROOT / "evals" / "verify-cap.py"


def run_cli(workspace, *arguments, stdin=""):
    result = subprocess.run(
        [sys.executable, str(MYCELIUM), *arguments],
        cwd=workspace,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f'Mycelium {" ".join(arguments[:2])} failed: {result.stderr.strip()}'
        )
    return result.stdout


def write_fixture(workspace, manifest):
    workspace = workspace.resolve()
    for relative, lines in manifest["sources"].items():
        target = (workspace / relative).resolve()
        target.relative_to(workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for node in manifest["nodes"]:
        arguments = [
            "node",
            node["goal"],
            node["node_id"],
            node.get("role", "hyphae"),
            node.get("status", "complete"),
            "\n".join(node["facts"]),
            "none",
            "dispatch cap",
            "--topics",
            node.get("topics", ""),
            "--evidence",
            node.get("evidence", ""),
            "--confidence",
            node.get("confidence", "high"),
            "--consumes",
            node.get("consumes", "none"),
            "--blocks",
            "none",
            "--source-refs",
            node.get("source_refs", ""),
            "--depends-on",
            node.get("depends_on", ""),
            "--supersedes",
            node.get("supersedes", ""),
            # The retrieval corpus is a fixture, not a tracked run.
            "--allow-untracked",
            "retrieval eval fixture",
        ]
        if node.get("invalidated"):
            arguments.append("--invalidated")
        run_cli(workspace, *arguments)


def evaluate(manifest):
    expected_total = 0
    expected_found = 0
    paraphrase_expected = 0
    paraphrase_found = 0
    stale_total = 0
    stale_selected = 0
    final_total = 0
    final_correct = 0
    context_sizes = []
    query_results = []

    with tempfile.TemporaryDirectory(prefix=".mycelium-retrieval-", dir=ROOT) as temporary:
        workspace = Path(temporary)
        write_fixture(workspace, manifest)

        for query in manifest["queries"]:
            search_arguments = ["search", query["goal"]]
            if query.get("topic"):
                search_arguments.append(query["topic"])
            output = run_cli(workspace, *search_arguments)
            selected = [
                line.split("\t", 1)[0]
                for line in output.splitlines()
                if line.strip()
            ][:5]
            expected = query.get("expected_ids", [])
            stale = query.get("stale_ids", [])
            found = [node_id for node_id in expected if node_id in selected]
            selected_stale = [node_id for node_id in stale if node_id in selected]
            context_bytes = len(output.encode("utf-8"))

            expected_total += len(expected)
            expected_found += len(found)
            if query["category"] == "paraphrase":
                paraphrase_expected += len(expected)
                paraphrase_found += len(found)
            stale_total += len(stale)
            stale_selected += len(selected_stale)
            context_sizes.append(context_bytes)

            stem_output = run_cli(
                workspace, "stem", query["goal"], "--for-cap", stdin=output
            )
            expected_cap_exit = query.get("cap_exit", 0)
            cap = subprocess.run(
                [sys.executable, str(MYCELIUM), "cap", query["goal"]],
                cwd=workspace,
                input=stem_output,
                text=True,
                capture_output=True,
                check=False,
            )
            required_ids = expected if expected_cap_exit == 0 else []
            stem_ok = (
                all(node_id in stem_output for node_id in required_ids)
                and all(node_id not in stem_output for node_id in stale)
                and all(
                    token in stem_output
                    for token in query.get("stem_contains", [])
                )
                and all(
                    token not in stem_output
                    for token in query.get("stem_excludes", [])
                )
            )
            cap_ok = cap.returncode == expected_cap_exit
            cap_path = workspace / f'cap-{query["id"]}.txt'
            cap_path.write_text(cap.stdout, encoding="utf-8")
            verified = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_CAP),
                    str(workspace),
                    cap_path.name,
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            if expected_cap_exit == 0:
                cap_ok = (
                    cap_ok
                    and verified.returncode == 0
                    and "Verification: PASS" in verified.stdout
                )
            else:
                cap_ok = (
                    cap_ok
                    and "Verification: FAIL" in cap.stdout
                    and verified.returncode == 1
                    and "Verification: FAIL" in verified.stdout
                )
            final_ok = stem_ok and cap_ok
            final_total += 1
            final_correct += int(final_ok)

            query_results.append(
                {
                    "id": query["id"],
                    "category": query["category"],
                    "selected": selected,
                    "missed": [node_id for node_id in expected if node_id not in selected],
                    "stale_selected": selected_stale,
                    "context_bytes": context_bytes,
                    "final_correct": final_ok,
                }
            )

    recall = expected_found / expected_total if expected_total else 1.0
    paraphrase_recall = (
        paraphrase_found / paraphrase_expected if paraphrase_expected else 1.0
    )
    stale_rate = stale_selected / stale_total if stale_total else 0.0
    final_accuracy = final_correct / final_total if final_total else 1.0
    sorted_sizes = sorted(context_sizes)
    p95_index = max(0, math.ceil(len(sorted_sizes) * 0.95) - 1)
    metrics = {
        "version": manifest["version"],
        "query_count": len(query_results),
        "recall_at_5": round(recall, 4),
        "paraphrase_recall_at_5": round(paraphrase_recall, 4),
        "stale_selection_rate": round(stale_rate, 4),
        "context_bytes_to_stem": {
            "mean": round(sum(context_sizes) / len(context_sizes), 1),
            "p95": sorted_sizes[p95_index],
            "max": max(context_sizes),
        },
        "final_answer_correctness": round(final_accuracy, 4),
        "missed_queries": [
            result["id"] for result in query_results if result["missed"]
        ],
        "queries": query_results,
    }
    semantic_gate = manifest["semantic_gate"]
    metrics["semantic_search_gate"] = {
        **semantic_gate,
        "lexical_gap_detected": (
            paraphrase_recall
            < semantic_gate["lexical_paraphrase_recall_below"]
        ),
        "hybrid_evaluated": False,
        "adopt_semantic_search": False,
    }

    thresholds = manifest["thresholds"]
    failures = []
    if recall < thresholds["min_recall_at_5"]:
        failures.append("recall_at_5")
    if stale_rate > thresholds["max_stale_selection_rate"]:
        failures.append("stale_selection_rate")
    if max(context_sizes) > thresholds["max_context_bytes_per_query"]:
        failures.append("context_bytes_to_stem")
    if final_accuracy < thresholds["min_final_answer_correctness"]:
        failures.append("final_answer_correctness")
    metrics["thresholds"] = thresholds
    metrics["thresholds_met"] = not failures
    metrics["failed_thresholds"] = failures
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("retrieval-v1.json"),
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    metrics = evaluate(manifest)
    print(json.dumps(metrics, indent=2))
    return 0 if metrics["thresholds_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
