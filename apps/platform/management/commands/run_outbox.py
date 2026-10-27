import dataclasses
import logging
import signal
import time

from django.core.management.base import BaseCommand

from apps.platform import outbox, webhooks

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Publish outbox events, run consumer deliveries and send webhooks (loop until SIGTERM, or --once)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process what is due, then exit.")
        parser.add_argument("--idle-sleep", type=float, default=1.0)

    def handle(self, *args, once: bool, idle_sleep: float, **options):
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True
            log.info("outbox worker stopping", extra={"data": {"signal": signum}})

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            published = outbox.publish_pending()
            stats = outbox.deliver_pending()
            sent = webhooks.send_due()
            busy = published or stats.delivered or stats.duplicates or stats.retried or stats.dead
            busy = busy or sent.delivered or sent.retried or sent.dead
            if busy:
                data = {"published": published, **dataclasses.asdict(stats), "webhooks": dataclasses.asdict(sent)}
                log.info("outbox cycle", extra={"data": data})
            if once:
                self.stdout.write(
                    f"published={published} delivered={stats.delivered} "
                    f"duplicates={stats.duplicates} retried={stats.retried} dead={stats.dead} "
                    f"webhooks_delivered={sent.delivered} webhooks_retried={sent.retried} webhooks_dead={sent.dead}"
                )
                return
            if not busy:
                time.sleep(idle_sleep)
