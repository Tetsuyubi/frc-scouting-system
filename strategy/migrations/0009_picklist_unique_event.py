from django.db import migrations, models


def deduplicate_picklists(apps, schema_editor):
    PickList_Data = apps.get_model('strategy', 'PickList_Data')
    database = schema_editor.connection.alias

    events = list(
        PickList_Data.objects.using(database)
        .values_list('event', flat=True)
        .distinct()
    )
    for event in events:
        rows = list(
            PickList_Data.objects.using(database)
            .filter(event=event)
            .order_by('-pk')
        )
        if len(rows) < 2:
            continue

        keeper = rows[0]
        for field in ('starred', 'struck'):
            merged = []
            seen = set()
            for row in rows:
                values = getattr(row, field)
                if not isinstance(values, list):
                    continue
                for value in values:
                    key = str(value)
                    if key not in seen:
                        merged.append(value)
                        seen.add(key)
            setattr(keeper, field, merged)
        keeper.save(using=database, update_fields=['starred', 'struck'])
        PickList_Data.objects.using(database).filter(
            pk__in=[row.pk for row in rows[1:]]
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('strategy', '0008_picklist_data_fourth_pick_updated_ms'),
    ]

    operations = [
        migrations.RunPython(deduplicate_picklists, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='picklist_data',
            name='event',
            field=models.CharField(default='testing', max_length=20),
        ),
        migrations.AddConstraint(
            model_name='picklist_data',
            constraint=models.UniqueConstraint(
                fields=('event',),
                name='unique_picklist_event',
            ),
        ),
    ]
