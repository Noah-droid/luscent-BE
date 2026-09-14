# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_cases', '0017_agentmission_error_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentmission',
            name='load_config',
            field=models.JSONField(blank=True, default=dict, help_text="Load test config: {'users': 10, 'spawnRate': 2, 'duration': '30s'}"),
        ),
    ]
