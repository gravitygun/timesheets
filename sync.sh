#!/bin/bash
#
# sync.sh — pull/push the timesheet DB via the timesheets-data git repo.
#
# Usage:
#   ./sync.sh pull           Restore local DB from the remote dump
#   ./sync.sh push           Dump local DB and push it to the remote
#   ./sync.sh status         Show whether local is in sync with the dump
#
# The local DB is a working copy. The dump in $DATA_REPO/timesheet.sql is
# the source of truth shared between machines.

set -euo pipefail

DATA_REPO="${HOME}/.timesheets-data"
DUMP_FILE="${DATA_REPO}/timesheet.sql"
DB_PATH="${TIMESHEET_DB:-${HOME}/Library/Application Support/timesheets/timesheet.db}"

red() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

require_data_repo() {
  if [[ ! -d "${DATA_REPO}/.git" ]]; then
    red "Data repo not found at ${DATA_REPO}"
    red "Run: git clone <your-private-data-repo-url> ${DATA_REPO}"
    exit 1
  fi
}

processes_running() {
  pgrep -f 'python.*app\.py|run_api\.sh|uvicorn.*api:' >/dev/null 2>&1
}

dump_db_to() {
  local target=$1
  if [[ ! -f "${DB_PATH}" ]]; then
    red "DB not found at ${DB_PATH}"
    exit 1
  fi
  # Strip sqlite_sequence inserts: SQLite auto-populates that table from the
  # data INSERTs during restore, so the explicit rows are redundant. Without
  # this the dump grows by one duplicate row each pull-restore cycle.
  sqlite3 "${DB_PATH}" .dump | grep -v '^INSERT INTO sqlite_sequence ' >"${target}"
}

local_dirty() {
  # Returns 0 (true) if the live DB has changes not yet captured in DUMP_FILE.
  [[ -f "${DB_PATH}" ]] || return 1
  [[ -f "${DUMP_FILE}" ]] || return 0
  local tmp
  tmp=$(mktemp)
  dump_db_to "${tmp}"
  if cmp -s "${tmp}" "${DUMP_FILE}"; then
    rm -f "${tmp}"
    return 1
  fi
  rm -f "${tmp}"
  return 0
}

cmd_pull() {
  require_data_repo
  local force=${1:-}

  if processes_running; then
    red "timesheets app/API still running. Quit it first."
    red "(replacing the live DB while it's open would corrupt it)"
    exit 1
  fi

  if [[ "${force}" != "--force" ]] && local_dirty; then
    red "Local DB has changes not in the dump."
    red "Run: ./sync.sh push    (to upload them first)"
    red "  or ./sync.sh pull --force    (to discard local changes)"
    exit 1
  fi

  cd "${DATA_REPO}"
  git fetch -q
  git pull --ff-only

  if [[ ! -f "${DUMP_FILE}" ]]; then
    yellow "No dump file in repo yet. Nothing to restore."
    return 0
  fi

  # Backup current DB before overwriting
  if [[ -f "${DB_PATH}" ]]; then
    cp "${DB_PATH}" "${DB_PATH}.pre-pull"
  fi

  # Replace the live DB
  mkdir -p "$(dirname "${DB_PATH}")"
  rm -f "${DB_PATH}" "${DB_PATH}-wal" "${DB_PATH}-shm"
  sqlite3 "${DB_PATH}" <"${DUMP_FILE}"

  green "Pulled. Local DB restored from dump."
  if [[ -f "${DB_PATH}.pre-pull" ]]; then
    yellow "Previous DB backed up to ${DB_PATH}.pre-pull"
  fi
}

cmd_push() {
  require_data_repo
  local force_running=0
  local allow_shrink=0
  for arg in "$@"; do
    case "${arg}" in
      --force-with-running) force_running=1 ;;
      --allow-shrink)       allow_shrink=1 ;;
      *) red "Unknown flag: ${arg}"; exit 1 ;;
    esac
  done

  if processes_running; then
    if [[ "${force_running}" == "1" ]]; then
      yellow "Pushing while processes are running (forced)."
    else
      red "timesheets app/API still running. Quit it first."
      red "  or run with --force-with-running (risk: dump may be mid-edit)"
      exit 1
    fi
  fi

  # Dump to a staging path first so we can sanity-check before overwriting.
  local staged
  staged=$(mktemp)
  dump_db_to "${staged}"

  if [[ -f "${DUMP_FILE}" && "${allow_shrink}" == "0" ]]; then
    local new_lines existing_lines
    new_lines=$(wc -l <"${staged}")
    existing_lines=$(wc -l <"${DUMP_FILE}")
    # Refuse if the new dump is less than half the size of what's there.
    # Catches accidents like dumping a stale/empty DB after a TIMESHEET_DB
    # mix-up. Use --allow-shrink to override (e.g. legitimate mass-deletion).
    if (( new_lines * 2 < existing_lines )); then
      red "Refusing to push: new dump (${new_lines} lines) is less than half"
      red "the existing dump (${existing_lines} lines) — looks like a wrong DB."
      red "Live DB: ${DB_PATH}"
      red "Override with: ./sync.sh push --allow-shrink"
      rm -f "${staged}"
      exit 1
    fi
  fi
  mv "${staged}" "${DUMP_FILE}"

  cd "${DATA_REPO}"
  if git diff --quiet; then
    green "No changes to push."
    return 0
  fi

  git add timesheet.sql
  git commit -q -m "session $(hostname -s) $(date '+%Y-%m-%d %H:%M')"
  git push -q
  green "Pushed."
}

cmd_status() {
  require_data_repo
  cd "${DATA_REPO}"
  git fetch -q 2>/dev/null || yellow "(could not fetch — offline?)"

  echo "Data repo: ${DATA_REPO}"
  echo "Live DB:   ${DB_PATH}"
  echo

  if local_dirty; then
    yellow "Local DB: has unsynced changes (push to save them)"
  else
    green "Local DB: in sync with dump"
  fi

  local ahead behind upstream_ok=1
  ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null) || upstream_ok=0
  behind=$(git rev-list --count 'HEAD..@{u}' 2>/dev/null) || upstream_ok=0
  if [[ "${upstream_ok}" == "0" ]]; then
    yellow "Remote:   no upstream configured"
    yellow "          set with: git -C ${DATA_REPO} remote add origin <url>"
    yellow "                    git -C ${DATA_REPO} push -u origin main"
  elif [[ "${ahead}" -gt 0 ]]; then
    yellow "Remote:   local is ${ahead} commit(s) ahead — push needed"
  elif [[ "${behind}" -gt 0 ]]; then
    yellow "Remote:   local is ${behind} commit(s) behind — pull needed"
  else
    green "Remote:   up to date"
  fi

  echo
  echo "Last 3 commits:"
  git --no-pager log --oneline -3 || true
}

main() {
  local cmd=${1:-}
  shift || true
  case "${cmd}" in
    pull) cmd_pull "$@" ;;
    push) cmd_push "$@" ;;
    status) cmd_status ;;
    *)
      cat <<EOF
Usage: $(basename "$0") <pull|push|status>

  pull       Restore local DB from the dump in ${DATA_REPO}
  push       Dump local DB and push to ${DATA_REPO}
  status     Show local + remote sync state

Flags:
  pull --force                  Discard local DB changes when pulling
  push --force-with-running     Push even if app/API processes are running
EOF
      exit 1
      ;;
  esac
}

main "$@"
