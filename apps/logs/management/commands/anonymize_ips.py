from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from logs.models import LogEntry


class Command(BaseCommand):
    help = "Anonymize IP addresses in log entries older than the retention period for GDPR compliance"

    def handle(self, *args, **options):
        # GDPR compliance: IP addresses should not be stored longer than necessary for security.
        # We use the retention period defined in settings.
        days = getattr(settings, "IP_ANONYMIZATION_DAYS", 90)
        cutoff_date = timezone.now() - timedelta(days=days)

        logs_to_anonymize = LogEntry.objects.filter(
            timestamp__lt=cutoff_date, ip_address__isnull=False
        )

        count = logs_to_anonymize.count()

        # We use update() for efficiency on large datasets.
        # This does not call the save() method, so signatures are not recalculated.
        # Since ip_address is not part of the signature calculation, this is safe.
        logs_to_anonymize.update(ip_address=None)

        if count > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully anonymized {count} log entries older than {days} days ({cutoff_date})."
                )
            )
        else:
            self.stdout.write(
                self.style.NOTICE(
                    "No log entries found that require anonymization."
                )
            )
