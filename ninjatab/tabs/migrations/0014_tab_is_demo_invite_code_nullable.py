import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tabs", "0013_tab_is_archived"),
    ]

    operations = [
        migrations.AddField(
            model_name="tab",
            name="is_demo",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="tab",
            name="invite_code",
            field=models.UUIDField(blank=True, default=uuid.uuid4, null=True, unique=True),
        ),
    ]
