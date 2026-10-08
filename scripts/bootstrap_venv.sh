#!/usr/bin/env bash
# Bootstrap the project-local venv WITHOUT apt (Debian PEP-668 / no
# python3-venv): --without-pip + get-pip, then install the package with the
# [tui,test] extras. Everything stays inside /home/alex/alx_fancontrol.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv --without-pip .venv
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o scripts/get-pip.py
  .venv/bin/python scripts/get-pip.py
fi
.venv/bin/pip install -q -e ".[tui,test]"
.venv/bin/python -c "import textual; print('textual', textual.__version__)"   # TUI GATE
