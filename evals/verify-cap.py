#!/usr/bin/env python3
"""External CAP evidence verifier. Keep this outside the agent treatment."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# Re-execution sandbox. There is no network jail available here, so the
# guarantee is: no shell, a fixed working directory, a scrubbed environment, a
# hard timeout, and a closed executable allowlist. Anything outside that is a
# verification failure, never a skip.
COMMAND_ALLOWLIST = frozenset({"python", "python3", "pwsh", "git", "node", "bash", "sh"})
COMMAND_TIMEOUT_SECONDS = 60
COMMAND_ENVIRONMENT_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "HOME")
EXPECT_EXIT_PREFIX = re.compile(r"^exit:(\d{1,3});(.*)$", re.DOTALL)
EXPECT_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
ABSENT_FIELD_VALUES = frozenset({"", "none"})

CAP_ROW = re.compile(
    r"^- \[(?P<node_id>[^\]]+)\]\s+(?P<fact>.+?)\s+"
    r"\((?P<path>\.mycelium/nodes/[^:()]+\.md):(?P<line>\d+)\)\s*$"
)
FLOW_FIELDS = (
    ("nodeId", "nodeid"),
    ("goal", "goal"),
    ("timestamp", "updated"),
    ("role", "role"),
    ("status", "status"),
    ("topics", "topics"),
    ("evidence", "evidence"),
    ("confidence", "confidence"),
    ("consumes", "consumes"),
    ("blocks", "blocks"),
    ("trace", "trace"),
    ("reflection", "reflection"),
    ("parentGoalId", "parent_goal_id"),
    ("producingAgent", "producing_agent"),
    ("version", "version"),
    ("sourceRefs", "source_refs"),
    ("dependsOn", "depends_on"),
    ("invalidated", "invalidated"),
    ("supersedes", "supersedes"),
)


def fail(reason):
    print("Verification: FAIL")
    print(f"Reason: {reason}")
    return 1


def inside(workspace, relative_path):
    candidate = (workspace / relative_path).resolve()
    candidate.relative_to(workspace)
    return candidate


def metadata(text):
    result = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("node is missing frontmatter")
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        if not line.strip():
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):\s*(.*)", line)
        if not match:
            raise ValueError(f"invalid frontmatter line: {line}")
        key = match.group(1).lower()
        if key in result:
            raise ValueError(f"duplicate metadata key: {key}")
        result[key] = match.group(2)
    if not closed:
        raise ValueError("node frontmatter is not closed")
    return result


def normalized_fact(text):
    value = " ".join(text.split()).strip()
    return re.sub(r"^[-*]\s+", "", value)


def canonical_lines(values, name, *, strip_bullets=False, allow_null=False):
    if values is None and allow_null:
        values = []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValueError(f"flow {name} must be a JSON string or string array")
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"flow {name} must contain only strings")
    return tuple(
        normalized_fact(row) if strip_bullets else " ".join(row.split())
        for value in values
        for row in value.splitlines()
        if row.strip()
    )


def canonical_facts(values):
    return canonical_lines(values, "facts", strip_bullets=True)


def canonical_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("flow timestamp must be a JSON string")
    text = value
    match = re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?P<fraction>\d{1,7}))?"
        r"(?:Z|[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00))",
        text,
    )
    if not match:
        raise ValueError(f"invalid flow timestamp: {text}")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        normalized = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"invalid flow timestamp: {text}") from error
    seventh = (match.group("fraction") or "").ljust(7, "0")[6]
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%f") + seventh + "Z"


def flow_key(values, facts, questions, next_steps, *, node=False):
    if not node:
        for name in ("facts", "questions", "next"):
            if name not in values:
                raise ValueError(f"missing flow field: {name}")
    key = []
    for event_name, node_name in FLOW_FIELDS:
        name = node_name if node else event_name
        if name not in values:
            raise ValueError(f"missing flow field: {name}")
        value = values[name]
        if node:
            if not isinstance(value, str):
                raise ValueError(f"node {name} must be a string")
            if event_name == "invalidated" and value not in ("true", "false"):
                raise ValueError("node invalidated must be true or false")
            text = value
        elif event_name == "invalidated":
            if not isinstance(value, bool):
                raise ValueError("flow invalidated must be a JSON boolean")
            text = str(value).lower()
        else:
            if not isinstance(value, str):
                raise ValueError(f"flow {event_name} must be a JSON string")
            text = value
        key.append(canonical_timestamp(text) if event_name == "timestamp" else text)
    key.append(canonical_facts(facts))
    key.append(canonical_lines(questions, "questions", allow_null=True))
    key.append(canonical_lines(next_steps, "next", allow_null=True))
    return tuple(key)


def node_fact_rows(text):
    rows = []
    in_facts = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.strip() == "## Facts":
            in_facts = True
            continue
        if in_facts and line.startswith("## "):
            break
        if in_facts and line.strip():
            rows.append((line_number, normalized_fact(line)))
    return rows


def section(text, name):
    match = re.search(
        rf"(?ms)^## {re.escape(name)}[ \t]*\r?\n(.*?)(?=^## |\Z)", text
    )
    return match.group(1).strip() if match else ""


def body_fields(text):
    header = re.split(r"(?m)^## Facts\s*$", text, maxsplit=1)[0]
    result = {}
    for name in ("Evidence", "Trace", "Reflection"):
        matches = re.findall(rf"(?m)^{name}:[ \t]*([^\r\n]*)\r?$", header)
        if len(matches) != 1:
            raise ValueError(f"node must contain exactly one {name} field")
        result[name.lower()] = matches[0]
    return result


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def has_flow_commit(workspace, projection, facts, questions, next_steps):
    expected = flow_key(projection, facts, questions, next_steps, node=True)
    flow_dir = workspace / ".mycelium" / "flows"
    if not flow_dir.is_dir():
        return False
    matched = False
    for path in sorted(flow_dir.glob("*.jsonl")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line, object_pairs_hook=_json_object)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid flow record {path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError("flow record must be a JSON object")
            if flow_key(
                record,
                record.get("facts"),
                record.get("questions"),
                record.get("next"),
            ) == expected:
                matched = True
    return matched


def field_is_absent(value):
    return str(value).strip().lower() in ABSENT_FIELD_VALUES


def command_environment():
    return {
        name: os.environ[name]
        for name in COMMAND_ENVIRONMENT_KEYS
        if name in os.environ
    }


def executable_stem(argument):
    name = Path(argument).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def parse_expectation(expect):
    """Return (expected_exit, kind, value) or raise ValueError."""
    text = expect.strip()
    expected_exit = 0
    prefix = EXPECT_EXIT_PREFIX.match(text)
    if prefix:
        expected_exit = int(prefix.group(1))
        text = prefix.group(2).strip()
    if not text:
        if not prefix:
            raise ValueError("node expect field is empty")
        return expected_exit, "exit", ""
    digest = EXPECT_SHA256.match(text)
    if digest:
        return expected_exit, "sha256", digest.group(1)
    if text.lower().startswith("sha256:"):
        raise ValueError("node expect sha256 must be 64 lowercase hex characters")
    return expected_exit, "substring", text


def normalized_stdout(data):
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def reexecute_node_command(workspace, command, expect):
    """Re-run a node command. Return None on success or a failure reason."""
    if field_is_absent(expect):
        raise ValueError("node command requires a non-empty expect field")
    expected_exit, kind, expected = parse_expectation(expect)
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise ValueError(f"node command cannot be parsed: {error}") from error
    if not argv:
        raise ValueError("node command is empty after parsing")
    if executable_stem(argv[0]) not in COMMAND_ALLOWLIST:
        raise ValueError(f"node command executable is not allowed: {argv[0]}")
    try:
        completed = subprocess.run(
            argv,
            cwd=str(workspace),
            env=command_environment(),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"node command timed out after {COMMAND_TIMEOUT_SECONDS}s"
    except OSError as error:
        return f"node command could not be executed: {error}"
    if completed.returncode != expected_exit:
        return (
            f"node command exit code is {completed.returncode}, "
            f"expected {expected_exit}"
        )
    stdout = normalized_stdout(completed.stdout)
    if kind == "sha256":
        actual = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        if actual != expected:
            return f"node command stdout sha256 is {actual}, expected {expected}"
    elif kind == "substring" and expected not in stdout:
        return "node command stdout does not contain the expected text"
    return None


def load_lineage_module():
    verifier_path = Path(__file__).resolve()
    module_path = verifier_path.with_name("mycelium_lineage.py")
    if not module_path.is_file():
        repo_module = verifier_path.parent.parent / "bin" / "mycelium_lineage.py"
        if verifier_path.parent.name != "evals" or not repo_module.is_file():
            raise ValueError(
                "tracked CAP verification requires an evaluator-owned "
                "mycelium_lineage.py"
            )
        module_path = repo_module
    spec = importlib.util.spec_from_file_location("mycelium_lineage_cap_verifier", module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load evaluator-owned lineage verifier: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_process_run(workspace, run_id, selected_nodes):
    lineage = load_lineage_module()
    snapshot = lineage.verify_snapshot(run_id, root=workspace)
    report = snapshot["report"]
    if not report.get("valid") or report.get("state") != "sealed":
        messages = [
            item.get("message", str(item)) if isinstance(item, dict) else str(item)
            for item in report.get("diagnostics", [])
        ]
        detail = "; ".join(messages) or f"state is {report.get('manifest_state', report.get('state'))}"
        return f"process run {run_id} is not sealed and current: {detail}"

    manifest = snapshot["manifest"]
    registered = {
        node_id: node
        for node_id, node in manifest.get("nodes", {}).items()
        if node.get("status") == "committed"
    }
    untracked = sorted({node_id for node_id, _ in selected_nodes} - set(registered))
    if untracked:
        return (
            f"selected node is not registered in process run {run_id}: "
            + ", ".join(untracked)
        )
    for node_id, selected_path in selected_nodes:
        committed_path = registered[node_id].get("commit", {}).get("node", {}).get("path")
        if selected_path != committed_path:
            return (
                f"selected node path does not match process commit path for {node_id}: "
                f"{selected_path} != {committed_path}"
            )
    return None


def verify(workspace, cap_file, run_id=None):
    workspace = workspace.resolve()
    cap_path = inside(workspace, cap_file)
    if not cap_path.is_file():
        return fail(f"missing CAP output: {cap_file}")

    cap_text = cap_path.read_text(encoding="utf-8", errors="replace")
    states = re.findall(r"(?m)^Verification:\s*(\S+)\s*$", cap_text)
    if states != ["EXTERNAL_REQUIRED"]:
        return fail(f"CAP verification states are invalid: {states}")

    result_lines = [line for line in cap_text.splitlines() if line.startswith("- ")]
    if not result_lines:
        return fail("CAP output contains no selected facts")

    selected_nodes = []
    parsed_rows = []
    for row in result_lines:
        match = CAP_ROW.fullmatch(row)
        if not match:
            return fail(f"unverifiable CAP row: {row}")
        selected_nodes.append((match.group("node_id"), match.group("path")))
        parsed_rows.append((row, match))

    if run_id:
        process_error = verify_process_run(workspace, run_id, selected_nodes)
        if process_error:
            return fail(process_error)

    reexecuted = False
    for row, match in parsed_rows:
        node_path = inside(workspace, match.group("path"))
        if not node_path.is_file():
            return fail(f"missing selected node: {match.group('path')}")
        node_text = node_path.read_text(encoding="utf-8", errors="replace")
        node_lines = node_text.splitlines()
        fact_rows = node_fact_rows(node_text)
        node_line = int(match.group("line"))
        if node_line < 1 or node_line > len(node_lines):
            return fail(f"selected node line is out of range: {match.group('path')}:{node_line}")
        fact = normalized_fact(match.group("fact"))
        if (node_line, fact) not in fact_rows or normalized_fact(node_lines[node_line - 1]) != fact:
            return fail(f"CAP fact does not match selected node line: {match.group('node_id')}")

        meta = metadata(node_text)
        if meta.get("version") == "2" and not run_id:
            return fail("schema-v2 selected nodes require --run-id")
        projection = {**meta, **body_fields(node_text)}
        if meta.get("nodeid") != match.group("node_id"):
            return fail(f"selected node ID mismatch: {match.group('path')}")
        if meta.get("status") != "complete" or meta.get("invalidated", "false").lower() == "true":
            return fail(f"selected node is not eligible: {match.group('node_id')}")
        if not has_flow_commit(
            workspace,
            projection,
            [value for _, value in fact_rows],
            section(node_text, "Questions"),
            section(node_text, "Next"),
        ):
            return fail(f"selected node has no matching flow commit: {match.group('node_id')}")

        refs = meta.get("source_refs", "")
        if not refs or refs.lower() == "none":
            return fail(f"selected node has no source_refs: {match.group('node_id')}")
        supported = False
        for ref in re.split(r"\s*[;,]\s*", refs):
            source = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", ref)
            if not source:
                return fail(f"invalid source_ref: {ref}")
            source_path = inside(workspace, source.group(1))
            if not source_path.is_file():
                return fail(f"missing source_ref file: {source.group(1)}")
            source_lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
            source_start = int(source.group(2))
            source_end = int(source.group(3) or source.group(2))
            if source_start < 1 or source_end < source_start or source_end > len(source_lines):
                return fail(f"source_ref line is out of range: {ref}")
            source_range = source_lines[source_start - 1:source_end]
            candidates = [normalized_fact(line) for line in source_range if line.strip()]
            candidates.append(normalized_fact(" ".join(source_range)))
            supported = supported or fact in candidates
        if not supported:
            return fail(f"source_refs do not support CAP fact: {match.group('node_id')}")

        # Re-execution is a tracked-run guarantee only. Untracked CAP files keep
        # the historical citation-only behavior.
        command = meta.get("command", "")
        if run_id and not field_is_absent(command):
            try:
                command_error = reexecute_node_command(
                    workspace, command, meta.get("expect", "")
                )
            except ValueError as error:
                return fail(f"{error}: {match.group('node_id')}")
            if command_error:
                return fail(f"{command_error}: {match.group('node_id')}")
            reexecuted = True

    print("Verification: PASS")
    print(f"Verification-Mode: {'re-executed' if reexecuted else 'citation-only'}")
    print(f"Verified nodes: {len(result_lines)}")
    if run_id:
        print(f"Verified run: {run_id}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("cap_file", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        return verify(args.workspace, args.cap_file, args.run_id)
    except (OSError, ValueError) as error:
        return fail(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
