#!/usr/bin/env bash
# Run levels sequentially: start the level's server, run the harness, stop the server.
# Usage: test-app/sweep.sh "2 3 4 5" [TRIALS]
set -uo pipefail
cd "$(dirname "$0")/.."
LEVELS="${1:-1 2 3 4 5}"; TRIALS="${2:-1}"
for L in $LEVELS; do
  PORT=$((8000 + L))
  make serve LEVEL=$L > "/tmp/warehouse-level$L.log" 2>&1 &
  SRV=$!
  for i in $(seq 1 60); do curl -sf "localhost:$PORT/healthz" >/dev/null && break; sleep 1; done
  echo "=== level $L (server pid $SRV)"
  make run LEVEL=$L TRIALS=$TRIALS 2>&1 | grep -E "^batch|  (mcp|a2a) t|report:|Error|Traceback"
  pkill -f "port $PORT" ; kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
done
echo "=== sweep done"
