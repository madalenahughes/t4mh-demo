#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${T4MH_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    printf 'Python environment missing. Run the installer, or set T4MH_PYTHON.\n' >&2
    exit 1
fi
exec "$PYTHON" -u "$ROOT/tools/demo.py" "$@"
