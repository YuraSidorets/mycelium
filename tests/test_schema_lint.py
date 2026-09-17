import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium.py"
INDEX_PS1_PATH = MODULE_PATH.with_name("mycelium-index.ps1")
LINT_PS1_PATH = MODULE_PATH.with_name("mycelium-lint.ps1")


class SchemaLintTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)

        result = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "node",
                "lint test goal", "good-node", "apex", "complete",
                "observed a good node", "none", "index next",
                "--topics", "lint",
                "--evidence", "fixture.md:1",
                "--confidence", "high",
                "--consumes", "none",
                "--blocks", "none",
                "--source-refs", "fixture.md:1",
                "--allow-untracked", "lint fixture",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

        self.bad_node_path = Path(".mycelium/nodes/bad-node.md")
        self.bad_node_path.write_text(
            "Goal: broken\nSpore: nothing recoverable here\n",
            encoding="utf-8",
        )

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def test_python_index_skips_malformed_node_with_warning(self):
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "index"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("1 skipped", result.stderr)

        index_text = Path(".mycelium/index.json").read_text(encoding="utf-8")
        self.assertIn("good-node", index_text)
        self.assertNotIn("bad-node", index_text)

    def test_python_lint_reports_malformed_node_and_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "lint"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("bad-node.md", result.stdout)
        self.assertNotIn("good-node.md", result.stdout)

    def test_powershell_index_and_lint_agree_with_python(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is not on PATH")

        index_result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(INDEX_PS1_PATH)],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, index_result.returncode, index_result.stderr)
        index_text = Path(".mycelium/index.json").read_text(encoding="utf-8")
        self.assertIn("good-node", index_text)
        self.assertNotIn("bad-node", index_text)

        lint_result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(LINT_PS1_PATH)],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, lint_result.returncode, lint_result.stderr)
        self.assertIn("bad-node.md", lint_result.stdout)
        self.assertNotIn("good-node.md", lint_result.stdout)


if __name__ == "__main__":
    unittest.main()
