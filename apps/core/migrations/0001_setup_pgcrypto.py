from django.db import migrations
from django.contrib.postgres.operations import CryptoExtension

class Migration(migrations.Migration):
    initial = True

    dependencies = [
    ]

    operations = [
        CryptoExtension(),
    ]
