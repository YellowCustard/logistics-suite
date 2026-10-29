from django.apps import AppConfig


class TmsConfig(AppConfig):
    name = "apps.tms"
    label = "tms"
    verbose_name = "Transport"

    def ready(self) -> None:
        from . import admin_dashboard

        admin_dashboard.register()
