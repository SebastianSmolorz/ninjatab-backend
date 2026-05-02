from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from ninjatab.auth.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    readonly_fields = ['uuid']
    search_fields = ['username', 'email', 'first_name', 'last_name', 'uuid']
    list_display = ['username', 'uuid', 'email', 'first_name', 'last_name', 'is_staff']
    fieldsets = BaseUserAdmin.fieldsets[:1] + (
        ('Identity', {'fields': ('uuid',)}),
    ) + BaseUserAdmin.fieldsets[1:]
