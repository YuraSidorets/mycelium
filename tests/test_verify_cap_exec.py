"""Re-execution depth for the external CAP verifier.

Covers the `command`/`expect` node fields, the sandbox contract, and the
`Verification-Mode:` annotation that reports whether anything was re-run.
"""

import hashlib
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium.py"
LINEAGE_MODULE_PATH = MODULE_PATH.with_name("mycelium_lineage.py")
NODE_PS1_PATH = MODULE_PATH.with_name("mycelium-node.ps1")
VERIFY_CAP_PATH = MODULE_PATH.parents[1] / "evals" / "verify-cap.py"
# shlex.split() is POSIX, so a Windows interpreter path has to be quoted or its
# separators are read as escapes.
PYTHON = shlex.quote(sys.executable)


def load_lineage():
    spec = importlib.util.spec_from_file_location("mycelium_lineage_for_exec", LINEAGE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cap_verifier():
    spec = importlib.util.spec_from_file_location("mycelium_cap_verifier_exec", VERIFY_CAP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifierExpectationGrammarTest(unittest.TestCase):
    def setUp(self):
        self.verifier = load_cap_verifier()

    def test_substring_and_sha256_and_exit_prefix_parse(self):
        self.assertEqual((0, "substring", "ok"), self.verifier.parse_expectation(" ok "))
        digest = "a" * 64
        self.assertEqual(
            (0, "sha256", digest), self.verifier.parse_expectation(f"sha256:{digest}")
        )
        self.assertEqual((3, "substring", "ok"), self.verifier.parse_expectation("exit:3;ok"))
        self.assertEqual((1, "exit", ""), self.verifier.parse_expectation("exit:1;"))

    def test_malformed_expectations_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "expect field is empty"):
            self.verifier.parse_expectation("   ")
        with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
            self.verifier.parse_expectation("sha256:not-a-digest")
        with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
            self.verifier.parse_expectation("sha256:" + "A" * 64)

    def test_absent_expect_with_a_command_is_a_parse_error(self):
        with self.assertRaisesRegex(ValueError, "non-empty expect field"):
            self.verifier.reexecute_node_command(Path.cwd(), f"{PYTHON} -c pass", "none")

    def test_scrubbed_environment_exposes_only_the_allowed_keys(self):
        self.assertLessEqual(
            set(self.verifier.command_environment()),
            {"PATH", "SYSTEMROOT", "TEMP", "HOME"},
        )


class VerifierSandboxTest(unittest.TestCase):
    def setUp(self):
        self.verifier = load_cap_verifier()
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_stdout_sha256_is_normalized_across_line_endings(self):
        digest = hashlib.sha256(b"42\n").hexdigest()
        self.assertIsNone(
            self.verifier.reexecute_node_command(
                self.workspace, f'{PYTHON} -c "print(42)"', f"sha256:{digest}"
            )
        )
        self.assertIn(
            "sha256",
            self.verifier.reexecute_node_command(
                self.workspace, f'{PYTHON} -c "print(43)"', f"sha256:{digest}"
            ),
        )

    def test_non_zero_exit_needs_an_explicit_exit_prefix(self):
        command = f'{PYTHON} -c "raise SystemExit(7)"'
        self.assertIn(
            "exit code is 7",
            self.verifier.reexecute_node_command(self.workspace, command, "exit:0;"),
        )
        self.assertIsNone(
            self.verifier.reexecute_node_command(self.workspace, command, "exit:7;")
        )

    def test_command_runs_in_the_workspace_and_not_the_verifier_directory(self):
        (self.workspace / "marker.txt").write_text("here\n", encoding="utf-8")
        self.assertIsNone(
            self.verifier.reexecute_node_command(
                self.workspace,
                f'{PYTHON} -c "print(open(\'marker.txt\').read())"',
                "here",
            )
        )

    def test_unparsable_and_empty_commands_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "cannot be parsed"):
            self.verifier.reexecute_node_command(self.workspace, 'python -c "unclosed', "ok")
        with self.assertRaisesRegex(ValueError, "empty after parsing"):
            self.verifier.reexecute_node_command(self.workspace, "   ", "ok")

    def test_executables_outside_the_allowlist_are_rejected(self):
        for command in ("curl https://example.com", "cmd /c echo ok", "./payload.exe"):
            with self.assertRaisesRegex(ValueError, "is not allowed"):
                self.verifier.reexecute_node_command(self.workspace, command, "ok")

    def test_timeout_is_a_failure_not_a_skip(self):
        self.verifier.COMMAND_TIMEOUT_SECONDS = 1
        reason = self.verifier.reexecute_node_command(
            self.workspace, f'{PYTHON} -c "import time; time.sleep(30)"', "ok"
        )
        self.assertIsNotNone(reason)
        self.assertIn("timed out", reason)


class VerifierModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        Path("bin").mkdir()
        shutil.copyfile(LINEAGE_MODULE_PATH, Path("bin/mycelium_lineage.py"))
        Path("source.md").write_text(
            "apex fact\nlegacy fact\nstem fact\ncap fact\n", encoding="utf-8"
        )

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def write_node(
        self, node_id, fact, source_ref, *,
        role="apex", run_id=None, command="", expect="", process_inputs=(),
    ):
        arguments = [
            sys.executable, str(MODULE_PATH), "node",
            "goal", node_id, role, "complete", fact, "none", "next",
            "--topics", "topic", "--evidence", source_ref, "--confidence", "high",
            "--consumes", process_inputs[0] if process_inputs else "none",
            "--blocks", "none", "--source-refs", source_ref,
        ]
        for process_input in process_inputs:
            arguments.extend(("--process-input", process_input))
        if run_id:
            arguments.extend(("--run-id", run_id))
        else:
            arguments.extend(("--allow-untracked", "verifier fixture"))
        if command:
            arguments.extend(("--command", command, "--expect", expect))
        result = subprocess.run(arguments, cwd=Path.cwd(), capture_output=True, text=True)
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

    def verify(self, cap_file, run_id=None):
        arguments = [sys.executable, str(VERIFY_CAP_PATH), ".", str(cap_file)]
        if run_id is not None:
            arguments.extend(("--run-id", run_id))
        return subprocess.run(arguments, cwd=Path.cwd(), capture_output=True, text=True)

    def tracked_node(self, run_id, *, command="", expect=""):
        """Seal a three-role run whose APEX node carries the command under test."""
        lineage = load_lineage()
        lineage.begin("goal", "apex-stem-cap", run_id=run_id, root=Path.cwd())
        self.write_node(
            "a", "apex fact", "source.md:1",
            run_id=run_id, command=command, expect=expect,
        )
        self.write_node(
            "s", "stem fact", "source.md:3",
            role="stem", run_id=run_id, process_inputs=("a",),
        )
        self.write_node(
            "c", "cap fact", "source.md:4",
            role="cap", run_id=run_id, process_inputs=("s",),
        )
        lineage.seal(run_id, root=Path.cwd())
        return self.cap_file(f"cap-{run_id}.txt", "a", "apex fact")

    def test_node_without_a_command_reports_citation_only(self):
        cap = self.tracked_node("plain")
        result = self.verify(cap, "plain")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            [
                "Verification: PASS",
                "Verification-Mode: citation-only",
                "Verified nodes: 1",
                "Verified run: plain",
            ],
            result.stdout.splitlines(),
        )

    def test_matching_command_reports_re_executed(self):
        cap = self.tracked_node("proven", command=f'{PYTHON} -c "print(42)"', expect="42")
        result = self.verify(cap, "proven")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("Verification: PASS", result.stdout.splitlines()[0])
        self.assertEqual("Verification-Mode: re-executed", result.stdout.splitlines()[1])

    def test_mismatching_expect_fails_the_whole_verification(self):
        cap = self.tracked_node("wrong", command=f'{PYTHON} -c "print(42)"', expect="nope")
        result = self.verify(cap, "wrong")
        self.assertEqual(1, result.returncode)
        self.assertIn("Verification: FAIL", result.stdout)
        self.assertIn("stdout does not contain the expected text: a", result.stdout)
        self.assertNotIn("Verification-Mode", result.stdout)

    def test_disallowed_executable_fails_the_whole_verification(self):
        cap = self.tracked_node("denied", command="curl https://example.com", expect="ok")
        result = self.verify(cap, "denied")
        self.assertEqual(1, result.returncode)
        self.assertIn("node command executable is not allowed: curl", result.stdout)

    def test_untracked_cap_never_re_executes_a_command(self):
        self.write_node(
            "legacy", "legacy fact", "source.md:2",
            command="curl https://example.com", expect="ok",
        )
        cap = self.cap_file("legacy-cap.txt", "legacy", "legacy fact")
        result = self.verify(cap)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            ["Verification: PASS", "Verification-Mode: citation-only", "Verified nodes: 1"],
            result.stdout.splitlines(),
        )


if __name__ == "__main__":
    unittest.main()
