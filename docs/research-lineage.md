# Research Lineage

Mycelium is a typed hierarchical workflow with blackboard-like evidence aggregation. The defensible claim is a distinctive combination of a fixed evidence-transformation chain, exactly one STEM per goal, mandatory CAP dispatch before completion, durable per-worker records with Node IDs, and a Codex-specific bridge from role briefs to real subagents and persistent records.

## Blackboard Systems

HEARSAY-II and Nii-style blackboard systems are the nearest classical research family. They share the broad shape of specialized contributors, accumulated problem state, and a control layer that decides what to use next.

The qualification matters: Mycelium is not a full blackboard implementation. It has typed roles, explicit downward delegation from one STEM coordinator, durable node files rather than a shared mutable blackboard, and mandatory CAP finalization rather than opportunistic worker activation.

## Magentic-One

Magentic-One is the closest LLM-era central-orchestrator topology analogue. Its lead Orchestrator plans, delegates to specialized agents, tracks progress in structured ledgers, and replans from returned results.

The differences matter: Magentic-One specialization is mostly by tool capability, its ledgers are working coordination state rather than a durable node-per-worker evidence corpus, and it has no equivalent fixed APEX->SEPTUM->HYPHAE->STEM->CAP transformation pipeline or mandatory CAP stage.

## MetaGPT

MetaGPT is the closest role/SOP workflow analogue. It uses role contracts, standard operating procedures, staged handoffs, intermediate artifacts, and assembly-line execution.

The scope differs: MetaGPT roles are modeled on a software company and its workflow is software-engineering-specific. Mycelium assigns epistemic functions such as gathering, filtering, compression, adjudication, and finalization.

## Contract Net

Contract Net is a secondary comparison. Its defining contribution is decentralized task allocation through announcements, bids, awards, and dynamically assumed manager or contractor roles.

Mycelium STEM directly assigns scoped work. There is no bidding, mutual selection, or absence of global control. Cite Contract Net for task contracts and delegation protocol vocabulary, not as the overall architectural ancestor.

## Stigmergy

Stigmergy is a weak analogy under the current specification. Stigmergic coordination occurs when agents alter an environment and those traces indirectly stimulate later behavior, normally without explicit centralized routing.

Mycelium nodes are explicitly produced, addressed by Node ID, and aggregated by STEM. That makes them closer to workflow provenance, checkpoints, or a persistent coordination repository. Stigmergy should only be cited as a possible secondary property if workers can independently discover and react to existing nodes.

## W3C PROV

W3C PROV is the stronger comparison for durable records. Mycelium nodes should identify the produced entity, producing agent, activity, source evidence, dependencies, version, and derivation chain so final assertions can be traced back to worker activity and original evidence.

## Mechanism-Level Influences

- ReAct: reasoning/action interleaving inside an agent.
- Reflexion: episodic feedback and reuse across attempts.
- Generative Agents: persistent memory, retrieval, reflection, and planning.
- Tree of Thoughts: branching, evaluation, and selection among candidate reasoning paths.

Tree of Thoughts has some affinity with SEPTUM branching and STEM selection, but these works do not define Mycelium role topology, durable node protocol, or mandatory final-production stage.

## Risks And Limits

- Category conflation: node files are not the same as a shared mutable blackboard.
- Stigmergy overclaim: durable traces alone are not stigmergy unless traces indirectly trigger or shape later agent behavior.
- Contract Net overclaim: structured briefs are not Contract Net without announcement, bidding, selection, and award semantics.
- Novelty overclaim: specialized agents, central orchestration, staged SOPs, final verification, structured memory, and provenance all have precedents.
- Architectural similarity versus scientific validation: lineage does not prove correctness, efficiency, or reliability.
- Single-coordinator limitations: one STEM simplifies conflict resolution but creates a bottleneck, coordination failure point, and aggregation-loss risk.
- Durability ambiguity: without immutability, versioning, lineage, attribution, and invalidation, old nodes can be mistaken for current evidence.
- CAP ambiguity: mandatory CAP is substantive only if it performs specified production or verification work rather than formatting a STEM answer.

## Evaluation Plan

Feature matrix rows: shared mutable state, centralized versus decentralized control, opportunistic versus explicit activation, fixed versus dynamic roles, task negotiation, durable per-worker records, branching and pruning, conflict resolution, mandatory finalization, provenance and replay.

Compare columns: Mycelium, HEARSAY-II, Contract Net, Magentic-One, MetaGPT, STORM, Tree of Thoughts.

Behavioral tests:
- Blackboard test: can a state update independently activate eligible workers without STEM issuing a direct command?
- Contract Net test: can workers bid for tasks and can STEM select among bids?
- Stigmergy test: can a worker discover a node trace and alter its behavior without direct communication from its creator or STEM?
- Provenance test: can every final assertion be traced through Node IDs to worker activity and original evidence?

Ablations: one Codex agent versus full Mycelium, no SEPTUM, no HYPHAE, no durable nodes, no CAP, one CAP versus multiple independent CAP reviewers, STEM receiving raw evidence versus compact HYPHAE facts.

Adversarial coordination cases: duplicated evidence, contradictory sources, high-volume irrelevant worker, failed worker, stale nodes, two nodes citing the same source, and CAP rejecting STEM selected conclusion.

Benchmark discipline: preregister benchmark tasks and compare against strong single-agent and orchestrated baselines before making performance claims.

Revisit embedding-based retrieval only if `.mycelium/reflections-digest.md` records three or more reflections where keyword search missed a node that a later step needed.
