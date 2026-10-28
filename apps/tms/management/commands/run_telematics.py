import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.integrations.telematics import adapter
from apps.tms import telematics

log = logging.getLogger(__name__)

PURGE_EVERY_SECONDS = 3600


class Command(BaseCommand):
    help = (
        "Poll the telematics vendor for vehicle positions every TELEMATICS_POLL_SECONDS and delete positions past "
        "TELEMATICS_RETENTION_DAYS (loop until SIGTERM, or --once). Idles when no adapter is configured."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Poll once, then exit.")

    def handle(self, *args, once: bool, **options):
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True
            log.info("telematics poller stopping", extra={"data": {"signal": signum}})

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        port = adapter()
        if port is None:
            log.info("telematics poller idle: TELEMATICS_ADAPTER is not set")
        next_purge = 0.0
        failures = 0
        while not stopping:
            if not once:
                close_old_connections()  # drop a connection the database closed while we slept
            delay = settings.TELEMATICS_POLL_SECONDS
            if port is not None:
                try:
                    result = telematics.poll(port)
                    failures = 0
                    if once:
                        self.stdout.write(
                            " ".join(f"{k}={v}" for k, v in result.stats.as_dict().items())
                            + f" until={result.until.isoformat()}"
                        )
                except Exception:
                    # Vendor or network trouble: keep the cursor, back off up to ten intervals, try again.
                    failures += 1
                    delay = min(delay * 2**failures, delay * 10)
                    log.exception("telematics poll failed", extra={"data": {"failures": failures}})
                    if once:
                        raise
            if time.monotonic() >= next_purge:
                deleted = telematics.purge_positions()
                if deleted:
                    log.info("telematics positions purged", extra={"data": {"deleted": deleted}})
                next_purge = time.monotonic() + PURGE_EVERY_SECONDS
            if once:
                return
            deadline = time.monotonic() + delay
            while not stopping and time.monotonic() < deadline:
                time.sleep(1)
