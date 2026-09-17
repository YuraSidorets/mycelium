import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium.py"
STEM_PS1_PATH = MODULE_PATH.with_name("stem.ps1")
GOAL = "cache mode conflict"


class StemParityTest(unittest.TestCase):
    """Locks the STEM claim-conflict and duplicate-source checks to
    identical stdout on the Python and PowerShell implementations.

    Fixture: node A and node B assert different values for the same
    subject ("Cache mode is enabled." / "Cache mode is disabled.") and
    cite the same source ref (notes.md:1-3), so both the conflict check
    (stem.ps1:475-482 / mycelium.py:1077-1119) and the duplicate-source
    check fire. Node C is unrelated and should be ignored.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)

        self._run(["git", "init", "-q"])
        Path("notes.md").write_text(
            "Cache mode is enabled.\n"
            "Cache mode is disabled.\n"
            "Something unrelated happened.\n",
            encoding="utf-8",
        )
        self._run(["git", "add", "notes.md"])
        self._run(
            [
                "git",
                "-c", "user.name=mycelium",
                "-c", "user.email=mycelium@example.invalid",
                "commit", "-qm", "init",
            ]
        )

        self._write_node("cap-cache-mode-a-parity", "- Cache mode is enabled.", "notes.md:1-1")
        self._write_node("cap-cache-mode-b-parity", "- Cache mode is disabled.", "notes.md:2-2")
        self._write_node("cap-unrelated-c-parity", "Something unrelated happened.", "notes.md:3-3")

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def _run(self, arguments):
        result = subprocess.run(
            arguments, cwd=Path.cwd(), capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result

    def _write_node(self, node_id, fact, evidence):
        result = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "node",
                GOAL, node_id, "apex", "complete", fact, "none", "next",
                "--topics", "cache,mode",
                "--evidence", evidence,
                "--confidence", "high",
                "--consumes", "none",
                "--blocks", "none",
                "--source-refs", "notes.md:1-3",
                "--allow-untracked", "parity fixture",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            input="",
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def _run_python_stem(self, *extra_args):
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "stem", GOAL, *extra_args],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            input="",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def _run_powershell_stem(self, *extra_args):
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(STEM_PS1_PATH), "-Goal", GOAL, *extra_args],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            input="",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.replace("\r\n", "\n")

    def test_for_cap_conflicts_match_between_platforms(self):
        # --for-cap short-circuits on conflicts and returns only the
        # Conflicts: block (both platforms), before duplicate-source
        # warnings would be computed on stdout.
        python_output = self._run_python_stem("--for-cap")
        self.assertTrue(python_output.startswith("Conflicts:\n"))
        self.assertIn("cache mode differs:", python_output)
        self.assertIn("says enabled", python_output)
        self.assertIn("says disabled", python_output)

        pwsh = shutil.which("pwsh")
        if pwsh:
            powershell_output = self._run_powershell_stem("-ForCap")
            self.assertEqual(python_output, powershell_output)

    def test_conflicts_and_duplicate_sources_match_between_platforms(self):
        # The full (non --for-cap) report surfaces both checks together:
        # the Conflicts: block and the Duplicate source warnings: block.
        python_output = self._run_python_stem()
        self.assertIn("Conflicts:", python_output)
        self.assertIn("cache mode differs:", python_output)
        self.assertIn("says enabled", python_output)
        self.assertIn("says disabled", python_output)
        self.assertIn("Duplicate source warnings:", python_output)
        self.assertIn("notes.md:1-3 cited by", python_output)

        pwsh = shutil.which("pwsh")
        if pwsh:
            powershell_output = self._run_powershell_stem()
            self.assertEqual(python_output, powershell_output)
        else:
            # No pwsh on PATH: assert the Python output alone contains
            # both the conflict and the duplicate-source evidence.
            self.assertIn("cache mode differs: cap-cache-mode-", python_output)
            self.assertIn("notes.md:1-3 cited by cap-cache-mode-", python_output)


if __name__ == "__main__":
    unittest.main()
