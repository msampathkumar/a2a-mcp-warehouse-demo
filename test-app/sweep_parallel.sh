#!/usr/bin/env bash
# All levels in parallel, one process per (level, trial). Usage: test-app/sweep_parallel.sh TAG TRIALS
set -uo pipefail
cd "$(dirname "$0")/.."
TAG="$1"; TRIALS="${2:-5}"
set -a; [ -f .env ] && . ./.env; set +a
PIDS=()
for L in 1 2 3 4 5; do
  (cd demo-app && LEVEL=$L ../.venv/bin/uvicorn --factory warehouse.app:create_app --host 127.0.0.1 --port $((8000+L)) > /tmp/warehouse-level$L.log 2>&1) &
  PIDS+=($!)
done
for L in 1 2 3 4 5; do for i in $(seq 1 60); do curl -sf localhost:$((8000+L))/healthz >/dev/null && break; sleep 1; done; done
echo "servers up"
JOBS=()
for L in 1 2 3 4 5; do for T in $(seq 1 $TRIALS); do
  (cd test-app && ../.venv/bin/python -m harness.run --level $L --trials 1 --trial-offset $((T-1)) --tag "$TAG" > /tmp/sweep-$TAG-L$L-t$T.log 2>&1) &
  JOBS+=($!)
done; done
wait "${JOBS[@]}"
echo "harness done"
kill "${PIDS[@]}" 2>/dev/null; pkill -f "warehouse.app:create_app" ; true
echo "=== sweep $TAG done"
