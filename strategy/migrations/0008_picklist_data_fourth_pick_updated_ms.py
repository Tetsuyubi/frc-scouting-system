from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("strategy", "0007_picklist_data_starred_picklist_data_struck"),
    ]

    operations = [
        migrations.AddField(
            model_name="picklist_data",
            name="fourth_pick",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="picklist_data",
            name="updated_ms",
            field=models.BigIntegerField(default=0),
        ),
    ]
