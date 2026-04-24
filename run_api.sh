#!/usr/bin/env bash
# Launch the timesheets HTTP API on localhost.
#
# Run from the repo root with the project venv either activated or available
# at .venv/. The API binds to 127.0.0.1 only - external access is intended via
# an SSH RemoteForward, never a network bind.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
HOST="${TIMESHEETS_API_HOST:-127.0.0.1}"
PORT="${TIMESHEETS_API_PORT:-8765}"

exec "${PYTHON}" -m uvicorn api:app --host "${HOST}" --port "${PORT}" "$@"
