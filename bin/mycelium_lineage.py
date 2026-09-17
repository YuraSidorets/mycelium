#!/usr/bin/env python3
"""Durable, explicit process lineage for Mycelium runs.

Evidence dependencies remain in schema-v2 nodes.  This module owns only the
execution topology and binds each process vertex to an exact node/flow commit.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
import uuid


SCHEMA = 1
ROLES = ("apex", "septum", "hyphae", "stem", "cap")
ROLE_RANK = {role: index for index, role in enumerate(ROLES)}
MANDATORY_ROLES = ("apex", "stem", "cap")
SKIP_CODES = {
    "septum": "single-clean-stream",
    "hyphae": "already-compact",
}
SAFE_SKIP_EVIDENCE = {
    "input_packets": 1,
    "all_directly_sourced": True,
    "conflicts": False,
    "duplicates": False,
    "stale": False,
    "uncertainty": False,
    "risk": "low",
}
TOPOLOGY_PRESETS = {
    "apex-stem-cap": [
        {"role": "apex", "state": "required"},
        {
            "role": "septum",
            "state": "skipped",
            "skip": {
                "code": SKIP_CODES["septum"],
                "evidence": deepcopy(SAFE_SKIP_EVIDENCE),
            },
        },
        {
            "role": "hyphae",
            "state": "skipped",
            "skip": {
                "code": SKIP_CODES["hyphae"],
                "evidence": deepcopy(SAFE_SKIP_EVIDENCE),
            },
        },
        {"role": "stem", "state": "required"},
        {"role": "cap", "state": "required"},
    ],
}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z\Z")
TOP_LEVEL_FIELDS = {
    "schema", "run_id", "goal", "state", "created_at", "updated_at",
    "sealed_at", "topology", "roles", "nodes", "diagnostics",
}
NODE_FIELDS = {
    "node_id", "role", "process_inputs", "sequence", "status",
    "reservation_token", "reserved_at", "finalized_at", "aborted_at",
    "abort_reason", "commit",
}
NODE_V2_FRONTMATTER_FIELDS = {
    "nodeId", "goal", "role", "status", "topics", "confidence", "consumes",
    "blocks", "parent_goal_id", "producing_agent", "version", "source_refs",
    "depends_on", "invalidated", "supersedes", "updated",
}
FLOW_V2_FIELDS = {
    "timestamp", "goal", "nodeId", "role", "status", "topics", "evidence",
    "confidence", "consumes", "blocks", "trace", "reflection", "parentGoalId",
    "producingAgent", "version", "sourceRefs", "dependsOn", "invalidated",
    "supersedes", "facts", "questions", "next",
}
# Optional re-execution contract. These are allowed but never required: runs
# sealed before the fields existed are re-validated against the same sets on
# every read, so making them mandatory would retroactively invalidate history.
NODE_V2_OPTIONAL_FIELDS = {"command", "expect", "untracked_reason"}
FLOW_V2_OPTIONAL_FIELDS = {"command", "expect", "untracked_reason"}


class ValidationError(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("; ".join(item["message"] for item in diagnostics))


_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_POSIX_LOCK_MODE = 0o644
_POSIX_ARTIFACT_MODE = 0o644
_POSIX_DIRECTORY_MODE = 0o755


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _safe_id(value, name):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(
            f"{name} must match {SAFE_ID.pattern!r} and contain no path separators"
        )
    if value in (".", ".."):
        raise ValueError(f"invalid {name}: {value}")
    return value


def _root(root):
    return Path(root).resolve(strict=True)


def _is_reparse_or_symlink(path):
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    return bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def _contained(root, path):
    try:
        path.resolve(strict=False).relative_to(root)
        return True
    except ValueError:
        return False


def _safe_runs_dir(root, *, create=False):
    root_input = Path(root).absolute()
    if not root_input.exists():
        raise ValueError(f"repository root does not exist: {root}")
    if _is_reparse_or_symlink(root_input):
        raise ValueError(f"repository root cannot be a symlink or reparse point: {root_input}")
    root_resolved = root_input.resolve(strict=True)
    current = root_input
    for name in (".mycelium", "runs"):
        current = current / name
        if create:
            try:
                current.mkdir()
            except FileExistsError:
                if not current.is_dir():
                    raise
            else:
                if os.name != "nt":
                    flags = os.O_RDONLY
                    for flag_name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
                        flags |= getattr(os, flag_name, 0)
                    descriptor = os.open(current, flags)
                    try:
                        opened = os.fstat(descriptor)
                        current_state = os.stat(current, follow_symlinks=False)
                        if not stat.S_ISDIR(opened.st_mode) or not os.path.samestat(
                            opened, current_state
                        ):
                            raise ValueError(
                                f"process run directory changed while creating it: {current}"
                            )
                        os.fchmod(descriptor, _POSIX_DIRECTORY_MODE)
                    finally:
                        os.close(descriptor)
        if not current.exists():
            raise ValueError(f"process run directory does not exist: {current}")
        if _is_reparse_or_symlink(current):
            raise ValueError(f"process run path cannot be a symlink or reparse point: {current}")
        if not current.is_dir() or not _contained(root_resolved, current):
            raise ValueError(f"process run path escapes repository root: {current}")
    return current.resolve(strict=True)


def _safe_run_target(root, path, *, allow_missing):
    root_resolved = _root(root)
    directory = _safe_runs_dir(root, create=False)
    path = Path(path)
    if path.parent.resolve(strict=True) != directory:
        raise ValueError(f"process run target escapes runs directory: {path}")
    if path.exists() or path.is_symlink():
        if _is_reparse_or_symlink(path):
            raise ValueError(f"process run target cannot be a symlink or reparse point: {path}")
        if not _contained(root_resolved, path):
            raise ValueError(f"process run target escapes repository root: {path}")
    elif not allow_missing:
        raise FileNotFoundError(path)
    return path


def _runs_dir(root):
    return _safe_runs_dir(root, create=False)


def _manifest_path(root, run_id):
    path = _runs_dir(root) / f"{_safe_id(run_id, 'run id')}.json"
    return _safe_run_target(root, path, allow_missing=True)


def _thread_lock(path):
    key = str(path.resolve()).casefold() if os.name == "nt" else str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _lock_file(handle, *, shared=False):
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except (OSError, PermissionError):
                time.sleep(0.05)
    else:
        import fcntl

        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), operation)


def _unlock_file(handle):
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_run_lock(root, path, *, shared):
    read_only = shared and os.name != "nt"
    flags = os.O_RDONLY if read_only else os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    mode = 0o600 if os.name == "nt" else _POSIX_LOCK_MODE
    created = False
    if shared:
        descriptor = os.open(path, flags)
    else:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, mode)
            created = True
        except FileExistsError:
            descriptor = os.open(path, flags, mode)

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"process run lock must be a regular file: {path}")
        if opened.st_nlink != 1:
            raise ValueError(f"process run lock cannot be a hard link: {path}")

        _safe_run_target(root, path, allow_missing=False)
        current = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(opened, current):
            raise ValueError(f"process run lock changed while opening: {path}")

        if os.name != "nt" and created:
            # The lock contains no task data.  A protected verifier opens this
            # descriptor read-only and takes LOCK_SH; writers retain LOCK_EX.
            os.fchmod(descriptor, _POSIX_LOCK_MODE)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def _run_lock(root, run_id, *, create_directory=False, shared=False):
    run_id = _safe_id(run_id, "run id")
    directory = _safe_runs_dir(root, create=create_directory)
    path = directory / f"{run_id}.lock"
    with _thread_lock(path):
        _safe_run_target(root, path, allow_missing=True)
        descriptor = _open_run_lock(root, path, shared=shared)
        mode = "rb" if shared and os.name != "nt" else "r+b"
        with os.fdopen(descriptor, mode, buffering=0) as handle:
            if not shared and os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            _lock_file(handle, shared=shared)
            try:
                yield
            finally:
                _unlock_file(handle)


def _atomic_write_json(path, value, *, root):
    path = _safe_run_target(root, path, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            if os.name != "nt":
                os.fchmod(handle.fileno(), _POSIX_ARTIFACT_MODE)
        os.replace(temporary, path)
        temporary = None
        _safe_run_target(root, path, allow_missing=False)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _normalize_topology(topology):
    if isinstance(topology, str):
        try:
            topology = deepcopy(TOPOLOGY_PRESETS[topology])
        except KeyError as error:
            raise ValueError(f"unknown topology preset: {topology}") from error
    if topology is None:
        topology = [{"role": role, "state": "required"} for role in ROLES]
    if not isinstance(topology, list) or len(topology) != len(ROLES):
        raise ValueError("topology must contain each of the five roles exactly once")
    normalized = []
    for index, raw in enumerate(topology):
        if not isinstance(raw, dict):
            raise ValueError("each topology entry must be an object")
        role = raw.get("role")
        if role != ROLES[index]:
            raise ValueError("topology roles must be ordered apex, septum, hyphae, stem, cap")
        state = raw.get("state")
        if state == "required":
            if set(raw) != {"role", "state"}:
                raise ValueError(f"required {role} topology entry has unknown fields")
            normalized.append({"role": role, "state": state})
            continue
        if state != "skipped":
            raise ValueError(f"invalid topology state for {role}: {state}")
        if role not in SKIP_CODES:
            raise ValueError(f"{role} cannot be skipped")
        if set(raw) != {"role", "state", "skip"} or not isinstance(raw["skip"], dict):
            raise ValueError(f"{role} skip requires structured code and evidence")
        skip = raw["skip"]
        if set(skip) != {"code", "evidence"}:
            raise ValueError(f"{role} skip requires exactly code and evidence")
        if skip["code"] != SKIP_CODES[role]:
            raise ValueError(f"invalid skip code for {role}: {skip['code']}")
        evidence = skip["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != set(SAFE_SKIP_EVIDENCE):
            raise ValueError(f"invalid skip evidence fields for {role}")
        exact_types = all(type(evidence[key]) is type(value) for key, value in SAFE_SKIP_EVIDENCE.items())
        if not exact_types or evidence != SAFE_SKIP_EVIDENCE:
            raise ValueError(f"unsafe or incorrectly typed skip evidence for {role}")
        normalized.append(
            {
                "role": role,
                "state": state,
                "skip": {"code": skip["code"], "evidence": deepcopy(evidence)},
            }
        )
    return normalized


def preflight(*, root=Path(".")):
    """Check runtime support without creating any files or directories."""
    del root
    supported = sys.version_info >= (3, 9)
    return {
        "ok": supported,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "executable": sys.executable,
        "minimum": "3.9",
        "schema": SCHEMA,
    }


def load_manifest(run_id, *, root=Path(".")):
    path = _manifest_path(root, run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ValueError(f"unknown process run: {run_id}") from error
    value = json.loads(text, object_pairs_hook=_json_object)
    if not isinstance(value, dict):
        raise ValueError(f"process manifest must be a JSON object: {path}")
    return value


def begin(goal, topology=None, *, run_id=None, root=Path(".")):
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string")
    if "\r" in goal or "\n" in goal:
        raise ValueError("goal must be a single line")
    topology = _normalize_topology(topology)
    if run_id is None:
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:48] or "goal"
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{slug}-{uuid.uuid4().hex[:8]}"
    run_id = _safe_id(run_id, "run id")
    with _run_lock(root, run_id, create_directory=True):
        path = _manifest_path(root, run_id)
        if path.exists():
            current = load_manifest(run_id, root=root)
            _require_manifest_shape(current, run_id)
            if current.get("goal") == goal and current.get("topology") == topology:
                return {
                    "run_id": run_id,
                    "state": current.get("state"),
                    "manifest": path.relative_to(_root(root)).as_posix(),
                    "idempotent": True,
                }
            raise ValueError(f"process run already exists with different contract: {run_id}")
        now = _timestamp()
        manifest = {
            "schema": SCHEMA,
            "run_id": run_id,
            "goal": goal,
            "state": "open",
            "created_at": now,
            "updated_at": now,
            "sealed_at": None,
            "topology": topology,
            "roles": [
                {
                    "role": item["role"],
                    "state": "skipped" if item["state"] == "skipped" else "pending",
                }
                for item in topology
            ],
            "nodes": {},
            "diagnostics": [],
        }
        _atomic_write_json(path, manifest, root=root)
    return {
        "run_id": run_id,
        "state": "open",
        "manifest": path.relative_to(_root(root)).as_posix(),
        "idempotent": False,
    }


def _assert_open(manifest):
    if manifest.get("state") != "open":
        raise ValueError(f"process run is {manifest.get('state')}; mutation is not allowed")


def _topology_by_role(manifest):
    return {item["role"]: item for item in manifest["topology"]}


def _normalize_inputs(process_inputs):
    if process_inputs is None:
        process_inputs = []
    if not isinstance(process_inputs, (list, tuple)):
        raise ValueError("process inputs must be an array")
    result = []
    for value in process_inputs:
        value = _safe_id(value, "process input node id")
        if value in result:
            raise ValueError(f"duplicate process input: {value}")
        result.append(value)
    return result


def _validate_transition(manifest, parent, child_role):
    parent_role = parent["role"]
    parent_rank = ROLE_RANK[parent_role]
    child_rank = ROLE_RANK[child_role]
    if parent_rank >= child_rank:
        kind = "same-role" if parent_rank == child_rank else "backward"
        raise ValueError(f"{kind} process edge is invalid: {parent_role} -> {child_role}")
    topology = _topology_by_role(manifest)
    blocked = [
        role
        for role in ROLES[parent_rank + 1 : child_rank]
        if topology[role]["state"] == "required"
    ]
    if blocked:
        raise ValueError(
            f"process edge {parent_role} -> {child_role} skips required role(s): {', '.join(blocked)}"
        )


def reserve(
    run_id,
    node_id,
    role,
    process_inputs=None,
    projection=None,
    *,
    root=Path("."),
):
    run_id = _safe_id(run_id, "run id")
    node_id = _safe_id(node_id, "node id")
    if role not in ROLES:
        raise ValueError(f"invalid role: {role}")
    inputs = _normalize_inputs(process_inputs)
    if projection is not None:
        _validate_retry_projection_shape(projection)
    with _run_lock(root, run_id):
        manifest = load_manifest(run_id, root=root)
        _require_manifest_shape(manifest, run_id)
        _assert_open(manifest)
        topology = _topology_by_role(manifest)
        if topology[role]["state"] != "required":
            raise ValueError(f"cannot register a node for skipped role: {role}")
        existing = manifest["nodes"].get(node_id)
        if existing is not None:
            exact = existing["role"] == role and existing["process_inputs"] == inputs
            if not exact:
                raise ValueError(f"conflicting reservation for node: {node_id}")
            if existing["status"] == "aborted":
                raise ValueError(f"reservation was aborted for node: {node_id}")
            commit = existing["commit"]
            if existing["status"] == "committed" and projection is not None:
                commit = _validate_retry_projection_locked(root, existing, projection)
            return {
                "run_id": run_id,
                "node_id": node_id,
                "reservation_token": existing["reservation_token"],
                "status": existing["status"],
                "commit": commit,
                "idempotent": True,
            }
        if role == "apex" and inputs:
            raise ValueError(
                "APEX process nodes cannot have process inputs; the process edge would be backward"
            )
        if role != "apex" and not inputs:
            raise ValueError(f"{role.upper()} process nodes require at least one process input")
        for parent_id in inputs:
            parent = manifest["nodes"].get(parent_id)
            if parent is None or parent.get("status") != "committed":
                raise ValueError(f"process input is not committed in this run: {parent_id}")
            _validate_transition(manifest, parent, role)
        token = uuid.uuid4().hex
        now = _timestamp()
        sequence = 1 + max(
            (
                item["sequence"]
                for item in manifest["nodes"].values()
                if type(item.get("sequence")) is int
            ),
            default=0,
        )
        manifest["nodes"][node_id] = {
            "node_id": node_id,
            "role": role,
            "process_inputs": inputs,
            "sequence": sequence,
            "status": "pending",
            "reservation_token": token,
            "reserved_at": now,
            "finalized_at": None,
            "aborted_at": None,
            "abort_reason": None,
            "commit": None,
        }
        manifest["updated_at"] = now
        manifest["diagnostics"] = []
        _atomic_write_json(_manifest_path(root, run_id), manifest, root=root)
    return {
        "run_id": run_id,
        "node_id": node_id,
        "reservation_token": token,
        "status": "pending",
        "commit": None,
        "idempotent": False,
    }


def _relative_artifact(root, path, directory, suffix):
    root = _root(root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    expected = (root / ".mycelium" / directory).resolve()
    try:
        resolved.relative_to(expected)
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"artifact must be under .mycelium/{directory}: {path}") from error
    if resolved.suffix != suffix:
        raise ValueError(f"artifact must have {suffix} suffix: {path}")
    return resolved, relative.as_posix()


def _node_metadata(data, path):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"node is not UTF-8: {path}") from error
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"node has no frontmatter: {path}")
    result = {}
    for line in lines[1:]:
        if line == "---":
            return result
        if ":" not in line:
            raise ValueError(f"invalid node frontmatter line: {path}: {line}")
        key, value = line.split(":", 1)
        if key in result:
            raise ValueError(f"duplicate node frontmatter key: {key}")
        result[key] = value.lstrip()
    raise ValueError(f"unterminated node frontmatter: {path}")


def _node_body_values(data, path):
    try:
        text = data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise ValueError(f"node is not UTF-8: {path}") from error
    lines = text.split("\n")
    scalar_names = ("Evidence", "Trace", "Reflection")
    scalars = {}
    for name in scalar_names:
        prefix = f"{name}: "
        matches = [line[len(prefix):] for line in lines if line.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"schema-v2 node requires exactly one {name} field: {path}")
        scalars[name.lower()] = matches[0]
    sections = {}
    for index, heading in enumerate(("Facts", "Questions", "Next")):
        marker = f"## {heading}"
        try:
            start = lines.index(marker) + 1
        except ValueError as error:
            raise ValueError(f"schema-v2 node missing {heading} section: {path}") from error
        end = len(lines)
        for later in ("Facts", "Questions", "Next")[index + 1:]:
            try:
                end = min(end, lines.index(f"## {later}", start))
            except ValueError:
                pass
        sections[heading.lower()] = "\n".join(lines[start:end]).strip()
    return {**scalars, **sections}


def _validate_schema_v2_commit(metadata, node_bytes, record, path):
    missing_node = sorted(NODE_V2_FRONTMATTER_FIELDS - set(metadata))
    missing_flow = sorted(FLOW_V2_FIELDS - set(record))
    if missing_node or missing_flow:
        raise ValueError(
            f"incomplete schema-v2 commit; missing node={missing_node}; missing flow={missing_flow}"
        )
    if metadata.get("version") != "2" or record.get("version") != "2":
        raise ValueError("process lineage requires schema-v2 node and flow commits")
    body = _node_body_values(node_bytes, path)
    required_text = {
        "topics": metadata.get("topics"),
        "evidence": body.get("evidence"),
        "confidence": metadata.get("confidence"),
        "consumes": metadata.get("consumes"),
        "blocks": metadata.get("blocks"),
        "facts": body.get("facts"),
        "questions": body.get("questions"),
        "next": body.get("next"),
    }
    missing = [name for name, value in required_text.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise ValueError(f"incomplete schema-v2 evidence fields: {', '.join(missing)}")
    if metadata.get("confidence") not in {"low", "medium", "high"}:
        raise ValueError("schema-v2 confidence must be low, medium, or high")
    for name in ("facts", "questions", "next"):
        values = record.get(name)
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"schema-v2 flow {name} must be a non-empty string array")
        if body[name] != "\n".join(values).strip():
            raise ValueError(f"schema-v2 node/flow {name} do not match")
    mappings = {
        "topics": "topics",
        "confidence": "confidence",
        "consumes": "consumes",
        "blocks": "blocks",
        "parent_goal_id": "parentGoalId",
        "producing_agent": "producingAgent",
        "version": "version",
        "source_refs": "sourceRefs",
        "depends_on": "dependsOn",
        "supersedes": "supersedes",
    }
    for node_name, flow_name in mappings.items():
        if metadata.get(node_name) != record.get(flow_name):
            raise ValueError(f"schema-v2 node/flow {node_name} do not match")
    if body["evidence"] != record.get("evidence"):
        raise ValueError("schema-v2 node/flow evidence do not match")
    if body["trace"] != record.get("trace") or body["reflection"] != record.get("reflection"):
        raise ValueError("schema-v2 node/flow trace or reflection do not match")
    invalidated = record.get("invalidated")
    if type(invalidated) is not bool or metadata.get("invalidated") != str(invalidated).lower() or invalidated:
        raise ValueError("invalidated schema-v2 commits cannot enter process lineage")


def build_commit_binding(root, node_id, role, node_path, flow_path, flow_line):
    """Bind a process vertex to current, complete schema-v2 artifacts."""
    node_id = _safe_id(node_id, "node id")
    if role not in ROLES:
        raise ValueError(f"invalid role: {role}")
    if type(flow_line) is not int or flow_line < 1:
        raise ValueError("flow line must be a positive integer")
    node_file, node_relative = _relative_artifact(root, node_path, "nodes", ".md")
    flow_file, flow_relative = _relative_artifact(root, flow_path, "flows", ".jsonl")
    if node_file.name != f"{node_id}.md":
        raise ValueError(f"node path does not match node id: {node_id}")
    node_bytes = node_file.read_bytes()
    metadata = _node_metadata(node_bytes, node_relative)
    flow_lines = flow_file.read_bytes().splitlines()
    if flow_line > len(flow_lines):
        raise ValueError(f"flow line does not exist: {flow_relative}:{flow_line}")
    line_bytes = flow_lines[flow_line - 1]
    try:
        record = json.loads(line_bytes.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid flow commit: {flow_relative}:{flow_line}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"flow commit must be a JSON object: {flow_relative}:{flow_line}")
    _validate_schema_v2_commit(metadata, node_bytes, record, node_relative)
    identities = {
        "node id": (metadata.get("nodeId"), record.get("nodeId"), node_id),
        "role": (metadata.get("role"), record.get("role"), role),
    }
    for name, values in identities.items():
        if values[0] != values[2] or values[1] != values[2]:
            raise ValueError(f"node/flow {name} does not match reservation: {values}")
    node_goal = metadata.get("goal")
    flow_goal = record.get("goal")
    if not isinstance(node_goal, str) or node_goal != flow_goal:
        raise ValueError("node and flow goal do not match")
    if metadata.get("status") != "complete" or record.get("status") != "complete":
        raise ValueError("only complete node/flow commits can enter process lineage")
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str) or metadata.get("updated") != timestamp:
        raise ValueError("node and flow timestamps do not match")
    core = {
        "node": {"path": node_relative, "sha256": _digest(node_bytes)},
        "flow": {
            "path": flow_relative,
            "line": flow_line,
            "sha256": _digest(line_bytes),
        },
        "timestamp": timestamp,
    }
    return {**core, "digest": _digest(_canonical_json(core))}


def _binding_identity(binding):
    if not isinstance(binding, dict):
        raise ValueError("commit binding must be an object")
    if set(binding) != {"node", "flow", "timestamp", "digest"}:
        raise ValueError("commit binding has missing or unknown fields")
    node = binding.get("node")
    flow = binding.get("flow")
    if not isinstance(node, dict) or set(node) != {"path", "sha256"}:
        raise ValueError("commit node binding is invalid")
    if not isinstance(flow, dict) or set(flow) != {"path", "line", "sha256"}:
        raise ValueError("commit flow binding is invalid")
    for name, value in (("node path", node["path"]), ("flow path", flow["path"])):
        if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
            raise ValueError(f"commit {name} must be a non-empty single-line string")
    if type(flow["line"]) is not int or flow["line"] < 1:
        raise ValueError("commit flow line must be a positive integer")
    for value in (node["sha256"], flow["sha256"], binding["digest"]):
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise ValueError("commit binding contains an invalid SHA-256 digest")
    if not isinstance(binding["timestamp"], str):
        raise ValueError("commit timestamp must be a string")
    return node["path"], flow["path"], flow["line"]


def _current_binding(root, node, binding):
    node_path, flow_path, flow_line = _binding_identity(binding)
    current = build_commit_binding(
        root, node["node_id"], node["role"], node_path, flow_path, flow_line
    )
    if current["node"]["sha256"] != binding["node"]["sha256"]:
        raise ValueError(f"node hash is stale or tampered: {node['node_id']}")
    if current["flow"]["sha256"] != binding["flow"]["sha256"]:
        raise ValueError(f"flow hash is stale or tampered: {node['node_id']}")
    if current != binding:
        raise ValueError(f"commit binding is stale or tampered: {node['node_id']}")
    return current


def _find_token(manifest, token):
    matches = [node for node in manifest["nodes"].values() if node.get("reservation_token") == token]
    if len(matches) != 1:
        raise ValueError("unknown or ambiguous reservation token")
    return matches[0]


def finalize(run_id, reservation_token, binding, *, root=Path(".")):
    run_id = _safe_id(run_id, "run id")
    _safe_id(reservation_token, "reservation token")
    with _run_lock(root, run_id):
        manifest = load_manifest(run_id, root=root)
        _require_manifest_shape(manifest, run_id)
        _assert_open(manifest)
        node = _find_token(manifest, reservation_token)
        current = _current_binding(root, node, binding)
        if current["timestamp"] and manifest["goal"] != _node_goal(root, current):
            raise ValueError("node/flow commit goal does not match process run goal")
        if node["status"] == "committed":
            if node["commit"] != current:
                raise ValueError(f"conflicting finalization for node: {node['node_id']}")
            return {
                "run_id": run_id,
                "node_id": node["node_id"],
                "status": "committed",
                "commit": current,
                "idempotent": True,
            }
        if node["status"] != "pending":
            raise ValueError(f"cannot finalize {node['status']} reservation: {node['node_id']}")
        now = _timestamp()
        node["status"] = "committed"
        node["commit"] = current
        node["finalized_at"] = now
        for role_state in manifest["roles"]:
            if role_state["role"] == node["role"]:
                role_state["state"] = "performed"
                break
        manifest["updated_at"] = now
        manifest["diagnostics"] = []
        _atomic_write_json(_manifest_path(root, run_id), manifest, root=root)
    return {
        "run_id": run_id,
        "node_id": node["node_id"],
        "status": "committed",
        "commit": current,
        "idempotent": False,
    }


def _node_goal(root, binding):
    node_path = _root(root) / binding["node"]["path"]
    return _node_metadata(node_path.read_bytes(), binding["node"]["path"]).get("goal")


def finalize_paths(
    run_id,
    reservation_token,
    node_path,
    flow_path,
    flow_line,
    *,
    root=Path("."),
):
    with _run_lock(root, run_id):
        manifest = load_manifest(run_id, root=root)
        _require_manifest_shape(manifest, run_id)
        _assert_open(manifest)
        node = _find_token(manifest, reservation_token)
        binding = build_commit_binding(
            root, node["node_id"], node["role"], node_path, flow_path, flow_line
        )
    return finalize(run_id, reservation_token, binding, root=root)


def _retry_flow_projection(root, binding):
    flow_file, flow_relative = _relative_artifact(
        root, binding["flow"]["path"], "flows", ".jsonl"
    )
    flow_lines = flow_file.read_bytes().splitlines()
    flow_line = binding["flow"]["line"]
    if flow_line > len(flow_lines):
        raise ValueError(f"flow line does not exist: {flow_relative}:{flow_line}")
    try:
        record = json.loads(
            flow_lines[flow_line - 1].decode("utf-8"),
            object_pairs_hook=_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"invalid flow commit: {flow_relative}:{flow_line}: {error}"
        ) from error

    return {key: value for key, value in record.items() if key != "timestamp"}


def _validate_retry_projection_shape(projection):
    expected_fields = FLOW_V2_FIELDS - {"timestamp"}
    if not isinstance(projection, dict):
        raise ValueError("retry projection has missing or unknown fields")
    present = set(projection)
    # Optional fields may be present or absent; everything else is exact.
    if present - FLOW_V2_OPTIONAL_FIELDS != expected_fields:
        raise ValueError("retry projection has missing or unknown fields")


def _without_empty_optional(projection):
    """An absent optional field and an empty one mean the same thing."""
    return {
        key: value
        for key, value in projection.items()
        if key not in FLOW_V2_OPTIONAL_FIELDS or value != ""
    }


def _validate_retry_projection_locked(root, node, projection):
    current = _current_binding(root, node, node["commit"])
    committed_projection = _retry_flow_projection(root, current)
    if _canonical_json(_without_empty_optional(projection)) != _canonical_json(
        _without_empty_optional(committed_projection)
    ):
        raise ValueError(f"conflicting committed retry for node: {node['node_id']}")
    return current


def abort(run_id, reservation_token, reason, *, root=Path(".")):
    run_id = _safe_id(run_id, "run id")
    _safe_id(reservation_token, "reservation token")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("abort reason must be a non-empty string")
    with _run_lock(root, run_id):
        manifest = load_manifest(run_id, root=root)
        _require_manifest_shape(manifest, run_id)
        _assert_open(manifest)
        node = _find_token(manifest, reservation_token)
        if node["status"] == "aborted":
            if node["abort_reason"] != reason:
                raise ValueError(f"conflicting abort for node: {node['node_id']}")
            return {
                "run_id": run_id,
                "node_id": node["node_id"],
                "status": "aborted",
                "idempotent": True,
            }
        if node["status"] != "pending":
            raise ValueError(f"cannot abort {node['status']} reservation: {node['node_id']}")
        now = _timestamp()
        node["status"] = "aborted"
        node["aborted_at"] = now
        node["abort_reason"] = reason
        manifest["updated_at"] = now
        manifest["diagnostics"] = []
        _atomic_write_json(_manifest_path(root, run_id), manifest, root=root)
    return {
        "run_id": run_id,
        "node_id": node["node_id"],
        "status": "aborted",
        "idempotent": False,
    }


def _diagnostic(code, message, node_id=None):
    result = {"code": code, "message": message}
    if node_id is not None:
        result["node_id"] = node_id
    return result


def _valid_manifest_timestamp(value):
    if not isinstance(value, str) or not MANIFEST_TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f0Z")
        return True
    except ValueError:
        return False


def _shape_diagnostics(manifest, expected_run_id):
    diagnostics = []
    if not isinstance(manifest, dict):
        return [_diagnostic("manifest-schema", "process manifest must be an object")]
    actual_fields = set(manifest)
    if actual_fields != TOP_LEVEL_FIELDS:
        missing = sorted(TOP_LEVEL_FIELDS - actual_fields)
        unknown = sorted(actual_fields - TOP_LEVEL_FIELDS)
        diagnostics.append(
            _diagnostic(
                "manifest-schema",
                f"manifest fields mismatch; missing={missing}; unknown={unknown}",
            )
        )
    if type(manifest.get("schema")) is not int or manifest.get("schema") != SCHEMA:
        diagnostics.append(_diagnostic("schema", f"unsupported process schema: {manifest.get('schema')}"))
    actual_run_id = manifest.get("run_id")
    try:
        _safe_id(actual_run_id, "manifest run id")
    except ValueError as error:
        diagnostics.append(_diagnostic("run-id", str(error)))
    if actual_run_id != expected_run_id:
        diagnostics.append(
            _diagnostic(
                "run-id",
                f"manifest run id {actual_run_id!r} does not match requested run {expected_run_id!r}",
            )
        )
    goal = manifest.get("goal")
    if not isinstance(goal, str) or not goal.strip() or "\n" in goal or "\r" in goal:
        diagnostics.append(_diagnostic("goal", "manifest goal must be a non-empty single line"))
    state = manifest.get("state")
    if state not in {"open", "sealed", "invalid"}:
        diagnostics.append(_diagnostic("manifest-state", f"invalid process run state: {state}"))
    for field in ("created_at", "updated_at"):
        if not _valid_manifest_timestamp(manifest.get(field)):
            diagnostics.append(_diagnostic("timestamp", f"invalid manifest timestamp: {field}"))
    created_at = manifest.get("created_at")
    updated_at = manifest.get("updated_at")
    if _valid_manifest_timestamp(created_at) and _valid_manifest_timestamp(updated_at) and created_at > updated_at:
        diagnostics.append(_diagnostic("timestamp", "manifest updated_at precedes created_at"))
    sealed_at = manifest.get("sealed_at")
    if state == "sealed":
        if not _valid_manifest_timestamp(sealed_at):
            diagnostics.append(_diagnostic("status-audit", "sealed run requires sealed_at timestamp"))
    elif sealed_at is not None:
        diagnostics.append(_diagnostic("status-audit", "unsealed run must have null sealed_at"))
    raw_topology = manifest.get("topology")
    try:
        if raw_topology is None:
            raise ValueError("manifest topology is missing")
        _normalize_topology(raw_topology)
    except ValueError as error:
        diagnostics.append(_diagnostic("topology", str(error)))
    roles = manifest.get("roles")
    if not (
        isinstance(roles, list)
        and len(roles) == len(ROLES)
        and all(
            isinstance(item, dict)
            and set(item) == {"role", "state"}
            and item.get("role") == ROLES[index]
            and item.get("state") in {"pending", "performed", "skipped"}
            for index, item in enumerate(roles)
        )
    ):
        diagnostics.append(
            _diagnostic("role-states", "manifest roles must exactly describe every ordered role state")
        )
    stored_diagnostics = manifest.get("diagnostics")
    if not isinstance(stored_diagnostics, list):
        diagnostics.append(_diagnostic("diagnostics-schema", "manifest diagnostics must be an array"))
    else:
        for item in stored_diagnostics:
            if not isinstance(item, dict) or set(item) not in (
                {"code", "message"}, {"code", "message", "node_id"}
            ) or not isinstance(item.get("code"), str) or not isinstance(item.get("message"), str):
                diagnostics.append(_diagnostic("diagnostics-schema", "invalid stored diagnostic entry"))
                break
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        diagnostics.append(_diagnostic("nodes", "manifest nodes must be an object"))
        return diagnostics
    tokens = set()
    sequences = set()
    for key, node in nodes.items():
        try:
            _safe_id(key, "manifest node key")
        except ValueError as error:
            diagnostics.append(_diagnostic("node-schema", str(error), str(key)))
        if not isinstance(node, dict):
            diagnostics.append(_diagnostic("node-schema", f"node entry is not an object: {key}", str(key)))
            continue
        if set(node) != NODE_FIELDS:
            diagnostics.append(
                _diagnostic(
                    "node-schema",
                    f"node fields mismatch for {key}; missing={sorted(NODE_FIELDS - set(node))}; unknown={sorted(set(node) - NODE_FIELDS)}",
                    str(key),
                )
            )
        if node.get("node_id") != key:
            diagnostics.append(_diagnostic("node-schema", f"node id does not match key: {key}", str(key)))
        try:
            _safe_id(node.get("node_id"), "node id")
        except ValueError as error:
            diagnostics.append(_diagnostic("node-schema", str(error), str(key)))
        if node.get("role") not in ROLES:
            diagnostics.append(_diagnostic("node-schema", f"invalid role for {key}: {node.get('role')}", str(key)))
        inputs = node.get("process_inputs")
        valid_inputs = isinstance(inputs, list) and all(isinstance(value, str) for value in inputs)
        if valid_inputs:
            valid_inputs = len(inputs) == len(set(inputs)) and node.get("node_id") not in inputs
            for value in inputs:
                try:
                    _safe_id(value, "process input node id")
                except ValueError:
                    valid_inputs = False
        if not valid_inputs:
            diagnostics.append(_diagnostic("process-inputs", f"invalid, duplicate, or self process inputs: {key}", str(key)))
        sequence = node.get("sequence")
        if type(sequence) is not int or sequence < 1 or sequence in sequences:
            diagnostics.append(_diagnostic("sequence", f"invalid or duplicate sequence: {key}", str(key)))
        else:
            sequences.add(sequence)
        token = node.get("reservation_token")
        try:
            _safe_id(token, "reservation token")
            if token in tokens:
                raise ValueError("duplicate reservation token")
            tokens.add(token)
        except ValueError as error:
            diagnostics.append(_diagnostic("reservation-token", str(error), str(key)))
        reserved_at = node.get("reserved_at")
        if not _valid_manifest_timestamp(reserved_at):
            diagnostics.append(_diagnostic("timestamp", f"invalid reserved_at: {key}", str(key)))
        status = node.get("status")
        if status == "pending":
            consistent = all(node.get(field) is None for field in ("finalized_at", "aborted_at", "abort_reason", "commit"))
        elif status == "committed":
            consistent = (
                _valid_manifest_timestamp(node.get("finalized_at"))
                and node.get("aborted_at") is None
                and node.get("abort_reason") is None
                and isinstance(node.get("commit"), dict)
            )
        elif status == "aborted":
            consistent = (
                node.get("finalized_at") is None
                and node.get("commit") is None
                and _valid_manifest_timestamp(node.get("aborted_at"))
                and isinstance(node.get("abort_reason"), str)
                and bool(node.get("abort_reason").strip())
            )
        else:
            consistent = False
        if not consistent:
            diagnostics.append(_diagnostic("status-audit", f"inconsistent {status!r} audit fields: {key}", str(key)))
        for audit_field in ("finalized_at", "aborted_at"):
            value = node.get(audit_field)
            if value is not None and _valid_manifest_timestamp(reserved_at) and _valid_manifest_timestamp(value) and value < reserved_at:
                diagnostics.append(_diagnostic("timestamp", f"{audit_field} precedes reservation: {key}", str(key)))
        if status == "committed" and isinstance(node.get("commit"), dict):
            try:
                _binding_identity(node["commit"])
            except ValueError as error:
                diagnostics.append(_diagnostic("commit-schema", str(error), str(key)))
    return diagnostics


def _require_manifest_shape(manifest, expected_run_id):
    diagnostics = _shape_diagnostics(manifest, expected_run_id)
    if diagnostics:
        raise ValidationError(diagnostics)


def _reachable(start, adjacency):
    seen = set()
    stack = list(start)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, ()))
    return seen


def _has_cycle(nodes, adjacency):
    colors = {}
    for start in nodes:
        if colors.get(start, 0) != 0:
            continue
        colors[start] = 1
        stack = [
            (
                start,
                iter(parent for parent in adjacency.get(start, ()) if parent in nodes),
            )
        ]
        while stack:
            node_id, adjacent = stack[-1]
            try:
                parent_id = next(adjacent)
            except StopIteration:
                colors[node_id] = 2
                stack.pop()
                continue
            parent_color = colors.get(parent_id, 0)
            if parent_color == 1:
                return True
            if parent_color == 0:
                colors[parent_id] = 1
                stack.append(
                    (
                        parent_id,
                        iter(
                            parent
                            for parent in adjacency.get(parent_id, ())
                            if parent in nodes
                        ),
                    )
                )
    return False


def _validation_report(manifest, root, expected_run_id, *, require_sealed=False):
    diagnostics = _shape_diagnostics(manifest, expected_run_id)
    if manifest.get("state") == "invalid":
        diagnostics.append(_diagnostic("manifest-state", "process run is explicitly invalid"))
    if require_sealed and manifest.get("state") != "sealed":
        diagnostics.append(_diagnostic("unsealed", "process run is not sealed"))
    goal = manifest.get("goal")
    if not isinstance(goal, str) or not goal.strip() or "\n" in goal or "\r" in goal:
        diagnostics.append(_diagnostic("goal", "manifest goal must be a non-empty single line"))
    raw_topology = manifest.get("topology")
    try:
        if raw_topology is None:
            raise ValueError("manifest topology is missing")
        topology = _normalize_topology(raw_topology)
    except ValueError as error:
        diagnostics.append(_diagnostic("topology", str(error)))
        topology = [{"role": role, "state": "required"} for role in ROLES]
    topology_by_role = {item["role"]: item for item in topology}
    role_states = manifest.get("roles")
    roles_valid = (
        isinstance(role_states, list)
        and len(role_states) == len(ROLES)
        and all(
            isinstance(item, dict)
            and set(item) == {"role", "state"}
            and item.get("role") == ROLES[index]
            and item.get("state") in {"pending", "performed", "skipped"}
            for index, item in enumerate(role_states)
        )
    )
    if not roles_valid:
        diagnostics.append(
            _diagnostic(
                "role-states",
                "manifest roles must be an ordered pending|performed|skipped state for every role",
            )
        )
        role_states = [
            {
                "role": role,
                "state": "skipped"
                if topology_by_role[role]["state"] == "skipped"
                else "pending",
            }
            for role in ROLES
        ]
    raw_nodes = manifest.get("nodes")
    if not isinstance(raw_nodes, dict):
        diagnostics.append(_diagnostic("nodes", "manifest nodes must be an object"))
        raw_nodes = {}
    committed = {}
    sequences = set()
    for key, node in raw_nodes.items():
        if not isinstance(node, dict) or node.get("node_id") != key or node.get("role") not in ROLES:
            diagnostics.append(_diagnostic("node-shape", f"invalid process node entry: {key}"))
            continue
        sequence = node.get("sequence")
        if type(sequence) is not int or sequence < 1 or sequence in sequences:
            diagnostics.append(
                _diagnostic("sequence", f"invalid or duplicate process sequence: {key}", key)
            )
        else:
            sequences.add(sequence)
        status = node.get("status")
        if status == "pending":
            diagnostics.append(_diagnostic("pending", f"pending reservation blocks seal: {key}", key))
        elif status == "committed":
            committed[key] = node
            try:
                current = _current_binding(root, node, node.get("commit"))
                if _node_goal(root, current) != manifest.get("goal"):
                    raise ValueError("node/flow commit goal does not match process run goal")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                diagnostics.append(_diagnostic("stale-commit", str(error), key))
        elif status != "aborted":
            diagnostics.append(_diagnostic("node-status", f"invalid reservation status for {key}: {status}", key))
    by_role = {role: [] for role in ROLES}
    for node in committed.values():
        by_role[node["role"]].append(node["node_id"])
    for role in ROLES:
        state = topology_by_role[role]["state"]
        if state == "required" and not by_role[role]:
            diagnostics.append(_diagnostic("missing-role", f"required role has no committed node: {role}"))
        if state == "skipped" and by_role[role]:
            diagnostics.append(_diagnostic("skipped-role-node", f"skipped role has committed nodes: {role}"))
    for item in role_states:
        role = item["role"]
        expected = (
            "skipped"
            if topology_by_role[role]["state"] == "skipped"
            else "performed"
            if by_role[role]
            else "pending"
        )
        if item["state"] != expected:
            diagnostics.append(
                _diagnostic(
                    "role-state-mismatch",
                    f"role state for {role} is {item['state']}; expected {expected}",
                )
            )
        if manifest.get("state") == "sealed" and item["state"] == "pending":
            diagnostics.append(
                _diagnostic("sealed-pending-role", f"sealed run has pending role: {role}")
            )
    for role in MANDATORY_ROLES:
        if not by_role[role]:
            diagnostics.append(_diagnostic("mandatory-role", f"mandatory role was not performed: {role}"))
    if len(by_role["stem"]) != 1:
        diagnostics.append(
            _diagnostic("stem-count", f"exactly one STEM is required; found {len(by_role['stem'])}")
        )
    parents = {}
    children = {node_id: [] for node_id in committed}
    for node_id, node in committed.items():
        inputs = node.get("process_inputs")
        if not isinstance(inputs, list) or any(not isinstance(value, str) for value in inputs):
            diagnostics.append(_diagnostic("process-inputs", f"invalid process inputs: {node_id}", node_id))
            inputs = []
        parents[node_id] = list(inputs)
        if node["role"] == "apex" and inputs:
            diagnostics.append(_diagnostic("apex-input", f"APEX node has process inputs: {node_id}", node_id))
        if node["role"] != "apex" and not inputs:
            diagnostics.append(_diagnostic("missing-input", f"non-APEX node has no process inputs: {node_id}", node_id))
        for parent_id in inputs:
            parent = committed.get(parent_id)
            if parent is None:
                diagnostics.append(
                    _diagnostic("cross-run-input", f"process input is not committed in this run: {parent_id}", node_id)
                )
                continue
            children[parent_id].append(node_id)
            try:
                _validate_transition({**manifest, "topology": topology}, parent, node["role"])
            except ValueError as error:
                diagnostics.append(_diagnostic("invalid-edge", str(error), node_id))
    if _has_cycle(committed, parents):
        diagnostics.append(_diagnostic("cycle", "process lineage contains a cycle"))
    if len(by_role["stem"]) == 1:
        stem_id = by_role["stem"][0]
        ancestors = _reachable(parents.get(stem_id, ()), parents)
        pre_stem = {
            node_id
            for node_id, node in committed.items()
            if ROLE_RANK[node["role"]] < ROLE_RANK["stem"]
        }
        disconnected = sorted(pre_stem - ancestors)
        for node_id in disconnected:
            diagnostics.append(
                _diagnostic("disconnected-pre-stem", f"disconnected pre-STEM branch: {node_id}", node_id)
            )
        apex_ids = set(by_role["apex"])
        for node_id in sorted(pre_stem):
            ancestry = _reachable([node_id], parents)
            if not ancestry.intersection(apex_ids):
                diagnostics.append(
                    _diagnostic("no-apex-ancestor", f"pre-STEM node is not downstream from APEX: {node_id}", node_id)
                )
        descendants = _reachable(children.get(stem_id, ()), children)
        for cap_id in by_role["cap"]:
            if cap_id not in descendants:
                diagnostics.append(
                    _diagnostic("cap-not-downstream", f"CAP is not downstream from the sole STEM: {cap_id}", cap_id)
                )
    report_state = manifest.get("state", "invalid") if not diagnostics else "invalid"
    return {
        "run_id": manifest.get("run_id"),
        "manifest_state": manifest.get("state"),
        "state": report_state,
        "valid": not diagnostics,
        "diagnostics": diagnostics,
        "committed_nodes": len(committed),
    }


def validate_manifest(manifest, run_id, *, root=Path("."), require_sealed=True):
    """Apply the canonical read-only schema and topology validation to one manifest."""
    run_id = _safe_id(run_id, "run id")
    return _validation_report(manifest, root, run_id, require_sealed=require_sealed)


def inspect_manifest(run_id, *, root=Path("."), require_sealed=True):
    """Best-effort diagnostic read of one manifest without creating lock state."""
    run_id = _safe_id(run_id, "run id")
    manifest = load_manifest(run_id, root=root)
    report = validate_manifest(
        manifest,
        run_id,
        root=root,
        require_sealed=require_sealed,
    )
    return {"manifest": deepcopy(manifest), "report": report}


def verify_snapshot(run_id, *, root=Path(".")):
    """Return one sealed-run validation snapshot while holding the run lock."""
    run_id = _safe_id(run_id, "run id")
    with _run_lock(root, run_id, shared=True):
        manifest = load_manifest(run_id, root=root)
        report = validate_manifest(
            manifest,
            run_id,
            root=root,
            require_sealed=True,
        )
        return {"manifest": deepcopy(manifest), "report": report}


def verify(run_id, *, root=Path(".")):
    return verify_snapshot(run_id, root=root)["report"]


def seal(run_id, *, root=Path(".")):
    run_id = _safe_id(run_id, "run id")
    with _run_lock(root, run_id):
        manifest = load_manifest(run_id, root=root)
        if manifest.get("state") not in ("open", "sealed"):
            raise ValueError(f"process run is {manifest.get('state')}; sealing is not allowed")
        report = validate_manifest(manifest, run_id, root=root, require_sealed=False)
        if not report["valid"]:
            raise ValidationError(report["diagnostics"])
        if manifest["state"] == "sealed":
            return {**report, "state": "sealed", "idempotent": True}
        now = _timestamp()
        manifest["state"] = "sealed"
        manifest["sealed_at"] = now
        manifest["updated_at"] = now
        manifest["diagnostics"] = []
        _atomic_write_json(_manifest_path(root, run_id), manifest, root=root)
        return {**report, "manifest_state": "sealed", "state": "sealed", "idempotent": False}


def _load_topology_argument(args):
    if args.topology_json is not None:
        return json.loads(args.topology_json, object_pairs_hook=_json_object)
    if args.topology_file is not None:
        return json.loads(Path(args.topology_file).read_text(encoding="utf-8"), object_pairs_hook=_json_object)
    if args.topology_preset is not None:
        return args.topology_preset
    return None


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(f"argument error: {message}")


def _parser():
    parser = _JsonArgumentParser(description="Manage explicit Mycelium process lineage")
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")

    begin_parser = commands.add_parser("begin")
    begin_parser.add_argument("--goal", required=True)
    begin_parser.add_argument("--run-id")
    topology = begin_parser.add_mutually_exclusive_group()
    topology.add_argument("--topology-json")
    topology.add_argument("--topology-file")
    topology.add_argument("--topology-preset")

    reserve_parser = commands.add_parser("reserve")
    reserve_parser.add_argument("--run-id", required=True)
    reserve_parser.add_argument("--node-id", required=True)
    reserve_parser.add_argument("--role", required=True, choices=ROLES)
    reserve_parser.add_argument("--process-input", action="append", default=[])
    reserve_parser.add_argument("--projection-json")

    finalize_parser = commands.add_parser("finalize")
    finalize_parser.add_argument("--run-id", required=True)
    finalize_parser.add_argument("--reservation-token", required=True)
    finalize_parser.add_argument("--node-path", required=True)
    finalize_parser.add_argument("--flow-path", required=True)
    finalize_parser.add_argument("--flow-line", required=True, type=int)

    abort_parser = commands.add_parser("abort")
    abort_parser.add_argument("--run-id", required=True)
    abort_parser.add_argument("--reservation-token", required=True)
    abort_parser.add_argument("--reason", required=True)

    for name in ("seal", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
    return parser


def _run_command(args):
    root = Path(args.root)
    if args.command == "preflight":
        result = preflight(root=root)
        return result, 0 if result["ok"] else 1
    if args.command == "begin":
        return begin(
            args.goal,
            _load_topology_argument(args),
            run_id=args.run_id,
            root=root,
        ), 0
    if args.command == "reserve":
        projection = None
        if args.projection_json is not None:
            projection = json.loads(
                args.projection_json, object_pairs_hook=_json_object
            )
        return reserve(
            args.run_id,
            args.node_id,
            args.role,
            args.process_input,
            projection,
            root=root,
        ), 0
    if args.command == "finalize":
        return finalize_paths(
            args.run_id,
            args.reservation_token,
            args.node_path,
            args.flow_path,
            args.flow_line,
            root=root,
        ), 0
    if args.command == "abort":
        return abort(
            args.run_id, args.reservation_token, args.reason, root=root
        ), 0
    if args.command == "seal":
        return seal(args.run_id, root=root), 0
    if args.command == "verify":
        result = verify(args.run_id, root=root)
        return result, 0 if result["valid"] else 1
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        result, status = _run_command(args)
        print(_canonical_json(result))
        return status
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {"error": str(error), "type": type(error).__name__}
        if isinstance(error, ValidationError):
            result["diagnostics"] = error.diagnostics
        print(_canonical_json(result), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
