#!/usr/bin/env bash
# Build the interface and run HunterAI on http://127.0.0.1:${HUNTERAI_PORT:-8790}: the app in
# the foreground, the ingest worker next to it.
set -euo pipefail
cd "$(dirname "$0")/.."
(cd backend && uv sync)
(cd frontend && bun install --frozen-lockfile && bun run build)
# The search and vector containers are left running on exit; stop them with
# scripts/searxng.sh stop and docker stop hunterai-qdrant.
scripts/searxng.sh start || echo "web_search is unavailable until SearXNG runs"
scripts/qdrant.sh || echo "the knowledge base is unavailable until Qdrant runs"

(cd backend && exec uv run python -m hunterai.worker) &
worker=$!
stop_worker() {
  # The worker reads its stop flag between documents and embed batches; a killed worker's
  # job resumes on the next start.
  kill -TERM "$worker" 2>/dev/null || return 0
  for _ in $(seq 1 150); do kill -0 "$worker" 2>/dev/null || return 0; sleep 0.1; done
  kill -KILL "$worker" 2>/dev/null || true
}
trap stop_worker EXIT INT TERM
echo "HunterAI: http://127.0.0.1:${HUNTERAI_PORT:-8790}"
(cd backend && uv run python -m hunterai)
