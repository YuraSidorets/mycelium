#!/usr/bin/env python3
"""Mycelium hook adapter for Claude Code.

Two events are wired in `claude/hooks/hooks.json`:

* `UserPromptExpansion`, matched on the typed `mycelium:run` command, binds the
  Claude session to a state directory under
  `${CLAUDE_PROJECT_DIR}/.mycelium/claude/<session-id>/`.
* `Stop` looks for that session's active run marker. With no marker it exits 0
  at once and the session stops normally. With a marker it requires sealed
  lineage and `evals/verify-cap.py` success before the session may stop.

This implements the completion gate of
`docs/superpowers/plans/2026-08-27-claude-code-port-plan.md`.

Standard library only, Python 3.9 or newer, no Mycelium imports. A hook must
not be the reason a session cannot end *by accident*, so every failure that is
neither a control-plane violation nor a failed completion check exits 0.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_VERSION = "1"
STATE_ROOT_PARTS = (".mycelium", "claude")
RUN_MARKER_NAME = "run.json"
SESSION_RECORD_NAME = "session.json"
SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

EXIT_OK = 0
EXIT_BLOCK = 2
EXIT_CONFIG = 3

# A run recorded in one of these states has already produced its durable
# outcome, so the session may stop. None of them is a success claim.
STOPPABLE_RUN_STATES = frozenset(
    {"blocked", "reflected", "reflection", "aborted", "done", "abandoned"}
)
VERIFIER_TIMEOUT_SECONDS = 90


class HookError(Exception):
    """A control-plane violation the hook must report instead of ignoring."""


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_payload(stream):
    """Read the event JSON object from stdin. Missing or unreadable input is an
    empty payload, not a crash: a hook that dies on stdin noise would block
    sessions that have nothing to do with Mycelium."""
    try:
        raw = stream.read()
    except OSError:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def project_directory(payload):
    """The target project root. `CLAUDE_PROJECT_DIR` is authoritative; the
    event's `cwd` is the documented fallback."""
    for candidate in (os.environ.get("CLAUDE_PROJECT_DIR"), payload.get("cwd")):
        if candidate:
            return Path(candidate).resolve()
    return Path.cwd().resolve()


def validate_session_id(session_id):
    """A session ID becomes a directory name, so it may not be a path."""
    if not isinstance(session_id, str) or not SAFE_SESSION_ID.match(session_id):
        raise HookError("Mycelium hook: unusable session_id; no state was read or written.")
    return session_id


def session_directory(project, session_id):
    """Resolve this session's state directory and prove it stays inside the
    target project's `.mycelium/claude`. Never inside the cached plugin."""
    state_root = project.joinpath(*STATE_ROOT_PARTS)
    resolved = (state_root / validate_session_id(session_id)).resolve()
    try:
        resolved.relative_to(state_root.resolve(strict=False))
    except ValueError:
        raise HookError(
            "Mycelium hook: session state path escaped the project .mycelium directory; nothing was written."
        )
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        plugin = Path(plugin_root).resolve()
        if resolved == plugin or plugin in resolved.parents:
            raise HookError(
                "Mycelium hook: refusing to write Mycelium state inside the plugin directory."
            )
    return resolved


def find_active_run_marker(directory):
    """The active run marker for a bound session, or None when the session has
    no tracked run. `None` is the normal case for every session that never
    typed `/mycelium:run`."""
    marker = directory / RUN_MARKER_NAME
    return marker if marker.is_file() else None


def note(message):
    """Say something on stderr without blocking. stderr on a zero exit is a
    note to the transcript, not a veto."""
    print(f"Mycelium hook: {message}", file=sys.stderr)


def shared_root():
    """The directory that holds `bin/` and `evals/`. The hook lives at
    `<root>/claude/hooks/mycelium_hook.py`, in the repository and in the cached
    plugin alike."""
    return Path(__file__).resolve().parents[2]


def read_marker(marker):
    """The marker as a dict, or None when it cannot be read or parsed.

    The absence of a marker file and the presence of an unreadable one are two
    different facts, and only the caller can tell them apart, so this returns
    None for both and `gate_active_run` re-tests `is_file()`."""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def mark_run_command(marker, payload):
    """The exact `bin/mycelium-mark-run.py blocked` command for this session.

    The session ID is the marker's own parent directory name, so it is correct
    even when the Stop payload is the thing that could not be trusted."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = marker.parent.name
    return (
        f'python bin/mycelium-mark-run.py blocked --session-id "{session_id}" '
        '--reason "<why the run is blocked>"'
    )


def marker_field(record, *names):
    """First non-empty string among `names`. The marker is written by the run
    skill, so both `run_id` and `runId` spellings are accepted."""
    for name in names:
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def output_field(text, name):
    """Read a `Name: value` field out of tool output by field name.

    `evals/verify-cap.py` prints `Verification-Mode:` directly after
    `Verification: PASS` and `Verified nodes:` after that, so no consumer may
    read any of those lines by position. Returns None when the field is absent.
    """
    for line in str(text).splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == name:
            return value.strip()
    return None


def run_tool(script, arguments, cwd):
    return subprocess.run(
        [sys.executable, "-B", str(script), *arguments],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=VERIFIER_TIMEOUT_SECONDS,
    )


def block(reason):
    return {"decision": "block", "reason": reason}


def gate_active_run(marker, payload):
    """Decide whether a session with an active run may stop.

    A tracked run may stop only when `mycelium_lineage.py verify` reports
    `state: sealed` with `valid: true` and `evals/verify-cap.py` exits 0 with a
    `Verification: PASS` field. A run recorded as blocked or reflected may stop
    without any success claim. A marker that exists but cannot be read or
    parsed blocks, because an unknown run state is not a finished one.
    `stop_hook_active` always allows the stop, so a Stop hook cannot loop
    forever.

    The verifier's output is read by field name, never by position:
    `evals/verify-cap.py` prints a `Verification-Mode:` line directly after
    `Verification: PASS`.

    Returns None to let the session stop, or a block decision dict.
    """
    payload = payload if isinstance(payload, dict) else {}

    if payload.get("stop_hook_active"):
        note(
            "stop_hook_active is set, so this session already continued once "
            "after a block. Allowing the stop; the Mycelium run is NOT verified."
        )
        return None

    record = read_marker(marker)
    if record is None:
        if not marker.is_file():
            note(
                f"the active run marker {marker} is missing, so no run could be "
                "checked. Allowing the stop; nothing was verified."
            )
            return None
        # A marker that exists but cannot be read is not evidence that no run
        # is active; it is evidence that the run state is unknown. Allowing the
        # stop here turned a corrupt file into a silent bypass of the whole
        # gate, so an unreadable marker blocks and names its own repair.
        return block(
            f"The Mycelium active run marker {marker} exists but is unreadable "
            "or is not a JSON object, so this session's run state cannot be "
            "proven and the session cannot end. Failing check: run marker "
            "readable. Repair the marker, or record the run as blocked with: "
            f"{mark_run_command(marker, payload)}"
        )

    run_id = marker_field(record, "run_id", "runId")
    state = marker_field(record, "state", "runState").lower()
    if state in STOPPABLE_RUN_STATES:
        note(
            f"Mycelium run {run_id or '<unnamed>'} is recorded as '{state}'. "
            "Allowing the stop. This is not a success: no verification was claimed."
        )
        return None

    if not run_id:
        note(
            f"the active run marker {marker} names no run_id, so no run could be "
            "checked. Allowing the stop; nothing was verified."
        )
        return None

    project = project_directory(payload)
    root = shared_root()
    lineage = root / "bin" / "mycelium_lineage.py"
    verifier = root / "evals" / "verify-cap.py"
    if not lineage.is_file() or not verifier.is_file():
        note(
            "the lineage verifier or the CAP verifier is not present next to this "
            f"hook (looked under {root}). Allowing the stop; nothing was verified."
        )
        return None

    try:
        lineage_result = run_tool(
            lineage, ["--root", str(project), "verify", "--run-id", run_id], project
        )
    except (OSError, subprocess.SubprocessError) as error:
        note(f"the lineage verifier could not be run ({error}). Allowing the stop.")
        return None

    lineage_command = (
        f'python bin/mycelium_lineage.py --root "{project}" verify --run-id "{run_id}"'
    )
    try:
        report = json.loads(lineage_result.stdout or "{}")
    except ValueError:
        report = {}
    if not isinstance(report, dict):
        report = {}
    # `verify` exits 1 for an invalid-but-readable run, so a missing `valid`
    # key -- not the exit code -- is what "no report at all" looks like.
    if "valid" not in report:
        detail = (lineage_result.stderr or lineage_result.stdout or "").strip()
        return block(
            f"Mycelium run {run_id} could not be verified ({detail.splitlines()[0] if detail else 'no report'}), "
            f"so this session cannot end. Failing check: mycelium_lineage verify. "
            f"Run: {lineage_command}"
        )
    manifest_state = report.get("manifest_state") or report.get("state")
    if manifest_state != "sealed":
        return block(
            f"Mycelium run {run_id} is in state {manifest_state or 'unknown'!r}, "
            "not 'sealed', so this session cannot end. Failing check: run state. "
            "Seal it with: "
            f'python bin/mycelium_lineage.py --root "{project}" seal --run-id "{run_id}"'
        )
    if not report.get("valid"):
        diagnostics = report.get("diagnostics") or []
        first = ""
        if isinstance(diagnostics, list) and diagnostics:
            item = diagnostics[0]
            first = item.get("message", str(item)) if isinstance(item, dict) else str(item)
        return block(
            f"Mycelium run {run_id} is sealed but not valid ({first or 'see diagnostics'}), "
            f"so this session cannot end. Failing check: mycelium_lineage verify. "
            f"Run: {lineage_command}"
        )

    cap_file = marker_field(
        record, "cap_file", "capFile", "cap_node_path", "capNodePath"
    )
    cap_command = (
        f'python evals/verify-cap.py "{project}" "{cap_file or "<cap-file>"}" '
        f'--run-id "{run_id}"'
    )
    if not cap_file:
        return block(
            f"Mycelium run {run_id} is sealed but its marker {marker} names no CAP "
            "file, so verification cannot be proven and this session cannot end. "
            "Failing check: cap_file in the run marker. Add the CAP spore path to "
            f"the marker, then run: {cap_command}"
        )

    try:
        cap_result = run_tool(
            verifier, [str(project), cap_file, "--run-id", run_id], project
        )
    except (OSError, subprocess.SubprocessError) as error:
        note(f"the CAP verifier could not be run ({error}). Allowing the stop.")
        return None

    verification = output_field(cap_result.stdout, "Verification")
    if cap_result.returncode != 0 or verification != "PASS":
        detail = output_field(cap_result.stdout, "Reason") or (
            verification or "no Verification field"
        )
        return block(
            f"Mycelium run {run_id} is sealed but external verification did not "
            f"pass ({detail}), so this session cannot end. Failing check: "
            f"evals/verify-cap.py. Run: {cap_command}"
        )
    return None


def bind_session(payload):
    """Session binding for an explicit `/mycelium:run` expansion. Records which
    project directory and Claude session this run belongs to. It deliberately
    does not create the run marker: the run ID, goal, and expected CAP path are
    known only after the session begins the tracked run."""
    project = project_directory(payload)
    directory = session_directory(project, payload.get("session_id"))
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "hookVersion": HOOK_VERSION,
        "sessionId": payload.get("session_id"),
        "projectDirectory": str(project),
        "boundAt": timestamp(),
        "activation": "mycelium:run",
    }
    (directory / SESSION_RECORD_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return directory


def handle_activation(payload):
    bind_session(payload)
    return EXIT_OK


def handle_stop(payload):
    """No marker means nothing to gate: exit 0 at once."""
    project = project_directory(payload)
    try:
        directory = session_directory(project, payload.get("session_id"))
    except HookError:
        # An unusable session ID on Stop cannot be a Mycelium run, because
        # binding would have failed too. Let the session stop.
        return EXIT_OK
    marker = find_active_run_marker(directory)
    if marker is None:
        return EXIT_OK

    decision = gate_active_run(marker, payload)
    if decision is None:
        return EXIT_OK
    # Both host conventions at once: the JSON decision on stdout and the reason
    # on stderr with exit 2. Either one blocks the stop; together they cannot be
    # missed.
    print(json.dumps(decision))
    print(decision["reason"], file=sys.stderr)
    return EXIT_BLOCK


HANDLERS = {
    "UserPromptExpansion": handle_activation,
    "UserPromptSubmit": handle_activation,
    "Stop": handle_stop,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mycelium hook adapter for Claude Code.")
    parser.add_argument("--event", default="")
    # argparse exits 2 on a usage error, and exit 2 blocks the session; a stray
    # argument must never be the reason a session cannot end.
    arguments, _unknown = parser.parse_known_args(argv)

    payload = read_payload(sys.stdin)
    event = payload.get("hook_event_name") or arguments.event
    handler = HANDLERS.get(event)
    if handler is None:
        return EXIT_OK

    try:
        return handler(payload)
    except HookError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CONFIG
    except OSError as error:
        print(f"Mycelium hook: {error}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
