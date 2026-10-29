from django.contrib import admin
from django.contrib.auth.models import Group
from django.urls import include, path

from .api import api

admin.site.site_header = "Nuronteq"
admin.site.site_title = "Nuronteq admin"
admin.site.site_url = "/portal/"  # "View site"; nothing is served at /
admin.site.enable_nav_sidebar = False  # the Nuronteq shell has its own sidebar
# Access is RoleGrant-based; Django groups are unused, so they stay out of the admin. Unregistered here because
# django.contrib.auth registers GroupAdmin after apps.platform's admin module has loaded.
admin.site.unregister(Group)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("scanner/", include("apps.scanner.urls")),
    path("portal/", include("apps.portal.urls")),
]
