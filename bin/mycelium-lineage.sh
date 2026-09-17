#!/bin/sh
set -eu

export PYTHONDONTWRITEBYTECODE=1
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
    then
        exec "$candidate" "$SCRIPT_DIR/mycelium_lineage.py" "$@"
    fi
done

printf '%s\n' 'Mycelium tracked lineage requires Python 3.9 or newer. Tried python3 and python.' >&2
exit 1
