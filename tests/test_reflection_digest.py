import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "mycelium.py"
SEARCH_PS1_PATH = ROOT / "bin" / "mycelium-search.ps1"


class ReflectionDigestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *arguments],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )

    def write_node(self, node_id, role, status, facts, *, topics, source_refs, **extra):
        arguments = [
            "node", "goal", node_id, role, status, facts, "none", "next",
            "--topics", topics, "--evidence", facts, "--confidence", "medium",
            "--consumes", "none", "--blocks", "none", "--source-refs", source_refs,
            "--allow-untracked", "digest fixture",
        ]
        for flag, value in extra.items():
            arguments.extend((f"--{flag.replace('_', '-')}", value))
        result = self.run_cli(*arguments)
        self.assertEqual(0, result.returncode, result.stderr)
        return result

    def test_reflect_regenerates_digest_naming_the_reflection(self):
        self.write_node(
            "failed-node", "apex", "alive", "attempted the thing",
            topics="attempt", source_refs="none",
        )
        result = self.run_cli(
            "reflect", "retry goal", "failed-node",
            "attempted the thing and it broke", "try the other approach",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        reflection_id = result.stdout.strip()
        self.assertEqual("reflection-failed-node", reflection_id)

        digest_path = Path(".mycelium/reflections-digest.md")
        self.assertTrue(digest_path.is_file())
        digest_text = digest_path.read_text(encoding="utf-8")
        self.assertIn(reflection_id, digest_text)
        self.assertIn("try the other approach", digest_text)

    def test_digest_drops_a_reflection_once_superseded(self):
        self.write_node(
            "failed-node", "apex", "alive", "attempted the thing",
            topics="attempt", source_refs="none",
        )
        reflect_result = self.run_cli(
            "reflect", "retry goal", "failed-node",
            "attempted the thing and it broke", "try the other approach",
        )
        reflection_id = reflect_result.stdout.strip()

        digest_before = Path(".mycelium/reflections-digest.md").read_text(encoding="utf-8")
        self.assertIn(reflection_id, digest_before)

        Path("fix.md").write_text("the fix landed\n", encoding="utf-8")
        self.write_node(
            "fix-node", "cap", "complete", "the fix landed",
            topics="fix", source_refs="fix.md:1", supersedes=reflection_id,
        )

        digest_result = self.run_cli("digest")
        self.assertEqual(0, digest_result.returncode, digest_result.stderr)
        digest_after = Path(".mycelium/reflections-digest.md").read_text(encoding="utf-8")
        self.assertNotIn(reflection_id, digest_after)

    def write_real_reflection_fixture(self):
        """A real `reflect` node next to a same-term non-reflection node whose
        source refs are current. `reflect` never sets `source_refs`, so this is
        the shape that used to be dropped as `stale-source-refs` before it
        could reach the reflection-first tie-break."""
        Path("source.md").write_text("widget fact line\n", encoding="utf-8")
        self.write_node(
            "plain-node", "apex", "alive", "widget fact line",
            topics="other", source_refs="source.md:1",
        )
        self.write_node(
            "failed-node", "apex", "alive", "attempted the thing",
            topics="attempt", source_refs="none",
        )
        result = self.run_cli(
            "reflect", "retry goal", "failed-node",
            "the widget check broke", "try the other approach",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        reflection_id = result.stdout.strip()
        self.assertEqual("reflection-failed-node", reflection_id)
        node_text = Path(f".mycelium/nodes/{reflection_id}.md").read_text(encoding="utf-8")
        self.assertIn("source_refs: \n", node_text)
        return reflection_id

    def test_search_ranks_a_real_reflect_node_that_has_no_source_refs(self):
        reflection_id = self.write_real_reflection_fixture()

        result = self.run_cli("search", "widget")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(f"{reflection_id}:stale-source-refs", result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        node_ids = [line.split("\t", 1)[0] for line in lines]
        self.assertIn("plain-node", node_ids)
        self.assertEqual(reflection_id, node_ids[0])

    def test_powershell_search_also_ranks_a_real_reflect_node(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is not on PATH")
        reflection_id = self.write_real_reflection_fixture()

        result = subprocess.run(
            [
                pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(SEARCH_PS1_PATH), "widget",
            ],
            cwd=Path.cwd(), capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn(f"{reflection_id}:stale-source-refs", result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        node_ids = [line.split("\t", 1)[0] for line in lines]
        self.assertIn("plain-node", node_ids)
        self.assertEqual(reflection_id, node_ids[0])

    def test_node_supersede_refreshes_the_digest_with_no_reflect_or_digest_call(self):
        self.write_node(
            "failed-node", "apex", "alive", "attempted the thing",
            topics="attempt", source_refs="none",
        )
        reflect_result = self.run_cli(
            "reflect", "retry goal", "failed-node",
            "attempted the thing and it broke", "try the other approach",
        )
        self.assertEqual(0, reflect_result.returncode, reflect_result.stderr)
        reflection_id = reflect_result.stdout.strip()
        digest_path = Path(".mycelium/reflections-digest.md")
        self.assertIn(reflection_id, digest_path.read_text(encoding="utf-8"))

        Path("fix.md").write_text("the fix landed\n", encoding="utf-8")
        self.write_node(
            "fix-node", "cap", "complete", "the fix landed",
            topics="fix", source_refs="fix.md:1", supersedes=reflection_id,
        )

        # No `reflect` and no `digest` call: the supersede itself has to
        # refresh the digest, or it keeps advertising a retired failure mode.
        self.assertNotIn(reflection_id, digest_path.read_text(encoding="utf-8"))

    def test_search_ranks_a_reflection_before_a_non_reflection_on_a_term_tie(self):
        Path("source.md").write_text("widget fact line\n", encoding="utf-8")
        self.write_node(
            "node-a-plain", "apex", "alive", "widget fact line",
            topics="other", source_refs="source.md:1",
        )
        self.write_node(
            "node-b-reflection", "cap", "blocked", "widget fact line",
            topics="reflection,verification", source_refs="source.md:1",
        )

        result = self.run_cli("search", "widget")
        self.assertEqual(0, result.returncode, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        node_ids = [line.split("\t", 1)[0] for line in lines]
        self.assertIn("node-b-reflection", node_ids)
        self.assertIn("node-a-plain", node_ids)
        self.assertLess(
            node_ids.index("node-b-reflection"),
            node_ids.index("node-a-plain"),
        )


if __name__ == "__main__":
    unittest.main()
