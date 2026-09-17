import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium_lineage.py"
NODE_WRITER_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium.py"
SPEC = importlib.util.spec_from_file_location("mycelium_lineage", MODULE_PATH)
lineage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lineage)


SAFE_SKIP_EVIDENCE = {
    "input_packets": 1,
    "all_directly_sourced": True,
    "conflicts": False,
    "duplicates": False,
    "stale": False,
    "uncertainty": False,
    "risk": "low",
}


def skipped_topology(*roles):
    codes = {"septum": "single-clean-stream", "hyphae": "already-compact"}
    return [
        {
            "role": role,
            "state": "skipped" if role in roles else "required",
            **(
                {"skip": {"code": codes[role], "evidence": SAFE_SKIP_EVIDENCE}}
                if role in roles
                else {}
            ),
        }
        for role in lineage.ROLES
    ]


class ProcessLineageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def directory_link(self, target, link):
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            os.symlink(target, link, target_is_directory=True)

    def artifact(self, node_id, role, goal="goal", *, status="complete"):
        node_dir = self.root / ".mycelium" / "nodes"
        flow_dir = self.root / ".mycelium" / "flows"
        node_dir.mkdir(parents=True, exist_ok=True)
        flow_dir.mkdir(parents=True, exist_ok=True)
        timestamp = f"2026-08-27T00:00:{len(list(node_dir.glob('*.md'))):02d}.0000000Z"
        node_path = node_dir / f"{node_id}.md"
        metadata = {
            "nodeId": node_id,
            "goal": goal,
            "role": role,
            "status": status,
            "topics": "topic",
            "confidence": "high",
            "consumes": "none",
            "blocks": "none",
            "parent_goal_id": goal,
            "producing_agent": "test",
            "version": "2",
            "source_refs": "source.md:1",
            "depends_on": "none",
            "invalidated": "false",
            "supersedes": "none",
            "updated": timestamp,
        }
        node_path.write_text(
            "\n".join(
                ["---"]
                + [f"{key}: {value}" for key, value in metadata.items()]
                + [
                    "---",
                    "",
                    f"# {node_id}",
                    "",
                    f"Goal: {goal}",
                    f"Role: {role}",
                    f"Status: {status}",
                    "Topics: topic",
                    "Evidence: source.md:1",
                    "Confidence: high",
                    "Consumes: none",
                    "Blocks: none",
                    "Trace: none",
                    "Reflection: none",
                    f"Parent Goal ID: {goal}",
                    "Producing Agent: test",
                    "Version: 2",
                    "Source Refs: source.md:1",
                    "Depends On: none",
                    "Invalidated: false",
                    "Supersedes: none",
                    "",
                    "## Facts",
                    "fact",
                    "",
                    "## Questions",
                    "none",
                    "",
                    "## Next",
                    "next",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        flow_path = flow_dir / "goal.jsonl"
        record = {
            "timestamp": timestamp,
            "goal": goal,
            "nodeId": node_id,
            "role": role,
            "status": status,
            "topics": "topic",
            "evidence": "source.md:1",
            "confidence": "high",
            "consumes": "none",
            "blocks": "none",
            "trace": "none",
            "reflection": "none",
            "parentGoalId": goal,
            "producingAgent": "test",
            "version": "2",
            "sourceRefs": "source.md:1",
            "dependsOn": "none",
            "invalidated": False,
            "supersedes": "none",
            "facts": ["fact"],
            "questions": ["none"],
            "next": ["next"],
        }
        with flow_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        line_number = len(flow_path.read_text(encoding="utf-8").splitlines())
        return lineage.build_commit_binding(
            self.root, node_id, role, node_path, flow_path, line_number
        )

    def commit(self, run_id, node_id, role, inputs=(), *, goal="goal"):
        reserved = lineage.reserve(
            run_id, node_id, role, list(inputs), root=self.root
        )
        binding = self.artifact(node_id, role, goal)
        return lineage.finalize(
            run_id, reserved["reservation_token"], binding, root=self.root
        )

    def full_chain(self, run_id):
        self.commit(run_id, "a", "apex")
        self.commit(run_id, "s", "septum", ["a"])
        self.commit(run_id, "h", "hyphae", ["s"])
        self.commit(run_id, "stem", "stem", ["h"])
        self.commit(run_id, "cap", "cap", ["stem"])

    def test_preflight_is_read_only(self):
        result = lineage.preflight(root=self.root)
        self.assertTrue(result["ok"])
        self.assertFalse((self.root / ".mycelium").exists())

    def test_begin_with_topology_preset_matches_hand_built_topology(self):
        preset = lineage.begin(
            "goal", "apex-stem-cap", run_id="preset", root=self.root
        )
        hand_built = lineage.begin(
            "goal",
            skipped_topology("septum", "hyphae"),
            run_id="hand-built",
            root=self.root,
        )
        preset_manifest = lineage.load_manifest("preset", root=self.root)
        hand_built_manifest = lineage.load_manifest("hand-built", root=self.root)
        self.assertEqual(
            hand_built_manifest["topology"], preset_manifest["topology"]
        )
        self.assertEqual("open", preset["state"])
        self.assertEqual("open", hand_built["state"])

    def test_unknown_topology_preset_raises(self):
        with self.assertRaisesRegex(ValueError, "unknown topology preset"):
            lineage.begin(
                "goal", "no-such-preset", run_id="bad-preset", root=self.root
            )
        self.assertFalse(
            (self.root / ".mycelium" / "runs" / "bad-preset.json").exists()
        )

    def test_begin_persists_schema_one_locked_topology(self):
        result = lineage.begin("goal", run_id="run-1", root=self.root)
        self.assertEqual("run-1", result["run_id"])
        manifest = lineage.load_manifest("run-1", root=self.root)
        self.assertEqual(1, manifest["schema"])
        self.assertEqual("open", manifest["state"])
        self.assertEqual(list(lineage.ROLES), [item["role"] for item in manifest["topology"]])
        self.assertTrue(all(item["state"] == "required" for item in manifest["topology"]))
        self.assertEqual(
            [{"role": role, "state": "pending"} for role in lineage.ROLES],
            manifest["roles"],
        )

    def test_shortcut_requires_exact_typed_skip_evidence(self):
        topology = skipped_topology("septum", "hyphae")
        lineage.begin("goal", topology, run_id="shortcut", root=self.root)
        self.commit("shortcut", "a", "apex")
        self.commit("shortcut", "stem", "stem", ["a"])
        self.commit("shortcut", "cap", "cap", ["stem"])
        report = lineage.seal("shortcut", root=self.root)
        self.assertEqual("sealed", report["state"])
        self.assertEqual(
            ["performed", "skipped", "skipped", "performed", "performed"],
            [item["state"] for item in lineage.load_manifest("shortcut", root=self.root)["roles"]],
        )

        bad = skipped_topology("septum")
        bad[1]["skip"]["evidence"] = {**SAFE_SKIP_EVIDENCE, "uncertainty": True}
        with self.assertRaisesRegex(ValueError, "skip evidence"):
            lineage.begin("goal", bad, run_id="bad", root=self.root)
        self.assertFalse((self.root / ".mycelium" / "runs" / "bad.json").exists())

    def test_full_chain_seals_and_verifies_current_commits(self):
        lineage.begin("goal", run_id="full", root=self.root)
        self.full_chain("full")
        sealed = lineage.seal("full", root=self.root)
        self.assertTrue(sealed["valid"])
        verified = lineage.verify("full", root=self.root)
        self.assertTrue(verified["valid"])
        self.assertEqual("sealed", verified["state"])

        snapshot = lineage.verify_snapshot("full", root=self.root)
        self.assertEqual("sealed", snapshot["manifest"]["state"])
        self.assertTrue(snapshot["report"]["valid"])

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_posix_verifier_uses_read_only_shared_lock_and_public_artifacts(self):
        previous_umask = os.umask(0o077)
        try:
            lineage.begin("goal", run_id="cross-uid", root=self.root)
        finally:
            os.umask(previous_umask)

        runs = self.root / ".mycelium" / "runs"
        lock_path = runs / "cross-uid.lock"
        manifest_path = runs / "cross-uid.json"
        self.assertEqual(0o755, runs.stat().st_mode & 0o777)
        self.assertEqual(0o644, lock_path.stat().st_mode & 0o777)
        self.assertEqual(0o644, manifest_path.stat().st_mode & 0o777)

        lock_path.chmod(0o444)
        snapshot = lineage.verify_snapshot("cross-uid", root=self.root)
        self.assertEqual("open", snapshot["manifest"]["state"])
        self.assertFalse(snapshot["report"]["valid"])

    def test_verifier_rejects_missing_lock_without_creating_it(self):
        lineage.begin("goal", run_id="missing-lock", root=self.root)
        lock_path = self.root / ".mycelium" / "runs" / "missing-lock.lock"
        lock_path.unlink()

        with self.assertRaises(FileNotFoundError):
            lineage.verify_snapshot("missing-lock", root=self.root)

        self.assertFalse(lock_path.exists())

    def test_fan_in_fan_out_is_valid(self):
        lineage.begin("goal", run_id="fan", root=self.root)
        self.commit("fan", "a1", "apex")
        self.commit("fan", "a2", "apex")
        self.commit("fan", "s1", "septum", ["a1"])
        self.commit("fan", "s2", "septum", ["a2"])
        self.commit("fan", "h1", "hyphae", ["s1"])
        self.commit("fan", "h2", "hyphae", ["s2"])
        self.commit("fan", "stem", "stem", ["h1", "h2"])
        self.commit("fan", "cap1", "cap", ["stem"])
        self.commit("fan", "cap2", "cap", ["stem"])
        self.assertTrue(lineage.seal("fan", root=self.root)["valid"])

    def test_partial_shortcut_is_valid_only_across_skipped_role(self):
        lineage.begin(
            "goal", skipped_topology("hyphae"), run_id="partial", root=self.root
        )
        self.commit("partial", "a", "apex")
        self.commit("partial", "s", "septum", ["a"])
        self.commit("partial", "stem", "stem", ["s"])
        self.commit("partial", "cap", "cap", ["stem"])
        self.assertTrue(lineage.seal("partial", root=self.root)["valid"])

    def test_pending_reservation_blocks_seal_then_abort_clears_it(self):
        lineage.begin(
            "goal", skipped_topology("septum", "hyphae"), run_id="pending", root=self.root
        )
        lineage.reserve("pending", "abandoned", "apex", [], root=self.root)
        with self.assertRaisesRegex(lineage.ValidationError, "pending reservation"):
            lineage.seal("pending", root=self.root)
        token = lineage.reserve("pending", "abandoned", "apex", [], root=self.root)[
            "reservation_token"
        ]
        lineage.abort("pending", token, "writer failed", root=self.root)
        self.commit("pending", "a", "apex")
        self.commit("pending", "stem", "stem", ["a"])
        self.commit("pending", "cap", "cap", ["stem"])
        self.assertTrue(lineage.seal("pending", root=self.root)["valid"])

    def test_open_run_is_not_authoritative_verification(self):
        lineage.begin("goal", run_id="open", root=self.root)
        report = lineage.verify("open", root=self.root)
        self.assertFalse(report["valid"])
        self.assertIn("unsealed", [item["code"] for item in report["diagnostics"]])

    def test_exact_retries_are_idempotent_and_conflicts_fail(self):
        lineage.begin("goal", run_id="retry", root=self.root)
        first = lineage.reserve("retry", "a", "apex", [], root=self.root)
        second = lineage.reserve("retry", "a", "apex", [], root=self.root)
        self.assertEqual(first["reservation_token"], second["reservation_token"])
        self.assertTrue(second["idempotent"])
        with self.assertRaisesRegex(ValueError, "conflicting reservation"):
            lineage.reserve("retry", "a", "septum", [], root=self.root)
        binding = self.artifact("a", "apex")
        finalized = lineage.finalize(
            "retry", first["reservation_token"], binding, root=self.root
        )
        replay = lineage.finalize(
            "retry", first["reservation_token"], binding, root=self.root
        )
        self.assertEqual(finalized["commit"]["digest"], replay["commit"]["digest"])
        self.assertTrue(replay["idempotent"])

    def test_finalize_rejects_cross_goal_and_incomplete_artifacts(self):
        lineage.begin("goal", run_id="binding", root=self.root)
        reservation = lineage.reserve("binding", "a", "apex", [], root=self.root)
        wrong = self.artifact("a", "apex", "another goal")
        with self.assertRaisesRegex(ValueError, "goal"):
            lineage.finalize(
                "binding", reservation["reservation_token"], wrong, root=self.root
            )

    def test_binding_accepts_real_legacy_node_writer_commit(self):
        lineage.begin("goal", run_id="real-writer", root=self.root)
        reservation = lineage.reserve(
            "real-writer", "a", "apex", [], root=self.root
        )
        written = subprocess.run(
            [
                sys.executable,
                str(NODE_WRITER_PATH),
                "node",
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
                "source:1",
                "--confidence",
                "high",
                "--consumes",
                "none",
                "--blocks",
                "none",
                "--version",
                "2",
                "--source-refs",
                "source.md:1",
                "--allow-untracked",
                "lineage binding fixture",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        result = lineage.finalize_paths(
            "real-writer",
            reservation["reservation_token"],
            ".mycelium/nodes/a.md",
            written.stdout.strip(),
            1,
            root=self.root,
        )
        self.assertEqual("committed", result["status"])

    def test_stale_or_tampered_commit_fails_validation(self):
        lineage.begin("goal", run_id="tampered", root=self.root)
        self.full_chain("tampered")
        node = self.root / ".mycelium" / "nodes" / "a.md"
        node.write_text(node.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(lineage.ValidationError, "node hash|schema-v2"):
            lineage.seal("tampered", root=self.root)

    def test_tampering_after_seal_makes_read_only_verification_invalid(self):
        lineage.begin(
            "goal", skipped_topology("septum", "hyphae"), run_id="sealed-tamper", root=self.root
        )
        self.commit("sealed-tamper", "a", "apex")
        self.commit("sealed-tamper", "stem", "stem", ["a"])
        self.commit("sealed-tamper", "cap", "cap", ["stem"])
        lineage.seal("sealed-tamper", root=self.root)
        flow = self.root / ".mycelium" / "flows" / "goal.jsonl"
        flow.write_text(flow.read_text(encoding="utf-8").replace('"status":"complete"', '"status":"blocked"', 1), encoding="utf-8")
        report = lineage.verify("sealed-tamper", root=self.root)
        self.assertFalse(report["valid"])
        self.assertEqual("invalid", report["state"])
        self.assertEqual("sealed", report["manifest_state"])

    def test_edges_cannot_skip_required_roles_or_go_backwards(self):
        lineage.begin("goal", run_id="edges", root=self.root)
        self.commit("edges", "a", "apex")
        with self.assertRaisesRegex(ValueError, "required role"):
            lineage.reserve("edges", "stem", "stem", ["a"], root=self.root)
        self.commit("edges", "s", "septum", ["a"])
        with self.assertRaisesRegex(ValueError, "backward|same-role"):
            lineage.reserve("edges", "a2", "apex", ["s"], root=self.root)

    def test_disconnected_pre_stem_branch_and_multiple_stems_fail(self):
        lineage.begin("goal", run_id="disconnected", root=self.root)
        self.commit("disconnected", "a1", "apex")
        self.commit("disconnected", "a2", "apex")
        self.commit("disconnected", "s", "septum", ["a1"])
        self.commit("disconnected", "h", "hyphae", ["s"])
        self.commit("disconnected", "stem", "stem", ["h"])
        self.commit("disconnected", "cap", "cap", ["stem"])
        with self.assertRaisesRegex(lineage.ValidationError, "disconnected"):
            lineage.seal("disconnected", root=self.root)

        lineage.begin("goal", run_id="two-stems", root=self.root)
        self.commit("two-stems", "a", "apex")
        self.commit("two-stems", "s", "septum", ["a"])
        self.commit("two-stems", "h", "hyphae", ["s"])
        self.commit("two-stems", "stem1", "stem", ["h"])
        self.commit("two-stems", "stem2", "stem", ["h"])
        self.commit("two-stems", "cap", "cap", ["stem1"])
        with self.assertRaisesRegex(lineage.ValidationError, "exactly one STEM"):
            lineage.seal("two-stems", root=self.root)

    def test_sealed_run_rejects_mutation(self):
        lineage.begin(
            "goal", skipped_topology("septum", "hyphae"), run_id="immutable", root=self.root
        )
        self.commit("immutable", "a", "apex")
        self.commit("immutable", "stem", "stem", ["a"])
        self.commit("immutable", "cap", "cap", ["stem"])
        lineage.seal("immutable", root=self.root)
        with self.assertRaisesRegex(ValueError, "sealed"):
            lineage.reserve("immutable", "another", "cap", ["stem"], root=self.root)

    def test_concurrent_reservations_do_not_lose_updates(self):
        lineage.begin("goal", run_id="concurrent", root=self.root)
        errors = []

        def worker(index):
            try:
                lineage.reserve(
                    "concurrent", f"apex-{index}", "apex", [], root=self.root
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        manifest = lineage.load_manifest("concurrent", root=self.root)
        self.assertEqual(8, len(manifest["nodes"]))
        self.assertEqual(
            list(range(1, 9)),
            sorted(node["sequence"] for node in manifest["nodes"].values()),
        )

    def test_concurrent_subprocess_reservations_do_not_lose_updates(self):
        lineage.begin("goal", run_id="processes", root=self.root)
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--root",
                    str(self.root),
                    "reserve",
                    "--run-id",
                    "processes",
                    "--node-id",
                    f"apex-{index}",
                    "--role",
                    "apex",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for index in range(12)
        ]
        results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]
        self.assertEqual([], [(err, code) for _, err, code in results if code != 0])
        manifest = lineage.load_manifest("processes", root=self.root)
        self.assertEqual(12, len(manifest["nodes"]))
        self.assertEqual(list(range(1, 13)), sorted(node["sequence"] for node in manifest["nodes"].values()))

    def test_cli_returns_machine_readable_reservation_and_verification(self):
        begin = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--root",
                str(self.root),
                "begin",
                "--goal",
                "goal",
                "--run-id",
                "cli",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("cli", json.loads(begin.stdout)["run_id"])
        reserved = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--root",
                str(self.root),
                "reserve",
                "--run-id",
                "cli",
                "--node-id",
                "a",
                "--role",
                "apex",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("pending", json.loads(reserved.stdout)["status"])

    def test_argparse_failures_are_canonical_json(self):
        failed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "reserve", "--not-an-option"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, failed.returncode)
        self.assertEqual("", failed.stdout)
        payload = json.loads(failed.stderr)
        self.assertEqual("ValueError", payload["type"])
        self.assertIn("argument error", payload["error"])

    def test_manifest_identity_and_exact_schemas_are_verified(self):
        cases = []

        def case(name, mutate, code):
            run_id = f"schema-{name}"
            lineage.begin("goal", run_id=run_id, root=self.root)
            path = self.root / ".mycelium" / "runs" / f"{run_id}.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            mutate(manifest)
            path.write_text(json.dumps(manifest), encoding="utf-8")
            report = lineage.verify(run_id, root=self.root)
            cases.append((code, [item["code"] for item in report["diagnostics"]]))

        case("run-id", lambda value: value.update(run_id="another-run"), "run-id")
        case("top-extra", lambda value: value.update(unknown=True), "manifest-schema")
        case("top-missing", lambda value: value.pop("created_at"), "manifest-schema")
        case("timestamp", lambda value: value.update(updated_at="yesterday"), "timestamp")
        for expected, actual in cases:
            self.assertIn(expected, actual)

    def test_node_schema_duplicate_inputs_and_status_audit_are_verified(self):
        lineage.begin("goal", run_id="node-schema", root=self.root)
        self.commit("node-schema", "a", "apex")
        path = self.root / ".mycelium" / "runs" / "node-schema.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        node = manifest["nodes"]["a"]
        node["unknown"] = True
        node["process_inputs"] = ["parent", "parent"]
        node["aborted_at"] = node["reserved_at"]
        node["abort_reason"] = "impossible committed audit"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        report = lineage.verify("node-schema", root=self.root)
        codes = [item["code"] for item in report["diagnostics"]]
        self.assertIn("node-schema", codes)
        self.assertIn("process-inputs", codes)
        self.assertIn("status-audit", codes)

    def test_run_directory_and_lock_reparse_points_are_rejected(self):
        outside = self.root.parent / f"outside-{os.getpid()}-{id(self)}"
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir() if outside.exists() and not any(outside.iterdir()) else None)
        mycelium = self.root / ".mycelium"
        self.directory_link(outside, mycelium)
        with self.assertRaisesRegex(ValueError, "reparse|symlink"):
            lineage.begin("goal", run_id="escape", root=self.root)
        self.assertFalse((outside / "runs").exists())

    def test_existing_lock_symlink_is_rejected(self):
        lineage.begin("goal", run_id="lock-link", root=self.root)
        lock_path = self.root / ".mycelium" / "runs" / "lock-link.lock"
        lock_path.unlink()
        outside = self.root / "outside-lock"
        outside.mkdir()
        self.directory_link(outside, lock_path)
        with self.assertRaisesRegex(ValueError, "reparse|symlink"):
            lineage.reserve("lock-link", "a", "apex", [], root=self.root)

    def test_existing_lock_hard_link_is_rejected(self):
        lineage.begin("goal", run_id="lock-hard-link", root=self.root)
        lock_path = self.root / ".mycelium" / "runs" / "lock-hard-link.lock"
        lock_path.unlink()
        outside = self.root / "outside-lock"
        outside.write_bytes(b"\0")
        os.link(outside, lock_path)
        with self.assertRaisesRegex(ValueError, "hard link"):
            lineage.reserve("lock-hard-link", "a", "apex", [], root=self.root)
        self.assertEqual(b"\0", outside.read_bytes())

    def test_incomplete_or_version_one_node_cannot_be_bound(self):
        node_dir = self.root / ".mycelium" / "nodes"
        flow_dir = self.root / ".mycelium" / "flows"
        node_dir.mkdir(parents=True)
        flow_dir.mkdir(parents=True)
        node = node_dir / "a.md"
        node.write_text("---\nnodeId: a\ngoal: goal\nrole: apex\nstatus: complete\nupdated: 2026-08-27T00:00:00.0000000Z\n---\n", encoding="utf-8")
        flow = flow_dir / "goal.jsonl"
        flow.write_text(json.dumps({"timestamp": "2026-08-27T00:00:00.0000000Z", "goal": "goal", "nodeId": "a", "role": "apex", "status": "complete"}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema-v2"):
            lineage.build_commit_binding(self.root, "a", "apex", node, flow, 1)

    def test_posix_node_wrapper_preserves_legacy_python3_without_version_gate(self):
        shell = shutil.which("sh")
        if shell is None and os.name == "nt":
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
            shell = str(git_bash) if git_bash.is_file() else None
        if shell is None:
            self.skipTest("POSIX shell is unavailable")

        isolated_bin = self.root / "bin"
        isolated_bin.mkdir()
        for source in (NODE_WRITER_PATH, MODULE_PATH.with_name("mycelium-node.sh")):
            shutil.copyfile(source, isolated_bin / source.name)

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        invocation_log = self.root / "legacy-python3.log"
        (fake_bin / "python3").write_text(
            "#!/bin/sh\n"
            "case \"$1\" in -c|-B) exit 73 ;; esac\n"
            "printf '%s\\n' \"$*\" > \"$MYCELIUM_LEGACY_LOG\"\n"
            "exec \"$MYCELIUM_REAL_PYTHON\" \"$@\"\n",
            encoding="utf-8",
        )
        os.chmod(fake_bin / "python3", 0o755)
        environment = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "MYCELIUM_REAL_PYTHON": sys.executable,
            "MYCELIUM_LEGACY_LOG": str(invocation_log),
        }
        result = subprocess.run(
            [
                shell,
                str(isolated_bin / "mycelium-node.sh"),
                "--allow-untracked", "legacy-wrapper-test",
                "--", "--run-id", "legacy", "apex", "alive", "legacy fact",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        invocation = invocation_log.read_text(encoding="utf-8")
        self.assertIn(
            "mycelium.py node --allow-untracked legacy-wrapper-test "
            "-- --run-id legacy apex alive legacy fact",
            invocation,
        )
        self.assertNotIn("-B", invocation)
        self.assertFalse((self.root / ".mycelium" / "runs").exists())

    def test_posix_node_wrapper_falls_back_to_compatible_python_for_tracked_write(self):
        shell = shutil.which("sh")
        if shell is None and os.name == "nt":
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
            shell = str(git_bash) if git_bash.is_file() else None
        if shell is None:
            self.skipTest("POSIX shell is unavailable")

        isolated_bin = self.root / "bin"
        isolated_bin.mkdir()
        for source in (NODE_WRITER_PATH, MODULE_PATH, MODULE_PATH.with_name("mycelium-node.sh")):
            shutil.copyfile(source, isolated_bin / source.name)

        (self.root / "source.md").write_text("fallback evidence\n", encoding="utf-8")
        lineage.begin("tracked fallback", run_id="fallback-run", root=self.root)

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "python3").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (fake_bin / "python").write_text(
            "#!/bin/sh\n"
            "printf '%s\\t%s\\n' \"${PYTHONDONTWRITEBYTECODE:-}\" \"$*\" >> \"$MYCELIUM_FALLBACK_LOG\"\n"
            "exec \"$MYCELIUM_REAL_PYTHON\" \"$@\"\n",
            encoding="utf-8",
        )
        os.chmod(fake_bin / "python3", 0o755)
        os.chmod(fake_bin / "python", 0o755)
        fallback_log = self.root / "fallback.log"
        environment = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "MYCELIUM_REAL_PYTHON": sys.executable,
            "MYCELIUM_FALLBACK_LOG": str(fallback_log),
        }
        result = subprocess.run(
            [
                shell,
                str(isolated_bin / "mycelium-node.sh"),
                "tracked fallback", "fallback-apex", "apex", "complete",
                "fallback fact", "none", "wake septum",
                "--topics", "tracked,fallback",
                "--evidence", "source.md:1",
                "--confidence", "high",
                "--consumes", "none",
                "--blocks", "none",
                "--source-refs", "source.md:1",
                "--run-id", "fallback-run",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads((self.root / ".mycelium" / "runs" / "fallback-run.json").read_text(encoding="utf-8"))
        self.assertEqual("committed", manifest["nodes"]["fallback-apex"]["status"])
        fallback_calls = fallback_log.read_text(encoding="utf-8")
        self.assertIn("fallback-apex", fallback_calls)
        self.assertIn("-B", fallback_calls)
        self.assertTrue(all(line.startswith("1\t") for line in fallback_calls.splitlines()))
        self.assertFalse((isolated_bin / "__pycache__").exists())

    @unittest.skipUnless(os.name == "nt", "PowerShell interpreter retry behavior is Windows-specific")
    def test_powershell_wrapper_preserves_selected_interpreter_failure(self):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        retry_marker = self.root / "retried.txt"
        (fake_bin / "python.cmd").write_text(
            "@echo off\r\nif \"%1\"==\"-B\" if \"%2\"==\"-c\" exit /b 0\r\necho {\"error\":\"synthetic\"} 1>&2\r\nexit /b 7\r\n",
            encoding="ascii",
        )
        (fake_bin / "python3.cmd").write_text(
            f"@echo off\r\necho retried>{retry_marker}\r\nexit /b 0\r\n",
            encoding="ascii",
        )
        environment = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(MODULE_PATH.with_name("mycelium-lineage.ps1")), "preflight"],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(7, result.returncode)
        self.assertIn("synthetic", result.stderr)
        self.assertFalse(retry_marker.exists())

    def test_powershell_lineage_wrapper_uses_dash_b_without_mutating_environment(self):
        wrapper = MODULE_PATH.with_name("mycelium-lineage.ps1").read_text(encoding="utf-8")

        self.assertNotIn("$env:PYTHONDONTWRITEBYTECODE", wrapper)
        self.assertGreaterEqual(wrapper.count("-B"), 2)


if __name__ == "__main__":
    unittest.main()
