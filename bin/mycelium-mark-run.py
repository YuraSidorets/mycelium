#!/usr/bin/env python3
"""Write this host session's Mycelium active run marker.

The `Stop` hook in `claude/hooks/mycelium_hook.py` gates a session on
`${CLAUDE_PROJECT_DIR or cwd}/.mycelium/claude/<session-id>/run.json`. This
helper is the only thing that should write that file. It replaces the inline
`python -c` one-liners the run skill used to carry: those were long enough to
be retyped wrong, picked their target session by modification time, and had no
way to refuse a `done` on a run that never began.

Standard library only, Python 3.9 or newer, no Mycelium imports. The marker is
replaced atomically -- a temporary file in the same directory, then
`os.replace` -- so a Stop hook running at the same moment reads either the old
marker or the new one and never a half-written one. That matters because an
unreadable marker now blocks the stop instead of being ignored.

The field names are the snake_case spellings the gate reads: `run_id`, `goal`,
`cap_file`, `cap_node_path`, and `state`, plus an `updated` ISO-8601 UTC
timestamp this helper maintains. `cap_file` is the CAP spore file the external
verifier reads, not the CAP node.

Subcommands:

* `begin --session-id <id> --run-id <id> --goal <text> --cap-file <path>
  [--cap-node-path <path>]` writes a marker in state `active`.
* `done --session-id <id>` records a sealed and verified run.
* `blocked --session-id <id> --reason <text>` records a run that could not
  reach its goal. A blocked marker lets the session end; it never claims a
  passing result.

`done` and `blocked` refuse when no marker exists, because neither is a way to
start tracking a run. `blocked` is the repair path the Stop hook prints for an
unreadable marker, so it is the one subcommand that may overwrite a marker it
could not parse; `done` refuses that case and says to use `blocked`.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT_PARTS = (".mycelium", "claude")
RUN_MARKER_NAME = "run.json"
# The same expression `claude/hooks/mycelium_hook.py` uses. A session ID
# becomes a directory name, so it may not be a path.
SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

EXIT_OK = 0
EXIT_REFUSED = 2

STATE_BY_COMMAND = {"begin": "active", "done": "done", "blocked": "blocked"}


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_directory():
    """The target project root. `CLAUDE_PROJECT_DIR` is authoritative, the
    working directory is the fallback. Same order as the hook."""
    candidate = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(candidate).resolve() if candidate else Path.cwd().resolve()


def marker_path(session_id):
    """This session's marker, proven to stay inside the project's
    `.mycelium/claude` directory."""
    if not SAFE_SESSION_ID.match(session_id or ""):
        raise ValueError(
            f"unusable --session-id {session_id!r}; it becomes a directory name, "
            "so it must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
        )
    state_root = project_directory().joinpath(*STATE_ROOT_PARTS)
    resolved = (state_root / session_id).resolve()
    try:
        resolved.relative_to(state_root.resolve(strict=False))
    except ValueError:
        raise ValueError(
            "session state path escaped the project .mycelium directory; "
            "nothing was written."
        )
    return resolved / RUN_MARKER_NAME


def read_marker(marker):
    """The marker as a dict, or None when it cannot be read or parsed."""
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def write_marker(marker, record):
    """Replace the marker atomically. A reader sees the old file or the new
    one, never a truncated one."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=str(marker.parent),
        prefix=f".{RUN_MARKER_NAME}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(marker))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return marker


def command_begin(args, marker):
    record = {
        "run_id": args.run_id,
        "goal": args.goal,
        "cap_file": args.cap_file,
        "cap_node_path": args.cap_node_path,
        "state": STATE_BY_COMMAND["begin"],
        "updated": timestamp(),
    }
    write_marker(marker, record)
    return EXIT_OK


def command_resolve(args, marker):
    """`done` and `blocked`. Neither starts tracking a run, so both refuse
    when there is no marker to update."""
    if not marker.is_file():
        print(
            f"no active run marker at {marker}; run "
            f'"mycelium-mark-run begin --session-id {args.session_id} ..." first. '
            "Nothing was written.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    record = read_marker(marker)
    if record is None:
        if args.command != "blocked":
            print(
                f"the marker at {marker} exists but is unreadable, so it cannot "
                f'be updated to "{STATE_BY_COMMAND[args.command]}". Record the run '
                "as blocked instead, which does not depend on the old contents: "
                f'mycelium-mark-run blocked --session-id {args.session_id} '
                '--reason "<why the run is blocked>". Nothing was written.',
                file=sys.stderr,
            )
            return EXIT_REFUSED
        # `blocked` is the repair path the Stop hook prints for an unreadable
        # marker, so it has to work without the old contents.
        print(
            f"the marker at {marker} was unreadable; recording a blocked run "
            "without its previous fields.",
            file=sys.stderr,
        )
        record = {}
    record["state"] = STATE_BY_COMMAND[args.command]
    record["updated"] = timestamp()
    if args.command == "blocked":
        record["reason"] = args.reason
    write_marker(marker, record)
    return EXIT_OK


def parser():
    root = argparse.ArgumentParser(
        prog="mycelium-mark-run",
        description="Write this host session's Mycelium active run marker.",
    )
    commands = root.add_subparsers(dest="command", required=True)

    begin_help = "write an active run marker for a run that has just begun"
    begin = commands.add_parser("begin", help=begin_help, description=begin_help)
    begin.add_argument("--session-id", required=True)
    begin.add_argument("--run-id", required=True)
    begin.add_argument("--goal", required=True)
    begin.add_argument(
        "--cap-file",
        required=True,
        help="the CAP spore file the external verifier reads, not the CAP node",
    )
    begin.add_argument("--cap-node-path", default="")
    begin.set_defaults(run=command_begin)

    done_help = "record the run as done, once it is sealed and externally verified"
    done = commands.add_parser("done", help=done_help, description=done_help)
    done.add_argument("--session-id", required=True)
    done.set_defaults(run=command_resolve)

    blocked_help = "record the run as blocked; this never claims a passing result"
    blocked = commands.add_parser("blocked", help=blocked_help, description=blocked_help)
    blocked.add_argument("--session-id", required=True)
    blocked.add_argument("--reason", required=True)
    blocked.set_defaults(run=command_resolve)

    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        marker = marker_path(args.session_id)
    except ValueError as error:
        print(f"mycelium-mark-run: {error}", file=sys.stderr)
        return EXIT_REFUSED
    status = args.run(args, marker)
    if status == EXIT_OK:
        print(marker.as_posix())
    return status


if __name__ == "__main__":
    raise SystemExit(main())
