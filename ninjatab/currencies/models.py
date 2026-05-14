from django.db import models
from uuid6 import uuid7


class Currency(models.TextChoices):
    GBP = 'GBP', 'British Pound'
    EUR = 'EUR', 'Euro'
    USD = 'USD', 'US Dollar'
    TRY = 'TRY', 'Turkish Lira'
    AED = 'AED', 'UAE Dirham'
    CAD = 'CAD', 'Canadian Dollar'
    AUD = 'AUD', 'Australian Dollar'
    CHF = 'CHF', 'Swiss Franc'
    JPY = 'JPY', 'Japanese Yen'
    THB = 'THB', 'Thai Baht'
    PLN = 'PLN', 'Polish Złoty'
    CZK = 'CZK', 'Czech Koruna'
    HUF = 'HUF', 'Hungarian Forint'
    DKK = 'DKK', 'Danish Krone'
    SEK = 'SEK', 'Swedish Krona'
    NOK = 'NOK', 'Norwegian Krone'
    MAD = 'MAD', 'Moroccan Dirham'
    MXN = 'MXN', 'Mexican Peso'
    ZAR = 'ZAR', 'South African Rand'
    NZD = 'NZD', 'New Zealand Dollar'
    ISK = 'ISK', 'Icelandic Króna'
    BGN = 'BGN', 'Bulgarian Lev'
    RON = 'RON', 'Romanian Leu'
    ALL = 'ALL', 'Albanian Lek'
    BAM = 'BAM', 'Bosnia-Herzegovina Convertible Mark'
    RSD = 'RSD', 'Serbian Dinar'
    MKD = 'MKD', 'Macedonian Denar'
    GEL = 'GEL', 'Georgian Lari'
    EGP = 'EGP', 'Egyptian Pound'
    TND = 'TND', 'Tunisian Dinar'
    INR = 'INR', 'Indian Rupee'
    IDR = 'IDR', 'Indonesian Rupiah'
    MYR = 'MYR', 'Malaysian Ringgit'
    SGD = 'SGD', 'Singapore Dollar'
    HKD = 'HKD', 'Hong Kong Dollar'
    CNY = 'CNY', 'Chinese Yuan'
    KRW = 'KRW', 'South Korean Won'
    TWD = 'TWD', 'New Taiwan Dollar'
    PHP = 'PHP', 'Philippine Peso'
    VND = 'VND', 'Vietnamese Dong'
    KHR = 'KHR', 'Cambodian Riel'
    LKR = 'LKR', 'Sri Lankan Rupee'
    NPR = 'NPR', 'Nepalese Rupee'
    MUR = 'MUR', 'Mauritian Rupee'
    SCR = 'SCR', 'Seychellois Rupee'
    MVR = 'MVR', 'Maldivian Rufiyaa'
    QAR = 'QAR', 'Qatari Riyal'
    SAR = 'SAR', 'Saudi Riyal'
    OMR = 'OMR', 'Omani Rial'
    JOD = 'JOD', 'Jordanian Dinar'
    ILS = 'ILS', 'Israeli New Shekel'
    BRL = 'BRL', 'Brazilian Real'
    ARS = 'ARS', 'Argentine Peso'
    CLP = 'CLP', 'Chilean Peso'
    COP = 'COP', 'Colombian Peso'
    PEN = 'PEN', 'Peruvian Sol'
    CRC = 'CRC', 'Costa Rican Colón'
    DOP = 'DOP', 'Dominican Peso'
    JMD = 'JMD', 'Jamaican Dollar'
    BBD = 'BBD', 'Barbadian Dollar'
    XCD = 'XCD', 'East Caribbean Dollar'
    AWG = 'AWG', 'Aruban Florin'
    BZD = 'BZD', 'Belize Dollar'
    FJD = 'FJD', 'Fijian Dollar'
    TOP = 'TOP', "Tongan Paʻanga"
    WST = 'WST', 'Samoan Tālā'
    XPF = 'XPF', 'CFP Franc'
    ANG = 'ANG', 'Netherlands Antillean Guilder'
    TTD = 'TTD', 'Trinidad and Tobago Dollar'
    BMD = 'BMD', 'Bermudian Dollar'
    KYD = 'KYD', 'Cayman Islands Dollar'
    BSD = 'BSD', 'Bahamian Dollar'
    CUP = 'CUP', 'Cuban Peso'
    GTQ = 'GTQ', 'Guatemalan Quetzal'
    HNL = 'HNL', 'Honduran Lempira'
    NIO = 'NIO', 'Nicaraguan Córdoba'
    PAB = 'PAB', 'Panamanian Balboa'
    UYU = 'UYU', 'Uruguayan Peso'
    BOB = 'BOB', 'Bolivian Boliviano'
    PYG = 'PYG', 'Paraguayan Guaraní'
    MNT = 'MNT', 'Mongolian Tögrög'
    LAK = 'LAK', 'Lao Kip'
    MMK = 'MMK', 'Myanmar Kyat'
    BDT = 'BDT', 'Bangladeshi Taka'
    PKR = 'PKR', 'Pakistani Rupee'
    KES = 'KES', 'Kenyan Shilling'
    TZS = 'TZS', 'Tanzanian Shilling'
    UGX = 'UGX', 'Ugandan Shilling'
    RWF = 'RWF', 'Rwandan Franc'
    ETB = 'ETB', 'Ethiopian Birr'
    GHS = 'GHS', 'Ghanaian Cedi'
    NGN = 'NGN', 'Nigerian Naira'
    XOF = 'XOF', 'West African CFA Franc'
    XAF = 'XAF', 'Central African CFA Franc'
    CVE = 'CVE', 'Cape Verdean Escudo'
    GMD = 'GMD', 'Gambian Dalasi'
    MZN = 'MZN', 'Mozambican Metical'
    NAD = 'NAD', 'Namibian Dollar'
    BWP = 'BWP', 'Botswana Pula'
    ZMW = 'ZMW', 'Zambian Kwacha'
    AOA = 'AOA', 'Angolan Kwanza'
    GYD = 'GYD', 'Guyanese Dollar'
    SRD = 'SRD', 'Surinamese Dollar'
    FKP = 'FKP', 'Falkland Islands Pound'
    VES = 'VES', 'Venezuelan Bolívar'
    AFN = 'AFN', 'Afghan Afghani'
    AMD = 'AMD', 'Armenian Dram'
    AZN = 'AZN', 'Azerbaijani Manat'
    BHD = 'BHD', 'Bahraini Dinar'
    BIF = 'BIF', 'Burundian Franc'
    BND = 'BND', 'Brunei Dollar'
    BTN = 'BTN', 'Bhutanese Ngultrum'
    BYN = 'BYN', 'Belarusian Ruble'
    CDF = 'CDF', 'Congolese Franc'
    DJF = 'DJF', 'Djiboutian Franc'
    DZD = 'DZD', 'Algerian Dinar'
    ERN = 'ERN', 'Eritrean Nakfa'
    GIP = 'GIP', 'Gibraltar Pound'
    GNF = 'GNF', 'Guinean Franc'
    HTG = 'HTG', 'Haitian Gourde'
    IQD = 'IQD', 'Iraqi Dinar'
    IRR = 'IRR', 'Iranian Rial'
    KGS = 'KGS', 'Kyrgystani Som'
    KMF = 'KMF', 'Comorian Franc'
    KPW = 'KPW', 'North Korean Won'
    KWD = 'KWD', 'Kuwaiti Dinar'
    KZT = 'KZT', 'Kazakhstani Tenge'
    LBP = 'LBP', 'Lebanese Pound'
    LRD = 'LRD', 'Liberian Dollar'
    LSL = 'LSL', 'Lesotho Loti'
    LYD = 'LYD', 'Libyan Dinar'
    MDL = 'MDL', 'Moldovan Leu'
    MGA = 'MGA', 'Malagasy Ariary'
    MOP = 'MOP', 'Macanese Pataca'
    MRU = 'MRU', 'Mauritanian Ouguiya'
    MWK = 'MWK', 'Malawian Kwacha'
    PGK = 'PGK', 'Papua New Guinean Kina'
    RUB = 'RUB', 'Russian Ruble'
    SBD = 'SBD', 'Solomon Islands Dollar'
    SDG = 'SDG', 'Sudanese Pound'
    SHP = 'SHP', 'Saint Helena Pound'
    SLE = 'SLE', 'Sierra Leonean Leone'
    SOS = 'SOS', 'Somali Shilling'
    SSP = 'SSP', 'South Sudanese Pound'
    STN = 'STN', 'São Tomé and Príncipe Dobra'
    SVC = 'SVC', 'Salvadoran Colón'
    SYP = 'SYP', 'Syrian Pound'
    SZL = 'SZL', 'Swazi Lilangeni'
    TJS = 'TJS', 'Tajikistani Somoni'
    TMT = 'TMT', 'Turkmenistani Manat'
    UAH = 'UAH', 'Ukrainian Hryvnia'
    UZS = 'UZS', 'Uzbekistani Som'
    VUV = 'VUV', 'Vanuatu Vatu'
    XCG = 'XCG', 'Caribbean Guilder'
    YER = 'YER', 'Yemeni Rial'
    ZWG = 'ZWG', 'Zimbabwe Gold'


class BaseModel(models.Model):
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ExchangeRate(BaseModel):
    """USD-base exchange rate with historical tracking: 1 USD = rate * currency."""
    currency = models.CharField(
        max_length=3,
        choices=Currency.choices
    )
    rate = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        help_text="Exchange rate: 1 USD = rate * currency"
    )
    effective_date = models.DateTimeField(
        help_text="Date and time when this rate became effective"
    )

    class Meta:
        ordering = ['-effective_date']
        unique_together = [['currency', 'effective_date']]
        indexes = [
            models.Index(fields=['currency', '-effective_date']),
        ]

    def __str__(self):
        return f"1 USD = {self.rate} {self.currency} (effective {self.effective_date.date()})"
