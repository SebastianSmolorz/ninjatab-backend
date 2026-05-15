from django.contrib import admin
from .models import ExchangeRate


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ('currency', 'rate', 'effective_date')
    ordering = ('-effective_date',)
