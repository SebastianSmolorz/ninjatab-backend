"""Refactor ExchangeRate to USD-base single-direction.

Drops all existing rows (which stored bidirectional pairs) and reshapes the
table to one row per (currency, effective_date) where rate = 1 USD * currency.

After applying, run `python manage.py fetch_exchange_rates` to repopulate.
"""

from django.db import migrations, models


def delete_all_rates(apps, schema_editor):
    ExchangeRate = apps.get_model('currencies', 'ExchangeRate')
    ExchangeRate.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('currencies', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(delete_all_rates, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='exchangerate',
            unique_together=set(),
        ),
        migrations.RemoveIndex(
            model_name='exchangerate',
            name='currencies__from_cu_d5f08b_idx',
        ),
        migrations.RemoveField(
            model_name='exchangerate',
            name='from_currency',
        ),
        migrations.RenameField(
            model_name='exchangerate',
            old_name='to_currency',
            new_name='currency',
        ),
        migrations.AlterField(
            model_name='exchangerate',
            name='rate',
            field=models.DecimalField(
                decimal_places=6,
                help_text='Exchange rate: 1 USD = rate * currency',
                max_digits=12,
            ),
        ),
        migrations.AlterUniqueTogether(
            name='exchangerate',
            unique_together={('currency', 'effective_date')},
        ),
        migrations.AddIndex(
            model_name='exchangerate',
            index=models.Index(
                fields=['currency', '-effective_date'],
                name='currencies__currenc_idx',
            ),
        ),
    ]
