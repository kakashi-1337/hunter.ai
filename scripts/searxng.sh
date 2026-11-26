#!/usr/bin/env bash
# The self-hosted search engine behind the web_search tool, on 127.0.0.1:8888 only.
#   scripts/searxng.sh start | stop | status
set -euo pipefail
NAME=hunterai-searxng
IMAGE=searxng/searxng:2026.11.12-de722ab1b
DIR="${HUNTERAI_HOME:-$HOME/.hunterai}/searxng"
URL='http://127.0.0.1:8888/search?q=test&format=json'

if ! command -v docker >/dev/null 2>&1; then
  echo "searxng: Docker is not installed, so web_search is unavailable"
  exit 1
fi

make() {
  docker run -d --name "$NAME" --memory 1g -p 127.0.0.1:8888:8080 \
    -v "$DIR:/etc/searxng" "$IMAGE" >/dev/null
}
stays_up() {
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" != "true" ]; then
    echo "searxng: the container $NAME stopped right after it was started. Its last lines:"
    docker logs --tail 20 "$NAME" 2>&1 || true
    exit 1
  fi
}
# What Docker publishes for the container now. How it was made does not say that.
published() { docker port "$NAME" 8080/tcp 2>/dev/null | head -n 1 || true; }

case "${1:-}" in
  start)
    # The container takes ownership of this folder on its first start, so chmod may be refused.
    mkdir -p "$DIR" && { chmod 0755 "$DIR" 2>/dev/null || true; }
    if [ ! -f "$DIR/settings.yml" ]; then
      # The container runs as its own non-root user and must be able to read this file. It
      # holds only the secret of an instance that listens on loopback.
      cat > "$DIR/settings.yml" <<YML
use_default_settings: true
server:
  secret_key: "$(openssl rand -hex 32)"
  limiter: false
search:
  formats: [html, json]
YML
      chmod 0644 "$DIR/settings.yml"
    fi
    if docker container inspect "$NAME" >/dev/null 2>&1; then
      # `docker start` leaves a container as it was made. One that publishes another address, or none (it then
      # runs and nothing can reach it), or that is of another version, is made again: it holds nothing, its
      # settings are in $DIR.
      bound="$(docker inspect -f '{{with index .HostConfig.PortBindings "8080/tcp"}}{{range .}}{{.HostIp}}:{{.HostPort}}{{end}}{{end}}' "$NAME" 2>/dev/null || true)"
      from="$(docker inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null || true)"
      why=""
      if [ "$bound" != "127.0.0.1:8888" ]; then
        why="does not publish 127.0.0.1:8888, so nothing can reach it"
      elif [ "$from" != "$IMAGE" ]; then
        why="was made from $from, and this version of HunterAI uses $IMAGE"
      fi
      if [ -n "$why" ]; then
        echo "searxng: the container $NAME $why. It is made again; its settings are kept in $DIR."
        docker rm -f "$NAME" >/dev/null
      fi
    fi
    if docker container inspect "$NAME" >/dev/null 2>&1; then
      docker start "$NAME" >/dev/null
    else
      make
    fi
    stays_up
    # A container that was made right can still run attached to no network of Docker: no port of it is published,
    # nothing reaches it, it can look up no name, and a restart leaves it so. A new container is attached again.
    if [ "$(published)" != "127.0.0.1:8888" ]; then
      echo "searxng: the container $NAME runs, and Docker publishes no port for it: it is attached to no network, so nothing can reach it and it can look up no name. It is made again; its settings are kept in $DIR."
      docker rm -f "$NAME" >/dev/null
      make
      stays_up
      if [ "$(published)" != "127.0.0.1:8888" ]; then
        echo "searxng: Docker publishes no port for the new container either. Restart Docker (sudo systemctl restart docker) and start HunterAI again."
        exit 1
      fi
    fi
    began="$(date +%s)"
    last=""
    for _ in $(seq 1 60); do
      # A try is a real search and may take its time, 15 seconds at most: one that hangs must not hold the start
      # for ever.
      if last="$(curl -fsS -m 15 -o /dev/null "$URL" 2>&1)"; then
        echo "searxng: ready on http://127.0.0.1:8888"
        exit 0
      fi
      [ $(( $(date +%s) - began )) -lt 60 ] || break       # sixty slow tries would be a quarter of an hour
      sleep 1
    done
    echo "searxng: not answering after 60 s${last:+. The last try: $last}"
    docker logs --tail 20 "$NAME" || true
    exit 1
    ;;
  stop)
    docker stop "$NAME" >/dev/null && echo "searxng: stopped"
    ;;
  status)
    if curl -fsS -m 15 "$URL" >/dev/null 2>&1; then echo "searxng: ready"; else echo "searxng: not running"; exit 1; fi
    ;;
  *)
    echo "usage: scripts/searxng.sh start | stop | status"
    exit 2
    ;;
esac
