#!/bin/sh
# PostgreSQL 17 client, custom-format backups, full restore verification, no shell secrets in logs.
set -u
umask 077
backup_dir=${BACKUPS_DIR:-/backups}
interval=${BACKUP_INTERVAL_SECONDS:-86400}
retention=${BACKUP_RETENTION_DAYS:-7}
verify_db=""
partial=""
mkdir -p "$backup_dir"

cleanup() {
    if [ -n "$verify_db" ]; then
        dropdb --if-exists "$verify_db" || true
        verify_db=""
    fi
    if [ -n "$partial" ]; then rm -f -- "$partial"; fi
}
trap 'cleanup; exit 0' TERM INT
trap cleanup EXIT

run_backup() {
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    filename="backup-$stamp.dump"
    partial="$backup_dir/$filename.partial"
    verify_db="watcher_restore_$(date +%s)_$$"
    echo "Creating PostgreSQL backup $filename"
    pg_dump --format=custom --file="$partial" || return 1
    createdb "$verify_db" || return 1
    pg_restore --exit-on-error --no-owner --no-privileges --dbname="$verify_db" "$partial" || return 1
    # Actually query the restored application tables; a readable archive alone is not sufficient.
    restored_users=$(psql --dbname="$verify_db" -At -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM users') || return 1
    psql --dbname="$verify_db" -At -v ON_ERROR_STOP=1 -c \
      'SELECT count(*) FROM products; SELECT count(*) FROM notifications; SELECT version_num FROM alembic_version;' >/dev/null || return 1
    dropdb "$verify_db" || return 1
    verify_db=""
    mv "$partial" "$backup_dir/$filename" || return 1
    partial=""
    (cd "$backup_dir" && sha256sum "$filename" > "$filename.sha256" && sha256sum -c "$filename.sha256") || return 1
    bytes=$(wc -c < "$backup_dir/$filename" | tr -d ' ')
    success_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '%s' "$success_at" > "$backup_dir/.last-success-at"
    printf '{"result":"ok","verified":true,"last_success_at":"%s","last_attempt_at":"%s","filename":"%s","bytes":%s,"restored_users":%s}\n' \
      "$success_at" "$attempt_at" "$filename" "$bytes" "$restored_users" > "$backup_dir/status.json.tmp"
    mv "$backup_dir/status.json.tmp" "$backup_dir/status.json"
    find "$backup_dir" -type f -name 'backup-*.dump*' -mtime "+$retention" -exec rm -f -- {} +
    echo "Backup $filename verified by restoring a temporary database; restored users=$restored_users"
}

while :; do
    now=$(date +%s)
    next_run=$(cat "$backup_dir/.next-run" 2>/dev/null || echo 0)
    case "$next_run" in ''|*[!0-9]*) next_run=0 ;; esac
    if [ -f "$backup_dir/request" ] || [ "$now" -ge "$next_run" ]; then
        rm -f "$backup_dir/request"
        attempt_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        if run_backup; then
            echo $(( $(date +%s) + interval )) > "$backup_dir/.next-run"
        else
            cleanup
            last_success_json=null
            if [ -f "$backup_dir/.last-success-at" ]; then
                last_success_json="\"$(cat "$backup_dir/.last-success-at")\""
            fi
            printf '{"result":"failed","verified":false,"last_success_at":%s,"last_attempt_at":"%s","error":"Backup or restore verification failed; see backup service logs"}\n' \
              "$last_success_json" "$attempt_at" > "$backup_dir/status.json.tmp"
            mv "$backup_dir/status.json.tmp" "$backup_dir/status.json"
            echo $(( $(date +%s) + 300 )) > "$backup_dir/.next-run"
            echo "Backup verification failed; retrying in 5 minutes" >&2
        fi
        if [ "${BACKUP_RUN_ONCE:-0}" = "1" ]; then
            [ "$(cat "$backup_dir/status.json" | grep -c '"result":"ok"')" = "1" ] && exit 0
            exit 1
        fi
    fi
    sleep 10
done
