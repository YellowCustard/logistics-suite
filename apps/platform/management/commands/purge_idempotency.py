from django.core.management.base import BaseCommand

from apps.platform.idempotency import purge_expired


class Command(BaseCommand):
    help = "Delete expired idempotency records (run daily)."

    def handle(self, *args, **options):
        self.stdout.write(f"deleted={purge_expired()}")
