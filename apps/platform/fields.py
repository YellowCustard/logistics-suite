"""Field types shared by every module."""

from functools import cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db import models


@cache
def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(key.encode()) for key in settings.FIELD_ENCRYPTION_KEYS])


class EncryptedTextField(models.TextField):
    """Application-level encryption for personal information (POPIA).

    Values are Fernet tokens at rest, so they cannot be filtered or indexed. Key rotation: put the new
    key first in FIELD_ENCRYPTION_KEYS; old tokens still decrypt and are re-encrypted on next save.
    """

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:  # wrong key or tampered data: fail loudly, never return ciphertext
            raise ValueError(f"Cannot decrypt {self.model.__name__}.{self.name}") from exc

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return _fernet().encrypt(value.encode()).decode()


class MoneyField(models.DecimalField):
    """Money amount; always paired with a sibling `currency` CharField(3) on the model."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 18)
        kwargs.setdefault("decimal_places", 4)
        super().__init__(*args, **kwargs)


class CurrencyField(models.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 3)
        kwargs.setdefault("default", "ZAR")
        super().__init__(*args, **kwargs)
