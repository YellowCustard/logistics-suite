"""M3 load test (Delivery Specification, test strategy: "250 vehicles posting every 30 s; 6 sites x 10 scanners with
1 scan every 5 s").

- VehicleUser (250): one telematics webhook push every 30 s per vehicle, signed like the fake adapter expects.
- ScannerUser (60): signs in like the PWA, pulls reference data once, then one scan every 5 s: mostly a stock move
  between bins L-A and L-B through offline sync, otherwise an incremental pull.

Fixtures: `manage.py seed_load --not-production` (see README, "Load test"). Environment for this file:
    LOAD_PASSWORD                operators' password (as given to seed_load)
    TELEMATICS_WEBHOOK_SECRET    the server's webhook secret
    LOAD_P95_MS / LOAD_P99_MS    pass thresholds per request type (defaults 500 / 1500)

The run fails (exit code 1) on any request failure or a request type over the thresholds.
"""

import hashlib
import hmac
import itertools
import json
import logging
import os
import random
import threading
import uuid
from datetime import UTC, datetime

from locust import FastHttpUser, constant_pacing, events, task

PASSWORD = os.environ.get("LOAD_PASSWORD", "")
SECRET = os.environ.get("TELEMATICS_WEBHOOK_SECRET", "")
P95_MS = float(os.environ.get("LOAD_P95_MS", "500"))
P99_MS = float(os.environ.get("LOAD_P99_MS", "1500"))
SITES, SCANNERS_PER_SITE, VEHICLES = 6, 10, 250
ORIGIN = (-26.20, 28.04)

_lock = threading.Lock()
_vehicle_numbers = itertools.count(1)
_scanner_numbers = itertools.count(0)


def _next(counter) -> int:
    with _lock:
        return next(counter)


class VehicleUser(FastHttpUser):
    fixed_count = VEHICLES
    wait_time = constant_pacing(30)

    def on_start(self):
        n = (_next(_vehicle_numbers) - 1) % VEHICLES + 1
        self.unit = f"LOAD-{n:03d}"
        self.lat = ORIGIN[0] + random.random() * 0.3  # noqa: S311 - simulation, not security
        self.lon = ORIGIN[1] + random.random() * 0.3  # noqa: S311

    @task
    def report_position(self):
        self.lat += random.uniform(-0.002, 0.002)  # noqa: S311
        self.lon += random.uniform(-0.002, 0.002)  # noqa: S311
        position = {
            "unit_id": self.unit,
            "time": datetime.now(UTC).isoformat(),
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "speed_kmh": round(random.uniform(0, 90), 1),  # noqa: S311
            "heading": round(random.uniform(0, 360), 1),  # noqa: S311
            "ignition": True,
        }
        body = json.dumps({"positions": [position]}).encode()
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        with self.client.post(
            "/api/v1/integrations/telematics/webhook",
            data=body,
            headers={"Content-Type": "application/json", "X-Telematics-Signature": signature},
            name="telematics webhook",
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"{response.status_code}: {response.text[:200]}")
            elif response.json().get("accepted") != 1:
                response.failure(f"position not accepted: {response.text[:200]}")


class ScannerUser(FastHttpUser):
    fixed_count = SITES * SCANNERS_PER_SITE
    wait_time = constant_pacing(5)

    def on_start(self):
        n = _next(_scanner_numbers) % (SITES * SCANNERS_PER_SITE)
        self.site_no, self.scanner_no = n // SCANNERS_PER_SITE + 1, n % SCANNERS_PER_SITE + 1
        self.device_id = f"load-{self.site_no}-{self.scanner_no:02d}"
        self.client.get("/scanner/", name="scanner shell")
        email = f"load-op-{self.site_no}-{self.scanner_no:02d}@load.invalid"
        with self.client.post(
            "/api/v1/auth/session",
            json={"email": email, "password": PASSWORD},
            headers=self._csrf(),
            name="sign in",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code}: {response.text[:200]}")
                self.stop()
                return
            self.site_id = response.json()["site_ids"][0]
        pulled = self.client.get(f"/api/v1/sync/pull?site_id={self.site_id}", name="sync pull (full)").json()
        self.cursor = pulled["cursor"]
        self.toward_b = self.scanner_no % 2 == 0

    def _csrf(self) -> dict:
        token = next((c.value for c in self.client.cookiejar if c.name == "csrftoken"), "")
        return {"X-CSRFToken": token, "Referer": self.host + "/scanner/"}

    @task(4)
    def scan_move(self):
        source, target = ("L-A", "L-B") if self.toward_b else ("L-B", "L-A")
        self.toward_b = not self.toward_b
        command = {
            "command_id": str(uuid.uuid4()),
            "type": "inventory.move",
            "recorded_at": datetime.now(UTC).isoformat(),
            "payload": {
                "site_id": self.site_id,
                "location_code": source,
                "sku": "LOAD-SKU",
                "to_location_code": target,
                "qty": "1",
            },
        }
        with self.client.post(
            "/api/v1/sync/push",
            json={"device_id": self.device_id, "commands": [command]},
            headers=self._csrf(),
            name="scan: stock move (sync push)",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code}: {response.text[:200]}")
                return
            result = response.json()["results"][0]
            if result["status"] != "applied":
                response.failure(f"{result['status']}: {json.dumps(result.get('problem'))[:200]}")

    @task(1)
    def scan_pull(self):
        response = self.client.get(
            f"/api/v1/sync/pull?site_id={self.site_id}&cursor={self.cursor}", name="scan: sync pull (incremental)"
        )
        if response.status_code == 200:
            self.cursor = response.json()["cursor"]


@events.init.add_listener
def check_configuration(environment, **kwargs):
    if not PASSWORD or not SECRET:
        logging.error("Set LOAD_PASSWORD and TELEMATICS_WEBHOOK_SECRET before running the load test.")
        environment.process_exit_code = 2


@events.quitting.add_listener
def judge(environment, **kwargs):
    """Pass only with no failures and every request type within the latency thresholds."""
    stats = environment.stats
    verdicts = []
    if stats.total.num_failures:
        verdicts.append(f"{stats.total.num_failures} failed requests")
    for entry in stats.entries.values():
        if entry.name in ("scanner shell", "sign in", "sync pull (full)"):
            continue  # once per device at start-up, not part of the steady load
        p95, p99 = entry.get_response_time_percentile(0.95), entry.get_response_time_percentile(0.99)
        if p95 > P95_MS or p99 > P99_MS:
            verdicts.append(f"{entry.name}: p95 {p95:.0f} ms, p99 {p99:.0f} ms")
    if verdicts:
        logging.error("LOAD TEST FAILED: %s", "; ".join(verdicts))
        environment.process_exit_code = 1
    else:
        logging.info("LOAD TEST PASSED (p95 <= %.0f ms, p99 <= %.0f ms, no failures)", P95_MS, P99_MS)
        environment.process_exit_code = 0
