import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { chooseGoalView, layoutProcessGraph } from "../web/mycelium-atlas/atlas-layout.js";

test("graph viewport remains in the flexible row when the warning is hidden", () => {
  const css = readFileSync(new URL("../web/mycelium-atlas/atlas.css", import.meta.url), "utf8");
  const viewportRule = css.match(/\.graph-viewport\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(viewportRule, /grid-row:\s*3\s*;/);
});

const vertices = [
  { id: "a", graphId: "node:a", role: "apex", sequence: 1 },
  { id: "b", graphId: "node:b", role: "apex", sequence: 2 },
  { id: "x", graphId: "node:x", role: "septum", sequence: 3 },
  { id: "y", graphId: "node:y", role: "septum", sequence: 4 },
  { id: "skip-hyphae", role: "hyphae", kind: "skip", sequence: 5 },
  { id: "stem", graphId: "node:stem", role: "stem", sequence: 6 },
  { id: "cap", graphId: "node:cap", role: "cap", sequence: 7 },
];

const processEdges = [
  { source: "b", target: "x" },
  { source: "a", target: "y" },
  { source: "x", target: "skip-hyphae" },
  { source: "y", target: "skip-hyphae" },
  { source: "skip-hyphae", target: "stem" },
  { source: "stem", target: "cap" },
];

function plainPositions(layout) {
  return Object.fromEntries([...layout.positions].sort(([left], [right]) => left.localeCompare(right)));
}

test("process layout uses explicit lineage to reduce crossings inside fixed role lanes", () => {
  const layout = layoutProcessGraph({ vertices, processEdges });

  assert.deepEqual(layout.roles, ["apex", "septum", "hyphae", "stem", "cap"]);
  assert.ok(layout.positions.get("a").y < layout.positions.get("y").y);
  assert.ok(layout.positions.get("y").x < layout.positions.get("x").x);
  assert.equal(layout.positions.get("skip-hyphae").kind, "skip");
});

test("evidence and lifecycle overlays cannot change process positions", () => {
  const expected = plainPositions(layoutProcessGraph({ vertices, processEdges }));
  const actual = plainPositions(layoutProcessGraph({
    vertices,
    processEdges,
    evidenceEdges: [{ source: "cap", target: "a" }],
    lifecycleEdges: [{ source: "x", target: "b" }],
  }));

  assert.deepEqual(actual, expected);
});

test("layout is deterministic when payload ordering changes", () => {
  const expected = plainPositions(layoutProcessGraph({ vertices, processEdges }));
  const actual = plainPositions(layoutProcessGraph({
    vertices: [...vertices].reverse(),
    processEdges: [...processEdges].reverse(),
  }));

  assert.deepEqual(actual, expected);
});

test("layout never infers an edge from role order", () => {
  const layout = layoutProcessGraph({
    vertices: [vertices[0], vertices[2], vertices[5]],
    processEdges: [],
  });

  assert.equal(layout.edges.length, 0);
  assert.deepEqual([...layout.positions.keys()].sort(), ["a", "stem", "x"]);
});

test("goal view selects a node bound to the authoritative run instead of a legacy node", () => {
  const view = chooseGoalView({
    goal: "goal",
    processPreferred: true,
    processRuns: [
      { runId: "run", goal: "goal", authoritative: true, current: true, updatedAt: "2026-08-27T01:00:00Z" },
    ],
    processVertices: [
      { vertexId: "run-node", runId: "run", graphId: "node:bound", kind: "node", sequence: 2 },
      { vertexId: "skip", runId: "run", graphId: "", kind: "skip", sequence: 1 },
    ],
    nodes: [
      { graphId: "node:legacy", goal: "goal" },
      { graphId: "node:bound", goal: "goal" },
    ],
  });

  assert.deepEqual(view, {
    process: true,
    evidenceFallback: false,
    runId: "run",
    graphId: "node:bound",
  });
});

test("goal view falls back to evidence when the selected goal has no authoritative run", () => {
  const view = chooseGoalView({
    goal: "legacy goal",
    processPreferred: true,
    processRuns: [
      { runId: "other", goal: "other goal", authoritative: true, current: true, updatedAt: "2026-08-27T01:00:00Z" },
    ],
    processVertices: [],
    nodes: [{ graphId: "node:legacy", goal: "legacy goal" }],
  });

  assert.deepEqual(view, {
    process: false,
    evidenceFallback: true,
    runId: "",
    graphId: "node:legacy",
  });
});

test("goal view rejects an authoritative run that is no longer current", () => {
  const view = chooseGoalView({
    goal: "goal",
    processPreferred: true,
    processRuns: [
      { runId: "stale", goal: "goal", authoritative: true, current: false, updatedAt: "2026-08-27T02:00:00Z" },
      { runId: "current", goal: "goal", authoritative: true, current: true, updatedAt: "2026-08-27T01:00:00Z" },
    ],
    processVertices: [
      { vertexId: "current-node", runId: "current", graphId: "node:current", kind: "node", sequence: 1 },
    ],
    nodes: [{ graphId: "node:current", goal: "goal" }],
  });

  assert.equal(view.runId, "current");
  assert.equal(view.graphId, "node:current");
});
