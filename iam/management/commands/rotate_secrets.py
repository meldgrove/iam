"""Re-encrypt connection secrets with the first key in ENCRYPTION_KEY."""

from django.core.management.base import BaseCommand
from django.db import transaction

from iam.crypto import rotate
from iam.models import Connection


class Command(BaseCommand):
    help = "Re-encrypt all connection secrets with the newest ENCRYPTION_KEY."

    def handle(self, *args, **options):
        count = 0
        with transaction.atomic():
            for connection in Connection.objects.select_for_update().exclude(secret_data=""):
                connection.secret_data = rotate(connection.secret_data)
                connection.save(update_fields=["secret_data"])
                count += 1
        self.stdout.write(f"Re-encrypted the secrets of {count} connection(s).")
