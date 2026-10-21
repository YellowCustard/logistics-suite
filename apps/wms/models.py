"""Warehouse module. M0 holds bin locations only; receiving, inventory and picking arrive in M1–M2."""

from django.db import models
from django.db.models import Q

from apps.masterdata.models import Site
from apps.platform.models import Audited, BaseModel


class LocationKind(models.TextChoices):
    STORAGE = "storage"
    PICK_FACE = "pick_face"
    DOCK = "dock"
    STAGING = "staging"
    QUARANTINE = "quarantine"


class Location(Audited, BaseModel):
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=30, help_text="e.g. A-01-03-B (zone-aisle-level-position)")
    zone = models.CharField(max_length=20, blank=True)
    kind = models.CharField(max_length=20, choices=LocationKind.choices, default=LocationKind.STORAGE)
    pallet_capacity = models.PositiveSmallIntegerField(default=1)
    pick_sequence = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["site", "code"], name="location_site_code_uq"),
            models.CheckConstraint(condition=Q(pallet_capacity__gte=0), name="location_capacity_non_negative"),
        ]
        indexes = [models.Index(fields=["site", "pick_sequence"], name="location_pick_path_idx")]

    def __str__(self) -> str:
        return self.code
