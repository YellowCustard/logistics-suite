from django.core.management.base import BaseCommand

from apps.platform.models import OutboxEvent
from apps.tms import shipments

REPLAYED = ("oms.sales_order.accepted", "oms.sales_order.cancelled", "wms.handling_unit.closed")


class Command(BaseCommand):
    help = (
        "Build shipments for orders accepted before transport was installed, by replaying their order and packing "
        "events from the outbox in the order they happened. Safe to run more than once."
    )

    def handle(self, *args, **options):
        counts = dict.fromkeys(REPLAYED, 0)
        for event in OutboxEvent.objects.filter(type__in=REPLAYED).order_by("occurred_at", "event_id").iterator():
            if event.type == "oms.sales_order.accepted":
                shipments.create_from_order(client_id=event.client_id, site_id=event.site_id, payload=event.payload)
            elif event.type == "oms.sales_order.cancelled":
                shipments.cancel_for_order(event.payload["sales_order_id"])
            else:
                shipments.attach_unit(event.payload)
            counts[event.type] += 1
        self.stdout.write(", ".join(f"{n} {kind}" for kind, n in counts.items()))
