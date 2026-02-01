from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('collection', '0002_initial'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='Collection',
            new_name='Endpoint',
        ),
        migrations.AlterModelTable(
            name='endpoint',
            table='collection_endpoint',
        ),
        # Manually rename indexes and constraints to avoid collision with new Collection model
        migrations.RunSQL(
            sql='ALTER INDEX IF EXISTS collection_collection_project_id_d39babdf RENAME TO collection_endpoint_project_id_idx;',
            reverse_sql='ALTER INDEX IF EXISTS collection_endpoint_project_id_idx RENAME TO collection_collection_project_id_d39babdf;'
        ),
        migrations.RunSQL(
            sql='ALTER TABLE collection_endpoint RENAME CONSTRAINT collection_collectio_project_id_d39babdf_fk_projects_ TO collection_endpoint_project_id_fk_projects;',
            reverse_sql='ALTER TABLE collection_endpoint RENAME CONSTRAINT collection_endpoint_project_id_fk_projects TO collection_collectio_project_id_d39babdf_fk_projects_;'
        ),
    ]
