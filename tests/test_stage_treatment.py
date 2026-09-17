#!/usr/bin/env python3

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGER = ROOT / "evals" / "stage-treatment.py"
MANIFEST = ROOT / "evals" / "treatment-files.txt"


class TreatmentStageTests(unittest.TestCase):
    def run_stager(self, destination, *extra, source=ROOT, manifest=MANIFEST):
        return subprocess.run(
            [
                sys.executable,
                str(STAGER),
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--manifest",
                str(manifest),
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_stage_and_verify_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "treatment"
            staged = self.run_stager(destination, "--exact")
            self.assertEqual(staged.returncode, 0, staged.stderr)
            attestation = json.loads(staged.stdout)
            files = [line for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(attestation["file_count"], len(files))
            self.assertRegex(attestation["package_sha256"], r"^[0-9a-f]{64}$")
            for relative in files:
                self.assertEqual((ROOT / relative).read_bytes(), (destination / relative).read_bytes())

            verified = self.run_stager(destination, "--verify-only", "--exact")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["package_sha256"], attestation["package_sha256"])

            (destination / "unexpected").mkdir()
            rejected = self.run_stager(destination, "--verify-only", "--exact")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("unexpected treatment entries", rejected.stderr)
            (destination / "unexpected").rmdir()

            if sys.platform != "win32":
                (destination / "unexpected-link").symlink_to(destination / "SKILL.md")
                rejected = self.run_stager(destination, "--verify-only", "--exact")
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("symlink", rejected.stderr)
                (destination / "unexpected-link").unlink()

            (destination / "SKILL.md").write_text("tampered\n", encoding="utf-8")
            rejected = self.run_stager(destination, "--verify-only", "--exact")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("digest mismatch", rejected.stderr)

    def test_manifest_contains_the_tracked_lineage_runtime_and_contract(self):
        files = {
            line
            for line in MANIFEST.read_text(encoding="utf-8").splitlines()
            if line
        }
        self.assertTrue(
            {
                "bin/mycelium_lineage.py",
                "bin/mycelium-lineage.ps1",
                "bin/mycelium-lineage.sh",
                "docs/design/mycelium-process-lineage.md",
            }
            <= files
        )

    def test_rejects_manifest_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "aliases.txt"
            manifest.write_text("SKILL.md\n./SKILL.md\n", encoding="utf-8")
            result = self.run_stager(Path(temp) / "stage", manifest=manifest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-canonical treatment path", result.stderr)

    def test_commit_mode_stages_committed_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "repository"
            (repository / "evals").mkdir(parents=True)
            (repository / "payload.txt").write_text("committed\n", encoding="utf-8")
            manifest = repository / "evals" / "files.txt"
            manifest.write_text("payload.txt\n", encoding="utf-8")
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "Treatment Test"],
                ["git", "add", "."],
                ["git", "commit", "-qm", "fixture"],
            ):
                subprocess.run(command, cwd=repository, check=True, capture_output=True)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
            ).stdout.strip()
            (repository / "payload.txt").write_text("worktree drift\n", encoding="utf-8")

            destination = Path(temp) / "stage"
            result = self.run_stager(
                destination,
                "--commit",
                commit,
                source=repository,
                manifest=manifest,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / "payload.txt").read_text(encoding="utf-8"), "committed\n")

    def test_benchmark_uses_the_manifest_stager(self):
        runner = (ROOT / "evals" / "run-benchmark.ps1").read_text(encoding="utf-8")
        self.assertIn("stage-treatment.py", runner)
        self.assertIn("status --porcelain", runner)
        self.assertIn("--commit", runner)
        self.assertIn("benchmark-files.txt", runner)
        self.assertNotIn('Copy-Item -Recurse -LiteralPath "$Root/bin"', runner)


if __name__ == "__main__":
    unittest.main()
