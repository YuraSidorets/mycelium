#!/bin/sh

export PYTHONDONTWRITEBYTECODE=1

for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$candidate" -B "$(dirname "$0")/mycelium-mark-run.py" "$@"
  fi
done

printf '%s\n' 'Mycelium run markers require Python 3.9 or newer. Tried python3 and python; no marker was written.' >&2
exit 1
