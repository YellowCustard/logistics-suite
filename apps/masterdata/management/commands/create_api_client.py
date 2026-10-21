import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.masterdata.services import is_client
from apps.platform.auth import create_api_client


class Command(BaseCommand):
    help = "Create API credentials for a client. The secret is printed once and never stored."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--client-id", required=True, type=uuid.UUID)

    def handle(self, *args, name: str, client_id: uuid.UUID, **options):
        if not is_client(client_id):
            raise CommandError("client-id is not an active client party")
        api_client, secret = create_api_client(name=name, client_id=client_id)
        self.stdout.write(f"client_id={api_client.key_id}\nclient_secret={secret}")
