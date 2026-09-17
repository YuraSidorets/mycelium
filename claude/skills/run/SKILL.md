---
name: run
description: Use for any task that gathers evidence, changes files, or produces a result the user will rely on. Starts a tracked Mycelium run in Claude Code: the main session is STEM, APEX/SEPTUM/HYPHAE/CAP run as plugin agents via the Agent tool, every result is a durable node, and the Stop hook gates completion on a sealed, verified run. Skip only for a direct factual answer that needs no file reading or change.
disable-model-invocation: false
---

# Mycelium run (Claude Code adapter)

This skill is the Claude Code host adapter. It adds nothing to the Mycelium
contract; it says which host primitive plays each part.

Mycelium is the default working method. Start this skill yourself at the
beginning of any task that gathers evidence, changes files, or produces a
result the user will rely on; the user may also type `/mycelium:run <goal>`.
Skip it only for a direct factual answer that needs no file reading or change,
and say so in one line. (Default activation was enabled on 2026-09-16 at the
user's request, superseding the explicit-only v1 decision in the port plan.)

## Load the shared contract first

Read `${CLAUDE_PLUGIN_ROOT}/SKILL.md` and follow it. It is the single source of
truth for the flow pattern, the control-plane boundary, the topology, tracked
process runs, the role rules, and the red flags. Also read the role file for
each role you dispatch: `${CLAUDE_PLUGIN_ROOT}/apex.md`,
`${CLAUDE_PLUGIN_ROOT}/septum.md`, `${CLAUDE_PLUGIN_ROOT}/hyphae.md`,
`${CLAUDE_PLUGIN_ROOT}/stem.md`, `${CLAUDE_PLUGIN_ROOT}/cap.md`.

Where the shared `SKILL.md` names `collaboration.spawn_agent` as the host spawn
capability, this host's capability is the `Agent` tool instead. Everything else
in that file applies unchanged.

## Host mapping

| Mycelium role | Claude Code primitive |
| --- | --- |
| STEM | This main session. There is no STEM agent and you never dispatch one. |
| APEX | `Agent` with `subagent_type: mycelium:apex` |
| SEPTUM | `Agent` with `subagent_type: mycelium:septum` |
| HYPHAE | `Agent` with `subagent_type: mycelium:hyphae` |
| CAP | `Agent` with `subagent_type: mycelium:cap` |
| Durable state | `${CLAUDE_PROJECT_DIR}/.mycelium` through the existing CLI |
| Shared code | `${CLAUDE_PLUGIN_ROOT}`, read only |

The four worker definitions live in `${CLAUDE_PLUGIN_ROOT}/agents/`. Use the
plugin-scoped `mycelium:<role>` name; the bare `<role>` name works when no
other plugin defines it.

## Dispatch is the Agent tool, not a script

`bin/subagent-brief.ps1` is for hosts with no native subagent primitive. This
host has one, so do not print a brief for a human to paste. Call the `Agent`
tool directly with the role's plugin agent and give it the goal, the Node ID,
the Run ID, and the explicit Process Inputs.

The four plugin agents load their role contracts by path and cannot call
`Agent` themselves, so there is no worker fan-out.

## Procedure

1. State the goal in one sentence.
2. Begin the tracked run and lock the five-role topology with
   `bin/mycelium-lineage.ps1` (PowerShell) or `bin/mycelium-lineage.sh` (POSIX).
   Keep the returned Run ID for every later call.
3. Write this session's active run marker immediately after `begin`, so the
   `Stop` hook knows there is a run to gate. The activation hook has already
   created `${CLAUDE_PROJECT_DIR}/.mycelium/claude/<session-id>/session.json`;
   the marker is `run.json` beside it, written by `bin/mycelium-mark-run.ps1`
   (PowerShell) or `bin/mycelium-mark-run.sh` (POSIX):

   ```powershell
   bin/mycelium-mark-run.ps1 begin --session-id "<session-id>" --run-id "<run-id>" --goal "<goal>" --cap-file "<cap-file>" --cap-node-path ".mycelium/nodes/<cap-node-id>.md"
   ```

   `<session-id>` is the directory name the activation hook created under
   `${CLAUDE_PROJECT_DIR}/.mycelium/claude/`, which is also the `sessionId`
   field inside that directory's `session.json`.

   `cap_file` is the CAP spore file the external verifier will read, not the
   CAP node. If you do not know it yet, write the path you intend to use; the
   gate blocks on a marker whose `cap_file` does not verify.
4. Reserve the Node ID, role, and explicit Process Inputs for each worker under
   the run lock before dispatching it.
5. Dispatch the worker with the `Agent` tool and the matching
   `mycelium:<role>` agent. Many APEX, SEPTUM, HYPHAE, or CAP agents are
   allowed when each has a narrow independent scope.
6. Record what the agent actually returned:

   ```powershell
   bin/mycelium-node.ps1 "<goal>" "<node-id>" <role> complete "<facts>" "<questions>" "<next>" -Topics "<topics>" -Evidence "<evidence>" -Confidence <confidence> -Consumes "<consumes>" -Blocks "<blocks>" -Trace "Action: Agent; Observation: returned agent <agent-id>; Result: <result>" -ProducingAgent "<agent-id>" -ParentGoalId "<goal>" -SourceRefs "<source-refs>" -Version "2" -RunId "<run-id>" -ProcessInputs "<node-id>","<node-id>"
   ```

   A `Trace` that names `Agent` requires a non-empty `-ProducingAgent` and the
   `Action:`, `Observation:`, and `Result:` segments. Record the agent ID the
   host returned; do not invent one. If you did not call `Agent`, do not write a
   `Trace` that says you did.
7. Finalize each reservation with the exact commit binding the writer printed.
8. Merge the worker records yourself, as STEM, and stop discovery when the
   evidence is enough.
9. Dispatch mandatory CAP to produce the spore under `.mycelium/`. CAP leaves
   `Verification: EXTERNAL_REQUIRED` and cannot certify itself.
10. Seal the run once every required role is resolved and CAP is downstream of
    the sole STEM.
11. Run the external verifier yourself, outside any agent:
    `python evals/verify-cap.py <workspace> <cap-file> --run-id <run-id>`.
    Require exit 0 and `Verification: PASS`. Read the `Verification-Mode:` line
    to see whether the verifier re-executed or accepted citations. If no
    verifier is configured, report the result as unverified, not successful.
12. Mark the marker done once the run is sealed and verified, or blocked when it
    is not:

    ```powershell
    bin/mycelium-mark-run.ps1 done --session-id "<session-id>"
    ```

    Use `blocked` instead of `done` when the goal could not be reached:

    ```powershell
    bin/mycelium-mark-run.ps1 blocked --session-id "<session-id>" --reason "<why the run is blocked>"
    ```

    A blocked marker lets the session end; it never claims a passing result.
    Both subcommands refuse when no marker exists, so neither can be used to
    start tracking a run.

## Completion gate

The plugin's `Stop` hook looks for this session's active run marker at
`${CLAUDE_PROJECT_DIR}/.mycelium/claude/<session-id>/run.json`. With no marker
the hook exits 0 and the session stops normally.

With an `active` marker the hook will not let the session stop until both of
these succeed:

- `bin/mycelium_lineage.py verify --run-id <run-id>` reports `valid: true` and
  `state: sealed`.
- `evals/verify-cap.py <workspace> <cap-file> --run-id <run-id>` exits 0 and
  prints `Verification: PASS`. The hook reads that field by name, so the
  `Verification-Mode:` line that follows it is not in the way.

When either check fails the hook prints
`{"decision": "block", "reason": ...}`, names the failing check and the exact
command to run, and exits 2. A marker that exists but cannot be read or parsed
also blocks: an unknown run state is not a finished one, and treating a corrupt
file as "no run" made it a silent bypass of the whole gate. The block reason
names the marker path and the `bin/mycelium-mark-run.py blocked` command that
repairs it.

Two cases end the session anyway, and neither is a success: a marker whose
`state` is `blocked`, `reflected`, or `aborted`; and a Stop event with
`stop_hook_active` set, which means the session already continued once after a
block. A session with no marker at all was never gated.

The gate reads `.mycelium/runs/` from the working tree, so it proves the run
state on this machine at that moment. A sealed run is provable from history
only once `.mycelium/` has been committed; new runs stay untracked until then.
