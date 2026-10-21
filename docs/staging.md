# Staging on a self-managed VPS

Staging runs from M0 onward; production is created at M4 the same way. One VPS runs everything with
Docker Compose: Caddy (HTTPS), the API, the outbox worker and PostgreSQL (TimescaleDB + PostGIS image).
Nightly encrypted backups go off the server with restic.

## What you need

| Item | Notes |
|---|---|
| VPS | Ubuntu 24.04, 2 vCPU, 4 GB RAM, 80 GB SSD, hosted in South Africa (POPIA). Root SSH key access. |
| DNS | An A record (e.g. `staging.<your domain>`) pointing at the VPS. Caddy gets the TLS certificate. |
| Off-site backup storage | An S3-compatible bucket **not** on the VPS, preferably in South Africa (e.g. AWS S3 `af-south-1`), with an access key limited to that bucket. Offshore storage is a cross-border transfer under POPIA section 72. |
| GitHub | Admin access to add the server's read-only deploy key. |

## Set up

```bash
scp deploy/bootstrap.sh root@<vps>:   # from a checkout of this repository
ssh root@<vps>
bash bootstrap.sh                     # prints a deploy key: add it in GitHub > Settings > Deploy keys (read-only)
bash bootstrap.sh                     # second run: asks for domain, email and backup bucket, then deploys
```

`bootstrap.sh` installs Docker from Docker's repository, turns on automatic security updates, fail2ban
and a firewall (SSH, 80, 443 only), creates a `deploy` user, switches SSH to keys only, writes `.env` and
`backup.env` with generated secrets (mode 600), installs the nightly timers, and runs the first deploy.
It is safe to re-run.

**Copy `/opt/logistics/.env` and `/opt/logistics/backup.env` into a password manager immediately.**
Losing `FIELD_ENCRYPTION_KEYS` makes encrypted ID and phone numbers unreadable; losing `RESTIC_PASSWORD`
makes every backup unreadable.

## Operate

All commands run on the server as `deploy` from `/opt/logistics`:

| Task | Command |
|---|---|
| Deploy the latest `main` | `deploy/ops.sh deploy` (discards local edits on the server; migrations run before the app starts) |
| Create an admin login | `deploy/ops.sh createsuperuser` |
| Back up now | `deploy/ops.sh backup` (also runs nightly at 01:30 SAST) |
| List backups | `deploy/ops.sh snapshots` |
| Restore drill | `deploy/ops.sh restore-drill` restores the latest backup into a scratch database, checks it and drops it. Run monthly; Phase 1 exit criteria require a full restore within 4 hours. |
| Real restore | `deploy/ops.sh restore [snapshot]` (asks you to type the domain; dumps the current database first) |
| Management command | `deploy/ops.sh manage <command>` |
| Logs | `docker compose -f compose.yaml -f deploy/compose.staging.yaml logs -f web outbox` |
| Timers | `systemctl list-timers 'logistics-*'`; last run: `journalctl -u logistics-backup` |

Nightly maintenance (03:00 SAST) purges expired idempotency keys and, once deployed, runs the inventory
reconciliation.

## Retention

restic keeps 7 daily, 4 weekly and 12 monthly snapshots, and verifies 1% of the stored data on every run.

## Known limits

- One server: if it dies, staging is down until it is rebuilt from a backup (bootstrap, then restore).
- Images build on the server. Production (M4) should pull tagged images from a registry instead, so the
  exact image tested in staging is what runs.
- Once TimescaleDB hypertables exist (M3), restores need `timescaledb_pre_restore()`/`post_restore()`;
  `ops.sh` will be updated then.
