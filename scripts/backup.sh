#!/bin/sh
# Back up the DebugOps database (all application data, audit log and workflow checkpoints).
# Usage: scripts/backup.sh [output-file]      Run from the repository root (where docker-compose.yml is).
set -eu
OUT=${1:-backups/debugops-$(date +%Y%m%d-%H%M%S).dump}
mkdir -p "$(dirname "$OUT")"
umask 077  # backups contain sensitive data
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$OUT.partial"
mv "$OUT.partial" "$OUT"
echo "backup written: $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes)"
