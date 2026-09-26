#!/bin/sh
set -eu

case "${1:-api}" in
  api)
    # Each uvicorn worker holds its own counters; prometheus_client aggregates them from this
    # directory, which must start empty or dead workers' files keep being reported.
    export PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus}"
    rm -rf "$PROMETHEUS_MULTIPROC_DIR"
    mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
    exec uvicorn src.api.router:app --host 0.0.0.0 --port 8000 \
      --workers "${API_WORKERS:-4}" --proxy-headers
    ;;
  worker)
    exec python -m arq src.ingestion.worker.WorkerSettings
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    exec "$@"
    ;;
esac
