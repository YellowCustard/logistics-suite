# Nuronteq

Nuronteq: contract warehousing, transport and billing for a South African 3PL. A Django modular monolith on one
PostgreSQL database, built milestone by milestone from the Phase 1 Delivery Specification. Done so far:
M0 (platform services, master data, bins), M1 (ASNs, receiving, inventory ledger, putaway, cycle counts,
the offline scanner PWA), M2 (sales orders, allocation, waves, picking, packing, webhooks) and most of
M3 (shipments, trips, load scan, dispatch, proof of delivery, telematics, and the client portal's first screens).

## Layout

| Path | What it is |
|---|---|
| `apps/platform` | Bottom layer: users and roles, principals and scoping, API tokens, audit log, transactional outbox, idempotency, documents, exception queue, business numbers, CSV import engine, outbound webhooks (`webhooks.py`) |
| `apps/masterdata` | Clients and other parties, sites, items (UoMs, barcodes), vehicles, drivers, contracts, versioned rate cards |
| `apps/oms` | Inbound notices (ASNs and returns) and outbound sales orders (API, CSV import, cancellation) |
| `apps/wms` | Bins, receiving, the inventory ledger (`ledger.py`, the only code that changes stock), putaway, allocation (`allocation.py`, FEFO then FIFO), waves and picking (`picking.py`), packing into handling units with SSCC labels (`packing.py`, `sscc.py`, `labels.py`) |
| `apps/tms` | Transport: shipments built from order and packing events (`shipments.py`), trips with stops, own-fleet or carrier assignment, load scanning and dispatch (`trips.py`), arrival, proof of delivery, failed delivery and trip completion (`delivery.py`, also as offline sync commands), vehicle positions, geofence arrival and ETA (`telematics.py`) |
| `apps/integrations` | Vendor adapters behind the protocols in `ports.py`. `telematics/fake.py` stands in for Powerfleet MiX until credentials arrive |
| `apps/portal` | Serves the client portal page at `/portal/` (the built React app, with a strict CSP); no business logic |
| `portal` | Client portal source: React, TypeScript and Vite. Builds into `apps/portal/static/portal`; API types are generated from the OpenAPI schema (`src/api/schema.d.ts`) |
| `loadtest` | Locust load test (`locustfile.py`); fixtures from `manage.py seed_load`, results in `docs/` |
| `config` | Settings (environment-driven), URLs, API root |
| `tests` | pytest suite against real PostgreSQL, including the M0 exit test (onboarding through CSV and the API) and the M2 exit test (`test_m2_exit.py`: a simulated week of 20 orders a day received, picked and packed through the API only) |

Module boundaries are enforced by import-linter (`pyproject.toml`): `platform` imports no business module,
`masterdata` imports no other business module, `oms` never imports `wms`, and `wms` reaches `oms` only through
`oms.services`. `tms` learns about orders and packing only from events and imports neither `oms` nor `wms`;
neither of them imports `tms`. Business modules reach master data only through its models and `services`.
`integrations` holds vendor adapters and imports no business module; the business module that needs one calls it.

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

The client portal needs Node 22 and a build once (the container image builds it itself):

```bash
cd portal && npx -y npm@11.21.0 ci && npm run build   # then open /portal/ on runserver
npm run dev                              # or: live reload on :5173, proxying /api to runserver on :8000
```

Or everything in containers: `cp .env.example .env`, fill in the secrets, then `docker compose up --build`.

Staging and production run on a self-managed xneelo Cloud instance: see [docs/staging.md](docs/staging.md).

## Checks

```bash
uv run ruff format --check . && uv run ruff check .
uv run lint-imports
uv run python manage.py makemigrations --check --dry-run
uv run pytest                            # needs a reachable PostgreSQL (DATABASE_URL); creates test_logistics
cd portal && npm test && npm run build      # portal: vitest, then type-check and build
cd portal && npm run api-types            # after any API change: regenerate src/api/schema.d.ts and commit it
```

CI (`.github/workflows/ci.yml`) runs all of the above plus `check --deploy`, a check that the portal's API types
match the API, and a container build.

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
- Webhooks: a client admin or the client's own system subscribes an `https` URL to event types
  (`GET /webhooks/events` lists them) with `POST /webhooks`; the signing secret is returned once. Each delivery
  is a `POST` of `{id, type, version, occurred_at, client_id, data}` with headers `Nuronteq-Event`,
  `Nuronteq-Delivery` and `Nuronteq-Signature: t=<unix>,v1=<hex>`, where `v1` is HMAC-SHA256 of
  `"<t>." + body` keyed by the secret. Receivers should check the signature, reject a `t` older than five
  minutes, and de-duplicate on `id` (delivery is at least once). Any 2xx is success; redirects are not followed.

## Runbook

| Task | How |
|---|---|
| Event delivery | `manage.py run_outbox` runs continuously (the `outbox` service in Compose). It stops cleanly on SIGTERM. |
| Failed events | After `OUTBOX_MAX_ATTEMPTS` (8) a delivery is dead-lettered and an exception case `outbox.dead_letter` opens. Fix the cause, then use the **Replay** action on Event deliveries in the admin. |
| Outbox lag | `GET /api/v1/readyz` reports `outbox_lag_seconds`; alert above 300. |
| Webhooks | `run_outbox` also sends webhook deliveries. A failed delivery retries with backoff (30 s doubling, at most 6 h apart) up to `WEBHOOK_MAX_ATTEMPTS` (10, about 15 hours), then goes dead and opens a `webhook.dead_letter` case; once the receiver is fixed, a superuser uses **Send selected dead deliveries again** under Webhook deliveries in the admin. Clients see their delivery history at `GET /webhooks/{id}/deliveries` and can test with `POST /webhooks/{id}/ping`. Disabling a subscription (`DELETE /webhooks/{id}`) drops its queued deliveries quietly. Only public addresses are called: every DNS answer must be a global address and the connection goes to the address that was checked. |
| Idempotency cleanup | Schedule `manage.py purge_idempotency` daily (cron or a scheduled container run). |
| Encryption key rotation | Generate a key, put it **first** in `FIELD_ENCRYPTION_KEYS`, keep the old ones after it, deploy. Old values still decrypt and are re-encrypted on their next save. Never drop a key while rows may still use it. |
| Inventory reconciliation | Schedule `manage.py reconcile_inventory` nightly. It compares every balance with its movement ledger and opens a critical `inventory.ledger_drift` case on any difference, and checks each balance's allocated quantity against its open allocations (`inventory.allocation_drift`). |
| Waves and picking | A supervisor plans a wave from allocated orders (`POST /waves`, up to 200) and releases it (`POST /waves/{id}/release`). Release moves the orders to `picking` (no more cancellation), drops any that were cancelled or went short since planning, and creates one pick task per allocation. Operators pick on the scanner in bin order: scan bin, scan item, confirm the quantity. A short pick (`POST /pick-tasks/{id}/short`, reason required) takes what was found, looks for the rest in other bins and adds those tasks to the same wave; if nothing is left it emits `wms.allocation.shorted` and opens a high `pick.short` case. Every short also opens an `inventory.short_pick` case to count that bin. The wave completes when every task is done or short (`wms.pick_wave.completed`). |
| Packing | At the packing bench (scanner **Pack**, online only) an operator starts a carton or pallet for an order in picking (`POST /handling-units`); it gets its SSCC at once. Scans add picked stock (`POST /handling-units/{id}/contents`, item barcode or SKU, base units, lot only when more than one was picked); nothing can be packed beyond what was picked. Closing (`POST /handling-units/{id}/close`) needs the weight, and the carton count for a pallet. Closed units can't change (database trigger). The label is `GET /handling-units/{id}/label` (4x6 inch PDF) or `?format=zpl` for Zebra printers. When picking is finished and everything picked is in closed units, the order becomes `packed` (`oms.sales_order.packed`). |
| Trips and dispatch | Every accepted order becomes a draft shipment; it is `ready` once its last unit is closed. A dispatcher starts a trip at a warehouse (`POST /trips`), sets its drop stops in visiting order (`PUT /trips/{id}/stops`; stop 1, the pickup, is added), and assigns either an own vehicle and driver or a carrier with the agreed cost (`POST /trips/{id}/assign`; expired licence disc, roadworthy, licence or PrDP is refused). Loaders scan each unit's SSCC onto the trip (scanner **Load**, online only) (`POST /trips/{id}/load`, undo with `DELETE /trips/{id}/load/{sscc}`). Dispatch (`POST /trips/{id}/dispatch`) needs every shipment packed and every unit loaded, and the vehicle and driver back from any earlier trip; it emits `tms.trip.dispatched`, and each order becomes `dispatched` (`oms.sales_order.dispatched`, offered as a webhook). The board is `GET /trips?site_id=&on=`. |
| On the road | The driver (a user with the `driver` role, linked from their Person record) sees their trips at `GET /driver/trips`. At a stop: arrive (`POST /trips/{id}/stops/{stop}/arrive`), upload the signature and photos (`…/evidence`, multipart, kind `pod_signature` or `pod_photo`; the same bytes again return the same document), then record the POD (`…/pod`: recipient name, the documents, and every unit scanned with `delivered`, `damaged` or `refused`; unscanned units count as missing) or a failure (`…/failed`: reason code, note, photos). `POST /trips/{id}/complete` once every stop is recorded: failed shipments come back unloaded and ready to plan. The same actions are sync commands (`trip.arrive`, `trip.pod`, `trip.failed`, `trip.complete`) and carry the device's time. A dispatcher at the site can do any of it, for example keying in a carrier's paper POD (leave `units` out to mark all delivered). In the PWA (`/scanner/`), a user whose only role is `driver` gets the driver menu instead of the warehouse one: their trips, stops with a maps link, arrive, deliver (scan units, recipient, finger signature, photos) and could-not-deliver. It all works offline; signatures and photos wait in IndexedDB (photos shrunk to 1600 px JPEG) and upload just before the POD that uses them. Exceptions open `delivery.exception` or `delivery.failed` cases; clients get `oms.sales_order.delivered` and `tms.shipment.delivery_failed` webhooks. |
| Carrier drivers | For a carrier trip, a dispatcher creates a sign-in link (`POST /trips/{id}/carrier-links`, 1 to 168 hours, default 48) and sends it to the driver. The link is shown once; only its SHA-256 is stored, and a new link replaces the old one. Opening it signs the driver in to the driver PWA for that trip only (role `carrier_driver`, no site or client access), until the link expires, the trip finishes, or `DELETE /trips/{id}/carrier-links` revokes it (which also signs the driver out). |
| Tracking | `GET /shipments/{id}/tracking` (clients see their own; staff their sites): status, trip, stops still ahead, and a timeline (received, packed, dispatched, arrived, delivered or failed). `GET /shipments/{id}/pod` gives the latest attempt: recipient, time, GPS, reason, unit outcomes and the evidence documents, which download from `/documents/{id}/download`. `eta` is filled while the trip is on the road and its vehicle reported in the last 15 minutes. |
| Client portal | Client users (roles `client_admin`, `client_user`, created in the admin with a password and their client) sign in at `/portal/`. Staff are refused there and use the scanner and the admin; client users are refused on the scanner. **Home**: open orders by status, deliveries on the road and delivered today, a stock summary, and open cases that affect the client (`GET /client/exceptions`: short orders, receipt differences, delivery problems, failing webhooks; never internal stock checks). **Orders**: search and filter, detail with the status timeline (`GET /orders/{id}/timeline`, from the audit log) and a link to the shipment, cancel with a reason until picking starts, a form for one order, and CSV upload with the problems listed by row. **Inbound**: notices (list, create, CSV upload, cancel until receiving starts) with expected against received per line, the receipt's lines with condition, items that weren't on the notice, and photos of damaged stock (clients may read their receipts: `GET /receipts?inbound_notice_id=`). **Stock**: on hand, allocated and available per warehouse, item, lot and expiry (`GET /inventory/summary`), with CSV export. **Tracking**: shipments newest first; a shipment shows the timeline, ETA while on the road, and the proof of delivery with the signature and photos to view and download. **Items**: the catalogue with search by SKU, barcode or description and CSV export; client admins add items (form or CSV), edit description, weight, volume and pallet quantity (`PATCH /items/{id}` with `If-Match: <version>`), add pack sizes (`POST /items/{id}/pack-sizes`), add and remove barcodes (`POST /items/{id}/barcodes`, `DELETE /items/{id}/barcodes/{barcode_id}`) and make items inactive; client users read only. **Documents**: PODs, receipt photos and uploaded files, newest first, filtered by type and capture date and searched by order, shipment, notice or receipt number or the client's own reference (`GET /documents`). Everything it shows comes from `/api/v1` under the client user's own scope. |
| Telematics | `TELEMATICS_ADAPTER` names the vendor adapter; the `telematics` service polls it every `TELEMATICS_POLL_SECONDS` and idles when it is empty. Vendors that push send to `POST /api/v1/integrations/telematics/webhook`, checked by the adapter's signature (the fake adapter: HMAC-SHA256 of the body with `TELEMATICS_WEBHOOK_SECRET` in `X-Telematics-Signature`). `manage.py sync_telematics_vehicles` links vendor units to vehicles by registration; a unit no vehicle is linked to opens a `telematics.unknown_vehicle` case. A dispatched vehicle at walking pace inside a drop stop's geofence (`Site.geofence_radius_m` around the site's coordinates) marks the stop arrived; set `GEOFENCE_AUTO_ARRIVE=0` to turn that off. Staff trip detail shows the last fix and an ETA per stop. Positions are kept `TELEMATICS_RETENTION_DAYS` (395). |
| Fleet simulator (demo only) | With dispatched own-fleet trips and coordinates on their sites: `deploy/ops.sh manage simulate_fleet --link` drives each vehicle toward its next stop with a fix every 30 s, through the same ingest as live data, until Ctrl-C. `--link` gives unlinked vehicles a `sim-<registration>` telematics ID; clear those before go-live. |
| Load test | Against a staging-like stack, never production: `manage.py seed_load --not-production --history-hours 24` (6 load sites, 60 operators sharing `LOAD_PASSWORD`, 250 vehicles on units LOAD-001..250, a day of positions), then `uv run --group load locust -f loadtest/locustfile.py --headless -H https://<host> -u 310 -r 5 -t 15m`. Set `LOGIN_RATE_LIMIT=1000/m` on the server for the run. It fails on any error or a p95/p99 over `LOAD_P95_MS`/`LOAD_P99_MS`. M3 results: `docs/loadtest-m3.md`. |
| Transport backfill | Once, after the first deploy that includes `apps.tms`: `manage.py backfill_shipments` builds shipments for orders accepted earlier by replaying their events from the outbox. Running it again changes nothing. |
| SSCC prefix | Set `GS1_COMPANY_PREFIX` (from GS1 South Africa) and `SSCC_EXTENSION_DIGIT`. Without a prefix, packing refuses to start units (503). SSCC serials come from one counter that never resets. Until the GS1 membership is through, staging can use `0000000`; those labels are for testing only. |
| Allocation | Every accepted order allocates at once: available stock in storage and pick-face bins only, earliest expiry first, then oldest arrival, then pick path. Expired, quarantined and dock stock is never allocated. A short order keeps what it got, becomes `backordered`, emits `wms.allocation.shorted` and opens one `allocation.short` case. After new stock is put away, a supervisor tops backorders up with `POST /allocations:run` (`site_id`, optional `sales_order_ids`); the case resolves when the order is full. Cancelling an order releases its reservations. |
| Cycle counts | A supervisor opens a count on a set of bins (`POST /cycle-counts`). Operators scan what they find; they see only their own lines, never system quantities. Uncounted stock in those bins counts as zero on submit. Variances within `INVENTORY_VARIANCE_APPROVAL_PCT` (default 2%) post at once; anything larger, or any stock found in an empty bin, waits for a site manager who neither counted nor submitted it. |
| Stock corrections | `POST /inventory/adjustments` (supervisors, reason required) posts immediately. A correction over the same threshold opens an `inventory.adjustment_review` case so a site manager reviews it after the fact. Every adjustment emits `wms.inventory.adjusted`. Bin-to-bin moves (`POST /inventory/moves`) are open to operators because they don't change quantities. |
| Scanner | Operators open `/scanner/` on the device and sign in with their user (session cookie plus CSRF; 10 attempts a minute per address and worker, `LOGIN_RATE_LIMIT`). Receiving, putaway, picking, counting and moves work offline: actions queue on the device and send in order when it reconnects. Refused actions show under **Activity** with the reason and changed nothing. |
| Scanner sync | `POST /api/v1/sync/push` applies queued commands once each; every command is kept in `platform_synccommand` with its device and outcome. `GET /api/v1/sync/pull?site_id=&cursor=` serves reference data and open work; incremental pulls overlap by `SYNC_PULL_OVERLAP_SECONDS` (300). |
| ASN upload | `POST /api/v1/inbound-notices:import` takes a CSV (`client_ref, site_code, expected_at, sku` or `barcode`, `qty`, optional `uom, lot, expiry, kind, notes`). Rows sharing a `client_ref` become one ASN. One bad row rejects the whole file with row numbers; the file is kept as an import document. Staff add `?client_id=`. |
| Receipt variances | Over, short or unexpected receipt lines open a `receipt.variance` case with the line-by-line detail. |
| Audit log | Append-only, enforced by a database trigger. Read it in the admin under Audit entries. |
| Rate cards | Published cards and their lines are immutable (database triggers). Changes mean a new version; publishing an open-ended newer version closes the older one the day before. |
| Backups | On the VPS, `deploy/ops.sh backup` runs nightly: a `pg_dump` plus the document volume, encrypted by restic and kept on the server (staging) or off-site in S3 (required for production). Rehearse `deploy/ops.sh restore-drill` monthly. See [docs/staging.md](docs/staging.md). |

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
- Direct stock corrections post immediately and are reviewed afterwards (a case, not a block), because a picker
  waiting on a manager stalls the floor. Count variances above the threshold are blocked until approved.
- Added `POST /cycle-counts/{id}/reject` (with a note) alongside approve; a rejected count frees its bins for a recount.
- `wms.inventory.adjusted` also carries `qty_before`, so consumers can judge the size of a correction without a lookup.
- A count line records the system quantity when it is counted, so stock moved in or out of the bin before
  submission doesn't show up as a variance. Bins in an open count are not frozen.
- The scanner is plain HTML and JavaScript served by Django (no build step), with a service worker for the
  shell and IndexedDB for the queue and reference data. Camera scanning, printing and device-specific
  features wait for the device spike; keyboard-wedge scanners work as they are.
- Damaged goods aren't received on the scanner yet: they need photos, which arrive with the device spike.
  Receive them through the API.
- Offline commands carry their own ids and are applied once each, so `/sync/push` doesn't take an
  `Idempotency-Key`. A refused command is final; the operator scans again, which is a new command.
- A device pushes only the signed-in user's commands, so a shared scanner never sends one person's work
  under another's name.

## Deliberate deviations and assumptions (M2)

- OMS doesn't check stock when it accepts an order (that would make OMS read WMS). Allocation runs from
  `oms.sales_order.accepted`, and an order that can't be filled becomes `backordered` a moment later.
- Webhooks are sent by the outbox worker rather than a separate job queue, and carry the event payload as is
  (the event catalog is the webhook contract). Only events about a client go to that client; internal events
  such as waves and pick tasks are not offered. A dead delivery isn't replayed automatically: the client
  pings to test the fix, then a superuser sends it again from the admin (Webhook deliveries).
- A short order keeps a partial allocation rather than holding nothing, so stock isn't taken by a later order.
  Backorders are topped up by a supervisor run, not automatically on every putaway.
- Allocation and cancellation lock the order row, so an order cancelled while allocating releases cleanly; a late
  allocation result never changes a cancelled order.
- Picked stock leaves the inventory ledger at the bin (movement kind `pick`). Packing and loading track it from
  there; there is no separate "picked" stock location.
- Only fully allocated orders can join a wave. A backordered order waits for a top-up rather than shipping part.
- One confirmation per pick task, for its full quantity. Anything less is a short pick.
- After a short pick the ledger still shows the missing stock in that bin until someone counts it (the
  `inventory.short_pick` case). Re-allocation for that pick skips the bin, but other orders could still be
  allocated from it, so count promptly.
- Pick tasks are listed at `GET /pick-tasks` (not `GET /tasks`, which stays putaway-only), because they carry
  different fields. `POST /tasks/{id}/claim` claims either kind.
- A handling unit holds one order. Its SSCC is assigned when it is opened, so an abandoned open unit leaves a gap
  in the serials (harmless; SSCCs only need to be unique).
- Packing is online only: the SSCC and the checks against picked quantities live on the server, and a packing bench
  has a printer and a connection anyway.
- Labels are built without a PDF or barcode library: GS1-128 (Code 128 set C, FNC1, AI 00) drawn as vector bars,
  checked by decoding rendered labels with zxing during development.
- Every pick locks its wave row, so picks within one wave run one at a time. At pilot volumes that's invisible,
  and it guarantees the wave completes exactly once.

## Deliberate deviations and assumptions (M3)

- One shipment per sales order, created from `oms.sales_order.accepted`; there is no `POST /shipments`. Splitting an
  order over two trips, or consolidating orders into one shipment, waits for a real need.
- TMS keeps its own copy of what it needs from orders and handling units (lines, SSCCs, weights) rather than reading
  OMS or WMS tables. Its view can lag the warehouse by one outbox cycle.
- A shipment can be planned onto a trip before it is packed; dispatch waits for packing. A shipment rides on one
  active trip at a time, and taking it off a trip needs its units unloaded first.
- Stops are replaced as a whole while planning (stop ids change on each `PUT`), and frozen at dispatch.
- Double booking of a vehicle or driver is checked at dispatch (not while planning), against trips still out.
- A cancelled order leaves any trip still being planned; stops left empty are removed and the rest renumbered.
- A POD is per stop. A delivery address belongs to one client, so a stop's evidence belongs to that client.
- Outcomes are per handling unit (delivered, damaged, refused, missing), not per line: delivered quantities are the
  contents of the units the recipient kept. A partly refused carton isn't recorded; refuse the unit and add a note.
- A shipment is delivered if the recipient kept any of its units; refused or missing units open a
  `delivery.exception` case and are not re-delivered automatically. A failed shipment is planned again only after
  its trip is completed (the goods are back at the warehouse).
- Units packed before this release have no contents on record, so their delivered quantities are empty.
- Device times are accepted up to 10 minutes in the future and back to 10 minutes before dispatch; anything else
  means the device clock is wrong and the action is refused.
- Loading warns, but doesn't refuse, when the loaded weight exceeds the vehicle's rated capacity.
- A carrier driver signs in through a link instead of an account. The user it creates is internal
  (`carrier-link-…@links.invalid`, no password) and is disabled when the link is replaced or revoked.
- Telematics is built against a fake adapter. The MiX Integrate adapter will be written from its API documentation
  once credentials arrive; nothing in `tms` changes when it does.
- Geofences are circles (site coordinates plus `geofence_radius_m`, haversine distance), not PostGIS polygons. A
  fix only counts as an arrival at 15 km/h or less, so passing a later stop on the way to an earlier one doesn't mark
  it. Fixes from before dispatch are ignored.
- ETA is straight-line distance times `ETA_ROAD_FACTOR` (1.3) at `ETA_AVERAGE_KMH` (40), plus `ETA_MINUTES_PER_STOP`
  (15) at each stop before, until a routing engine is justified. It is withheld when the last fix is over 15 minutes
  old. Clients see the ETA only; the vehicle's position is shown to staff.
- On TimescaleDB, positions are a hypertable (1-day chunks, compressed after 7 days, dropped after 395 days); its
  primary key is `(id, recorded_at)`. On plain PostgreSQL the poller deletes expired positions hourly.
- The client portal is a React app built with Vite, served by Django from the same origin with session auth and
  CSRF (no tokens in the browser, no CORS). Node is needed only to build it. It is being built out in steps: Home,
  Orders, Inbound, Stock, Tracking, Items and Documents so far. Statements wait for billing (M4).
- Item SKU, base unit and lot/expiry tracking can't be edited (stock and open orders depend on them), and pack sizes
  can be added but not changed or removed. Inactive items stay listed but new orders and notices can't use them.
- Barcodes are 1 to 64 printable characters without spaces. One that is all digits with the length of a GTIN-8,
  UPC-A, EAN-13 or GTIN-14 must carry the right GS1 check digit (scanners verify it on the printed code, so a
  mistyped number could never match a scan); this applies to the item CSV import too. Barcodes created before this
  rule, such as the demo data's, are left as they are.
- Document search matches the start of a reference. Modules describe and search their own document links through
  `apps.platform.document_links` (registered in each module's `document_links.py`), so platform never reads
  business tables.
- Clients can list sites (`GET /sites`): every active warehouse and their own delivery addresses, so they can order
  from any warehouse. An order from a warehouse that holds none of their stock is backordered, as by API.
- New orders and inbound notices start at the warehouse holding most of the client's stock lines.
- Expected arrival times typed in the portal are South African time (UTC+2, no daylight saving).
- Order counts on Home include every open order but only orders delivered or cancelled in the last 7 days.
- Stock CSV exports are built in the browser from `GET /inventory/summary` (up to 5000 rows; narrow by warehouse or
  SKU beyond that). Cells starting with `=`, `+`, `-` or `@` get a leading apostrophe so spreadsheets don't run them.
- `POST /auth/session` takes `app` (`scanner`, the default, or `portal`): each app accepts only its own kind of user.
- Positions are de-duplicated on (vehicle, fix time). Positions with no GPS lock (0, 0), out-of-range or non-finite
  coordinates, or times more than 10 minutes ahead are dropped.
