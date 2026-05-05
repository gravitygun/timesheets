#!/usr/bin/env bash
# Launch the timesheets HTTP API on localhost.
#
# Run from the repo root with the project venv either activated or available
# at .venv/. The API binds to 127.0.0.1 only - external access is intended via
# an SSH RemoteForward, never a network bind.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .venv/bin/activate ]]; then
  printf '\033[31mNo virtualenv found at %s/.venv\033[0m\n' "$(pwd)" >&2
  cat >&2 <<EOF

Set one up first:

  cd $(pwd)
  python3.12 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt

(See README.md for full setup instructions.)
EOF
  exit 1
fi

# shellcheck source=/dev/null
source .venv/bin/activate

HOST="${TIMESHEETS_API_HOST:-127.0.0.1}"
PORT="${TIMESHEETS_API_PORT:-8765}"

exec python -m uvicorn api:app \
  --host "${HOST}" --port "${PORT}" \
  --log-config api_logging.json \
  "$@"
