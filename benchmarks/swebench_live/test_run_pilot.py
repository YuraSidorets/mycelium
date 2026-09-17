import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).parent))
import run_pilot
import test_pilot as pilot_fixture


FAKE_ADAPTER = r'''#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import sys

request = json.load(sys.stdin)
root = Path(__file__).parent
mode_path = root / "adapter-mode.txt"
mode = mode_path.read_text(encoding="utf-8").strip() if mode_path.exists() else "ok"
with (root / "adapter-calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(request, sort_keys=True) + "\n")
if request["schema"] == "mycelium.swebench-live-preflight-request/v1":
    if mode == "preflight-nonzero":
        raise SystemExit(7)
    if mode == "preflight-malformed":
        print("not-json")
        raise SystemExit(0)
    receipt_directory = Path(request["receipt_directory"])
    receipt_directory.mkdir()
    schema = {
        "definitions": {
            "RawResponseCompletedNotification": {"properties": {
                "responseId": {"type": "string"},
                "threadId": {"type": "string"},
                "turnId": {"type": "string"},
                "usage": {"anyOf": [
                    {"$ref": "#/definitions/TokenUsageBreakdown"},
                    {"type": "null"},
                ]},
            }, "required": ["responseId", "threadId", "turnId"]},
            "TokenUsageBreakdown": {
                "type": "object",
                "properties": {
                    field: {"type": "integer"} for field in (
                        "cachedInputTokens", "inputTokens", "outputTokens",
                        "reasoningOutputTokens", "totalTokens",
                    )
                },
                "required": [
                    "cachedInputTokens", "inputTokens", "outputTokens",
                    "reasoningOutputTokens", "totalTokens",
                ],
            },
            "Thread": {"properties": {
                "parentThreadId": {"type": ["string", "null"]},
                "modelProvider": {"type": "string"},
            }},
            "ThreadSettings": {"properties": {
                "model": {"type": "string"},
                "modelProvider": {"type": "string"},
                "effort": {"type": ["string", "null"]},
            }},
            "ModelReroutedNotification": {
                "required": ["fromModel", "reason", "threadId", "toModel", "turnId"],
            },
            "ThreadItem": {"oneOf": [{
                "required": ["senderThreadId", "receiverThreadIds", "type"],
                "properties": {
                    "senderThreadId": {"type": "string"},
                    "receiverThreadIds": {"type": "array", "items": {"type": "string"}},
                    "type": {"enum": ["collabAgentToolCall"]},
                },
            }]},
            "ServerNotification": {"oneOf": [
                {"properties": {"method": {"enum": ["rawResponse/completed"]}}},
                {"properties": {"method": {"enum": ["thread/tokenUsage/updated"]}}},
                {"properties": {"method": {"enum": ["model/rerouted"]}}},
            ]},
        },
    }
    schema_path = receipt_directory / "codex_app_server_protocol.v2.schemas.json"
    schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
    schema_sha256 = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    model = request["model"]
    effective_defaults = {
        "provider": model["provider"],
        "model": model["requested"],
        "reasoning_effort": model["reasoning_effort"],
    }
    provider_capabilities = {"imageGeneration": True, "namespaceTools": True, "webSearch": True}
    transcript = {
        "sent": [
            {"method": "initialize", "id": 1},
            {"method": "initialized"},
            {"method": "model/list", "id": 2},
            {
                "method": "config/read",
                "id": 3,
                "params": {"cwd": request["treatment_root"], "includeLayers": False},
            },
            {"method": "modelProvider/capabilities/read", "id": 4},
            {"method": "experimentalFeature/list", "id": 5},
            {"method": "account/read", "id": 6, "params": {"refreshToken": True}},
        ],
        "responses": [
            {"id": 1, "result": {"userAgent": "Codex Desktop/0.146.0 (test)", "platformFamily": "test", "platformOs": "test"}},
            {"id": 2, "result": {"model": {
                "id": "gpt-test",
                "model": model["requested"],
                "supportedReasoningEfforts": [model["reasoning_effort"]],
            }}},
            {"id": 3, "result": {"effectiveDefaults": effective_defaults}},
            {"id": 4, "result": {"capabilities": provider_capabilities}},
            {"id": 5, "result": {"multi_agent": {"name": "multi_agent", "enabled": True, "stage": "stable"}}},
            {"id": 6, "result": {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}},
        ],
    }
    transcript_sha256 = hashlib.sha256(
        json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    platform_version, platform_identity = next(iter(request["manifest"]["runtime"]["codex_platform_packages"].items()))
    binary_path = str((root / "fake-codex").resolve())
    process = {"returncode": 0, "stdout": {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}, "stderr": {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}}
    receipt = {
        "schema": "mycelium.swebench-live-runtime-preflight/v2",
        "status": "STRUCTURAL_READY_NO_MODEL_CALL",
        "completed_at": "2026-08-02T00:00:00Z",
        "package": {
            "name": "@openai/codex",
            "version": request["manifest"]["runtime"]["codex_version"],
            "integrity": request["manifest"]["runtime"]["codex_npm_integrity"],
            "process": process,
        },
        "platform_package": {
            "version": platform_version,
            "integrity": platform_identity["integrity"],
            "package_json_sha256": "a" * 64,
            "binary": {"path": binary_path, "bytes": Path(binary_path).stat().st_size, "sha256": platform_identity["binary_sha256"]},
            "process": process,
        },
        "runtime": {
            "command": [binary_path],
            "launcher_sha256": platform_identity["binary_sha256"],
            "version": "codex-cli " + request["manifest"]["runtime"]["codex_version"],
            "version_process": process,
        },
        "protocol_schema": {
            "artifact_path": "preflight/" + schema_path.name,
            "bytes": schema_path.stat().st_size,
            "sha256": schema_sha256,
            "requirements": {
                "collaboration_lineage": True,
                "effective_model_settings": True,
                "parent_thread_identity": True,
                "upstream_usage_dto_shape": True,
                "notification_methods": [
                    "thread/tokenUsage/updated", "model/rerouted",
                ],
            },
            "process": process,
        },
        "app_server": {
            "process": {"returncode": 0, "stderr": process["stderr"]},
            "responses": {
                "initialize_sha256": "a" * 64,
                "model_list_sha256": "b" * 64,
                "config_read_sha256": "c" * 64,
                "provider_capabilities_sha256": "d" * 64,
                "feature_list_sha256": "e" * 64,
                "account_read_sha256": "f" * 64,
            },
            "user_agent": "Codex Desktop/0.146.0 (test)",
            "platform_family": "test",
            "platform_os": "test",
            "requested_model_available": True,
            "reasoning_effort_supported": True,
            "effective_defaults": effective_defaults,
            "provider_capabilities": provider_capabilities,
            "revision_observation": "NOT_VERIFIED",
            "exact_upstream_usage_observation": "NOT_VERIFIED",
            "multi_agent": {"enabled": True, "stage": "stable"},
            "account": {"type": "chatgpt", "requires_openai_auth": True},
            "transcript": transcript,
            "transcript_sha256": transcript_sha256,
            "model_calls": 0,
            "turn_start_requests": 0,
        },
        "model": model,
        "model_calls": 0,
        "turn_start_requests": 0,
        "model_authorization": "NOT_VERIFIED",
        "model_canary_required": True,
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if mode == "preflight-mutate-input":
        Path(request["prompts"]["full"]["path"]).write_text("mutated\n", encoding="utf-8")
    if mode == "preflight-artifact-binding":
        receipt["protocol_schema"]["artifact_path"] = "other/" + schema_path.name
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps({key: value for key, value in receipt.items() if key != "receipt_sha256"}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    response = {
        "schema": "mycelium.swebench-live-preflight-response/v1",
        "request_sha256": request["request_sha256"],
        "pilot_manifest_sha256": request["pilot_manifest_sha256"],
        "receipt": receipt,
    }
    if mode == "preflight-binding":
        response["request_sha256"] = "0" * 64
    print(json.dumps(response, sort_keys=True))
    raise SystemExit(0)
run = request["run"]
full = run["arm"] == "full"
if mode == "delete-prior-trace":
    for prior_trace in Path(request["trace_path"]).parent.glob("*.trace.jsonl"):
        prior_trace.unlink()
trace_binding = request["runtime_binding"]
if mode == "runtime-binding":
    trace_binding = {**trace_binding, "native_binary_sha256": "0" * 64}
evidence = Path(request["trace_path"]).parent
resolved = run["arm"] != "vanilla"
native_results = (json.dumps({
    "error_ids": [], "incomplete_ids": [], "empty_patch_ids": [],
    "success_ids": [run["instance_id"]],
}, sort_keys=True) + "\n").encode()
native_report = (json.dumps({"resolved": resolved}, sort_keys=True) + "\n").encode()
native_evidence = []
for index, (source_path, data) in enumerate((
    ("process/stdout", b"official evaluator stdout\n"),
    ("process/stderr", b""),
    ("results.json", native_results),
    (run["instance_id"] + "/report.json", native_report),
), 1):
    native_path = evidence / f"{run['run_id']}.evaluator-{index:03d}.bin"
    native_path.write_bytes(data)
    native_evidence.append({
        "source_path": source_path,
        "path": native_path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
    })
grader_report = {
    "schema": "mycelium.swebench-live-normalized-grader-report/v2",
    "run_id": run["run_id"],
    "instance_id": run["instance_id"],
    "evaluator": request["evaluator"],
    "image_digest": request["task"]["image_digest"],
    "hidden_row_sha256": request["task"]["hidden_row_sha256"],
    "native_evidence": native_evidence,
    "resolved": resolved,
}
grader_bytes = (json.dumps(grader_report, sort_keys=True) + "\n").encode()
Path(request["grader_report_path"]).write_bytes(grader_bytes)
grader_digest = hashlib.sha256(grader_bytes).hexdigest()
response = {
    "run_id": run["run_id"],
    "session_id": "shared-session" if mode == "reuse-session" else "session-" + run["run_id"],
    "parent_session_id": "parent-" + run["run_id"] if full else None,
    "instance_id": run["instance_id"],
    "arm": run["arm"],
    "phase": run["phase"],
    "requested_model": request["model"]["requested"],
    "observed_model": request["model"]["requested"],
    "observed_provider": request["model"]["provider"],
    "observed_reasoning_effort": request["model"]["reasoning_effort"],
    "replicate": run["replicate"],
    "arm_order": run["arm_order"],
    "started_at": "2026-08-01T00:00:00Z",
    "ended_at": "2026-08-01T00:00:01Z",
    "input_tokens": 100,
    "cached_input_tokens": 10,
    "output_tokens": 50,
    "reasoning_tokens": 20,
    "cost_usd": "0.00015",
    "wall_time_seconds": 1.0,
    "steps": 2,
    "tool_calls": 1,
    "patch_sha256": grader_digest,
    "patch_files": ["solution.patch"],
    "spawn_count": 1 if full else 0,
    "peak_parallel_agents": 2 if full else 1,
    "durable_node_ids": ["apex-repo"] if full else [],
    "fresh_retrieval": full,
    "stem_ledger_sha256": grader_digest if full else None,
    "cap_verification": "PASS" if full else "NOT_APPLICABLE",
    "grader_report_sha256": grader_digest,
    "grader_resolved": resolved,
    "trace_sha256": "0" * 64,
}
if mode == "mismatch":
    response["arm"] = "vanilla" if run["arm"] != "vanilla" else "full"
elif mode == "cap":
    response["cost_usd"] = "5.01"
elif mode == "wall":
    response["wall_time_seconds"] = 1801
elif mode == "model-config":
    response["observed_reasoning_effort"] = "wrong"
elif mode == "extra":
    response["unexpected"] = True
usage = {
    "schema": "mycelium.swebench-live-provider-usage/v2",
    "run_id": run["run_id"],
    "model": response["observed_model"],
    "provider": response["observed_provider"],
    "reasoning_effort": response["observed_reasoning_effort"],
    "input_tokens": response["input_tokens"],
    "cached_input_tokens": response["cached_input_tokens"],
    "output_tokens": response["output_tokens"],
    "reasoning_tokens": response["reasoning_tokens"],
    "cost_usd": response["cost_usd"],
}
if mode == "usage-evidence":
    usage["input_tokens"] += 1
trajectory_path = evidence / f"{run['run_id']}.trajectory.json"
trajectory_path.write_text(json.dumps({"session_id": response["parent_session_id"] or response["session_id"]}) + "\n", encoding="utf-8")
runtime_artifacts = {
    "trajectory": {
        "path": trajectory_path.name,
        "sha256": hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
    },
    "rollouts": [],
}
for index in range(response["spawn_count"] + 1):
    rollout_path = evidence / f"{run['run_id']}.rollout-{index + 1:02d}.jsonl"
    rollout_path.write_text(json.dumps({"session": index}) + "\n", encoding="utf-8")
    runtime_artifacts["rollouts"].append({
        "path": rollout_path.name,
        "sha256": hashlib.sha256(rollout_path.read_bytes()).hexdigest(),
    })
linux_identity = request["runtime"]["codex_platform_packages"][
    request["runtime"]["codex_version"] + "-linux-x64"
]
trace = (
    "\n".join(json.dumps(event, sort_keys=True) for event in [
        {
            "schema": "mycelium.swebench-live-trace-header/v1",
            "run_id": run["run_id"],
            "runtime_binding": trace_binding,
        },
        {
            "schema": "mycelium.codex-pier-runtime/v1",
            "run_id": run["run_id"],
            "receipt": {
                "base_commit": request["task"]["base_commit"],
                "cap_verifier_python_ready": full,
                "codex_version": "codex-cli " + request["runtime"]["codex_version"],
                "mycelium_absent_at_start": True,
                "native_binary_sha256": linux_identity["binary_sha256"],
                "worktree_clean_at_start": True,
            },
            **runtime_artifacts,
        },
        usage,
        *[
            {
                "schema": "mycelium.swebench-live-trajectory-event/v1",
                "run_id": run["run_id"],
                "sequence": sequence,
                "kind": kind,
                "content_sha256": hashlib.sha256(
                    f"{run['run_id']}:{sequence}:{kind}".encode()
                ).hexdigest(),
            }
            for sequence, kind in enumerate(
                ["turn"] * response["steps"] + ["tool"] * response["tool_calls"]
            )
        ],
        {
            "schema": "mycelium.swebench-live-grader-receipt/v1",
            "run_id": run["run_id"],
            "instance_id": response["instance_id"],
            "evaluator": request["evaluator"],
            "image_digest": request["task"]["image_digest"],
            "hidden_row_sha256": request["task"]["hidden_row_sha256"],
            "grader_report_path": Path(request["grader_report_path"]).name,
            "grader_report_sha256": response["grader_report_sha256"],
            "grader_resolved": response["grader_resolved"],
        },
        {
            "schema": "mycelium.swebench-live-trace-complete/v1",
            "run_id": run["run_id"],
            "trajectory_events": response["steps"] + response["tool_calls"],
            "turn_events": response["steps"],
            "tool_events": response["tool_calls"],
        },
    ]) + "\n"
).encode()
if mode == "duplicate-header":
    header, remainder = trace.split(b"\n", 1)
    trace = header + b"\n" + header + b"\n" + remainder
Path(request["trace_path"]).write_bytes(trace)
response["trace_sha256"] = hashlib.sha256(trace).hexdigest()
if mode == "nonzero":
    raise SystemExit(7)
if mode == "malformed":
    print("not-json")
    raise SystemExit(0)
if mode == "trajectory-evidence":
    response["steps"] += 1
elif mode == "grader-report":
    Path(request["grader_report_path"]).write_text("tampered\n", encoding="utf-8")
elif mode == "native-grader":
    (evidence / native_evidence[0]["path"]).write_text("tampered\n", encoding="utf-8")
elif mode == "trace":
    response["trace_sha256"] = "a" * 64
elif mode == "missing":
    response.pop("trace_sha256")
elif mode == "duplicate":
    encoded = json.dumps(response, separators=(",", ":"))
    print(encoded[:-1] + ',"run_id":"duplicate"}')
    raise SystemExit(0)
elif mode == "mutate-input":
    Path(request["prompt"]["path"]).write_text("mutated\n", encoding="utf-8")
print(json.dumps(response, sort_keys=True))
'''


class EvaluatorRuntimeTest(unittest.TestCase):
    def test_requires_evaluator_dependencies_and_docker_before_model_calls(self):
        success = subprocess.CompletedProcess([], 0, b"ready", b"")
        failure = subprocess.CompletedProcess([], 1, b"", b"unavailable")
        with mock.patch.object(run_pilot.subprocess, "run", side_effect=(success, failure)) as run:
            with self.assertRaisesRegex(run_pilot.PilotRunError, "Docker daemon"):
                run_pilot._validate_evaluator_runtime(Path("frozen-evaluator"))
        self.assertEqual([sys.executable, "-c", "import evaluation.evaluation"], run.call_args_list[0].args[0])
        self.assertEqual(sys.executable, run.call_args_list[1].args[0][0])
        self.assertIn("docker.from_env", run.call_args_list[1].args[0][2])


class PilotRunnerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = pilot_fixture.PilotManifestTest("test_freeze_is_deterministic_balanced_and_validates")
        self.fixture.setUp()
        self.evaluator_root = self.fixture.root / "evaluator"
        self.evaluator_root.mkdir()
        subprocess.run(["git", "-C", str(self.evaluator_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.evaluator_root), "config", "user.email", "pilot@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.evaluator_root), "config", "user.name", "Pilot Test"], check=True)
        (self.evaluator_root / "evaluation").mkdir()
        (self.evaluator_root / "evaluation" / "evaluation.py").write_text(
            "# frozen evaluator\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.evaluator_root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.evaluator_root), "commit", "-qm", "evaluator"], check=True)
        evaluator_commit = subprocess.run(
            ["git", "-C", str(self.evaluator_root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.evaluator_identity = mock.patch.object(run_pilot.pilot, "EVALUATOR_COMMIT", evaluator_commit)
        self.evaluator_identity.start()
        self.evaluator_runtime = mock.patch.object(run_pilot, "_validate_evaluator_runtime")
        self.evaluator_runtime.start()
        self.task_rows = self.fixture.root / "task-rows"
        self.task_rows.mkdir()
        candidates = json.loads(self.fixture.candidates.read_text(encoding="utf-8"))
        candidates["evaluator_commit"] = evaluator_commit
        for candidate in candidates["candidates"]:
            row = {
                "instance_id": candidate["instance_id"],
                "repo": candidate["repository"],
                "base_commit": candidate["base_commit"],
                "problem_statement": f"Fix {candidate['instance_id']}",
                "FAIL_TO_PASS": ["hidden evaluator test"],
            }
            raw = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
            candidate["hidden_row_sha256"] = hashlib.sha256(raw).hexdigest()
            (self.task_rows / f"{candidate['hidden_row_sha256']}.json").write_bytes(raw)
        self.fixture.candidates.write_text(json.dumps(candidates), encoding="utf-8")
        self.runtime_binary = self.fixture.root / "fake-codex"
        self.runtime_binary.write_bytes(b"fake pinned codex binary")
        self.runtime_binary_sha256 = hashlib.sha256(self.runtime_binary.read_bytes()).hexdigest()
        self.platform_identity = mock.patch.dict(
            run_pilot.pilot.CODEX_PLATFORM_PACKAGES,
            {f"{run_pilot.pilot.CODEX_VERSION}-test": {
                "integrity": "sha512-test-platform",
                "binary_sha256": self.runtime_binary_sha256,
            }, f"{run_pilot.pilot.CODEX_VERSION}-linux-x64": {
                "integrity": "sha512-test-linux-platform",
                "binary_sha256": self.runtime_binary_sha256,
            }},
            clear=True,
        )
        self.platform_identity.start()
        self.fixture.runner.write_text(textwrap.dedent(FAKE_ADAPTER), encoding="utf-8")
        if os.name != "nt":
            self.fixture.runner.chmod(0o755)
        self.manifest = self.fixture._freeze()
        self.preregistration = self.fixture.root / "preregistration.json"
        preregistration = {
            "schema": run_pilot.PREREGISTRATION_SCHEMA,
            "uri": "https://example.test/preregistration/immutable-v1",
            "created_at": "2026-08-01T00:00:00Z",
            "pilot_manifest_sha256": self.manifest["manifest_sha256"],
            "schedule_sha256": self.manifest["schedule_sha256"],
            "arms": list(run_pilot.pilot.ARMS),
            "expected_runs": 30,
            "model": {
                field: self.manifest["model"][field]
                for field in ("provider", "requested", "revision", "reasoning_effort")
            },
            "cost_caps": self.manifest["cost_caps"],
            "cost_accounting": {
                "basis": "NOTIONAL_API_EQUIVALENT",
                "currency": "USD",
                "input_usd_per_million": "1.00",
                "cached_input_usd_per_million": "1.00",
                "output_usd_per_million": "1.00",
            },
            "stop_rules": list(run_pilot.STOP_RULES),
        }
        self.preregistration.write_text(
            json.dumps(preregistration, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.preregistration_sha256 = run_pilot._file_sha256(self.preregistration)
        self.output = self.fixture.root / "pilot-results.json"
        self.mode = self.fixture.root / "adapter-mode.txt"
        self.calls = self.fixture.root / "adapter-calls.jsonl"

    def tearDown(self):
        self.evaluator_runtime.stop()
        self.platform_identity.stop()
        self.evaluator_identity.stop()
        self.fixture.tearDown()

    def _run(self, output=None, *, authorize=True, accept_auth=None,
             canary_only=False, canary_receipt=None):
        if accept_auth is None:
            accept_auth = authorize
        return run_pilot.run(
            manifest_path=self.fixture.output,
            output_path=output or self.output,
            candidates_path=self.fixture.candidates,
            task_rows_path=self.task_rows,
            evaluator_root=self.evaluator_root,
            treatment_root=self.fixture.repo,
            installed_treatment=self.fixture.installed,
            prompt_paths=self.fixture.prompts,
            adapter_path=self.fixture.runner,
            preregistration_uri="https://example.test/preregistration/immutable-v1",
            preregistration_sha256=self.preregistration_sha256,
            preregistration_path=self.preregistration,
            authorize_model_calls=authorize,
            accept_task_container_auth_risk=accept_auth,
            canary_only=canary_only,
            canary_receipt=canary_receipt,
        )

    def test_runs_the_exact_frozen_schedule_and_writes_one_complete_artifact(self):
        canary_output = self.fixture.root / "canary-results.json"
        canary = self._run(canary_output, canary_only=True)
        self.assertEqual("MODEL_CANARY_COMPLETE_UNVERIFIED", canary["status"])
        self.assertEqual(1, canary["expected_runs"])
        self.assertEqual("full", canary["runs"][0]["arm"])
        self.assertEqual("canary", canary["runs"][0]["phase"])
        self.assertEqual(
            self.manifest["canary"]["task"]["instance_id"],
            canary["runs"][0]["instance_id"],
        )
        self.assertTrue(canary["runs"][0]["run_id"].startswith("canary-"))
        self.assertEqual(
            "EXPLICIT_MODEL_CANARY_ARGUMENT",
            canary["runtime_preflight"]["model_call_authorization"],
        )
        self.assertNotIn("diagnostics", canary)

        artifact = self._run(canary_receipt=canary_output)

        self.assertEqual("RUNS_COMPLETE_UNVERIFIED", artifact["status"])
        self.assertEqual(30, artifact["expected_runs"])
        self.assertEqual(30, len(artifact["runs"]))
        self.assertEqual(
            [
                (row["instance_id"], arm)
                for row in self.manifest["schedule"]
                for arm in row["arm_order"]
            ],
            [(row["instance_id"], row["arm"]) for row in artifact["runs"]],
        )
        self.assertEqual({"pilot"}, {row["phase"] for row in artifact["runs"]})
        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(33, len(calls))
        self.assertEqual(run_pilot.PREFLIGHT_REQUEST_SCHEMA, calls[0]["schema"])
        self.assertEqual(run_pilot.REQUEST_SCHEMA, calls[1]["schema"])
        canary_task = self.manifest["canary"]["task"]
        self.assertEqual(
            {
                "row_path": str((self.task_rows / f"{canary_task['hidden_row_sha256']}.json").resolve()),
                "row_sha256": canary_task["hidden_row_sha256"],
                "problem_statement": f"Fix {canary_task['instance_id']}",
                "image_digest": canary_task["image_digest"],
                "platform": "linux",
            },
            calls[1]["task_materialization"],
        )
        self.assertEqual(str(self.evaluator_root.resolve()), calls[1]["evaluator_root"])
        self.assertEqual(canary["runtime_binding"], calls[1]["runtime_binding"])
        self.assertEqual(self.manifest["cost_caps"]["max_total_usd"], calls[1]["batch_budget"]["max_total_usd"])
        self.assertEqual("0.00015", calls[3]["batch_budget"]["spent_usd_before_run"])
        self.assertEqual("5.00", calls[3]["batch_budget"]["effective_run_cap_usd"])
        self.assertEqual(
            {field: self.manifest["model"][field] for field in run_pilot.preflight.MODEL_FIELDS},
            calls[0]["model"],
        )
        self.assertEqual(artifact, json.loads(self.output.read_text(encoding="utf-8")))
        self.assertEqual("RETAINED_BINDING_VERIFIED", artifact["preregistration"]["validation"])
        self.assertEqual(self.manifest["runner_sha256"], artifact["adapter_sha256"])
        self.assertEqual(self.manifest["executor_sha256"], artifact["executor_sha256"])
        self.assertEqual(self.manifest["schedule_sha256"], artifact["schedule_sha256"])
        self.assertEqual(run_pilot.preflight.STATUS, artifact["runtime_preflight"]["status"])
        self.assertEqual(0, artifact["runtime_preflight"]["model_calls"])
        self.assertEqual(0, artifact["runtime_preflight"]["turn_start_requests"])
        self.assertTrue(artifact["runtime_preflight"]["model_canary_required"])
        self.assertEqual(
            "EXPLICIT_PILOT_ARGUMENT_WITH_VALIDATED_MODEL_CANARY",
            artifact["runtime_preflight"]["model_call_authorization"],
        )
        self.assertEqual("gpt-5.6-sol", artifact["model"]["observed"])
        self.assertEqual(
            "NOTIONAL_API_EQUIVALENT",
            json.loads(self.preregistration.read_text(encoding="utf-8"))["cost_accounting"]["basis"],
        )
        self.assertTrue({"model_calls", "coordinator_tokens", "handoff_tokens"}.isdisjoint(
            artifact["runs"][0]
        ))
        self.assertEqual(self.manifest["model"]["provider"], artifact["model"]["observed_provider"])
        self.assertEqual(
            self.manifest["model"]["reasoning_effort"],
            artifact["model"]["observed_reasoning_effort"],
        )
        self.assertNotEqual(1.0, artifact["runs"][0]["wall_time_seconds"])
        self.assertEqual(
            ["BASELINE_FLOOR", "NO_OBSERVED_ORCHESTRATION_VALUE"],
            artifact["diagnostics"]["stop_reasons"],
        )
        self.assertEqual("STOP_OR_RECALIBRATE", artifact["diagnostics"]["decision"])
        self.assertEqual(artifact["result_sha256"], run_pilot._document_sha256(artifact))
        evidence = Path(artifact["evidence_directory"])
        self.assertEqual(30, len(list(evidence.glob("*.trace.jsonl"))))
        self.assertEqual(30, len(list(evidence.glob("*.grader.json"))))
        self.assertEqual(120, len(list(evidence.glob("*.evaluator-*.bin"))))
        self.assertTrue((evidence / "runtime-preflight.json").is_file())
        self.assertEqual(
            self.preregistration_sha256,
            run_pilot._file_sha256(evidence / "preregistration.json"),
        )
        self.assertTrue((evidence / "preflight" / "codex_app_server_protocol.v2.schemas.json").is_file())
        records = [json.loads(line) for line in (evidence / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [
                "pilot_started",
                "preflight_started",
                "preflight_completed",
                "canary_receipt_validated",
                "model_calls_authorized",
                "run_started",
            ],
            [row["event"] for row in records[:6]],
        )
        self.assertEqual(
            artifact["journal_sha256"],
            run_pilot._file_sha256(evidence / "journal.jsonl"),
        )
        self.assertNotIn("lift", artifact)
        self.assertNotIn("score", artifact)

    def test_task_rows_and_evaluator_are_validated_before_preflight(self):
        task = self.manifest["canary"]["task"]
        row_path = self.task_rows / f"{task['hidden_row_sha256']}.json"
        original_row = row_path.read_bytes()
        row_path.write_bytes(original_row + b" ")
        with self.assertRaisesRegex(run_pilot.PilotRunError, "task row digest"):
            self._run(self.fixture.root / "bad-task-row.json", canary_only=True)
        self.assertFalse(self.calls.exists())
        row_path.write_bytes(original_row)

        evaluator_file = self.evaluator_root / "evaluation" / "evaluation.py"
        evaluator_file.write_text("# dirty evaluator\n", encoding="utf-8")
        with self.assertRaisesRegex(run_pilot.PilotRunError, "not clean"):
            self._run(self.fixture.root / "dirty-evaluator.json", canary_only=True)
        self.assertFalse(self.calls.exists())
        evaluator_file.write_text("# frozen evaluator\n", encoding="utf-8")

    def test_preflight_failure_emits_zero_run_requests(self):
        for mode in ("preflight-binding", "preflight-artifact-binding"):
            with self.subTest(mode=mode):
                self.mode.write_text(mode, encoding="utf-8")
                self.calls.unlink(missing_ok=True)
                output = self.fixture.root / f"results-{mode}.json"
                with self.assertRaises(run_pilot.PilotRunError):
                    self._run(output, canary_only=True)

                self.assertFalse(output.exists())
                self.assertEqual(1, len(self.calls.read_text(encoding="utf-8").splitlines()))
                journal = output.with_name(output.name + ".evidence") / "journal.jsonl"
                records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(
                    ["pilot_started", "preflight_started", "preflight_failed"],
                    [row["event"] for row in records],
                )
                self.assertEqual({"stdout", "stderr"}, set(records[-1]["process"]))

    def test_preregistration_must_be_retained_and_bound_before_preflight(self):
        original = self.preregistration.read_bytes()
        original_digest = self.preregistration_sha256
        for mode in ("digest", "schedule"):
            with self.subTest(mode=mode):
                self.calls.unlink(missing_ok=True)
                if mode == "digest":
                    self.preregistration.write_bytes(original + b" ")
                    self.preregistration_sha256 = original_digest
                else:
                    document = json.loads(original)
                    document["schedule_sha256"] = "0" * 64
                    self.preregistration.write_text(
                        json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
                    )
                    self.preregistration_sha256 = run_pilot._file_sha256(self.preregistration)
                with self.assertRaises(run_pilot.PilotRunError):
                    self._run(self.fixture.root / f"results-preregistration-{mode}.json")
                self.assertFalse(self.calls.exists())
                self.preregistration.write_bytes(original)
                self.preregistration_sha256 = original_digest

    def test_preflight_revalidates_frozen_inputs_before_run_one(self):
        self.mode.write_text("preflight-mutate-input", encoding="utf-8")
        with self.assertRaises(run_pilot.pilot.PilotError):
            self._run()
        self.assertEqual(1, len(self.calls.read_text(encoding="utf-8").splitlines()))
        journal = self.output.with_name(self.output.name + ".evidence") / "journal.jsonl"
        records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("preflight_failed", records[-1]["event"])

    def test_structural_preflight_does_not_authorize_model_calls(self):
        with self.assertRaises(run_pilot.PilotRunError):
            self._run(authorize=False)

        self.assertFalse(self.output.exists())
        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([run_pilot.PREFLIGHT_REQUEST_SCHEMA], [row["schema"] for row in calls])
        journal = self.output.with_name(self.output.name + ".evidence") / "journal.jsonl"
        records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("model_calls_not_authorized", records[-1]["event"])

    def test_direct_codex_requires_separate_task_container_auth_risk_acceptance(self):
        with self.assertRaisesRegex(run_pilot.PilotRunError, "auth exposure"):
            self._run(authorize=True, accept_auth=False, canary_only=True)
        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([run_pilot.PREFLIGHT_REQUEST_SCHEMA], [row["schema"] for row in calls])
        journal = self.output.with_name(self.output.name + ".evidence") / "journal.jsonl"
        records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("task_container_auth_risk_not_accepted", records[-1]["event"])

    def test_full_pilot_requires_a_model_canary_receipt(self):
        with self.assertRaises(run_pilot.PilotRunError):
            self._run()

        self.assertFalse(self.output.exists())
        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([run_pilot.PREFLIGHT_REQUEST_SCHEMA], [row["schema"] for row in calls])
        journal = self.output.with_name(self.output.name + ".evidence") / "journal.jsonl"
        records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("canary_receipt_failed", records[-1]["event"])

    def test_model_canary_notional_cost_reduces_the_publication_budget(self):
        self.manifest["cost_caps"]["max_total_usd"] = "0.00020"
        self.manifest["manifest_sha256"] = run_pilot.pilot.document_digest(self.manifest)
        self.fixture.output.write_text(json.dumps(self.manifest), encoding="utf-8")
        preregistration = json.loads(self.preregistration.read_text(encoding="utf-8"))
        preregistration["pilot_manifest_sha256"] = self.manifest["manifest_sha256"]
        preregistration["cost_caps"] = self.manifest["cost_caps"]
        self.preregistration.write_text(
            json.dumps(preregistration, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.preregistration_sha256 = run_pilot._file_sha256(self.preregistration)

        canary_output = self.fixture.root / "budget-canary.json"
        self._run(canary_output, canary_only=True)
        with self.assertRaisesRegex(run_pilot.PilotRunError, "remaining frozen publication budget"):
            self._run(self.fixture.root / "budget-pilot.json", canary_receipt=canary_output)

        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("0.00005", calls[-1]["batch_budget"]["effective_run_cap_usd"])

    def test_fails_closed_without_a_partial_artifact(self):
        for mode in (
            "mismatch", "cap", "wall", "trace", "missing", "extra", "duplicate",
            "model-config", "runtime-binding", "usage-evidence", "trajectory-evidence",
            "grader-report", "native-grader", "duplicate-header", "malformed", "nonzero",
        ):
            with self.subTest(mode=mode):
                self.mode.write_text(mode, encoding="utf-8")
                self.calls.unlink(missing_ok=True)
                output = self.fixture.root / f"results-{mode}.json"
                with self.assertRaises(run_pilot.PilotRunError):
                    self._run(output, canary_only=True)
                self.assertFalse(output.exists())
                self.assertEqual(2, len(self.calls.read_text(encoding="utf-8").splitlines()))
                journal = output.with_name(output.name + ".evidence") / "journal.jsonl"
                records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
                self.assertEqual("run_failed", records[-1]["event"])
                self.assertEqual({"stdout", "stderr"}, set(records[-1]["process"]))
                self.assertEqual(64, len(records[-1]["process"]["stdout"]["sha256"]))

    def test_fails_if_inputs_change_or_sessions_are_reused(self):
        canary_output = self.fixture.root / "batch-canary.json"
        self._run(canary_output, canary_only=True)
        self.calls.unlink(missing_ok=True)
        original_prompts = {arm: path.read_bytes() for arm, path in self.fixture.prompts.items()}
        for mode, expected_calls, final_event in (
            ("mutate-input", 2, "batch_failed"),
            ("reuse-session", 31, "batch_failed"),
            ("delete-prior-trace", 31, "batch_failed"),
        ):
            with self.subTest(mode=mode):
                self.mode.write_text(mode, encoding="utf-8")
                self.calls.unlink(missing_ok=True)
                output = self.fixture.root / f"results-{mode}.json"
                with self.assertRaises((run_pilot.PilotRunError, run_pilot.pilot.PilotError)):
                    self._run(
                        output,
                        canary_only=mode == "mutate-input",
                        canary_receipt=None if mode == "mutate-input" else canary_output,
                    )
                self.assertFalse(output.exists())
                self.assertEqual(expected_calls, len(self.calls.read_text(encoding="utf-8").splitlines()))
                journal = output.with_name(output.name + ".evidence") / "journal.jsonl"
                records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(final_event, records[-1]["event"])
                if mode == "mutate-input":
                    for arm, path in self.fixture.prompts.items():
                        path.write_bytes(original_prompts[arm])

    def test_diagnostics_stop_on_equal_prompt_full_and_task_floor_ceiling(self):
        def rows(outcomes):
            return [
                {
                    "instance_id": instance_id,
                    "arm": arm,
                    "grader_resolved": resolved,
                    "cost_usd": "0.01",
                }
                for instance_id, arms in outcomes.items()
                for arm, resolved in arms.items()
            ]

        equal_totals = run_pilot._diagnostics(rows({
            "a": {"vanilla": False, "prompt": True, "full": False},
            "b": {"vanilla": True, "prompt": False, "full": True},
        }))
        self.assertIn("NO_OBSERVED_ORCHESTRATION_VALUE", equal_totals["stop_reasons"])

        task_extremes = run_pilot._diagnostics(rows({
            "floor": {"vanilla": False, "prompt": False, "full": False},
            "ceiling": {"vanilla": True, "prompt": True, "full": True},
        }))
        self.assertIn("TASK_FLOOR", task_extremes["stop_reasons"])
        self.assertIn("TASK_CEILING", task_extremes["stop_reasons"])


if __name__ == "__main__":
    unittest.main()
