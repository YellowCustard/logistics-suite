from django.apps import AppConfig


class PlatformConfig(AppConfig):
    name = "apps.platform"
    label = "platform"
    verbose_name = "Platform"

    def ready(self) -> None:
        from . import audit

        audit.connect()
