# Local Pipeline Example

Use the local pipeline when one context is enough and no real subagents are needed.

```powershell
bin/apex.ps1 "dashboard port" |
  bin/septum.ps1 "dashboard port" |
  bin/hyphae.ps1 |
  bin/stem.ps1 "dashboard port" |
  bin/cap.ps1 "dashboard port"
```

Expected shape:

```text
Goal: dashboard port
Spore:
Result:
- dashboard listens on port 3300 (notes.md:3)
Verification: EXTERNAL_REQUIRED
Residual risk: selected facts and source evidence are not yet externally verified.
Follow-up: run the external verifier before claiming PASS.
```

## Reuse durable nodes

When workers have already written records under `.mycelium/nodes`, ask STEM
to emit CAP's four-column pipeline contract explicitly:

```powershell
"" | bin/stem.ps1 "dashboard port" -ForCap |
  bin/cap.ps1 "dashboard port"
```

`-ForCap` emits selected nodes only. Each fact carries its Node ID and a path
such as `.mycelium/nodes/hyphae-summary.md`; that file contains the durable
`source_refs` metadata needed to trace the CAP result back to its source.
Invalidated, non-complete, insufficiently relevant, and source-less nodes are
not dispatched. If selected durable nodes conflict, STEM emits the conflict
instead of fact records and CAP exits with `Verification: FAIL`.
