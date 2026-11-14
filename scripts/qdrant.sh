#!/usr/bin/env bash
# The vector store behind the knowledge base, on 127.0.0.1:6333 only. Started and left
# running; stop it with: docker stop hunterai-qdrant
set -euo pipefail
NAME=hunterai-qdrant
IMAGE=qdrant/qdrant:v1.20.0

if [ "${HUNTERAI_VECTOR:-qdrant}" = "memory" ]; then
  echo "vector store: memory (Qdrant not started)"
  exit 0
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "qdrant: Docker is not installed, so the knowledge base cannot store vectors"
  exit 1
fi
data="${HUNTERAI_HOME:-$HOME/.hunterai}/qdrant"
mkdir -p "$data/storage" "$data/snapshots"
if docker container inspect "$NAME" >/dev/null 2>&1; then
  docker start "$NAME" >/dev/null
else
  docker run -d --name "$NAME" --user "$(id -u):$(id -g)" --memory 4g -p 127.0.0.1:6333:6333 \
    -v "$data/storage:/qdrant/storage" -v "$data/snapshots:/qdrant/snapshots" "$IMAGE" >/dev/null
fi
port="$(docker port "$NAME" 6333/tcp)"
if [ "$port" != "127.0.0.1:6333" ]; then
  echo "qdrant: expected 127.0.0.1:6333, the container publishes: $port"
  exit 1
fi
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:6333/readyz >/dev/null 2>&1; then
    echo "qdrant: ready on 127.0.0.1:6333"
    exit 0
  fi
  sleep 1
done
echo "qdrant: not ready after 60 s"
docker logs --tail 20 "$NAME" || true
exit 1
