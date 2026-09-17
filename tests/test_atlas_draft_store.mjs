import assert from "node:assert/strict";
import test from "node:test";

import {
  emptyDraftPlan,
  loadDraftPlan,
  saveDraftPlan,
} from "../web/mycelium-atlas/atlas-draft-store.js";

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      values.set(key, value);
    },
    value(key) {
      return values.get(key);
    },
  };
}

test("draft loading ignores corrupt and repository-mismatched values", () => {
  const corrupt = memoryStorage({ "mycelium-atlas-plan-v1:repo-id": "{" });
  assert.deepEqual(
    loadDraftPlan({ storage: corrupt, repositoryId: "repo-id", repository: "repository" }),
    emptyDraftPlan("repo-id", "repository"),
  );

  const mismatched = memoryStorage({
    "mycelium-atlas-plan-v1:repo-id": JSON.stringify({
      schemaVersion: 1,
      repositoryId: "another-repository",
      proposals: [],
    }),
  });
  assert.deepEqual(
    loadDraftPlan({ storage: mismatched, repositoryId: "repo-id", repository: "repository" }),
    emptyDraftPlan("repo-id", "repository"),
  );
});

test("draft loading sanitizes proposal fields and relations", () => {
  const storage = memoryStorage({
    "mycelium-atlas-plan-v1:repo-id": JSON.stringify({
      schemaVersion: 1,
      repositoryId: "repo-id",
      proposals: [
        {
          source: { graphId: "graph:a", nodeId: "node-a", goal: "goal" },
          current: { dependsOn: "graph:b, none", supersedes: ["graph:c"] },
          proposed: {
            role: "stem",
            relations: [
              { type: "depends_on", targetGraphId: "graph:b" },
              { type: "unsupported", targetGraphId: "graph:c" },
            ],
          },
          note: "review",
        },
        { source: {}, proposed: {} },
      ],
    }),
  });

  const plan = loadDraftPlan({ storage, repositoryId: "repo-id", repository: "repository" });

  assert.equal(plan.proposals.length, 1);
  assert.deepEqual(plan.proposals[0].current.dependsOn, ["graph:b"]);
  assert.deepEqual(plan.proposals[0].current.supersedes, ["graph:c"]);
  assert.deepEqual(plan.proposals[0].proposed.relations, [{
    type: "depends_on",
    targetGraphId: "graph:b",
    targetNodeId: "graph:b",
  }]);
});

test("draft saving scopes the value and refreshes repository identity", () => {
  const storage = memoryStorage();
  const plan = emptyDraftPlan("old-id", "old repository");

  saveDraftPlan({ storage, plan, repositoryId: "repo-id", repository: "repository" });

  assert.equal(plan.repositoryId, "repo-id");
  assert.equal(plan.repository, "repository");
  assert.deepEqual(
    JSON.parse(storage.value("mycelium-atlas-plan-v1:repo-id")),
    plan,
  );
});

test("draft storage failures remain visible to the UI boundary", () => {
  const storage = {
    setItem() {
      throw new Error("blocked");
    },
  };

  assert.throws(
    () => saveDraftPlan({ storage, plan: emptyDraftPlan(), repositoryId: "repo-id", repository: "repository" }),
    /blocked/,
  );
});
