# Logistics suite

Contract warehousing, transport and billing for a South African 3PL. A Django modular monolith on one
PostgreSQL database, built milestone by milestone from the Phase 1 Delivery Specification. Done so far:
M0 (platform services, master data, bins) and the first part of M1 (ASNs, receiving, inventory ledger,
putaway).

## Layout

| Path | What it is |
|---|---|
| `apps/platform` | Bottom layer: users and roles, principals and scoping, API tokens, audit log, transactional outbox, idempotency, documents, exception queue, business numbers, CSV import engine |
| `apps/masterdata` | Clients and other parties, sites, items (UoMs, barcodes), vehicles, drivers, contracts, versioned rate cards |
| `apps/oms` | Inbound notices (ASNs and returns). Outbound orders arrive in M2 |
| `apps/wms` | Bins, receiving, the inventory ledger (`ledger.py`, the only code that changes stock), putaway. Picking arrives in M2 |
| `config` | Settings (environment-driven), URLs, API root |
| `tests` | pytest suite against real PostgreSQL, including the M0 exit test |

Module boundaries are enforced by import-linter (`pyproject.toml`): `platform` imports no business module,
`masterdata` imports no other business module, `oms` never imports `wms`, and `wms` reaches `oms` only through
`oms.services`. Business modules reach master data only through its models and `services`.

## Run locally

Requires Python 3.13, [uv](https://docs.astral.sh/uv/) and PostgreSQL 16+.

```bash
uv sync
createdb logistics                       # or: docker compose up -d db
export DJANGO_DEBUG=1                    # dev defaults for SECRET_KEY and FIELD_ENCRYPTION_KEYS
export DATABASE_URL=postgres://logistics:logistics@localhost:5432/logistics
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver        # API docs at /api/v1/docs (log in at /admin first)
uv run python manage.py run_outbox       # second terminal: event delivery worker
```

Or everything in containers: `cp .env.example .env`, fill in the secrets, then `docker compose up --build`.

Staging and production run on a self-managed VPS: see [docs/staging.md](docs/staging.md).

## Checks

```bash
uv run ruff format --check . && uv run ruff check .
uv run lint-imports
uv run python manage.py makemigrations --check --dry-run
uv run pytest                            # needs a reachable PostgreSQL (DATABASE_URL); creates test_logistics
```

CI (`.github/workflows/ci.yml`) runs all of the above plus `check --deploy` and a container build.

## API conventions

- Base path `/api/v1`. Docs at `/api/v1/docs` and the schema at `/api/v1/openapi.json`, both for logged-in staff only.
- Auth: Django session (staff, admin) or OAuth2 client credentials for client systems
  (`POST /api/v1/auth/token`, then `Authorization: Bearer …`). Create credentials with
  `manage.py create_api_client --name "SDB ERP" --client-id <party uuid>`; the secret is shown once.
- Every `POST`/`PUT`/`PATCH`/`DELETE` needs an `Idempotency-Key` header. A repeat with the same key and body
  replays the stored response (`Idempotent-Replayed: true`); the same key with a different body is a 422.
  Error responses are not stored, so a corrected request may reuse its key. Keys expire after 48 hours.
- Errors are RFC 9457 `application/problem+json`; CSV imports add `errors: [{row, field, message}]`.
- Lists use keyset pagination: `?cursor=<next_cursor>&limit=50`.
- Updates take `If-Match: <version>` and return 412 when the record changed.
- Rows outside the caller's clients or sites return 404, never 403, so other clients' ids do not leak.
- CSV imports are all-or-nothing. The uploaded file is kept as a `Document`.

## Runbook

| Task | How |
|---|---|
| Event delivery | `manage.py run_outbox` runs continuously (the `outbox` service in Compose). It stops cleanly on SIGTERM. |
| Failed events | After `OUTBOX_MAX_ATTEMPTS` (8) a delivery is dead-lettered and an exception case `outbox.dead_letter` opens. Fix the cause, then use the **Replay** action on Event deliveries in the admin. |
| Outbox lag | `GET /api/v1/readyz` reports `outbox_lag_seconds`; alert above 300. |
| Idempotency cleanup | Schedule `manage.py purge_idempotency` daily (cron or a scheduled container run). |
| Encryption key rotation | Generate a key, put it **first** in `FIELD_ENCRYPTION_KEYS`, keep the old ones after it, deploy. Old values still decrypt and are re-encrypted on their next save. Never drop a key while rows may still use it. |
| Inventory reconciliation | Schedule `manage.py reconcile_inventory` nightly. It compares every balance with its movement ledger and opens a critical `inventory.ledger_drift` case on any difference. |
| Receipt variances | Over, short or unexpected receipt lines open a `receipt.variance` case with the line-by-line detail. |
| Audit log | Append-only, enforced by a database trigger. Read it in the admin under Audit entries. |
| Rate cards | Published cards and their lines are immutable (database triggers). Changes mean a new version; publishing an open-ended newer version closes the older one the day before. |
| Backups | On the VPS, `deploy/ops.sh backup` runs nightly: a `pg_dump` plus the document volume, encrypted by restic and stored off-site. Rehearse `deploy/ops.sh restore-drill` monthly. See [docs/staging.md](docs/staging.md). |

## Deliberate deviations from the specification (M0)

- Tables use Django's default `<app>_<model>` names rather than one Postgres schema per module; boundaries
  are enforced in code by import-linter. Extraction later means moving tables, not renaming them.
- UUIDv7 keys are generated in the application (`apps/platform/ids.py`, RFC 9562), not by PostgreSQL 18's
  `uuidv7()`, so the code runs on PostgreSQL 16 and 17 too.
- PostGIS columns are deferred to M3 (routing). Sites carry decimal latitude and longitude for now.
- The outbox relay is a management command; a job queue (Procrastinate) arrives with the first scheduled
  job in M1.
- mypy is not yet in CI.

## Deliberate deviations and assumptions (M1)

- WMS reads an ASN through `oms.services` when the dock opens a receipt, instead of materialising an
  "expected receipt" from `oms.inbound_notice.accepted`. The inbound notice list (`GET /inbound-notices?status=accepted`)
  is the dock plan.
- Added event `oms.inbound_notice.cancelled` (not in the spec's catalog) for portal webhooks later.
- One receipt per ASN. A split delivery arrives as a second ASN.
- One scanned receipt line is one pallet; `wms.receipt.completed` reports `pallets: 1` per line for handling-in charges.
- Clients may send ASNs to any active warehouse; client-to-site assignment arrives with the portal.
- Putaway suggestions count open tasks as occupied, but two receipts completing at the same instant can be
  offered the same empty bin. The operator then scans another bin with a reason.
