from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from ninjatab.auth.models import User, UserPaymentMethod


class UserPaymentMethodInline(admin.TabularInline):
    model = UserPaymentMethod
    extra = 0
    fields = ['provider', 'username', 'is_preferred']
    readonly_fields = ['uuid']


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    readonly_fields = ['uuid']
    search_fields = ['username', 'email', 'first_name', 'last_name', 'uuid']
    list_display = ['username', 'uuid', 'email', 'first_name', 'last_name', 'date_joined', 'analytics_opted_in', 'platform']
    ordering = ['-uuid']
    inlines = [UserPaymentMethodInline]
    fieldsets = BaseUserAdmin.fieldsets[:1] + (
        ('Identity', {'fields': ('uuid',)}),
    ) + BaseUserAdmin.fieldsets[1:]


@admin.register(UserPaymentMethod)
class UserPaymentMethodAdmin(admin.ModelAdmin):
    list_display = ['user', 'provider', 'is_preferred', 'created_at']
    list_filter = ['provider', 'is_preferred']
    search_fields = ['user__username', 'user__email', 'user__uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    raw_id_fields = ['user']
