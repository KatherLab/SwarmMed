# Generated manually for HIPAA compliance changes

from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ('project', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('network', '0001_initial'),
        ('logs', '0004_logentry_source_logentry_swarm_network'),
    ]

    operations = [
        migrations.AlterField(
            model_name='logentry',
            name='category',
            field=models.CharField(choices=[('project', 'Project'), ('data', 'Data'), ('network', 'Network'), ('training', 'Training'), ('results', 'Results'), ('auth', 'Authentication'), ('access', 'Access Control')], max_length=20),
        ),
        migrations.AlterField(
            model_name='logentry',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='log_entries', to='project.project'),
        ),
        migrations.AlterField(
            model_name='logentry',
            name='user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='log_entries', to=settings.AUTH_USER_MODEL),
        ),
    ]
