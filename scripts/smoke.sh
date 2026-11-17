#!/usr/bin/env bash
# Checks against the real services of this install: Ollama and the local model, Qdrant, SearXNG, your proxy,
# the model providers, and a running HunterAI. Nothing is sent to a target and nothing of yours is changed.
#   scripts/smoke.sh            every check that costs nothing
#   scripts/smoke.sh --deep     and a long chat answered by the local model (minutes on a slow machine)
#   scripts/smoke.sh --calls    and one short request to each cloud provider that is ready (a few tokens)
#   scripts/smoke.sh --only local,proxy
# Exit code 0: nothing failed. 1: a check failed, and its line says what to do.
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uv run python -m hunterai.smoke "$@"
