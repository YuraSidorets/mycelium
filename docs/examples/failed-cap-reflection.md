# Failed CAP Reflection Example

When verification fails, record the failure as a reflection node. Do not hide the failed check and do not start an automatic retry loop.

```powershell
bin/mycelium-reflect.ps1 `
  "dashboard port" `
  "cap-final" `
  "tests/smoke.ps1 failed: missing schema-v2 heading" `
  "add heading persistence and rerun smoke"
```

The script writes `.mycelium/nodes/reflection-cap-final.md` and records the failed node in `Consumes`.
