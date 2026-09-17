const ROLE_ORDER = ["apex", "septum", "hyphae", "stem", "cap"];

export function selectAuthoritativeRun(processRuns = [], goal) {
  return processRuns
    .filter((run) => (
      (goal === undefined || run?.goal === goal)
      && run?.authoritative === true
      && run?.current === true
    ))
    .sort((left, right) => (
      String(right.updatedAt || "").localeCompare(String(left.updatedAt || ""))
      || String(left.runId || "").localeCompare(String(right.runId || ""))
    ))[0] || null;
}

export function normalizePayload(raw) {
  const repository = typeof raw.repository === "string"
    ? raw.repository
    : raw.repository?.name || raw.repositoryName || "mycelium";
  const repositoryId = String(raw.repositoryId ?? raw.repository_id ?? "");
  const rawNodes = Array.isArray(raw.nodes) ? raw.nodes : [];
  const nodes = rawNodes.map((node, index) => normalizeNode(node, index));
  const nodeByGraphId = new Map(nodes.map((node) => [node.graphId, node]));
  const graphIdsByNodeId = new Map();
  nodes.forEach((node) => {
    if (!graphIdsByNodeId.has(node.id)) graphIdsByNodeId.set(node.id, []);
    graphIdsByNodeId.get(node.id).push(node.graphId);
  });
  const edges = (Array.isArray(raw.edges) ? raw.edges : []).map((edge, index) => {
    const source = resolveGraphId(edge.source ?? edge.from, nodeByGraphId, graphIdsByNodeId);
    const target = resolveGraphId(edge.target ?? edge.to, nodeByGraphId, graphIdsByNodeId);
    return {
      id: String(edge.edgeId || edge.id || `${edge.type || "edge"}:${source}:${target}:${index}`),
      type: edge.type === "supersedes" ? "supersedes" : "depends_on",
      source,
      target,
      declaringNode: String(edge.declaringNodeId || edge.declaringNode || edge.declaredBy || ""),
      rawDirection: normalizeDirection(edge.rawDirection),
      canvasDirection: normalizeDirection(edge.canvasDirection),
    };
  }).filter((edge) => edge.source && edge.target);
  const processRuns = (Array.isArray(raw.processRuns) ? raw.processRuns : [])
    .map((run) => ({
      runId: String(run.runId ?? run.run_id ?? ""),
      goal: String(run.goal || ""),
      state: String(run.state || "unknown"),
      artifactPath: String(run.artifactPath ?? run.artifact_path ?? ""),
      createdAt: String(run.createdAt ?? run.created_at ?? ""),
      updatedAt: String(run.updatedAt ?? run.updated_at ?? ""),
      sealedAt: String(run.sealedAt ?? run.sealed_at ?? ""),
      topology: Array.isArray(run.topology) ? run.topology : [],
      roles: Array.isArray(run.roles) ? run.roles : [],
      skippedRoles: Array.isArray(run.skippedRoles) ? run.skippedRoles : [],
      authoritative: Boolean(run.authoritative),
      current: Boolean(run.current),
    }))
    .filter((run) => run.runId);
  const processVertices = (Array.isArray(raw.processVertices) ? raw.processVertices : [])
    .map((vertex) => ({
      vertexId: String(vertex.vertexId ?? vertex.vertex_id ?? ""),
      runId: String(vertex.runId ?? vertex.run_id ?? ""),
      graphId: String(vertex.graphId ?? vertex.graph_id ?? ""),
      nodeId: String(vertex.nodeId ?? vertex.node_id ?? ""),
      role: normalizeRole(vertex.role),
      sequence: Number(vertex.sequence || 0),
      kind: ["node", "skip", "untracked"].includes(vertex.kind) ? vertex.kind : "node",
      processInputs: toTokens(vertex.processInputs ?? vertex.process_inputs),
      skip: vertex.skip && typeof vertex.skip === "object" ? vertex.skip : null,
      authoritative: Boolean(vertex.authoritative),
      current: Boolean(vertex.current),
      reason: String(vertex.reason || ""),
    }))
    .filter((vertex) => vertex.vertexId);
  const processEdges = (Array.isArray(raw.processEdges) ? raw.processEdges : [])
    .map((edge, index) => ({
      id: String(edge.edgeId || edge.id || `process:${index}`),
      type: "process",
      runId: String(edge.runId ?? edge.run_id ?? ""),
      source: String(edge.source || ""),
      target: String(edge.target || ""),
      sourceNodeId: String(edge.sourceNodeId ?? edge.source_node_id ?? ""),
      targetNodeId: String(edge.targetNodeId ?? edge.target_node_id ?? ""),
      declaredSourceNodeId: String(edge.declaredSourceNodeId ?? edge.declared_source_node_id ?? edge.sourceNodeId ?? ""),
      declaredTargetNodeId: String(edge.declaredTargetNodeId ?? edge.declared_target_node_id ?? edge.targetNodeId ?? ""),
      segment: Number(edge.segment || 1),
      segmentCount: Number(edge.segmentCount ?? edge.segment_count ?? 1),
      authoritative: Boolean(edge.authoritative),
    }))
    .filter((edge) => edge.source && edge.target);
  const processMap = new Map();
  nodes.forEach((node) => {
    if (!processMap.has(node.goal)) processMap.set(node.goal, []);
    processMap.get(node.goal).push(node);
  });
  const providedProcesses = Array.isArray(raw.processes) ? raw.processes : [];
  const processes = providedProcesses.length
    ? providedProcesses.map((process) => ({
        goal: String(process.goal ?? process.id ?? ""),
        nodeCount: Number(process.nodeCount ?? process.count ?? processMap.get(String(process.goal ?? process.id ?? ""))?.length ?? 0),
        updated: String(process.updated || processMap.get(String(process.goal ?? process.id ?? ""))?.map((node) => node.updated).sort().at(-1) || ""),
      }))
    : [...processMap.entries()].map(([goal, group]) => ({
        goal,
        nodeCount: group.length,
        updated: group.map((node) => node.updated).sort().at(-1) || "",
      }));
  processes.sort((left, right) => right.updated.localeCompare(left.updated) || left.goal.localeCompare(right.goal));
  const schemaVersion = Number(raw.schemaVersion || 1);
  return {
    schemaVersion,
    repository,
    repositoryId,
    generatedAt: String(raw.generatedAt || ""),
    nodes,
    edges,
    processRuns,
    processVertices,
    processEdges,
    hasAuthoritativeLineage: schemaVersion >= 2 && Boolean(selectAuthoritativeRun(processRuns)),
    processes,
    diagnostics: Array.isArray(raw.diagnostics) ? raw.diagnostics.map(normalizeDiagnostic) : [],
    summary: raw.summary || {},
    nodeByGraphId,
    graphIdsByNodeId,
  };
}

export function emptyData() {
  return {
    schemaVersion: 1,
    repository: "mycelium",
    repositoryId: "",
    generatedAt: "",
    nodes: [],
    edges: [],
    processRuns: [],
    processVertices: [],
    processEdges: [],
    hasAuthoritativeLineage: false,
    processes: [],
    diagnostics: [],
    summary: {},
    nodeByGraphId: new Map(),
    graphIdsByNodeId: new Map(),
  };
}

export function chooseInitialGoal(data) {
  const authoritative = selectAuthoritativeRun(data.processRuns);
  if (authoritative) return authoritative.goal;
  const withCurrent = data.processes.find((process) => data.nodes.some((node) => node.goal === process.goal && node.lifecycle.current));
  return withCurrent?.goal || data.processes[0]?.goal || "";
}

function normalizeNode(node, index) {
  const id = String(node.id ?? node.nodeId ?? `artifact-${index + 1}`);
  const graphId = String(node.graphId ?? node.graph_id ?? id);
  const role = String(node.role || node.rawRole || "other").toLowerCase();
  const facts = normalizeFacts(node.facts, node.factRows);
  const sourceRefs = normalizeSourceRefs(node.sourceRefs ?? node.source_refs, node.sourceRefDetails);
  const lifecycleInput = node.lifecycle || {};
  const classification = normalizeLifecycleClass(lifecycleInput.classification || node.classification || node.lifecycleState || deriveLifecycle(node, lifecycleInput));
  const lifecycle = {
    parsed: booleanValue(lifecycleInput.parsed, true),
    flowCommitted: booleanValue(lifecycleInput.flowCommitted, node.flowCommitted),
    invalidated: booleanValue(lifecycleInput.invalidated, node.invalidated),
    sourcePresent: booleanValue(lifecycleInput.sourcePresent, sourceRefs.length > 0),
    sourceCurrent: booleanValue(lifecycleInput.sourceCurrent, node.sourceCurrent),
    eligibleCurrent: booleanValue(lifecycleInput.eligibleCurrent, node.eligibleCurrent),
    superseded: booleanValue(lifecycleInput.superseded, node.superseded),
    searchVisible: booleanValue(lifecycleInput.searchVisible, node.searchVisible),
    capEligible: booleanValue(lifecycleInput.capEligible, node.capEligible),
    classification,
    current: booleanValue(lifecycleInput.current, node.current ?? lifecycleInput.capEligible ?? node.capEligible ?? classification === "current"),
  };
  return {
    graphId,
    id,
    goal: String(node.goal || node.parentGoalId || node.parent_goal_id || "Unassigned goal"),
    role,
    rawRole: String(node.rawRole || node.role || ""),
    status: String(node.status || "unknown"),
    confidence: String(node.confidence || ""),
    topics: toTokens(node.topics),
    facts,
    firstFact: String(node.firstFact || facts[0]?.text || ""),
    evidence: String(node.evidence || ""),
    questions: toLines(node.questions),
    next: toLines(node.next ?? node.nextSteps),
    trace: String(node.trace || ""),
    reflection: String(node.reflection || ""),
    consumes: String(node.consumes || ""),
    blocks: String(node.blocks || ""),
    parentGoalId: String(node.parentGoalId ?? node.parent_goal_id ?? ""),
    producingAgent: String(node.producingAgent ?? node.producing_agent ?? ""),
    version: String(node.version || ""),
    sourceRefs,
    dependsOn: toTokens(node.dependsOn ?? node.depends_on),
    supersedes: toTokens(node.supersedes),
    invalidated: booleanValue(node.invalidated, lifecycle.invalidated),
    updated: String(node.updated || ""),
    artifactPath: String(node.artifactPath ?? node.node_path ?? node.path ?? ""),
    flowHistory: normalizeFlowHistory(node.flowHistory ?? node.flow_history),
    lifecycle,
  };
}

function normalizeFacts(facts, factRows) {
  const value = Array.isArray(facts) ? facts : Array.isArray(factRows) ? factRows : toLines(facts);
  return value.filter(Boolean).map((fact) => typeof fact === "string"
    ? { text: fact.replace(/^[-*]\s+/, ""), line: 0 }
    : { text: String(fact.text || fact.fact || "").replace(/^[-*]\s+/, ""), line: Number(fact.line || 0) }).filter((fact) => fact.text);
}

function normalizeSourceRefs(value, details) {
  const source = Array.isArray(details) && details.length ? details : Array.isArray(value) ? value : toTokens(value);
  return source.map((reference) => typeof reference === "string"
    ? { reference, current: false, reason: "Validation details unavailable" }
    : {
        reference: String(reference.reference ?? reference.value ?? reference.ref ?? reference.raw ?? ""),
        current: Boolean(reference.current ?? reference.valid),
        reason: String(reference.reason || ""),
      }).filter((reference) => reference.reference);
}

function normalizeFlowHistory(value) {
  if (!Array.isArray(value)) return [];
  return value.map((record) => ({
    path: String(record.path || record.flowPath || record.artifactPath || ""),
    line: Number(record.line || 0),
    timestamp: String(record.timestamp || ""),
    status: String(record.status || ""),
    role: String(record.role || ""),
    matchesCurrent: Boolean(record.matchesCurrent ?? record.matchesProjection ?? record.committed),
  }));
}

function normalizeDiagnostic(item) {
  return {
    severity: String(item.severity || "warning"),
    kind: String(item.kind || item.code || "artifact"),
    message: String(item.message || item.error || "Artifact warning"),
    path: String(item.path || item.artifactPath || ""),
    line: Number(item.line || 0),
    nodeId: String(item.nodeId || ""),
    graphId: String(item.graphId || item.nodeGraphId || ""),
  };
}

function deriveLifecycle(node, lifecycle) {
  if (booleanValue(lifecycle.invalidated, node.invalidated)) return "invalidated";
  if (!booleanValue(lifecycle.flowCommitted, node.flowCommitted)) return "uncommitted";
  if (String(node.status || "").toLowerCase() === "blocked") return "blocked";
  if (String(node.status || "").toLowerCase() !== "complete") return "alive";
  if (booleanValue(lifecycle.superseded, node.superseded)) return "superseded";
  if (!booleanValue(lifecycle.sourceCurrent, node.sourceCurrent)) return "source-drift";
  return "current";
}

function normalizeLifecycleClass(value) {
  const classification = slug(String(value || "ineligible"));
  if (classification === "cap-eligible" || classification === "eligible-current") return "current";
  if (classification === "source-drifted" || classification === "stale-source") return "source-drift";
  return classification;
}

function toTokens(value) {
  if (Array.isArray(value)) return value.map(String).map((item) => item.trim()).filter((item) => item && item.toLowerCase() !== "none");
  return String(value || "").split(/[,;]/).map((item) => item.trim()).filter((item) => item && item.toLowerCase() !== "none");
}

function toLines(value) {
  if (Array.isArray(value)) return value.map((item) => typeof item === "string" ? item : item.text || "").map((item) => String(item).trim()).filter(Boolean);
  return String(value || "").split(/\r?\n/).map((item) => item.trim()).filter((item) => item && item.toLowerCase() !== "none");
}

function booleanValue(primary, fallback = false) {
  const value = primary === undefined || primary === null ? fallback : primary;
  if (typeof value === "string") return value.toLowerCase() === "true";
  return Boolean(value);
}

function resolveGraphId(value, byGraphId, byNodeId) {
  const id = String(value || "");
  if (byGraphId.has(id)) return id;
  const matches = byNodeId.get(id) || [];
  return matches.length === 1 ? matches[0] : "";
}

function normalizeDirection(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  const from = value.fromNodeId ?? value.from ?? "";
  const to = value.toNodeId ?? value.to ?? "";
  return from && to ? `${from} → ${to}` : "";
}

function normalizeRole(value) {
  const role = String(value || "").toLowerCase();
  return ROLE_ORDER.includes(role) ? role : "other";
}

function slug(value) {
  return String(value || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}
