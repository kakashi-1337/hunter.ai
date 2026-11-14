#!/usr/bin/env bash
# Put the current code into service: copy it to ~/.hunterai/app and restart the app from
# that copy. The running app then no longer changes while the source tree is being edited.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="$PWD"
APP="${HUNTERAI_HOME:-$HOME/.hunterai}/app"
PORT="${HUNTERAI_PORT:-8790}"

(cd backend && uv sync -q)
(cd frontend && bun install --frozen-lockfile >/dev/null && bun run build >/dev/null)
rm -rf "$APP.new"
mkdir -p "$APP.new/backend" "$APP.new/frontend"
cp -r backend/hunterai "$APP.new/backend/hunterai"
find "$APP.new" -name __pycache__ -type d -prune -exec rm -rf {} +
cp -r frontend/dist "$APP.new/frontend/dist"
git rev-parse --short HEAD > "$APP.new/VERSION" 2>/dev/null || true

DATA="${HUNTERAI_HOME:-$HOME/.hunterai}"
# Stop the ingest worker of the previous copy (its job resumes when the new one starts).
if [ -f "$DATA/worker.pid" ]; then
  wpid="$(cat "$DATA/worker.pid")"
  if [ -n "$wpid" ] && tr '\0' ' ' < "/proc/$wpid/cmdline" 2>/dev/null | grep -q "hunterai.worker"; then
    kill -TERM "$wpid" 2>/dev/null || true
    for _ in $(seq 1 150); do kill -0 "$wpid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL "$wpid" 2>/dev/null || true
  fi
  rm -f "$DATA/worker.pid"
fi
pid="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -n "$pid" ]; then
  kill "$pid"
  for _ in $(seq 1 100); do ss -ltnH "sport = :$PORT" | grep -q . || break; sleep 0.2; done
fi
rm -rf "$APP.old"; [ -d "$APP" ] && mv "$APP" "$APP.old"; mv "$APP.new" "$APP"; rm -rf "$APP.old"

scripts/searxng.sh start >/dev/null 2>&1 || echo "web_search is unavailable until SearXNG runs"
scripts/qdrant.sh || echo "the knowledge base is unavailable until Qdrant runs"
cd "$APP/backend"
HUNTERAI_PROJECT="$PROJECT" nohup "$PROJECT/backend/.venv/bin/python" -m hunterai.worker \
  > "$DATA/worker.log" 2>&1 &
echo $! > "$DATA/worker.pid"
HUNTERAI_PROJECT="$PROJECT" nohup "$PROJECT/backend/.venv/bin/python" -m hunterai \
  > "${HUNTERAI_HOME:-$HOME/.hunterai}/server.log" 2>&1 &
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/api/settings" >/dev/null 2>&1; then
    echo "HunterAI $(cat "$APP/VERSION" 2>/dev/null) is running on http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 0.5
done
echo "HunterAI did not start; see ${HUNTERAI_HOME:-$HOME/.hunterai}/server.log"
exit 1
