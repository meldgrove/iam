from django.core.management.base import BaseCommand

from iam.crypto import generate_key


class Command(BaseCommand):
    help = "Print a new ENCRYPTION_KEY value."

    def handle(self, *args, **options):
        self.stdout.write(generate_key())
