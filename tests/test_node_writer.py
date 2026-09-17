import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium.py"
LINEAGE_MODULE_PATH = MODULE_PATH.with_name("mycelium_lineage.py")
VERIFY_CAP_PATH = MODULE_PATH.parents[0].parent / "evals" / "verify-cap.py"
SPEC = importlib.util.spec_from_file_location("mycelium_cli", MODULE_PATH)
mycelium = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mycelium)


def load_lineage():
    spec = importlib.util.spec_from_file_location("mycelium_lineage_for_writer", LINEAGE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cap_verifier(path=VERIFY_CAP_PATH):
    spec = importlib.util.spec_from_file_location("mycelium_cap_verifier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NodeWriterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        Path("bin").mkdir()
        shutil.copyfile(LINEAGE_MODULE_PATH, Path("bin/mycelium_lineage.py"))

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def run_node(self, *arguments, module_path=MODULE_PATH):
        return subprocess.run(
            [sys.executable, str(module_path), "node", *arguments],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )

    def write_complete_node(self, run_id, node_id, role, fact, source_ref, *process_inputs):
        arguments = [
            "goal", node_id, role, "complete", fact, "none", "next",
            "--topics", "topic", "--evidence", source_ref, "--confidence", "high",
            "--consumes", process_inputs[0] if process_inputs else "none",
            "--blocks", "none", "--source-refs", source_ref, "--run-id", run_id,
        ]
        for process_input in process_inputs:
            arguments.extend(("--process-input", process_input))
        result = self.run_node(*arguments)
        self.assertEqual(0, result.returncode, result.stderr)

    def cap_file(self, filename, node_id, fact):
        node_path = Path(f".mycelium/nodes/{node_id}.md")
        line_number = next(
            index
            for index, line in enumerate(node_path.read_text(encoding="utf-8").splitlines(), 1)
            if line == fact
        )
        path = Path(filename)
        path.write_text(
            f"Verification: EXTERNAL_REQUIRED\n- [{node_id}] {fact} "
            f"(.mycelium/nodes/{node_id}.md:{line_number})\n",
            encoding="utf-8",
        )
        return path

    def run_cap_verifier(self, cap_file, run_id=None):
        arguments = [sys.executable, str(VERIFY_CAP_PATH), ".", str(cap_file)]
        if run_id is not None:
            arguments.extend(("--run-id", run_id))
        return subprocess.run(
            arguments,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )

    def test_untracked_cli_stays_legacy_and_does_not_create_a_run(self):
        result = self.run_node(
            "goal", "legacy", "apex", "alive", "--allow-untracked", "legacy path"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(".mycelium/flows/goal.jsonl", result.stdout.strip())
        self.assertFalse(Path(".mycelium/runs").exists())
        self.assertIn("\nversion: 1\n", Path(".mycelium/nodes/legacy.md").read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_posix_writer_publishes_verifier_readable_artifacts(self):
        previous_umask = os.umask(0o077)
        try:
            result = self.run_node(
                "goal", "public", "apex", "alive", "--allow-untracked", "permission contract"
            )
        finally:
            os.umask(previous_umask)
        self.assertEqual(0, result.returncode, result.stderr)

        for directory in (
            Path(".mycelium"),
            Path(".mycelium/flows"),
            Path(".mycelium/nodes"),
        ):
            self.assertEqual(0o755, directory.stat().st_mode & 0o777)
        for artifact in (
            Path(".mycelium/flows/goal.jsonl"),
            Path(".mycelium/nodes/public.md"),
        ):
            self.assertEqual(0o644, artifact.stat().st_mode & 0o777)

    def test_cap_verifier_ignores_treatment_controlled_lineage_module(self):
        marker = Path("workspace-lineage-executed")
        Path("bin/mycelium_lineage.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker.resolve())!r}).write_text('executed', encoding='utf-8')\n"
            "def verify_snapshot(*args, **kwargs):\n"
            "    return {\n"
            "        'report': {'valid': True, 'state': 'sealed', 'diagnostics': []},\n"
            "        'manifest': {'nodes': {}},\n"
            "    }\n",
            encoding="utf-8",
        )
        cap_path = Path("cap.txt")
        cap_path.write_text(
            "Verification: EXTERNAL_REQUIRED\n"
            "- [forged] forged fact (.mycelium/nodes/forged.md:1)\n",
            encoding="utf-8",
        )

        with tempfile.TemporaryDirectory() as evaluator_temp:
            evaluator = Path(evaluator_temp)
            verifier_path = evaluator / "verify-cap.py"
            lineage_path = evaluator / "mycelium_lineage.py"
            shutil.copyfile(VERIFY_CAP_PATH, verifier_path)
            shutil.copyfile(LINEAGE_MODULE_PATH, lineage_path)

            verifier = load_cap_verifier(verifier_path)
            loaded = verifier.load_lineage_module()
            self.assertEqual(lineage_path.resolve(), Path(loaded.__file__).resolve())

            result = subprocess.run(
                [
                    sys.executable,
                    str(verifier_path),
                    str(Path.cwd()),
                    str(cap_path),
                    "--run-id",
                    "missing-run",
                ],
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
            )

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("process run directory does not exist", result.stdout)
        self.assertFalse(marker.exists())

    def test_cap_membership_uses_the_locked_verification_snapshot(self):
        verifier = load_cap_verifier()
        lineage_module = mock.Mock()
        lineage_module.verify_snapshot.return_value = {
            "report": {"valid": True, "state": "sealed", "diagnostics": []},
            "manifest": {
                "nodes": {
                    "a": {
                        "status": "committed",
                        "commit": {"node": {"path": ".mycelium/nodes/a.md"}},
                    }
                }
            },
        }
        with mock.patch.object(
            verifier,
            "load_lineage_module",
            return_value=lineage_module,
        ):
            error = verifier.verify_process_run(
                Path.cwd(),
                "tracked",
                [("a", ".mycelium/nodes/a.md")],
            )

        self.assertIsNone(error)
        lineage_module.verify_snapshot.assert_called_once_with(
            "tracked",
            root=Path.cwd(),
        )
        lineage_module.load_manifest.assert_not_called()

    def test_tracked_cli_finalizes_once_and_exact_retry_is_idempotent(self):
        lineage = load_lineage()
        lineage.begin("goal", run_id="tracked", root=Path.cwd())

        arguments = (
            "goal", "a", "apex", "complete", "fact", "none", "next",
            "--topics", "topic", "--evidence", "evidence", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--run-id", "tracked",
        )
        first = self.run_node(*arguments)
        self.assertEqual(0, first.returncode, first.stderr)
        node_before = Path(".mycelium/nodes/a.md").read_bytes()
        flow_before = Path(".mycelium/flows/goal.jsonl").read_bytes()
        manifest_before = Path(".mycelium/runs/tracked.json").read_bytes()
        manifest = lineage.load_manifest("tracked", root=Path.cwd())
        registered = manifest["nodes"]["a"]
        self.assertEqual("committed", registered["status"])
        self.assertIn("\nversion: 2\n", Path(".mycelium/nodes/a.md").read_text(encoding="utf-8"))

        replay = self.run_node(*arguments)
        self.assertEqual(0, replay.returncode, replay.stderr)
        self.assertEqual(first.stdout, replay.stdout)
        self.assertEqual(node_before, Path(".mycelium/nodes/a.md").read_bytes())
        self.assertEqual(flow_before, Path(".mycelium/flows/goal.jsonl").read_bytes())
        self.assertEqual(manifest_before, Path(".mycelium/runs/tracked.json").read_bytes())

        changed_content = self.run_node(
            "goal", "a", "apex", "complete", "changed fact", "none", "next",
            "--topics", "topic", "--evidence", "evidence", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--run-id", "tracked",
        )
        self.assertNotEqual(0, changed_content.returncode)
        self.assertIn("conflicting committed retry", changed_content.stderr)
        self.assertEqual(node_before, Path(".mycelium/nodes/a.md").read_bytes())
        self.assertEqual(flow_before, Path(".mycelium/flows/goal.jsonl").read_bytes())
        self.assertEqual(manifest_before, Path(".mycelium/runs/tracked.json").read_bytes())

        changed_provenance = self.run_node(
            "goal", "a", "apex", "complete", "fact", "none", "next",
            "--topics", "topic", "--evidence", "evidence", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--run-id", "tracked",
            "--producing-agent", "different-agent",
        )
        self.assertNotEqual(0, changed_provenance.returncode)
        self.assertIn("conflicting committed retry", changed_provenance.stderr)
        self.assertEqual(node_before, Path(".mycelium/nodes/a.md").read_bytes())
        self.assertEqual(flow_before, Path(".mycelium/flows/goal.jsonl").read_bytes())
        self.assertEqual(manifest_before, Path(".mycelium/runs/tracked.json").read_bytes())

        conflict = self.run_node(
            "goal", "a", "septum", "complete", "fact", "none", "next",
            "--topics", "topic", "--evidence", "evidence", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--run-id", "tracked",
            "--process-input", "a",
        )
        self.assertNotEqual(0, conflict.returncode)
        self.assertIn("conflicting reservation", conflict.stderr)
        self.assertEqual(node_before, Path(".mycelium/nodes/a.md").read_bytes())
        self.assertEqual(flow_before, Path(".mycelium/flows/goal.jsonl").read_bytes())

    def test_tracked_write_failure_leaves_a_pending_reservation(self):
        lineage = load_lineage()
        lineage.begin("goal", run_id="pending", root=Path.cwd())
        result = self.run_node(
            "goal",
            "a",
            "apex",
            "complete",
            "fact",
            "none",
            "next",
            "--topics",
            "topic",
            "--evidence",
            "evidence",
            "--confidence",
            "high",
            "--consumes",
            "none",
            "--blocks",
            "none",
            "--run-id",
            "pending",
            "--depends-on",
            "seed\nstatus: complete",
        )
        self.assertNotEqual(0, result.returncode)
        manifest = lineage.load_manifest("pending", root=Path.cwd())
        reserved = manifest["nodes"]["a"]
        self.assertEqual("pending", reserved["status"])
        self.assertFalse(Path(".mycelium/nodes/a.md").exists())
        self.assertFalse(Path(".mycelium/flows/goal.jsonl").exists())

    def test_tracked_cli_preflights_lineage_before_any_legacy_write(self):
        isolated_bin = Path("isolated-bin")
        isolated_bin.mkdir()
        isolated_writer = isolated_bin / "mycelium.py"
        shutil.copyfile(MODULE_PATH, isolated_writer)
        result = self.run_node(
            "goal", "a", "apex", "alive", "--run-id", "missing-core", module_path=isolated_writer
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("tracked node writes require bin/mycelium_lineage.py", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(Path(".mycelium").exists())

    def test_cap_verifier_requires_a_sealed_current_run_and_tracked_selected_nodes(self):
        lineage = load_lineage()
        Path("source.md").write_text(
            "apex fact\nseptum fact\nhyphae fact\nstem fact\ncap fact\nlegacy fact\n",
            encoding="utf-8",
        )
        lineage.begin("goal", run_id="sealed", root=Path.cwd())
        self.write_complete_node("sealed", "a", "apex", "apex fact", "source.md:1")
        self.write_complete_node("sealed", "s", "septum", "septum fact", "source.md:2", "a")
        self.write_complete_node("sealed", "h", "hyphae", "hyphae fact", "source.md:3", "s")
        self.write_complete_node("sealed", "stem", "stem", "stem fact", "source.md:4", "h")
        self.write_complete_node("sealed", "cap", "cap", "cap fact", "source.md:5", "stem")
        lineage.seal("sealed", root=Path.cwd())

        cap_file = self.cap_file("cap.txt", "a", "apex fact")
        missing_run = self.run_cap_verifier(cap_file)
        self.assertNotEqual(0, missing_run.returncode)
        self.assertIn("schema-v2 selected nodes require --run-id", missing_run.stdout)

        accepted = self.run_cap_verifier(cap_file, "sealed")
        self.assertEqual(0, accepted.returncode, accepted.stdout + accepted.stderr)
        self.assertIn("Verification: PASS", accepted.stdout)
        self.assertIn("Verified run: sealed", accepted.stdout)

        alias_path = Path(".mycelium/nodes/alias.md")
        shutil.copyfile(".mycelium/nodes/a.md", alias_path)
        alias_line = next(
            index
            for index, line in enumerate(alias_path.read_text(encoding="utf-8").splitlines(), 1)
            if line == "apex fact"
        )
        alias_cap = Path("alias-cap.txt")
        alias_cap.write_text(
            "Verification: EXTERNAL_REQUIRED\n"
            f"- [a] apex fact (.mycelium/nodes/alias.md:{alias_line})\n",
            encoding="utf-8",
        )
        aliased = self.run_cap_verifier(alias_cap, "sealed")
        self.assertNotEqual(0, aliased.returncode)
        self.assertIn("does not match process commit path", aliased.stdout)

        legacy = self.run_node(
            "goal", "legacy", "apex", "complete", "legacy fact", "none", "next",
            "--topics", "topic", "--evidence", "source.md:6", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--source-refs", "source.md:6",
            "--allow-untracked", "legacy CAP fixture",
        )
        self.assertEqual(0, legacy.returncode, legacy.stderr)
        legacy_cap = self.cap_file("legacy-cap.txt", "legacy", "legacy fact")
        legacy_accepted = self.run_cap_verifier(legacy_cap)
        self.assertEqual(
            0,
            legacy_accepted.returncode,
            legacy_accepted.stdout + legacy_accepted.stderr,
        )
        self.assertIn("Verification: PASS", legacy_accepted.stdout)

        untracked = self.run_cap_verifier(legacy_cap, "sealed")
        self.assertNotEqual(0, untracked.returncode)
        self.assertIn("not registered in process run", untracked.stdout)

        lineage.begin("goal", run_id="pending", root=Path.cwd())
        lineage.reserve("pending", "waiting", "apex", [], root=Path.cwd())
        pending = self.run_cap_verifier(cap_file, "pending")
        self.assertNotEqual(0, pending.returncode)
        self.assertIn("pending reservation", pending.stdout)

        node_path = Path(".mycelium/nodes/a.md")
        node_path.write_text(node_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
        tampered = self.run_cap_verifier(cap_file, "sealed")
        self.assertNotEqual(0, tampered.returncode)
        self.assertIn("process run sealed is not sealed and current", tampered.stdout)

    def test_rejects_frontmatter_injection_before_writing(self):
        with self.assertRaisesRegex(ValueError, "single line"):
            mycelium.write_node(
                "goal", "blocked-node", "hyphae", "blocked",
                depends_on="seed\nstatus: complete",
            )
        self.assertFalse(Path(".mycelium").exists())

        with self.assertRaisesRegex(ValueError, "duplicate metadata key"):
            mycelium.metadata("---\nstatus: blocked\nstatus: complete\n---\n")

    def test_node_failure_cannot_publish_selectable_state(self):
        with mock.patch.object(mycelium.os, "replace", side_effect=OSError("simulated node failure")):
            with self.assertRaisesRegex(OSError, "simulated node failure"):
                mycelium.write_node("goal", "node", "apex", "alive")
        self.assertFalse(Path(".mycelium/nodes/node.md").exists())
        self.assertEqual([], mycelium.load_nodes())

    def test_flow_failure_cannot_publish_a_selectable_node(self):
        with mock.patch.object(mycelium.Path, "open", side_effect=OSError("simulated flow failure")):
            with self.assertRaisesRegex(OSError, "simulated flow failure"):
                mycelium.write_node("goal", "orphan", "apex", "alive")
        self.assertFalse(Path(".mycelium/nodes/orphan.md").exists())
        self.assertEqual([], mycelium.load_nodes())
        mycelium.build_index()
        self.assertEqual([], json.loads(Path(".mycelium/index.json").read_text(encoding="utf-8")))

    def test_flow_reader_rejects_non_object_json(self):
        Path(".mycelium/flows").mkdir(parents=True)
        Path(".mycelium/flows/invalid.jsonl").write_text("[]\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "flow record must be a JSON object"):
            mycelium.flow_commits()

    def test_flow_lists_are_arrays(self):
        mycelium.write_node("goal", "node", "apex", "alive", facts="one")
        record = json.loads(Path(".mycelium/flows/goal.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(["one"], record["facts"])
        self.assertEqual([], record["questions"])
        self.assertEqual([], record["next"])

    def test_flow_identity_rejects_lossy_timestamps_and_scalar_types(self):
        first = mycelium.canonical_timestamp("2026-07-31T12:00:00.1234567Z")
        second = mycelium.canonical_timestamp("2026-07-31T12:00:00.1234566Z")
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(ValueError, "invalid flow timestamp"):
            mycelium.canonical_timestamp("2026-07-31T12:00:00+15:00")
        with self.assertRaisesRegex(ValueError, "invalid flow timestamp"):
            mycelium.canonical_timestamp("2026-07-31t12:00:00z")
        with self.assertRaisesRegex(ValueError, "invalid flow timestamp"):
            mycelium.canonical_timestamp("0001-01-01T00:00:00+14:00")

        record = {event_name: "" for event_name, _ in mycelium.FLOW_FIELDS}
        record.update(
            timestamp="2026-07-31T12:00:00Z",
            invalidated=False,
            facts=[],
            questions=[],
            next=[],
            version=1.0,
        )
        with self.assertRaisesRegex(ValueError, "version must be a JSON string"):
            mycelium.flow_key(record, record["facts"], record["questions"], record["next"])

    def test_body_fields_are_identical_with_crlf(self):
        text = "Evidence: source.md:1\nTrace: \nReflection: none\n\n## Facts\nfact\n"
        self.assertEqual(mycelium.body_fields(text), mycelium.body_fields(text.replace("\n", "\r\n")))

    def test_command_and_expect_default_to_empty_everywhere(self):
        mycelium.write_node("goal", "plain", "apex", "alive", facts="fact")
        node_text = Path(".mycelium/nodes/plain.md").read_text(encoding="utf-8")
        self.assertIn("\ncommand: \n", node_text)
        self.assertIn("\nexpect: \n", node_text)
        self.assertIn("\nCommand: \n", node_text)
        self.assertIn("\nExpect: \n", node_text)
        record = json.loads(Path(".mycelium/flows/goal.jsonl").read_text(encoding="utf-8"))
        self.assertEqual("", record["command"])
        self.assertEqual("", record["expect"])

    def test_command_and_expect_are_written_to_frontmatter_body_and_flow(self):
        result = self.run_node(
            "goal", "checked", "apex", "complete", "fact", "none", "next",
            "--topics", "topic", "--evidence", "source.md:1", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--source-refs", "source.md:1",
            "--command", 'python -c "print(42)"', "--expect", "42",
            "--allow-untracked", "field coverage fixture",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        node_text = Path(".mycelium/nodes/checked.md").read_text(encoding="utf-8")
        self.assertIn('\ncommand: python -c "print(42)"\n', node_text)
        self.assertIn("\nexpect: 42\n", node_text)
        self.assertIn('\nCommand: python -c "print(42)"\n', node_text)
        self.assertIn("\nExpect: 42\n", node_text)
        self.assertEqual('python -c "print(42)"', mycelium.metadata(node_text)["command"])
        record = json.loads(Path(".mycelium/flows/goal.jsonl").read_text(encoding="utf-8"))
        self.assertEqual('python -c "print(42)"', record["command"])
        self.assertEqual("42", record["expect"])
        # The flow projection shape is compared as an exact set on retry.
        lineage = load_lineage()
        self.assertEqual(
            (lineage.FLOW_V2_FIELDS | lineage.FLOW_V2_OPTIONAL_FIELDS) - {"timestamp"},
            set(mycelium.node_flow_projection("goal", "checked", "apex", "complete")),
        )
        self.assertLessEqual(
            lineage.NODE_V2_FRONTMATTER_FIELDS | lineage.NODE_V2_OPTIONAL_FIELDS,
            {"nodeId" if key == "nodeid" else key for key in mycelium.metadata(node_text)},
        )
        # Runs sealed before these fields existed must stay valid, so the
        # required sets themselves must not have grown.
        self.assertNotIn("command", lineage.NODE_V2_FRONTMATTER_FIELDS)
        self.assertNotIn("expect", lineage.FLOW_V2_FIELDS)

    def test_retry_projection_accepts_legacy_and_new_field_shapes(self):
        lineage = load_lineage()
        new_shape = mycelium.node_flow_projection("goal", "n", "apex", "complete")
        lineage._validate_retry_projection_shape(new_shape)
        legacy_shape = {
            key: value
            for key, value in new_shape.items()
            if key not in lineage.FLOW_V2_OPTIONAL_FIELDS
        }
        lineage._validate_retry_projection_shape(legacy_shape)
        with self.assertRaisesRegex(ValueError, "missing or unknown fields"):
            lineage._validate_retry_projection_shape({**new_shape, "surprise": ""})
        with self.assertRaisesRegex(ValueError, "missing or unknown fields"):
            lineage._validate_retry_projection_shape(
                {key: value for key, value in new_shape.items() if key != "goal"}
            )

    def test_multiline_command_is_rejected_before_writing(self):
        with self.assertRaisesRegex(ValueError, "single line"):
            mycelium.write_node("goal", "bad", "apex", "alive", command="one\nstatus: complete")
        with self.assertRaisesRegex(ValueError, "single line"):
            mycelium.write_node("goal", "bad", "apex", "alive", expect="one\nstatus: complete")
        self.assertFalse(Path(".mycelium").exists())

    def test_tracked_write_with_command_reserves_and_finalizes(self):
        lineage = load_lineage()
        Path("source.md").write_text("apex fact\nstem fact\ncap fact\n", encoding="utf-8")
        lineage.begin("goal", "apex-stem-cap", run_id="checked", root=Path.cwd())
        arguments = [
            "goal", "a", "apex", "complete", "apex fact", "none", "next",
            "--topics", "topic", "--evidence", "source.md:1", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--source-refs", "source.md:1",
            "--run-id", "checked", "--command", 'python -c "print(42)"', "--expect", "42",
        ]
        result = self.run_node(*arguments)
        self.assertEqual(0, result.returncode, result.stderr)

        # Repeating the identical tracked write must be idempotent. That path
        # compares the caller's projection against the stored one, so the new
        # fields have to round-trip through both shapes.
        repeat = self.run_node(*arguments)
        self.assertEqual(0, repeat.returncode, repeat.stderr)
        self.assertNotIn("missing or unknown fields", repeat.stderr)

        self.write_complete_node("checked", "s", "stem", "stem fact", "source.md:2", "a")
        self.write_complete_node("checked", "c", "cap", "cap fact", "source.md:3", "s")
        report = lineage.seal("checked", root=Path.cwd())
        self.assertEqual("sealed", report["state"])
        self.assertEqual([], report["diagnostics"])

    def test_node_write_without_a_run_flag_is_refused_and_writes_nothing(self):
        result = self.run_node("goal", "silent", "apex", "alive")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            'node writes are tracked by default; pass --run-id <id> or '
            '--allow-untracked "<reason>"',
            result.stderr,
        )
        self.assertFalse(Path(".mycelium").exists(), "the refused write must not touch state")

    def test_allow_untracked_records_the_reason_in_node_body_and_flow(self):
        result = self.run_node(
            "goal", "stated", "apex", "alive", "--allow-untracked", "demo"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        node_text = Path(".mycelium/nodes/stated.md").read_text(encoding="utf-8")
        self.assertIn("\nuntracked_reason: demo\n", node_text)
        self.assertIn("\nUntracked Reason: demo\n", node_text)
        self.assertEqual("demo", mycelium.metadata(node_text)["untracked_reason"])
        self.assertEqual("1", mycelium.metadata(node_text)["version"])
        record = json.loads(Path(".mycelium/flows/goal.jsonl").read_text(encoding="utf-8"))
        self.assertEqual("demo", record["untracked_reason"])
        self.assertFalse(Path(".mycelium/runs").exists())
        index = json.loads(
            mycelium.build_index(mycelium.load_nodes()).read_text(encoding="utf-8")
        )
        self.assertEqual(["demo"], [item["untracked_reason"] for item in index])

    def test_untracked_reason_defaults_to_empty_and_is_never_required(self):
        mycelium.write_node("goal", "plain", "apex", "alive", facts="fact")
        node_text = Path(".mycelium/nodes/plain.md").read_text(encoding="utf-8")
        self.assertIn("\nuntracked_reason: \n", node_text)
        self.assertIn("\nUntracked Reason: \n", node_text)
        record = json.loads(Path(".mycelium/flows/goal.jsonl").read_text(encoding="utf-8"))
        self.assertEqual("", record["untracked_reason"])
        # Sealed runs are re-validated on every read, so the required sets must
        # not have grown.
        lineage = load_lineage()
        self.assertIn("untracked_reason", lineage.NODE_V2_OPTIONAL_FIELDS)
        self.assertIn("untracked_reason", lineage.FLOW_V2_OPTIONAL_FIELDS)
        self.assertNotIn("untracked_reason", lineage.NODE_V2_FRONTMATTER_FIELDS)
        self.assertNotIn("untracked_reason", lineage.FLOW_V2_FIELDS)

    def test_a_tracked_write_is_unchanged_and_carries_no_reason(self):
        lineage = load_lineage()
        lineage.begin("goal", run_id="tracked", root=Path.cwd())
        result = self.run_node(
            "goal", "a", "apex", "complete", "fact", "none", "next",
            "--topics", "topic", "--evidence", "evidence", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--run-id", "tracked",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        node_text = Path(".mycelium/nodes/a.md").read_text(encoding="utf-8")
        self.assertIn("\nversion: 2\n", node_text)
        self.assertIn("\nuntracked_reason: \n", node_text)
        self.assertEqual(
            "committed",
            lineage.load_manifest("tracked", root=Path.cwd())["nodes"]["a"]["status"],
        )

    def test_the_two_run_flags_are_mutually_exclusive(self):
        result = self.run_node(
            "goal", "both", "apex", "alive",
            "--run-id", "tracked", "--allow-untracked", "demo",
        )
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("mutually exclusive", result.stderr)
        self.assertFalse(Path(".mycelium").exists())

    def test_powershell_writer_refuses_a_write_with_no_run_flag(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not on PATH")
        shell_root = Path("powershell-refusal")
        shell_root.mkdir()
        result = subprocess.run(
            [
                pwsh, "-NoProfile", "-File", str(MODULE_PATH.with_name("mycelium-node.ps1")),
                "goal", "silent", "apex", "alive",
            ],
            cwd=shell_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            'node writes are tracked by default; pass -RunId <id> or '
            '-AllowUntracked "<reason>"',
            result.stdout + result.stderr,
        )
        self.assertFalse((shell_root / ".mycelium").exists())

    def test_posix_wrapper_passes_allow_untracked_through(self):
        shell = shutil.which("sh")
        if shell is None and os.name == "nt":
            git_bash = (
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Git" / "bin" / "bash.exe"
            )
            shell = str(git_bash) if git_bash.is_file() else None
        if shell is None:
            self.skipTest("POSIX shell is unavailable")
        wrapper_bin = Path("wrapper-bin")
        wrapper_bin.mkdir()
        for source in (MODULE_PATH, MODULE_PATH.with_name("mycelium-node.sh")):
            shutil.copyfile(source, wrapper_bin / source.name)
        # `python3` is a Windows Store stub in this environment, so the wrapper's
        # untracked branch is exercised through a shim that is the real
        # interpreter.
        shim = Path("shim")
        shim.mkdir()
        (shim / "python3").write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
        )
        os.chmod(shim / "python3", 0o755)
        environment = {
            **os.environ,
            "PATH": str(shim.resolve()) + os.pathsep + os.environ.get("PATH", ""),
        }
        result = subprocess.run(
            [
                shell, str(wrapper_bin / "mycelium-node.sh"),
                "goal", "through", "apex", "alive",
                "--allow-untracked", "wrapper passthrough",
            ],
            cwd=Path.cwd(), capture_output=True, text=True, env=environment,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            "\nuntracked_reason: wrapper passthrough\n",
            Path(".mycelium/nodes/through.md").read_text(encoding="utf-8"),
        )

    def test_powershell_writer_matches_the_python_node_and_flow_shape(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not on PATH")
        command = 'python -c "print(42)"'
        arguments = [
            "goal", "parity", "apex", "complete", "fact", "none", "next",
            "--topics", "topic", "--evidence", "source.md:1", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--source-refs", "source.md:1",
            "--command", command, "--expect", "42",
            "--allow-untracked", "parity fixture",
        ]
        python_result = self.run_node(*arguments)
        self.assertEqual(0, python_result.returncode, python_result.stderr)
        python_node = Path(".mycelium/nodes/parity.md").read_text(encoding="utf-8")
        python_record = json.loads(Path(".mycelium/flows/goal.jsonl").read_text(encoding="utf-8"))

        shell_root = Path("powershell-parity")
        shell_root.mkdir()
        shell = subprocess.run(
            [
                pwsh, "-NoProfile", "-File", str(MODULE_PATH.with_name("mycelium-node.ps1")),
                "goal", "parity", "apex", "complete", "fact", "none", "next",
                "-Topics", "topic", "-Evidence", "source.md:1", "-Confidence", "high",
                "-Consumes", "none", "-Blocks", "none", "-SourceRefs", "source.md:1",
                "-Command", command, "-Expect", "42",
                "-AllowUntracked", "parity fixture",
            ],
            cwd=shell_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, shell.returncode, shell.stdout + shell.stderr)
        shell_node = (shell_root / ".mycelium/nodes/parity.md").read_text(encoding="utf-8")
        shell_record = json.loads(
            (shell_root / ".mycelium/flows/goal.jsonl").read_text(encoding="utf-8")
        )

        # The PowerShell here-string omits the final newline; that predates the
        # command/expect fields and is not what this test is about.
        normalize = lambda text: re.sub(
            r"(?m)^updated: .*$", "updated: <ts>", text
        ).rstrip("\n")
        self.assertEqual(normalize(python_node), normalize(shell_node))
        self.assertEqual(list(python_record), list(shell_record))
        self.assertEqual(command, shell_record["command"])
        self.assertEqual("42", shell_record["expect"])


if __name__ == "__main__":
    unittest.main()
