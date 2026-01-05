"""
Management command to rotate the log signing key.
Usage: python manage.py rotate_signing_key
"""

import secrets
import string
from django.core.management.base import BaseCommand
from logs.models import LogSigningKey


class Command(BaseCommand):
    help = "Rotates the active cryptographic key used for signing audit logs."

    def handle(self, *args, **options):
        self.stdout.write("Rotating log signing key...")

        # 1. Deactivate current key
        LogSigningKey.objects.filter(is_active=True).update(is_active=False)

        # 2. Generate new key
        alphabet = string.ascii_letters + string.digits
        new_key_str = "".join(secrets.choice(alphabet) for _ in range(64))

        new_key = LogSigningKey.objects.create(key=new_key_str, is_active=True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully rotated signing key. New key ID: {new_key.id}"
            )
        )
