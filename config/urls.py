from django.contrib import admin
from django.urls import include, path

from .api import api

admin.site.site_header = "Logistics Suite"
admin.site.site_title = "Logistics Suite admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("scanner/", include("apps.scanner.urls")),
]
