# Mycelium Atlas design specification

## Product boundary

Mycelium Atlas is a repository-local, read-only explorer for `.mycelium/nodes/*.md`,
`.mycelium/flows/*.jsonl`, and `.mycelium/runs/*.json`. It visualizes one goal at
a time, keeps process lineage separate from evidence and lifecycle relationships,
exposes provenance details, and lets a user prepare an adjustment plan.
Drafts remain in browser storage and export as JSON. The first version does not
rewrite node or flow artifacts.

Visual references:

- `docs/design/mycelium-atlas-desktop.png` — primary 1600 x 1000 workspace.
- `docs/design/mycelium-atlas-mobile.png` — 430 x 932 graph with an inspector sheet.

## Information architecture

1. Top bar: product mark and title, repository path, search, Reload, Export plan.
2. Process rail: goal list with role and status filters plus the read-only notice.
3. Graph canvas: deterministic role lanes, independent Process, Evidence, and
   Lifecycle layers, pan/zoom/fit, and legend.
4. Inspector: Details, Relations, and Draft tabs for the selected node.
5. Status rail: visible node/edge counts, lifecycle filter, server state, warnings.

## Design system

- Canvas: `#08111b`; chrome: `#0d1722`; raised surface: `#111d29`.
- Text: `#f4f7fb`; secondary text: `#9ca9b7`; border: `#2a3948`.
- Focus/action: `#2f6fed`; connected: `#72c47c`; warning: `#e7ad45`;
  invalid/error: `#ef6d68`.
- Role colors: APEX `#76c86c`, SEPTUM `#e3ad45`, HYPHAE `#49c7e8`,
  STEM `#a67bea`, CAP `#ef7771`.
- UI chrome uses `Inter`, `Segoe UI`, or a system sans fallback. Facts and long
  evidence use the same family with a relaxed line height. Code and paths use
  `Cascadia Code`, `SFMono-Regular`, or a monospace fallback.
- Spacing scale: 4, 8, 12, 16, 24, 32. Borders are 1 px. Radii are 6 or 9 px.
- Motion is limited to selection, inspector transitions, and graph transforms;
  `prefers-reduced-motion` disables nonessential animation.

## Graph model

- Process lineage comes only from a schema-1 run manifest. A committed node's
  `process_inputs` create input-to-output process edges. Role order, timestamps,
  `depends_on`, `consumes`, and artifact proximity never create process edges.
- Only a sealed run whose bound node and flow bytes still match their recorded
  SHA-256 hashes, identities, timestamp, and commit digest is authoritative.
  Open, invalid, malformed, and stale runs remain visible as diagnostics.
- Process is the default layer when the selected goal has an authoritative run.
  A schema-v1 payload or goal without one falls back to the Evidence layer and
  displays a lineage-unavailable warning instead of inventing hierarchy.
- Process nodes and explicit skipped-role markers are placed in APEX, SEPTUM,
  HYPHAE, STEM, and CAP lanes. Within each lane a deterministic median sweep uses
  only process edges to reduce crossings. Evidence and Lifecycle toggles never
  reposition process vertices.
- When an explicit `process_inputs` edge crosses roles declared skipped by the
  locked topology, Atlas segments that one declared edge through the corresponding
  skip markers. It does not retain a parallel direct bridge or leave markers floating.
- `depends_on` belongs to the Evidence layer and is drawn dependency to dependent;
  the inspector preserves the declaring node and raw stored value.
- `supersedes` belongs to the Lifecycle layer and is drawn predecessor to
  replacement; the inspector preserves the replacement and raw stored value.
- `consumes` and `blocks` remain workflow-detail fields because the current schema
  accepts free-form text and does not define them as typed graph edges.
- The inspector groups Process upstream/downstream, Evidence dependencies, and
  Lifecycle replacements so the three meanings cannot be mistaken for one graph.
- Schema-v1/v2 nodes not present in an authoritative run are `legacy-untracked`.
  They remain inspectable in the fallback view and are never automatically bound.
- Invalidated, superseded, incomplete, uncommitted, source-drifted, and current
  nodes remain inspectable. Lifecycle filters change visibility, not source data.
- Malformed artifacts appear as warnings and do not prevent valid nodes loading.

## Graph payload

Payload schema 2 retains `nodes`, `edges`, and `processes` from schema 1. The
meaning and direction of every existing `edges` item is unchanged. It adds:

- `processRuns`: manifest state, role-state vector, topology, skip reasons, and
  authoritative/current flags.
- `processVertices`: committed run-node memberships, skipped-role markers, and
  explicit untracked projections for legacy artifacts.
- `processEdges`: only the manifest's `process_inputs` relationships.
- `topologyDiagnostics`: read-only observations about malformed, open, invalid,
  stale, or internally inconsistent run manifests.

## Draft adjustment plan

The Draft tab captures a change note, suggested role, suggested status, and one
or more typed relations. `Add to plan` stores the proposal in `localStorage`
scoped by a non-sensitive repository ID. Malformed browser drafts are ignored
without blocking the graph.
`Export plan` downloads a JSON document containing the repository, source node,
current values, proposed values, relations, note, and export time. Every draft
surface states that source files are not edited.

## Responsive behavior

- At desktop width, the process rail, graph, and inspector are simultaneous.
- Below 1100 px, the process rail becomes an overlay opened from the top bar.
- Below 760 px, the graph fills the viewport and the inspector becomes a bottom
  sheet. The graph remains pannable; filters become a compact toolbar.
- Touch targets are at least 40 px. No primary content may overflow the viewport.

## Allowed first-viewport copy

`Mycelium Atlas`, `Processes`, `Search nodes, facts, files...`, `Reload`,
`Export plan`, `Process`, `Evidence`, `Lifecycle`, `Details`, `Relations`, `Draft`, `Source artifacts are read-only`,
`Drafts do not edit source files.`, `Change note`, `Suggested role`,
`Suggested status`, `Add relation`, `Add to plan`, `Lifecycle`, `Fit`, and the
runtime repository, process, node, role, status, relation, and count values.

## Implementation inventory

- Python standard-library HTTP server bound to loopback by default.
- Non-loopback `Host` headers are rejected before repository data is served.
- The server permits only GET and HEAD; no endpoint writes repository artifacts.
- Fresh in-memory graph payload built from node, flow, and run artifacts on every
  API request; `.mycelium/index.json` is never read or rebuilt.
- Native HTML, CSS, JavaScript, and SVG. No package installation or CDN runtime.
- PowerShell and POSIX shell launchers matching the existing command surfaces.
- Python tests for parsing, lifecycle/edge resolution, HTTP behavior, and the
  read-only `.mycelium` contract; browser verification covers the interactive UI.
