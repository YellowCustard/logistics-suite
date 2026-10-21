from django.core.management.base import BaseCommand

from apps.platform import exceptions_queue
from apps.platform.models import Severity
from apps.wms import ledger


class Command(BaseCommand):
    help = "Check every inventory balance against its movement ledger; open an exception case on drift."

    def handle(self, *args, **options):
        drifted = list(ledger.drift()[:1000])
        for balance in drifted:
            exceptions_queue.raise_case(
                kind="inventory.ledger_drift",
                severity=Severity.CRITICAL,
                summary=f"Balance {balance.pk} shows {balance.qty_on_hand} but its ledger sums to {balance.ledger}",
                detail={
                    "balance_id": str(balance.pk),
                    "on_hand": str(balance.qty_on_hand),
                    "ledger": str(balance.ledger),
                },
                client_id=balance.client_id,
                entity_type="wms.inventorybalance",
                entity_id=str(balance.pk),
                dedupe_key=f"inventory.ledger_drift:{balance.pk}",
            )
        self.stdout.write(f"{len(drifted)} drifted balance(s)")
        return None
