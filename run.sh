#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -f .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/installed.flag ]; then
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/installed.flag
fi
.venv/bin/python -m streamlit run app.py
