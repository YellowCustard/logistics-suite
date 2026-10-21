#!/bin/sh
# Runs inside the restic container (see deploy/ops.sh backup). The database dump was already written to
# /data/dump by pg_dump inside the db container, so client and server versions always match.
set -eu

restic cat config >/dev/null 2>&1 || restic init

case "${1:-backup}" in
  backup)
    restic backup --tag nightly --host logistics /data/dump/logistics.dump /data/media
    restic forget --tag nightly --host logistics --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
    restic check --read-data-subset=1%
    ;;
  restore)
    # Restores into /data/dump/restore/<snapshot paths>; the caller loads it from there.
    rm -rf /data/dump/restore
    restic restore "${2:-latest}" --host logistics --target /data/dump/restore
    ;;
  snapshots)
    restic snapshots --host logistics
    ;;
  *)
    echo "usage: backup.sh [backup|restore SNAPSHOT|snapshots]" >&2
    exit 2
    ;;
esac
