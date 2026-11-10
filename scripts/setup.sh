#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${HERMES_SELL_PYTHON:-python3.14}"
if ! command -v "$python_bin" >/dev/null && [[ -x /opt/homebrew/bin/python3.14 ]]; then
  python_bin=/opt/homebrew/bin/python3.14
fi
"$python_bin" -c 'import sys; assert sys.version_info[:2] == (3, 14), "Python 3.14 is required"'
"$python_bin" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
.venv/bin/python -m pip install --disable-pip-version-check --no-deps -e .
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build
