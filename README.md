# mycelium

Mycelium is a typed hierarchical workflow with blackboard-like evidence aggregation for agent work. Durable worker records give STEM evidence to select from and CAP material to verify, but workers are delegated explicitly rather than opportunistically activated from a shared mutable blackboard.

Status: experimental. Tested on Windows 11 (PowerShell 7, Git Bash) and Linux
with Python 3.9+ and Node 18+. The Python side is standard library only.

## Install

Clone the repository; there is no package to install for the core:

```bash
git clone https://github.com/YuraSidorets/mycelium.git
```

Every command below runs from the clone root and writes state only under the
*current project's* `.mycelium/` directory, never inside the clone.

### As a Claude Code plugin

Claude Code loads any folder under `~/.claude/skills/` that contains a
`.claude-plugin/plugin.json` as a plugin, in place. Link the clone there once:

```powershell
# Windows (directory junction, no admin rights needed)
New-Item -ItemType Junction -Path "$HOME\.claude\skills\mycelium" -Target "<path-to-clone>"
```

```bash
# Linux / macOS
ln -s <path-to-clone> ~/.claude/skills/mycelium
```

The next session exposes `/mycelium:run`, the `mycelium:apex|septum|hyphae|cap`
agents, and a Stop hook that gates session end on a sealed, verified run. The
hooks run Python through the plugin option `python_executable`. Its manifest
default is `python`, but Claude Code applies that default only once the option
is stored, so add it to `~/.claude/settings.json` (or set it in `/plugin manage`):

```json
"pluginConfigs": {
  "mycelium@skills-dir": { "options": { "python_executable": "python" } }
}
```

To make the shared contract mandatory for every session, import it from your
`~/.claude/CLAUDE.md`:

```
@skills/mycelium/SKILL.md
```

### With Codex

Codex reads skills from its skills directory and instructions from `AGENTS.md`.
Link the same clone there:

```powershell
# Windows
New-Item -ItemType Junction -Path "$HOME\.codex\skills\mycelium" -Target "<path-to-clone>"
```

```bash
# Linux / macOS (Codex CLI; some setups use ~/.agents/skills instead)
ln -s <path-to-clone> ~/.codex/skills/mycelium
```

Then import the contract from your global `~/.codex/AGENTS.md` (or a project
`AGENTS.md`), the same way as for Claude:

```
@~/.codex/skills/mycelium/SKILL.md
```

On Codex the spawn capability is `collaboration.spawn_agent`, which is the
default in `bin/subagent-brief.*`; no adapter files are needed. The `agents/`,
`claude/`, and `.claude-plugin/` directories are Claude Code only and are
ignored by Codex.

### Making it the default, not just available

Importing `SKILL.md` loads the contract; it does not by itself make an agent
use it. Add an explicit instruction next to the import in `CLAUDE.md` or
`AGENTS.md`, for example:

```
Mycelium is mandatory, not a reference. For any task that gathers evidence,
changes files, or produces a result the user will rely on, begin a tracked run
first (bin/mycelium-lineage.* begin --topology-preset apex-stem-cap, or
bin/mycelium-quick.* for a low-risk single-context goal), record every role
with -RunId, seal, and run evals/verify-cap.py before reporting done. Only a
direct factual answer that needs no file reading may skip this; say so in one
line.
```

## Local pipeline

Windows (PowerShell 7.5 or newer):

```powershell
bin/apex.ps1 "dashboard port" |
  bin/septum.ps1 "dashboard port" |
  bin/hyphae.ps1 |
  bin/stem.ps1 "dashboard port" |
  bin/cap.ps1 "dashboard port"
```

Linux with Python 3:

```bash
bash bin/apex.sh "dashboard port" |
  bash bin/septum.sh "dashboard port" |
  bash bin/hyphae.sh |
  bash bin/stem.sh "dashboard port" |
  bash bin/cap.sh "dashboard port"
```

## Tracked process runs

Use a tracked run when the executed hierarchy must be verified instead of
reconstructed from evidence relationships:

```powershell
$run = bin/mycelium-lineage.ps1 begin --goal "dashboard port" | ConvertFrom-Json
bin/mycelium-node.ps1 "dashboard port" "apex-docs" apex complete `
  "dashboard listens on port 3300" "none" "wake septum" `
  -Topics "repo,tests" -Evidence "notes.md:3" -Confidence high `
  -Consumes "none" -Blocks "none" -Version 2 -RunId $run.run_id
# Record SEPTUM, HYPHAE, STEM, and CAP with the same Run ID and explicit Process Inputs.
bin/mycelium-lineage.ps1 seal --run-id $run.run_id
```

`bash bin/mycelium-lineage.sh` accepts the same subcommands and long options.

The tracked writer durably reserves the node before writing the existing
schema-v2 node and flow record, then finalizes the reservation with exact file
hashes. A crash leaves a visible pending reservation and the run cannot seal.
Calls without `-RunId` retain the legacy Python-free Windows behavior.
Task-plane restrictions such as `do not write docs` still constrain the requested
project result, but do not suppress these internal `.mycelium` control records.

The default topology runs all five roles. SEPTUM and HYPHAE can be skipped only
with their structured, low-risk evidence gates; APEX, the sole STEM, and CAP are
always required. See `docs/design/mycelium-process-lineage.md` for the manifest,
locking, skip, and verification contracts.

## Explore artifact graphs

Mycelium Atlas is a local, read-only explorer for durable node and flow artifacts.
It requires Python 3.9 or newer, serves only on loopback, and does not read or rebuild
`.mycelium/index.json`.

When sealed run manifests exist, Atlas opens the explicit Process layer by
default. Evidence dependencies and lifecycle supersession are separate toggles;
enabling them does not move process nodes. Legacy artifacts remain visible as
untracked evidence and never receive inferred hierarchy.

Windows:

```powershell
bin/mycelium-graph.ps1
```

Linux:

```bash
bash bin/mycelium-graph.sh
```

To make Atlas available through `npx` on this machine, create a global link
from this checkout once:

```powershell
cd <path-to-clone>
npm link --ignore-scripts --offline
```

Then run it from any directory. Atlas inspects the current directory unless
you pass `--root`:

```powershell
npx mycelium-atlas
npx mycelium-atlas --root <path-to-another-repository>
```

Use `npx --offline --yes=false mycelium-atlas` when the command must not fall
back to the npm registry. Put Atlas options after `mycelium-atlas`. The link
continues to use this checkout, so moving or deleting the checkout breaks it.
Remove the link with `npm unlink --global mycelium-atlas`.

The default URL is `http://127.0.0.1:8765`. Pass `--port 0` for an available
ephemeral port or `--no-open` to keep the browser closed. Atlas shows every
lifecycle state and reports malformed artifacts without hiding valid nodes.

The Draft inspector stores proposals per repository in browser `localStorage`.
**Export plan** downloads those proposals as JSON; it never edits node or flow
files.

## Durable nodes

```powershell
bin/mycelium-node.ps1 "dashboard port" "apex-docs" apex complete `
  "dashboard listens on port 3300" "none" "wake stem" `
  -Topics "repo,tests" `
  -Evidence "notes.md:3" `
  -Confidence high `
  -Consumes "none" `
  -Blocks "none" `
  -SourceRefs "notes.md:3" `
  -AllowUntracked "example outside a tracked run"
```

Node writes are tracked by default. Pass `-RunId <id>` (see *Tracked process
runs* above) or state why the write is untracked with
`-AllowUntracked "<reason>"`; the reason is stored in the node's
`untracked_reason` field. With neither flag the writer exits 2 and writes
nothing.

```bash
bash bin/mycelium-node.sh "dashboard port" "apex-docs" apex complete \
  "dashboard listens on port 3300" "none" "wake stem" \
  --topics "repo,tests" \
  --evidence "notes.md:3" \
  --confidence high \
  --consumes "none" \
  --blocks "none" \
  --source-refs "notes.md:3" \
  --allow-untracked "example outside a tracked run"
```

Node files are written to `.mycelium/nodes/`. Flow records are written to `.mycelium/flows/`.
Index, STEM, and external CAP publish a node only when its metadata matches a flow commit.

Repository Markdown, ADRs, and runbooks remain canonical project knowledge. Mycelium nodes are derived, source-cited claims; `.mycelium/index.json` is a disposable retrieval view. Obsidian may edit or view repository Markdown, but it is not runtime infrastructure.

## Search and STEM

```powershell
bin/mycelium-index.ps1
bin/mycelium-search.ps1 "dashboard port" "repo"
bin/stem.ps1 "dashboard port"
bin/mycelium-search.ps1 "dashboard port" "repo" | bin/stem.ps1 "dashboard port" -ForCap
```

```bash
bash bin/mycelium-index.sh
bash bin/mycelium-search.sh "dashboard port" "repo"
bash bin/stem.sh "dashboard port"
bash bin/mycelium-search.sh "dashboard port" "repo" | bash bin/stem.sh "dashboard port" --for-cap
```

Search scores node IDs, topics, all facts, Evidence, source references, and typed `depends_on`/`supersedes` edges. It returns compact TSV candidates with match reasons and relationships; raw Evidence is read from the node at query time and is not copied into the index. Ignored lifecycle or source-drifted matches are traced on stderr.

Invalidated nodes and predecessors superseded by an eligible current node are omitted. With compact search input, STEM loads only the first five candidate nodes and eligible one-hop dependencies, up to ten nodes total. Without candidate input, it retains its goal-match fallback. Before `ForCap`, malformed, missing, out-of-range, or content-drifted local source refs are rejected; the external CAP verifier repeats that check after CAP.

Run the frozen 20-query lexical gate before adding semantic retrieval:

```powershell
python evals/run-retrieval.py
```

```bash
python3 evals/run-retrieval.py
```

## Reflection

```powershell
bin/mycelium-reflect.ps1 "dashboard port" "cap-final" "smoke failed" "fix schema and rerun"
```

```bash
bash bin/mycelium-reflect.sh "dashboard port" "cap-final" "smoke failed" "fix schema and rerun"
```

Reflection nodes record failed verification or blocked loops. They do not trigger automatic retries.

## Verify

```powershell
tests/smoke.ps1
```

```bash
bash tests/smoke.sh
```

## Paired workflow benchmark (optional, paid)

The benchmark harness under `benchmarks/` and `evals/run-benchmark.ps1` drives
external model tooling in Docker and spends model calls. It is not part of the
test suite and is not needed to use Mycelium. After a completed skill change,
the frozen version-7 workflow suite runs with:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File evals/run-benchmark.ps1 `
  -JobsDir jobs/skillsbench/<checkpoint> `
  -Model "<model-id>" `
  -Concurrency 4
```

The runner executes matched with-skill and no-skill arms and writes
`mycelium-summary.json` with workflow-contract compliance difference, tool
calls, timing, timeouts, expected-script use, and grader-inspection checks.
Existing results measure workflow-contract behavior only; they do not establish
general improvements in model quality, cost, or speed. Use one run for slice
feedback and three unchanged runs before claiming improvement.

## License

Apache-2.0. See `LICENSE`. Third-party tools invoked by the benchmark adapters
keep their own licenses and are not redistributed here.

## Security

See `SECURITY.md`. Report vulnerabilities through GitHub's private
vulnerability reporting, not in public issues. Your own `.mycelium/` records
are project data; keep them out of bug reports.
