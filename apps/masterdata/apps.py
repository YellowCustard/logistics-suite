from django.apps import AppConfig


class MasterdataConfig(AppConfig):
    name = "apps.masterdata"
    label = "masterdata"
    verbose_name = "Master data"

    def ready(self) -> None:
        from . import admin_dashboard

        admin_dashboard.register()
