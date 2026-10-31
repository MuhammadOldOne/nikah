#!/usr/bin/env bash
# PostgreSQL backup of the docker compose stack: pg_dump | gzip into a timestamped file, keeping
# only the newest $KEEP files. Run from anywhere on the server (e.g. daily from cron):
#
#   ./scripts/backup.sh                                   -> backups/nikah-YYYYmmdd-HHMMSS.sql.gz
#   BACKUP_DIR=/var/backups/nikah KEEP=30 ./scripts/backup.sh
#
# Backups contain personal data: they are created readable by the owner only (umask 077).
# Restore instructions: docs/DEPLOY.md, section «Резервные копии».
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP="${KEEP:-14}"

case "$KEEP" in
  '' | *[!0-9]* | 0)
    echo "KEEP должен быть положительным числом" >&2
    exit 1
    ;;
esac

umask 077
mkdir -p "$BACKUP_DIR"

target="$BACKUP_DIR/nikah-$(date -u +%Y%m%d-%H%M%S).sql.gz"
partial="$target.partial"
trap 'rm -f "$partial"' EXIT

# --clean --if-exists: the dump drops existing objects first, so it restores over a live database.
docker compose exec -T db pg_dump --username=nikah --dbname=nikah --clean --if-exists --no-owner \
  | gzip -9 > "$partial"
gzip -t "$partial"
mv "$partial" "$target"
echo "Резервная копия сохранена: $target ($(du -h "$target" | cut -f1))"

ls -1t "$BACKUP_DIR"/nikah-*.sql.gz | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do
  rm -f -- "$old"
  echo "Удалена старая копия: $old"
done
