#!/usr/bin/env bash
# End-to-end: maidr-mcp in ext-apps' reference host (basic-host), driven the way
# a model and a reader would drive it. Run from anywhere:
#
#   bash e2e/run.sh
#
# EXT_APPS_REF picks the ext-apps tag the host is built from. CHROMIUM points the
# driver at a Chromium binary; without it, run `npx playwright-core install chromium`
# in e2e/ first.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CACHE="$HERE/work"  # not a dot directory: express will not send files from one
EXT_APPS_REF="${EXT_APPS_REF:-v2.0.3}"
HOST_DIR="$CACHE/basic-host-$EXT_APPS_REF"
SERVER_PORT="${SERVER_PORT:-3001}"

if [ ! -f "$HOST_DIR/dist/index.html" ]; then
  rm -rf "$CACHE/ext-apps" "$HOST_DIR"
  mkdir -p "$CACHE"
  git clone -q --depth 1 --branch "$EXT_APPS_REF" https://github.com/modelcontextprotocol/ext-apps.git "$CACHE/ext-apps"
  # Built on its own: inside the ext-apps checkout, npm would install the whole workspace.
  cp -r "$CACHE/ext-apps/examples/basic-host" "$HOST_DIR"
  (
    cd "$HOST_DIR"
    npm install --no-audit --no-fund --loglevel=error
    npm install --no-save --no-audit --no-fund --loglevel=error tsx@4
    INPUT=index.html npx vite build --logLevel error
    INPUT=sandbox.html npx vite build --logLevel error
  )
fi
(cd "$HERE" && npm install --no-audit --no-fund --loglevel=error)
uv sync --quiet --project "$ROOT"

# Started directly rather than through uv or npx, so the trap stops the processes holding the ports.
"$ROOT/.venv/bin/maidr-mcp" --port "$SERVER_PORT" &
SERVER=$!
(cd "$HOST_DIR" && SERVERS="[\"http://localhost:$SERVER_PORT/mcp\"]" exec node --import tsx serve.ts) &
HOST=$!
trap 'kill $SERVER $HOST 2>/dev/null || true' EXIT

# A GET on /mcp would open an event stream, so ask with OPTIONS, and never wait long.
for url in "http://localhost:8080/" "http://localhost:$SERVER_PORT/mcp"; do
  for _ in $(seq 60); do
    curl -s -o /dev/null --max-time 2 -X OPTIONS "$url" && break
    sleep 1
  done
done

SERVER_URL="http://localhost:$SERVER_PORT/mcp" node "$HERE/drive.mjs"
