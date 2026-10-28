from django.core.management import call_command
from django.db import migrations


def create_shared_cache_table(apps, schema_editor):
    table_name = 'scouting_cache'
    if table_name not in schema_editor.connection.introspection.table_names():
        call_command(
            'createcachetable',
            table_name,
            database=schema_editor.connection.alias,
            verbosity=0,
        )


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.RunPython(create_shared_cache_table, migrations.RunPython.noop),
    ]
