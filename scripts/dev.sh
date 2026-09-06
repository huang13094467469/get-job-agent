#!/usr/bin/env bash
set -euo pipefail

# 本地启动 Local Agent Server
cd "$(dirname "$0")/.."

export PYTHONPATH="$(pwd)/src:${PYTHONPATH:-}"

HOST="${SERVER_HOST:-127.0.0.1}"
PORT="${SERVER_PORT:-8791}"

exec uvicorn get_job_agent.main:app --host "$HOST" --port "$PORT" --reload