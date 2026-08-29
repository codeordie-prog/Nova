#!/usr/bin/env bash
#
# Bring up the local Redis and Postgres that the workers talk to.
#
# Prefers Docker Compose so the versions match staging. Falls back to Homebrew
# services when Docker is not installed, because a working local stack beats a
# matching one you cannot start.
#
#   ./dermascan/scripts/local_stack.sh up       start, wait for health, apply migrations
#   ./dermascan/scripts/local_stack.sh down     stop the services, keep the data
#   ./dermascan/scripts/local_stack.sh status   what is listening and what is migrated
#   ./dermascan/scripts/local_stack.sh migrate  apply pending migrations only
#   ./dermascan/scripts/local_stack.sh psql     open a shell on qutis_db
#   ./dermascan/scripts/local_stack.sh redis    open redis-cli
#   ./dermascan/scripts/local_stack.sh reset    destroy all local data and re-migrate

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

REDIS_PORT=6379
PG_PORT=5432
PG_DB=qutis_db
PG_USER=postgres
PG_PASSWORD=localdev
BREW_PG_FORMULA=postgresql@14

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m error\033[0m %s\n' "$*" >&2; exit 1; }

port_pid() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1; }
port_owner() {
    local pid; pid="$(port_pid "$1")"
    [ -n "$pid" ] && ps -o comm= -p "$pid" 2>/dev/null | xargs basename
}

have_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

# Which stack should we drive? Docker when it is usable, Homebrew otherwise.
resolve_driver() {
    if have_docker; then echo docker; else echo brew; fi
}

ensure_env_local() {
    if [ ! -f .env.local ]; then
        say "creating .env.local from .env.local.example"
        cp .env.local.example .env.local
        warn "PAYSTACK_SECRET_KEY in .env.local is empty — top-up flows will fail until you set an sk_test_ key"
    fi
}

# --- docker path -------------------------------------------------------------

docker_up() {
    for port in "$REDIS_PORT" "$PG_PORT"; do
        owner="$(port_owner "$port" || true)"
        case "$owner" in
            ""|com.docker*|docker*) ;;
            *) die "port $port is already held by '$owner'. The compose stack uses the
        same ports as Homebrew so one .env.local serves both. Stop the other one:
            brew services stop redis $BREW_PG_FORMULA" ;;
        esac
    done

    say "starting compose services"
    docker compose up -d --wait
}

docker_down() { docker compose down; }

docker_reset() {
    say "destroying local volumes"
    docker compose down --volumes
    docker_up
}

# --- homebrew path -----------------------------------------------------------

brew_up() {
    command -v brew >/dev/null 2>&1 || die "neither Docker nor Homebrew is available"

    for svc in redis "$BREW_PG_FORMULA"; do
        if brew services list | awk -v s="$svc" '$1 == s { print $2 }' | grep -q started; then
            say "$svc already running"
        else
            say "starting $svc"
            brew services start "$svc"
        fi
    done

    wait_for_port "$REDIS_PORT" redis
    wait_for_port "$PG_PORT" postgres
    brew_provision_pg
}

# Homebrew initialises the cluster with the login user as the only superuser and
# no `postgres` role, but billing/db.py builds its conninfo from POSTGRES_USER
# and refuses an empty password. Create the role and database it expects.
brew_provision_pg() {
    if ! psql -h 127.0.0.1 -p "$PG_PORT" -d postgres -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname = '$PG_USER'" | grep -q 1; then
        say "creating role $PG_USER"
        psql -h 127.0.0.1 -p "$PG_PORT" -d postgres -c \
            "CREATE ROLE $PG_USER LOGIN SUPERUSER PASSWORD '$PG_PASSWORD'"
    fi

    if ! psql -h 127.0.0.1 -p "$PG_PORT" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$PG_DB'" | grep -q 1; then
        say "creating database $PG_DB"
        psql -h 127.0.0.1 -p "$PG_PORT" -d postgres -c \
            "CREATE DATABASE $PG_DB OWNER $PG_USER"
    fi
}

brew_down() {
    say "stopping Homebrew services"
    brew services stop redis || true
    brew services stop "$BREW_PG_FORMULA" || true
}

brew_reset() {
    say "dropping $PG_DB and flushing Redis"
    psql -h 127.0.0.1 -p "$PG_PORT" -d postgres -c "DROP DATABASE IF EXISTS $PG_DB" >/dev/null
    redis-cli -p "$REDIS_PORT" FLUSHALL >/dev/null
    brew_provision_pg
}

# --- shared ------------------------------------------------------------------

wait_for_port() {
    local port="$1" name="$2" tries=40
    while [ "$tries" -gt 0 ]; do
        if nc -z 127.0.0.1 "$port" 2>/dev/null; then return 0; fi
        sleep 0.25
        tries=$((tries - 1))
    done
    die "$name did not start listening on port $port"
}

python_bin() {
    if [ -x "$REPO_ROOT/venv/bin/python" ]; then echo "$REPO_ROOT/venv/bin/python"
    else echo python3; fi
}

migrate() {
    say "applying billing migrations"
    "$(python_bin)" -m dermascan.scripts.migrate
}

status() {
    printf '%-12s %s\n' "driver" "$(resolve_driver)"
    for pair in "redis:$REDIS_PORT" "postgres:$PG_PORT"; do
        name="${pair%%:*}"; port="${pair##*:}"
        owner="$(port_owner "$port" || true)"
        if [ -n "$owner" ]; then
            printf '%-12s up on %s (%s)\n' "$name" "$port" "$owner"
        else
            printf '%-12s down (port %s free)\n' "$name" "$port"
        fi
    done

    printf '%-12s %s\n' "env" "$([ -f .env.local ] && echo '.env.local present' || echo '.env.local MISSING')"
    "$(python_bin)" -m dermascan.scripts.migrate --dry-run 2>&1 | tail -2
}

case "${1:-up}" in
    up)
        ensure_env_local
        case "$(resolve_driver)" in
            docker) docker_up ;;
            brew)
                warn "Docker not available — using Homebrew services instead."
                warn "Redis $(redis-server --version 2>/dev/null | sed -n 's/.*v=\([0-9.]*\).*/\1/p') and Postgres 14 locally vs redis:7 / postgres:16 in compose."
                brew_up ;;
        esac
        migrate
        say "local stack ready — REDIS_URL=redis://127.0.0.1:$REDIS_PORT/0  POSTGRES=$PG_DB@127.0.0.1:$PG_PORT"
        ;;
    down)
        case "$(resolve_driver)" in docker) docker_down ;; brew) brew_down ;; esac
        ;;
    reset)
        case "$(resolve_driver)" in docker) docker_reset ;; brew) brew_reset ;; esac
        migrate
        ;;
    migrate) migrate ;;
    status)  status ;;
    psql)    PGPASSWORD="$PG_PASSWORD" psql -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" ;;
    redis)   redis-cli -p "$REDIS_PORT" ;;
    *) die "unknown command '${1}' — try: up down reset migrate status psql redis" ;;
esac
