#!/usr/bin/env bash
# Bring this folder to the newest HunterAI and start it again:
#     scripts/update.sh
# It stops a running HunterAI, fetches the new code, keeps what you changed in the folder, and
# runs scripts/start.sh. `scripts/update.sh --help` lists its switches; the README has the rest
# under "Update".
set -euo pipefail

# The update replaces this file while it runs, and bash reads a script as it goes: everything
# is inside one function, which bash has read in full before the first command runs.
main() {
  cd "$(dirname "$0")/.."
  local code=0
  (cd backend && uv run python -m hunterai.update "$@") || code=$?
  if [ "$code" -eq 10 ]; then      # the folder was updated: start what is there now
    exec scripts/start.sh
  fi
  exit "$code"
}
main "$@"
