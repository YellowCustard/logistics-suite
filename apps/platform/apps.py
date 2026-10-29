from django.apps import AppConfig


class PlatformConfig(AppConfig):
    name = "apps.platform"
    label = "platform"
    verbose_name = "Platform"

    def ready(self) -> None:
        from . import admin_dashboard, audit

        audit.connect()
        admin_dashboard.register()
