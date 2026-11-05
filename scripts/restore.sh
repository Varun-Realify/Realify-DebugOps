#!/bin/sh
# Restore the DebugOps database from a backup made by scripts/backup.sh.
# DESTRUCTIVE: replaces all current data. Usage: scripts/restore.sh <backup-file> [--yes]
set -eu
FILE=${1:?usage: scripts/restore.sh <backup-file> [--yes]}
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 1; }
if [ "${2:-}" != "--yes" ]; then
  printf "This REPLACES all DebugOps data with %s. Type 'restore' to continue: " "$FILE"
  read -r answer; [ "$answer" = "restore" ] || { echo "aborted"; exit 1; }
fi
docker compose stop api worker llm-gateway web
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction' < "$FILE"
docker compose up -d api worker llm-gateway web
echo "restore complete from $FILE"
