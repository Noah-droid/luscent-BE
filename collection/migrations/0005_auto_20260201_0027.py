from django.db import migrations

def migrate_endpoints_to_collections(apps, schema_editor):
    Project = apps.get_model('projects', 'Project')
    Collection = apps.get_model('collection', 'Collection')
    Endpoint = apps.get_model('collection', 'Endpoint')

    for project in Project.objects.all():
        # Create a default collection for each project
        default_collection, created = Collection.objects.get_or_create(
            project=project,
            name="Default Collection",
            defaults={"description": "Automatically created during migration to group existing endpoints."}
        )

        # Move all endpoints associated with this project (via the old FK) to the new collection
        Endpoint.objects.filter(project=project).update(collection=default_collection)

class Migration(migrations.Migration):

    dependencies = [
        ('collection', '0004_alter_endpoint_unique_together_and_more'),
    ]

    operations = [
        migrations.RunPython(migrate_endpoints_to_collections),
    ]
