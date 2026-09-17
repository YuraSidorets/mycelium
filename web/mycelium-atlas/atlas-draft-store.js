const PLAN_STORAGE_PREFIX = "mycelium-atlas-plan-v1";

export function emptyDraftPlan(repositoryId = "", repository = "") {
  return { schemaVersion: 1, repositoryId, repository, proposals: [] };
}

export function loadDraftPlan({ storage, repositoryId = "", repository = "" } = {}) {
  const fallback = emptyDraftPlan(repositoryId, repository);
  try {
    const target = storage ?? globalThis.localStorage;
    const parsed = JSON.parse(target.getItem(planStorageKey(repositoryId, repository)) || "null");
    if (!parsed || parsed.schemaVersion !== 1 || !Array.isArray(parsed.proposals)) return fallback;
    if (parsed.repositoryId && parsed.repositoryId !== repositoryId) return fallback;
    return {
      ...fallback,
      proposals: parsed.proposals.map(sanitizeProposal).filter(Boolean),
    };
  } catch (_error) {
    return fallback;
  }
}

export function saveDraftPlan({ storage, plan, repositoryId = "", repository = "" } = {}) {
  plan.repositoryId = repositoryId;
  plan.repository = repository;
  const target = storage ?? globalThis.localStorage;
  target.setItem(planStorageKey(repositoryId, repository), JSON.stringify(plan));
}

function planStorageKey(repositoryId, repository) {
  const scope = repositoryId || slug(repository) || "local";
  return `${PLAN_STORAGE_PREFIX}:${scope}`;
}

function sanitizeProposal(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value.source;
  const proposed = value.proposed;
  if (!source || typeof source !== "object" || !proposed || typeof proposed !== "object") return null;
  const graphId = typeof source.graphId === "string" ? source.graphId : "";
  if (!graphId) return null;
  const current = value.current && typeof value.current === "object" ? value.current : {};
  const relations = Array.isArray(proposed.relations)
    ? proposed.relations.map((relation) => {
        if (!relation || typeof relation !== "object") return null;
        const type = relation.type === "supersedes" ? "supersedes" : relation.type === "depends_on" ? "depends_on" : "";
        const targetGraphId = typeof relation.targetGraphId === "string" ? relation.targetGraphId : "";
        if (!type || !targetGraphId) return null;
        return {
          type,
          targetGraphId,
          targetNodeId: typeof relation.targetNodeId === "string" && relation.targetNodeId
            ? relation.targetNodeId
            : targetGraphId,
        };
      }).filter(Boolean)
    : [];
  return {
    source: {
      graphId,
      nodeId: typeof source.nodeId === "string" ? source.nodeId : graphId,
      goal: typeof source.goal === "string" ? source.goal : "",
      artifactPath: typeof source.artifactPath === "string" ? source.artifactPath : "",
    },
    current: {
      role: typeof current.role === "string" ? current.role : "",
      status: typeof current.status === "string" ? current.status : "",
      dependsOn: toTokens(current.dependsOn),
      supersedes: toTokens(current.supersedes),
    },
    proposed: {
      role: typeof proposed.role === "string" && proposed.role ? proposed.role : null,
      status: typeof proposed.status === "string" && proposed.status ? proposed.status : null,
      relations,
    },
    note: typeof value.note === "string" ? value.note : "",
    updatedAt: typeof value.updatedAt === "string" ? value.updatedAt : "",
  };
}

function toTokens(value) {
  if (Array.isArray(value)) return value.map(String).map((item) => item.trim()).filter((item) => item && item.toLowerCase() !== "none");
  return String(value || "").split(/[,;]/).map((item) => item.trim()).filter((item) => item && item.toLowerCase() !== "none");
}

function slug(value) {
  return String(value || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}
