#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROLES = ("apex", "septum", "hyphae", "stem", "cap")
STATUSES = ("alive", "blocked", "complete")
CONFIDENCES = ("", "low", "medium", "high")
INDEX_VERSION = 2
SEARCH_FIELDS = (
    "nodeId",
    "topics",
    "facts",
    "evidence",
    "source_refs",
    "depends_on",
    "supersedes",
    "reflection",
)
POSIX_ARTIFACT_MODE = 0o644
POSIX_DIRECTORY_MODE = 0o755


def ensure_artifact_directory(path):
    try:
        path.mkdir()
    except FileExistsError:
        if not path.is_dir():
            raise
        return

    if os.name == "nt":
        return

    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or not os.path.samestat(opened, current):
            raise ValueError(f"artifact directory changed while creating it: {path}")
        os.fchmod(descriptor, POSIX_DIRECTORY_MODE)
    finally:
        os.close(descriptor)


def ensure_artifact_directories(*directories):
    paths = (Path(".mycelium"), *directories)
    for path in paths:
        ensure_artifact_directory(path)


def terms(text):
    return text.lower().split()


def relation_ids(value):
    return [
        part
        for part in re.split(r"\s*[,;]\s*", str(value))
        if part and part.lower() != "none"
    ]


def safe_id(value, fallback=""):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return cleaned or fallback


def goal_slug(goal):
    return re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-") or "goal"


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not match:
            raise ValueError(f"invalid frontmatter line: {line}")
        key = match.group(1).lower()
        if key in result:
            raise ValueError(f"duplicate metadata key: {key}")
        result[key] = match.group(2)
    if not closed:
        raise ValueError("node frontmatter is not closed")
    return result


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


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
        re.sub(r"^[-*]\s+", "", " ".join(line.split()))
        if strip_bullets
        else " ".join(line.split())
        for value in values
        for line in value.splitlines()
        if line.strip()
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


def flow_commits():
    commits = set()
    flow_dir = Path(".mycelium/flows")
    if not flow_dir.is_dir():
        return commits
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
            commits.add(
                flow_key(
                    record,
                    record.get("facts"),
                    record.get("questions"),
                    record.get("next"),
                )
            )
    return commits


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


def stdin_lines():
    return [] if sys.stdin.isatty() else [line.rstrip("\r\n") for line in sys.stdin]


def write_lines(lines):
    for line in lines:
        print(line)


def command_apex(args):
    goal_terms = terms(args.goal)
    files = []
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            files = sorted({Path(line) for line in result.stdout.splitlines() if line})
    except FileNotFoundError:
        pass

    if not files:
        files = [
            path
            for path in Path(".").rglob("*")
            if path.is_file() and ".git" not in path.parts
        ]

    for path in files:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                for line_number, line in enumerate(handle, 1):
                    lower = line.lower()
                    if sum(term in lower for term in goal_terms):
                        print(f"apex\t{path.as_posix()}\t{line_number}\t{line.strip()}")
        except OSError:
            continue
    return 0


def command_septum(args):
    goal_terms = terms(args.goal)
    required = 2 if len(goal_terms) > 1 else 1
    for record in stdin_lines():
        parts = record.split("\t", 3)
        if len(parts) < 4:
            continue
        if sum(term in parts[3].lower() for term in goal_terms) >= required:
            print(f"septum\t{parts[1]}\t{parts[2]}\t{parts[3]}")
    return 0


def command_hyphae(_args):
    for record in stdin_lines():
        parts = record.split("\t", 3)
        if len(parts) >= 4:
            print(f"hyphae\t{parts[1]}\t{parts[2]}\t{' '.join(parts[3].split())}")
    return 0


def node_flow_projection(
    goal,
    node_id,
    role,
    status,
    facts="",
    questions="",
    next_step="",
    *,
    topics="",
    evidence="",
    confidence="",
    consumes="",
    blocks="",
    trace="",
    reflection="",
    parent_goal_id="",
    producing_agent="",
    version="1",
    source_refs="",
    depends_on="",
    invalidated=False,
    supersedes="",
    command="",
    expect="",
    untracked_reason="",
):
    return {
        "goal": goal,
        "nodeId": node_id,
        "role": role,
        "status": status,
        "topics": topics,
        "evidence": evidence,
        "confidence": confidence,
        "consumes": consumes,
        "blocks": blocks,
        "trace": trace,
        "reflection": reflection,
        "parentGoalId": parent_goal_id,
        "producingAgent": producing_agent,
        "version": version,
        "sourceRefs": source_refs,
        "dependsOn": depends_on,
        "invalidated": invalidated,
        "supersedes": supersedes,
        "command": command,
        "expect": expect,
        "untracked_reason": untracked_reason,
        "facts": [facts] if facts else [],
        "questions": [questions] if questions else [],
        "next": [next_step] if next_step else [],
    }


def write_node(
    goal,
    node_id,
    role,
    status,
    facts="",
    questions="",
    next_step="",
    *,
    topics="",
    evidence="",
    confidence="",
    consumes="",
    blocks="",
    trace="",
    reflection="",
    parent_goal_id="",
    producing_agent="",
    version="1",
    source_refs="",
    depends_on="",
    invalidated=False,
    supersedes="",
    command="",
    expect="",
    untracked_reason="",
    return_commit=False,
):
    node_id = safe_id(node_id)
    if not node_id:
        raise ValueError("NodeId must contain at least one safe character")

    if status == "complete":
        required = {
            "Facts": facts,
            "Topics": topics,
            "Evidence": evidence,
            "Confidence": confidence,
            "Consumes": consumes,
            "Questions": questions,
            "Blocks": blocks,
            "Next": next_step,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(
                f"Complete node missing required fields: {', '.join(missing)}. "
                "Use 'none' when a field is intentionally empty."
            )

    # A Trace that claims a spawn must cost the same fields a real one carries.
    # This still cannot prove an agent ran; it stops a spawn claim that carries
    # nothing.
    trace_text = str(trace)
    if trace_text.strip() and re.search(r"collaboration\.spawn_agent|\bAgent\b", trace_text):
        segments = ("Action:", "Observation:", "Result:")
        if not str(producing_agent).strip() or not all(part in trace_text for part in segments):
            raise ValueError(
                "Trace names a spawn tool; "
                "ProducingAgent and Action/Observation/Result are required."
            )

    frontmatter = {
        "Goal": goal,
        "Topics": topics,
        "Consumes": consumes,
        "Blocks": blocks,
        "ParentGoalId": parent_goal_id,
        "ProducingAgent": producing_agent,
        "Version": version,
        "SourceRefs": source_refs,
        "DependsOn": depends_on,
        "Supersedes": supersedes,
        "Command": command,
        "Expect": expect,
        "UntrackedReason": untracked_reason,
    }
    single_line = {
        **frontmatter,
        "Evidence": evidence,
        "Trace": trace,
        "Reflection": reflection,
    }
    multiline = [name for name, value in single_line.items() if re.search(r"[\r\n]", str(value))]
    if multiline:
        raise ValueError(f"Frontmatter fields must be a single line: {', '.join(multiline)}")

    flow_dir = Path(".mycelium/flows")
    node_dir = Path(".mycelium/nodes")
    ensure_artifact_directories(flow_dir, node_dir)

    created = timestamp()
    record = {
        "timestamp": created,
        **node_flow_projection(
            goal,
            node_id,
            role,
            status,
            facts,
            questions,
            next_step,
            topics=topics,
            evidence=evidence,
            confidence=confidence,
            consumes=consumes,
            blocks=blocks,
            trace=trace,
            reflection=reflection,
            parent_goal_id=parent_goal_id,
            producing_agent=producing_agent,
            version=version,
            source_refs=source_refs,
            depends_on=depends_on,
            invalidated=invalidated,
            supersedes=supersedes,
            command=command,
            expect=expect,
            untracked_reason=untracked_reason,
        ),
    }

    invalidated_text = str(invalidated).lower()
    node_text = f"""---
nodeId: {node_id}
goal: {goal}
role: {role}
status: {status}
topics: {topics}
confidence: {confidence}
consumes: {consumes}
blocks: {blocks}
parent_goal_id: {parent_goal_id}
producing_agent: {producing_agent}
version: {version}
source_refs: {source_refs}
depends_on: {depends_on}
invalidated: {invalidated_text}
supersedes: {supersedes}
command: {command}
expect: {expect}
untracked_reason: {untracked_reason}
updated: {created}
---

# {node_id}

Goal: {goal}
Role: {role}
Status: {status}
Topics: {topics}
Evidence: {evidence}
Confidence: {confidence}
Consumes: {consumes}
Blocks: {blocks}
Trace: {trace}
Reflection: {reflection}
Parent Goal ID: {parent_goal_id}
Producing Agent: {producing_agent}
Version: {version}
Source Refs: {source_refs}
Depends On: {depends_on}
Invalidated: {invalidated_text}
Supersedes: {supersedes}
Command: {command}
Expect: {expect}
Untracked Reason: {untracked_reason}

## Facts
{facts}

## Questions
{questions}

## Next
{next_step}
"""
    node_path = node_dir / f"{node_id}.md"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=node_dir, prefix=f".{node_id}.", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(node_text)
            handle.flush()
            os.fsync(handle.fileno())
            if os.name != "nt":
                os.fchmod(handle.fileno(), POSIX_ARTIFACT_MODE)
        # The flow record and node metadata share a timestamp. Readers publish only matching pairs.
        flow_path = flow_dir / f"{goal_slug(goal)}.jsonl"
        with flow_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            if os.name != "nt":
                os.fchmod(handle.fileno(), POSIX_ARTIFACT_MODE)
        os.replace(temporary_path, node_path)
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)

    if not return_commit:
        return flow_path.as_posix()

    flow_line = None
    for line_number, line in enumerate(flow_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() and json.loads(line) == record:
            flow_line = line_number
    if flow_line is None:
        raise ValueError(f"written flow commit cannot be found: {flow_path.as_posix()}")
    return {
        "flow_path": flow_path,
        "flow_line": flow_line,
        "node_path": node_path,
    }


def load_lineage_module():
    module_path = Path(__file__).with_name("mycelium_lineage.py")
    if not module_path.is_file():
        raise ValueError(
            "tracked node writes require bin/mycelium_lineage.py; no node or flow was written"
        )
    spec = importlib.util.spec_from_file_location("mycelium_lineage_runtime", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRACKED_BY_DEFAULT_MESSAGE = (
    'node writes are tracked by default; pass --run-id <id> or --allow-untracked "<reason>"'
)


def command_node(args):
    # Tracked is the default. The untracked path still exists, but it has to be
    # asked for by name and the stated reason lands in the node itself, so a
    # bypass is greppable instead of silent.
    untracked_reason = str(args.allow_untracked or "")
    if not args.run_id and not untracked_reason.strip():
        print(TRACKED_BY_DEFAULT_MESSAGE, file=sys.stderr)
        return 2
    if args.run_id and untracked_reason.strip():
        print(
            "--run-id and --allow-untracked are mutually exclusive; a tracked write needs no reason",
            file=sys.stderr,
        )
        return 2

    effective_version = args.version or ("2" if args.run_id else "1")
    lineage = None
    reservation = None
    if args.run_id:
        safe_node_id = safe_id(args.node_id)
        if not safe_node_id:
            raise ValueError("NodeId must contain at least one safe character")
        lineage = load_lineage_module()
        lineage.preflight(root=Path.cwd())
        projection = node_flow_projection(
            args.goal,
            safe_node_id,
            args.role,
            args.status,
            args.facts,
            args.questions,
            args.next_step,
            topics=args.topics,
            evidence=args.evidence,
            confidence=args.confidence,
            consumes=args.consumes,
            blocks=args.blocks,
            trace=args.trace,
            reflection=args.reflection,
            parent_goal_id=args.parent_goal_id,
            producing_agent=args.producing_agent,
            version=effective_version,
            source_refs=args.source_refs,
            depends_on=args.depends_on,
            invalidated=args.invalidated,
            supersedes=args.supersedes,
            command=args.node_command,
            expect=args.expect,
            untracked_reason=untracked_reason,
        )
        reservation = lineage.reserve(
            args.run_id,
            safe_node_id,
            args.role,
            args.process_input,
            projection,
            root=Path.cwd(),
        )
        if reservation["status"] == "committed":
            print(reservation["commit"]["flow"]["path"])
            return 0

    written = write_node(
            args.goal,
            args.node_id,
            args.role,
            args.status,
            args.facts,
            args.questions,
            args.next_step,
            topics=args.topics,
            evidence=args.evidence,
            confidence=args.confidence,
            consumes=args.consumes,
            blocks=args.blocks,
            trace=args.trace,
            reflection=args.reflection,
            parent_goal_id=args.parent_goal_id,
            producing_agent=args.producing_agent,
            version=effective_version,
            source_refs=args.source_refs,
            depends_on=args.depends_on,
            invalidated=args.invalidated,
            supersedes=args.supersedes,
            command=args.node_command,
            expect=args.expect,
            untracked_reason=untracked_reason,
            return_commit=bool(args.run_id),
        )
    if args.run_id:
        lineage.finalize_paths(
            args.run_id,
            reservation["reservation_token"],
            written["node_path"],
            written["flow_path"],
            written["flow_line"],
            root=Path.cwd(),
        )
        output_path = written["flow_path"].as_posix()
    else:
        output_path = written
    # Only `reflect` used to regenerate the digest, so a node that retired a
    # reflection with `--supersedes` left the digest advertising a failure mode
    # that no longer applies until the next reflection was written. Regenerate
    # it here too. This writes no stdout, so `node` output stays byte-identical.
    if supersedes_a_reflection(args.supersedes):
        write_reflections_digest()
    print(output_path)
    return 0


def load_nodes(paths=None, skipped=None):
    nodes = []
    node_dir = Path(".mycelium/nodes")
    if not node_dir.is_dir():
        return nodes
    commits = flow_commits()
    node_paths = sorted(node_dir.glob("*.md")) if paths is None else sorted(set(map(Path, paths)))
    node_root = node_dir.resolve()
    for path in node_paths:
        if not path.is_file() or path.resolve().parent != node_root:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        try:
            meta = metadata(text)
        except ValueError as error:
            if skipped is not None:
                skipped.append({"path": path.as_posix(), "reason": str(error)})
            continue
        facts = section(text, "Facts")
        questions = section(text, "Questions")
        next_steps = section(text, "Next")
        normalized_updated = ""
        body = {}
        try:
            body = body_fields(text)
            projection = {**meta, **body}
            node_key = flow_key(
                projection, facts, questions, next_steps, node=True
            )
            normalized_updated = node_key[2]
            flow_committed = node_key in commits
        except ValueError:
            flow_committed = False
        fact_rows = []
        in_facts = False
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.strip() == "## Facts":
                in_facts = True
                continue
            if in_facts and line.startswith("## "):
                break
            if in_facts and line.strip():
                fact_rows.append({"line": line_number, "text": line})
        nodes.append(
            {
                "nodeId": meta.get("nodeid", path.stem),
                "role": meta.get("role", ""),
                "status": meta.get("status", ""),
                "topics": meta.get("topics", ""),
                "confidence": meta.get("confidence", ""),
                "updated": normalized_updated,
                "consumes": meta.get("consumes", ""),
                "blocks": meta.get("blocks", ""),
                "parent_goal_id": meta.get("parent_goal_id", ""),
                "producing_agent": meta.get("producing_agent", ""),
                "version": meta.get("version", ""),
                "source_refs": meta.get("source_refs", ""),
                "depends_on": meta.get("depends_on", ""),
                "invalidated": meta.get("invalidated", ""),
                "supersedes": meta.get("supersedes", ""),
                "untracked_reason": meta.get("untracked_reason", ""),
                "evidence": body.get("evidence", ""),
                "reflection": body.get("reflection", ""),
                "facts": facts,
                "firstFact": next((line for line in facts.splitlines() if line.strip()), ""),
                "factRows": fact_rows,
                "flowCommitted": flow_committed,
                "path": path,
            }
        )
    return nodes


def eligible_current(node):
    source_refs = node.get("source_refs", "").strip()
    return (
        str(node.get("invalidated", "")).lower() != "true"
        and node.get("flowCommitted")
        and node.get("status") == "complete"
        and source_refs
        and source_refs.lower() != "none"
    )


def superseded_node_ids(nodes):
    return {
        target
        for node in nodes
        if eligible_current(node)
        for target in relation_ids(node.get("supersedes", ""))
        if target != node.get("nodeId")
    }


def source_refs_current(node):
    root = Path.cwd().resolve()
    references = relation_ids(node.get("source_refs", ""))
    if not references:
        return False
    source_candidates = set()
    for reference in references:
        match = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", reference)
        if not match:
            return False
        path = (root / match.group(1)).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return False
        if not path.is_file():
            return False
        source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = int(match.group(2))
        end = int(match.group(3) or match.group(2))
        if start < 1 or end < start or end > len(source_lines):
            return False
        source_range = source_lines[start - 1:end]
        source_candidates.update(canonical_facts(source_range))
        source_candidates.update(canonical_facts([" ".join(source_range)]))
    return all(
        canonical_facts([fact["text"]])[0] in source_candidates
        for fact in node["factRows"]
    )


def build_index(nodes=None):
    Path(".mycelium").mkdir(exist_ok=True)
    source_nodes = load_nodes() if nodes is None else nodes
    published = [node for node in source_nodes if node["flowCommitted"]]
    known_ids = {node["nodeId"] for node in published}
    depended_on_by = {node_id: [] for node_id in known_ids}
    superseded_by = {node_id: [] for node_id in known_ids}
    for node in published:
        for target in relation_ids(node["depends_on"]):
            if target in known_ids and node["nodeId"] not in depended_on_by[target]:
                depended_on_by[target].append(node["nodeId"])
        for target in relation_ids(node["supersedes"]):
            if target in known_ids and node["nodeId"] not in superseded_by[target]:
                superseded_by[target].append(node["nodeId"])

    items = []
    for node in published:
        item = {
            key: value
            for key, value in node.items()
            if key not in ("factRows", "evidence", "path")
        }
        item.update(
            {
                "node_path": node["path"].as_posix(),
                "depended_on_by": depended_on_by[node["nodeId"]],
                "superseded_by": superseded_by[node["nodeId"]],
                "indexVersion": INDEX_VERSION,
            }
        )
        items.append(item)
    index_path = Path(".mycelium/index.json")
    index_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index_path


def command_lint(_args):
    violations = []
    node_dir = Path(".mycelium/nodes")
    if node_dir.is_dir():
        for path in sorted(node_dir.glob("*.md")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            try:
                meta = metadata(text)
            except ValueError as error:
                violations.append(f"{path.as_posix()}: {error}")
                continue
            missing = [
                display
                for key, display in (("nodeid", "nodeId"), ("role", "role"), ("status", "status"))
                if key not in meta
            ]
            if missing:
                violations.append(
                    f"{path.as_posix()}: missing required field(s): {', '.join(missing)}"
                )

    flow_dir = Path(".mycelium/flows")
    if flow_dir.is_dir():
        for path in sorted(flow_dir.glob("*.jsonl")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    violations.append(f"{path.as_posix()}:{line_number}: {error}")
                    continue
                if not isinstance(record, dict):
                    violations.append(
                        f"{path.as_posix()}:{line_number}: flow record must be a JSON object"
                    )
                    continue
                missing = [
                    field
                    for field in ("timestamp", "nodeId", "role", "status")
                    if field not in record
                ]
                if missing:
                    violations.append(
                        f"{path.as_posix()}:{line_number}: missing required field(s): {', '.join(missing)}"
                    )

    for violation in violations:
        print(violation)
    return 1 if violations else 0


def command_index(_args):
    skipped = []
    nodes = load_nodes(skipped=skipped)
    index_path = build_index(nodes)
    if skipped:
        print(
            f"{len(nodes)} nodes loaded, {len(skipped)} skipped (run mycelium-lint for detail)",
            file=sys.stderr,
        )
    print(index_path.as_posix())
    return 0


def calibration_penalties():
    """Read `.mycelium/calibration.json` into {confidence bucket: rate}.

    A missing, unreadable, or malformed ledger yields no penalties, so
    `--calibrated` degrades to the default ranking instead of failing.
    """
    path = Path(".mycelium/calibration.json")
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    buckets = data.get("by_confidence") if isinstance(data, dict) else None
    if not isinstance(buckets, dict):
        return {}
    penalties = {}
    for bucket, stats in buckets.items():
        rate = stats.get("rate") if isinstance(stats, dict) else None
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            continue
        penalties[str(bucket)] = min(1.0, max(0.0, float(rate)))
    return penalties


def calibrated_confidence(rank, confidence, penalties):
    if not penalties:
        return rank
    bucket = str(confidence).strip().lower() or "unset"
    return rank * (1.0 - penalties.get(bucket, 0.0))


def command_search(args):
    index_path = Path(".mycelium/index.json")
    # ponytail: rebuild on every search; add write-time invalidation if node volume makes this measurable.
    skipped = []
    nodes = load_nodes(skipped=skipped)
    build_index(nodes)
    if skipped:
        print(
            f"{len(nodes)} nodes loaded, {len(skipped)} skipped (run mycelium-lint for detail)",
            file=sys.stderr,
        )
    if not index_path.is_file():
        return 0

    items = json.loads(index_path.read_text(encoding="utf-8") or "[]")
    evidence_by_id = {node["nodeId"]: node["evidence"] for node in nodes}
    nodes_by_id = {node["nodeId"]: node for node in nodes}
    superseded = superseded_node_ids(nodes)
    goal_terms = terms(args.goal)
    ranked = []
    ignored = []
    confidence_rank = {"high": 3, "medium": 2, "low": 1, "": 0}
    calibration = calibration_penalties() if getattr(args, "calibrated", False) else {}
    for item in items:
        if args.topic and args.topic.lower() not in str(item.get("topics", "")).lower():
            continue
        matches = []
        matched_terms = set()
        for field in SEARCH_FIELDS:
            value = (
                evidence_by_id.get(item["nodeId"], "")
                if field == "evidence"
                else item.get(field, "")
            )
            field_terms = [term for term in goal_terms if term in str(value).lower()]
            if field_terms:
                matches.append(f'{field}:{",".join(field_terms)}')
                matched_terms.update(field_terms)
        if not matched_terms:
            continue
        if str(item.get("invalidated", "")).lower() == "true":
            ignored.append(f'{item["nodeId"]}:invalidated')
            continue
        if item["nodeId"] in superseded:
            ignored.append(f'{item["nodeId"]}:superseded')
            continue
        is_reflection = "reflection" in [
            t.strip() for t in str(item.get("topics", "")).lower().split(",")
        ]
        # A reflection records why an attempt failed, so it cites no source
        # range and `reflect` never sets `source_refs`. Holding it to the
        # currency gate would drop every real reflection before ranking, which
        # is exactly what made the reflection-first tie-break dead code. The
        # exemption is scoped to search; `stem` keeps the gate, and reflections
        # are `status: blocked` so `eligible_current` already excludes them
        # from STEM's pool.
        if not is_reflection and not source_refs_current(nodes_by_id[item["nodeId"]]):
            ignored.append(f'{item["nodeId"]}:stale-source-refs')
            continue
        ranked.append(
            (
                is_reflection,
                len(matched_terms),
                calibrated_confidence(
                    confidence_rank.get(item.get("confidence", ""), 0),
                    item.get("confidence", ""),
                    calibration,
                ),
                item.get("updated", ""),
                item,
                ";".join(matches),
            )
        )

    ranked.sort(key=lambda row: row[3], reverse=True)
    ranked.sort(key=lambda row: row[2], reverse=True)
    ranked.sort(key=lambda row: row[1], reverse=True)
    ranked.sort(key=lambda row: row[0], reverse=True)
    compact = lambda value: re.sub(r"[\t\r\n]+", " ", str(value)).strip()
    ignored_trace = ",".join(ignored) or "none"
    if ignored:
        print(
            f"trace\tquery={compact(args.goal)}\tindex=v{INDEX_VERSION}\tignored={ignored_trace}",
            file=sys.stderr,
        )
    for _, _, _, _, item, match_reason in ranked[:10]:
        print(
            "\t".join(
                [
                    *(compact(item.get(key, "")) for key in (
                        "nodeId",
                        "role",
                        "status",
                        "confidence",
                        "topics",
                        "firstFact",
                    )),
                    f"match={match_reason}",
                    f"query={compact(args.goal)}",
                    f'source_refs={compact(item.get("source_refs", ""))}',
                    f'depends_on={compact(item.get("depends_on", ""))}',
                    f'consumes={compact(item.get("consumes", ""))}',
                    f'supersedes={compact(item.get("supersedes", ""))}',
                    f'depended_on_by={",".join(item.get("depended_on_by", []))}',
                    f'superseded_by={",".join(item.get("superseded_by", []))}',
                    f'index=v{item.get("indexVersion", INDEX_VERSION)}',
                    f"ignored={ignored_trace}",
                ]
            )
        )
    return 0


def write_stem_ledger(goal, selected, ignored, conflicts, cap_dispatch):
    flow_dir = Path(".mycelium/flows")
    flow_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("stem-ledger-%Y%m%d%H%M%S%f.json")
    payload = {
        "goal": goal,
        "selected_nodes": selected,
        "ignored_nodes": ignored,
        "conflicts": conflicts,
        "cap_dispatch": cap_dispatch,
        "created": timestamp(),
    }
    (flow_dir / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def command_stem(args):
    goal_terms = terms(args.goal)
    required = 2 if len(goal_terms) > 1 else 1
    seen = set()
    rows = []
    candidate_ids = []
    candidate_prefixes = (
        "match=", "query=", "source_refs=", "depends_on=", "consumes=",
        "supersedes=", "depended_on_by=", "superseded_by=", "index=", "ignored=",
    )
    for record in stdin_lines():
        candidate = record.split("\t")
        trace_columns = [
            index
            for index, value in enumerate(candidate)
            if any(value.startswith(prefix) for prefix in candidate_prefixes)
        ]
        if trace_columns:
            if (
                len(candidate) != 16
                or any(
                    not candidate[index + 6].startswith(prefix)
                    for index, prefix in enumerate(candidate_prefixes)
                )
            ):
                print("malformed search candidate", file=sys.stderr)
                return 2
            if candidate[0] not in candidate_ids:
                candidate_ids.append(candidate[0])
            continue
        parts = record.split("\t", 3)
        if len(parts) < 4 or parts[3] in seen:
            continue
        seen.add(parts[3])
        rows.append(
            {
                "score": sum(term in parts[3].lower() for term in goal_terms),
                "path": parts[1],
                "line": parts[2],
                "fact": parts[3],
            }
        )

    if rows:
        rows.sort(key=lambda row: (row["path"], row["line"]))
        rows.sort(key=lambda row: row["score"], reverse=True)
        selected = rows[:10]
        write_stem_ledger(
            args.goal,
            [f'{row["path"]}:{row["line"]}' for row in selected],
            [],
            [],
            "use selected pipeline facts for the spore and verification.",
        )
        for row in selected:
            print(f'stem\t{row["path"]}\t{row["line"]}\t{row["fact"]}')
        return 0

    if not Path(".mycelium/nodes").is_dir():
        return 0

    skipped = []
    role_rank = {"hyphae": 3, "septum": 2, "apex": 1, "stem": 0, "cap": 0}
    confidence_rank = {"high": 3, "medium": 2, "low": 1, "": 0}
    index_items = []
    if candidate_ids:
        index_path = Path(".mycelium/index.json")
        if not index_path.is_file():
            build_index()
        index_items = json.loads(index_path.read_text(encoding="utf-8") or "[]")
        indexed_by_id = {item["nodeId"]: item for item in index_items}
        requested_ids = list(candidate_ids[:5])
        for candidate_id in list(requested_ids):
            for dependency_id in relation_ids(
                indexed_by_id.get(candidate_id, {}).get("depends_on", "")
            ):
                if len(requested_ids) >= 10:
                    break
                if dependency_id not in requested_ids:
                    requested_ids.append(dependency_id)
        nodes = load_nodes(
            (
                indexed_by_id[node_id]["node_path"]
                for node_id in requested_ids
                if node_id in indexed_by_id
            ),
            skipped=skipped,
        )
        superseded = superseded_node_ids(index_items)
    else:
        nodes = load_nodes(skipped=skipped)
        superseded = superseded_node_ids(nodes)
    if skipped:
        print(
            f"{len(nodes)} nodes loaded, {len(skipped)} skipped (run mycelium-lint for detail)",
            file=sys.stderr,
        )
    for node in nodes:
        haystack = " ".join((node["nodeId"], node["topics"], node["facts"])).lower()
        node["score"] = sum(term in haystack for term in goal_terms)
        node["confidence_rank"] = confidence_rank.get(node["confidence"], 0)
        node["role_rank"] = role_rank.get(node["role"], 0)
        node["source_current"] = source_refs_current(node)

    nodes_by_id = {node["nodeId"]: node for node in nodes}
    if candidate_ids:
        ranked = [
            nodes_by_id[node_id]
            for node_id in candidate_ids[:5]
            if node_id in nodes_by_id
            and eligible_current(nodes_by_id[node_id])
            and node_id not in superseded
            and (not args.for_cap or nodes_by_id[node_id]["source_current"])
        ]
    else:
        ranked = [
            node
            for node in nodes
            if eligible_current(node)
            and node["nodeId"] not in superseded
            and node["score"] >= required
            and (not args.for_cap or node["source_current"])
        ]
        ranked.sort(key=lambda node: node["nodeId"])
        ranked.sort(key=lambda node: node["updated"], reverse=True)
        ranked.sort(key=lambda node: node["role_rank"], reverse=True)
        ranked.sort(key=lambda node: node["confidence_rank"], reverse=True)
        ranked.sort(key=lambda node: node["score"], reverse=True)
    direct_selected = ranked[:5]
    selected = list(direct_selected)
    selected_ids = {node["nodeId"] for node in selected}
    for node in direct_selected:
        for dependency_id in relation_ids(node["depends_on"]):
            if len(selected) >= 10:
                break
            dependency = nodes_by_id.get(dependency_id)
            if (
                dependency
                and dependency_id not in selected_ids
                and dependency_id not in superseded
                and eligible_current(dependency)
                and (not args.for_cap or dependency["source_current"])
            ):
                selected.append(dependency)
                selected_ids.add(dependency_id)
    ignored = [node for node in nodes if node["nodeId"] not in selected_ids][:5]

    claims = {}
    conflicts = []
    for node in selected:
        for line in node["facts"].splitlines():
            match = re.match(r"^\s*[-*]?\s*(.+?)\s+is\s+(.+?)\.?\s*$", line)
            if not match:
                continue
            subject = match.group(1).strip().lower()
            value = match.group(2).strip()
            if subject in claims and claims[subject]["value"] != value:
                previous = claims[subject]
                conflicts.append(
                    f'{subject} differs: {previous["node_id"]} says {previous["value"]}; '
                    f'{node["nodeId"]} says {value}'
                )
            else:
                claims[subject] = {"value": value, "node_id": node["nodeId"]}

    sources = {}
    duplicate_sources = []
    for node in selected:
        for source in re.split(r"[,;]", node["source_refs"]):
            source = source.strip()
            if source:
                sources.setdefault(source, []).append(node["nodeId"])
    for source, node_ids in sources.items():
        unique_ids = list(dict.fromkeys(node_ids))
        if len(unique_ids) > 1:
            duplicate_sources.append(f'{source} cited by {", ".join(unique_ids)}')

    cap_dispatch = "use selected node IDs and node-file paths for the spore and verification."
    write_stem_ledger(
        args.goal,
        [node["nodeId"] for node in selected],
        [node["nodeId"] for node in ignored],
        conflicts,
        cap_dispatch,
    )

    if args.for_cap:
        if conflicts:
            print("Conflicts:")
            write_lines(f"- {conflict}" for conflict in conflicts)
            return 0
        for node in selected:
            for fact in node["factRows"]:
                print(
                    f'stem\t.mycelium/nodes/{node["path"].name}\t{fact["line"]}'
                    f'\t[{node["nodeId"]}] {fact["text"]}'
                )
        return 0

    print(f"Goal: {args.goal}")
    print("Selected nodes:")
    for node in selected:
        print(f'- {node["nodeId"]} [{node["role"]}, {node["confidence"]}] {node["facts"]}')
    print("Ignored nodes:")
    for node in ignored:
        if str(node["invalidated"]).lower() == "true":
            reason = "invalidated"
        elif node["nodeId"] in superseded:
            reason = "superseded"
        elif not node["flowCommitted"]:
            reason = "missing flow commit"
        elif node["status"] != "complete":
            reason = f'status {node["status"]}'
        elif args.for_cap and not node["source_current"]:
            reason = "stale source refs"
        elif node["score"] < required:
            reason = "insufficient goal match"
        elif not node["source_refs"].strip():
            reason = "missing source refs"
        else:
            reason = "lower ranked"
        print(f'- {node["nodeId"]} [{node["role"]}, {node["confidence"]}] {reason}')
    if conflicts:
        print("Conflicts:")
        write_lines(f"- {conflict}" for conflict in conflicts)
    else:
        print("Conflicts: none found in selected nodes")
    if duplicate_sources:
        print("Duplicate source warnings:")
        write_lines(f"- {warning}" for warning in duplicate_sources)
    else:
        print("Duplicate source warnings: none")
    print("Enough evidence because: selected nodes match the goal terms and include durable evidence for CAP.")
    print("Missing but not needed: no further discovery required before CAP unless CAP verification fails.")
    print("Next role: CAP")
    print(f"CAP dispatch: {cap_dispatch}")
    return 0


def command_cap(args):
    records = stdin_lines()
    text = "\n".join(records)
    if re.search(r"(?im)^-\s+.+ differs: .+ says .+; .+ says .+", text):
        write_lines(
            (
                f"Goal: {args.goal}",
                "Spore: blocked by unresolved STEM conflict.",
                "Verification: FAIL",
                "Residual risk: selected evidence conflicts.",
                "Follow-up: return to STEM with conflicting Node IDs and resolve before final CAP.",
            )
        )
        return 1

    rows = []
    for record in records:
        parts = record.split("\t", 3)
        if len(parts) >= 4:
            rows.append({"path": parts[1], "line": parts[2], "fact": parts[3]})

    print(f"Goal: {args.goal}")
    print("Spore:")
    print("Result:")
    if not rows:
        write_lines(
            (
                "- No relevant facts found.",
                "Verification: FAIL",
                "Residual risk: no facts reached CAP.",
                "Follow-up: run APEX, SEPTUM, HYPHAE, and STEM again with narrower evidence.",
            )
        )
        return 1
    for row in rows:
        print(f'- {row["fact"]} ({row["path"]}:{row["line"]})')
    write_lines(
        (
            "Verification: EXTERNAL_REQUIRED",
            "Residual risk: selected facts and source evidence are not yet externally verified.",
            "Follow-up: run the external verifier before claiming PASS.",
        )
    )
    return 0


def node_topics(node_id):
    """The `topics` frontmatter field of an already written node, or `""` when
    the file is absent or unreadable. Only the lowercase frontmatter key is
    matched; the body's `Topics:` line is a different spelling on purpose."""
    path = Path(".mycelium/nodes") / f"{safe_id(node_id)}.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(?m)^topics:[ \t]*(.*?)[ \t]*\r?$", text)
    return match.group(1) if match else ""


def supersedes_a_reflection(supersedes):
    """True when `supersedes` names at least one node whose topics include
    `reflection`. A missing node file is not an error here: the supersede may
    name an id this workspace never wrote."""
    for target in relation_ids(supersedes):
        if "reflection" in [
            topic.strip() for topic in node_topics(target).lower().split(",")
        ]:
            return True
    return False


def write_reflections_digest():
    skipped = []
    nodes = load_nodes(skipped=skipped)
    superseded = superseded_node_ids(nodes)
    reflections = [
        node
        for node in nodes
        if "reflection"
        in [t.strip() for t in str(node.get("topics", "")).lower().split(",")]
        and str(node.get("invalidated", "")).lower() != "true"
        and node["nodeId"] not in superseded
    ]
    reflections.sort(key=lambda node: node["nodeId"])
    reflections.sort(key=lambda node: node["updated"], reverse=True)
    lines = ["# Known failure modes (generated; do not edit)", ""]
    for node in reflections:
        lines.append(
            "- {nodeId} ({updated}) — blocked on: {blocks} — next: {reflection}".format(
                nodeId=node["nodeId"],
                updated=node.get("updated", ""),
                blocks=node.get("blocks", ""),
                reflection=node.get("reflection", ""),
            )
        )
    digest_path = Path(".mycelium/reflections-digest.md")
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return digest_path, nodes, skipped


def command_digest(_args):
    digest_path, nodes, skipped = write_reflections_digest()
    if skipped:
        print(
            f"{len(nodes)} nodes loaded, {len(skipped)} skipped (run mycelium-lint for detail)",
            file=sys.stderr,
        )
    print(digest_path.as_posix())
    return 0


def command_reflect(args):
    failed = safe_id(args.failed_node_id, "node")
    reflection_id = f"reflection-{failed}"
    write_node(
        args.goal,
        reflection_id,
        "cap",
        "blocked",
        args.failure_summary,
        "",
        args.next_attempt,
        topics="reflection,verification",
        evidence=args.failure_summary,
        confidence="medium",
        consumes=failed,
        blocks=args.failure_summary,
        reflection=args.next_attempt,
        # A reflection records why the previous attempt failed, so it is never
        # a member of the run it criticises. It stays untracked by design and
        # says so in the node.
        untracked_reason="reflection",
    )
    write_reflections_digest()
    print(reflection_id)
    return 0


def command_subagent_brief(args):
    # This brief is for hosts without a native subagent primitive. Hosts with
    # one (Claude Code `Agent`, plugin agents) dispatch directly and skip this
    # script. This command prints a role brief; it has never dispatched
    # anything.
    contracts = {
        "apex": "Many APEX workers are allowed when each has a separate search path. Gather only local evidence relevant to the goal. Evidence must name commands, files, URLs, or line references used.",
        "septum": "Many SEPTUM workers are allowed when each owns a separate filter boundary. Do not edit files. Pass only records that help the goal and name the kept/dropped boundary.",
        "hyphae": "Many HYPHAE workers are allowed when each digests a separate evidence bundle. Compress evidence, keep source paths and line numbers, and preserve uncertainty.",
        "stem": "Only one STEM coordinates the tracked run. Merge worker outputs, select the smallest useful fact set, surface conflicts, and decide when evidence is enough.",
        "cap": "CAP is mandatory. Many CAP workers are allowed when producing separate spores such as final answer, review, or verification. Include verification evidence or an explicit no-verification reason.",
    }
    node_id = f'{args.role}-{safe_id(args.instance)}' if args.instance else args.role
    instance_lines = (
        f"Instance: {args.instance}\nProducing Agent: {args.instance}\n"
        if args.instance
        else ""
    )
    run_line = f"Run ID: {args.run_id}\n" if args.run_id else "Run ID: untracked\n"
    process_input_text = ",".join(args.process_input) if args.process_input else "none"
    # Node writes are tracked by default, so an untracked brief has to print a
    # record command that names its own reason.
    lineage_arguments = ' --allow-untracked "no run id supplied"'
    if args.run_id:
        lineage_arguments = f' --run-id "{args.run_id}"' + "".join(
            f' --process-input "{node_id}"' for node_id in args.process_input
        )
    print(
        f"""This brief is for hosts without a native subagent primitive. Hosts with one (Claude Code `Agent`, plugin agents) dispatch directly and skip this script.

You are mycelium {args.role.upper()}.

Goal: {args.goal}
{instance_lines}
{run_line}Process Inputs: {process_input_text}
Node ID: {node_id}
Parent Goal: {args.goal}
Scope: {args.instance}
Input command: follow the coordinator prompt for this role
Output contract:
- Node ID
- Run ID when supplied
- Process Inputs
- Status
- Topics
- Facts
- Evidence
- Source Refs
- Confidence
- Consumes
- Questions
- Blocks
- Next
- Trace when tools were used: Action; Observation; Result
Trace must name the invoked spawn tool and returned agent ID.
Wake condition: coordinator references this Node ID again
Spawn tool: {args.spawn_tool}
Spawn rule: STEM invokes this capability with the brief using the tool's declared schema; scripts cannot spawn host subagents. Never spawn another STEM for the same tracked run.

{contracts[args.role]}

Record command:
bin/mycelium-node.sh "{args.goal}" "<node-id>" {args.role} <status> "<facts>" "<questions>" "<next>" --topics "<topics>" --evidence "<evidence>" --confidence <confidence> --consumes "<consumes>" --blocks "<blocks>" --trace "<trace>" --producing-agent "{args.instance}" --parent-goal-id "{args.goal}" --source-refs "<source-refs>" --version "2"{lineage_arguments}

Record contract: role\tpath\tline\ttext

Return only your result. Do not include unrelated context."""
    )
    return 0


def add_node_options(parser):
    parser.add_argument("goal")
    parser.add_argument("node_id")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("status", choices=STATUSES)
    parser.add_argument("facts", nargs="?", default="")
    parser.add_argument("questions", nargs="?", default="")
    parser.add_argument("next_step", nargs="?", default="")
    parser.add_argument("--topics", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--confidence", choices=CONFIDENCES, default="")
    parser.add_argument("--consumes", default="")
    parser.add_argument("--blocks", default="")
    parser.add_argument("--trace", default="")
    parser.add_argument("--reflection", default="")
    parser.add_argument("--parent-goal-id", default="")
    parser.add_argument("--producing-agent", default="")
    parser.add_argument("--version")
    parser.add_argument("--source-refs", default="")
    parser.add_argument("--depends-on", default="")
    parser.add_argument("--invalidated", action="store_true")
    parser.add_argument("--supersedes", default="")
    # dest avoids the subparser's own "command" destination.
    parser.add_argument("--command", dest="node_command", default="")
    parser.add_argument("--expect", default="")
    parser.add_argument("--run-id", default="")
    # dest is explicit so a future --allow flag cannot silently take it over.
    parser.add_argument("--allow-untracked", dest="allow_untracked", default="")
    parser.add_argument("--process-input", action="append", default=[])


def parser():
    root = argparse.ArgumentParser(description="Native Linux Mycelium commands")
    commands = root.add_subparsers(dest="command", required=True)

    apex_help = "print candidate lines that contain at least one goal term"
    apex = commands.add_parser("apex", help=apex_help, description=apex_help)
    apex.add_argument("goal")
    apex.set_defaults(run=command_apex)

    septum_help = "keep piped candidate lines that match the term threshold"
    septum = commands.add_parser("septum", help=septum_help, description=septum_help)
    septum.add_argument("goal")
    septum.set_defaults(run=command_septum)

    hyphae_help = "normalize whitespace in piped candidate lines"
    hyphae = commands.add_parser("hyphae", help=hyphae_help, description=hyphae_help)
    hyphae.set_defaults(run=command_hyphae)

    stem = commands.add_parser("stem")
    stem.add_argument("goal")
    stem.add_argument("--for-cap", action="store_true")
    stem.set_defaults(run=command_stem)

    cap = commands.add_parser("cap")
    cap.add_argument("goal")
    cap.set_defaults(run=command_cap)

    node = commands.add_parser("node")
    add_node_options(node)
    node.set_defaults(run=command_node)

    index = commands.add_parser("index")
    index.set_defaults(run=command_index)

    lint = commands.add_parser("lint")
    lint.set_defaults(run=command_lint)

    search = commands.add_parser("search")
    search.add_argument("goal")
    search.add_argument("topic", nargs="?", default="")
    search.add_argument("--calibrated", action="store_true")
    search.set_defaults(run=command_search)

    reflect = commands.add_parser("reflect")
    reflect.add_argument("goal")
    reflect.add_argument("failed_node_id")
    reflect.add_argument("failure_summary")
    reflect.add_argument("next_attempt")
    reflect.set_defaults(run=command_reflect)

    digest = commands.add_parser("digest")
    digest.set_defaults(run=command_digest)

    brief = commands.add_parser("subagent-brief")
    brief.add_argument("role", choices=ROLES)
    brief.add_argument("goal")
    brief.add_argument("instance", nargs="?", default="")
    brief.add_argument("--spawn-tool", default="collaboration.spawn_agent")
    brief.add_argument("--run-id", default="")
    brief.add_argument("--process-input", action="append", default=[])
    brief.set_defaults(run=command_subagent_brief)
    return root


def main():
    args = parser().parse_args()
    try:
        return args.run(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
