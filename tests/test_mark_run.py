#!/usr/bin/env python3
"""`bin/mycelium-mark-run.py`, the run-marker writer for the Claude adapter.

Nothing here spends a model call and nothing here writes under the
repository's own `.mycelium/`: every invocation runs with its working
directory and `CLAUDE_PROJECT_DIR` inside a temporary directory.

The last two tests are the ones that matter. The marker is only useful if the
Stop hook in `claude/hooks/mycelium_hook.py` reads what this helper writes, so
they drive the real gate against a real sealed run.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARK_RUN_PATH = ROOT / "bin" / "mycelium-mark-run.py"
HOOK_SCRIPT_PATH = ROOT / "claude" / "hooks" / "mycelium_hook.py"
NODE_PY_PATH = ROOT / "bin" / "mycelium.py"
LINEAGE_PATH = ROOT / "bin" / "mycelium_lineage.py"
SESSION_ID = "session-mark1"
RUN_ID = "mark-run"


class MarkRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        self.marker = (
            self.project / ".mycelium" / "claude" / SESSION_ID / "run.json"
        )

    def tearDown(self):
        self.temp.cleanup()

    def environment(self):
        values = dict(os.environ)
        values["CLAUDE_PROJECT_DIR"] = str(self.project)
        values["PYTHONDONTWRITEBYTECODE"] = "1"
        values.pop("CLAUDE_PLUGIN_ROOT", None)
        return values

    def mark_run(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", str(MARK_RUN_PATH), *arguments],
            cwd=str(self.project), env=self.environment(),
            check=False, capture_output=True, text=True,
        )

    def begin_marker(self, **overrides):
        arguments = {
            "--session-id": SESSION_ID,
            "--run-id": RUN_ID,
            "--goal": "mark goal",
            "--cap-file": "cap.txt",
            "--cap-node-path": ".mycelium/nodes/mark-cap.md",
        }
        arguments.update(overrides)
        flat = [part for pair in arguments.items() for part in pair]
        result = self.mark_run("begin", *flat)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def marker_record(self):
        return json.loads(self.marker.read_text(encoding="utf-8"))

    def run_stop(self):
        payload = {
            "session_id": SESSION_ID,
            "cwd": str(self.project),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        return subprocess.run(
            [sys.executable, "-B", str(HOOK_SCRIPT_PATH), "--event", "Stop"],
            input=json.dumps(payload),
            cwd=str(self.project), env=self.environment(),
            check=False, capture_output=True, text=True,
        )

    def test_begin_writes_the_snake_case_shape_the_gate_reads(self):
        result = self.begin_marker()
        self.assertEqual(self.marker.as_posix(), result.stdout.strip())
        record = self.marker_record()
        self.assertEqual(
            {"run_id", "goal", "cap_file", "cap_node_path", "state", "updated"},
            set(record),
        )
        self.assertEqual(RUN_ID, record["run_id"])
        self.assertEqual("mark goal", record["goal"])
        self.assertEqual("cap.txt", record["cap_file"])
        self.assertEqual(".mycelium/nodes/mark-cap.md", record["cap_node_path"])
        self.assertEqual("active", record["state"])
        self.assertRegex(record["updated"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_begin_leaves_cap_node_path_empty_when_it_is_not_supplied(self):
        result = self.mark_run(
            "begin", "--session-id", SESSION_ID, "--run-id", RUN_ID,
            "--goal", "mark goal", "--cap-file", "cap.txt",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", self.marker_record()["cap_node_path"])

    def test_begin_leaves_no_temporary_file_behind(self):
        self.begin_marker()
        self.assertEqual(
            ["run.json"], sorted(path.name for path in self.marker.parent.iterdir())
        )

    def test_done_sets_the_state_and_keeps_the_other_fields(self):
        self.begin_marker()
        before = self.marker_record()
        result = self.mark_run("done", "--session-id", SESSION_ID)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        record = self.marker_record()
        self.assertEqual("done", record["state"])
        for field in ("run_id", "goal", "cap_file", "cap_node_path"):
            self.assertEqual(before[field], record[field])

    def test_blocked_sets_the_state_and_records_the_reason(self):
        self.begin_marker()
        result = self.mark_run(
            "blocked", "--session-id", SESSION_ID, "--reason", "verifier unavailable",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        record = self.marker_record()
        self.assertEqual("blocked", record["state"])
        self.assertEqual("verifier unavailable", record["reason"])
        self.assertEqual(RUN_ID, record["run_id"])

    def test_done_refuses_when_no_marker_exists(self):
        result = self.mark_run("done", "--session-id", SESSION_ID)
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("no active run marker", result.stderr)
        self.assertIn("begin", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_blocked_refuses_when_no_marker_exists(self):
        result = self.mark_run(
            "blocked", "--session-id", SESSION_ID, "--reason", "never started",
        )
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("no active run marker", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_done_refuses_an_unreadable_marker_and_names_blocked_instead(self):
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("{ not json", encoding="utf-8")
        result = self.mark_run("done", "--session-id", SESSION_ID)
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("unreadable", result.stderr)
        self.assertIn("blocked --session-id", result.stderr)
        self.assertEqual("{ not json", self.marker.read_text(encoding="utf-8"))

    def test_blocked_repairs_an_unreadable_marker(self):
        # This is the exact command the Stop hook prints when it blocks on an
        # unreadable marker, so it has to work without the old contents.
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("{ not json", encoding="utf-8")
        result = self.mark_run(
            "blocked", "--session-id", SESSION_ID, "--reason", "marker was corrupt",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        record = self.marker_record()
        self.assertEqual("blocked", record["state"])
        self.assertEqual("marker was corrupt", record["reason"])

    def test_a_session_id_that_is_a_path_is_refused(self):
        result = self.mark_run(
            "begin", "--session-id", "../escape", "--run-id", RUN_ID,
            "--goal", "mark goal", "--cap-file", "cap.txt",
        )
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("--session-id", result.stderr)
        self.assertFalse((self.project.parent / "escape").exists())

    def test_every_subcommand_requires_a_session_id(self):
        for arguments in (
            ("begin", "--run-id", RUN_ID, "--goal", "g", "--cap-file", "cap.txt"),
            ("done",),
            ("blocked", "--reason", "why"),
        ):
            with self.subTest(command=arguments[0]):
                result = self.mark_run(*arguments)
                self.assertEqual(2, result.returncode)
                self.assertIn("--session-id", result.stderr)


class MarkRunGateTests(unittest.TestCase):
    """The marker this helper writes has to be the marker the gate reads."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    environment = MarkRunTests.environment
    mark_run = MarkRunTests.mark_run
    run_stop = MarkRunTests.run_stop

    def load_lineage(self):
        spec = importlib.util.spec_from_file_location(
            "mycelium_lineage_for_mark_run", LINEAGE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def begin_run(self):
        lineage = self.load_lineage()
        (self.project / "source.md").write_text(
            "apex fact\nstem fact\ncap fact\n", encoding="utf-8"
        )
        lineage.begin("mark goal", "apex-stem-cap", run_id=RUN_ID, root=self.project)
        return lineage

    def write_tracked_node(self, node_id, role, fact, source_ref, *inputs):
        arguments = [
            sys.executable, "-B", str(NODE_PY_PATH), "node",
            "mark goal", node_id, role, "complete", fact, "none", "next",
            "--topics", "topic", "--evidence", source_ref, "--confidence", "high",
            "--consumes", inputs[0] if inputs else "none", "--blocks", "none",
            "--source-refs", source_ref, "--run-id", RUN_ID,
        ]
        for process_input in inputs:
            arguments.extend(("--process-input", process_input))
        result = subprocess.run(
            arguments, cwd=str(self.project), check=False, capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def seal_and_cite(self, lineage):
        self.write_tracked_node("mark-apex", "apex", "apex fact", "source.md:1")
        self.write_tracked_node("mark-stem", "stem", "stem fact", "source.md:2", "mark-apex")
        self.write_tracked_node("mark-cap", "cap", "cap fact", "source.md:3", "mark-stem")
        report = lineage.seal(RUN_ID, root=self.project)
        self.assertEqual("sealed", report["state"])
        node_path = self.project / ".mycelium" / "nodes" / "mark-apex.md"
        line_number = next(
            index
            for index, line in enumerate(
                node_path.read_text(encoding="utf-8").splitlines(), 1
            )
            if line == "apex fact"
        )
        (self.project / "cap.txt").write_text(
            "Verification: EXTERNAL_REQUIRED\n"
            f"- [mark-apex] apex fact (.mycelium/nodes/mark-apex.md:{line_number})\n",
            encoding="utf-8",
        )

    def helper_begin(self):
        result = self.mark_run(
            "begin", "--session-id", SESSION_ID, "--run-id", RUN_ID,
            "--goal", "mark goal", "--cap-file", "cap.txt",
            "--cap-node-path", ".mycelium/nodes/mark-cap.md",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_the_gate_blocks_on_a_helper_marker_whose_run_is_still_open(self):
        self.begin_run()
        self.helper_begin()
        result = self.run_stop()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        reason = json.loads(result.stdout.strip().splitlines()[0])["reason"]
        self.assertIn(RUN_ID, reason)
        self.assertIn("not 'sealed'", reason)

    def test_the_gate_accepts_a_helper_marker_for_a_sealed_and_verified_run(self):
        lineage = self.begin_run()
        self.seal_and_cite(lineage)
        self.helper_begin()
        result = self.run_stop()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_the_gate_lets_a_helper_blocked_marker_stop_without_claiming_success(self):
        self.begin_run()
        self.helper_begin()
        blocked = self.mark_run(
            "blocked", "--session-id", SESSION_ID, "--reason", "goal not reached",
        )
        self.assertEqual(0, blocked.returncode, blocked.stderr)
        result = self.run_stop()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout.strip())
        self.assertIn("blocked", result.stderr)
        self.assertNotIn("PASS", result.stderr)


if __name__ == "__main__":
    unittest.main()
