from django.apps import AppConfig


class OmsConfig(AppConfig):
    name = "apps.oms"
    label = "oms"
    verbose_name = "Orders"

    def ready(self) -> None:
        from . import admin_dashboard

        admin_dashboard.register()
