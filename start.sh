#!/usr/bin/env bash
# Port Analyzer launcher (Mac / Linux)
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  echo "Setting up for first use. This takes a minute or two..."
  "$PY" -m venv .venv
fi

if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing the libraries the app needs..."
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi

exec .venv/bin/python app.py
