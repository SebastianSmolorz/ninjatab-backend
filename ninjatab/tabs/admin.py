from django.contrib import admin
from django.utils.html import format_html
from .models import Tab, TabPerson, Bill, LineItem, PersonLineItemClaim, Settlement, Contact
from ninjatab.currencies.currency_utils import minor_to_decimal


class TabPersonInline(admin.TabularInline):
    model = TabPerson
    extra = 1
    fields = ['uuid', 'name', 'user', 'created_at']
    readonly_fields = ['uuid', 'created_at']
    autocomplete_fields = ['user']


@admin.register(Tab)
class TabAdmin(admin.ModelAdmin):
    list_display = ['name', 'uuid', 'default_currency', 'settlement_currency', 'is_pro', 'is_settled', 'created_by', 'created_at']
    list_filter = ['is_pro', 'is_settled', 'default_currency', 'settlement_currency', 'created_at']
    search_fields = ['name', 'description', 'uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    inlines = [TabPersonInline]

    fieldsets = (
        ('Basic Information', {
            'fields': ('uuid', 'name', 'description', 'default_currency', 'settlement_currency', 'created_by')
        }),
        ('Status', {
            'fields': ('is_pro', 'is_settled')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(TabPerson)
class TabPersonAdmin(admin.ModelAdmin):
    list_display = ['name', 'uuid', 'tab', 'user_link', 'created_at']
    list_filter = ['tab', 'created_at']
    search_fields = ['name', 'uuid', 'user__username', 'user__email']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    autocomplete_fields = ['tab', 'user']

    fieldsets = (
        ('Person Information', {
            'fields': ('uuid', 'tab', 'name')
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
                '<a href="/admin/ninjatab_auth/user/{}/change/">{}</a>',
                obj.user.id,
                obj.user.username
            )
        return '-'

    user_link.short_description = 'User'


class LineItemInline(admin.TabularInline):
    model = LineItem
    extra = 1
    fields = ['uuid', 'description', 'value', 'split_type', 'total_claimed', 'created_at']
    readonly_fields = ['uuid', 'total_claimed', 'created_at']

    def total_claimed(self, obj):
        if obj.pk:
            total = sum(
                claim.calculated_amount or 0
                for claim in obj.person_claims.all()
            )
            return str(minor_to_decimal(total, obj.bill.currency))
        return "-"

    total_claimed.short_description = 'Total Claimed'


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = ['description', 'uuid', 'tab', 'currency', 'total_amount', 'is_itemised', 'status', 'date', 'has_receipt']
    list_filter = ['status', 'currency', 'date', 'created_at']
    search_fields = ['description', 'uuid', 'tab__name', 'tab__uuid']
    readonly_fields = ['uuid', 'total_amount', 'receipt_image_link', 'created_at', 'updated_at']
    autocomplete_fields = ['tab', 'creator', 'paid_by']
    date_hierarchy = 'date'
    inlines = [LineItemInline]

    fieldsets = (
        ('Bill Information', {
            'fields': ('uuid', 'tab', 'description', 'currency', 'date')
        }),
        ('People', {
            'fields': ('creator', 'paid_by')
        }),
        ('Status', {
            'fields': ('status', 'total_amount')
        }),
        ('Receipt', {
            'fields': ('receipt_image_link',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def has_receipt(self, obj):
        return bool(obj.receipt_image_url)
    has_receipt.boolean = True
    has_receipt.short_description = 'Receipt'

    def receipt_image_link(self, obj):
        if obj.receipt_image_url:
            return format_html(
                '<a href="{}" target="_blank">View receipt image</a>',
                obj.receipt_image_url,
            )
        return '-'
    receipt_image_link.short_description = 'Receipt Image'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('tab', 'creator', 'paid_by').prefetch_related('line_items')


class PersonLineItemClaimInline(admin.TabularInline):
    model = PersonLineItemClaim
    extra = 0
    fields = ['uuid', 'person', 'split_value', 'calculated_amount', 'has_claimed']
    readonly_fields = ['uuid', 'calculated_amount']
    autocomplete_fields = ['person']


@admin.register(LineItem)
class LineItemAdmin(admin.ModelAdmin):
    list_display = ['description', 'uuid', 'bill', 'value', 'split_type', 'total_claimed_amount', 'claims_count', 'created_at']
    list_filter = ['split_type', 'bill__tab', 'created_at']
    search_fields = ['description', 'uuid', 'bill__description', 'bill__uuid', 'bill__tab__name']
    readonly_fields = ['uuid', 'created_at', 'updated_at', 'claims_count', 'total_claimed_amount']
    autocomplete_fields = ['bill']
    inlines = [PersonLineItemClaimInline]

    fieldsets = (
        ('Line Item Information', {
            'fields': ('uuid', 'bill', 'description', 'value', 'split_type')
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
        return str(minor_to_decimal(total, obj.bill.currency))

    total_claimed_amount.short_description = 'Total Claimed'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('bill', 'bill__tab').prefetch_related('person_claims')


@admin.register(PersonLineItemClaim)
class PersonLineItemClaimAdmin(admin.ModelAdmin):
    list_display = [
        'uuid',
        'person',
        'line_item',
        'split_value',
        'calculated_amount',
        'has_claimed',
        'created_at'
    ]
    list_filter = ['has_claimed', 'line_item__split_type', 'created_at']
    search_fields = [
        'uuid',
        'person__name',
        'person__uuid',
        'line_item__description',
        'line_item__uuid',
        'line_item__bill__description',
        'line_item__bill__tab__name'
    ]
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    autocomplete_fields = ['person', 'line_item']

    fieldsets = (
        ('Claim Information', {
            'fields': ('uuid', 'person', 'line_item')
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
    list_display = ['uuid', 'tab', 'from_person', 'to_person', 'amount', 'currency', 'created_at']
    list_filter = ['currency', 'tab', 'created_at']
    search_fields = ['uuid', 'tab__name', 'tab__uuid', 'from_person__name', 'to_person__name']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    autocomplete_fields = ['tab', 'from_person', 'to_person']

    fieldsets = (
        ('Settlement Information', {
            'fields': ('uuid', 'tab', 'from_person', 'to_person', 'amount', 'currency')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('tab', 'from_person', 'to_person')


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ['uuid', 'owner', 'contact_user', 'created_at']
    list_filter = ['created_at']
    search_fields = ['uuid', 'owner__email', 'owner__first_name', 'contact_user__email', 'contact_user__first_name']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    autocomplete_fields = ['owner', 'contact_user']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('owner', 'contact_user')
