from django.core.management.base import BaseCommand

from ninjatab.utilities.models import Option
from ninjatab.utilities.registry import OPTION_REGISTRY


class Command(BaseCommand):
    help = "Sync the Option registry to the database (get_or_create; never overwrites existing values)."

    def handle(self, *args, **options):
        for name, defaults in OPTION_REGISTRY.items():
            _, created = Option.objects.get_or_create(name=name, defaults=defaults)
            status = "created" if created else "exists"
            self.stdout.write(f"{name}: {status}")
