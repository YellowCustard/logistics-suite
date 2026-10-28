from django.core.management.base import BaseCommand, CommandError

from apps.integrations.telematics import adapter
from apps.tms import telematics


class Command(BaseCommand):
    help = (
        "Link the telematics vendor's units to fleet vehicles by registration (spaces and dashes ignored). "
        "Vehicles already linked to another unit are reported, not changed. Safe to run more than once."
    )

    def handle(self, *args, **options):
        port = adapter()
        if port is None:
            raise CommandError("TELEMATICS_ADAPTER is not set.")
        result = telematics.sync_vehicles(port)
        self.stdout.write(
            f"linked={len(result.linked)} unchanged={result.unchanged} "
            f"unmatched={len(result.unmatched)} conflicts={len(result.conflicts)}"
        )
        for label, rows in (("linked", result.linked), ("unmatched", result.unmatched), ("conflict", result.conflicts)):
            for registration in rows:
                self.stdout.write(f"  {label}: {registration}")
