from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):

    dependencies = [
        ('collection', '0003_rename_collection_to_endpoint'),
        ('test_cases', '0003_testrun_batch_id_testrun_triggered_by'),
    ]

    operations = [
        migrations.RenameField(
            model_name='testcase',
            old_name='collection',
            new_name='endpoint',
        ),
        migrations.AlterField(
            model_name='testcase',
            name='endpoint',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='test_cases', to='collection.endpoint'),
        ),
    ]
