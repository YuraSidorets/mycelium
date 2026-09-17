import contextlib
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "mycelium_graph.py"
SPEC = importlib.util.spec_from_file_location("mycelium_graph", MODULE_PATH)
graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graph)


def all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from all_strings(key)
            yield from all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from all_strings(item)


def artifact_snapshot(root):
    result = {}
    if not root.exists():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = (path.read_bytes(), path.stat().st_mtime_ns)
    return result


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class MyceliumGraphTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def directory_link(self, target, link):
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            os.symlink(target, link, target_is_directory=True)

    def write_source(self, name, *lines):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def write_node(self, node_id, fact, source_name, **options):
        source_path = self.root / source_name
        source_lines = source_path.read_text(encoding="utf-8").splitlines()
        line = source_lines.index(fact) + 1
        with working_directory(self.root):
            graph.CORE.write_node(
                options.pop("goal", "atlas goal"),
                node_id,
                options.pop("role", "hyphae"),
                options.pop("status", "complete"),
                fact,
                options.pop("questions", "none"),
                options.pop("next_step", "dispatch cap"),
                topics=options.pop("topics", "atlas"),
                evidence=options.pop("evidence", f"{source_name}:{line}"),
                confidence=options.pop("confidence", "high"),
                consumes=options.pop("consumes", "none"),
                blocks=options.pop("blocks", "none"),
                source_refs=options.pop("source_refs", f"{source_name}:{line}"),
                **options,
            )

    def node(self, payload, raw_id):
        return next(node for node in payload["nodes"] if node["rawId"] == raw_id)

    def committed_binding(self, node_id, process_inputs=()):
        node = self.node(graph.inspect_graph(self.root), node_id)
        flow = node["matchingFlow"]
        node_path = self.root / node["artifactPath"]
        flow_path = self.root / flow["artifactPath"]
        flow_line = flow_path.read_bytes().splitlines()[flow["line"] - 1]
        raw_timestamp = json.loads(flow_line)["timestamp"]
        if node["updatedRaw"] != raw_timestamp:
            node_text = node_path.read_text(encoding="utf-8")
            node_path.write_text(
                node_text.replace(f"Updated: {node['updatedRaw']}", f"Updated: {raw_timestamp}", 1),
                encoding="utf-8",
            )
            node = self.node(graph.inspect_graph(self.root), node_id)
        commit = {
            "node": {
                "path": node["artifactPath"],
                "sha256": hashlib.sha256(node_path.read_bytes()).hexdigest(),
            },
            "flow": {
                "path": flow["artifactPath"],
                "line": flow["line"],
                "sha256": hashlib.sha256(flow_line).hexdigest(),
            },
            "timestamp": raw_timestamp,
        }
        commit["digest"] = hashlib.sha256(
            json.dumps(commit, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "node_id": node_id,
            "role": node["role"],
            "process_inputs": list(process_inputs),
            "status": "committed",
            "reservation_token": f"token-{node_id}",
            "reserved_at": "2026-08-20T12:00:00.0000000Z",
            "finalized_at": "2026-08-20T12:00:00.0000000Z",
            "aborted_at": None,
            "abort_reason": None,
            "commit": commit,
        }

    def write_run(self, run_id, *, state="sealed", nodes=None, topology=None, goal="atlas goal"):
        run_dir = self.root / ".mycelium" / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        timestamp = "2026-08-20T12:00:00.0000000Z"
        topology = topology or [
            {"role": "apex", "state": "required"},
            {"role": "septum", "state": "required"},
            {"role": "hyphae", "state": "required"},
            {"role": "stem", "state": "required"},
            {"role": "cap", "state": "required"},
        ]
        nodes = nodes or {}
        for sequence, binding in enumerate(nodes.values(), 1):
            binding.setdefault("sequence", sequence)
        manifest = {
            "schema": 1,
            "run_id": run_id,
            "goal": goal,
            "state": state,
            "created_at": timestamp,
            "updated_at": timestamp,
            "sealed_at": timestamp if state == "sealed" else None,
            "topology": topology,
            "roles": [
                {
                    "role": item["role"],
                    "state": "skipped" if item["state"] == "skipped" else "performed" if state == "sealed" else "pending",
                }
                for item in topology
            ],
            "nodes": nodes,
            "diagnostics": [],
        }
        (run_dir / f"{run_id}.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    def write_canonical_run(self, run_id):
        roles = ("apex", "septum", "hyphae", "stem", "cap")
        source_name = f"{run_id}.md"
        self.write_source(source_name, *(f"{role} fact" for role in roles))
        for role in roles:
            self.write_node(role, f"{role} fact", source_name, role=role, version="2")
        bindings = {}
        previous = None
        for role in roles:
            bindings[role] = self.committed_binding(role, [previous] if previous else [])
            previous = role
        return self.write_run(run_id, nodes=bindings)

    def test_inspect_graph_is_deterministic_relative_and_detailed(self):
        self.write_source("evidence/source.md", "supported fact")
        self.write_node(
            "node-one",
            "supported fact",
            "evidence/source.md",
            consumes="raw upstream notes",
            blocks="none",
            trace="Action: inspect; Observation: found; Result: recorded",
            parent_goal_id="parent goal",
            producing_agent="agent-one",
            version="2",
        )
        ledger_path = self.root / ".mycelium" / "flows" / "stem-ledger-ignored.json"
        ledger_path.write_text(
            json.dumps(
                {
                    "goal": "ledger-only goal",
                    "selected_nodes": ["node-one"],
                    "ignored_nodes": [],
                    "conflicts": [],
                    "cap_dispatch": "ignored",
                    "created": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

        first = graph.inspect_graph(self.root)
        second = graph.inspect_graph(self.root)
        first_without_time = {key: value for key, value in first.items() if key != "generatedAt"}
        second_without_time = {key: value for key, value in second.items() if key != "generatedAt"}
        self.assertEqual(first_without_time, second_without_time)
        self.assertEqual(2, first["schemaVersion"])
        self.assertEqual(self.root.name, first["repository"])
        self.assertRegex(first["repositoryId"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(first["repositoryId"], second["repositoryId"])
        for value in all_strings(first):
            self.assertNotIn(str(self.root), value)
            self.assertNotIn(self.root.as_posix(), value)

        other_root = self.root / "other-repository"
        other_root.mkdir()
        other = graph.inspect_graph(other_root)
        self.assertNotEqual(first["repositoryId"], other["repositoryId"])
        for value in all_strings(other):
            self.assertNotIn(str(other_root), value)
            self.assertNotIn(other_root.as_posix(), value)

        node = self.node(first, "node-one")
        self.assertEqual(".mycelium/nodes/node-one.md", node["artifactPath"])
        self.assertEqual("supported fact", node["facts"][0]["text"])
        self.assertIsInstance(node["facts"][0]["line"], int)
        self.assertEqual("agent-one", node["producingAgent"])
        self.assertTrue(node["sourceRefs"][0]["valid"])
        self.assertTrue(node["lifecycle"]["flowCommitted"])
        self.assertTrue(node["lifecycle"]["eligibleCurrent"])
        self.assertTrue(node["lifecycle"]["sourceCurrent"])
        self.assertTrue(node["lifecycle"]["capEligible"])
        self.assertEqual("cap-eligible", node["classification"])
        self.assertEqual(1, len(node["flowHistory"]))
        self.assertTrue(node["flowHistory"][0]["matchesProjection"])
        self.assertEqual(1, first["summary"]["processes"])
        self.assertNotIn("stemRuns", first["summary"])
        self.assertNotIn("ledger-only goal", {process["goal"] for process in first["processes"]})

    def test_sealed_current_manifest_adds_explicit_process_lineage_without_changing_evidence_edges(self):
        self.write_source("lineage.md", "apex fact", "stem fact", "cap fact", "legacy fact")
        self.write_node("apex", "apex fact", "lineage.md", role="apex", version="2")
        self.write_node("stem", "stem fact", "lineage.md", role="stem", depends_on="cap", version="2")
        self.write_node("cap", "cap fact", "lineage.md", role="cap", depends_on="stem", version="2")
        self.write_node("legacy", "legacy fact", "lineage.md", role="septum")
        evidence_edges = graph.inspect_graph(self.root)["edges"]
        skip_evidence = {
            "input_packets": 1,
            "all_directly_sourced": True,
            "conflicts": False,
            "duplicates": False,
            "stale": False,
            "uncertainty": False,
            "risk": "low",
        }
        topology = [
            {"role": "apex", "state": "required"},
            {"role": "septum", "state": "skipped", "skip": {"code": "single-clean-stream", "evidence": skip_evidence}},
            {"role": "hyphae", "state": "skipped", "skip": {"code": "already-compact", "evidence": skip_evidence}},
            {"role": "stem", "state": "required"},
            {"role": "cap", "state": "required"},
        ]
        self.write_run(
            "run-one",
            topology=topology,
            nodes={
                "apex": self.committed_binding("apex"),
                "stem": self.committed_binding("stem", ["apex"]),
                "cap": self.committed_binding("cap", ["stem"]),
            },
        )

        payload = graph.inspect_graph(self.root)

        self.assertEqual(evidence_edges, payload["edges"])
        self.assertEqual(2, payload["schemaVersion"])
        run = next(item for item in payload["processRuns"] if item["runId"] == "run-one")
        self.assertTrue(run["authoritative"])
        self.assertTrue(run["current"])
        self.assertEqual(["septum", "hyphae"], [item["role"] for item in run["skippedRoles"]])
        run_vertices = [item for item in payload["processVertices"] if item["runId"] == "run-one"]
        self.assertEqual(3, sum(item["kind"] == "node" for item in run_vertices))
        self.assertEqual(2, sum(item["kind"] == "skip" for item in run_vertices))
        self.assertIn("legacy", {item["nodeId"] for item in payload["processVertices"] if item["kind"] == "untracked"})
        vertex_by_kind_role = {(item["kind"], item["role"]): item["vertexId"] for item in run_vertices}
        apex_vertex = next(item["vertexId"] for item in run_vertices if item["nodeId"] == "apex")
        stem_vertex = next(item["vertexId"] for item in run_vertices if item["nodeId"] == "stem")
        cap_vertex = next(item["vertexId"] for item in run_vertices if item["nodeId"] == "cap")
        self.assertEqual(
            {
                (apex_vertex, vertex_by_kind_role[("skip", "septum")]),
                (vertex_by_kind_role[("skip", "septum")], vertex_by_kind_role[("skip", "hyphae")]),
                (vertex_by_kind_role[("skip", "hyphae")], stem_vertex),
                (stem_vertex, cap_vertex),
            },
            {(edge["source"], edge["target"]) for edge in payload["processEdges"]},
        )
        self.assertFalse(any(edge["source"] == apex_vertex and edge["target"] == stem_vertex for edge in payload["processEdges"]))
        self.assertTrue(all(edge["authoritative"] for edge in payload["processEdges"]))

    def test_canonical_lineage_validation_controls_atlas_authority(self):
        manifest = self.write_canonical_run("canonical-run")
        canonical_before = graph.LINEAGE.validate_manifest(
            manifest,
            "canonical-run",
            root=self.root,
        )
        self.assertTrue(canonical_before["valid"], canonical_before["diagnostics"])

        mutations = {
            "missing audit timestamp": (
                lambda value: value["nodes"]["apex"].__setitem__("reserved_at", None),
                "timestamp",
            ),
            "unknown binding field": (
                lambda value: value["nodes"]["apex"].__setitem__("unexpected", True),
                "node-schema",
            ),
            "non-string node path": (
                lambda value: value["nodes"]["apex"]["commit"]["node"].__setitem__("path", None),
                "commit-schema",
            ),
            "non-integer flow line": (
                lambda value: value["nodes"]["apex"]["commit"]["flow"].__setitem__("line", "1"),
                "commit-schema",
            ),
        }
        run_path = self.root / ".mycelium" / "runs" / "canonical-run.json"
        for label, (mutate, expected_code) in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(manifest))
                mutate(changed)
                run_path.write_text(
                    json.dumps(changed, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                canonical = graph.LINEAGE.validate_manifest(
                    changed,
                    "canonical-run",
                    root=self.root,
                )
                self.assertFalse(canonical["valid"])

                payload = graph.inspect_graph(self.root)
                run = next(item for item in payload["processRuns"] if item["runId"] == "canonical-run")
                self.assertFalse(run["authoritative"])
                self.assertFalse(run["current"])
                self.assertIn(
                    expected_code,
                    {item["code"] for item in payload["topologyDiagnostics"]},
                )

    def test_authoritative_binding_selects_exact_artifact_when_raw_node_id_is_duplicated(self):
        manifest = self.write_canonical_run("duplicate-id-run")
        bound_path = manifest["nodes"]["apex"]["commit"]["node"]["path"]
        duplicate_path = self.root / ".mycelium" / "nodes" / "apex-duplicate.md"
        shutil.copyfile(self.root / bound_path, duplicate_path)

        canonical = graph.LINEAGE.validate_manifest(
            manifest,
            "duplicate-id-run",
            root=self.root,
        )
        self.assertTrue(canonical["valid"], canonical["diagnostics"])

        payload = graph.inspect_graph(self.root)

        run = next(item for item in payload["processRuns"] if item["runId"] == "duplicate-id-run")
        self.assertTrue(run["authoritative"])
        self.assertTrue(run["current"])
        bound_node = next(item for item in payload["nodes"] if item["artifactPath"] == bound_path)
        bound_vertex = next(
            item
            for item in payload["processVertices"]
            if item["runId"] == "duplicate-id-run" and item["nodeId"] == "apex"
        )
        self.assertEqual(bound_node["graphId"], bound_vertex["graphId"])
        self.assertNotIn(
            "stale-process-commit",
            {item["code"] for item in payload["topologyDiagnostics"]},
        )

    def test_run_directory_reparse_point_is_never_authoritative(self):
        self.write_canonical_run("linked-run")
        runs = self.root / ".mycelium" / "runs"
        target = self.root / ".mycelium" / "real-runs"
        runs.rename(target)
        try:
            self.directory_link(target, runs)
        except (OSError, subprocess.CalledProcessError) as error:
            self.skipTest(f"cannot create directory link: {error}")

        payload = graph.inspect_graph(self.root)

        run = next(item for item in payload["processRuns"] if item["runId"] == "linked-run")
        self.assertFalse(run["authoritative"])
        self.assertFalse(run["current"])
        self.assertIn(
            "malformed-process-manifest",
            {item["code"] for item in payload["topologyDiagnostics"]},
        )

    def test_rehashed_flow_with_changed_projection_is_not_authoritative(self):
        self.write_source("projection.md", "apex fact", "stem fact", "cap fact")
        self.write_node("apex", "apex fact", "projection.md", role="apex", version="2")
        self.write_node("stem", "stem fact", "projection.md", role="stem", version="2")
        self.write_node("cap", "cap fact", "projection.md", role="cap", version="2")
        skip_evidence = {
            "input_packets": 1,
            "all_directly_sourced": True,
            "conflicts": False,
            "duplicates": False,
            "stale": False,
            "uncertainty": False,
            "risk": "low",
        }
        topology = [
            {"role": "apex", "state": "required"},
            {"role": "septum", "state": "skipped", "skip": {"code": "single-clean-stream", "evidence": skip_evidence}},
            {"role": "hyphae", "state": "skipped", "skip": {"code": "already-compact", "evidence": skip_evidence}},
            {"role": "stem", "state": "required"},
            {"role": "cap", "state": "required"},
        ]
        manifest = self.write_run(
            "projection-run",
            topology=topology,
            nodes={
                "apex": self.committed_binding("apex"),
                "stem": self.committed_binding("stem", ["apex"]),
                "cap": self.committed_binding("cap", ["stem"]),
            },
        )
        commit = manifest["nodes"]["stem"]["commit"]
        flow_path = self.root / commit["flow"]["path"]
        lines = flow_path.read_bytes().splitlines()
        record = json.loads(lines[commit["flow"]["line"] - 1])
        record["facts"] = ["changed fact with valid identity fields"]
        changed_line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        lines[commit["flow"]["line"] - 1] = changed_line
        flow_path.write_bytes(b"\n".join(lines) + b"\n")
        commit["flow"]["sha256"] = hashlib.sha256(changed_line).hexdigest()
        commit["digest"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in commit.items() if key != "digest"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        (self.root / ".mycelium" / "runs" / "projection-run.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        payload = graph.inspect_graph(self.root)

        run = next(item for item in payload["processRuns"] if item["runId"] == "projection-run")
        self.assertFalse(run["authoritative"])
        self.assertIn("stale-process-commit", {item["code"] for item in payload["topologyDiagnostics"]})

    def test_canonical_cycle_check_handles_more_than_python_recursion_depth(self):
        count = 1_401
        nodes = {}
        for index in range(count):
            node_id = f"node-{index}"
            parent = f"node-{count - 1}" if index == 0 else f"node-{index - 1}"
            nodes[node_id] = {
                "node_id": node_id,
                "role": "apex",
                "process_inputs": [parent],
                "sequence": index + 1,
                "status": "committed",
            }
        manifest = {
            "schema": 1,
            "run_id": "deep-cycle",
            "goal": "deep cycle",
            "state": "open",
            "topology": [{"role": role, "state": "required"} for role in graph.CORE.ROLES],
            "roles": [{"role": role, "state": "performed"} for role in graph.CORE.ROLES],
            "nodes": nodes,
        }

        report = graph.LINEAGE.validate_manifest(
            manifest,
            "deep-cycle",
            root=self.root,
            require_sealed=False,
        )

        self.assertIn("cycle", {item["code"] for item in report["diagnostics"]})

    def test_process_lineage_never_infers_edges_and_reports_non_authoritative_manifests(self):
        self.write_source("runs.md", "apex fact", "stem fact", "cap fact")
        self.write_node("apex", "apex fact", "runs.md", role="apex")
        self.write_node("stem", "stem fact", "runs.md", role="stem")
        self.write_node("cap", "cap fact", "runs.md", role="cap")
        without_manifest = graph.inspect_graph(self.root)
        self.assertEqual([], without_manifest["processEdges"])
        self.assertEqual({"apex", "stem", "cap"}, {
            item["nodeId"] for item in without_manifest["processVertices"] if item["kind"] == "untracked"
        })

        stale = self.committed_binding("cap", ["stem"])
        stale["commit"]["node"]["sha256"] = "0" * 64
        self.write_run("open-run", state="open", nodes={"apex": self.committed_binding("apex")})
        self.write_run("invalid-run", state="invalid", nodes={"stem": self.committed_binding("stem", ["apex"])})
        self.write_run("stale-run", nodes={"cap": stale})
        skip_evidence = {
            "input_packets": 1,
            "all_directly_sourced": True,
            "conflicts": False,
            "duplicates": False,
            "stale": False,
            "uncertainty": False,
            "risk": "low",
        }
        shortcut = [
            {"role": "apex", "state": "required"},
            {"role": "septum", "state": "skipped", "skip": {"code": "single-clean-stream", "evidence": skip_evidence}},
            {"role": "hyphae", "state": "skipped", "skip": {"code": "already-compact", "evidence": skip_evidence}},
            {"role": "stem", "state": "required"},
            {"role": "cap", "state": "required"},
        ]
        self.write_run(
            "cycle-run",
            topology=shortcut,
            nodes={
                "apex": self.committed_binding("apex"),
                "stem": self.committed_binding("stem", ["cap"]),
                "cap": self.committed_binding("cap", ["stem"]),
            },
        )
        run_dir = self.root / ".mycelium" / "runs"
        (run_dir / "broken.json").write_text('{"schema":', encoding="utf-8")
        (run_dir / "open-run.lock").write_text("ignored", encoding="utf-8")

        payload = graph.inspect_graph(self.root)

        by_id = {item["runId"]: item for item in payload["processRuns"]}
        self.assertFalse(by_id["open-run"]["authoritative"])
        self.assertFalse(by_id["invalid-run"]["authoritative"])
        self.assertFalse(by_id["stale-run"]["authoritative"])
        self.assertFalse(by_id["stale-run"]["current"])
        self.assertFalse(by_id["cycle-run"]["authoritative"])
        codes = {item["code"] for item in payload["topologyDiagnostics"]}
        self.assertIn("open-process-run", codes)
        self.assertIn("invalid-process-run", codes)
        self.assertIn("stale-process-commit", codes)
        self.assertIn("malformed-process-manifest", codes)
        self.assertIn("cycle", codes)
        self.assertNotIn("open-run.lock", {item.get("artifactPath") for item in payload["topologyDiagnostics"]})

    def test_edges_use_canvas_direction_and_ignore_free_form_fields(self):
        self.write_source(
            "graph.md",
            "dependency fact",
            "dependent fact",
            "old fact",
            "new fact",
        )
        self.write_node("dependency", "dependency fact", "graph.md")
        self.write_node("dependent", "dependent fact", "graph.md", depends_on="dependency,Missing", consumes="dependency; manual approval", blocks="waiting on owner")
        self.write_node("old", "old fact", "graph.md")
        self.write_node("new", "new fact", "graph.md", supersedes="old", consumes="old")

        payload = graph.inspect_graph(self.root)
        dependency = self.node(payload, "dependency")
        dependent = self.node(payload, "dependent")
        old = self.node(payload, "old")
        new = self.node(payload, "new")
        dependency_edge = next(edge for edge in payload["edges"] if edge["type"] == "depends_on")
        supersedes_edge = next(edge for edge in payload["edges"] if edge["type"] == "supersedes")

        self.assertEqual(dependency["graphId"], dependency_edge["source"])
        self.assertEqual(dependent["graphId"], dependency_edge["target"])
        self.assertEqual(dependent["graphId"], dependency_edge["declaringNode"])
        self.assertEqual({"fromNodeId": "dependent", "toNodeId": "dependency"}, dependency_edge["rawDirection"])
        self.assertEqual(old["graphId"], supersedes_edge["source"])
        self.assertEqual(new["graphId"], supersedes_edge["target"])
        self.assertEqual("dependency; manual approval", dependent["consumes"])
        self.assertEqual("waiting on owner", dependent["blocks"])
        self.assertEqual(2, len(payload["edges"]))
        self.assertIn("dangling-edge-endpoint", {item["code"] for item in payload["diagnostics"]})

    def test_lifecycle_flags_are_booleans_for_legacy_string_metadata(self):
        self.write_source("legacy.md", "legacy fact")
        self.write_node(
            "legacy",
            "legacy fact",
            "legacy.md",
            role="HYPHAE",
            confidence="HIGH",
            source_refs="",
        )

        node = self.node(graph.inspect_graph(self.root), "legacy")

        self.assertEqual("hyphae", node["role"])
        self.assertEqual("HYPHAE", node["rawRole"])
        self.assertEqual("high", node["confidence"])
        self.assertEqual("HIGH", node["rawConfidence"])
        self.assertTrue(all(type(value) is bool for value in node["lifecycle"].values()))
        self.assertTrue(node["lifecycle"]["flowCommitted"])
        self.assertFalse(node["lifecycle"]["eligibleCurrent"])
        self.assertFalse(node["lifecycle"]["sourceCurrent"])
        self.assertFalse(node["lifecycle"]["searchVisible"])
        self.assertFalse(node["lifecycle"]["capEligible"])
        self.assertEqual("source-less", node["classification"])

    def test_source_current_requires_every_reference_valid_and_every_fact_supported(self):
        self.write_source("mixed.md", "supported fact")
        self.write_node(
            "mixed",
            "supported fact",
            "mixed.md",
            source_refs="mixed.md:1,missing.md:1",
        )

        node = self.node(graph.inspect_graph(self.root), "mixed")

        self.assertEqual([True, False], [reference["valid"] for reference in node["sourceRefs"]])
        self.assertTrue(node["lifecycle"]["flowCommitted"])
        self.assertTrue(node["lifecycle"]["eligibleCurrent"])
        self.assertFalse(node["lifecycle"]["sourceCurrent"])
        self.assertFalse(node["lifecycle"]["searchVisible"])
        self.assertFalse(node["lifecycle"]["capEligible"])
        self.assertEqual("source-drifted", node["classification"])

        with working_directory(self.root):
            graph.CORE.write_node(
                "atlas goal",
                "unsupported-vector",
                "hyphae",
                "complete",
                "supported fact\nunsupported fact",
                "none",
                "dispatch cap",
                topics="atlas",
                evidence="mixed.md:1",
                confidence="high",
                consumes="none",
                blocks="none",
                source_refs="mixed.md:1",
            )
        unsupported = self.node(graph.inspect_graph(self.root), "unsupported-vector")
        self.assertFalse(unsupported["lifecycle"]["sourceCurrent"])
        self.assertEqual("source-drifted", unsupported["classification"])

    def test_tolerates_malformed_duplicate_and_dangling_artifacts_without_reading_index(self):
        self.write_source("source.md", "valid fact", "child fact")
        self.write_node("valid", "valid fact", "source.md")
        self.write_node("child", "child fact", "source.md", depends_on="valid,missing")
        node_dir = self.root / ".mycelium" / "nodes"
        (node_dir / "legacy-spore.md").write_text("Goal: old CAP output\n", encoding="utf-8")
        shutil.copyfile(node_dir / "valid.md", node_dir / "valid-copy.md")
        flow_dir = self.root / ".mycelium" / "flows"
        (flow_dir / "broken.jsonl").write_text('{"nodeId":\n[]\n', encoding="utf-8")
        run_dir = self.root / ".mycelium" / "runs"
        run_dir.mkdir()
        (run_dir / "broken.json").write_text('{"schema":\n', encoding="utf-8")

        index_path = self.root / ".mycelium" / "index.json"
        index_path.write_bytes(b'{"stale":true}\n')
        fixed = 1_700_000_000_000_000_000
        os.utime(index_path, ns=(fixed, fixed))
        before_tree = artifact_snapshot(self.root / ".mycelium")

        payload = graph.inspect_graph(self.root)

        server = graph.create_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request("GET", "/api/v1/graph")
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(before_tree, artifact_snapshot(self.root / ".mycelium"))
        self.assertEqual(3, len(payload["nodes"]))
        codes = {item["code"] for item in payload["diagnostics"]}
        self.assertIn("malformed-node", codes)
        self.assertIn("malformed-flow-record", codes)
        self.assertIn("malformed-process-manifest", codes)
        self.assertIn("duplicate-node-id", codes)
        self.assertIn("ambiguous-edge-endpoint", codes)
        self.assertIn("dangling-edge-endpoint", codes)
        self.assertTrue(any(node["lifecycle"]["flowCommitted"] for node in payload["nodes"]))

    def test_escaping_node_symlink_is_diagnosed_without_hiding_valid_nodes(self):
        self.write_source("source.md", "valid fact")
        self.write_node("valid", "valid fact", "source.md")
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "escape.md"
            target.write_text("Goal: outside\n", encoding="utf-8")
            link = self.root / ".mycelium" / "nodes" / "escape.md"
            try:
                link.symlink_to(target)
                payload = graph.inspect_graph(self.root)
            except (NotImplementedError, OSError):
                link.write_text("Goal: simulated escaping artifact\n", encoding="utf-8")
                original = graph._artifact_is_contained
                with mock.patch.object(
                    graph,
                    "_artifact_is_contained",
                    side_effect=lambda root, path: False if path == link else original(root, path),
                ):
                    payload = graph.inspect_graph(self.root)

        self.assertEqual(["valid"], [node["rawId"] for node in payload["nodes"]])
        self.assertIn(
            "artifact-symlink-outside-repository",
            {item["code"] for item in payload["diagnostics"]},
        )

    def test_read_only_http_get_head_methods_traversal_refresh_and_shutdown(self):
        static = self.root / "web" / "mycelium-atlas"
        static.mkdir(parents=True)
        (static / "index.html").write_text("<h1>Atlas</h1>", encoding="utf-8")
        (static / "atlas.css").write_text("body{}", encoding="utf-8")
        (static / "atlas.js").write_text("void 0;", encoding="utf-8")
        (static / "atlas-layout.js").write_text("export const layout = {};", encoding="utf-8")
        (static / "atlas-model.js").write_text("export const model = {};", encoding="utf-8")
        (static / "atlas-draft-store.js").write_text("export const drafts = {};", encoding="utf-8")
        self.write_source("http.md", "first fact", "second fact")
        self.write_node("first", "first fact", "http.md")

        server = graph.create_server(self.root, port=0, static_root=static)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        def request(method, path, host=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Host": host} if host is not None else {}
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            headers = dict(response.getheaders())
            connection.close()
            return response.status, headers, body

        try:
            status, headers, body = request("GET", "/")
            self.assertEqual(200, status)
            self.assertIn(b"Atlas", body)
            self.assertEqual("nosniff", headers["X-Content-Type-Options"])

            status, headers, body = request("HEAD", "/atlas.js")
            self.assertEqual(200, status)
            self.assertEqual(b"", body)
            self.assertEqual(str(len("void 0;")), headers["Content-Length"])

            status, headers, body = request("GET", "/atlas-layout.js")
            self.assertEqual(200, status)
            self.assertEqual("text/javascript; charset=utf-8", headers["Content-Type"])
            self.assertEqual(b"export const layout = {};", body)

            for route, expected in (
                ("/atlas-model.js", b"export const model = {};"),
                ("/atlas-draft-store.js", b"export const drafts = {};"),
            ):
                status, headers, body = request("GET", route)
                self.assertEqual(200, status)
                self.assertEqual("text/javascript; charset=utf-8", headers["Content-Type"])
                self.assertEqual(expected, body)

            status, headers, body = request("GET", "/api/v1/graph")
            self.assertEqual(200, status)
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertEqual(1, json.loads(body)["summary"]["nodes"])

            self.write_node("second", "second fact", "http.md")
            status, _, body = request("GET", "/api/v1/graph")
            self.assertEqual(200, status)
            self.assertEqual(2, json.loads(body)["summary"]["nodes"])

            status, _, _ = request("GET", "/api/v1/graph", f"localhost:{port}")
            self.assertEqual(200, status)
            status, headers, _ = request("GET", "/api/v1/graph", "atlas.attacker.invalid")
            self.assertEqual(403, status)
            self.assertEqual("no-store", headers["Cache-Control"])
            wrong_port = port - 1 if port == 65535 else port + 1
            status, _, _ = request("GET", "/api/v1/graph", f"127.0.0.1:{wrong_port}")
            self.assertEqual(403, status)

            status, headers, _ = request("POST", "/api/v1/graph")
            self.assertEqual(405, status)
            self.assertEqual("GET, HEAD", headers["Allow"])
            status, _, _ = request("GET", "/%2e%2e/secret")
            self.assertEqual(404, status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_wrappers_invoke_the_shared_backend_and_forward_arguments(self):
        repository = MODULE_PATH.parents[1]
        self.write_source("wrapper.md", "wrapper fact")
        self.write_node("wrapper", "wrapper fact", "wrapper.md")
        with tempfile.TemporaryDirectory(prefix="mycelium atlas wrappers ") as runtime_dir:
            runtime = Path(runtime_dir)
            runtime_bin = runtime / "bin"
            runtime_bin.mkdir()
            for name in (
                "mycelium_graph.py",
                "mycelium_lineage.py",
                "mycelium.py",
                "mycelium-graph.ps1",
                "mycelium-graph.sh",
            ):
                shutil.copy2(repository / "bin" / name, runtime_bin / name)
            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment.pop("PYTHONPYCACHEPREFIX", None)
            commands = []
            if shutil.which("pwsh"):
                commands.append(
                    [
                        "pwsh",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(runtime_bin / "mycelium-graph.ps1"),
                        "--json",
                        "--root",
                        str(self.root),
                    ]
                )
            shell = shutil.which("sh")
            if shell is None and os.name == "nt":
                git_shell = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "sh.exe"
                shell = str(git_shell) if git_shell.is_file() else None
            if shell:
                commands.append(
                    [
                        shell,
                        (runtime_bin / "mycelium-graph.sh").as_posix(),
                        "--json",
                        "--root",
                        self.root.as_posix(),
                    ]
                )
            self.assertTrue(commands, "no launcher runtime is available")
            runtime_before = artifact_snapshot(runtime)
            for command in commands:
                with self.subTest(launcher=command[0]):
                    completed = subprocess.run(
                        command,
                        cwd=runtime,
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    payload = json.loads(completed.stdout)
                    self.assertEqual(self.root.name, payload["repository"])
                    self.assertEqual(1, payload["summary"]["nodes"])
                    self.assertEqual(runtime_before, artifact_snapshot(runtime))

    def test_atlas_launchers_require_python_3_9_and_disable_bytecode(self):
        repository = MODULE_PATH.parents[1]
        minimum_probe = "sys.version_info >= (3, 9)"
        for relative in (
            "bin/mycelium-atlas.mjs",
            "bin/mycelium-graph.ps1",
            "bin/mycelium-graph.sh",
        ):
            with self.subTest(launcher=relative):
                text = (repository / relative).read_text(encoding="utf-8")
                self.assertIn(minimum_probe, text)
                self.assertIn("-B", text)


if __name__ == "__main__":
    unittest.main()
