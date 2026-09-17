# Security policy

## Supported versions

Mycelium is experimental. Only the latest commit on `main` receives fixes.

## Reporting a vulnerability

Do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting on this repository ("Report a vulnerability" under the
Security tab). Include the affected file, a minimal reproduction, and the
impact you expect. You will get an acknowledgement within seven days.

## What is in scope

- The CLI scripts under `bin/`, the hook and plugin adapter under `claude/`,
  `agents/`, and `.claude-plugin/`, the verifier under `evals/`, and the Atlas
  viewer under `web/` and `bin/mycelium_graph.py`.
- Path containment: every writer must stay under the current project's
  `.mycelium/` directory, and the Stop hook must never write inside the plugin
  directory.
- The Atlas server binds to loopback only and serves reads only.

## What is not a vulnerability

- Content of your own `.mycelium/` records. They contain whatever your tasks
  put there; treat them as private project data and do not share them in
  reports.
- Behavior of external benchmark adapters under `benchmarks/`, which invoke
  third-party tools with their own security models.

## Handling

A confirmed report gets a fix on `main`, a changelog entry, and a GitHub
security advisory when the impact warrants one.
