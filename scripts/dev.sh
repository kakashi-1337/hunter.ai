#!/usr/bin/env bash
# Development: backend with the API on :${HUNTERAI_PORT:-8790}, Vite with hot reload on :5173,
# and the ingest worker.
set -euo pipefail
cd "$(dirname "$0")/.."
(cd backend && uv sync)
scripts/searxng.sh start || echo "web_search is unavailable until SearXNG runs"
scripts/qdrant.sh || echo "the knowledge base is unavailable until Qdrant runs"
(cd backend && HUNTERAI_DEV=1 exec uv run python -m hunterai) &
backend=$!
(cd frontend && exec bun run dev) &
frontend=$!
(cd backend && exec uv run python -m hunterai.worker) &
worker=$!
stop_all() {
  kill "$backend" "$frontend" 2>/dev/null || true
  kill -TERM "$worker" 2>/dev/null || return 0
  for _ in $(seq 1 150); do kill -0 "$worker" 2>/dev/null || return 0; sleep 0.1; done
  kill -KILL "$worker" 2>/dev/null || true
}
trap stop_all EXIT INT TERM
echo "HunterAI dev: http://127.0.0.1:5173"
wait -n "$backend" "$frontend"
