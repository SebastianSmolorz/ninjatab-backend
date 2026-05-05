from django.contrib import admin
from .models import AppMessage


@admin.register(AppMessage)
class AppMessageAdmin(admin.ModelAdmin):
    list_display = ['level', 'message', 'active', 'created_at']
    list_filter = ['level', 'active']
    list_editable = ['active']
