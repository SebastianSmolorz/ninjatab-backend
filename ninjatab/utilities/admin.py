from django.contrib import admin
from .models import AppMessage, Option


@admin.register(AppMessage)
class AppMessageAdmin(admin.ModelAdmin):
    list_display = ['level', 'message', 'active', 'created_at']
    list_filter = ['level', 'active']
    list_editable = ['active']


@admin.register(Option)
class OptionAdmin(admin.ModelAdmin):
    list_display = ['name', 'active', 'value', 'updated_at']
    list_filter = ['active']
    list_editable = ['active', 'value']
    search_fields = ['name', 'value']
