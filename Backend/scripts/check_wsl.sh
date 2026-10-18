#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [ ! -x .venv-wsl/bin/python ]; then
  python3 -m venv .venv-wsl
fi
.venv-wsl/bin/python -m pip install -r requirements.txt
.venv-wsl/bin/python -B -m unittest discover -s tests -v
