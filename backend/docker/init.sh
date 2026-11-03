#!/bin/sh
set -eu

alembic upgrade head

history_count=$(
python - <<'PY'
from sqlalchemy import func, select

from app.core.config import get_config
from app.core.db import make_engine, make_session_factory
from app.models.history_import import HistoryImport

engine = make_engine(get_config().database_url)
factory = make_session_factory(engine)
try:
    with factory() as db:
        print(int(db.scalar(select(func.count()).select_from(HistoryImport)) or 0))
finally:
    engine.dispose()
PY
)

if [ "${history_count}" = "0" ] && [ -d "${HISTORY_DIR:-/app/data/history}" ]; then
    python -m app.cli import-history --dir "${HISTORY_DIR:-/app/data/history}"
fi

if [ -n "${QSA_ADMIN_USERNAME:-}" ] && [ -n "${QSA_ADMIN_PASSWORD:-}" ]; then
    python -m app.cli ensure-admin \
        --username "${QSA_ADMIN_USERNAME}" \
        --password "${QSA_ADMIN_PASSWORD}"
fi
