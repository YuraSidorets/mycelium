#!/usr/bin/env python3
"""Static checks for the Claude Code plugin adapter.

Nothing here spends a model call and nothing here writes under the
repository's own `.mycelium/`. Every hook or writer invocation runs with its
working directory and `CLAUDE_PROJECT_DIR` inside a temporary directory.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / ".claude-plugin" / "plugin.json"
PAYLOAD_MANIFEST_PATH = ROOT / "claude" / "plugin-files.txt"
RUN_SKILL_PATH = ROOT / "claude" / "skills" / "run" / "SKILL.md"
HOOKS_JSON_PATH = ROOT / "claude" / "hooks" / "hooks.json"
HOOK_SCRIPT_PATH = ROOT / "claude" / "hooks" / "mycelium_hook.py"
STAGER_PATH = ROOT / "evals" / "stage-treatment.py"
NODE_PY_PATH = ROOT / "bin" / "mycelium.py"
NODE_PS1_PATH = ROOT / "bin" / "mycelium-node.ps1"
WORKER_ROLES = ("apex", "septum", "hyphae", "cap")
SPAWN_TRACE = "Action: Agent; Observation: returned agent apex-one; Result: fact recorded"
SPAWN_ERROR = "Trace names a spawn tool; ProducingAgent and Action/Observation/Result are required."


def load_hook_module():
    spec = importlib.util.spec_from_file_location("mycelium_claude_hook", HOOK_SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"{path} has no YAML frontmatter block")
    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip() != line:
            continue
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return fields, text


class PluginManifestTests(unittest.TestCase):
    def test_manifest_parses_and_declares_the_adapter(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "mycelium")
        self.assertTrue(manifest["description"].strip())
        self.assertNotIn("mcpServers", manifest, "the port plan rejects an MCP server")

        option = manifest["userConfig"]["python_executable"]
        self.assertEqual(option["type"], "string")
        self.assertTrue(option["title"].strip())
        self.assertTrue(option["description"].strip())

        for field in ("skills", "hooks"):
            value = manifest[field]
            paths = value if isinstance(value, list) else [value]
            for relative in paths:
                self.assertTrue(
                    (ROOT / relative.lstrip("./")).exists(),
                    f"manifest {field} path does not exist: {relative}",
                )

    def test_manifest_records_the_attested_baseline_commit(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        metadata = manifest["metadata"]
        self.assertRegex(metadata["baselineCommit"], r"^[0-9a-f]{40}$")
        self.assertRegex(metadata["baselinePackageSha256"], r"^[0-9a-f]{64}$")
        self.assertTrue((ROOT / metadata["baselineManifest"]).is_file())
        self.assertTrue((ROOT / metadata["pluginManifest"]).is_file())

    def test_no_stem_agent_is_defined(self):
        # The main session is STEM. A STEM worker would be a second trunk.
        self.assertFalse((ROOT / "agents" / "stem.md").exists())


class PayloadManifestTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            line
            for line in PAYLOAD_MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_every_payload_path_exists_and_is_canonical(self):
        self.assertTrue(self.entries)
        self.assertEqual(len(self.entries), len(set(self.entries)), "duplicate payload path")
        for relative in self.entries:
            self.assertNotIn("\\", relative, f"non-canonical payload path: {relative}")
            self.assertNotIn("..", Path(relative).parts, f"escaping payload path: {relative}")
            self.assertTrue((ROOT / relative).is_file(), f"missing payload file: {relative}")

    def test_payload_carries_the_runtime_role_contracts_and_verifier(self):
        required = {
            "SKILL.md",
            "apex.md",
            "septum.md",
            "hyphae.md",
            "stem.md",
            "cap.md",
            "evals/verify-cap.py",
            "bin/mycelium.py",
            "bin/mycelium_lineage.py",
            "bin/mycelium-node.ps1",
            "bin/mycelium-node.sh",
            ".claude-plugin/plugin.json",
            "claude/hooks/hooks.json",
            "claude/hooks/mycelium_hook.py",
            "claude/skills/run/SKILL.md",
        }
        missing = sorted(required - set(self.entries))
        self.assertEqual([], missing, f"payload manifest is missing: {missing}")
        for role in WORKER_ROLES:
            self.assertIn(f"agents/{role}.md", self.entries)

    def test_the_codex_treatment_manifest_is_untouched_by_this_payload(self):
        treatment = [
            line
            for line in (ROOT / "evals" / "treatment-files.txt").read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertNotIn("evals/verify-cap.py", treatment)
        self.assertNotIn("claude/plugin-files.txt", treatment)
        self.assertTrue(set(treatment).issubset(set(self.entries)))

    def test_payload_stages_exactly_from_the_working_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "stage"
            result = subprocess.run(
                [
                    sys.executable, "-B", str(STAGER_PATH),
                    "--source", str(ROOT),
                    "--destination", str(destination),
                    "--manifest", str(PAYLOAD_MANIFEST_PATH),
                    "--exact",
                ],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            attestation = json.loads(result.stdout)
            self.assertEqual(attestation["file_count"], len(self.entries))
            self.assertRegex(attestation["package_sha256"], r"^[0-9a-f]{64}$")


class WorkerAgentDefinitionTests(unittest.TestCase):
    def test_each_worker_forbids_the_agent_tool(self):
        for role in WORKER_ROLES:
            path = ROOT / "agents" / f"{role}.md"
            with self.subTest(role=role):
                fields, text = frontmatter(path)
                self.assertEqual(role, fields.get("name"))
                self.assertTrue(fields.get("description", "").strip())

                disallowed = [item.strip() for item in fields.get("disallowedTools", "").split(",")]
                self.assertIn("Agent", disallowed, f"{role} does not disallow Agent")

                allowed = [item.strip() for item in fields.get("tools", "").split(",")]
                self.assertTrue(allowed and allowed[0], f"{role} declares no tools")
                self.assertNotIn("Agent", allowed, f"{role} lists Agent as an allowed tool")

                self.assertIn("${CLAUDE_PLUGIN_ROOT}/" + role + ".md", text)
                self.assertNotIn("subagent_type", text)

    def test_each_worker_requires_the_output_contract_fields(self):
        contract = (
            "Node ID", "Run ID", "Process Inputs", "Status", "Topics", "Facts",
            "Evidence", "Confidence", "Consumes", "Questions", "Blocks", "Next", "Trace",
        )
        for role in WORKER_ROLES:
            text = (ROOT / "agents" / f"{role}.md").read_text(encoding="utf-8")
            for field in contract:
                with self.subTest(role=role, field=field):
                    self.assertIn(field, text)

    def test_cap_still_defers_verification(self):
        text = (ROOT / "agents" / "cap.md").read_text(encoding="utf-8")
        self.assertIn("Verification: EXTERNAL_REQUIRED", text)


class RunSkillTests(unittest.TestCase):
    def test_run_skill_loads_the_shared_contract_and_names_the_host_primitive(self):
        fields, text = frontmatter(RUN_SKILL_PATH)
        self.assertEqual("run", fields.get("name"))
        self.assertTrue(fields.get("description", "").strip())
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/SKILL.md", text)
        self.assertIn("`Agent`", text)
        self.assertIn("bin/mycelium-node.ps1", text)
        self.assertIn("-RunId", text)
        for role in WORKER_ROLES:
            self.assertIn(f"mycelium:{role}", text)
        self.assertNotIn("mycelium:stem", text)


class HooksConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.configuration = json.loads(HOOKS_JSON_PATH.read_text(encoding="utf-8"))["hooks"]

    def test_only_activation_and_stop_are_wired(self):
        self.assertEqual({"UserPromptExpansion", "Stop"}, set(self.configuration))

    def test_every_handler_runs_the_hook_script_with_the_configured_interpreter(self):
        for event, groups in self.configuration.items():
            for group in groups:
                for handler in group["hooks"]:
                    with self.subTest(event=event):
                        self.assertEqual("command", handler["type"])
                        self.assertEqual("${user_config.python_executable}", handler["command"])
                        self.assertIn(
                            "${CLAUDE_PLUGIN_ROOT}/claude/hooks/mycelium_hook.py",
                            handler["args"],
                        )

    def test_activation_matches_the_run_command_only(self):
        matcher = self.configuration["UserPromptExpansion"][0]["matcher"]
        pattern = re.compile(matcher)
        self.assertTrue(pattern.match("mycelium:run"))
        self.assertTrue(pattern.match("run"))
        self.assertIsNone(pattern.match("mycelium:runaway"))
        self.assertIsNone(pattern.match("deploy"))


class HookBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, event, payload):
        environment = dict(os.environ)
        environment["CLAUDE_PROJECT_DIR"] = str(self.project)
        environment["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", str(HOOK_SCRIPT_PATH), "--event", event],
            input=json.dumps(payload),
            check=False, capture_output=True, text=True,
            cwd=str(self.project), env=environment,
        )

    def stop_payload(self, session_id="session-abc123"):
        return {
            "session_id": session_id,
            "transcript_path": str(self.project / "transcript.jsonl"),
            "cwd": str(self.project),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }

    def test_stop_exits_zero_and_silent_with_no_marker(self):
        result = self.run_hook("Stop", self.stop_payload())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())
        self.assertFalse((self.project / ".mycelium").exists(), "the no-marker path must not write")

    def test_stop_exits_zero_with_empty_stdin(self):
        result = subprocess.run(
            [sys.executable, "-B", str(HOOK_SCRIPT_PATH), "--event", "Stop"],
            input="", check=False, capture_output=True, text=True, cwd=str(self.project),
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_activation_binds_the_session_under_the_project_directory(self):
        payload = self.stop_payload()
        payload["hook_event_name"] = "UserPromptExpansion"
        result = self.run_hook("UserPromptExpansion", payload)
        self.assertEqual(0, result.returncode, result.stderr)
        record = self.project / ".mycelium" / "claude" / "session-abc123" / "session.json"
        self.assertTrue(record.is_file())
        self.assertEqual("session-abc123", json.loads(record.read_text(encoding="utf-8"))["sessionId"])

    def test_activation_refuses_a_session_id_that_is_a_path(self):
        payload = self.stop_payload(session_id="../escape")
        payload["hook_event_name"] = "UserPromptExpansion"
        result = self.run_hook("UserPromptExpansion", payload)
        self.assertNotEqual(2, result.returncode, "a path violation must not erase the prompt")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.project.parent / "escape").exists())

    def test_the_completion_gate_reads_the_verifier_by_field_name(self):
        module = load_hook_module()
        self.assertTrue(callable(module.gate_active_run))
        self.assertIn("Verification-Mode:", module.gate_active_run.__doc__)
        # `Verification-Mode:` sits between `Verification: PASS` and
        # `Verified nodes:`, so positional reads are wrong by construction.
        verifier_output = (
            "Verification: PASS\n"
            "Verification-Mode: citation-only\n"
            "Verified nodes: 3\n"
            "Verified run: run-1\n"
        )
        self.assertEqual("PASS", module.output_field(verifier_output, "Verification"))
        self.assertEqual(
            "citation-only", module.output_field(verifier_output, "Verification-Mode")
        )
        self.assertEqual("3", module.output_field(verifier_output, "Verified nodes"))
        self.assertIsNone(module.output_field(verifier_output, "Reason"))

    def test_a_missing_marker_never_blocks(self):
        # A marker that is absent and a marker that cannot be parsed are two
        # different facts. Absent means the session was never tracked, so it
        # still allows the stop; unparseable blocks (see CompletionGateTests).
        module = load_hook_module()
        noise = io.StringIO()
        with contextlib.redirect_stderr(noise):
            self.assertIsNone(module.gate_active_run(self.project / "absent.json", {}))
        self.assertIn("missing", noise.getvalue())


class CompletionGateTests(unittest.TestCase):
    """The Stop gate, proven against a temporary project. Nothing here spends a
    model call; the only subprocesses are the two verifiers."""

    SESSION_ID = "session-gate1"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        self.marker_directory = self.project / ".mycelium" / "claude" / self.SESSION_ID
        self.marker_directory.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def load_lineage(self):
        spec = importlib.util.spec_from_file_location(
            "mycelium_lineage_for_gate", ROOT / "bin" / "mycelium_lineage.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def write_marker(self, **fields):
        (self.marker_directory / "run.json").write_text(
            json.dumps(fields, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def run_stop(self, stop_hook_active=False):
        environment = dict(os.environ)
        environment["CLAUDE_PROJECT_DIR"] = str(self.project)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.pop("CLAUDE_PLUGIN_ROOT", None)
        payload = {
            "session_id": self.SESSION_ID,
            "cwd": str(self.project),
            "hook_event_name": "Stop",
            "stop_hook_active": stop_hook_active,
        }
        return subprocess.run(
            [sys.executable, "-B", str(HOOK_SCRIPT_PATH), "--event", "Stop"],
            input=json.dumps(payload),
            check=False, capture_output=True, text=True,
            cwd=str(self.project), env=environment,
        )

    def write_tracked_node(self, run_id, node_id, role, fact, source_ref, *inputs):
        arguments = [
            sys.executable, "-B", str(NODE_PY_PATH), "node",
            "gate goal", node_id, role, "complete", fact, "none", "next",
            "--topics", "topic", "--evidence", source_ref, "--confidence", "high",
            "--consumes", inputs[0] if inputs else "none", "--blocks", "none",
            "--source-refs", source_ref, "--run-id", run_id,
        ]
        for process_input in inputs:
            arguments.extend(("--process-input", process_input))
        result = subprocess.run(
            arguments, cwd=str(self.project), check=False, capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def begin_run(self, run_id="gate-run"):
        lineage = self.load_lineage()
        (self.project / "source.md").write_text(
            "apex fact\nstem fact\ncap fact\n", encoding="utf-8"
        )
        lineage.begin("gate goal", "apex-stem-cap", run_id=run_id, root=self.project)
        return lineage

    def seal_and_cite(self, lineage, run_id="gate-run"):
        self.write_tracked_node(run_id, "gate-apex", "apex", "apex fact", "source.md:1")
        self.write_tracked_node(
            run_id, "gate-stem", "stem", "stem fact", "source.md:2", "gate-apex"
        )
        self.write_tracked_node(
            run_id, "gate-cap", "cap", "cap fact", "source.md:3", "gate-stem"
        )
        report = lineage.seal(run_id, root=self.project)
        self.assertEqual("sealed", report["state"])
        node_path = self.project / ".mycelium" / "nodes" / "gate-apex.md"
        line_number = next(
            index
            for index, line in enumerate(
                node_path.read_text(encoding="utf-8").splitlines(), 1
            )
            if line == "apex fact"
        )
        cap_file = self.project / "cap.txt"
        cap_file.write_text(
            "Verification: EXTERNAL_REQUIRED\n"
            f"- [gate-apex] apex fact (.mycelium/nodes/gate-apex.md:{line_number})\n",
            encoding="utf-8",
        )
        return "cap.txt"

    def test_no_marker_lets_the_session_stop(self):
        result = self.run_stop()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_an_open_run_blocks_with_a_json_decision_and_a_command(self):
        self.begin_run()
        self.write_marker(
            run_id="gate-run", goal="gate goal", cap_file="cap.txt", state="active"
        )
        result = self.run_stop()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        decision = json.loads(result.stdout.strip().splitlines()[0])
        self.assertEqual("block", decision["decision"])
        self.assertIn("gate-run", decision["reason"])
        self.assertIn("not 'sealed'", decision["reason"])
        self.assertIn("seal --run-id", decision["reason"])
        self.assertIn(decision["reason"], result.stderr)

    def test_an_unknown_run_blocks_and_names_the_lineage_check(self):
        self.write_marker(run_id="absent-run", cap_file="cap.txt", state="active")
        result = self.run_stop()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        decision = json.loads(result.stdout.strip().splitlines()[0])
        self.assertIn("mycelium_lineage verify", decision["reason"])

    def test_a_sealed_run_with_no_cap_file_blocks(self):
        lineage = self.begin_run()
        self.seal_and_cite(lineage)
        self.write_marker(run_id="gate-run", goal="gate goal", state="active")
        result = self.run_stop()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("cap_file", json.loads(result.stdout.strip().splitlines()[0])["reason"])

    def test_a_sealed_run_whose_cap_does_not_verify_blocks(self):
        lineage = self.begin_run()
        self.seal_and_cite(lineage)
        (self.project / "bad-cap.txt").write_text(
            "Verification: EXTERNAL_REQUIRED\n"
            "- [gate-apex] invented fact (.mycelium/nodes/gate-apex.md:1)\n",
            encoding="utf-8",
        )
        self.write_marker(run_id="gate-run", cap_file="bad-cap.txt", state="active")
        result = self.run_stop()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        reason = json.loads(result.stdout.strip().splitlines()[0])["reason"]
        self.assertIn("evals/verify-cap.py", reason)
        self.assertIn("--run-id", reason)

    def test_a_sealed_and_verified_run_lets_the_session_stop(self):
        lineage = self.begin_run()
        cap_file = self.seal_and_cite(lineage)
        self.write_marker(
            run_id="gate-run", goal="gate goal", cap_file=cap_file,
            cap_node_path=".mycelium/nodes/gate-cap.md", state="active",
        )
        result = self.run_stop()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_stop_hook_active_allows_the_stop_with_a_note(self):
        self.begin_run()
        self.write_marker(run_id="gate-run", cap_file="cap.txt", state="active")
        result = self.run_stop(stop_hook_active=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual("", result.stdout.strip())
        self.assertIn("stop_hook_active", result.stderr)
        self.assertIn("NOT verified", result.stderr)

    def test_a_blocked_run_may_stop_without_claiming_success(self):
        self.begin_run()
        self.write_marker(run_id="gate-run", cap_file="cap.txt", state="blocked")
        result = self.run_stop()
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual("", result.stdout.strip())
        self.assertIn("blocked", result.stderr)
        self.assertNotIn("PASS", result.stderr)

    def test_a_done_marker_may_stop(self):
        self.write_marker(run_id="gate-run", state="done")
        result = self.run_stop()
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual("", result.stdout.strip())

    def test_an_unreadable_marker_blocks_instead_of_bypassing_the_gate(self):
        # An unreadable marker is not evidence that no run is active; it is
        # evidence that the run state is unknown. Allowing the stop here turned
        # a corrupt file into a silent bypass of the whole completion gate.
        marker = self.marker_directory / "run.json"
        marker.write_text("{ not json", encoding="utf-8")
        result = self.run_stop()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        reason = json.loads(result.stdout.strip().splitlines()[0])["reason"]
        self.assertIn(str(marker), reason)
        self.assertIn("unreadable", reason)
        self.assertIn("mycelium-mark-run.py blocked", reason)
        self.assertIn(f'--session-id "{self.SESSION_ID}"', reason)
        self.assertIn(reason, result.stderr)

    def test_an_unreadable_marker_may_stop_when_stop_hook_active_is_set(self):
        # A Stop hook must never be the reason a session cannot end twice.
        (self.marker_directory / "run.json").write_text("{ not json", encoding="utf-8")
        result = self.run_stop(stop_hook_active=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout.strip())
        self.assertIn("stop_hook_active", result.stderr)
        self.assertIn("NOT verified", result.stderr)

    def test_a_marker_that_is_valid_json_but_not_an_object_blocks(self):
        (self.marker_directory / "run.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = self.run_stop()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn(
            "not a JSON object",
            json.loads(result.stdout.strip().splitlines()[0])["reason"],
        )


class SpawnTraceRecordingTests(unittest.TestCase):
    """The writer cannot prove a spawn happened, but a Trace that names one
    must carry the fields a real spawn would have produced."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        shutil.copyfile(ROOT / "bin" / "mycelium_lineage.py", self.workspace / "mycelium_lineage.py")

    def tearDown(self):
        self.temp.cleanup()

    def run_python_node(self, *extra):
        arguments = [
            sys.executable, "-B", str(NODE_PY_PATH), "node",
            "spawn trace goal", "apex-spawn", "apex", "complete",
            "fact", "none", "next",
            "--topics", "topic", "--evidence", "notes.md:1-1", "--confidence", "high",
            "--consumes", "none", "--blocks", "none", "--version", "2",
            "--allow-untracked", "spawn trace fixture",
            *extra,
        ]
        return subprocess.run(
            arguments, check=False, capture_output=True, text=True,
            cwd=str(self.workspace), input="",
        )

    def run_powershell_node(self, *extra):
        arguments = [
            shutil.which("pwsh"), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(NODE_PS1_PATH),
            "spawn trace goal", "apex-spawn", "apex", "complete",
            "fact", "none", "next",
            "-Topics", "topic", "-Evidence", "notes.md:1-1", "-Confidence", "high",
            "-Consumes", "none", "-Blocks", "none", "-Version", "2",
            "-AllowUntracked", "spawn trace fixture",
            *extra,
        ]
        return subprocess.run(
            arguments, check=False, capture_output=True, text=True, cwd=str(self.workspace)
        )

    def assert_rejected(self, result):
        self.assertNotEqual(0, result.returncode, result.stdout)
        # pwsh on Linux colors and wraps error text at the console width.
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
        self.assertIn(" ".join(SPAWN_ERROR.split()), " ".join(output.split()))

    def test_python_rejects_a_spawn_trace_without_a_producing_agent(self):
        self.assert_rejected(self.run_python_node("--trace", SPAWN_TRACE))

    def test_python_rejects_a_codex_spawn_trace_without_a_producing_agent(self):
        trace = "Action: collaboration.spawn_agent; Observation: returned id; Result: recorded"
        self.assert_rejected(self.run_python_node("--trace", trace))

    def test_python_rejects_a_spawn_trace_missing_a_contract_segment(self):
        self.assert_rejected(
            self.run_python_node(
                "--trace", "Agent returned apex-one", "--producing-agent", "apex-one"
            )
        )

    def test_python_accepts_a_complete_spawn_trace(self):
        result = self.run_python_node("--trace", SPAWN_TRACE, "--producing-agent", "apex-one")
        self.assertEqual(0, result.returncode, result.stderr)

    def test_python_leaves_a_non_spawn_trace_alone(self):
        # Existing callers pass a tool Trace with no spawn-tool name and no
        # ProducingAgent. That must keep working.
        trace = "Action: search; Observation: notes match; Result: fact recorded"
        result = self.run_python_node("--trace", trace)
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not on PATH")
    def test_powershell_rejects_a_spawn_trace_without_a_producing_agent(self):
        self.assert_rejected(self.run_powershell_node("-Trace", SPAWN_TRACE))

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not on PATH")
    def test_powershell_rejects_a_spawn_trace_missing_a_contract_segment(self):
        self.assert_rejected(
            self.run_powershell_node(
                "-Trace", "Agent returned apex-one", "-ProducingAgent", "apex-one"
            )
        )

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not on PATH")
    def test_powershell_accepts_a_complete_spawn_trace(self):
        result = self.run_powershell_node("-Trace", SPAWN_TRACE, "-ProducingAgent", "apex-one")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not on PATH")
    def test_powershell_leaves_a_non_spawn_trace_alone(self):
        trace = "Action: search; Observation: notes match; Result: fact recorded"
        result = self.run_powershell_node("-Trace", trace)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class HostNeutralDispatchTests(unittest.TestCase):
    def test_the_brief_script_no_longer_implies_that_it_dispatches(self):
        header = "This brief is for hosts without a native subagent primitive."
        skip = "dispatch directly and skip this script."
        for path in (ROOT / "bin" / "subagent-brief.ps1", ROOT / "bin" / "mycelium.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn(header, text)
                self.assertIn(skip, text)

    def test_both_brief_implementations_print_the_same_first_line(self):
        with tempfile.TemporaryDirectory() as temp:
            python_brief = subprocess.run(
                [sys.executable, "-B", str(NODE_PY_PATH), "subagent-brief", "apex", "goal"],
                check=False, capture_output=True, text=True, cwd=temp,
            )
            self.assertEqual(0, python_brief.returncode, python_brief.stderr)
            expected = python_brief.stdout.splitlines()[0]
            self.assertIn("native subagent primitive", expected)

            pwsh = shutil.which("pwsh")
            if not pwsh:
                self.skipTest("pwsh is not on PATH")
            powershell_brief = subprocess.run(
                [
                    pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "bin" / "subagent-brief.ps1"), "apex", "goal",
                ],
                check=False, capture_output=True, text=True, cwd=temp,
            )
            self.assertEqual(0, powershell_brief.returncode, powershell_brief.stderr)
            self.assertEqual(expected, powershell_brief.stdout.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
