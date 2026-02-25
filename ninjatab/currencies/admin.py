from django.contrib import admin
from .models import ExchangeRate


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ('from_currency', 'to_currency', 'rate', 'effective_date')
    list_filter = ('from_currency', 'to_currency')
    ordering = ('-effective_date',)
