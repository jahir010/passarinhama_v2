#!/bin/sh
set -e

DB_WAIT_HOST="${DB_HOST:-}"
DB_WAIT_PORT="${DB_PORT:-3306}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"
AUTO_CREATE_MIGRATIONS="${AUTO_CREATE_MIGRATIONS:-false}"
FAKE_UPGRADE_ON_CONFLICT="${FAKE_UPGRADE_ON_CONFLICT:-true}"
MIGRATION_NAME="${MIGRATION_NAME:-server_auto}"

has_migration_files() {
    find /app/migrations -type f -name "*.py" ! -name "__init__.py" -print -quit | grep -q .
}

run_with_log() {
    log_file="$1"
    shift

    if "$@" >"$log_file" 2>&1; then
        cat "$log_file"
        return 0
    fi

    status=$?
    cat "$log_file"
    return "$status"
}

is_fakeable_upgrade_error() {
    log_file="$1"
    grep -Eiq "Duplicate column name|Duplicate key name|already exists|already exist|Table .* exists|Column .* exists|Unknown column.*already|1060|1061|1050" "$log_file"
}

if [ -z "$DB_WAIT_HOST" ] && [ -n "${DATABASE_URL:-}" ]; then
    DB_WAIT_HOST=$(printf "%s" "$DATABASE_URL" | sed -nE 's|^[a-zA-Z0-9+]+://[^@]+@([^:/?]+).*|\1|p')
    DB_WAIT_PORT_FROM_URL=$(printf "%s" "$DATABASE_URL" | sed -nE 's|^[a-zA-Z0-9+]+://[^@]+@[^:/?]+:([0-9]+).*|\1|p')
    if [ -n "$DB_WAIT_PORT_FROM_URL" ]; then
        DB_WAIT_PORT="$DB_WAIT_PORT_FROM_URL"
    fi
fi

# Wait for database to be ready when host is provided
if [ -n "$DB_WAIT_HOST" ]; then
    echo "Waiting for database at $DB_WAIT_HOST:$DB_WAIT_PORT..."
    until nc -z "$DB_WAIT_HOST" "$DB_WAIT_PORT"; do
        echo "Database not ready, sleeping 3s..."
        sleep 3
    done
    echo "Database is reachable."
else
    echo "No DB host found in DB_HOST or DATABASE_URL. Skipping DB wait."
fi

# Ensure migrations path exists and is writable for first-time boot.
mkdir -p /app/migrations
mkdir -p /app/migrations/models

if [ "$RUN_MIGRATIONS" = "true" ]; then
    if has_migration_files; then
        if [ "$AUTO_CREATE_MIGRATIONS" = "true" ]; then
            echo "Checking for model changes with Aerich..."
            MIGRATE_LOG=$(mktemp)
            if ! run_with_log "$MIGRATE_LOG" aerich migrate --name "$MIGRATION_NAME"; then
                if grep -Eiq "No changes detected|no changes detected" "$MIGRATE_LOG"; then
                    echo "No schema changes detected."
                else
                    echo "aerich migrate failed."
                    rm -f "$MIGRATE_LOG"
                    exit 1
                fi
            fi
            rm -f "$MIGRATE_LOG"
        fi

        echo "Applying Aerich migrations..."
        UPGRADE_LOG=$(mktemp)
        if ! run_with_log "$UPGRADE_LOG" aerich upgrade; then
            if [ "$FAKE_UPGRADE_ON_CONFLICT" = "true" ] && is_fakeable_upgrade_error "$UPGRADE_LOG"; then
                echo "aerich upgrade hit an already-existing schema object. Retrying with --fake, then upgrade."
                aerich upgrade --fake
                aerich upgrade
            else
                echo "aerich upgrade failed with a non-fakeable error."
                rm -f "$UPGRADE_LOG"
                exit 1
            fi
        fi
        rm -f "$UPGRADE_LOG"
    else
        echo "No migration files found. Attempting aerich init-db..."
        aerich init-db || echo "aerich init-db skipped (already initialized or not required)."
    fi
else
    echo "RUN_MIGRATIONS=false. Skipping Aerich migration step."
fi

# Start FastAPI
PORT="${PORT:-8000}"
WORKERS="${WEB_CONCURRENCY:-3}"
echo "Starting FastAPI on port $PORT with $WORKERS workers..."
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers "$WORKERS"