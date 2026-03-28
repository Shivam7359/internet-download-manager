#!/usr/bin/env bash
set -euo pipefail

echo "[build_linux] Creating virtual environment if needed"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

echo "[build_linux] Activating environment"
source .venv/bin/activate

echo "[build_linux] Installing dependencies"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo "[build_linux] Running tests"
pytest tests/ -v

echo "[build_linux] Building executable"
pyinstaller idm.spec

echo "[build_linux] Build complete"