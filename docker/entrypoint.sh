#!/bin/sh
set -eu

case "${1:-api}" in
  api)
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
