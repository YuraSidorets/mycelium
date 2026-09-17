#!/usr/bin/env python3
"""Execute one frozen three-arm pilot through a pinned external adapter."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

try:
    from . import pilot, preflight
except ImportError:  # Direct script execution puts this directory on sys.path.
    import pilot
    import preflight


RESULT_SCHEMA = "mycelium.swebench-live-pilot-results/v3"
CANARY_RESULT_SCHEMA = "mycelium.swebench-live-model-canary-results/v2"
REQUEST_SCHEMA = "mycelium.swebench-live-run-request/v4"
PREFLIGHT_REQUEST_SCHEMA = "mycelium.swebench-live-preflight-request/v1"
PREFLIGHT_RESPONSE_SCHEMA = "mycelium.swebench-live-preflight-response/v1"
TRACE_HEADER_SCHEMA = "mycelium.swebench-live-trace-header/v1"
TRACE_RUNTIME_SCHEMA = "mycelium.codex-pier-runtime/v1"
TRACE_USAGE_SCHEMA = "mycelium.swebench-live-provider-usage/v2"
TRACE_GRADER_SCHEMA = "mycelium.swebench-live-grader-receipt/v1"
TRACE_EVENT_SCHEMA = "mycelium.swebench-live-trajectory-event/v1"
TRACE_COMPLETE_SCHEMA = "mycelium.swebench-live-trace-complete/v1"
GRADER_REPORT_SCHEMA = "mycelium.swebench-live-normalized-grader-report/v2"
PREREGISTRATION_SCHEMA = "mycelium.swebench-live-preregistration/v2"
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
TASK_ROW_FIELDS = {"instance_id", "repo", "base_commit", "problem_statement"}
STOP_RULES = (
    "BASELINE_FLOOR",
    "BASELINE_CEILING",
    "TASK_FLOOR",
    "TASK_CEILING",
    "NO_FULL_BASELINE_DISCORDANCE",
    "NO_OBSERVED_ORCHESTRATION_VALUE",
    "FULL_COSTLIER_WITHOUT_SUCCESS_GAIN",
)
COUNTER_FIELDS = {
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "steps",
    "tool_calls",
    "spawn_count",
    "peak_parallel_agents",
}


class PilotRunError(ValueError):
    def __init__(self, message, *, process_evidence=None):
        super().__init__(message)
        self.process_evidence = process_evidence


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PilotRunError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _file_sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise PilotRunError(f"cannot hash file {path}: {exc}") from exc


def _stream_evidence(value):
    if value is None:
        data = b""
    elif isinstance(value, bytes):
        data = value
    else:
        data = value.encode("utf-8", errors="replace")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _process_evidence(stdout, stderr):
    return {"stdout": _stream_evidence(stdout), "stderr": _stream_evidence(stderr)}


def _document_sha256(document):
    unsigned = dict(document)
    unsigned.pop("result_sha256", None)
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preregistration_identity(uri, digest):
    if not isinstance(uri, str) or any(character.isspace() for character in uri):
        raise PilotRunError("preregistration URI is invalid")
    parsed = urlparse(uri)
    if not parsed.scheme or not (parsed.netloc or parsed.path):
        raise PilotRunError("preregistration URI must be absolute")
    if not isinstance(digest, str) or not HEX_SHA256.fullmatch(digest):
        raise PilotRunError("preregistration digest must be 64 lowercase hex characters")
    return {"uri": uri, "sha256": digest}


def _timestamp(value, field):
    if not isinstance(value, str):
        raise PilotRunError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PilotRunError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise PilotRunError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _string_list(value, field, *, allow_empty=True):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise PilotRunError(f"{field} must be a list of non-empty strings")
    if len(value) != len(set(value)) or (not allow_empty and not value):
        raise PilotRunError(f"{field} must be non-empty and unique" if not allow_empty else f"{field} must be unique")
    return value


def _validate_patch_paths(paths):
    values = _string_list(paths, "patch_files")
    if values != sorted(values):
        raise PilotRunError("patch_files must be in canonical sorted order")
    for value in values:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise PilotRunError(f"patch file path is not canonical: {value}")


def _validate_response(response, expected, manifest, preregistration_document):
    if not isinstance(response, dict) or set(response) != set(pilot.TELEMETRY_FIELDS):
        raise PilotRunError(f"adapter response fields must be exactly {sorted(pilot.TELEMETRY_FIELDS)}")

    for field in ("run_id", "instance_id", "arm", "phase", "requested_model", "replicate", "arm_order"):
        if response[field] != expected[field]:
            raise PilotRunError(f"adapter response {field} does not match the frozen run")
    if not isinstance(response["observed_model"], str) or response["observed_model"] != expected["requested_model"]:
        raise PilotRunError("observed model does not match the requested model slug")
    if response["observed_provider"] != expected["model_provider"]:
        raise PilotRunError("observed provider does not match the frozen model provider")
    if response["observed_reasoning_effort"] != expected["reasoning_effort"]:
        raise PilotRunError("observed reasoning effort does not match the frozen model contract")
    if not isinstance(response["session_id"], str) or not response["session_id"]:
        raise PilotRunError("session_id must be a non-empty string")

    for field in COUNTER_FIELDS:
        value = response[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PilotRunError(f"{field} must be a non-negative integer")
    if response["steps"] < 1:
        raise PilotRunError("steps (completed turns) must be positive for a complete model trace")
    if response["tool_calls"] > manifest["cost_caps"]["max_tool_calls_per_run"]:
        raise PilotRunError("run exceeds the frozen publication tool-call limit")
    if response["cached_input_tokens"] > response["input_tokens"]:
        raise PilotRunError("cached input tokens exceed input tokens")
    if response["reasoning_tokens"] > response["output_tokens"]:
        raise PilotRunError("reasoning tokens exceed output tokens")
    token_total = response["input_tokens"] + response["output_tokens"]
    if token_total > manifest["cost_caps"]["max_tokens_per_run"]:
        raise PilotRunError("run exceeds the frozen publication token limit")

    cost = response["cost_usd"]
    try:
        parsed_cost = Decimal(cost) if isinstance(cost, str) and DECIMAL.fullmatch(cost) else None
    except InvalidOperation:
        parsed_cost = None
    if parsed_cost is None or parsed_cost > Decimal(manifest["cost_caps"]["max_usd_per_run"]):
        raise PilotRunError("notional API-equivalent cost is invalid or exceeds the frozen publication limit")
    if parsed_cost != _derived_cost(response, preregistration_document):
        raise PilotRunError("notional API-equivalent cost does not match preregistered prices and retained usage")
    wall_time = response["wall_time_seconds"]
    if isinstance(wall_time, bool) or not isinstance(wall_time, (int, float)) or not math.isfinite(wall_time) or wall_time < 0:
        raise PilotRunError("wall_time_seconds must be finite and non-negative")
    if wall_time > manifest["cost_caps"]["max_wall_time_seconds"]:
        raise PilotRunError("run exceeded the frozen wall-time cap")

    started = _timestamp(response["started_at"], "started_at")
    ended = _timestamp(response["ended_at"], "ended_at")
    if ended < started:
        raise PilotRunError("ended_at precedes started_at")
    for field in ("patch_sha256", "grader_report_sha256", "trace_sha256"):
        if not isinstance(response[field], str) or not HEX_SHA256.fullmatch(response[field]):
            raise PilotRunError(f"{field} must be a SHA-256 digest")
    _validate_patch_paths(response["patch_files"])
    _string_list(response["durable_node_ids"], "durable_node_ids")
    if not isinstance(response["fresh_retrieval"], bool) or not isinstance(response["grader_resolved"], bool):
        raise PilotRunError("fresh_retrieval and grader_resolved must be booleans")
    if response["arm"] == "full":
        if not isinstance(response["parent_session_id"], str) or not response["parent_session_id"]:
            raise PilotRunError("full arm requires a parent session")
        if response["parent_session_id"] == response["session_id"]:
            raise PilotRunError("full arm parent and child sessions must differ")
        if response["spawn_count"] < 1 or response["peak_parallel_agents"] < 2:
            raise PilotRunError("full arm requires real spawned-agent telemetry")
        _string_list(response["durable_node_ids"], "durable_node_ids", allow_empty=False)
        if response["fresh_retrieval"] is not True:
            raise PilotRunError("full arm requires fresh retrieval")
        if not isinstance(response["stem_ledger_sha256"], str) or not HEX_SHA256.fullmatch(response["stem_ledger_sha256"]):
            raise PilotRunError("full arm requires a STEM ledger digest")
        if response["cap_verification"] != "PASS":
            raise PilotRunError("full arm requires externally verified CAP")
    else:
        if response["parent_session_id"] is not None or response["spawn_count"] != 0:
            raise PilotRunError("non-full arm must not report spawned sessions")
        if response["peak_parallel_agents"] != 1:
            raise PilotRunError("non-full arm must remain single-agent")
        if response["durable_node_ids"] or response["fresh_retrieval"]:
            raise PilotRunError("non-full arm must not report full-workflow provenance")
        if response["stem_ledger_sha256"] is not None or response["cap_verification"] != "NOT_APPLICABLE":
            raise PilotRunError("non-full arm must not report STEM or CAP provenance")
    return response


def _validate_grader_report(path, digest, response, task, manifest):
    report_path = Path(path)
    if report_path.is_symlink() or not report_path.is_file() or _file_sha256(report_path) != digest:
        raise PilotRunError(f"retained grader report is missing, unsafe, or changed: {report_path}")
    report = _load_document(report_path, "normalized grader report")
    expected = {
        "schema": GRADER_REPORT_SCHEMA,
        "run_id": response["run_id"],
        "instance_id": response["instance_id"],
        "evaluator": manifest["evaluator"],
        "image_digest": task["image_digest"],
        "hidden_row_sha256": task["hidden_row_sha256"],
        "resolved": response["grader_resolved"],
    }
    if set(report) != set(expected) | {"native_evidence"} \
            or any(report.get(field) != value for field, value in expected.items()):
        raise PilotRunError(f"retained grader report does not match frozen identities or telemetry: {report_path}")
    receipts = report["native_evidence"]
    if not isinstance(receipts, list) or not receipts:
        raise PilotRunError(f"retained grader report has no native evaluator evidence: {report_path}")
    by_source = {}
    names = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {"source_path", "path", "sha256"} \
                or not isinstance(receipt["source_path"], str) \
                or PurePosixPath(receipt["source_path"]).is_absolute() \
                or ".." in PurePosixPath(receipt["source_path"]).parts \
                or not isinstance(receipt["path"], str) \
                or PurePosixPath(receipt["path"]).name != receipt["path"] \
                or not isinstance(receipt["sha256"], str) \
                or not HEX_SHA256.fullmatch(receipt["sha256"]):
            raise PilotRunError(f"native evaluator evidence receipt is invalid: {report_path}")
        if receipt["source_path"] in by_source or receipt["path"] in names:
            raise PilotRunError(f"native evaluator evidence receipt is duplicated: {report_path}")
        retained = report_path.parent / receipt["path"]
        if retained.is_symlink() or not retained.is_file() \
                or _file_sha256(retained) != receipt["sha256"]:
            raise PilotRunError(f"native evaluator evidence is missing or changed: {retained}")
        by_source[receipt["source_path"]] = retained
        names.add(receipt["path"])
    required = {"process/stdout", "process/stderr", "results.json"}
    if not required.issubset(by_source):
        raise PilotRunError(f"native evaluator stdout, stderr, or results are missing: {report_path}")
    results = _load_document(by_source["results.json"], "native evaluator results")
    instance_id = response["instance_id"]
    if instance_id in results.get("error_ids", []) or instance_id in results.get("incomplete_ids", []):
        raise PilotRunError(f"native evaluator result is incomplete: {report_path}")
    native_report_path = f"{instance_id}/report.json"
    if instance_id in results.get("empty_patch_ids", []):
        if response["grader_resolved"]:
            raise PilotRunError(f"empty native patch cannot be resolved: {report_path}")
    else:
        if native_report_path not in by_source:
            raise PilotRunError(f"native evaluator instance report is missing: {report_path}")
        native_report = _load_document(by_source[native_report_path], "native evaluator instance report")
        if native_report.get("resolved") is not response["grader_resolved"]:
            raise PilotRunError(f"native and normalized evaluator outcomes differ: {report_path}")
    return names


def _validate_trace(trace_path, digest, run_id, runtime_binding, response, task, manifest):
    path = Path(trace_path)
    if path.is_symlink() or not path.is_file():
        raise PilotRunError(f"adapter did not retain a regular trace file: {path}")
    if _file_sha256(path) != digest:
        raise PilotRunError(f"retained trace digest does not match adapter telemetry: {path}")
    try:
        with path.open("r", encoding="utf-8") as stream:
            events = [
                json.loads(line, object_pairs_hook=_object_without_duplicates)
                for line in stream
            ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PilotRunError(f"adapter trace is not valid JSONL: {path}") from exc
    if not events or any(not isinstance(event, dict) for event in events):
        raise PilotRunError(f"adapter trace has no valid runtime-binding header: {path}")
    allowed_schemas = {
        TRACE_HEADER_SCHEMA, TRACE_RUNTIME_SCHEMA, TRACE_USAGE_SCHEMA,
        TRACE_EVENT_SCHEMA, TRACE_GRADER_SCHEMA, TRACE_COMPLETE_SCHEMA,
    }
    if any(event.get("schema") not in allowed_schemas for event in events):
        raise PilotRunError(f"adapter trace contains an unknown event schema: {path}")
    if sum(event.get("schema") == TRACE_HEADER_SCHEMA for event in events) != 1 \
            or sum(event.get("schema") == TRACE_COMPLETE_SCHEMA for event in events) != 1:
        raise PilotRunError(f"adapter trace must contain one header and one terminal receipt: {path}")
    expected_header = {
        "schema": TRACE_HEADER_SCHEMA,
        "run_id": run_id,
        "runtime_binding": runtime_binding,
    }
    if events[0] != expected_header:
        raise PilotRunError(f"adapter trace runtime binding does not match preflight: {path}")
    runtime_events = [event for event in events[1:] if event.get("schema") == TRACE_RUNTIME_SCHEMA]
    if len(runtime_events) != 1:
        raise PilotRunError(f"adapter trace has no single task-runtime receipt: {path}")
    runtime_event = runtime_events[0]
    linux_identity = manifest["runtime"]["codex_platform_packages"][
        f"{manifest['runtime']['codex_version']}-linux-x64"
    ]
    expected_receipt = {
        "base_commit": task["base_commit"],
        "cap_verifier_python_ready": response["arm"] == "full",
        "codex_version": f"codex-cli {manifest['runtime']['codex_version']}",
        "mycelium_absent_at_start": True,
        "native_binary_sha256": linux_identity["binary_sha256"],
        "worktree_clean_at_start": True,
    }
    if set(runtime_event) != {"schema", "run_id", "receipt", "trajectory", "rollouts"} \
            or runtime_event.get("run_id") != run_id \
            or runtime_event.get("receipt") != expected_receipt:
        raise PilotRunError(f"adapter trace task-runtime receipt is inconsistent: {path}")
    artifacts = [runtime_event.get("trajectory")]
    rollouts = runtime_event.get("rollouts")
    if not isinstance(rollouts, list) or len(rollouts) < response["spawn_count"] + 1:
        raise PilotRunError(f"adapter trace does not retain every session rollout: {path}")
    artifacts.extend(rollouts)
    names = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"} \
                or not isinstance(artifact["path"], str) \
                or PurePosixPath(artifact["path"]).name != artifact["path"] \
                or not isinstance(artifact["sha256"], str) \
                or not HEX_SHA256.fullmatch(artifact["sha256"]):
            raise PilotRunError(f"adapter trace runtime artifact receipt is invalid: {path}")
        retained = path.parent / artifact["path"]
        if retained.is_symlink() or not retained.is_file() \
                or _file_sha256(retained) != artifact["sha256"]:
            raise PilotRunError(f"adapter trace runtime artifact is missing or changed: {retained}")
        names.append(artifact["path"])
    if len(names) != len(set(names)):
        raise PilotRunError(f"adapter trace runtime artifacts are duplicated: {path}")
    expected_usage = {
        "schema": TRACE_USAGE_SCHEMA,
        "run_id": run_id,
        "model": response["observed_model"],
        "provider": response["observed_provider"],
        "reasoning_effort": response["observed_reasoning_effort"],
        "input_tokens": response["input_tokens"],
        "cached_input_tokens": response["cached_input_tokens"],
        "output_tokens": response["output_tokens"],
        "reasoning_tokens": response["reasoning_tokens"],
        "cost_usd": response["cost_usd"],
    }
    expected_grader = {
        "schema": TRACE_GRADER_SCHEMA,
        "run_id": run_id,
        "instance_id": response["instance_id"],
        "evaluator": manifest["evaluator"],
        "image_digest": task["image_digest"],
        "hidden_row_sha256": task["hidden_row_sha256"],
        "grader_report_path": f"{run_id}.grader.json",
        "grader_report_sha256": response["grader_report_sha256"],
        "grader_resolved": response["grader_resolved"],
    }
    for expected, label in ((expected_usage, "provider usage"), (expected_grader, "grader receipt")):
        matches = [event for event in events[1:] if event.get("schema") == expected["schema"]]
        if matches != [expected]:
            raise PilotRunError(f"adapter trace {label} is missing, duplicated, or inconsistent: {path}")
    trajectory = [event for event in events[1:] if event.get("schema") == TRACE_EVENT_SCHEMA]
    if len(trajectory) != response["steps"] + response["tool_calls"]:
        raise PilotRunError(f"adapter trace completed-turn steps and tool-event count are incomplete: {path}")
    if [event.get("sequence") for event in trajectory] != list(range(len(trajectory))):
        raise PilotRunError(f"adapter trace trajectory sequence is invalid: {path}")
    if any(
        set(event) != {"schema", "run_id", "sequence", "kind", "content_sha256"}
        or event["run_id"] != run_id
        or event["kind"] not in {"turn", "tool"}
        or not isinstance(event["content_sha256"], str)
        or not HEX_SHA256.fullmatch(event["content_sha256"])
        for event in trajectory
    ):
        raise PilotRunError(f"adapter trace trajectory event is invalid: {path}")
    if sum(event["kind"] == "turn" for event in trajectory) != response["steps"] \
            or sum(event["kind"] == "tool" for event in trajectory) != response["tool_calls"]:
        raise PilotRunError(f"adapter trace completed-turn steps or tool counts do not match telemetry: {path}")
    expected_complete = {
        "schema": TRACE_COMPLETE_SCHEMA,
        "run_id": run_id,
        "trajectory_events": len(trajectory),
        "turn_events": response["steps"],
        "tool_events": response["tool_calls"],
    }
    if events[-1] != expected_complete:
        raise PilotRunError(f"adapter trace has no matching terminal completeness receipt: {path}")
    return set(names)


def _invoke_adapter_process(adapter_path, request, label, manifest):
    if _file_sha256(adapter_path) != manifest["runner_sha256"]:
        raise PilotRunError("adapter digest changed after manifest validation")
    command = [sys.executable, str(Path(adapter_path).resolve())] if Path(adapter_path).suffix == ".py" else [str(Path(adapter_path).resolve())]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=manifest["cost_caps"]["max_wall_time_seconds"],
        )
    except subprocess.TimeoutExpired as exc:
        raise PilotRunError(
            f"adapter timed out for {label}",
            process_evidence=_process_evidence(exc.stdout, exc.stderr),
        ) from exc
    except OSError as exc:
        raise PilotRunError(f"cannot invoke adapter: {exc}") from exc
    host_wall_time = time.monotonic() - started
    process_evidence = _process_evidence(completed.stdout, completed.stderr)
    if host_wall_time > manifest["cost_caps"]["max_wall_time_seconds"]:
        raise PilotRunError(
            f"adapter exceeded the host wall-time cap for {label}",
            process_evidence=process_evidence,
        )
    if completed.returncode != 0:
        raise PilotRunError(
            f"adapter exited nonzero for {label}: {completed.returncode}",
            process_evidence=process_evidence,
        )
    try:
        response = json.loads(completed.stdout, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PilotRunError(
            f"adapter returned malformed JSON for {label}",
            process_evidence=process_evidence,
        ) from exc
    except PilotRunError as exc:
        exc.process_evidence = process_evidence
        raise
    if not isinstance(response, dict):
        raise PilotRunError(
            f"adapter returned non-object JSON for {label}",
            process_evidence=process_evidence,
        )
    return response, process_evidence, host_wall_time


def _invoke_adapter(adapter_path, request, expected, manifest, preregistration_document):
    response, process_evidence, host_wall_time = _invoke_adapter_process(
        adapter_path, request, expected["run_id"], manifest
    )
    try:
        response = _validate_response(response, expected, manifest, preregistration_document)
        native_artifact_names = _validate_grader_report(
            request["grader_report_path"], response["grader_report_sha256"],
            response, request["task"], manifest,
        )
        runtime_artifact_names = native_artifact_names | _validate_trace(
            request["trace_path"],
            response["trace_sha256"],
            expected["run_id"],
            request["runtime_binding"],
            response,
            request["task"],
            manifest,
        )
    except PilotRunError as exc:
        exc.process_evidence = process_evidence
        raise
    response["wall_time_seconds"] = round(host_wall_time, 6)
    return response, runtime_artifact_names


def _invoke_preflight_adapter(adapter_path, request, manifest, evidence_root):
    response, process_evidence, host_wall_time = _invoke_adapter_process(
        adapter_path, request, "preflight", manifest
    )
    expected_fields = {"schema", "request_sha256", "pilot_manifest_sha256", "receipt"}
    if set(response) != expected_fields or response.get("schema") != PREFLIGHT_RESPONSE_SCHEMA \
            or response.get("request_sha256") != request["request_sha256"] \
            or response.get("pilot_manifest_sha256") != manifest["manifest_sha256"]:
        raise PilotRunError("adapter preflight response binding is invalid", process_evidence=process_evidence)
    protocol = response["receipt"].get("protocol_schema") if isinstance(response["receipt"], dict) else None
    artifact_path = protocol.get("artifact_path") if isinstance(protocol, dict) else None
    expected_directory = Path(request["receipt_directory"]).name
    relative = PurePosixPath(artifact_path) if isinstance(artifact_path, str) else None
    if relative is None or len(relative.parts) != 2 or relative.parts[0] != expected_directory:
        raise PilotRunError("adapter preflight artifact binding is invalid", process_evidence=process_evidence)
    try:
        receipt = preflight.validate_receipt(
            response["receipt"], request["model"], evidence_root, request["treatment_root"]
        )
    except preflight.PreflightError as exc:
        raise PilotRunError(str(exc), process_evidence=process_evidence) from exc
    return receipt, process_evidence, round(host_wall_time, 6)


def _diagnostics(runs):
    by_task = {}
    costs = {arm: Decimal("0") for arm in pilot.ARMS}
    for row in runs:
        by_task.setdefault(row["instance_id"], {})[row["arm"]] = row["grader_resolved"]
        costs[row["arm"]] += Decimal(row["cost_usd"])

    def paired(before, after):
        values = [(outcomes[before], outcomes[after]) for outcomes in by_task.values()]
        return {
            "improvements": sum(not left and right for left, right in values),
            "regressions": sum(left and not right for left, right in values),
            "ties": sum(left == right for left, right in values),
        }

    successes = {
        arm: sum(outcomes[arm] for outcomes in by_task.values())
        for arm in pilot.ARMS
    }
    full_vs_vanilla = paired("vanilla", "full")
    prompt_vs_full = paired("prompt", "full")
    all_arms_failed = sorted(
        instance_id for instance_id, outcomes in by_task.items() if not any(outcomes.values())
    )
    all_arms_passed = sorted(
        instance_id for instance_id, outcomes in by_task.items() if all(outcomes.values())
    )
    stop_reasons = []
    if successes["vanilla"] == 0:
        stop_reasons.append("BASELINE_FLOOR")
    elif successes["vanilla"] == len(by_task):
        stop_reasons.append("BASELINE_CEILING")
    if all_arms_failed:
        stop_reasons.append("TASK_FLOOR")
    if all_arms_passed:
        stop_reasons.append("TASK_CEILING")
    if full_vs_vanilla["improvements"] + full_vs_vanilla["regressions"] == 0:
        stop_reasons.append("NO_FULL_BASELINE_DISCORDANCE")
    if successes["prompt"] == successes["full"]:
        stop_reasons.append("NO_OBSERVED_ORCHESTRATION_VALUE")
    if successes["full"] <= successes["vanilla"] and costs["full"] > costs["vanilla"]:
        stop_reasons.append("FULL_COSTLIER_WITHOUT_SUCCESS_GAIN")
    return {
        "decision": "STOP_OR_RECALIBRATE" if stop_reasons else "TRACE_DIAGNOSIS_ELIGIBLE",
        "stop_reasons": stop_reasons,
        "successes": successes,
        "cost_usd": {arm: format(costs[arm], "f") for arm in pilot.ARMS},
        "full_vs_vanilla": full_vs_vanilla,
        "prompt_vs_full": prompt_vs_full,
        "all_arms_failed": all_arms_failed,
        "all_arms_passed": all_arms_passed,
    }


def _validate_task_rows(manifest, task_rows_path):
    root = Path(task_rows_path)
    if root.is_symlink() or not root.is_dir():
        raise PilotRunError(f"task-row root is not a regular directory: {root}")
    root = root.resolve()
    materializations = {}
    tasks = [*manifest["selection"]["tasks"], manifest["canary"]["task"]]
    for task in tasks:
        digest = task["hidden_row_sha256"]
        row_path = root / f"{digest}.json"
        if row_path.is_symlink() or not row_path.is_file():
            raise PilotRunError(f"frozen task row is not a regular file: {row_path}")
        try:
            raw = row_path.read_bytes()
            row = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PilotRunError(f"frozen task row is not valid JSON: {row_path}") from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise PilotRunError(f"frozen task row digest does not match its filename: {row_path}")
        if not isinstance(row, dict) or not TASK_ROW_FIELDS <= set(row):
            raise PilotRunError(f"frozen task row is missing required fields: {row_path}")
        expected = {
            "instance_id": task["instance_id"],
            "repo": task["repository"],
            "base_commit": task["base_commit"],
        }
        if any(row[field] != value for field, value in expected.items()) \
                or not isinstance(row["problem_statement"], str) \
                or not row["problem_statement"].strip():
            raise PilotRunError(f"frozen task row does not match the selected task: {row_path}")
        materializations[task["instance_id"]] = {
            "row_path": str(row_path),
            "row_sha256": digest,
            "problem_statement": row["problem_statement"],
            "image_digest": task["image_digest"],
            "platform": "linux",
        }
    return materializations


def _validate_evaluator_root(evaluator_root):
    candidate = Path(evaluator_root)
    if candidate.is_symlink() or not candidate.is_dir():
        raise PilotRunError(f"evaluator root is not a regular directory: {candidate}")
    root = candidate.resolve()
    try:
        top_level = pilot._git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
        commit = pilot._git(root, "rev-parse", "HEAD").decode("ascii").strip()
        status = pilot._git(root, "status", "--porcelain=v1")
    except (pilot.PilotError, UnicodeError) as exc:
        raise PilotRunError(f"cannot validate evaluator git worktree: {exc}") from exc
    if Path(top_level).resolve() != root:
        raise PilotRunError("evaluator root must be the git worktree root")
    if commit != pilot.EVALUATOR_COMMIT:
        raise PilotRunError("evaluator worktree is not at the frozen commit")
    if status:
        raise PilotRunError("evaluator worktree is not clean")
    if not (root / "evaluation" / "evaluation.py").is_file():
        raise PilotRunError("evaluator worktree is missing evaluation/evaluation.py")
    return str(root)


def _validate_evaluator_runtime(evaluator_root):
    probes = (
        ([sys.executable, "-c", "import evaluation.evaluation"], "Python dependencies"),
        ([sys.executable, "-c", "import docker; c=docker.from_env(); c.ping(); c.close()"],
         "Docker daemon"),
    )
    for command, label in probes:
        try:
            completed = subprocess.run(
                command, cwd=evaluator_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PilotRunError(f"official evaluator {label} preflight failed: {exc}") from exc
        if completed.returncode:
            raise PilotRunError(
                f"official evaluator {label} preflight failed",
                process_evidence=_process_evidence(completed.stdout, completed.stderr),
            )


def _validate_inputs(manifest_path, candidates_path, treatment_root, installed_treatment, prompt_paths,
                     adapter_path, task_rows_path, evaluator_root):
    manifest = pilot.validate_manifest(
        manifest_path, candidates_path, treatment_root, installed_treatment, prompt_paths, adapter_path
    )
    if pilot.executor_digest() != manifest["executor_sha256"]:
        raise PilotRunError("executing pilot files do not match the frozen executor digest")
    return (
        manifest,
        _validate_task_rows(manifest, task_rows_path),
        _validate_evaluator_root(evaluator_root),
    )


def _create_evidence_directory(output_path, treatment_root):
    destination = Path(output_path).resolve()
    treatment = Path(treatment_root).resolve()
    try:
        destination.relative_to(treatment)
    except ValueError:
        pass
    else:
        raise PilotRunError("result and evidence paths must be outside the frozen treatment repository")
    evidence = destination.with_name(destination.name + ".evidence")
    if os.path.lexists(evidence):
        raise FileExistsError(evidence)
    try:
        evidence.mkdir(mode=0o700)
        journal = evidence / "journal.jsonl"
        journal.touch(exist_ok=False)
    except OSError as exc:
        raise PilotRunError(f"cannot create pilot evidence directory: {exc}") from exc
    return evidence, journal


def _append_journal(path, record):
    data = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _trace_record(path):
    trace = Path(path)
    if trace.is_symlink() or not trace.is_file():
        return {"status": "MISSING"}
    return {"status": "RETAINED", "path": trace.name, "sha256": _file_sha256(trace)}


def _scheduled_runs(manifest, canary_only):
    if canary_only:
        task = manifest["canary"]["task"]
        return [(-1, {"instance_id": task["instance_id"], "arm_order": ["full"]}, 0, "full")]
    rows = [
        (schedule_index, schedule_row, arm_position, arm_name)
        for schedule_index, schedule_row in enumerate(manifest["schedule"])
        for arm_position, arm_name in enumerate(schedule_row["arm_order"])
    ]
    return rows


def _expected_run(manifest, scheduled):
    schedule_index, schedule_row, arm_position, arm_name = scheduled
    phase = "canary" if schedule_index == -1 else "pilot"
    return {
        "run_id": (
            f"canary-{manifest['manifest_sha256'][:12]}-r{manifest['replicate']}-"
            f"{schedule_row['instance_id']}"
            if phase == "canary" else
            f"{manifest['manifest_sha256'][:12]}-r{manifest['replicate']}-"
            f"t{schedule_index:02d}-p{arm_position}-{arm_name}"
        ),
        "instance_id": schedule_row["instance_id"],
        "arm": arm_name,
        "phase": phase,
        "requested_model": manifest["model"]["requested"],
        "model_revision": manifest["model"]["revision"],
        "model_provider": manifest["model"]["provider"],
        "reasoning_effort": manifest["model"]["reasoning_effort"],
        "replicate": manifest["replicate"],
        "arm_order": schedule_row["arm_order"],
    }


def _runtime_binding(receipt, treatment_root):
    return {
        "receipt_sha256": receipt["receipt_sha256"],
        "native_binary_sha256": receipt["platform_package"]["binary"]["sha256"],
        "runtime_command": receipt["runtime"]["command"],
        "model": receipt["model"],
        "effective_defaults": receipt["app_server"]["effective_defaults"],
        "config_cwd": str(Path(treatment_root).resolve()),
    }


def _load_document(path, label):
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise PilotRunError(f"{label} is not a regular file: {candidate}")
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PilotRunError(f"{label} is not valid JSON: {candidate}") from exc
    if not isinstance(document, dict):
        raise PilotRunError(f"{label} must contain a JSON object")
    return document


def _validate_preregistration(path, uri, digest, manifest):
    identity = _preregistration_identity(uri, digest)
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise PilotRunError(f"preregistration artifact is not a regular file: {candidate}")
    if _file_sha256(candidate) != digest:
        raise PilotRunError("preregistration artifact digest does not match the published digest")
    document = _load_document(candidate, "preregistration artifact")
    expected_fields = {
        "schema", "uri", "created_at", "pilot_manifest_sha256", "schedule_sha256",
        "arms", "expected_runs", "model", "cost_caps", "cost_accounting", "stop_rules",
    }
    if set(document) != expected_fields or document.get("schema") != PREREGISTRATION_SCHEMA:
        raise PilotRunError("preregistration artifact schema or fields are invalid")
    created_at = _timestamp(document["created_at"], "preregistration created_at")
    if created_at > datetime.now(timezone.utc):
        raise PilotRunError("preregistration creation time is in the future")
    expected = {
        "uri": uri,
        "pilot_manifest_sha256": manifest["manifest_sha256"],
        "schedule_sha256": manifest["schedule_sha256"],
        "arms": list(pilot.ARMS),
        "expected_runs": 30,
        "model": {
            field: manifest["model"][field]
            for field in ("provider", "requested", "revision", "reasoning_effort")
        },
        "cost_caps": manifest["cost_caps"],
        "stop_rules": list(STOP_RULES),
    }
    if any(document.get(field) != value for field, value in expected.items()):
        raise PilotRunError("preregistration artifact does not bind the frozen pilot")
    accounting = document["cost_accounting"]
    rate_fields = {
        "input_usd_per_million", "cached_input_usd_per_million", "output_usd_per_million",
    }
    if not isinstance(accounting, dict) \
            or set(accounting) != rate_fields | {"basis", "currency"} \
            or accounting.get("basis") != "NOTIONAL_API_EQUIVALENT" \
            or accounting.get("currency") != "USD":
        raise PilotRunError("preregistration cost-accounting fields are invalid")
    for field in rate_fields:
        value = accounting[field]
        try:
            valid = isinstance(value, str) and DECIMAL.fullmatch(value) and Decimal(value) >= 0
        except InvalidOperation:
            valid = False
        if not valid:
            raise PilotRunError(f"preregistration {field} is not a non-negative decimal string")
    return {
        **identity,
        "artifact_path": "preregistration.json",
        "validation": "RETAINED_BINDING_VERIFIED",
    }, document


def _retain_preregistration(source, destination, digest):
    data = Path(source).read_bytes()
    with Path(destination).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if hashlib.sha256(data).hexdigest() != digest or _file_sha256(destination) != digest:
        raise PilotRunError("retained preregistration artifact digest changed")


def _derived_cost(response, preregistration_document):
    accounting = preregistration_document["cost_accounting"]
    uncached = response["input_tokens"] - response["cached_input_tokens"]
    numerator = (
        Decimal(uncached) * Decimal(accounting["input_usd_per_million"])
        + Decimal(response["cached_input_tokens"]) * Decimal(accounting["cached_input_usd_per_million"])
        + Decimal(response["output_tokens"]) * Decimal(accounting["output_usd_per_million"])
    )
    return numerator / Decimal(1_000_000)


def _validate_canary_receipt(path, manifest, treatment_root, current_binding, preregistration):
    canary = _load_document(path, "canary receipt")
    expected_fields = {
        "schema", "status", "pilot_manifest_sha256", "schedule_sha256", "adapter_sha256",
        "executor_sha256", "runtime_binding", "runtime_preflight", "preregistration", "model",
        "replicate", "expected_runs", "runs", "evidence_directory", "journal_sha256", "traces",
        "grader_reports", "result_sha256",
    }
    if set(canary) != expected_fields or canary.get("schema") != CANARY_RESULT_SCHEMA \
            or canary.get("status") != "MODEL_CANARY_COMPLETE_UNVERIFIED" \
            or canary.get("result_sha256") != _document_sha256(canary):
        raise PilotRunError("canary receipt schema, status, or digest is invalid")
    bindings = {
        "pilot_manifest_sha256": manifest["manifest_sha256"],
        "schedule_sha256": manifest["schedule_sha256"],
        "adapter_sha256": manifest["runner_sha256"],
        "executor_sha256": manifest["executor_sha256"],
        "replicate": manifest["replicate"],
    }
    if any(canary.get(field) != value for field, value in bindings.items()):
        raise PilotRunError("canary receipt does not match the frozen pilot")
    if canary.get("preregistration") != preregistration:
        raise PilotRunError("canary receipt preregistration does not match the current pilot")

    if not isinstance(canary["evidence_directory"], str):
        raise PilotRunError("canary evidence directory path is invalid")
    evidence = Path(canary["evidence_directory"])
    if evidence.is_symlink() or not evidence.is_dir():
        raise PilotRunError("canary evidence directory is missing or unsafe")
    prior_preregistration, prior_preregistration_document = _validate_preregistration(
        evidence / preregistration["artifact_path"],
        preregistration["uri"],
        preregistration["sha256"],
        manifest,
    )
    if prior_preregistration != preregistration:
        raise PilotRunError("canary retained preregistration identity is invalid")
    preflight_summary = canary["runtime_preflight"]
    if not isinstance(preflight_summary, dict):
        raise PilotRunError("canary runtime-preflight summary is invalid")
    preflight_name = preflight_summary.get("path")
    if not isinstance(preflight_name, str) or PurePosixPath(preflight_name).name != preflight_name:
        raise PilotRunError("canary runtime-preflight path is invalid")
    preflight_path = evidence / preflight_name
    prior_preflight = _load_document(preflight_path, "canary runtime preflight")
    if _file_sha256(preflight_path) != preflight_summary.get("sha256"):
        raise PilotRunError("canary runtime-preflight artifact digest is invalid")
    preflight_model = {
        field: manifest["model"][field]
        for field in preflight.MODEL_FIELDS
    }
    try:
        preflight.validate_receipt(prior_preflight, preflight_model, evidence, treatment_root)
    except preflight.PreflightError as exc:
        raise PilotRunError(f"canary runtime preflight is invalid: {exc}") from exc
    prior_binding = _runtime_binding(prior_preflight, treatment_root)
    if canary["runtime_binding"] != prior_binding:
        raise PilotRunError("canary trace binding does not match its runtime preflight")
    stable_fields = set(current_binding) - {"receipt_sha256"}
    if any(prior_binding[field] != current_binding[field] for field in stable_fields):
        raise PilotRunError("canary runtime does not match the current structural preflight")

    scheduled = _scheduled_runs(manifest, True)[0]
    expected = _expected_run(manifest, scheduled)
    runs = canary["runs"]
    if canary["expected_runs"] != 1 or not isinstance(runs, list) or len(runs) != 1:
        raise PilotRunError("canary receipt must contain exactly one run")
    run = _validate_response(runs[0], expected, manifest, prior_preregistration_document)
    expected_model = {
        "provider": manifest["model"]["provider"],
        "requested": manifest["model"]["requested"],
        "revision": manifest["model"]["revision"],
        "reasoning_effort": manifest["model"]["reasoning_effort"],
        "observed": run["observed_model"],
        "observed_provider": run["observed_provider"],
        "observed_reasoning_effort": run["observed_reasoning_effort"],
    }
    if canary["model"] != expected_model:
        raise PilotRunError("canary observed-model summary is invalid")
    trace = canary["traces"]
    expected_trace = {run["run_id"]: {
        "path": f"{run['run_id']}.trace.jsonl",
        "sha256": run["trace_sha256"],
    }}
    if trace != expected_trace:
        raise PilotRunError("canary trace index is invalid")
    expected_grader = {run["run_id"]: {
        "path": f"{run['run_id']}.grader.json",
        "sha256": run["grader_report_sha256"],
    }}
    if canary["grader_reports"] != expected_grader:
        raise PilotRunError("canary grader-report index is invalid")
    _validate_grader_report(
        evidence / expected_grader[run["run_id"]]["path"],
        run["grader_report_sha256"],
        run,
        manifest["canary"]["task"],
        manifest,
    )
    _validate_trace(
        evidence / expected_trace[run["run_id"]]["path"],
        run["trace_sha256"],
        run["run_id"],
        prior_binding,
        run,
        manifest["canary"]["task"],
        manifest,
    )
    journal = evidence / "journal.jsonl"
    if not journal.is_file() or _file_sha256(journal) != canary["journal_sha256"]:
        raise PilotRunError("canary journal evidence is invalid")
    return canary


def _atomic_write(path, document):
    destination = Path(path)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    if not destination.parent.is_dir():
        raise PilotRunError(f"result directory does not exist: {destination.parent}")
    data = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        raise PilotRunError(f"cannot publish result artifact atomically: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(*, manifest_path, output_path, candidates_path, task_rows_path, evaluator_root, treatment_root,
        installed_treatment, prompt_paths, adapter_path, preregistration_uri, preregistration_sha256,
        preregistration_path,
        authorize_model_calls=False,
        accept_task_container_auth_risk=False,
        canary_only=False, canary_receipt=None):
    if canary_only and canary_receipt is not None:
        raise PilotRunError("canary-only execution cannot consume a prior canary receipt")
    if os.path.lexists(output_path):
        raise FileExistsError(output_path)
    manifest, task_materializations, evaluator_root = _validate_inputs(
        manifest_path, candidates_path, treatment_root, installed_treatment, prompt_paths, adapter_path,
        task_rows_path, evaluator_root,
    )
    preregistration, preregistration_document = _validate_preregistration(
        preregistration_path, preregistration_uri, preregistration_sha256, manifest
    )
    if _file_sha256(adapter_path) != manifest["runner_sha256"]:
        raise PilotRunError("adapter does not match the frozen runner digest")
    evidence, journal = _create_evidence_directory(output_path, treatment_root)
    _retain_preregistration(
        preregistration_path,
        evidence / preregistration["artifact_path"],
        preregistration["sha256"],
    )
    _append_journal(journal, {
        "event": "pilot_started",
        "pilot_manifest_sha256": manifest["manifest_sha256"],
        "executor_sha256": manifest["executor_sha256"],
        "adapter_sha256": manifest["runner_sha256"],
        "preregistration": preregistration,
    })
    preflight_path = evidence / "runtime-preflight.json"
    preflight_artifacts = evidence / "preflight"
    preflight_model = {
        field: manifest["model"][field]
        for field in ("provider", "requested", "revision", "reasoning_effort")
    }
    preflight_request = {
        "schema": PREFLIGHT_REQUEST_SCHEMA,
        "pilot_manifest_sha256": manifest["manifest_sha256"],
        "manifest": manifest,
        "preregistration": preregistration,
        "model": preflight_model,
        "prompts": {
            arm["name"]: {
                "path": str(Path(prompt_paths[arm["name"]]).resolve()),
                "sha256": arm["prompt_sha256"],
            }
            for arm in manifest["arms"]
        },
        "treatment_root": str(Path(treatment_root).resolve()),
        "installed_treatment": str(Path(installed_treatment).resolve()),
        "receipt_directory": str(preflight_artifacts.resolve()),
        "adapter_sha256": manifest["runner_sha256"],
        "executor_sha256": manifest["executor_sha256"],
    }
    preflight_request["request_sha256"] = _document_sha256(preflight_request)
    _append_journal(journal, {
        "event": "preflight_started",
        "runtime": manifest["runtime"],
        "requested_model": manifest["model"]["requested"],
        "request_sha256": preflight_request["request_sha256"],
    })
    try:
        _validate_evaluator_runtime(evaluator_root)
        if os.path.lexists(preflight_artifacts):
            raise PilotRunError(f"preflight artifact path already exists: {preflight_artifacts}")
        runtime_preflight, preflight_process, preflight_wall_time = _invoke_preflight_adapter(
            adapter_path, preflight_request, manifest, evidence
        )
        _atomic_write(preflight_path, runtime_preflight)
        current, _, _ = _validate_inputs(
            manifest_path, candidates_path, treatment_root, installed_treatment, prompt_paths, adapter_path,
            task_rows_path, evaluator_root,
        )
        if current["manifest_sha256"] != manifest["manifest_sha256"]:
            raise PilotRunError("frozen pilot inputs changed during preflight")
    except (preflight.PreflightError, PilotRunError, pilot.PilotError, FileExistsError) as exc:
        failure = {"event": "preflight_failed", "error": str(exc)}
        if preflight_path.is_file():
            failure["receipt"] = {
                "path": preflight_path.name,
                "sha256": _file_sha256(preflight_path),
            }
        if getattr(exc, "process_evidence", None) is not None:
            failure["process"] = exc.process_evidence
        _append_journal(journal, failure)
        raise
    _append_journal(journal, {
        "event": "preflight_completed",
        "status": runtime_preflight["status"],
        "receipt_sha256": runtime_preflight["receipt_sha256"],
        "artifact_sha256": _file_sha256(preflight_path),
        "model_calls": runtime_preflight["model_calls"],
        "turn_start_requests": runtime_preflight["turn_start_requests"],
        "host_wall_time_seconds": preflight_wall_time,
        "process": preflight_process,
    })
    runtime_binding = _runtime_binding(runtime_preflight, treatment_root)
    if not authorize_model_calls:
        _append_journal(journal, {
            "event": "model_calls_not_authorized",
            "error": "explicit model-call authorization is required after structural preflight",
        })
        raise PilotRunError("explicit model-call authorization is required after structural preflight")
    if not accept_task_container_auth_risk:
        _append_journal(journal, {
            "event": "task_container_auth_risk_not_accepted",
            "error": "direct Pier execution exposes ephemeral ChatGPT auth to the task container",
        })
        raise PilotRunError(
            "explicit acceptance of task-container ChatGPT-auth exposure is required"
        )
    canary = None
    if not canary_only:
        try:
            if canary_receipt is None:
                raise PilotRunError("a validated model-canary receipt is required before the full pilot")
            canary = _validate_canary_receipt(
                canary_receipt, manifest, treatment_root, runtime_binding, preregistration
            )
        except (preflight.PreflightError, PilotRunError, pilot.PilotError) as exc:
            _append_journal(journal, {"event": "canary_receipt_failed", "error": str(exc)})
            raise
        _append_journal(journal, {
            "event": "canary_receipt_validated",
            "path": str(Path(canary_receipt).resolve()),
            "result_sha256": canary["result_sha256"],
        })
    _append_journal(journal, {
        "event": "model_calls_authorized",
        "source": "explicit model-canary argument" if canary_only else "explicit pilot argument with validated model canary",
    })
    arms = {arm["name"]: arm for arm in manifest["arms"]}
    tasks = {
        task["instance_id"]: task
        for task in [*manifest["selection"]["tasks"], manifest["canary"]["task"]]
    }
    runs = []
    seen = set()
    runtime_artifact_names = set()
    scheduled_runs = _scheduled_runs(manifest, canary_only)
    cumulative_cost = (
        sum((Decimal(row["cost_usd"]) for row in canary["runs"]), Decimal("0"))
        if canary is not None else Decimal("0")
    )
    max_total_cost = Decimal(manifest["cost_caps"]["max_total_usd"])
    for schedule_index, schedule_row, arm_position, arm_name in scheduled_runs:
        expected = _expected_run(
            manifest, (schedule_index, schedule_row, arm_position, arm_name)
        )
        run_id = expected["run_id"]
        arm = arms[arm_name]
        trace_path = evidence / f"{run_id}.trace.jsonl"
        grader_report_path = evidence / f"{run_id}.grader.json"
        remaining_cost = max_total_cost - cumulative_cost
        if remaining_cost <= 0:
            _append_journal(journal, {"event": "budget_exhausted", "spent_usd": format(cumulative_cost, "f")})
            raise PilotRunError("pilot notional API-equivalent publication budget is exhausted")
        request = {
                "schema": REQUEST_SCHEMA,
                "pilot_manifest_sha256": manifest["manifest_sha256"],
                "preregistration": preregistration,
                "run": {
                    **expected,
                    "schedule_index": schedule_index,
                    "arm_position": arm_position,
                },
                "task": tasks[schedule_row["instance_id"]],
                "task_materialization": task_materializations[schedule_row["instance_id"]],
                "arm_config": arm,
                "dataset": manifest["dataset"],
                "evaluator": manifest["evaluator"],
                "evaluator_root": evaluator_root,
                "runtime": manifest["runtime"],
                "model": manifest["model"],
                "cost_caps": manifest["cost_caps"],
                "batch_budget": {
                    "max_total_usd": manifest["cost_caps"]["max_total_usd"],
                    "spent_usd_before_run": format(cumulative_cost, "f"),
                    "remaining_usd_before_run": format(remaining_cost, "f"),
                    "effective_run_cap_usd": format(
                        min(remaining_cost, Decimal(manifest["cost_caps"]["max_usd_per_run"])),
                        "f",
                    ),
                },
                "runtime_binding": runtime_binding,
                "trace_path": str(trace_path),
                "grader_report_path": str(grader_report_path),
                "prompt": {
                    "path": str(Path(prompt_paths[arm_name]).resolve()),
                    "sha256": arm["prompt_sha256"],
                },
                "treatment": manifest["treatment"] if arm["treatment_enabled"] else None,
                "installed_treatment": str(Path(installed_treatment).resolve()) if arm["treatment_enabled"] else None,
                "adapter_sha256": manifest["runner_sha256"],
                "executor_sha256": manifest["executor_sha256"],
                "authentication": {
                    "transport": "PIER_CONTAINER_AUTH_JSON",
                    "task_container_exposure_acknowledged": True,
                },
        }
        _append_journal(journal, {"event": "run_started", "run": expected})
        try:
            current, current_materializations, current_evaluator_root = _validate_inputs(
                manifest_path, candidates_path, treatment_root, installed_treatment,
                prompt_paths, adapter_path, task_rows_path, evaluator_root,
            )
            if current["manifest_sha256"] != manifest["manifest_sha256"]:
                raise PilotRunError("frozen pilot inputs changed during execution")
            if current_materializations != task_materializations or current_evaluator_root != evaluator_root:
                raise PilotRunError("task or evaluator inputs changed during execution")
            if os.path.lexists(trace_path) or os.path.lexists(grader_report_path):
                raise PilotRunError(f"run evidence path already exists: {run_id}")
            response, run_runtime_artifacts = _invoke_adapter(
                adapter_path, request, expected, manifest, preregistration_document
            )
            if runtime_artifact_names.intersection(run_runtime_artifacts):
                raise PilotRunError("runtime artifact names are reused across runs")
            runtime_artifact_names.update(run_runtime_artifacts)
            run_cost = Decimal(response["cost_usd"])
            if run_cost > remaining_cost:
                raise PilotRunError("notional API-equivalent cost exceeded the remaining frozen publication budget")
            cumulative_cost += run_cost
            if cumulative_cost > max_total_cost:
                raise PilotRunError("pilot exceeded the frozen notional API-equivalent publication budget")
        except (PilotRunError, pilot.PilotError) as exc:
            failure = {
                "event": "run_failed",
                "run": expected,
                "error": str(exc),
                "trace": _trace_record(trace_path),
            }
            if getattr(exc, "process_evidence", None) is not None:
                failure["process"] = exc.process_evidence
            _append_journal(journal, failure)
            raise
        if response["run_id"] in seen:
            raise PilotRunError(f"duplicate run result: {response['run_id']}")
        seen.add(response["run_id"])
        runs.append(response)
        _append_journal(journal, {
            "event": "run_completed",
            "run": expected,
            "telemetry": response,
            "trace": _trace_record(trace_path),
        })

    expected_runs = len(scheduled_runs)
    try:
        current, current_materializations, current_evaluator_root = _validate_inputs(
            manifest_path, candidates_path, treatment_root, installed_treatment, prompt_paths, adapter_path,
            task_rows_path, evaluator_root,
        )
        if current["manifest_sha256"] != manifest["manifest_sha256"]:
            raise PilotRunError("frozen pilot inputs changed during execution")
        if current_materializations != task_materializations or current_evaluator_root != evaluator_root:
            raise PilotRunError("task or evaluator inputs changed during execution")
        if expected_runs != (1 if canary_only else 30) \
                or len(runs) != expected_runs or len(seen) != expected_runs:
            raise PilotRunError("pilot result set is missing, extra, or duplicated")
        trace_names = {f"{row['run_id']}.trace.jsonl" for row in runs}
        grader_names = {f"{row['run_id']}.grader.json" for row in runs}
        evidence_names = {path.name for path in evidence.iterdir()}
        if evidence_names != trace_names | grader_names | runtime_artifact_names | {
            journal.name, preflight_path.name, preflight_artifacts.name,
            preregistration["artifact_path"],
        }:
            raise PilotRunError("pilot evidence directory is missing traces or contains unexpected files")
        if preflight_artifacts.is_symlink() or not preflight_artifacts.is_dir():
            raise PilotRunError("pilot preflight artifact directory is missing or unsafe")
        expected_schema = PurePosixPath(runtime_preflight["protocol_schema"]["artifact_path"]).name
        if {path.name for path in preflight_artifacts.iterdir()} != {expected_schema}:
            raise PilotRunError("pilot preflight artifact directory contains unexpected files")
        preflight.validate_receipt(
            runtime_preflight,
            preflight_model,
            evidence,
            treatment_root,
        )
        for row in runs:
            _validate_grader_report(
                evidence / f"{row['run_id']}.grader.json",
                row["grader_report_sha256"],
                row,
                tasks[row["instance_id"]],
                manifest,
            )
            _validate_trace(
                evidence / f"{row['run_id']}.trace.jsonl",
                row["trace_sha256"],
                row["run_id"],
                runtime_binding,
                row,
                tasks[row["instance_id"]],
                manifest,
            )
        observed_models = {row["observed_model"] for row in runs}
        if observed_models != {manifest["model"]["requested"]}:
            raise PilotRunError("pilot observed a model slug outside the requested contract")
        observed_providers = {row["observed_provider"] for row in runs}
        observed_efforts = {row["observed_reasoning_effort"] for row in runs}
        sessions = [row["session_id"] for row in runs]
        if len(set(sessions)) != expected_runs:
            raise PilotRunError("pilot reused a session across isolated arm runs")
        full_rows = [row for row in runs if row["arm"] == "full"]
        parents = [row["parent_session_id"] for row in full_rows]
        if len(set(parents)) != len(full_rows) or set(parents) & set(sessions):
            raise PilotRunError("full-arm parent sessions are not isolated")
        if canary is not None:
            canary_run = canary["runs"][0]
            canary_sessions = {canary_run["session_id"], canary_run["parent_session_id"]}
            if canary_sessions & (set(sessions) | set(parents)):
                raise PilotRunError("pilot reused a canary session")
    except (preflight.PreflightError, PilotRunError, pilot.PilotError) as exc:
        _append_journal(journal, {"event": "batch_failed", "error": str(exc)})
        raise

    _append_journal(journal, {"event": "runs_complete", "run_count": expected_runs})
    journal_sha256 = _file_sha256(journal)
    artifact = {
        "schema": CANARY_RESULT_SCHEMA if canary_only else RESULT_SCHEMA,
        "status": "MODEL_CANARY_COMPLETE_UNVERIFIED" if canary_only else "RUNS_COMPLETE_UNVERIFIED",
        "pilot_manifest_sha256": manifest["manifest_sha256"],
        "schedule_sha256": manifest["schedule_sha256"],
        "adapter_sha256": manifest["runner_sha256"],
        "executor_sha256": manifest["executor_sha256"],
        "runtime_binding": runtime_binding,
        "runtime_preflight": {
            "path": preflight_path.name,
            "sha256": _file_sha256(preflight_path),
            "receipt_sha256": runtime_preflight["receipt_sha256"],
            "status": runtime_preflight["status"],
            "model_calls": runtime_preflight["model_calls"],
            "turn_start_requests": runtime_preflight["turn_start_requests"],
            "model_authorization": runtime_preflight["model_authorization"],
            "model_canary_required": runtime_preflight["model_canary_required"],
            "model_call_authorization": (
                "EXPLICIT_MODEL_CANARY_ARGUMENT"
                if canary_only else "EXPLICIT_PILOT_ARGUMENT_WITH_VALIDATED_MODEL_CANARY"
            ),
        },
        "preregistration": preregistration,
        "model": {
            "provider": manifest["model"]["provider"],
            "requested": manifest["model"]["requested"],
            "revision": manifest["model"]["revision"],
            "reasoning_effort": manifest["model"]["reasoning_effort"],
            "observed": next(iter(observed_models)),
            "observed_provider": next(iter(observed_providers)),
            "observed_reasoning_effort": next(iter(observed_efforts)),
        },
        "replicate": manifest["replicate"],
        "expected_runs": expected_runs,
        "runs": runs,
        "evidence_directory": str(evidence),
        "journal_sha256": journal_sha256,
        "traces": {
            row["run_id"]: {
                "path": f"{row['run_id']}.trace.jsonl",
                "sha256": row["trace_sha256"],
            }
            for row in runs
        },
        "grader_reports": {
            row["run_id"]: {
                "path": f"{row['run_id']}.grader.json",
                "sha256": row["grader_report_sha256"],
            }
            for row in runs
        },
    }
    if not canary_only:
        artifact["diagnostics"] = _diagnostics(runs)
    artifact["result_sha256"] = _document_sha256(artifact)
    try:
        _atomic_write(output_path, artifact)
    except (PilotRunError, FileExistsError) as exc:
        _append_journal(journal, {"event": "publication_failed", "error": str(exc)})
        raise
    return artifact


def _prompt_paths(args):
    return {"vanilla": args.vanilla_prompt, "prompt": args.prompt_prompt, "full": args.full_prompt}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--task-rows", required=True, type=Path)
    parser.add_argument("--evaluator-root", required=True, type=Path)
    parser.add_argument("--treatment-root", required=True, type=Path)
    parser.add_argument("--installed-treatment", required=True, type=Path)
    parser.add_argument("--vanilla-prompt", required=True, type=Path)
    parser.add_argument("--prompt-prompt", required=True, type=Path)
    parser.add_argument("--full-prompt", required=True, type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--preregistration-uri", required=True)
    parser.add_argument("--preregistration-sha256", required=True)
    parser.add_argument("--preregistration-artifact", required=True, type=Path)
    parser.add_argument(
        "--authorize-model-calls",
        action="store_true",
        help="Explicitly authorize model calls after the zero-call adapter preflight passes.",
    )
    parser.add_argument(
        "--accept-task-container-auth-risk",
        action="store_true",
        help=(
            "Acknowledge that Pier places ephemeral ChatGPT auth in the untrusted task container. "
            "Required for direct Codex model execution."
        ),
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--canary-only",
        action="store_true",
        help="Run exactly one full-arm model canary and stop without producing pilot diagnostics.",
    )
    execution.add_argument(
        "--canary-receipt",
        type=Path,
        help="Validated prior model-canary artifact required to authorize the full 30-run pilot.",
    )
    args = parser.parse_args(argv)
    try:
        artifact = run(
            manifest_path=args.manifest,
            output_path=args.output,
            candidates_path=args.candidates,
            task_rows_path=args.task_rows,
            evaluator_root=args.evaluator_root,
            treatment_root=args.treatment_root,
            installed_treatment=args.installed_treatment,
            prompt_paths=_prompt_paths(args),
            adapter_path=args.adapter,
            preregistration_uri=args.preregistration_uri,
            preregistration_sha256=args.preregistration_sha256,
            preregistration_path=args.preregistration_artifact,
            authorize_model_calls=args.authorize_model_calls,
            accept_task_container_auth_risk=args.accept_task_container_auth_risk,
            canary_only=args.canary_only,
            canary_receipt=args.canary_receipt,
        )
    except (PilotRunError, pilot.PilotError, FileExistsError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"{artifact['status']} {artifact['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
