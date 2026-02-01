from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):

    dependencies = [
        ('collection', '0005_auto_20260201_0027'),
        ('projects', '0002_initial'),
    ]

    operations = [
        # Just set the new unique_together without trying to drop the old one
        migrations.AlterField(
            model_name='endpoint',
            name='collection',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='endpoints', to='collection.collection'),
        ),
        migrations.AlterUniqueTogether(
            name='collection',
            unique_together={('project', 'name')},
        ),
        migrations.AlterUniqueTogether(
            name='endpoint',
            unique_together={('collection', 'method', 'url')},
        ),
        migrations.RemoveField(
            model_name='endpoint',
            name='project',
        ),
        migrations.RemoveField(
            model_name='endpoint',
            name='source',
        ),
    ]
