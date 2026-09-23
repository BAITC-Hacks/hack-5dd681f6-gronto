#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -f .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m ensurepip --upgrade || {
    echo 'pip could not be restored. Install Python with venv/ensurepip support.' >&2
    exit 1
  }
fi
if [ ! -f .venv/installed.flag ]; then
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/installed.flag
fi
.venv/bin/python -m streamlit run app.py
