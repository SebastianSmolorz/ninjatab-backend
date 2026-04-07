from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tabs", "0009_add_pagination_and_query_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lineitem",
            name="value",
            field=models.IntegerField(),
        ),
        migrations.AlterField(
            model_name="personlineitemclaim",
            name="split_value",
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text="Number of shares (SHARES mode) or minor currency units (VALUE mode)",
            ),
        ),
        migrations.AlterField(
            model_name="personlineitemclaim",
            name="calculated_amount",
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text="The actual currency amount this person owes, in minor units",
            ),
        ),
        migrations.AlterField(
            model_name="personlineitemclaim",
            name="settlement_amount",
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text="calculated_amount converted to the tab's settlement_currency, in minor units",
            ),
        ),
        migrations.AlterField(
            model_name="settlement",
            name="amount",
            field=models.IntegerField(),
        ),
        migrations.AlterField(
            model_name="tab",
            name="settlement_currency_settled_total",
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text="Total spent in settlement currency (minor units), snapshotted at settlement time",
            ),
        ),
    ]
