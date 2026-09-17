#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).parent))
import codex_adapter


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.treatment = self.root / "treatment"
        self.treatment.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, arm="vanilla"):
        return {
            "authentication": {
                "transport": "PIER_CONTAINER_AUTH_JSON",
                "task_container_exposure_acknowledged": True,
            },
            "run": {
                "run_id": "pilot-task-r1-" + arm,
                "instance_id": "owner__repo-1",
                "arm": arm,
            },
            "task": {
                "base_commit": "a" * 40,
                "image_digest": "ghcr.io/example/task@sha256:" + "b" * 64,
                "hidden_row_sha256": "c" * 64,
            },
            "task_materialization": {"problem_statement": "Repair the supplied bug."},
            "model": {
                "provider": "openai",
                "requested": "gpt-5.5",
                "reasoning_effort": "high",
            },
            "cost_caps": {"max_wall_time_seconds": 900},
            "installed_treatment": str(self.treatment),
        }

    def test_preflight_delegates_to_the_zero_call_probe(self):
        request = {
            "authentication": {
                "transport": "PIER_CONTAINER_AUTH_JSON",
                "task_container_exposure_acknowledged": True,
            },
            "model": {"requested": "gpt-5.5"},
            "treatment_root": str(self.treatment),
            "receipt_directory": str(self.root / "receipts"),
            "request_sha256": "a" * 64,
            "pilot_manifest_sha256": "b" * 64,
        }
        receipt = {"schema": codex_adapter.preflight.SCHEMA, "model_canary_required": True}
        with mock.patch.object(codex_adapter, "_validate_pier_host") as host, \
                mock.patch.object(codex_adapter.preflight, "probe", return_value=receipt) as probe:
            response = codex_adapter._preflight(request)
        host.assert_called_once_with()
        probe.assert_called_once_with(
            request["model"],
            config_cwd=request["treatment_root"],
            artifact_directory=request["receipt_directory"],
        )
        self.assertIs(response["receipt"], receipt)
        self.assertEqual(response["request_sha256"], request["request_sha256"])

    def test_harbor_task_keeps_arms_isolated_and_binds_the_base_commit(self):
        vanilla = self.request("vanilla")
        vanilla_root = self.root / "vanilla"
        vanilla_root.mkdir()
        instruction = codex_adapter._write_harbor_task(vanilla_root, vanilla, "Frozen prompt\n")
        task_toml = (vanilla_root / "task.toml").read_text(encoding="utf-8")
        script = (vanilla_root / "pre_artifacts.sh").read_text(encoding="utf-8")
        config = codex_adapter._pier_config(vanilla, vanilla_root, self.root / "jobs-vanilla")
        self.assertNotIn("mycelium", instruction.lower())
        self.assertNotIn("cap-verification", task_toml)
        self.assertIn('test ! -e "$repo/.mycelium"', script)
        self.assertIn('diff --binary "$base"', script)
        self.assertIn(vanilla["task"]["base_commit"], script)
        self.assertNotIn("mounts", config["environment"])
        self.assertNotIn("skills_dir", config["agents"][0]["kwargs"])
        self.assertFalse(config["agents"][0]["kwargs"]["verify_cap"])
        self.assertNotIn("multi_agent", config["agents"][0]["kwargs"]["config_toml"])

        prompt = self.request("prompt")
        prompt_config = codex_adapter._pier_config(
            prompt, vanilla_root, self.root / "jobs-prompt"
        )
        self.assertNotIn("multi_agent", prompt_config["agents"][0]["kwargs"]["config_toml"])

        full = self.request("full")
        full_root = self.root / "full"
        full_root.mkdir()
        instruction = codex_adapter._write_harbor_task(full_root, full, "Frozen prompt\n")
        task_toml = (full_root / "task.toml").read_text(encoding="utf-8")
        script = (full_root / "pre_artifacts.sh").read_text(encoding="utf-8")
        config = codex_adapter._pier_config(full, full_root, self.root / "jobs-full")
        self.assertIn(".mycelium/evidence/cap.txt", instruction)
        self.assertIn("actual Git root", instruction)
        self.assertNotIn("/testbed/.mycelium", instruction)
        self.assertIn("cap-verification.txt", task_toml)
        self.assertIn("/mycelium-verifier/verify-cap.py", script)
        self.assertTrue(config["environment"]["mounts"][0]["read_only"])
        self.assertEqual(config["environment"]["mounts"][1]["source"], str(
            codex_adapter.pilot.CAP_VERIFIER_PATH.resolve()
        ))
        self.assertEqual(config["agents"][0]["kwargs"]["skills_dir"], "/mycelium-skills")
        self.assertTrue(config["agents"][0]["kwargs"]["verify_cap"])
        self.assertIn("multi_agent = true", config["agents"][0]["kwargs"]["config_toml"])
        self.assertTrue(config["environment"]["delete"])

    def test_trajectory_extracts_observed_root_usage(self):
        document = {
            "schema_version": "ATIF-v1.7",
            "agent": {"version": codex_adapter.pilot.CODEX_VERSION, "model_name": "gpt-5.5"},
            "steps": [
                {"source": "user", "message": "task"},
                {"source": "agent", "message": "work", "tool_calls": [
                    {"name": "command_execution", "arguments": {"command": "git status"}},
                ]},
            ],
            "final_metrics": {
                "total_prompt_tokens": 120,
                "total_cached_tokens": 20,
                "total_completion_tokens": 40,
                "extra": {"reasoning_output_tokens": 10},
            },
        }
        steps, tools, usage = codex_adapter._trajectory_evidence(document)
        self.assertEqual((len(steps), len(tools)), (1, 1))
        self.assertEqual(usage, {
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "output_tokens": 40,
            "reasoning_tokens": 10,
        })

    def test_rollouts_aggregate_root_and_child_usage_with_parent_lineage(self):
        root_session = "01900000-0000-7000-8000-000000000000"
        root_turn = "01900000-0001-7000-8000-000000000000"
        child_session = "01900000-0002-7000-8000-000000000000"
        child_turn = "01900000-0003-7000-8000-000000000000"

        def metadata(session_id, source):
            return {"type": "session_meta", "payload": {
                "id": session_id, "session_id": root_session, "source": source,
                "model_provider": "openai",
            }}

        def boundary(kind, turn_id, timestamp):
            return {"type": "event_msg", "timestamp": timestamp, "payload": {
                "type": kind, "turn_id": turn_id,
            }}

        def context(turn_id, timestamp):
            return {"type": "turn_context", "timestamp": timestamp, "payload": {
                "turn_id": turn_id, "model": "gpt-5.5", "effort": "high",
            }}

        def tool(call_id, timestamp):
            return {"type": "response_item", "timestamp": timestamp, "payload": {
                "type": "function_call", "call_id": call_id, "name": "send_message",
            }}

        def tokens(usage, timestamp):
            return {"type": "event_msg", "timestamp": timestamp, "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": usage[0], "cached_input_tokens": usage[1],
                    "output_tokens": usage[2], "reasoning_output_tokens": usage[3],
                }},
            }}

        root_documents = [
            metadata(root_session, "exec"),
            boundary("task_started", root_turn, "2026-08-03T00:00:00Z"),
            context(root_turn, "2026-08-03T00:00:00.100Z"),
            tool("root-call", "2026-08-03T00:00:01Z"),
            tokens((100, 20, 40, 10), "2026-08-03T00:00:09Z"),
            boundary("task_complete", root_turn, "2026-08-03T00:00:10Z"),
        ]
        child_documents = [
            metadata(child_session, {"subagent": {"thread_spawn": {
                "parent_thread_id": root_session,
            }}}),
            *root_documents,
            boundary("task_started", child_turn, "2026-08-03T00:00:02Z"),
            context(child_turn, "2026-08-03T00:00:02.100Z"),
            tool("child-call", "2026-08-03T00:00:03Z"),
            tokens((150, 25, 60, 15), "2026-08-03T00:00:07Z"),
            tokens((150, 25, 60, 15), "2026-08-03T00:00:07.100Z"),
            boundary("task_complete", child_turn, "2026-08-03T00:00:08Z"),
        ]
        paths = []
        for name, documents in (("root.jsonl", root_documents), ("child.jsonl", child_documents)):
            path = self.root / name
            path.write_text("\n".join(json.dumps(row) for row in documents) + "\n", encoding="utf-8")
            paths.append(path)

        steps, tools, usage, root_usage, receivers, peak, identity = codex_adapter._rollout_evidence(
            paths, root_session
        )
        self.assertEqual((len(steps), len(tools)), (2, 2))
        self.assertEqual(usage, {
            "input_tokens": 150, "cached_input_tokens": 25,
            "output_tokens": 60, "reasoning_tokens": 15,
        })
        self.assertEqual(root_usage["input_tokens"], 100)
        self.assertEqual(receivers, [child_session])
        self.assertEqual(peak, 2)
        self.assertEqual(identity, {
            "model": "gpt-5.5", "provider": "openai", "reasoning_effort": "high",
        })

    def test_request_validation_accepts_official_hidden_row_fields(self):
        with self.assertRaisesRegex(codex_adapter.AdapterError, "explicitly acknowledged"):
            codex_adapter._validate_request_inputs({})
        row = {
            "instance_id": "owner__repo-1",
            "repo": "owner/repo",
            "base_commit": "a" * 40,
            "problem_statement": "Repair the supplied bug.",
            "FAIL_TO_PASS": ["hidden test"],
            "test_patch": "hidden patch",
        }
        row_path = self.root / "row.json"
        row_path.write_text(json.dumps(row), encoding="utf-8")
        row_sha = hashlib.sha256(row_path.read_bytes()).hexdigest()
        evaluator = self.root / "evaluator"
        (evaluator / "evaluation").mkdir(parents=True)
        (evaluator / "evaluation" / "evaluation.py").write_text("# evaluator\n", encoding="utf-8")
        prompt = self.root / "prompt.md"
        prompt.write_bytes(b"Frozen prompt\n")
        request = {
            "authentication": {
                "transport": "PIER_CONTAINER_AUTH_JSON",
                "task_container_exposure_acknowledged": True,
            },
            "task": {
                "instance_id": row["instance_id"],
                "repository": row["repo"],
                "base_commit": row["base_commit"],
                "hidden_row_sha256": row_sha,
            },
            "task_materialization": {
                "row_path": str(row_path),
                "row_sha256": row_sha,
                "problem_statement": row["problem_statement"],
            },
            "evaluator_root": str(evaluator),
            "prompt": {
                "path": str(prompt),
                "sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
            },
        }

        def fake_git(_root, *args):
            return b"frozen-evaluator\n" if args == ("rev-parse", "HEAD") else b""

        with mock.patch.object(codex_adapter, "_git", side_effect=fake_git), \
                mock.patch.object(codex_adapter.pilot, "EVALUATOR_COMMIT", "frozen-evaluator"):
            actual_row, prompt_text, actual_evaluator = codex_adapter._validate_request_inputs(request)
        self.assertEqual(actual_row, row)
        self.assertEqual(prompt_text, "Frozen prompt\n")
        self.assertEqual(actual_evaluator, evaluator)

    def test_official_evaluator_is_a_separate_fail_closed_step(self):
        evaluator = self.root / "evaluator"
        evaluator.mkdir()
        grader_path = self.root / "evidence" / "run.grader.json"
        grader_path.parent.mkdir()
        request = self.request()
        request.update({
            "evaluator_root": str(evaluator),
            "evaluator": {"repository": "microsoft/SWE-bench-Live", "commit": "frozen"},
            "grader_report_path": str(grader_path),
        })
        row = {"instance_id": request["run"]["instance_id"], "test_patch": "hidden"}

        def evaluator_run(command, **_kwargs):
            output = Path(command[command.index("--output_dir") + 1])
            instance_id = request["run"]["instance_id"]
            (output / instance_id).mkdir(parents=True)
            (output / "results.json").write_text(json.dumps({
                "error_ids": [], "incomplete_ids": [], "empty_patch_ids": [],
                "success_ids": [instance_id],
            }), encoding="utf-8")
            (output / instance_id / "report.json").write_text(
                json.dumps({"resolved": True}), encoding="utf-8"
            )
            (output / "evaluator.log").write_bytes(b"native evaluator log\n")
            return subprocess.CompletedProcess(
                command, 0, b"official evaluator stdout\n", b"official evaluator stderr\n"
            )

        with mock.patch.object(codex_adapter.subprocess, "run", side_effect=evaluator_run) as run:
            report, digest = codex_adapter._run_evaluator(request, row, "diff --git a/a b/a\n", self.root)
        self.assertTrue(report["resolved"])
        self.assertEqual(digest, hashlib.sha256(grader_path.read_bytes()).hexdigest())
        self.assertEqual(
            {receipt["source_path"] for receipt in report["native_evidence"]},
            {
                "process/stdout", "process/stderr", "results.json", "evaluator.log",
                f"{request['run']['instance_id']}/report.json",
            },
        )
        for receipt in report["native_evidence"]:
            retained = grader_path.parent / receipt["path"]
            self.assertEqual(receipt["sha256"], hashlib.sha256(retained.read_bytes()).hexdigest())
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "-m", "evaluation.evaluation"])
        prediction = json.loads((self.root / "prediction.json").read_text(encoding="utf-8"))
        self.assertEqual(prediction[request["run"]["instance_id"]]["model_patch"], "diff --git a/a b/a\n")

    def test_full_arm_requires_retained_external_cap_pass(self):
        evidence = self.root / "jobs" / "artifacts"
        mycelium = evidence / "mycelium"
        (mycelium / "nodes").mkdir(parents=True)
        (mycelium / "flows").mkdir()
        (mycelium / "nodes" / "node-one.md").write_text("durable\n", encoding="utf-8")
        ledger = mycelium / "flows" / "stem-ledger-1.json"
        ledger.write_text(json.dumps({
            "selected_nodes": ["node-one"],
            "conflicts": [],
            "cap_dispatch": "verify selected facts",
        }), encoding="utf-8")
        receipt = evidence / "cap-verification.txt"
        receipt.write_text("Verification: PASS\nVerified nodes: 1\n", encoding="utf-8")
        nodes, fresh, ledger_sha, status = codex_adapter._mycelium_evidence(self.root / "jobs", "full")
        self.assertEqual((nodes, fresh, status), (["node-one"], True, "PASS"))
        self.assertEqual(ledger_sha, hashlib.sha256(ledger.read_bytes()).hexdigest())
        receipt.write_text("Verification: FAIL\n", encoding="utf-8")
        with self.assertRaisesRegex(codex_adapter.AdapterError, "passing external CAP"):
            codex_adapter._mycelium_evidence(self.root / "jobs", "full")


if __name__ == "__main__":
    unittest.main()
