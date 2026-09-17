# Durable Nodes Example

Record worker results as durable Mycelium nodes.

```powershell
bin/mycelium-node.ps1 "dashboard port" "apex-docs" apex complete `
  "dashboard listens on port 3300" "none" "wake stem" `
  -Topics "repo,tests" `
  -Evidence "notes.md:3; command: rg dashboard" `
  -Confidence high `
  -Consumes "none" `
  -Blocks "none" `
  -SourceRefs "notes.md:3"
```

Rebuild the topic index and search it:

```powershell
bin/mycelium-index.ps1
bin/mycelium-search.ps1 "dashboard port" "repo"
```

Run STEM without pipeline input to select from durable nodes:

```powershell
bin/stem.ps1 "dashboard port"
```
