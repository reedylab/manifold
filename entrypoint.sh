#!/bin/bash
set -e
mkdir -p /app/logs /app/data /app/output /app/logos /app/streams /app/bumps
exec uvicorn manifold.web.app:app --host 0.0.0.0 --port 40000 --workers 1 --timeout-keep-alive 75
