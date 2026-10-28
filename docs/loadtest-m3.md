# M3 load test: 250 vehicles and 60 scanners

**Result: passed on 2026-10-28.** There were no failed requests, and every steady-state request type had p95 at or below 210 ms and p99 at or below 250 ms. The thresholds were 500 ms and 1,500 ms.

## What ran

The test follows the Delivery Specification's test strategy: "250 vehicles posting every 30 s; 6 sites × 10 scanners with 1 scan every 5 s".

| Load | Rate | What each request does |
|---|---|---|
| 250 vehicles | one push every 30 s each (8.2/s) | A signed telematics webhook with one position. It is stored in the hypertable, the unit is looked up, and geofence arrival is checked. |
| 60 scanners (6 sites × 10) | one scan every 5 s each (11.6/s) | 80% are a stock move between bins through `/sync/push`, which writes two ledger entries, an outbox event and an audit trail. 20% are an incremental `/sync/pull`. |

- **Duration:** 15 minutes at full load, after a 1-minute ramp (5 users/s).
- **Starting data:** the positions table already held 24 hours of history for all 250 vehicles (720,000 rows, 172 MB). Lookups and inserts therefore ran against a full day's volume.
- **Note on the specification:** the spec says "24 h of simulated time". This run covers that as data volume, not as a 24-hour wall-clock run.

**Stack.** Everything ran on one 4 vCPU / 15 GB machine, including Locust itself. That makes the result pessimistic compared with the staging VPS, where Locust would run elsewhere.

- Caddy 2.10 terminating TLS
- gunicorn with 3 sync workers (the Dockerfile default)
- `DJANGO_DEBUG=0`
- TimescaleDB 2.31 on PostgreSQL 17
- The outbox worker and the telematics poller

## Results (steady load)

| Request | Count | Failures | p50 | p95 | p99 | Max |
|---|---:|---:|---:|---:|---:|---:|
| Telematics webhook | 7,363 | 0 | 95 ms | 180 ms | 240 ms | 480 ms |
| Scan: stock move (sync push) | 8,467 | 0 | 120 ms | 210 ms | 250 ms | 320 ms |
| Scan: incremental pull | 1,991 | 0 | 110 ms | 190 ms | 250 ms | 330 ms |
| *Once per device:* sign in | 60 | 0 | 780 ms | 980 ms | 1,000 ms | 1,000 ms |
| *Once per device:* full pull | 60 | 0 | 44 ms | 260 ms | 280 ms | 280 ms |

Sign-in is slow on purpose: password hashing uses PBKDF2 with 1,000,000 iterations. It happens once per device per shift, so it is left out of the thresholds.

**Postgres, sampled every 30 s:**
- Connections: 7 open, at most 2 active.
- Lock waits: none.
- Deadlocks: no new ones.
- Outbox backlog: 0 the whole time.
- Positions table: grew from 172 MB to 174 MB.
- Load average stayed under 1.2.

The app logs had no ERROR lines.

## What the first run found

A one-minute smoke run failed with **database deadlocks on stock moves**.

- **Cause:** two scanners moving the same stock in opposite directions (L-A to L-B and L-B to L-A) each locked their source balance, then waited for the other's.
- **Fix:** `ledger.transfer` now locks both balances in id order before posting.
- **Regression test:** `tests/test_ledger_concurrency.py` checks that lock order, and fails without the fix. A threaded reproduction also deadlocked before the fix and passed after it, but it can't live in the suite: it needs a flushed database, and the append-only audit table rules that out.

The same smoke run spawned 30 users a second. That made all 60 sign-ins land at once, which tied up the three workers for about 20 seconds on password hashing. Real scanners sign in over minutes, so the 15-minute run used a ramp of 5 users a second.

## Sizing notes

- Positions grow by about 172 MB a day uncompressed (720,000 rows), so about 1.2 GB before compression starts at 7 days.
- Compressed chunks are typically a small fraction of that; I did not measure the ratio here.
- Plan staging and production disk for about 2 GB of positions plus compressed history up to the 395-day retention. Re-measure after a week on staging with `SELECT * FROM hypertable_compression_stats('tms_vehicleposition')`.
- At about 20 requests a second, three gunicorn workers had plenty of headroom, so no tuning was needed.

## How to repeat it

```bash
# Server side (a staging-like stack, never production): raise the sign-in limit for the run and set a webhook secret.
export LOGIN_RATE_LIMIT=1000/m TELEMATICS_ADAPTER=apps.integrations.telematics.fake.FakeTelematics
export TELEMATICS_WEBHOOK_SECRET=<random> LOAD_PASSWORD=<random, 12+ chars>
uv run python manage.py seed_load --not-production --history-hours 24

# Load generator (same LOAD_PASSWORD and TELEMATICS_WEBHOOK_SECRET):
uv run --group load locust -f loadtest/locustfile.py --headless -H https://<host> -u 310 -r 5 -t 15m --only-summary
```

The run exits with code 1 in any of these cases:
- a request fails;
- a steady-state request type exceeds `LOAD_P95_MS` (default 500 ms);
- a steady-state request type exceeds `LOAD_P99_MS` (default 1,500 ms).
