# SWE-bench-Live direct Codex pilot

This directory contains the fail-closed external pilot harness. It does not
contain candidate metadata, hidden task rows, a preregistration, or results.
Those experiment inputs must be prepared outside the treatment checkout.

## 1. Deterministic gate

```powershell
python -B -m unittest discover -s benchmarks/swebench_live -p "test_*.py"
```

## 2. Isolated runtime

Use Python 3.12 or newer because Pier 0.3.0 requires it. Keep the evaluator
and dependencies outside this repository.

```powershell
$PilotRoot = Join-Path $env:LOCALAPPDATA "Temp\mycelium-swebench-live"
$EvaluatorRoot = Join-Path $PilotRoot "evaluator"
$PilotRuntime = Join-Path $PilotRoot "runtime"
$PilotPython = Join-Path $PilotRuntime "Scripts\python.exe"

New-Item -ItemType Directory -Path $PilotRoot -Force | Out-Null
git clone https://github.com/microsoft/SWE-bench-Live.git $EvaluatorRoot
git -C $EvaluatorRoot checkout --detach 70ec57e852e3f2d195790fe71f553e272c691833
git -C $EvaluatorRoot submodule update --init --recursive
uv venv --python 3.12 $PilotRuntime
uv pip install --python $PilotPython -e $EvaluatorRoot datacurve-pier==0.3.0
```

Verify the pinned Codex app-server without starting a model turn. The model
and effort must match the effective Codex configuration; revision identity is
confirmed only by the later canary.

```powershell
$ModelRevision = "replace-with-pinned-model-revision"
$ReasoningEffort = "replace-with-configured-effort"

$PilotPython -B benchmarks/swebench_live/preflight.py `
  --provider openai `
  --model gpt-5.6-sol `
  --revision $ModelRevision `
  --reasoning-effort $ReasoningEffort `
  --cwd $PWD.Path `
  --artifacts (Join-Path $PilotRoot "preflight")
```

Require `STRUCTURAL_READY_NO_MODEL_CALL`, `model_calls: 0`, and
`turn_start_requests: 0`.

## 3. External inputs

Before freezing a manifest, provide:

- candidate metadata from `SWE-bench-Live/MultiLang` revision
  `608f7ae9ab8ea1f9f0d030fe04562cf6bd1a0c8b`, including retained 3/3 gold
  receipts and immutable image digests;
- hidden task rows whose hashes match the candidate metadata;
- an immutable published preregistration artifact, URI, and SHA-256;
- an exact committed treatment and an installed treatment with identical
  staged bytes.

Use `pilot.py freeze --help` to freeze the v4 `NO-SCORE` manifest, then
`pilot.py validate` before any execution.

## 4. Explicit execution boundary

`run_pilot.py` will not make a model call unless both
`--authorize-model-calls` and `--accept-task-container-auth-risk` are present.
The latter acknowledges that Pier places ephemeral ChatGPT authentication in
the untrusted task container.

First run exactly one full-arm canary with `--canary-only`. Validate and retain
its receipt. The 30-run diagnostic pilot additionally requires that receipt
through `--canary-receipt`. Do not infer efficacy from the structural preflight
or from a canary alone.
