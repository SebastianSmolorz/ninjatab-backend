from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from ninjatab.auth.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    readonly_fields = ['uuid']
    search_fields = ['username', 'email', 'first_name', 'last_name', 'uuid']
