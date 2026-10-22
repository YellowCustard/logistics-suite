# Staging on xneelo Cloud

Staging runs from M0 onward; production is created at M4 the same way. One xneelo Cloud instance runs everything with
Docker Compose: Caddy (HTTPS), the API, the outbox worker and PostgreSQL (TimescaleDB + PostGIS image).
Nightly encrypted backups are made with restic: on the server itself by default, or off-site in an S3 bucket.

## What you need

| Item | Notes |
|---|---|
| Server | An xneelo Cloud instance (see below): Ubuntu 24.04, 2 vCPU, 4 GB RAM, 80 GB NVMe, hosted in South Africa (POPIA). |
| DNS | An A record (e.g. `staging.<your domain>`) pointing at the instance's public IPv4. Caddy gets the TLS certificate. |
| Off-site backup storage (optional for staging, required before production) | An S3 bucket **not** at xneelo, in South Africa: AWS S3 `af-south-1` (Cape Town), with an access key limited to that bucket. xneelo Cloud has no object storage, and backups on the same provider don't survive losing the account. Offshore storage is a cross-border transfer under POPIA section 72. Without it, backups stay on the server (see Known limits). |
| GitHub | Admin access to add the server's read-only deploy key. |

## Create the xneelo Cloud instance

In the xneelo Cloud dashboard:

1. **Network → Security Groups → Create Security Group** `logistics-web` with ingress rules TCP 22 (ideally
   only from your own IP), TCP 80, TCP 443 and UDP 443 (HTTP/3). Leave egress open.
2. **Compute → Instances → Launch Instance**:
   - Flavor `s-g-2cpu-4gb` (about R198/month). Production at M4: `s-g-4cpu-8gb`.
   - Boot source: the Ubuntu 24.04 image; boot volume **80 GB**, Premium (NVMe), about R144/month.
     Set "Automatically delete this volume when the instance is deleted?" to **No**.
   - Key pair: import your public key (`~/.ssh/id_ed25519.pub`). xneelo can't recover a lost private key.
   - Network: **Public** (gives the instance a public IPv4 and IPv6).
   - Security groups: add `logistics-web`.
3. Note the public IPv4 under Compute → Instances and create the DNS A record.

Instance snapshots are a useful extra before risky changes, but they are not the backup: restic to S3 is.

## Create the backup bucket (AWS, optional for staging)

Skip this section to keep backups on the server; press Enter at the bootstrap prompt for the repository.

1. In the AWS account, enable the **Africa (Cape Town) `af-south-1`** region (Account → AWS Regions; it is
   opt-in), then create a private bucket there with versioning off and default encryption on.
2. Create an IAM user for the server with only this policy, and an access key for it:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": "arn:aws:s3:::<bucket>"},
       {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": "arn:aws:s3:::<bucket>/*"}
     ]
   }
   ```

   `bootstrap.sh` asks for the repository (`s3:https://s3.af-south-1.amazonaws.com/<bucket>/logistics`) and
   this key, and stores them in `backup.env` only.

To move server-only backups off-site later: create the bucket and key as above, set `RESTIC_REPOSITORY` and
add `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in `/opt/logistics/backup.env`, then run `deploy/ops.sh backup`
(it initialises the new repository; older server-only snapshots stay in `/var/lib/logistics/restic`).

## Set up

xneelo's Ubuntu image logs in as `ubuntu` (not root), so the script runs with sudo:

```bash
scp deploy/bootstrap.sh ubuntu@<ip>:   # from a checkout of this repository, or on the server:
# curl -fsSLO https://raw.githubusercontent.com/YellowCustard/logistics-suite/main/deploy/bootstrap.sh
ssh ubuntu@<ip>
sudo bash bootstrap.sh                 # prints a deploy key: add it in GitHub > Settings > Deploy keys (read-only)
sudo bash bootstrap.sh                 # second run: asks for domain, email and backup bucket (Enter = on-server), then deploys
```

`bootstrap.sh` installs Docker from Docker's repository, turns on automatic security updates, fail2ban
and a firewall (SSH, 80, 443 only), creates a `deploy` user with your SSH key, switches SSH to keys only (no root login; `ubuntu` keeps working), writes `.env` and
`backup.env` with generated secrets (mode 600), installs the nightly timers, and runs the first deploy.
It is safe to re-run.

**Copy `/opt/logistics/.env` and `/opt/logistics/backup.env` into a password manager immediately.**
Losing `FIELD_ENCRYPTION_KEYS` makes encrypted ID and phone numbers unreadable; losing `RESTIC_PASSWORD`
makes every backup unreadable.

## Operate

All commands run on the server as `deploy` (`ssh deploy@<ip>`) from `/opt/logistics`:

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
- Server-only backups (no bucket) protect against mistakes and bad migrations, not against losing the instance,
  its volume or the xneelo account. Add the off-site bucket before production (M4).
- Images build on the server. Production (M4) should pull tagged images from a registry instead, so the
  exact image tested in staging is what runs.
- Once TimescaleDB hypertables exist (M3), restores need `timescaledb_pre_restore()`/`post_restore()`;
  `ops.sh` will be updated then.
