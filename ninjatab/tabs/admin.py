from django.contrib import admin
from django.utils.html import format_html
from .models import Tab, TabPerson, Bill, LineItem, PersonLineItemClaim, Settlement, ExchangeRate


class TabPersonInline(admin.TabularInline):
    model = TabPerson
    extra = 1
    fields = ['name', 'email', 'user', 'created_at']
    readonly_fields = ['created_at']
    autocomplete_fields = ['user']


@admin.register(Tab)
class TabAdmin(admin.ModelAdmin):
    list_display = ['name', 'default_currency', 'settlement_currency', 'bill_count', 'is_settled', 'created_at']
    list_filter = ['is_settled', 'default_currency', 'settlement_currency', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['bill_count', 'created_at', 'updated_at']
    inlines = [TabPersonInline]

    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'default_currency', 'settlement_currency')
        }),
        ('Status', {
            'fields': ('is_settled', 'bill_count')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(TabPerson)
class TabPersonAdmin(admin.ModelAdmin):
    list_display = ['name', 'tab', 'email', 'user_link', 'created_at']
    list_filter = ['tab', 'created_at']
    search_fields = ['name', 'email', 'user__username', 'user__email']
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['tab', 'user']

    fieldsets = (
        ('Person Information', {
            'fields': ('tab', 'name', 'email')
        }),
        ('User Association', {
            'fields': ('user',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def user_link(self, obj):
        if obj.user:
            return format_html(
                '<a href="/admin/auth/user/{}/change/">{}</a>',
                obj.user.id,
                obj.user.username
            )
        return '-'

    user_link.short_description = 'User'


class LineItemInline(admin.TabularInline):
    model = LineItem
    extra = 1
    fields = ['description', 'value', 'split_type', 'total_claimed', 'created_at']
    readonly_fields = ['total_claimed', 'created_at']

    def total_claimed(self, obj):
        if obj.pk:
            total = sum(
                claim.calculated_amount or 0
                for claim in obj.person_claims.all()
            )
            return f"£{total:.2f}"
        return "-"

    total_claimed.short_description = 'Total Claimed'


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = ['description', 'tab', 'currency', 'total_amount', 'status', 'date']
    list_filter = ['status', 'currency', 'date', 'created_at']
    search_fields = ['description', 'tab__name']
    readonly_fields = ['total_amount', 'created_at', 'updated_at']
    autocomplete_fields = ['tab', 'creator', 'paid_by']
    date_hierarchy = 'date'
    inlines = [LineItemInline]

    fieldsets = (
        ('Bill Information', {
            'fields': ('tab', 'description', 'currency', 'date')
        }),
        ('People', {
            'fields': ('creator', 'paid_by')
        }),
        ('Status', {
            'fields': ('status', 'total_amount')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('tab', 'creator', 'paid_by').prefetch_related('line_items')


class PersonLineItemClaimInline(admin.TabularInline):
    model = PersonLineItemClaim
    extra = 0
    fields = ['person', 'split_value', 'calculated_amount', 'has_claimed']
    readonly_fields = ['calculated_amount']
    autocomplete_fields = ['person']


@admin.register(LineItem)
class LineItemAdmin(admin.ModelAdmin):
    list_display = ['description', 'bill', 'value', 'split_type', 'total_claimed_amount', 'claims_count', 'created_at']
    list_filter = ['split_type', 'bill__tab', 'created_at']
    search_fields = ['description', 'bill__description', 'bill__tab__name']
    readonly_fields = ['created_at', 'updated_at', 'claims_count', 'total_claimed_amount']
    autocomplete_fields = ['bill']
    inlines = [PersonLineItemClaimInline]

    fieldsets = (
        ('Line Item Information', {
            'fields': ('bill', 'description', 'value', 'split_type')
        }),
        ('Claims', {
            'fields': ('claims_count', 'total_claimed_amount')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def claims_count(self, obj):
        return obj.person_claims.count()

    claims_count.short_description = 'Number of Claims'

    def total_claimed_amount(self, obj):
        total = sum(
            claim.calculated_amount or 0
            for claim in obj.person_claims.all()
        )
        return f"£{total:.2f}"

    total_claimed_amount.short_description = 'Total Claimed'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('bill', 'bill__tab').prefetch_related('person_claims')


@admin.register(PersonLineItemClaim)
class PersonLineItemClaimAdmin(admin.ModelAdmin):
    list_display = [
        'person',
        'line_item',
        'split_value',
        'calculated_amount',
        'has_claimed',
        'created_at'
    ]
    list_filter = ['has_claimed', 'line_item__split_type', 'created_at']
    search_fields = [
        'person__name',
        'line_item__description',
        'line_item__bill__description',
        'line_item__bill__tab__name'
    ]
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['person', 'line_item']

    fieldsets = (
        ('Claim Information', {
            'fields': ('person', 'line_item')
        }),
        ('Split Details', {
            'fields': ('split_value', 'calculated_amount')
        }),
        ('Status', {
            'fields': ('has_claimed',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            'person',
            'person__tab',
            'line_item',
            'line_item__bill',
            'line_item__bill__tab'
        )


@admin.register(Settlement)
class SettlementAdmin(admin.ModelAdmin):
    list_display = ['tab', 'from_person', 'to_person', 'amount', 'currency', 'created_at']
    list_filter = ['currency', 'tab', 'created_at']
    search_fields = ['tab__name', 'from_person__name', 'to_person__name']
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['tab', 'from_person', 'to_person']

    fieldsets = (
        ('Settlement Information', {
            'fields': ('tab', 'from_person', 'to_person', 'amount', 'currency')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('tab', 'from_person', 'to_person')


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ['from_currency', 'to_currency', 'rate', 'effective_date', 'created_at']
    list_filter = ['from_currency', 'to_currency', 'effective_date', 'created_at']
    search_fields = ['from_currency', 'to_currency']
    readonly_fields = ['created_at', 'updated_at']
    date_hierarchy = 'effective_date'
    ordering = ['-effective_date', 'from_currency', 'to_currency']

    fieldsets = (
        ('Exchange Rate Information', {
            'fields': ('from_currency', 'to_currency', 'rate', 'effective_date')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request)
