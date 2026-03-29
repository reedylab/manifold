#!/bin/bash
set -e
mkdir -p /app/logs /app/data /app/output /app/logos /app/streams /app/bumps
exec gunicorn -c gunicorn.config.py "manifold.web.app:app"
