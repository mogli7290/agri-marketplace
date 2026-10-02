#!/usr/bin/env bash
set -euo pipefail

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

# Optionally create the initial admin user when credentials are provided.
if [[ -n "${DJANGO_SUPERUSER_USERNAME:-}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
    echo "Ensuring superuser '${DJANGO_SUPERUSER_USERNAME}' exists..."
    python manage.py createsuperuser --noinput || true
fi

# Seed reference crops on first boot (idempotent). Set SEED_ON_BOOT=false to skip.
if [[ "${SEED_ON_BOOT:-true}" == "true" ]]; then
    python manage.py seed_data || true
fi

echo "Starting: $*"
exec "$@"
