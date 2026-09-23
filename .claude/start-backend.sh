#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/Scripts/uvicorn.exe src.api.router:app --host 127.0.0.1 --port 8000
