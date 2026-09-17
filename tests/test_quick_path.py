import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
QUICK_PS1 = REPO_ROOT / "bin" / "mycelium-quick.ps1"
QUICK_SH = REPO_ROOT / "bin" / "mycelium-quick.sh"
LINEAGE_MODULE = REPO_ROOT / "bin" / "mycelium_lineage.py"


def _verify(root, run_id):
    result = subprocess.run(
        [
            sys.executable,
            str(LINEAGE_MODULE),
            "--root",
            str(root),
            "verify",
            "--run-id",
            run_id,
        ],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout), result


def _last_json_line(stdout):
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    return json.loads(lines[-1])


class QuickPathTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_quick_path_powershell_seals_and_verifies(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not on PATH")

        result = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-File",
                str(QUICK_PS1),
                "-Goal",
                "quick path powershell test",
                "-ApexFacts",
                "apex fact",
                "-ApexEvidence",
                "source.md:1",
                "-StemFacts",
                "stem fact",
                "-CapFacts",
                "cap fact",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        seal_result = _last_json_line(result.stdout)
        self.assertEqual("sealed", seal_result["state"])
        self.assertTrue(seal_result["valid"])

        report, verify_result = _verify(self.root, seal_result["run_id"])
        self.assertEqual(0, verify_result.returncode, verify_result.stderr)
        self.assertEqual("sealed", report["state"])
        self.assertTrue(report["valid"])

    def test_quick_path_bash_seals_and_verifies(self):
        python3 = shutil.which("python3")
        if python3 is None:
            self.skipTest("python3 is not on PATH")
        probe = subprocess.run(
            [python3, "-c", "import sys"], capture_output=True, text=True
        )
        if probe.returncode != 0:
            self.skipTest(
                "python3 on PATH is not a working interpreter "
                "(e.g. the Windows Store alias stub)"
            )

        shell = shutil.which("bash") or shutil.which("sh")
        if shell is None and os.name == "nt":
            git_bash = (
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Git"
                / "bin"
                / "bash.exe"
            )
            shell = str(git_bash) if git_bash.is_file() else None
        if shell is None:
            self.skipTest("POSIX shell is unavailable")

        result = subprocess.run(
            [
                shell,
                str(QUICK_SH),
                "quick path bash test",
                "apex fact",
                "source.md:1",
                "stem fact",
                "cap fact",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        seal_result = _last_json_line(result.stdout)
        self.assertEqual("sealed", seal_result["state"])
        self.assertTrue(seal_result["valid"])

        report, verify_result = _verify(self.root, seal_result["run_id"])
        self.assertEqual(0, verify_result.returncode, verify_result.stderr)
        self.assertEqual("sealed", report["state"])
        self.assertTrue(report["valid"])


if __name__ == "__main__":
    unittest.main()
