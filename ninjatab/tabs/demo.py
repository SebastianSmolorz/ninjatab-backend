from datetime import date

from ninjatab.tabs.models import Tab, TabPerson, Bill, LineItem, PersonLineItemClaim

_DEMO_RECEIPT_URL = "https://tab-ninja-receipt-scans.lon1.cdn.digitaloceanspaces.com/demo/demo1.jpg"

_DEMO_RECEIPT_DATA = {
    "description": "(receipt scan) Villa Perla Kaleici Turkiye",
    "currency": "TRY",
    "line_items": [
        {
            "description": "DEMI GLACE SOSLU TAVUK SARMA",
            "translated_name": "DEMI GLACE CREAMY CHICKEN SARMA",
            "value": 72000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "__you__", "split_value": 1, "calculated_amount": 72000},
            ],
        },
        {
            "description": "KASARLI KOFFE",
            "translated_name": "KASARLI COFFEE",
            "value": 63000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Alex", "split_value": 1, "calculated_amount": 63000},
            ],
        },
        {
            "description": "SOGAN PANE",
            "translated_name": "ONION PANE",
            "value": 20000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Sam", "split_value": 1, "calculated_amount": 20000},
            ],
        },
        {
            "description": "KREMA JAMBOH SOSLU SPAGET FI",
            "translated_name": "CREAMY JAMBOH SAUCE SPAGHETTI",
            "value": 50000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Jordan", "split_value": 1, "calculated_amount": 50000},
            ],
        },
        {
            "description": "KUZU SIS",
            "translated_name": "LAMB SKEWER",
            "value": 78000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Alex", "split_value": 1, "calculated_amount": 78000},
            ],
        },
        {
            "description": "KARIDES TAVA (Terryapli)",
            "translated_name": "SHRIMP PAN (Buttered)",
            "value": 85000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Sam", "split_value": 1, "calculated_amount": 85000},
            ],
        },
        {
            "description": "KARISIK IZGARA",
            "translated_name": "MIXED GRILL",
            "value": 130000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Jordan", "split_value": 1, "calculated_amount": 130000},
            ],
        },
        {
            "description": "MOJITO",
            "translated_name": "MOJITO",
            "value": 58000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Jordan", "split_value": 1, "calculated_amount": 58000},
            ],
        },
        {
            "description": "BUYUK SU",
            "translated_name": "LARGE WATER",
            "value": 8000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Sam", "split_value": 1, "calculated_amount": 8000},
            ],
        },
        {
            "description": "IDOL UGNI B.LANC-CHARDONAY",
            "translated_name": "IDOL UGNI B.LANC-CHARDONAY",
            "value": 150000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Alex", "split_value": 1, "calculated_amount": 50000},
                {"person_name": "__you__", "split_value": 1, "calculated_amount": 50000},
                {"person_name": "Sam", "split_value": 1, "calculated_amount": 50000},
            ],
        },
        {
            "description": "TAZE PORTAKAL SUYU",
            "translated_name": "FRESH ORANGE JUICE",
            "value": 15000,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "Jordan", "split_value": 1, "calculated_amount": 15000},
            ],
        },
        {
            "description": "EFES (50CL)",
            "translated_name": "EFES (50CL)",
            "value": 27500,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "__you__", "split_value": 3, "calculated_amount": 27500},
            ],
        },
        {
            "description": "10% Servis Upreci",
            "translated_name": "10% Service Charge",
            "value": 75650,
            "split_type": "shares",
            "person_claims": [
                {"person_name": "__you__", "split_value": 1, "calculated_amount": 18912},
                {"person_name": "Alex", "split_value": 1, "calculated_amount": 18912},
                {"person_name": "Sam", "split_value": 1, "calculated_amount": 18912},
                {"person_name": "Jordan", "split_value": 1, "calculated_amount": 18912},
            ],
        },
    ],
}


def create_demo_tab(user) -> Tab:
    """Factory that builds a pre-populated demo tab for the given user."""
    user_name = user.get_full_name().strip() or user.username or "You"

    tab = Tab.objects.create(
        name="Road Trip",
        description="A demo tab, feel free to explore and edit!",
        default_currency="GBP",
        settlement_currency="GBP",
        created_by=user,
        is_demo=True,
        is_pro=True,
        invite_code=None,
    )

    you = TabPerson.objects.create(tab=tab, name=user_name, user=user)
    alex = TabPerson.objects.create(tab=tab, name="Alex")
    sam = TabPerson.objects.create(tab=tab, name="Sam")
    jordan = TabPerson.objects.create(tab=tab, name="Jordan")
    all_people = [you, alex, sam, jordan]

    # Bill 1: Groceries — £48.00, equal 4-way split (£12.00 each)
    bill1 = Bill.objects.create(
        tab=tab, description="Fuel", currency="GBP",
        creator=you, paid_by=you,
    )
    li1 = LineItem.objects.create(
        bill=bill1, description="Groceries", value=4800, split_type="shares",
    )
    for person in all_people:
        PersonLineItemClaim.objects.create(
            person=person, line_item=li1,
            split_value=1, calculated_amount=1200, settlement_amount=1200,
        )

    # Bill 2: Campsite fees — £80.00, equal 4-way split (£20.00 each)
    bill2 = Bill.objects.create(
        tab=tab, description="Hotel in Germany", currency="EUR",
        creator=alex, paid_by=alex,
    )
    li2 = LineItem.objects.create(
        bill=bill2, description="Hotel in Germany", value=8000, split_type="shares",
    )
    for person in all_people:
        PersonLineItemClaim.objects.create(
            person=person, line_item=li2,
            split_value=1, calculated_amount=2000, settlement_amount=2000,
        )

    # Bill 3: Activities in Poland
    bill3 = Bill.objects.create(
        tab=tab, description="Activities in Poland", currency="PLN",
        creator=sam, paid_by=sam,
    )
    li3 = LineItem.objects.create(
        bill=bill3, description="Quad biking", value=50000, split_type="shares",
    )
    li4 = LineItem.objects.create(
        bill=bill3, description="Swimming pool", value=43000, split_type="shares",
    )
    for person in [you, alex]:
        PersonLineItemClaim.objects.create(
            person=person, line_item=li3,
            split_value=1, calculated_amount=25000, settlement_amount=25000,
        )
    for person in [sam, jordan]:
        PersonLineItemClaim.objects.create(
            person=person, line_item=li4,
            split_value=1, calculated_amount=21500, settlement_amount=21500,
        )

    # Bill 4: Restaurant receipt
    _person_map = {'__you__': you, 'Alex': alex, 'Sam': sam, 'Jordan': jordan}

    receipt_bill = Bill.objects.create(
        tab=tab,
        description=_DEMO_RECEIPT_DATA['description'],
        currency=_DEMO_RECEIPT_DATA['currency'],
        creator=you,
        paid_by=jordan,
        date=date.today(),
        receipt_image_url=_DEMO_RECEIPT_URL,
        # receipt_image_key intentionally empty — public CDN URL needs no presigned wrapping
    )

    for li_data in _DEMO_RECEIPT_DATA['line_items']:
        li = LineItem.objects.create(
            bill=receipt_bill,
            description=li_data['description'],
            translated_name=li_data.get('translated_name', ''),
            value=li_data['value'],
            split_type=li_data['split_type'],
        )
        for claim_data in li_data['person_claims']:
            person = _person_map.get(claim_data['person_name'])
            if person is None:
                continue
            PersonLineItemClaim.objects.create(
                person=person,
                line_item=li,
                split_value=claim_data['split_value'],
                calculated_amount=claim_data['calculated_amount'],
                settlement_amount=None,  # TRY bill, GBP settlement — left for simplify
            )

    # Bill 5: Drinks at the hotel — €80.00, shares split
    bill5 = Bill.objects.create(
        tab=tab, description="Drinks at the hotel", currency="EUR",
        creator=you, paid_by=you,
    )
    li5 = LineItem.objects.create(
        bill=bill5, description="Local Beer", value=8000, split_type="shares",
    )
    for person, shares, amount in [
        (you, 3, 3000),
        (alex, 2, 2000),
        (sam, 2, 2000),
        (jordan, 1, 1000),
    ]:
        PersonLineItemClaim.objects.create(
            person=person, line_item=li5,
            split_value=shares, calculated_amount=amount, settlement_amount=amount,
        )

    return tab
