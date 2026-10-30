#!/usr/bin/env bash
#
# Package jira_sync for carrying across to HS.
#
# Writes jira_sync-VERSION.zip into OUT_DIR (default ~/jira_sync-transfer):
# the tool, its docs and any exports given, with SHA256SUMS so the far side
# can check nothing changed on the way.
#
# Usage: bundle.sh [-o OUT_DIR] [EXPORT.json ...]

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SRC_DIR
readonly FILES=(
  sync_worklogs.py
  mock_jira.py
  config.example.ini
  requirements.txt
  README.md
)

usage() {
  echo "Usage: $(basename "$0") [-o OUT_DIR] [EXPORT.json ...]" >&2
  exit 2
}

main() {
  local out_dir="${HOME}/jira_sync-transfer"
  local opt
  while getopts "o:h" opt; do
    case "${opt}" in
      o) out_dir="${OPTARG}" ;;
      *) usage ;;
    esac
  done
  shift $((OPTIND - 1))

  local version
  version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' \
    "${SRC_DIR}/sync_worklogs.py")"
  if [[ -z "${version}" ]]; then
    echo "Cannot find __version__ in sync_worklogs.py" >&2
    exit 1
  fi
  local name="jira_sync-${version}"

  local stage
  stage="$(mktemp -d)"
  # shellcheck disable=SC2064  # expand now: stage is local
  trap "rm -rf '${stage}'" EXIT
  mkdir -p "${stage}/${name}"

  local f
  for f in "${FILES[@]}"; do
    cp "${SRC_DIR}/${f}" "${stage}/${name}/"
  done
  local included=("${FILES[@]}")
  local export_file
  for export_file in "$@"; do
    if [[ ! -f "${export_file}" ]]; then
      echo "No such export: ${export_file}" >&2
      exit 1
    fi
    cp "${export_file}" "${stage}/${name}/"
    included+=("$(basename "${export_file}")")
  done
  (cd "${stage}/${name}" && shasum -a 256 "${included[@]}" > SHA256SUMS)

  mkdir -p "${out_dir}"
  local zip_path="${out_dir}/${name}.zip"
  rm -f "${zip_path}"
  (cd "${stage}" && zip -q -X -r "${zip_path}" "${name}")

  echo "Zip:       ${zip_path}"
  echo "Contents:  ${included[*]} SHA256SUMS"
}

main "$@"
