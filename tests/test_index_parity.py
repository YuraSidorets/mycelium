import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium.py"
INDEX_PS1_PATH = MODULE_PATH.with_name("mycelium-index.ps1")
SEARCH_PS1_PATH = MODULE_PATH.with_name("mycelium-search.ps1")

# Keys whose values legitimately differ between two independent runs even
# though both sides normalize them (e.g. sub-second timestamp formatting
# quirks). Excluded from the value comparison below.
TIMESTAMP_LIKE_KEYS = {"updated"}


class IndexParityTest(unittest.TestCase):
    """Pins whether bin/mycelium.py's build_index and bin/mycelium-index.ps1
    produce the same .mycelium/index.json for the same on-disk nodes.

    Python's build_index (bin/mycelium.py) copies every key of a node dict
    into the index. mycelium-index.ps1 writes a fixed, hand-maintained list
    of properties. Nothing else detects drift between the two lists, so this
    test exists to notice it.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)

        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )

        Path("fixture.md").write_text(
            "line one\nline two\nline three\n",
            encoding="utf-8",
        )

        # Plain node: no supersedes, no explicit source-refs.
        result_a = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "node",
                "index parity fixture", "parity-node-a", "apex", "complete",
                "fact one", "none", "none",
                "--topics", "parity",
                "--evidence", "fixture.md:1",
                "--confidence", "high",
                "--consumes", "none",
                "--blocks", "none",
                "--allow-untracked", "index parity fixture",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result_a.returncode, result_a.stderr)

        # Second node: supersedes the first, non-empty source-refs pointing
        # at a real file.
        result_b = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "node",
                "index parity fixture", "parity-node-b", "stem", "complete",
                "fact two", "none", "none",
                "--topics", "parity",
                "--evidence", "fixture.md:1",
                "--confidence", "high",
                "--consumes", "parity-node-a",
                "--blocks", "none",
                "--supersedes", "parity-node-a",
                "--source-refs", "fixture.md:1",
                "--allow-untracked", "index parity fixture",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result_b.returncode, result_b.stderr)

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    @staticmethod
    def _normalize(item):
        normalized = {}
        for key, value in item.items():
            if key in TIMESTAMP_LIKE_KEYS:
                continue
            if isinstance(value, list):
                value = sorted(value)
            normalized[key] = value
        return normalized

    def test_python_and_powershell_index_agree(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is not on PATH")

        python_result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "index"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, python_result.returncode, python_result.stderr)
        python_index = json.loads(Path(".mycelium/index.json").read_text(encoding="utf-8"))
        python_by_id = {item["nodeId"]: item for item in python_index}

        ps1_result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(INDEX_PS1_PATH)],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, ps1_result.returncode, ps1_result.stderr)
        ps1_index = json.loads(Path(".mycelium/index.json").read_text(encoding="utf-8"))
        ps1_by_id = {item["nodeId"]: item for item in ps1_index}

        self.assertEqual(
            {"parity-node-a", "parity-node-b"},
            set(python_by_id.keys()),
            "python index is missing an expected fixture node",
        )
        self.assertEqual(
            set(python_by_id.keys()),
            set(ps1_by_id.keys()),
            "python and powershell index disagree on which nodeIds are present",
        )

        for node_id in python_by_id:
            python_item = python_by_id[node_id]
            ps1_item = ps1_by_id[node_id]

            self.assertEqual(
                set(python_item.keys()),
                set(ps1_item.keys()),
                f"python and powershell index disagree on key set for {node_id!r}",
            )

            python_normalized = self._normalize(python_item)
            ps1_normalized = self._normalize(ps1_item)
            self.assertEqual(
                python_normalized,
                ps1_normalized,
                f"python and powershell index values disagree for {node_id!r}",
            )

    def test_python_and_powershell_search_agree_on_reflection_only_match(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is not on PATH")

        # A node whose only occurrence of the query term is in the Reflection
        # body field: not in facts, evidence, topics, source_refs, etc. This
        # pins that both platforms' SEARCH_FIELDS-equivalents include
        # "reflection" and that the index carries the field through.
        result = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "node",
                "reflection search fixture", "node-reflect-only", "cap", "complete",
                "cap fact unrelated", "none", "wake stem",
                "--topics", "reflection,verification",
                "--evidence", "none",
                "--confidence", "high",
                "--consumes", "none",
                "--blocks", "none",
                "--reflection", "zorptastic-unique-term",
                "--allow-untracked", "reflection search fixture",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

        python_result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "search", "zorptastic-unique-term"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, python_result.returncode, python_result.stderr)

        index_path = Path(".mycelium/index.json")
        if index_path.exists():
            index_path.unlink()

        ps1_result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(SEARCH_PS1_PATH), "zorptastic-unique-term"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, ps1_result.returncode, ps1_result.stderr)

        self.assertIn("node-reflect-only", python_result.stdout)
        self.assertIn("match=reflection:zorptastic-unique-term", python_result.stdout)
        self.assertEqual(python_result.stdout, ps1_result.stdout)


if __name__ == "__main__":
    unittest.main()
