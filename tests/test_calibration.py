"""Confidence calibration ledger: how often a declared confidence was wrong.

The signal is the existing `reflection` link -- a reflection node's `consumes`
names the node that failed -- so nothing here depends on new schema.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium.py"
CALIBRATE_PATH = MODULE_PATH.with_name("mycelium-calibrate.py")
CALIBRATE_PS1_PATH = MODULE_PATH.with_name("mycelium-calibrate.ps1")
CALIBRATE_SH_PATH = MODULE_PATH.with_name("mycelium-calibrate.sh")
SPEC = importlib.util.spec_from_file_location("mycelium_cli_for_calibration", MODULE_PATH)
mycelium = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mycelium)


class CalibrationLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def run_calibrate(self, script=None):
        return subprocess.run(
            [sys.executable, str(script or CALIBRATE_PATH)],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
        )

    def ledger(self):
        result = self.run_calibrate()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(".mycelium/calibration.json", result.stdout.strip())
        return json.loads(Path(".mycelium/calibration.json").read_text(encoding="utf-8"))

    def subject(self, node_id, confidence, fact="fact"):
        mycelium.write_node(
            "goal", node_id, "apex", "complete", fact, "none", "next",
            topics="topic", evidence="source.md:1", confidence=confidence,
            consumes="none", blocks="none", source_refs="source.md:1",
        )

    def reflect(self, failed_node_id):
        mycelium.write_node(
            "goal", f"reflection-{failed_node_id}", "cap", "blocked",
            "it failed", "", "try again",
            topics="reflection,verification", evidence="it failed",
            confidence="medium", consumes=failed_node_id, blocks="it failed",
            reflection="try again",
        )

    def test_empty_workspace_reports_zeroed_buckets(self):
        ledger = self.ledger()
        self.assertEqual([], ledger["reflected_nodes"])
        self.assertEqual(
            {"high", "medium", "low", "unset"}, set(ledger["by_confidence"])
        )
        for stats in ledger["by_confidence"].values():
            self.assertEqual({"total": 0, "later_reflected": 0, "rate": 0.0}, stats)
        self.assertTrue(ledger["generated"].endswith("Z"))

    def test_reflection_marks_its_consumed_node_at_that_confidence(self):
        self.subject("high-wrong", "high")
        self.subject("high-right", "high")
        self.subject("low-right", "low")
        self.reflect("high-wrong")
        ledger = self.ledger()

        self.assertEqual(
            {"total": 2, "later_reflected": 1, "rate": 0.5},
            ledger["by_confidence"]["high"],
        )
        self.assertEqual(
            {"total": 1, "later_reflected": 0, "rate": 0.0},
            ledger["by_confidence"]["low"],
        )
        # The reflection node itself is a verdict, not a subject under test.
        self.assertEqual(
            {"total": 0, "later_reflected": 0, "rate": 0.0},
            ledger["by_confidence"]["medium"],
        )
        self.assertEqual(
            [
                {
                    "nodeId": "high-wrong",
                    "confidence": "high",
                    "role": "apex",
                    "reflections": ["reflection-high-wrong"],
                }
            ],
            ledger["reflected_nodes"],
        )

    def test_nodes_without_a_confidence_land_in_the_unset_bucket(self):
        mycelium.write_node("goal", "vague", "apex", "alive", "fact")
        ledger = self.ledger()
        self.assertEqual(1, ledger["by_confidence"]["unset"]["total"])
        self.assertEqual(0, ledger["by_confidence"]["high"]["total"])

    def test_reflection_with_empty_source_refs_is_still_counted(self):
        # `mycelium reflect` writes blocked reflections with no source_refs;
        # filtering on source-ref currency would silently drop every one.
        self.subject("stale-subject", "high")
        self.reflect("stale-subject")
        reflection_text = Path(".mycelium/nodes/reflection-stale-subject.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("\nsource_refs: \n", reflection_text)
        self.assertIn("\nstatus: blocked\n", reflection_text)
        ledger = self.ledger()
        self.assertEqual(1, ledger["by_confidence"]["high"]["later_reflected"])

    def test_one_reflection_can_name_several_consumed_nodes(self):
        self.subject("first", "medium")
        self.subject("second", "medium")
        mycelium.write_node(
            "goal", "reflection-pair", "cap", "blocked", "both failed", "", "retry",
            topics="reflection", evidence="both failed", confidence="medium",
            consumes="first,second", blocks="both failed", reflection="retry",
        )
        ledger = self.ledger()
        self.assertEqual(
            {"total": 2, "later_reflected": 2, "rate": 1.0},
            ledger["by_confidence"]["medium"],
        )
        self.assertEqual(
            ["first", "second"], [row["nodeId"] for row in ledger["reflected_nodes"]]
        )

    def test_reflect_subcommand_feeds_the_ledger_end_to_end(self):
        Path("source.md").write_text("fact\n", encoding="utf-8")
        self.subject("failing", "high")
        reflected = subprocess.run(
            [sys.executable, str(MODULE_PATH), "reflect", "goal", "failing", "it broke", "retry"],
            cwd=Path.cwd(), capture_output=True, text=True,
        )
        self.assertEqual(0, reflected.returncode, reflected.stderr)
        ledger = self.ledger()
        self.assertEqual(1.0, ledger["by_confidence"]["high"]["rate"])
        self.assertEqual(
            ["reflection-failing"], ledger["reflected_nodes"][0]["reflections"]
        )

    def test_unreadable_nodes_are_reported_and_skipped(self):
        self.subject("good", "high")
        Path(".mycelium/nodes/broken.md").write_text("no frontmatter\n", encoding="utf-8")
        result = self.run_calibrate()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("1 skipped", result.stderr)
        ledger = json.loads(Path(".mycelium/calibration.json").read_text(encoding="utf-8"))
        self.assertEqual(1, ledger["by_confidence"]["high"]["total"])

    def test_powershell_wrapper_produces_the_same_ledger(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not on PATH")
        self.subject("ps-subject", "high")
        self.reflect("ps-subject")
        expected = self.ledger()
        Path(".mycelium/calibration.json").unlink()
        result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(CALIBRATE_PS1_PATH)],
            cwd=Path.cwd(), capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(".mycelium/calibration.json", result.stdout.strip())
        actual = json.loads(Path(".mycelium/calibration.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["by_confidence"], actual["by_confidence"])
        self.assertEqual(expected["reflected_nodes"], actual["reflected_nodes"])

    def test_shell_wrapper_produces_the_same_ledger(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not on PATH")
        probe = subprocess.run(
            [bash, "-c", "python3 -c 'import sys'"], capture_output=True, text=True
        )
        if probe.returncode != 0:
            self.skipTest("python3 is not usable from bash on this host")
        self.subject("sh-subject", "high")
        self.reflect("sh-subject")
        expected = self.ledger()
        Path(".mycelium/calibration.json").unlink()
        result = subprocess.run(
            [bash, str(CALIBRATE_SH_PATH)],
            cwd=Path.cwd(), capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        actual = json.loads(Path(".mycelium/calibration.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["by_confidence"], actual["by_confidence"])


class CalibratedSearchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        Path("source.md").write_text("shared term high\nshared term low\n", encoding="utf-8")
        mycelium.write_node(
            "goal", "confident", "apex", "complete", "shared term high", "none", "next",
            topics="topic", evidence="source.md:1", confidence="high",
            consumes="none", blocks="none", source_refs="source.md:1",
        )
        mycelium.write_node(
            "goal", "modest", "apex", "complete", "shared term low", "none", "next",
            topics="topic", evidence="source.md:2", confidence="low",
            consumes="none", blocks="none", source_refs="source.md:2",
        )

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def search(self, *extra):
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "search", "shared", *extra],
            cwd=Path.cwd(), capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return [line.split("\t")[0] for line in result.stdout.splitlines() if line.strip()]

    def write_ledger(self, high_rate):
        Path(".mycelium").mkdir(exist_ok=True)
        Path(".mycelium/calibration.json").write_text(
            json.dumps(
                {
                    "generated": "2026-09-15T00:00:00.000000Z",
                    "by_confidence": {
                        "high": {"total": 4, "later_reflected": 4, "rate": high_rate},
                        "medium": {"total": 0, "later_reflected": 0, "rate": 0.0},
                        "low": {"total": 1, "later_reflected": 0, "rate": 0.0},
                        "unset": {"total": 0, "later_reflected": 0, "rate": 0.0},
                    },
                    "reflected_nodes": [],
                }
            ),
            encoding="utf-8",
        )

    def test_default_search_ignores_the_ledger(self):
        self.write_ledger(1.0)
        self.assertEqual(["confident", "modest"], self.search())

    def test_calibrated_search_demotes_an_overconfident_level(self):
        self.write_ledger(1.0)
        self.assertEqual(["modest", "confident"], self.search("--calibrated"))

    def test_calibrated_search_without_a_ledger_keeps_default_order(self):
        self.assertEqual(["confident", "modest"], self.search("--calibrated"))

    def test_malformed_ledger_does_not_break_calibrated_search(self):
        Path(".mycelium").mkdir(exist_ok=True)
        Path(".mycelium/calibration.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(["confident", "modest"], self.search("--calibrated"))

    def test_powershell_search_matches_calibrated_python_order(self):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not on PATH")
        self.write_ledger(1.0)
        expected = self.search("--calibrated")
        result = subprocess.run(
            [
                pwsh, "-NoProfile", "-File",
                str(MODULE_PATH.with_name("mycelium-search.ps1")),
                "shared", "", "-Calibrated",
            ],
            cwd=Path.cwd(), capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        actual = [
            line.split("\t")[0] for line in result.stdout.splitlines() if line.strip()
        ]
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
