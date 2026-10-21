"""Settings for the test suite: production settings plus fixed test secrets and a fast hasher."""

import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("FIELD_ENCRYPTION_KEYS", "3q2-7wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("MEDIA_ROOT", tempfile.mkdtemp(prefix="logistics-test-media-"))

from .settings import *  # noqa: F403

SECURE_SSL_REDIRECT = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # speed only; never in production
STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}  # noqa: F405
STATIC_ROOT = tempfile.mkdtemp(prefix="logistics-test-static-")
