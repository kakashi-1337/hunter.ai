#!/usr/bin/env bash
# What the tools need on this machine. Exits 1 when something required is missing.
set -uo pipefail
cd "$(dirname "$0")/.."
bad=0
line() { if [ "$2" = ok ]; then echo "OK       $1"; else echo "MISSING  $1${3:+ ($3)}"; fi; }

command -v rg >/dev/null 2>&1 && line "ripgrep (repo_search)" ok || { line "ripgrep (repo_search)" no "install rg"; bad=1; }
command -v docker >/dev/null 2>&1 && line "docker (web_search)" ok || line "docker (web_search)" no "web_search stays unavailable"
chunker="${HUNTERAI_CHUNKER:-backend/hunterai/mcp_servers/file_chunker_mcp.py}"
[ -f "$chunker" ] && line "file chunker ($chunker)" ok || { line "file chunker ($chunker)" no "read uses its plain fallback"; bad=1; }
(cd backend && uv run python -c "import mcp, jsonschema" >/dev/null 2>&1) && line "python packages mcp, jsonschema" ok \
  || { line "python packages mcp, jsonschema" no "run: cd backend && uv sync"; bad=1; }
curl -fsS 'http://127.0.0.1:8888/search?q=test&format=json' >/dev/null 2>&1 && line "SearXNG on 127.0.0.1:8888" ok \
  || line "SearXNG on 127.0.0.1:8888" no "run: scripts/searxng.sh start"
exit $bad
