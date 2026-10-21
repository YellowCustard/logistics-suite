from django.contrib import admin

from .models import Location


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ["code", "site", "zone", "kind", "pallet_capacity", "is_active"]
    list_filter = ["site", "kind", "zone"]
    search_fields = ["code"]
