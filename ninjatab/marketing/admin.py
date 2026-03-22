from django.contrib import admin
from ninjatab.marketing.models import WaitlistEntry


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(admin.ModelAdmin):
    list_display = ["email", "platform", "created_at"]
    list_filter = ["platform"]
    search_fields = ["email"]
    readonly_fields = ["created_at"]
