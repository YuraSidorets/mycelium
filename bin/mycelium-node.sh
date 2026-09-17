#!/bin/sh

tracked=false
for argument in "$@"; do
  case "$argument" in
    --) break ;;
    --run-id|--run-id=*) tracked=true; break ;;
  esac
done

if [ "$tracked" = false ]; then
  untracked_python3_path="$(command -v python3 2>/dev/null)"
  untracked_python=python3
  case "$(printf '%s' "$untracked_python3_path" | tr '[:upper:]' '[:lower:]')" in
    *windowsapps*) untracked_python=python ;;
  esac
  exec "$untracked_python" "$(dirname "$0")/mycelium.py" node "$@"
fi

export PYTHONDONTWRITEBYTECODE=1

for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$candidate" -B "$(dirname "$0")/mycelium.py" node "$@"
  fi
done

printf '%s\n' 'Mycelium tracked node writes require Python 3.9 or newer. Tried python3 and python; no node or flow was written.' >&2
exit 1
