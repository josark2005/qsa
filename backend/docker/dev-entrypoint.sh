#!/bin/sh
set -eu

alembic upgrade head

if [ -n "${QSA_ADMIN_USERNAME:-}" ] && [ -n "${QSA_ADMIN_PASSWORD:-}" ]; then
    python -m app.cli ensure-admin \
        --username "${QSA_ADMIN_USERNAME}" \
        --password "${QSA_ADMIN_PASSWORD}" \
        --no-force-change
fi

exec "$@"
