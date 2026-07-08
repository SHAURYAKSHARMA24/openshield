#!/bin/bash
set -euo pipefail

# Default to production so gunicorn-based deployments fail closed on a missing
# or insecure JWT_SECRET. Override with OPENSHIELD_ENV=development only for
# local/demo runs launched via this script.
export OPENSHIELD_ENV="${OPENSHIELD_ENV:-production}"

echo "=== OpenShield startup ==="
echo "Applying database migrations..."
alembic upgrade head

echo "Startup complete. Starting Gunicorn web server..."
# NOTE: The scan worker is intentionally NOT started here. It runs as a
# separate Render background worker service (see render.yaml:
# openshield-worker / openshield-worker-staging, start command
# `python -m scanner.worker`). Keeping the worker out of the web container
# means restarting or scaling the web service does not kill the worker
# process; the standalone worker recovers stale scans on its own. Do not
# reintroduce a background worker subshell here.

exec gunicorn --bind=0.0.0.0:$PORT --timeout 120 --workers 2 api.app:application
