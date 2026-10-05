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

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
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
  # --nosys omits system tables (sqlite_sequence): SQLite auto-populates it
  # from the data INSERTs during restore, so the explicit rows are redundant,
  # and including them made the dump grow by one duplicate row each
  # pull-restore cycle. It also keeps the output stable across sqlite3
  # versions — 3.54 started wrapping sqlite_sequence in writable_schema
  # PRAGMAs, which made an unchanged DB look dirty against an older dump.
  sqlite3 "${DB_PATH}" '.dump --nosys' >"${target}"
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

# Fold this machine's unsynced changes into the incoming dump, rather than
# having to pick one side. Runs the merge BEFORE fast-forwarding the repo:
# if it hits a conflict we leave the repo behind the remote, so a later push
# still refuses rather than clobbering the other machine's work.
keep_local_pull() {
  if [[ ! -f "${DUMP_FILE}" ]]; then
    red "No dump in the data repo yet — nothing to merge against."
    red "Run: ./sync.sh push    (to seed it from this machine)"
    exit 1
  fi

  if ! git rev-parse --verify -q '@{u}' >/dev/null; then
    yellow "No upstream configured — nothing incoming. Local changes kept."
    return 0
  fi

  local behind
  behind=$(git rev-list --count 'HEAD..@{u}')
  if (( behind == 0 )); then
    green "Already up to date. Local changes kept — push when ready."
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    red "python3 not found — needed to merge. Use pull --force to discard local."
    exit 1
  fi

  local tmpdir
  tmpdir=$(mktemp -d)
  # shellcheck disable=SC2064  # expand tmpdir now: it's out of scope at trap time
  trap "rm -rf '${tmpdir}'" EXIT

  # base = what we last synced with, target = what's incoming. The live DB is
  # read directly, and read-only, by the merge.
  sqlite3 "${tmpdir}/base.db" <"${DUMP_FILE}"
  git show '@{u}:timesheet.sql' >"${tmpdir}/incoming.sql"
  sqlite3 "${tmpdir}/merged.db" <"${tmpdir}/incoming.sql"

  yellow "Merging local changes with ${behind} incoming commit(s)…"
  local rc=0
  python3 "${SCRIPT_DIR}/sync_merge.py" \
    --base "${tmpdir}/base.db" \
    --local "${DB_PATH}" \
    --target "${tmpdir}/merged.db" || rc=$?

  if (( rc != 0 )); then
    red ""
    if (( rc == 3 )); then
      red "Both machines changed the same row, so this needs your decision."
    else
      red "Merge failed."
    fi
    red "Nothing has been changed: your local DB is untouched and the data repo"
    red "is still ${behind} commit(s) behind, so push will keep refusing."
    red ""
    red "To take the other machine's version wholesale:"
    red "    ./sync.sh pull --force    (your DB is backed up to .pre-pull first)"
    exit 1
  fi

  # Merge is good — now it's safe to move the repo forward and swap the DB in.
  git pull --ff-only -q
  cp "${DB_PATH}" "${DB_PATH}.pre-pull"
  rm -f "${DB_PATH}" "${DB_PATH}-wal" "${DB_PATH}-shm"
  mv "${tmpdir}/merged.db" "${DB_PATH}"

  green "Pulled ${behind} new commit(s) and merged your local changes in."
  yellow "Previous DB backed up to ${DB_PATH}.pre-pull"
  yellow "Run './sync.sh push' to upload the merged result."
}

cmd_pull() {
  require_data_repo
  local force=0
  local keep_local=0
  for arg in "$@"; do
    case "${arg}" in
      --force)      force=1 ;;
      --keep-local) keep_local=1 ;;
      *) red "Unknown flag: ${arg}"; exit 1 ;;
    esac
  done

  if (( force == 1 && keep_local == 1 )); then
    red "--force and --keep-local do opposite things; pick one."
    exit 1
  fi

  if processes_running; then
    red "timesheets app/API still running. Quit it first."
    red "(replacing the live DB while it's open would corrupt it)"
    exit 1
  fi

  # Settle this before pulling: once the dump changes underneath us, the live
  # DB looks dirty whether or not this machine actually changed anything.
  local was_dirty=0
  if local_dirty; then
    was_dirty=1
  fi

  if (( was_dirty == 1 && force == 0 && keep_local == 0 )); then
    red "Local DB has changes not in the dump."
    red "Run: ./sync.sh push                 (to upload them first)"
    red "  or ./sync.sh pull --keep-local    (to merge them with the remote's)"
    red "  or ./sync.sh pull --force         (to discard local changes)"
    exit 1
  fi

  cd "${DATA_REPO}"
  local before_head after_head
  before_head=$(git rev-parse HEAD)
  git fetch -q

  if (( keep_local == 1 && was_dirty == 1 )); then
    keep_local_pull
    return
  fi

  git pull --ff-only -q
  after_head=$(git rev-parse HEAD)

  if [[ ! -f "${DUMP_FILE}" ]]; then
    yellow "No dump file in repo yet. Nothing to restore."
    return 0
  fi

  # If the live DB already matches the dump (whether or not the dump
  # itself changed in this fetch), there is nothing to restore.
  if [[ -f "${DB_PATH}" ]] && ! local_dirty; then
    if [[ "${before_head}" == "${after_head}" ]]; then
      green "Already up to date. Local DB matches the dump — nothing to do."
    else
      local n
      n=$(git rev-list --count "${before_head}..${after_head}")
      green "Fetched ${n} new commit(s), but timesheet.sql didn't change. Local DB unchanged."
    fi
    return 0
  fi

  # Restore needed: either the dump changed, the live DB diverged, or
  # there's no live DB yet. Back up first if anything is there.
  if [[ -f "${DB_PATH}" ]]; then
    cp "${DB_PATH}" "${DB_PATH}.pre-pull"
  fi
  mkdir -p "$(dirname "${DB_PATH}")"
  rm -f "${DB_PATH}" "${DB_PATH}-wal" "${DB_PATH}-shm"
  sqlite3 "${DB_PATH}" <"${DUMP_FILE}"

  if [[ ! -f "${DB_PATH}.pre-pull" ]]; then
    green "Local DB created from dump (no previous DB found)."
  elif [[ "${before_head}" == "${after_head}" ]]; then
    green "Local DB restored from dump (--force: discarded local changes)."
    yellow "Previous DB backed up to ${DB_PATH}.pre-pull"
  else
    local n
    n=$(git rev-list --count "${before_head}..${after_head}")
    green "Pulled ${n} new commit(s). Local DB restored from updated dump."
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

  # Refuse early if the remote has moved on. Fetch and check BEFORE we dump or
  # commit anything: pushing while behind either needs a force-push (silently
  # clobbering the other machine's work) or leaves a stale local commit that
  # the real push rejects — the mess this whole guard exists to prevent. If we
  # bail here, nothing has been written and the tree is untouched.
  cd "${DATA_REPO}"
  if git rev-parse '@{u}' >/dev/null 2>&1; then
    if git fetch -q 2>/dev/null; then
      local behind
      behind=$(git rev-list --count 'HEAD..@{u}' 2>/dev/null || echo 0)
      if (( behind > 0 )); then
        red "Remote is ${behind} commit(s) ahead — another machine has pushed since"
        red "this one last synced. Nothing was dumped or committed; the tree is clean."
        red ""
        red "If your local changes are NEW work not on the other machine:"
        red "    ./sync.sh pull --keep-local    merges both sides, then push"
        red "If they're stale (this laptop was left open / slept for days):"
        red "    ./sync.sh pull --force         discards local, takes the remote"
        exit 1
      fi
    else
      yellow "Could not fetch (offline?) — pushing against last-known remote."
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

  # (already in DATA_REPO from the behind-check above)
  # Stage first, then compare against the index: a plain `git diff` ignores
  # untracked files, so seeding a fresh data repo looked like "nothing to do".
  git add timesheet.sql
  if git diff --cached --quiet; then
    green "No changes to push."
    return 0
  fi

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
  pull --keep-local             Merge local DB changes with the incoming dump
  pull --force                  Discard local DB changes when pulling
  push --force-with-running     Push even if app/API processes are running
EOF
      exit 1
      ;;
  esac
}

main "$@"
