#!/bin/sh
set -eu

database_path="${JOURNAL_DATABASE_PATH:-/data/journal.sqlite3}"
backup_directory="${JOURNAL_BACKUP_DIRECTORY:-/data/backups}"
destination="${JOURNAL_BACKUP_DESTINATION:?Set JOURNAL_BACKUP_DESTINATION to an off-box rsync target}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${backup_directory}/journal-${timestamp}.sqlite3"

mkdir -p "${backup_directory}"
sqlite3 "${database_path}" ".backup '${backup_path}'"
sqlite3 "${backup_path}" "PRAGMA integrity_check;" | grep -qx "ok"
rsync -a "${backup_path}" "${destination}"
find "${backup_directory}" -type f -name 'journal-*.sqlite3' -mtime +14 -delete
