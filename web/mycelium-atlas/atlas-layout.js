import { selectAuthoritativeRun } from "./atlas-model.js";

const ROLE_ORDER = ["apex", "septum", "hyphae", "stem", "cap"];

export function chooseGoalView({
  goal = "",
  processPreferred = false,
  processRuns = [],
  processVertices = [],
  nodes = [],
} = {}) {
  const run = selectAuthoritativeRun(processRuns, goal);
  const process = Boolean(processPreferred && run);
  const nodeIds = new Set(nodes.filter((node) => node.goal === goal).map((node) => node.graphId));
  let graphId = "";
  if (process) {
    graphId = processVertices
      .filter((vertex) => vertex.runId === run.runId && vertex.kind === "node" && nodeIds.has(vertex.graphId))
      .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0) || String(left.vertexId).localeCompare(String(right.vertexId)))[0]?.graphId || "";
  } else {
    graphId = nodes.find((node) => node.goal === goal)?.graphId || "";
  }
  return {
    process,
    evidenceFallback: Boolean(processPreferred && !run),
    runId: run?.runId || "",
    graphId,
  };
}

function vertexId(vertex) {
  return String(vertex?.vertexId ?? vertex?.id ?? vertex?.graphId ?? "");
}

function displayRole(value) {
  const role = String(value || "").toLowerCase();
  return ROLE_ORDER.includes(role) ? role : "other";
}

function stableVertexSort(left, right) {
  const leftSequence = Number.isFinite(Number(left.sequence)) ? Number(left.sequence) : Number.MAX_SAFE_INTEGER;
  const rightSequence = Number.isFinite(Number(right.sequence)) ? Number(right.sequence) : Number.MAX_SAFE_INTEGER;
  return leftSequence - rightSequence || vertexId(left).localeCompare(vertexId(right));
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function reorderLane(lane, adjacentIds, adjacentOrder) {
  const priorOrder = new Map(lane.map((vertex, index) => [vertexId(vertex), index]));
  lane.sort((left, right) => {
    const leftScores = (adjacentIds.get(vertexId(left)) || [])
      .map((id) => adjacentOrder.get(id))
      .filter(Number.isFinite);
    const rightScores = (adjacentIds.get(vertexId(right)) || [])
      .map((id) => adjacentOrder.get(id))
      .filter(Number.isFinite);
    const leftMedian = median(leftScores);
    const rightMedian = median(rightScores);
    if (leftMedian !== null && rightMedian !== null && leftMedian !== rightMedian) return leftMedian - rightMedian;
    if (leftMedian !== null && rightMedian === null) return -1;
    if (leftMedian === null && rightMedian !== null) return 1;
    return priorOrder.get(vertexId(left)) - priorOrder.get(vertexId(right)) || stableVertexSort(left, right);
  });
}

/**
 * Produce deterministic role-lane positions from explicit process lineage only.
 * Extra edge collections on the input object are deliberately ignored so an
 * evidence or lifecycle overlay can never move process vertices.
 */
export function layoutProcessGraph({ vertices = [], processEdges = [] } = {}) {
  const unique = new Map();
  for (const vertex of vertices) {
    const id = vertexId(vertex);
    if (id && !unique.has(id)) unique.set(id, { ...vertex, id, role: displayRole(vertex.role) });
  }

  const normalizedVertices = [...unique.values()];
  const roles = [...ROLE_ORDER];
  if (normalizedVertices.some((vertex) => vertex.role === "other")) roles.push("other");
  const byRole = new Map(roles.map((role) => [role, []]));
  for (const vertex of normalizedVertices) byRole.get(vertex.role).push(vertex);
  for (const lane of byRole.values()) lane.sort(stableVertexSort);

  const edges = processEdges
    .map((edge, index) => ({
      ...edge,
      id: String(edge?.edgeId ?? edge?.id ?? `process:${edge?.source}:${edge?.target}:${index}`),
      source: String(edge?.source ?? ""),
      target: String(edge?.target ?? ""),
      type: "process",
    }))
    .filter((edge) => unique.has(edge.source) && unique.has(edge.target))
    .sort((left, right) => left.id.localeCompare(right.id));

  const predecessors = new Map(normalizedVertices.map((vertex) => [vertex.id, []]));
  const successors = new Map(normalizedVertices.map((vertex) => [vertex.id, []]));
  for (const edge of edges) {
    predecessors.get(edge.target).push(edge.source);
    successors.get(edge.source).push(edge.target);
  }

  const laneOrder = (role) => new Map((byRole.get(role) || []).map((vertex, index) => [vertex.id, index]));
  for (let sweep = 0; sweep < 4; sweep += 1) {
    for (let index = 1; index < roles.length; index += 1) {
      const prior = new Map();
      for (let priorIndex = 0; priorIndex < index; priorIndex += 1) {
        for (const [id, position] of laneOrder(roles[priorIndex])) prior.set(id, position);
      }
      reorderLane(byRole.get(roles[index]), predecessors, prior);
    }
    for (let index = roles.length - 2; index >= 0; index -= 1) {
      const following = new Map();
      for (let nextIndex = index + 1; nextIndex < roles.length; nextIndex += 1) {
        for (const [id, position] of laneOrder(roles[nextIndex])) following.set(id, position);
      }
      reorderLane(byRole.get(roles[index]), successors, following);
    }
  }

  const laneHeight = 142;
  const nodeWidth = 205;
  const nodeHeight = 62;
  const startX = 148;
  const gapX = 62;
  const positions = new Map();
  let maxColumns = 1;
  roles.forEach((role, roleIndex) => {
    const lane = byRole.get(role);
    maxColumns = Math.max(maxColumns, lane.length);
    lane.forEach((vertex, index) => positions.set(vertex.id, {
      x: startX + index * (nodeWidth + gapX),
      y: 34 + roleIndex * laneHeight,
      width: nodeWidth,
      height: nodeHeight,
      role,
      index,
      kind: vertex.kind === "skip" ? "skip" : "node",
    }));
  });

  return {
    positions,
    roles,
    byRole,
    edges,
    laneHeight,
    nodeWidth,
    nodeHeight,
    width: Math.max(880, startX + maxColumns * (nodeWidth + gapX) + 50),
    height: Math.max(500, roles.length * laneHeight + 22),
  };
}
