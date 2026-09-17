import assert from "node:assert/strict";
import test from "node:test";

import {
  chooseInitialGoal,
  emptyData,
  normalizePayload,
  selectAuthoritativeRun,
} from "../web/mycelium-atlas/atlas-model.js";

test("authoritative run selection requires an exact goal and strict current flags", () => {
  const runs = [
    { runId: "wrong-goal", goal: "other", authoritative: true, current: true, updatedAt: "2026-08-27T04:00:00Z" },
    { runId: "stale", goal: "goal", authoritative: true, current: false, updatedAt: "2026-08-27T03:00:00Z" },
    { runId: "coerced", goal: "goal", authoritative: 1, current: true, updatedAt: "2026-08-27T02:00:00Z" },
    { runId: "older", goal: "goal", authoritative: true, current: true, updatedAt: "2026-08-27T00:00:00Z" },
    { runId: "newer", goal: "goal", authoritative: true, current: true, updatedAt: "2026-08-27T01:00:00Z" },
  ];

  assert.equal(selectAuthoritativeRun(runs, "goal")?.runId, "newer");
  assert.equal(selectAuthoritativeRun(runs, "missing"), null);
});

test("payload normalization preserves the schema-v1 evidence fallback", () => {
  const payload = normalizePayload({
    repository: { name: "repository" },
    repository_id: "repo-id",
    nodes: [{
      nodeId: "node-a",
      graph_id: "graph:a",
      parent_goal_id: "goal",
      role: "septum",
      status: "complete",
      facts: ["- fact"],
      source_refs: ["source.md:1"],
      lifecycle: { current: true },
    }],
    edges: [{ type: "depends_on", from: "node-a", to: "graph:a" }],
  });

  assert.equal(payload.schemaVersion, 1);
  assert.equal(payload.hasAuthoritativeLineage, false);
  assert.equal(payload.repository, "repository");
  assert.equal(payload.repositoryId, "repo-id");
  assert.equal(payload.nodes[0].graphId, "graph:a");
  assert.equal(payload.nodes[0].facts[0].text, "fact");
  assert.equal(payload.edges[0].source, "graph:a");
  assert.equal(payload.processes[0].goal, "goal");
  assert.equal(chooseInitialGoal(payload), "goal");
});

test("payload lineage availability and initial goal use the canonical selector", () => {
  const payload = normalizePayload({
    schemaVersion: 2,
    processRuns: [
      { runId: "stale", goal: "stale goal", authoritative: true, current: false, updatedAt: "2026-08-27T03:00:00Z" },
      { runId: "current", goal: "current goal", authoritative: true, current: true, updatedAt: "2026-08-27T02:00:00Z" },
    ],
  });

  assert.equal(payload.hasAuthoritativeLineage, true);
  assert.equal(chooseInitialGoal(payload), "current goal");
});

test("empty data provides stable collection and lookup shapes", () => {
  const data = emptyData();

  assert.equal(data.schemaVersion, 1);
  assert.deepEqual(data.nodes, []);
  assert.ok(data.nodeByGraphId instanceof Map);
  assert.ok(data.graphIdsByNodeId instanceof Map);
});
