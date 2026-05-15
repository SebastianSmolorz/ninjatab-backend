from django.contrib import admin
from django.db.models import Count, Sum
from django.utils.html import format_html
from .models import Tab, TabPerson, Bill, LineItem, PersonLineItemClaim, Settlement, Contact, SplitType
from ninjatab.currencies.currency_utils import minor_to_decimal


class MoneyAdminMixin:
    @staticmethod
    def format_money(amount, currency):
        if amount is None:
            return '-'
        return f"{minor_to_decimal(amount, currency)} {currency}"


class TabPersonInline(admin.TabularInline):
    model = TabPerson
    extra = 1
    fields = ['uuid', 'name', 'user', 'created_at']
    readonly_fields = ['uuid', 'created_at']
    raw_id_fields = ['user']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')


class BillInline(MoneyAdminMixin, admin.TabularInline):
    model = Bill
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ['uuid', 'description', 'date', 'created_at', 'currency', 'display_total', 'status', 'paid_by', 'line_item_count']
    readonly_fields = ['uuid', 'description', 'date', 'created_at', 'currency', 'display_total', 'status', 'paid_by', 'line_item_count']
    ordering = ['-date']

    def has_add_permission(self, request, obj=None):
        return False

    def display_total(self, obj):
        return self.format_money(obj._total_amount, obj.currency)
    display_total.short_description = 'Total'

    def line_item_count(self, obj):
        from django.urls import reverse
        count = obj._line_item_count
        url = reverse('admin:tabs_lineitem_changelist') + f'?bill__id__exact={obj.pk}'
        return format_html('<a href="{}">{} item{}</a>', url, count, '' if count == 1 else 's')
    line_item_count.short_description = 'Line Items'

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related('paid_by')
            .annotate(
                _total_amount=Sum('line_items__value'),
                _line_item_count=Count('line_items'),
            )
        )


class SettlementInline(MoneyAdminMixin, admin.TabularInline):
    model = Settlement
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ['from_person', 'to_person', 'display_amount']
    readonly_fields = ['from_person', 'to_person', 'display_amount']

    def has_add_permission(self, request, obj=None):
        return False

    def display_amount(self, obj):
        return self.format_money(obj.amount, obj.currency)
    display_amount.short_description = 'Amount'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('from_person', 'to_person')


class DemoTabFilter(admin.SimpleListFilter):
    title = 'demo'
    parameter_name = 'demo'
    field_name = 'is_demo'

    def lookups(self, request, model_admin):
        return (
            ('real', 'Real (default)'),
            ('demo', 'Demo'),
            ('all', 'All'),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == 'demo':
            return queryset.filter(**{self.field_name: True})
        if value == 'all':
            return queryset
        return queryset.filter(**{self.field_name: False})

    def choices(self, changelist):
        value = self.value() or 'real'
        for lookup, title in self.lookup_choices:
            yield {
                'selected': value == lookup,
                'query_string': changelist.get_query_string({self.parameter_name: lookup}, []),
                'display': title,
            }


class BillDemoTabFilter(DemoTabFilter):
    field_name = 'tab__is_demo'


@admin.register(Tab)
class TabAdmin(admin.ModelAdmin):
    list_display = ['name', 'uuid', 'is_demo', 'default_currency', 'settlement_currency', 'is_pro', 'is_settled', 'is_archived', 'created_by', 'created_at']
    ordering = ['-uuid']
    list_filter = [DemoTabFilter, 'is_pro', 'is_settled', 'is_archived', 'default_currency', 'settlement_currency', 'created_at']
    search_fields = ['name', 'description', 'uuid', 'created_by__uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    raw_id_fields = ['created_by']
    show_full_result_count = False
    inlines = [TabPersonInline, BillInline, SettlementInline]

    def get_inline_instances(self, request, obj=None):
        instances = super().get_inline_instances(request, obj)
        if obj is None or not obj.settlements.exists():
            return [i for i in instances if not isinstance(i, SettlementInline)]
        return instances

    fieldsets = (
        ('Basic Information', {
            'fields': ('uuid', 'name', 'description', 'default_currency', 'settlement_currency', 'created_by')
        }),
        ('Status', {
            'fields': ('is_pro', 'is_settled', 'is_archived')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('created_by')


@admin.register(TabPerson)
class TabPersonAdmin(admin.ModelAdmin):
    list_display = ['name', 'uuid', 'tab', 'user_link', 'created_at']
    ordering = ['-uuid']
    list_filter = ['created_at']
    search_fields = ['name', 'uuid', 'user__username', 'user__email', 'user__uuid', 'tab__name', 'tab__uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    raw_id_fields = ['tab', 'user']

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

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('tab', 'user')


class LineItemInline(MoneyAdminMixin, admin.TabularInline):
    model = LineItem
    extra = 1
    fields = ['uuid', 'description', 'translated_name', 'value', 'split_type', 'total_claimed', 'created_at']
    readonly_fields = ['uuid', 'total_claimed', 'created_at']

    def total_claimed(self, obj):
        if obj.pk:
            total = sum(
                claim.calculated_amount or 0
                for claim in obj.person_claims.all()
            )
            return self.format_money(total, obj.bill.currency)
        return "-"

    total_claimed.short_description = 'Total Claimed'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('bill').prefetch_related('person_claims')


@admin.register(Bill)
class BillAdmin(MoneyAdminMixin, admin.ModelAdmin):
    list_display = ['description', 'uuid', 'tab_link', 'tab_is_demo', 'currency', 'display_total_amount', 'display_is_itemised', 'status', 'date', 'has_receipt']
    ordering = ['-uuid']
    list_filter = [BillDemoTabFilter, 'status', 'currency', 'date', 'created_at']
    search_fields = ['description', 'uuid', 'tab__name', 'tab__uuid']
    readonly_fields = ['uuid', 'tab_is_demo', 'display_total_amount', 'receipt_image_link', 'created_at', 'updated_at']
    raw_id_fields = ['tab', 'creator', 'paid_by']
    date_hierarchy = 'date'
    show_full_result_count = False
    inlines = [LineItemInline]

    fieldsets = (
        ('Bill Information', {
            'fields': ('uuid', 'tab', 'tab_is_demo', 'description', 'currency', 'date')
        }),
        ('People', {
            'fields': ('creator', 'paid_by')
        }),
        ('Status', {
            'fields': ('status', 'display_total_amount')
        }),
        ('Receipt', {
            'fields': ('receipt_image_link',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def display_total_amount(self, obj):
        return self.format_money(obj._total_amount, obj.currency)
    display_total_amount.short_description = 'Total Amount'

    def tab_link(self, obj):
        from django.urls import reverse
        name = obj.tab.name or str(obj.tab.uuid)
        truncated = name if len(name) <= 30 else name[:29] + '…'
        url = reverse('admin:tabs_tab_change', args=[obj.tab.pk])
        return format_html(
            '<a href="{}" title="{}" style="display:inline-block;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;">{}</a>',
            url, name, truncated,
        )
    tab_link.short_description = 'Tab'
    tab_link.admin_order_field = 'tab__name'

    def tab_is_demo(self, obj):
        return obj.tab.is_demo
    tab_is_demo.boolean = True
    tab_is_demo.short_description = 'Demo tab'
    tab_is_demo.admin_order_field = 'tab__is_demo'

    def display_is_itemised(self, obj):
        return (obj._line_items_count or 0) > 1
    display_is_itemised.boolean = True
    display_is_itemised.short_description = 'Itemised'

    def has_receipt(self, obj):
        return bool(obj.receipt_image_url)
    has_receipt.boolean = True
    has_receipt.short_description = 'Receipt'

    def receipt_image_link(self, obj):
        if obj.receipt_image_key:
            from ninjatab.tabs.receipt_service import generate_presigned_url
            url = generate_presigned_url(obj.receipt_image_key)
            return format_html(
                '<a href="{}" target="_blank">View receipt image</a> <span style="color:#888">({})</span>',
                url,
                obj.receipt_image_key,
            )
        if obj.receipt_image_url:
            return format_html(
                '<a href="{}" target="_blank">View receipt image</a>',
                obj.receipt_image_url,
            )
        return '-'
    receipt_image_link.short_description = 'Receipt Image'

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related('tab', 'creator', 'paid_by')
            .annotate(
                _total_amount=Sum('line_items__value'),
                _line_items_count=Count('line_items'),
            )
        )


class PersonLineItemClaimInline(MoneyAdminMixin, admin.TabularInline):
    model = PersonLineItemClaim
    extra = 0
    fields = ['uuid', 'person', 'split_value', 'display_calculated_amount', 'has_claimed']
    readonly_fields = ['uuid', 'display_calculated_amount']
    raw_id_fields = ['person']

    def display_calculated_amount(self, obj):
        if obj.pk:
            return self.format_money(obj.calculated_amount, obj.line_item.bill.currency)
        return '-'
    display_calculated_amount.short_description = 'Calculated Amount'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('line_item', 'line_item__bill')


@admin.register(LineItem)
class LineItemAdmin(MoneyAdminMixin, admin.ModelAdmin):
    list_display = ['description', 'translated_name', 'uuid', 'bill', 'display_value', 'split_type', 'total_claimed_amount', 'claims_count', 'created_at']
    ordering = ['-uuid']
    list_filter = ['split_type', 'created_at']
    search_fields = ['description', 'translated_name', 'uuid', 'bill__description', 'bill__uuid', 'bill__tab__name', 'bill__tab__uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at', 'claims_count', 'total_claimed_amount']
    raw_id_fields = ['bill']
    show_full_result_count = False
    inlines = [PersonLineItemClaimInline]

    fieldsets = (
        ('Line Item Information', {
            'fields': ('uuid', 'bill', 'description', 'translated_name', 'value', 'split_type')
        }),
        ('Claims', {
            'fields': ('claims_count', 'total_claimed_amount')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def display_value(self, obj):
        return self.format_money(obj.value, obj.bill.currency)
    display_value.short_description = 'Value'

    def claims_count(self, obj):
        return len(obj.person_claims.all())
    claims_count.short_description = 'Number of Claims'

    def total_claimed_amount(self, obj):
        total = sum(
            claim.calculated_amount or 0
            for claim in obj.person_claims.all()
        )
        return self.format_money(total, obj.bill.currency)
    total_claimed_amount.short_description = 'Total Claimed'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('bill', 'bill__tab').prefetch_related('person_claims')


@admin.register(PersonLineItemClaim)
class PersonLineItemClaimAdmin(MoneyAdminMixin, admin.ModelAdmin):
    list_display = [
        'uuid',
        'person',
        'line_item',
        'display_split_value',
        'display_calculated_amount',
        'has_claimed',
        'created_at'
    ]
    ordering = ['-uuid']
    list_filter = ['has_claimed', 'created_at']
    search_fields = [
        'uuid',
        'person__name',
        'person__uuid',
        'line_item__description',
        'line_item__uuid',
        'line_item__bill__description',
    ]
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    show_full_result_count = False
    raw_id_fields = ['person', 'line_item']

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

    def display_calculated_amount(self, obj):
        return self.format_money(obj.calculated_amount, obj.line_item.bill.currency)
    display_calculated_amount.short_description = 'Calculated Amount'

    def display_split_value(self, obj):
        if obj.line_item.split_type == SplitType.VALUE:
            return self.format_money(obj.split_value, obj.line_item.bill.currency)
        return obj.split_value
    display_split_value.short_description = 'Split Value'

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
class SettlementAdmin(MoneyAdminMixin, admin.ModelAdmin):
    list_display = ['uuid', 'tab', 'from_person', 'to_person', 'display_amount', 'currency', 'created_at']
    ordering = ['-uuid']
    list_filter = ['currency', 'created_at']
    search_fields = ['uuid', 'tab__name', 'tab__uuid', 'from_person__name', 'to_person__name']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    show_full_result_count = False
    raw_id_fields = ['tab', 'from_person', 'to_person']

    fieldsets = (
        ('Settlement Information', {
            'fields': ('uuid', 'tab', 'from_person', 'to_person', 'amount', 'currency')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def display_amount(self, obj):
        return self.format_money(obj.amount, obj.currency)
    display_amount.short_description = 'Amount'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('tab', 'from_person', 'to_person')


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ['uuid', 'owner', 'contact_user', 'created_at']
    ordering = ['-uuid']
    list_filter = ['created_at']
    search_fields = ['uuid', 'owner__email', 'owner__first_name', 'contact_user__email', 'contact_user__first_name']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    raw_id_fields = ['owner', 'contact_user']

    fieldsets = (
        ('Contact Information', {
            'fields': ('uuid', 'owner', 'contact_user')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('owner', 'contact_user')

