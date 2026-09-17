# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-09-17

First public release.

### Added

- Role contracts (`SKILL.md`, `apex.md`, `septum.md`, `hyphae.md`, `stem.md`,
  `cap.md`) and the durable node and flow record format under `.mycelium/`.
- Python and PowerShell implementations of the local pipeline, node writer,
  index, search, lineage (tracked runs with reservations and sealing), lint,
  reflection digest, calibration ledger, and the quick single-command run.
- External CAP verifier (`evals/verify-cap.py`) with optional sandboxed
  re-execution of cited commands and a `Verification-Mode` line.
- Claude Code plugin adapter: `/mycelium:run` skill, four worker agent
  definitions, activation hook, and a Stop hook that gates session end on a
  sealed and verified run.
- Mycelium Atlas, a loopback-only, read-only viewer for node, flow, and run
  artifacts.
- Test suites for Python, PowerShell, POSIX shell, and Node paths.
