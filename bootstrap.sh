#!/usr/bin/env bash
# Optional setup on a NEW machine. The presentation installer reuses the working Pi environment.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ -e .venv || -L .venv ]]; then
    echo '.venv already exists. Nothing installed or changed.' >&2
    exit 1
fi
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
echo 'Python environment created. Native VLC must also be installed on the operating system.'
