import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).parent))
import pilot


LANGUAGES = ("Go",) * 3 + ("TypeScript",) * 3 + ("JavaScript",) * 2 + ("Rust",) * 2


class PilotManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "treatment"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "pilot@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Pilot Test"], check=True)
        (self.repo / "SKILL.md").write_text("frozen treatment\n", encoding="utf-8")
        (self.repo / "evals").mkdir()
        (self.repo / "evals" / "treatment-files.txt").write_text("SKILL.md\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "treatment"], check=True)
        self.installed = self.root / "installed-treatment"
        self.installed.mkdir()
        committed_skill = subprocess.run(
            ["git", "-C", str(self.repo), "show", "HEAD:SKILL.md"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        (self.installed / "SKILL.md").write_bytes(committed_skill)

        self.prompts = {}
        for arm in pilot.ARMS:
            path = self.root / f"{arm}.txt"
            path.write_text(f"{arm} prompt\n", encoding="utf-8")
            self.prompts[arm] = path
        self.runner = self.root / "runner.py"
        self.runner.write_text("# frozen runner\n", encoding="utf-8")
        self.candidates = self.root / "candidates.json"
        self._write_candidates()
        self.output = self.root / "pilot-manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def _candidate(self, index, language, instance_id=None):
        instance_id = instance_id or f"task-{index:02d}"
        digest = hashlib.sha256(instance_id.encode()).hexdigest()
        return {
            "instance_id": instance_id,
            "repository": f"example/repo-{index:02d}",
            "language": language,
            "created_at": "2026-02-01T00:00:00Z",
            "declared_tests": 100 + index,
            "gold_passes": 3,
            "gold_attempts": 3,
            "base_commit": f"{index + 1:040x}",
            "image_digest": f"ghcr.io/example/{instance_id}@sha256:{digest}",
            "hidden_row_sha256": digest,
        }

    def _write_candidates(self, mutate=None):
        document = {
            "dataset_name": pilot.DATASET_NAME,
            "dataset_revision": pilot.DATASET_REVISION,
            "evaluator_repository": pilot.EVALUATOR_REPOSITORY,
            "evaluator_commit": pilot.EVALUATOR_COMMIT,
            "candidates": [
                *[self._candidate(i, language) for i, language in enumerate(LANGUAGES)],
                self._candidate(100, "Go", pilot.PUBLIC_FEASIBILITY_IDS[0]),
            ],
        }
        if mutate:
            mutate(document)
        self.candidates.write_text(json.dumps(document), encoding="utf-8")

    def _freeze(self, replicate=0):
        return pilot.freeze(
            candidates_path=self.candidates,
            output_path=self.output,
            treatment_root=self.repo,
            installed_treatment=self.installed,
            prompt_paths=self.prompts,
            runner_path=self.runner,
            model_provider="openai",
            requested_model="gpt-5.6-sol",
            model_revision="gpt-5.6-sol-2026-07-31",
            reasoning_effort="high",
            replicate=replicate,
            max_usd_per_run="5.00",
            max_total_usd="10.00",
            max_tokens_per_run=100_000,
            max_wall_time_seconds=1_800,
            max_tool_calls_per_run=200,
        )

    def test_freeze_is_deterministic_balanced_and_validates(self):
        manifest = self._freeze()

        self.assertEqual("mycelium.swebench-live-pilot/v4", manifest["schema"])
        self.assertEqual("NO-SCORE", manifest["status"])
        self.assertEqual(
            {"name": pilot.DATASET_NAME, "revision": pilot.DATASET_REVISION},
            manifest["dataset"],
        )
        self.assertEqual(
            {"repository": pilot.EVALUATOR_REPOSITORY, "commit": pilot.EVALUATOR_COMMIT},
            manifest["evaluator"],
        )
        self.assertEqual(list(pilot.ARMS), [arm["name"] for arm in manifest["arms"]])
        self.assertEqual(
            [(False, False), (True, False), (True, True)],
            [(arm["treatment_enabled"], arm["multi_agent_enabled"]) for arm in manifest["arms"]],
        )
        self.assertEqual(pilot.DATASET_REVISION, manifest["dataset"]["revision"])
        self.assertEqual(pilot.EVALUATOR_COMMIT, manifest["evaluator"]["commit"])
        self.assertEqual(
            {
                "pier_version": pilot.PIER_VERSION,
                "codex_version": pilot.CODEX_VERSION,
                "codex_npm_integrity": pilot.CODEX_NPM_INTEGRITY,
                "codex_platform_packages": pilot.CODEX_PLATFORM_PACKAGES,
            },
            manifest["runtime"],
        )
        self.assertEqual(pilot.LANGUAGE_QUOTAS, manifest["selection"]["language_quotas"])
        self.assertEqual(10, len(manifest["selection"]["tasks"]))
        self.assertEqual(10, len({task["repository"] for task in manifest["selection"]["tasks"]}))
        canary = manifest["canary"]
        self.assertEqual("full", canary["arm"])
        self.assertEqual(pilot.PUBLIC_FEASIBILITY_IDS[0], canary["task"]["instance_id"])
        self.assertEqual(pilot.TASK_FIELDS, set(canary["task"]))
        self.assertNotIn(
            canary["task"]["instance_id"],
            {task["instance_id"] for task in manifest["selection"]["tasks"]},
        )
        self.assertNotIn(
            canary["task"]["repository"].casefold(),
            {task["repository"].casefold() for task in manifest["selection"]["tasks"]},
        )
        orders = [tuple(row["arm_order"]) for row in manifest["schedule"]]
        self.assertEqual(set(itertools.permutations(pilot.ARMS)), set(orders))
        self.assertLessEqual(max(orders.count(order) for order in set(orders)), 2)
        for position in range(len(pilot.ARMS)):
            counts = {arm: sum(order[position] == arm for order in orders) for arm in pilot.ARMS}
            self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        self.assertIsNone(manifest["model"]["observed"])
        self.assertEqual("gpt-5.6-sol-2026-07-31", manifest["model"]["revision"])
        self.assertEqual(pilot.executor_digest(), manifest["executor_sha256"])
        self.assertIn("0.146.0-linux-x64", manifest["runtime"]["codex_platform_packages"])
        self.assertEqual(1_800, manifest["cost_caps"]["max_wall_time_seconds"])
        self.assertEqual("10.00", manifest["cost_caps"]["max_total_usd"])
        self.assertNotIn("max_model_calls_per_run", manifest["cost_caps"])
        self.assertNotIn("results", manifest)
        self.assertTrue({
            "cached_input_tokens", "reasoning_tokens", "steps", "wall_time_seconds",
            "tool_calls", "patch_sha256", "spawn_count",
            "peak_parallel_agents", "durable_node_ids", "cap_verification",
            "grader_report_sha256",
        }.issubset(manifest["telemetry_required"]))
        self.assertTrue({"model_calls", "coordinator_tokens", "handoff_tokens"}.isdisjoint(
            manifest["telemetry_required"]
        ))
        skill_digest = hashlib.sha256(b"frozen treatment\n").hexdigest()
        expected_package = hashlib.sha256(f"SKILL.md\0{skill_digest}\n".encode()).hexdigest()
        self.assertEqual(expected_package, manifest["treatment"]["package_sha256"])
        self.assertEqual(expected_package, manifest["treatment"]["installed_package_sha256"])

        validated = pilot.validate_manifest(
            manifest_path=self.output,
            candidates_path=self.candidates,
            treatment_root=self.repo,
            installed_treatment=self.installed,
            prompt_paths=self.prompts,
            runner_path=self.runner,
        )
        self.assertEqual(manifest["manifest_sha256"], validated["manifest_sha256"])

        with self.assertRaises(FileExistsError):
            self._freeze()

    def test_replicates_balance_every_arm_position(self):
        manifests = []
        for replicate in range(3):
            if replicate:
                self.output = self.root / f"pilot-manifest-r{replicate}.json"
            manifests.append(self._freeze(replicate=replicate))
        self.assertTrue(all(manifests[0]["selection"] == item["selection"] for item in manifests[1:]))
        for manifest in manifests:
            for position in range(len(pilot.ARMS)):
                counts = {
                    arm: sum(row["arm_order"][position] == arm for row in manifest["schedule"])
                    for arm in pilot.ARMS
                }
                self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        for position in range(len(pilot.ARMS)):
            counts = {
                arm: sum(
                    row["arm_order"][position] == arm
                    for manifest in manifests
                    for row in manifest["schedule"]
                )
                for arm in pilot.ARMS
            }
            self.assertEqual({arm: 10 for arm in pilot.ARMS}, counts)

    def test_failed_gold_validation_is_replaced_within_language(self):
        def add_replacement(document):
            document["candidates"][0]["gold_attempts"] = 4
            document["candidates"].append(self._candidate(99, "Go"))

        self._write_candidates(add_replacement)
        selected_ids = {task["instance_id"] for task in self._freeze()["selection"]["tasks"]}
        self.assertNotIn("task-00", selected_ids)
        self.assertIn("task-99", selected_ids)

    def test_public_feasibility_canary_is_excluded_from_diagnostics(self):
        manifest = self._freeze()
        selected_ids = {task["instance_id"] for task in manifest["selection"]["tasks"]}
        self.assertTrue(selected_ids.isdisjoint(pilot.PUBLIC_FEASIBILITY_IDS))
        self.assertEqual(pilot.PUBLIC_FEASIBILITY_IDS[0], manifest["canary"]["task"]["instance_id"])

    def test_canary_contract_fails_closed(self):
        manifest = self._freeze()
        selected = manifest["selection"]["tasks"][0]
        cases = (
            ("schema", lambda candidate: candidate.pop("canary"), "manifest fields"),
            ("arm", lambda candidate: candidate["canary"].__setitem__("arm", "prompt"), "full arm"),
            (
                "task ID overlap",
                lambda candidate: candidate["canary"]["task"].__setitem__("instance_id", selected["instance_id"]),
                "task ID must be disjoint",
            ),
            (
                "repository overlap",
                lambda candidate: candidate["canary"]["task"].__setitem__("repository", selected["repository"]),
                "repository must be disjoint",
            ),
            (
                "task schema",
                lambda candidate: candidate["canary"]["task"].__setitem__("gold_passes", 3),
                "canary task fields",
            ),
        )
        for label, mutate, error in cases:
            with self.subTest(label):
                tampered = json.loads(json.dumps(manifest))
                mutate(tampered)
                tampered["manifest_sha256"] = pilot.document_digest(tampered)
                with self.assertRaisesRegex(pilot.PilotError, error):
                    pilot.validate_structure(tampered)

    def test_dataset_and_evaluator_identities_fail_closed(self):
        manifest = self._freeze()
        for field, value, error in (
            (("dataset", "name"), "SWE-bench-Live", "dataset identity"),
            (("evaluator", "repository"), "example/evaluator", "evaluator identity"),
        ):
            tampered = json.loads(json.dumps(manifest))
            tampered[field[0]][field[1]] = value
            tampered["manifest_sha256"] = pilot.document_digest(tampered)
            with self.assertRaisesRegex(pilot.PilotError, error):
                pilot.validate_structure(tampered)

        for field, value, error in (
            ("dataset_name", "SWE-bench-Live", "dataset identity"),
            ("evaluator_repository", "example/evaluator", "evaluator identity"),
        ):
            self._write_candidates(lambda document: document.__setitem__(field, value))
            with self.assertRaisesRegex(pilot.PilotError, error):
                self._freeze_to_new_output()

    def test_freeze_fails_closed_on_dirty_treatment_and_invalid_candidates(self):
        (self.repo / "dirty.txt").write_text("untracked\n", encoding="utf-8")
        with self.assertRaisesRegex(pilot.PilotError, "dirty"):
            self._freeze()
        (self.repo / "dirty.txt").unlink()

        (self.installed / "SKILL.md").write_text("different treatment\n", encoding="utf-8")
        with self.assertRaisesRegex(pilot.PilotError, "installed treatment"):
            self._freeze()
        (self.installed / "SKILL.md").write_bytes(
            subprocess.run(
                ["git", "-C", str(self.repo), "show", "HEAD:SKILL.md"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
        )

        self._write_candidates(lambda doc: doc["candidates"].__setitem__(0, {
            **doc["candidates"][0], "hidden_row_sha256": "latest"
        }))
        with self.assertRaisesRegex(pilot.PilotError, "digest"):
            self._freeze()

        self._write_candidates(lambda doc: doc["candidates"].__setitem__(0, {
            **doc["candidates"][0], "language": "Python"
        }))
        with self.assertRaisesRegex(pilot.PilotError, "quota"):
            self._freeze()

    def test_outcomes_and_incomplete_full_provenance_are_rejected(self):
        self._write_candidates(lambda doc: doc["candidates"][0].__setitem__("score", 1.0))
        with self.assertRaisesRegex(pilot.PilotError, "outcome"):
            self._freeze()

        self._write_candidates()
        manifest = self._freeze()
        manifest["arms"][2]["required_provenance"].remove("external_cap")
        manifest["manifest_sha256"] = pilot.document_digest(manifest)
        with self.assertRaisesRegex(pilot.PilotError, "full arm provenance"):
            pilot.validate_structure(manifest)

        manifest = self._freeze_to_new_output()
        manifest["arms"][1]["multi_agent_enabled"] = True
        manifest["manifest_sha256"] = pilot.document_digest(manifest)
        with self.assertRaisesRegex(pilot.PilotError, "only the full arm"):
            pilot.validate_structure(manifest)

    def test_validation_detects_mutated_inputs_and_manifest(self):
        manifest = self._freeze()
        self.runner.write_text("# changed runner\n", encoding="utf-8")
        with self.assertRaisesRegex(pilot.PilotError, "runner digest"):
            pilot.validate_manifest(
                self.output, self.candidates, self.repo, self.installed, self.prompts, self.runner
            )
        self.runner.write_text("# frozen runner\n", encoding="utf-8")
        manifest["schedule"][0]["arm_order"].reverse()
        self.output.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(pilot.PilotError, "manifest digest"):
            pilot.validate_manifest(
                self.output, self.candidates, self.repo, self.installed, self.prompts, self.runner
            )

    def _freeze_to_new_output(self):
        self.output = self.root / f"pilot-manifest-{len(list(self.root.glob('pilot-manifest-*.json')))}.json"
        return self._freeze()


if __name__ == "__main__":
    unittest.main()
