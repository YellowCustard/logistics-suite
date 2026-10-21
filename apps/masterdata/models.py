"""Shared reference data. Every other module references these rows by id."""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q

from apps.platform.fields import CurrencyField, EncryptedTextField, MoneyField
from apps.platform.models import Audited, BaseModel


class Currency(Audited, models.Model):
    code = models.CharField(max_length=3, primary_key=True)
    name = models.CharField(max_length=60)
    minor_units = models.PositiveSmallIntegerField(default=2)

    class Meta:
        verbose_name_plural = "currencies"

    def __str__(self) -> str:
        return self.code


class FxRate(Audited, BaseModel):
    """Units of `quote` per 1 `base` on `rate_date` (e.g. base=USD, quote=ZAR)."""

    base = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="+")
    quote = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="+")
    rate_date = models.DateField()
    rate = models.DecimalField(max_digits=18, decimal_places=8, validators=[MinValueValidator(Decimal("0"))])
    source = models.CharField(max_length=60, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["base", "quote", "rate_date"], name="fx_rate_uq"),
            models.CheckConstraint(condition=Q(rate__gt=0), name="fx_rate_positive"),
            models.CheckConstraint(condition=~Q(base=F("quote")), name="fx_rate_distinct_currencies"),
        ]

    def __str__(self) -> str:
        return f"{self.base_id}/{self.quote_id} {self.rate_date} {self.rate}"


class TaxCode(Audited, BaseModel):
    code = models.CharField(max_length=20)
    description = models.CharField(max_length=120)
    rate = models.DecimalField(max_digits=6, decimal_places=4)  # 0.1500 = 15%
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["code", "valid_from"], name="tax_code_validity_uq"),
            models.CheckConstraint(condition=Q(rate__gte=0) & Q(rate__lt=1), name="tax_code_rate_range"),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gt=F("valid_from")), name="tax_code_window"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.rate:.2%})"


# --- Parties ------------------------------------------------------------------------------------------------


class Party(Audited, BaseModel):
    """A legal entity: client, carrier, consignee or supplier (one party may hold several roles)."""

    code = models.CharField(max_length=20, unique=True, help_text="Short code, e.g. SDB")
    legal_name = models.CharField(max_length=200)
    trading_name = models.CharField(max_length=200, blank=True)
    registration_no = models.CharField(max_length=40, blank=True)
    vat_number = models.CharField(max_length=20, blank=True)
    vat_exempt = models.BooleanField(default=False)
    sage_customer_code = models.CharField(max_length=40, blank=True)
    billing_email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "parties"

    def __str__(self) -> str:
        return f"{self.code} · {self.legal_name}"


class PartyRoleType(models.TextChoices):
    CLIENT = "client"
    CARRIER = "carrier"
    CONSIGNEE = "consignee"
    SUPPLIER = "supplier"


class PartyRole(Audited, BaseModel):
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="roles")
    role = models.CharField(max_length=20, choices=PartyRoleType.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["party", "role"], name="party_role_uq")]

    def __str__(self) -> str:
        return f"{self.party.code} · {self.role}"


# --- Sites -----------------------------------------------------------------------------------------------------


class SiteKind(models.TextChoices):
    WAREHOUSE = "warehouse"
    DEPOT = "depot"
    DELIVERY_ADDRESS = "delivery_address"


class Site(Audited, BaseModel):
    """Warehouses, depots and delivery addresses.

    Coordinates are plain decimals in M0; a PostGIS geography column and geofence polygon arrive with
    geofenced arrival (M3), when the PostGIS extension is enabled.
    """

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=SiteKind.choices)
    party = models.ForeignKey(
        Party,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        help_text="Owner of a delivery address (consignee or client)",
    )
    address = models.TextField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geofence_radius_m = models.PositiveIntegerField(default=150)
    pallet_capacity = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True) | (Q(latitude__gte=-90) & Q(latitude__lte=90)),
                name="site_latitude_range",
            ),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True) | (Q(longitude__gte=-180) & Q(longitude__lte=180)),
                name="site_longitude_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


# --- Items ---------------------------------------------------------------------------------------------------


class Item(Audited, BaseModel):
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="items")
    sku = models.CharField(max_length=60)
    description = models.CharField(max_length=255)
    base_uom = models.CharField(max_length=10, default="EA")
    weight_kg = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    volume_m3 = models.DecimalField(max_digits=12, decimal_places=5, null=True, blank=True)
    units_per_pallet = models.PositiveIntegerField(null=True, blank=True)
    lot_tracked = models.BooleanField(default=False)
    expiry_tracked = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["client", "sku"], name="item_client_sku_uq")]
        indexes = [models.Index(fields=["client", "is_active"], name="item_client_active_idx")]

    def __str__(self) -> str:
        return f"{self.sku} · {self.description}"


class ItemUom(Audited, BaseModel):
    """A pack size of an item, as an integer number of base units (e.g. CASE = 12 EA)."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="uoms")
    uom = models.CharField(max_length=10)
    factor = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["item", "uom"], name="item_uom_uq"),
            models.CheckConstraint(condition=Q(factor__gte=1), name="item_uom_factor_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.item.sku} {self.uom}×{self.factor}"


class ItemBarcode(Audited, BaseModel):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="barcodes")
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    barcode = models.CharField(max_length=64)
    uom = models.CharField(max_length=10, blank=True)

    class Meta:
        # Barcodes are unique per client: different clients may legitimately share an EAN.
        constraints = [models.UniqueConstraint(fields=["client", "barcode"], name="item_barcode_client_uq")]

    def save(self, *args, **kwargs):
        self.client_id = self.item.client_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.barcode


# --- Fleet and people --------------------------------------------------------------------------------------------


class Vehicle(Audited, BaseModel):
    registration = models.CharField(max_length=20, unique=True)
    fleet_number = models.CharField(max_length=20, blank=True)
    telematics_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    carrier = models.ForeignKey(Party, null=True, blank=True, on_delete=models.PROTECT, help_text="Empty for own fleet")
    capacity_kg = models.PositiveIntegerField(null=True, blank=True)
    pallet_slots = models.PositiveSmallIntegerField(null=True, blank=True)
    licence_disc_expiry = models.DateField(null=True, blank=True)
    roadworthy_expiry = models.DateField(null=True, blank=True)
    home_site = models.ForeignKey(Site, null=True, blank=True, on_delete=models.SET_NULL)
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.registration


class Person(Audited, BaseModel):
    """Drivers and staff. ID and phone numbers are encrypted at rest (POPIA)."""

    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    employee_code = models.CharField(max_length=30, null=True, blank=True, unique=True)
    carrier = models.ForeignKey(
        Party, null=True, blank=True, on_delete=models.PROTECT, help_text="Set for subcontractor drivers"
    )
    is_driver = models.BooleanField(default=False)
    id_number = EncryptedTextField(blank=True)
    phone = EncryptedTextField(blank=True)
    licence_expiry = models.DateField(null=True, blank=True)
    prdp_expiry = models.DateField(null=True, blank=True, help_text="Professional Driving Permit")
    user_id = models.UUIDField(null=True, blank=True, unique=True, help_text="platform User, if any")
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "people"

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"


# --- Contracts and rates ---------------------------------------------------------------------------------------------


class Contract(Audited, BaseModel):
    client = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="contracts")
    reference = models.CharField(max_length=40)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(default=30)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["client", "reference"], name="contract_client_ref_uq"),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")), name="contract_window"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.client.code} {self.reference}"


class RateCardStatus(models.TextChoices):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class RateCard(Audited, BaseModel):
    """Versioned price list. Published cards and their lines are immutable (enforced by a trigger)."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="rate_cards")
    number = models.PositiveIntegerField(help_text="Version number within the contract")
    status = models.CharField(max_length=20, choices=RateCardStatus.choices, default=RateCardStatus.DRAFT)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.UUIDField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["contract", "number"], name="rate_card_version_uq"),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")), name="rate_card_window"
            ),
            models.CheckConstraint(
                condition=~Q(status="published") | Q(published_at__isnull=False),
                name="rate_card_published_has_timestamp",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.contract} v{self.number} ({self.status})"


class ChargeType(models.TextChoices):
    STORAGE_PALLET_DAY = "STORAGE_PALLET_DAY"
    HANDLING_IN_PALLET = "HANDLING_IN_PALLET"
    HANDLING_IN_CARTON = "HANDLING_IN_CARTON"
    HANDLING_OUT_PALLET = "HANDLING_OUT_PALLET"
    PICK_CARTON = "PICK_CARTON"
    PICK_UNIT = "PICK_UNIT"
    PACK_HANDLING_UNIT = "PACK_HANDLING_UNIT"
    DELIVERY_DROP = "DELIVERY_DROP"
    DELIVERY_PALLET = "DELIVERY_PALLET"
    VAS_UNIT = "VAS_UNIT"
    ADMIN_FEE = "ADMIN_FEE"


class RateLine(Audited, BaseModel):
    rate_card = models.ForeignKey(RateCard, on_delete=models.CASCADE, related_name="lines")
    charge_type = models.CharField(max_length=40, choices=ChargeType.choices)
    uom = models.CharField(max_length=20, help_text="Billing unit, e.g. PALLET_DAY, CARTON, DROP")
    unit_price = MoneyField()
    currency = CurrencyField()
    min_charge = MoneyField(null=True, blank=True)
    site = models.ForeignKey(
        Site, null=True, blank=True, on_delete=models.PROTECT, help_text="Empty = applies at every site"
    )
    tax_code = models.CharField(max_length=20, default="STD")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="rate_line_price_non_negative"),
            models.CheckConstraint(
                condition=Q(min_charge__isnull=True) | Q(min_charge__gte=0), name="rate_line_min_non_negative"
            ),
            # One price per charge type and site on a card; NULL site counts as "all sites".
            models.UniqueConstraint(
                fields=["rate_card", "charge_type", "site"],
                name="rate_line_unique_price",
                nulls_distinct=False,
            ),
        ]

    def __str__(self) -> str:
        return f"{self.charge_type} {self.unit_price} {self.currency}/{self.uom}"
