#!/usr/bin/env bash
set -euo pipefail

# Docker Desktop on Windows otherwise rewrites container paths such as /tmp.
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PG_CONTAINER="${PG_CONTAINER:-qsa-postgres}"
PG_USER="${PG_USER:-qsa}"
PG_DATABASE="${PG_DATABASE:-qsa}"
OUTPUT="${1:-data/bootstrap/qsa-full.dump}"

mkdir -p "$(dirname "$OUTPUT")"

echo "Dumping ${PG_DATABASE} from ${PG_CONTAINER} ..."
docker exec "$PG_CONTAINER" pg_dump \
  -U "$PG_USER" \
  -d "$PG_DATABASE" \
  -Fc \
  -f /tmp/qsa-full.dump
docker cp "${PG_CONTAINER}:/tmp/qsa-full.dump" "$OUTPUT"
docker exec "$PG_CONTAINER" rm -f /tmp/qsa-full.dump

echo "Full dump written to ${OUTPUT}"
ls -lh "$OUTPUT"
