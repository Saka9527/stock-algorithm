#!/bin/sh
set -e

mkdir -p /app/data/parquet/market /app/data/parquet/factor /app/output

exec uvicorn api.main:app \
  --host "${API_HOST:-0.0.0.0}" \
  --port "${API_PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips='*'
