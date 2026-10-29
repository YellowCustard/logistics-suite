from django.apps import AppConfig


class WmsConfig(AppConfig):
    name = "apps.wms"
    label = "wms"
    verbose_name = "Warehouse"

    def ready(self) -> None:
        from . import admin_dashboard

        admin_dashboard.register()
