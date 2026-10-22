#!/usr/bin/env bash
# Day-to-day operations on the staging/production VPS. Run from the repository root as the deploy user.
#
#   deploy/ops.sh deploy                 pull main, rebuild, migrate, restart
#   deploy/ops.sh backup                 dump Postgres, then push dump + documents to the restic repo
#   deploy/ops.sh snapshots              list backups
#   deploy/ops.sh restore-drill [SNAP]   restore a backup into a scratch database, verify it, drop it
#   deploy/ops.sh restore [SNAP]         DESTRUCTIVE: replace the live database and documents from a backup
#   deploy/ops.sh maintenance            nightly housekeeping (idempotency purge, inventory reconciliation)
#   deploy/ops.sh createsuperuser        create an admin login interactively
#   deploy/ops.sh manage CMD [ARGS...]   run a Django management command
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091  # created on the server by bootstrap.sh
if [[ -f .env ]]; then set -a; source .env; set +a; fi
COMPOSE=(docker compose -f compose.yaml -f deploy/compose.staging.yaml)
DUMP_DIR="${BACKUP_DUMP_DIR:-/var/lib/logistics/dump}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

dump_db() {
  local target="$1" tmp
  tmp="$target.tmp"
  "${COMPOSE[@]}" exec -T db pg_dump --format=custom --no-owner -U logistics logistics >"$tmp"
  mv "$tmp" "$target"
}

restore_files() {
  "${COMPOSE[@]}" run --rm backup restore "${1:-latest}" >&2
  local restored="$DUMP_DIR/restore/data/dump/logistics.dump"
  [[ -s "$restored" ]] || { log "restored snapshot has no database dump"; exit 1; }
  echo "$restored"
}

cmd="${1:-}"
shift || true
case "$cmd" in
  deploy)
    git fetch --quiet origin main
    git checkout --quiet main
    git reset --quiet --hard origin/main
    "${COMPOSE[@]}" build --pull
    "${COMPOSE[@]}" up -d --remove-orphans
    "${COMPOSE[@]}" ps
    log "deployed $(git rev-parse --short HEAD)"
    ;;
  backup)
    mkdir -p "$DUMP_DIR"
    dump_db "$DUMP_DIR/logistics.dump"
    "${COMPOSE[@]}" run --rm backup backup
    log "backup complete"
    ;;
  snapshots)
    "${COMPOSE[@]}" run --rm backup snapshots
    ;;
  restore-drill)
    started=$(date +%s)
    restored=$(restore_files "${1:-latest}" | tail -n 1)
    "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U logistics -d postgres \
      -c "DROP DATABASE IF EXISTS restore_drill" -c "CREATE DATABASE restore_drill"
    "${COMPOSE[@]}" exec -T db pg_restore --no-owner --exit-on-error -U logistics -d restore_drill <"$restored"
    "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U logistics -d restore_drill -At -c \
      "SELECT 'audit entries: ' || count(*) FROM platform_auditentry UNION ALL
       SELECT 'migrations: ' || count(*) FROM django_migrations"
    "${COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U logistics -d postgres -c "DROP DATABASE restore_drill"
    media_files=$(find "$DUMP_DIR/restore/data/media" -type f | wc -l)
    log "restore drill passed in $(( $(date +%s) - started ))s; $media_files document file(s) restored"
    ;;
  restore)
    read -r -p "This REPLACES the live database and documents. Type the domain to continue: " answer
    [[ "$answer" == "${DOMAIN:?export DOMAIN first}" ]] || { log "aborted"; exit 1; }
    restored=$(restore_files "${1:-latest}" | tail -n 1)
    mkdir -p "$DUMP_DIR"
    dump_db "$DUMP_DIR/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).dump"  # keep what we are about to replace
    "${COMPOSE[@]}" stop web outbox
    "${COMPOSE[@]}" exec -T db pg_restore --clean --if-exists --no-owner --exit-on-error \
      -U logistics -d logistics <"$restored"
    "${COMPOSE[@]}" run --rm --no-deps --entrypoint sh \
      -v "$DUMP_DIR/restore/data/media:/restore:ro" web -c 'cp -a /restore/. /app/media/'
    "${COMPOSE[@]}" up -d
    log "restore complete"
    ;;
  maintenance)
    "${COMPOSE[@]}" exec -T web python manage.py purge_idempotency
    if "${COMPOSE[@]}" exec -T web python manage.py help reconcile_inventory >/dev/null 2>&1; then
      "${COMPOSE[@]}" exec -T web python manage.py reconcile_inventory
    fi
    ;;
  createsuperuser)
    "${COMPOSE[@]}" exec web python manage.py createsuperuser
    ;;
  manage)
    "${COMPOSE[@]}" exec -T web python manage.py "$@"
    ;;
  *)
    sed -n '2,13p' "$0"
    exit 2
    ;;
esac
