#!/usr/bin/env bash
set -euo pipefail

# Long-lived services (gunicorn) want the full boot sequence: schema applied,
# static files collected, reference data present.
#
# Short-lived ones do not. The health watchdog runs twelve times an hour, and
# re-running `collectstatic` — which walks every file in staticfiles/ — on every
# one of those runs is minutes of wasted work and a slower alert. Set
# SKIP_BOOT_TASKS=true to go straight to the command.
if [[ "${SKIP_BOOT_TASKS:-false}" != "true" ]]; then
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
else
    echo "SKIP_BOOT_TASKS=true — going straight to: $*"
fi

echo "Starting: $*"
exec "$@"