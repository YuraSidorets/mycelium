#!/usr/bin/env python3
"""Read-only Mycelium graph projection and local Atlas HTTP server."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


sys.dont_write_bytecode = True


SCHEMA_VERSION = 2
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/atlas.css": ("atlas.css", "text/css; charset=utf-8"),
    "/atlas.js": ("atlas.js", "text/javascript; charset=utf-8"),
    "/atlas-layout.js": ("atlas-layout.js", "text/javascript; charset=utf-8"),
    "/atlas-model.js": ("atlas-model.js", "text/javascript; charset=utf-8"),
    "/atlas-draft-store.js": ("atlas-draft-store.js", "text/javascript; charset=utf-8"),
}


def _load_core():
    module_path = Path(__file__).with_name("mycelium.py")
    spec = importlib.util.spec_from_file_location("mycelium_atlas_core", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load adjacent mycelium.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORE = _load_core()


def _load_lineage():
    module_path = Path(__file__).with_name("mycelium_lineage.py")
    spec = importlib.util.spec_from_file_location("mycelium_atlas_lineage", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load adjacent mycelium_lineage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LINEAGE = _load_lineage()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _artifact_is_contained(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _safe_error(error: Exception, root: Path) -> str:
    message = f"{type(error).__name__}: {error}"
    for value in (str(root), root.as_posix()):
        message = message.replace(value, ".")
    return message


def _add_diagnostic(
    diagnostics: list[dict],
    code: str,
    message: str,
    *,
    severity: str = "warning",
    artifact_path: str = "",
    line: int | None = None,
    node_graph_id: str = "",
    **details,
) -> None:
    diagnostic = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if artifact_path:
        diagnostic["artifactPath"] = artifact_path
    if line is not None:
        diagnostic["line"] = line
    if node_graph_id:
        diagnostic["nodeGraphId"] = node_graph_id
    diagnostic.update(details)
    diagnostics.append(diagnostic)


def _section_rows(text: str, name: str) -> list[dict]:
    rows = []
    active = False
    heading = re.compile(rf"^## {re.escape(name)}[ \t]*$")
    for line_number, line in enumerate(text.splitlines(), 1):
        if heading.match(line):
            active = True
            continue
        if active and line.startswith("## "):
            break
        if active and line.strip():
            rows.append({"line": line_number, "text": line})
    return rows


def _fallback_body_fields(text: str) -> dict[str, str]:
    header = re.split(r"(?m)^## Facts\s*$", text, maxsplit=1)[0]
    result = {}
    for name in ("Evidence", "Trace", "Reflection"):
        matches = re.findall(rf"(?m)^{name}:[ \t]*([^\r\n]*)\r?$", header)
        result[name.lower()] = matches[0] if matches else ""
    return result


def _split_topics(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]


def _canonical_fact(value: str) -> str:
    normalized = CORE.canonical_facts([value])
    return normalized[0] if normalized else ""


def _validate_source_refs(
    root: Path,
    raw_value: str,
    fact_rows: list[dict],
    diagnostics: list[dict],
    artifact_path: str,
    graph_id: str,
) -> tuple[list[dict], bool]:
    references = CORE.relation_ids(raw_value)
    normalized_facts = [
        {"line": row["line"], "text": row["text"], "normalized": _canonical_fact(row["text"])}
        for row in fact_rows
    ]
    source_candidates: set[str] = set()
    results = []

    for raw_reference in references:
        result = {
            "raw": raw_reference,
            "path": "",
            "startLine": None,
            "endLine": None,
            "valid": False,
            "current": False,
            "supportsFactLines": [],
            "reason": "invalid-format",
        }
        match = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", raw_reference)
        if not match:
            _add_diagnostic(
                diagnostics,
                "invalid-source-ref",
                f"Source reference has invalid syntax: {raw_reference}",
                artifact_path=artifact_path,
                node_graph_id=graph_id,
                reference=raw_reference,
            )
            results.append(result)
            continue

        raw_path = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3) or match.group(2))
        result.update(startLine=start, endLine=end)
        candidate = (root / raw_path).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            result["reason"] = "outside-repository"
            _add_diagnostic(
                diagnostics,
                "source-ref-outside-repository",
                f"Source reference leaves the repository: {raw_reference}",
                artifact_path=artifact_path,
                node_graph_id=graph_id,
                reference=raw_reference,
            )
            results.append(result)
            continue

        result["path"] = relative
        if not candidate.is_file():
            result["reason"] = "missing-file"
            _add_diagnostic(
                diagnostics,
                "missing-source-ref",
                f"Source reference file does not exist: {raw_reference}",
                artifact_path=artifact_path,
                node_graph_id=graph_id,
                reference=raw_reference,
            )
            results.append(result)
            continue

        try:
            source_lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            result["reason"] = "read-error"
            _add_diagnostic(
                diagnostics,
                "source-ref-read-error",
                f"Cannot read source reference {raw_reference}: {_safe_error(error, root)}",
                artifact_path=artifact_path,
                node_graph_id=graph_id,
                reference=raw_reference,
            )
            results.append(result)
            continue

        if start < 1 or end < start or end > len(source_lines):
            result["reason"] = "line-range-out-of-bounds"
            _add_diagnostic(
                diagnostics,
                "source-ref-range-error",
                f"Source reference line range is out of bounds: {raw_reference}",
                artifact_path=artifact_path,
                node_graph_id=graph_id,
                reference=raw_reference,
            )
            results.append(result)
            continue

        source_range = source_lines[start - 1 : end]
        local_candidates = set(CORE.canonical_facts(source_range))
        local_candidates.update(CORE.canonical_facts([" ".join(source_range)]))
        source_candidates.update(local_candidates)
        supporting_lines = [
            fact["line"]
            for fact in normalized_facts
            if fact["normalized"] in local_candidates
        ]
        result.update(
            valid=True,
            current=not normalized_facts or bool(supporting_lines),
            supportsFactLines=supporting_lines,
            reason="ok" if supporting_lines or not normalized_facts else "no-fact-match",
        )
        results.append(result)

    source_current = (
        bool(references)
        and len(results) == len(references)
        and all(result["valid"] for result in results)
        and all(fact["normalized"] in source_candidates for fact in normalized_facts)
    )
    if references and not source_current:
        _add_diagnostic(
            diagnostics,
            "stale-source-refs",
            "Source references do not currently support every fact.",
            artifact_path=artifact_path,
            node_graph_id=graph_id,
        )
    return results, source_current


def _flow_event(record: dict, artifact_path: str, line_number: int, key: tuple) -> dict:
    return {
        "eventId": f"flow:{artifact_path}:{line_number}",
        "artifactPath": artifact_path,
        "line": line_number,
        "nodeId": record["nodeId"],
        "goal": record["goal"],
        "timestamp": key[2],
        "role": record["role"],
        "status": record["status"],
        "topics": record["topics"],
        "evidence": record["evidence"],
        "confidence": record["confidence"],
        "consumes": record["consumes"],
        "blocks": record["blocks"],
        "trace": record["trace"],
        "reflection": record["reflection"],
        "parentGoalId": record["parentGoalId"],
        "producingAgent": record["producingAgent"],
        "version": record["version"],
        "sourceRefs": record["sourceRefs"],
        "dependsOn": CORE.relation_ids(record["dependsOn"]),
        "supersedes": CORE.relation_ids(record["supersedes"]),
        "invalidated": record["invalidated"],
        "facts": list(CORE.canonical_facts(record["facts"])),
        "questions": list(CORE.canonical_lines(record["questions"], "questions", allow_null=True)),
        "next": list(CORE.canonical_lines(record["next"], "next", allow_null=True)),
    }


def _load_flow_events(root: Path, diagnostics: list[dict]) -> list[dict]:
    flow_dir = root / ".mycelium" / "flows"
    if not flow_dir.is_dir():
        return []
    events = []
    for path in sorted(flow_dir.glob("*.jsonl")):
        artifact_path = _relative(root, path)
        if not _artifact_is_contained(root, path):
            _add_diagnostic(
                diagnostics,
                "artifact-symlink-outside-repository",
                "Flow artifact symlink leaves the repository and was ignored.",
                artifact_path=artifact_path,
            )
            continue
        try:
            handle = path.open("rb")
        except OSError as error:
            _add_diagnostic(
                diagnostics,
                "flow-file-read-error",
                f"Cannot read flow file: {_safe_error(error, root)}",
                severity="error",
                artifact_path=artifact_path,
            )
            continue
        with handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    continue
                try:
                    line = raw_line.decode("utf-8")
                    record = json.loads(line, object_pairs_hook=CORE._json_object)
                    if not isinstance(record, dict):
                        raise ValueError("flow record must be a JSON object")
                    key = CORE.flow_key(
                        record,
                        record.get("facts"),
                        record.get("questions"),
                        record.get("next"),
                    )
                    events.append(
                        {
                            "key": key,
                            "record": record,
                            "public": _flow_event(record, artifact_path, line_number, key),
                        }
                    )
                except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
                    _add_diagnostic(
                        diagnostics,
                        "malformed-flow-record",
                        f"Flow record was ignored: {_safe_error(error, root)}",
                        artifact_path=artifact_path,
                        line=line_number,
                    )
    events.sort(
        key=lambda event: (
            event["public"]["timestamp"],
            event["public"]["artifactPath"],
            event["public"]["line"],
        )
    )
    return events


def _parse_node(
    root: Path,
    path: Path,
    events_by_key: dict[tuple, list[dict]],
    history_by_node_id: dict[str, list[dict]],
    diagnostics: list[dict],
) -> dict | None:
    artifact_path = _relative(root, path)
    graph_id = f"node:{artifact_path}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        _add_diagnostic(
            diagnostics,
            "node-file-read-error",
            f"Cannot read node artifact: {_safe_error(error, root)}",
            severity="error",
            artifact_path=artifact_path,
        )
        return None

    try:
        meta = CORE.metadata(text)
    except ValueError as error:
        _add_diagnostic(
            diagnostics,
            "malformed-node",
            f"Node artifact was ignored: {_safe_error(error, root)}",
            artifact_path=artifact_path,
        )
        return None

    facts_text = CORE.section(text, "Facts")
    questions_text = CORE.section(text, "Questions")
    next_text = CORE.section(text, "Next")
    facts = _section_rows(text, "Facts")
    questions = [row["text"] for row in _section_rows(text, "Questions")]
    next_steps = [row["text"] for row in _section_rows(text, "Next")]

    body_valid = True
    try:
        body = CORE.body_fields(text)
    except ValueError as error:
        body_valid = False
        body = _fallback_body_fields(text)
        _add_diagnostic(
            diagnostics,
            "malformed-node-body",
            f"Node body fields are invalid: {_safe_error(error, root)}",
            severity="error",
            artifact_path=artifact_path,
            node_graph_id=graph_id,
        )

    commit_key = None
    if body_valid:
        try:
            commit_key = CORE.flow_key(
                {**meta, **body}, facts_text, questions_text, next_text, node=True
            )
        except ValueError as error:
            _add_diagnostic(
                diagnostics,
                "invalid-node-projection",
                f"Node cannot match a flow commit: {_safe_error(error, root)}",
                severity="error",
                artifact_path=artifact_path,
                node_graph_id=graph_id,
            )

    raw_id = meta.get("nodeid", path.stem)
    raw_role = meta.get("role", "")
    normalized_role = raw_role.lower()
    role = normalized_role if normalized_role in CORE.ROLES else "unknown"
    raw_status = meta.get("status", "")
    normalized_status = raw_status.lower()
    status = normalized_status if normalized_status in CORE.STATUSES else raw_status or "unknown"
    raw_confidence = meta.get("confidence", "")
    normalized_confidence = raw_confidence.lower()
    confidence = (
        normalized_confidence
        if normalized_confidence in CORE.CONFIDENCES
        else raw_confidence
    )
    raw_invalidated = meta.get("invalidated", "")
    invalidated_valid = raw_invalidated in ("true", "false")
    if not invalidated_valid:
        _add_diagnostic(
            diagnostics,
            "invalid-invalidated-value",
            "Node invalidated must be the lowercase literal true or false.",
            artifact_path=artifact_path,
            node_graph_id=graph_id,
        )

    raw_updated = meta.get("updated", "")
    try:
        updated = CORE.canonical_timestamp(raw_updated)
    except ValueError:
        updated = raw_updated

    matching_events = events_by_key.get(commit_key, []) if commit_key is not None else []
    matching_flows = [
        {
            "eventId": event["public"]["eventId"],
            "artifactPath": event["public"]["artifactPath"],
            "line": event["public"]["line"],
            "timestamp": event["public"]["timestamp"],
        }
        for event in matching_events
    ]
    if len(matching_flows) > 1:
        _add_diagnostic(
            diagnostics,
            "duplicate-flow-commit",
            "More than one flow record matches this node projection.",
            artifact_path=artifact_path,
            node_graph_id=graph_id,
            matchingFlows=matching_flows,
        )

    source_refs, source_current = _validate_source_refs(
        root,
        meta.get("source_refs", ""),
        facts,
        diagnostics,
        artifact_path,
        graph_id,
    )
    history = [
        {**event["public"], "matchesProjection": commit_key is not None and event["key"] == commit_key}
        for event in history_by_node_id.get(raw_id, [])
    ]

    node = {
        "graphId": graph_id,
        "nodeId": raw_id,
        "rawId": raw_id,
        "goal": meta.get("goal", ""),
        "role": role,
        "rawRole": raw_role,
        "status": status,
        "rawStatus": raw_status,
        "confidence": confidence,
        "rawConfidence": raw_confidence,
        "topics": _split_topics(meta.get("topics", "")),
        "topicsRaw": meta.get("topics", ""),
        "facts": facts,
        "factsText": facts_text,
        "evidence": body.get("evidence", ""),
        "questions": questions,
        "questionsText": questions_text,
        "next": next_steps,
        "nextText": next_text,
        "trace": body.get("trace", ""),
        "reflection": body.get("reflection", ""),
        "consumes": meta.get("consumes", ""),
        "blocks": meta.get("blocks", ""),
        "parentGoalId": meta.get("parent_goal_id", ""),
        "producingAgent": meta.get("producing_agent", ""),
        "version": meta.get("version", ""),
        "sourceRefsRaw": meta.get("source_refs", ""),
        "sourceRefs": source_refs,
        "dependsOnRaw": meta.get("depends_on", ""),
        "dependsOn": CORE.relation_ids(meta.get("depends_on", "")),
        "supersedesRaw": meta.get("supersedes", ""),
        "supersedes": CORE.relation_ids(meta.get("supersedes", "")),
        # Optional re-execution contract; empty on every node written before it
        # existed, and never run by the inspector itself.
        "command": meta.get("command", ""),
        "expect": meta.get("expect", ""),
        # Why this node was written outside a tracked run, when it was. Empty on
        # every tracked node and on every node written before the field existed.
        "untrackedReason": meta.get("untracked_reason", ""),
        "invalidated": raw_invalidated.lower() == "true",
        "invalidatedRaw": raw_invalidated,
        "updated": updated,
        "updatedRaw": raw_updated,
        "artifactPath": artifact_path,
        "matchingFlow": matching_flows[-1] if matching_flows else None,
        "matchingFlows": matching_flows,
        "flowHistory": history,
        "lifecycle": {
            "flowCommitted": bool(matching_flows),
            "eligibleCurrent": False,
            "sourceCurrent": source_current,
            "superseded": False,
            "searchVisible": False,
            "capEligible": False,
        },
        "classification": "uncommitted",
        "_core": {
            "nodeId": raw_id,
            "invalidated": raw_invalidated,
            "flowCommitted": bool(matching_flows),
            "status": raw_status,
            "source_refs": meta.get("source_refs", ""),
            "supersedes": meta.get("supersedes", ""),
        },
    }
    if not matching_flows:
        _add_diagnostic(
            diagnostics,
            "missing-flow-commit",
            "No flow record matches this node projection.",
            artifact_path=artifact_path,
            node_graph_id=graph_id,
        )
    return node


def _classification(node: dict) -> str:
    lifecycle = node["lifecycle"]
    if not lifecycle["flowCommitted"]:
        return "uncommitted"
    if node["invalidated"]:
        return "invalidated"
    if lifecycle["superseded"]:
        return "superseded"
    if node["status"] == "blocked":
        return "blocked"
    if node["status"] == "alive":
        return "alive"
    if not node["sourceRefs"]:
        return "source-less"
    if not lifecycle["sourceCurrent"]:
        return "source-drifted"
    if lifecycle["capEligible"]:
        return "cap-eligible"
    if lifecycle["eligibleCurrent"]:
        return "eligible-current"
    return "ineligible"


def _build_edges(nodes: list[dict], diagnostics: list[dict]) -> list[dict]:
    nodes_by_id: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        nodes_by_id[node["rawId"]].append(node)

    for raw_id, duplicates in sorted(nodes_by_id.items()):
        if len(duplicates) > 1:
            _add_diagnostic(
                diagnostics,
                "duplicate-node-id",
                f"Multiple node artifacts declare the case-sensitive ID {raw_id}.",
                severity="error",
                nodeId=raw_id,
                artifactPaths=sorted(node["artifactPath"] for node in duplicates),
            )

    edges = []
    for declaring in nodes:
        for edge_type, targets in (
            ("depends_on", declaring["dependsOn"]),
            ("supersedes", declaring["supersedes"]),
        ):
            for ordinal, declared_target_id in enumerate(targets, 1):
                candidates = nodes_by_id.get(declared_target_id, [])
                if not candidates:
                    _add_diagnostic(
                        diagnostics,
                        "dangling-edge-endpoint",
                        f"{edge_type} target does not resolve: {declared_target_id}",
                        artifact_path=declaring["artifactPath"],
                        node_graph_id=declaring["graphId"],
                        edgeType=edge_type,
                        declaredTargetId=declared_target_id,
                    )
                    continue
                if len(candidates) > 1:
                    _add_diagnostic(
                        diagnostics,
                        "ambiguous-edge-endpoint",
                        f"{edge_type} target resolves to duplicate node IDs: {declared_target_id}",
                        severity="error",
                        artifact_path=declaring["artifactPath"],
                        node_graph_id=declaring["graphId"],
                        edgeType=edge_type,
                        declaredTargetId=declared_target_id,
                    )
                    continue

                declared_target = candidates[0]
                source = declared_target["graphId"]
                target = declaring["graphId"]
                is_self = source == target
                if is_self:
                    _add_diagnostic(
                        diagnostics,
                        "self-edge",
                        f"Node declares a self-referential {edge_type} edge.",
                        artifact_path=declaring["artifactPath"],
                        node_graph_id=declaring["graphId"],
                        edgeType=edge_type,
                    )
                edges.append(
                    {
                        "edgeId": f"edge:{edge_type}:{declaring['graphId']}:{ordinal}",
                        "type": edge_type,
                        "source": source,
                        "target": target,
                        "declaringNode": declaring["graphId"],
                        "declaringNodeId": declaring["rawId"],
                        "declaredTargetId": declared_target_id,
                        "rawDirection": {
                            "fromNodeId": declaring["rawId"],
                            "toNodeId": declared_target_id,
                        },
                        "canvasDirection": {"from": source, "to": target},
                        "self": is_self,
                    }
                )
    return edges


def _canonical_digest(value: dict) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_path(root: Path, value) -> Path | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        return None
    resolved = (root / candidate).resolve()
    return resolved if _artifact_is_contained(root, resolved) else None


def _binding_is_current(
    root: Path,
    run: dict,
    node_id: str,
    binding: dict,
    nodes_by_id: dict[str, list[dict]],
) -> tuple[bool, str, dict | None]:
    commit = binding.get("commit")
    if not isinstance(commit, dict):
        return False, "commit is missing", None
    node_commit = commit.get("node")
    flow_commit = commit.get("flow")
    timestamp = commit.get("timestamp")
    if (
        set(commit) != {"node", "flow", "timestamp", "digest"}
        or not isinstance(node_commit, dict)
        or set(node_commit) != {"path", "sha256"}
        or not isinstance(flow_commit, dict)
        or set(flow_commit) != {"path", "line", "sha256"}
        or not isinstance(timestamp, str)
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (node_commit.get("sha256"), flow_commit.get("sha256"), commit.get("digest"))
        )
    ):
        return False, "commit binding is malformed", None

    declared_digest = commit.get("digest")
    digest_input = {key: value for key, value in commit.items() if key != "digest"}
    if not isinstance(declared_digest, str) or declared_digest != _canonical_digest(digest_input):
        return False, "commit digest does not match its binding", None

    candidates = nodes_by_id.get(node_id, [])
    if not candidates:
        return False, "node ID does not resolve uniquely", None

    node_path = _manifest_path(root, node_commit.get("path"))
    if node_path is None:
        resolved = candidates[0] if len(candidates) == 1 else None
        return False, "node path does not match the resolved artifact", resolved
    node_relative = _relative(root, node_path)
    path_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("artifactPath") == node_relative
    ]
    if len(path_candidates) != 1:
        resolved = candidates[0] if len(candidates) == 1 else None
        return False, "node path does not match the resolved artifact", resolved
    node = path_candidates[0]
    expected_role = str(binding.get("role", "")).lower()
    if (
        node["goal"] != run.get("goal")
        or node["role"] != expected_role
        or node["status"] != "complete"
    ):
        return False, "node identity no longer matches the run binding", node

    try:
        node_hash = hashlib.sha256(node_path.read_bytes()).hexdigest()
    except OSError:
        return False, "node artifact cannot be read", node
    if node_hash != node_commit.get("sha256"):
        return False, "node artifact hash changed", node

    flow_path = _manifest_path(root, flow_commit.get("path"))
    line_number = flow_commit.get("line")
    flow_root = (root / ".mycelium" / "flows").resolve()
    if (
        flow_path is None
        or flow_path.suffix != ".jsonl"
        or not _artifact_is_contained(flow_root, flow_path)
        or not isinstance(line_number, int)
        or isinstance(line_number, bool)
        or line_number < 1
    ):
        return False, "flow binding path or line is invalid", node
    flow_relative = _relative(root, flow_path)
    if not any(
        match.get("artifactPath") == flow_relative and match.get("line") == line_number
        for match in node.get("matchingFlows", [])
    ):
        return False, "bound flow line does not match the node's full canonical projection", node
    try:
        flow_lines = flow_path.read_bytes().splitlines()
        raw_line = flow_lines[line_number - 1]
    except (OSError, IndexError):
        return False, "bound flow line cannot be read", node
    if hashlib.sha256(raw_line).hexdigest() != flow_commit.get("sha256"):
        return False, "bound flow line hash changed", node
    try:
        record = json.loads(raw_line.decode("utf-8"), object_pairs_hook=CORE._json_object)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return False, "bound flow line is malformed", node
    if not isinstance(record, dict) or (
        record.get("nodeId") != node_id
        or record.get("goal") != run.get("goal")
        or str(record.get("role", "")).lower() != expected_role
        or str(record.get("status", "")).lower() != "complete"
        or record.get("timestamp") != timestamp
        or node.get("updatedRaw") != timestamp
    ):
        return False, "flow identity no longer matches the run binding", node
    return True, "", node


def _load_process_lineage(
    root: Path,
    nodes: list[dict],
    diagnostics: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    run_dir = root / ".mycelium" / "runs"
    paths = sorted(run_dir.glob("*.json")) if run_dir.is_dir() else []
    nodes_by_id: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        nodes_by_id[node["rawId"]].append(node)
    runs: list[dict] = []
    vertices: list[dict] = []
    edges: list[dict] = []

    def process_diagnostic(code: str, message: str, *, path: str, severity="warning", **details):
        _add_diagnostic(
            diagnostics,
            code,
            message,
            severity=severity,
            artifact_path=path,
            domain="process-lineage",
            **details,
        )

    for path in paths:
        artifact_path = _relative(root, path)
        fallback_id = path.stem
        if not _artifact_is_contained(root, path):
            process_diagnostic(
                "artifact-symlink-outside-repository",
                "Process manifest symlink leaves the repository and was ignored.",
                path=artifact_path,
                runId=fallback_id,
            )
            continue
        try:
            canonical = LINEAGE.inspect_manifest(
                fallback_id,
                root=root,
                require_sealed=True,
            )
            manifest = canonical["manifest"]
            canonical_report = canonical["report"]
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            process_diagnostic(
                "malformed-process-manifest",
                f"Process manifest was ignored: {_safe_error(error, root)}",
                path=artifact_path,
                runId=fallback_id,
            )
            runs.append({
                "runId": fallback_id,
                "goal": "",
                "state": "malformed",
                "schemaVersion": None,
                "artifactPath": artifact_path,
                "createdAt": "",
                "updatedAt": "",
                "sealedAt": "",
                "topology": [],
                "skippedRoles": [],
                "nodeVertexIds": [],
                "authoritative": False,
                "current": False,
            })
            continue

        run_id = manifest.get("run_id") if isinstance(manifest.get("run_id"), str) else fallback_id
        goal = manifest.get("goal") if isinstance(manifest.get("goal"), str) else ""
        state = manifest.get("state") if isinstance(manifest.get("state"), str) else "invalid"
        run_current = bool(canonical_report.get("valid"))
        if not run_current:
            canonical_diagnostics = canonical_report.get("diagnostics", [])
            emitted = False
            for item in canonical_diagnostics:
                if not isinstance(item, dict):
                    continue
                code = item.get("code")
                message = item.get("message")
                if not isinstance(code, str) or not code:
                    code = "invalid-process-manifest"
                if not isinstance(message, str) or not message:
                    message = "Canonical process-lineage validation failed."
                details = {}
                node_id = item.get("node_id")
                if isinstance(node_id, str) and node_id:
                    details["nodeId"] = node_id
                process_diagnostic(
                    code,
                    message,
                    path=artifact_path,
                    severity="error",
                    runId=run_id or fallback_id,
                    **details,
                )
                emitted = True
            if not emitted:
                process_diagnostic(
                    "invalid-process-manifest",
                    "Canonical process-lineage validation failed without diagnostics.",
                    path=artifact_path,
                    severity="error",
                    runId=run_id or fallback_id,
                )
        if (
            manifest.get("schema") != 1
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id or "") is None
            or run_id != fallback_id
            or not goal.strip()
            or "\n" in goal
            or "\r" in goal
            or state not in ("open", "sealed", "invalid")
        ):
            run_current = False
            process_diagnostic(
                "invalid-process-manifest",
                "Process manifest identity or schema is invalid.",
                path=artifact_path,
                severity="error",
                runId=run_id or fallback_id,
            )
        if state == "open":
            run_current = False
            process_diagnostic(
                "open-process-run",
                "Process run is open and is not authoritative.",
                path=artifact_path,
                runId=run_id,
            )
        elif state == "invalid":
            run_current = False
            process_diagnostic(
                "invalid-process-run",
                "Process run is invalid and is not authoritative.",
                path=artifact_path,
                severity="error",
                runId=run_id,
            )

        topology = manifest.get("topology") if isinstance(manifest.get("topology"), list) else []
        if not isinstance(manifest.get("topology"), list):
            run_current = False
            process_diagnostic(
                "invalid-process-topology",
                "Process topology must be a list.",
                path=artifact_path,
                severity="error",
                runId=run_id,
            )
        roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []
        expected_roles = list(CORE.ROLES)
        roles_valid = (
            len(roles) == len(expected_roles)
            and all(
                isinstance(item, dict)
                and set(item) == {"role", "state"}
                and item.get("role") == expected_roles[index]
                and item.get("state") in ("pending", "performed", "skipped")
                for index, item in enumerate(roles)
            )
            and not (state == "sealed" and any(item["state"] == "pending" for item in roles))
        )
        if not roles_valid:
            run_current = False
            process_diagnostic(
                "invalid-process-roles",
                "Process role states must contain the ordered five-role state vector.",
                path=artifact_path,
                severity="error",
                runId=run_id,
            )
        topology_by_role = {
            str(item.get("role", "")).lower(): item
            for item in topology
            if isinstance(item, dict)
        }
        skipped_roles = []
        run_vertices = []
        for sequence, item in enumerate(roles, 1):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).lower()
            if item.get("state") == "skipped" and role in CORE.ROLES:
                topology_item = topology_by_role.get(role, {})
                skip = topology_item.get("skip") if isinstance(topology_item.get("skip"), dict) else {}
                marker = {
                    "vertexId": f"process:{run_id}:skip:{role}",
                    "runId": run_id,
                    "graphId": "",
                    "nodeId": "",
                    "role": role,
                    "sequence": sequence,
                    "kind": "skip",
                    "skip": {
                        "code": str(skip.get("code", "")),
                        "evidence": skip.get("evidence") if isinstance(skip.get("evidence"), dict) else {},
                    },
                    "current": state == "sealed",
                    "authoritative": False,
                }
                skipped_roles.append({"role": role, **marker["skip"], "vertexId": marker["vertexId"]})
                run_vertices.append(marker)

        bindings = manifest.get("nodes") if isinstance(manifest.get("nodes"), dict) else {}
        if not isinstance(manifest.get("nodes"), dict):
            run_current = False
            process_diagnostic(
                "invalid-process-nodes",
                "Process nodes must be an object keyed by node ID.",
                path=artifact_path,
                severity="error",
                runId=run_id,
            )
        binding_vertices: dict[str, dict] = {}
        seen_sequences = set()
        for fallback_sequence, (node_id, binding) in enumerate(bindings.items(), 1):
            if not isinstance(binding, dict) or binding.get("node_id") != node_id:
                run_current = False
                process_diagnostic(
                    "invalid-process-binding",
                    f"Process binding is malformed: {node_id}",
                    path=artifact_path,
                    severity="error",
                    runId=run_id,
                    nodeId=node_id,
                )
                continue
            sequence = binding.get("sequence")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 1
                or sequence in seen_sequences
            ):
                run_current = False
                process_diagnostic(
                    "invalid-process-sequence",
                    f"Process registration sequence is invalid or duplicated: {node_id}",
                    path=artifact_path,
                    severity="error",
                    runId=run_id,
                    nodeId=node_id,
                )
                sequence = fallback_sequence
            seen_sequences.add(sequence)
            if binding.get("status") != "committed":
                if binding.get("status") != "aborted":
                    run_current = False
                continue
            current, reason, node = _binding_is_current(root, manifest, node_id, binding, nodes_by_id)
            if not current:
                run_current = False
                process_diagnostic(
                    "stale-process-commit",
                    f"Committed process binding is stale: {node_id} ({reason}).",
                    path=artifact_path,
                    severity="error",
                    runId=run_id,
                    nodeId=node_id,
                )
            role = str(binding.get("role", "")).lower()
            vertex = {
                "vertexId": f"process:{run_id}:node:{node_id}",
                "runId": run_id,
                "graphId": node["graphId"] if node else "",
                "nodeId": node_id,
                "role": role,
                "sequence": sequence,
                "kind": "node",
                "processInputs": [str(value) for value in binding.get("process_inputs", [])] if isinstance(binding.get("process_inputs"), list) else [],
                "current": current,
                "authoritative": False,
            }
            binding_vertices[node_id] = vertex
            run_vertices.append(vertex)

        run_edges = []
        skip_vertices_by_role = {
            vertex["role"]: vertex
            for vertex in run_vertices
            if vertex["kind"] == "skip"
        }
        role_rank = {role: index for index, role in enumerate(expected_roles)}
        for target_id, target_vertex in sorted(binding_vertices.items()):
            for ordinal, source_id in enumerate(target_vertex["processInputs"], 1):
                source_vertex = binding_vertices.get(source_id)
                if source_vertex is None:
                    run_current = False
                    process_diagnostic(
                        "dangling-process-input",
                        f"Process input does not resolve in this run: {source_id}",
                        path=artifact_path,
                        severity="error",
                        runId=run_id,
                        nodeId=target_id,
                        inputNodeId=source_id,
                    )
                    continue
                source_rank = role_rank.get(source_vertex["role"], -1)
                target_rank = role_rank.get(target_vertex["role"], -1)
                skipped_vertices = [
                    skip_vertices_by_role[expected_roles[index]]
                    for index in range(source_rank + 1, target_rank)
                    if expected_roles[index] in skip_vertices_by_role
                ] if 0 <= source_rank < target_rank else []
                route = [source_vertex, *skipped_vertices, target_vertex]
                for segment, (route_source, route_target) in enumerate(zip(route, route[1:]), 1):
                    run_edges.append({
                        "edgeId": f"process:{run_id}:{target_id}:{ordinal}:{segment}",
                        "type": "process",
                        "runId": run_id,
                        "source": route_source["vertexId"],
                        "target": route_target["vertexId"],
                        "sourceNodeId": route_source.get("nodeId", ""),
                        "targetNodeId": route_target.get("nodeId", ""),
                        "declaredSourceNodeId": source_id,
                        "declaredTargetNodeId": target_id,
                        "segment": segment,
                        "segmentCount": len(route) - 1,
                        "authoritative": False,
                    })

        authoritative = state == "sealed" and run_current
        for vertex in run_vertices:
            vertex["authoritative"] = authoritative
        for edge in run_edges:
            edge["authoritative"] = authoritative
        vertices.extend(run_vertices)
        edges.extend(run_edges)
        runs.append({
            "runId": run_id,
            "goal": goal,
            "state": state,
            "schemaVersion": manifest.get("schema"),
            "artifactPath": artifact_path,
            "createdAt": str(manifest.get("created_at", "")),
            "updatedAt": str(manifest.get("updated_at", "")),
            "sealedAt": str(manifest.get("sealed_at") or ""),
            "topology": topology,
            "roles": roles,
            "skippedRoles": skipped_roles,
            "nodeVertexIds": [vertex["vertexId"] for vertex in run_vertices if vertex["kind"] == "node"],
            "authoritative": authoritative,
            "current": run_current,
        })

    authoritative_graph_ids = {
        vertex["graphId"]
        for vertex in vertices
        if vertex["kind"] == "node" and vertex["authoritative"] and vertex["graphId"]
    }
    for sequence, node in enumerate(nodes, 1):
        if node["graphId"] not in authoritative_graph_ids:
            vertices.append({
                "vertexId": f"untracked:{node['graphId']}",
                "runId": "",
                "graphId": node["graphId"],
                "nodeId": node["rawId"],
                "role": node["role"],
                "sequence": sequence,
                "kind": "untracked",
                "processInputs": [],
                "current": False,
                "authoritative": False,
                "reason": "legacy-untracked",
            })
    runs.sort(key=lambda item: (item["artifactPath"], item["runId"]))
    vertices.sort(key=lambda item: (item["runId"], item["sequence"], item["vertexId"]))
    edges.sort(key=lambda item: item["edgeId"])
    return runs, vertices, edges


def _process_id(goal: str) -> str:
    slug = CORE.goal_slug(goal)
    digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:8]
    return f"goal:{slug}:{digest}"


def inspect_graph(root) -> dict:
    """Return a tolerant, read-only graph projection for a repository root."""

    repository_root = Path(root).resolve()
    if not repository_root.is_dir():
        raise ValueError("repository root must be an existing directory")

    diagnostics: list[dict] = []
    flow_events = _load_flow_events(repository_root, diagnostics)
    events_by_key: dict[tuple, list[dict]] = defaultdict(list)
    history_by_node_id: dict[str, list[dict]] = defaultdict(list)
    for event in flow_events:
        events_by_key[event["key"]].append(event)
        history_by_node_id[event["record"]["nodeId"]].append(event)

    node_dir = repository_root / ".mycelium" / "nodes"
    node_paths = sorted(node_dir.glob("*.md")) if node_dir.is_dir() else []
    nodes = []
    for path in node_paths:
        artifact_path = _relative(repository_root, path)
        if not _artifact_is_contained(repository_root, path):
            _add_diagnostic(
                diagnostics,
                "artifact-symlink-outside-repository",
                "Node artifact symlink leaves the repository and was ignored.",
                artifact_path=artifact_path,
            )
            continue
        node = _parse_node(
            repository_root,
            path,
            events_by_key,
            history_by_node_id,
            diagnostics,
        )
        if node is not None:
            nodes.append(node)

    superseded_ids = CORE.superseded_node_ids([node["_core"] for node in nodes])
    for node in nodes:
        eligible_current = bool(CORE.eligible_current(node["_core"]))
        superseded = bool(node["rawId"] in superseded_ids)
        invalidated = bool(node["invalidated"])
        source_current = bool(node["lifecycle"]["sourceCurrent"])
        flow_committed = bool(node["lifecycle"]["flowCommitted"])
        node["lifecycle"].update(
            eligibleCurrent=eligible_current,
            superseded=superseded,
            searchVisible=bool(
                flow_committed and not invalidated and not superseded and source_current
            ),
            capEligible=bool(eligible_current and not superseded and source_current),
        )
        node["classification"] = _classification(node)

    nodes.sort(key=lambda node: node["artifactPath"])
    edges = _build_edges(nodes, diagnostics)
    edges.sort(key=lambda edge: edge["edgeId"])
    process_runs, process_vertices, process_edges = _load_process_lineage(
        repository_root,
        nodes,
        diagnostics,
    )

    goals = {
        node["goal"] for node in nodes
    } | {
        event["public"]["goal"] for event in flow_events
    }
    processes = []
    for goal in sorted(goals):
        goal_events = [event["public"] for event in flow_events if event["public"]["goal"] == goal]
        processes.append(
            {
                "processId": _process_id(goal),
                "goal": goal,
                "nodeGraphIds": [node["graphId"] for node in nodes if node["goal"] == goal],
                "flowFiles": sorted({event["artifactPath"] for event in goal_events}),
                "flowEventCount": len(goal_events),
                "flowEvents": goal_events,
            }
        )

    for node in nodes:
        node.pop("_core", None)

    diagnostics.sort(
        key=lambda item: (
            item.get("artifactPath", ""),
            item.get("line", 0),
            item["code"],
            item["message"],
        )
    )
    for index, diagnostic in enumerate(diagnostics, 1):
        diagnostic["diagnosticId"] = f"diagnostic:{index}"
    topology_diagnostics = [
        item for item in diagnostics if item.get("domain") == "process-lineage"
    ]

    classification_counts = Counter(node["classification"] for node in nodes)
    role_counts = Counter(node["role"] for node in nodes)
    status_counts = Counter(node["status"] for node in nodes)
    severity_counts = Counter(item["severity"] for item in diagnostics)
    summary = {
        "nodeArtifacts": len(node_paths),
        "nodes": len(nodes),
        "flowRecords": len(flow_events),
        "processes": len(processes),
        "processRuns": len(process_runs),
        "authoritativeProcessRuns": sum(run["authoritative"] for run in process_runs),
        "processVertices": len(process_vertices),
        "processEdges": len(process_edges),
        "edges": len(edges),
        "dependencyEdges": sum(edge["type"] == "depends_on" for edge in edges),
        "supersessionEdges": sum(edge["type"] == "supersedes" for edge in edges),
        "flowCommitted": sum(node["lifecycle"]["flowCommitted"] for node in nodes),
        "eligibleCurrent": sum(node["lifecycle"]["eligibleCurrent"] for node in nodes),
        "sourceCurrent": sum(node["lifecycle"]["sourceCurrent"] for node in nodes),
        "superseded": sum(node["lifecycle"]["superseded"] for node in nodes),
        "searchVisible": sum(node["lifecycle"]["searchVisible"] for node in nodes),
        "capEligible": sum(node["lifecycle"]["capEligible"] for node in nodes),
        "diagnostics": len(diagnostics),
        "classifications": dict(sorted(classification_counts.items())),
        "roles": dict(sorted(role_counts.items())),
        "statuses": dict(sorted(status_counts.items())),
        "diagnosticSeverities": dict(sorted(severity_counts.items())),
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "repository": repository_root.name,
        "repositoryId": hashlib.sha256(str(repository_root).encode("utf-8")).hexdigest(),
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "processes": processes,
        "processRuns": process_runs,
        "processVertices": process_vertices,
        "processEdges": process_edges,
        "topologyDiagnostics": topology_diagnostics,
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
        "summary": summary,
    }


def _loopback_authority(authority: str, port: int) -> bool:
    value = authority.strip().lower()
    if not value:
        return False
    port_text = ""
    if value.startswith("["):
        match = re.fullmatch(r"\[(::1|0:0:0:0:0:0:0:1)\](?::(\d{1,5}))?", value)
        if not match:
            return False
        port_text = match.group(2) or ""
    else:
        if value.count(":") > 1:
            return False
        host, separator, port_text = value.partition(":")
        if host not in ("127.0.0.1", "localhost"):
            return False
        if separator and not port_text:
            return False
    if port_text:
        if not port_text.isascii() or not port_text.isdecimal():
            return False
        requested_port = int(port_text)
        if requested_port < 1 or requested_port > 65535 or requested_port != port:
            return False
    return True


def _handler_for(root: Path, static_root: Path | None = None):
    if static_root is None:
        static_root = Path(__file__).resolve().parent.parent / "web" / "mycelium-atlas"
    else:
        static_root = Path(static_root).resolve()

    class AtlasHandler(BaseHTTPRequestHandler):
        server_version = "MyceliumAtlas/1"
        sys_version = ""

        def log_message(self, _format, *_args):
            return

        def _request_is_loopback(self) -> bool:
            host_headers = self.headers.get_all("Host") or []
            port = self.server.server_address[1]
            if len(host_headers) != 1 or not _loopback_authority(host_headers[0], port):
                return False
            request_target = urlsplit(self.path)
            return not request_target.netloc or _loopback_authority(request_target.netloc, port)

        def _respond(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            head_only: bool,
            cache_control: str = "no-cache",
            allow: str = "",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            if allow:
                self.send_header("Allow", allow)
            self.end_headers()
            if not head_only and body:
                self.wfile.write(body)

        def _serve(self, *, head_only: bool) -> None:
            if not self._request_is_loopback():
                self._respond(
                    403,
                    b"Forbidden\n",
                    "text/plain; charset=utf-8",
                    head_only=head_only,
                    cache_control="no-store",
                )
                return
            request_path = unquote(urlsplit(self.path).path)
            if request_path == "/api/v1/graph":
                try:
                    body = json.dumps(
                        inspect_graph(root),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._respond(
                        200,
                        body,
                        "application/json; charset=utf-8",
                        head_only=head_only,
                        cache_control="no-store",
                    )
                except (OSError, ValueError) as error:
                    body = json.dumps(
                        {"error": "graph inspection failed", "detail": _safe_error(error, root)},
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._respond(
                        500,
                        body,
                        "application/json; charset=utf-8",
                        head_only=head_only,
                        cache_control="no-store",
                    )
                return

            static = STATIC_FILES.get(request_path)
            if static is None:
                self._respond(
                    404,
                    b"Not Found\n",
                    "text/plain; charset=utf-8",
                    head_only=head_only,
                    cache_control="no-store",
                )
                return
            filename, content_type = static
            path = static_root / filename
            try:
                path.resolve().relative_to(static_root.resolve())
                body = path.read_bytes()
            except (OSError, ValueError):
                self._respond(
                    404,
                    b"Not Found\n",
                    "text/plain; charset=utf-8",
                    head_only=head_only,
                    cache_control="no-store",
                )
                return
            self._respond(200, body, content_type, head_only=head_only)

        def do_GET(self):
            self._serve(head_only=False)

        def do_HEAD(self):
            self._serve(head_only=True)

        def _method_not_allowed(self):
            if not self._request_is_loopback():
                self._respond(
                    403,
                    b"Forbidden\n",
                    "text/plain; charset=utf-8",
                    head_only=False,
                    cache_control="no-store",
                )
                return
            self._respond(
                405,
                b"Method Not Allowed\n",
                "text/plain; charset=utf-8",
                head_only=False,
                cache_control="no-store",
                allow="GET, HEAD",
            )

        do_POST = _method_not_allowed
        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed

    return AtlasHandler


def create_server(
    root,
    host: str = "127.0.0.1",
    port: int = 0,
    static_root=None,
) -> ThreadingHTTPServer:
    repository_root = Path(root).resolve()
    if not repository_root.is_dir():
        raise ValueError("repository root must be an existing directory")
    if host == "localhost":
        host = "127.0.0.1"
    if host != "127.0.0.1":
        raise ValueError("Mycelium Atlas may bind only to the loopback address")
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    server = ThreadingHTTPServer(
        (host, port),
        _handler_for(repository_root, static_root=static_root),
    )
    server.daemon_threads = True
    return server


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Inspect Mycelium artifacts in a local Atlas")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    result.add_argument("--port", type=int, default=8765, help="loopback port (default: 8765)")
    result.add_argument("--no-open", action="store_true", help="do not open the browser")
    result.add_argument("--json", action="store_true", help="print the graph payload and exit")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.json:
            print(json.dumps(inspect_graph(args.root), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        server = create_server(args.root, port=args.port)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"Mycelium Atlas: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
