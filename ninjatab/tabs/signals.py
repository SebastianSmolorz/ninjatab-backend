from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


def _touch_tab(tab):
    from .models import Tab
    Tab.objects.filter(pk=tab.pk).update(updated_at=timezone.now())


@receiver(post_save, sender='tabs.Bill')
def bill_saved(sender, instance, **kwargs):
    _touch_tab(instance.tab)


@receiver(post_save, sender='tabs.TabPerson')
def tab_person_saved(sender, instance, **kwargs):
    _touch_tab(instance.tab)


@receiver(post_save, sender='tabs.LineItem')
def line_item_saved(sender, instance, **kwargs):
    _touch_tab(instance.bill.tab)


@receiver(post_save, sender='tabs.PersonLineItemClaim')
def claim_saved(sender, instance, **kwargs):
    _touch_tab(instance.line_item.bill.tab)
