"""Django settings. All environment-specific values come from environment variables."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Environment variable {name} is required")
    return value or ""


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-only-key" if DEBUG else None, required=not DEBUG)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# Fernet key (urlsafe base64, 32 bytes) for POPIA-sensitive fields. Several keys may be given,
# comma-separated, newest first, to allow rotation.
FIELD_ENCRYPTION_KEYS = [
    k
    for k in env(
        "FIELD_ENCRYPTION_KEYS", "Wd8vKCkmQ8D3y1q4mL3o0n7xjv6kPq2s9e5bZtF1uYc=" if DEBUG else "", required=not DEBUG
    ).split(",")
    if k
]

INSTALLED_APPS = [
    "apps.platform",  # first, so its admin/base_site.html (Nuronteq skin) overrides the stock one
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "apps.masterdata",
    "apps.oms",
    "apps.wms",
    "apps.tms",
    "apps.scanner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "apps.platform.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.platform.middleware.ActorContextMiddleware",
    "apps.platform.idempotency.IdempotencyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", "postgres://logistics:logistics@localhost:5432/logistics"),
        conn_max_age=60,
        conn_health_checks=True,
    )
}
DATABASES["default"]["OPTIONS"] = {"options": "-c statement_timeout=30000"}

AUTH_USER_MODEL = "platform.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-za"
TIME_ZONE = "Africa/Johannesburg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "media")))
STORAGES = {
    # Swap "default" for S3 (django-storages) in production; documents only store the key.
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# Trust X-Forwarded-Proto only behind a proxy that sets it and strips client-supplied copies.
if env_bool("DJANGO_BEHIND_TLS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    NINJA_NUM_PROXIES = 1  # rate limits key on the client address Caddy puts last in X-Forwarded-For
else:
    NINJA_NUM_PROXIES = 0  # ignore X-Forwarded-For entirely; anyone could forge it
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SECURE_REDIRECT_EXEMPT = [r"^api/v1/(healthz|readyz)$"]  # load balancer probes arrive over plain HTTP
# Start short; raise to 31536000 once every subdomain is known to serve HTTPS.
SECURE_HSTS_SECONDS = int(env("DJANGO_SECURE_HSTS_SECONDS", "0" if DEBUG else "3600"))
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Platform behaviour
API_TOKEN_TTL_SECONDS = int(env("API_TOKEN_TTL_SECONDS", "3600"))
IDEMPOTENCY_TTL_HOURS = int(env("IDEMPOTENCY_TTL_HOURS", "48"))
OUTBOX_MAX_ATTEMPTS = int(env("OUTBOX_MAX_ATTEMPTS", "8"))
# A stock correction larger than this share of the system quantity (or any correction of a zero balance)
# needs site manager approval when it comes from a count, and opens an exception case when made directly.
LOGIN_RATE_LIMIT = env("LOGIN_RATE_LIMIT", "10/m")
# Incremental scanner pulls re-read this far behind the cursor, so a row committed late by a slow transaction
# is not missed. Devices upsert by id, so overlap costs only bandwidth.
SYNC_PULL_OVERLAP_SECONDS = int(env("SYNC_PULL_OVERLAP_SECONDS", "300"))
INVENTORY_VARIANCE_APPROVAL_PCT = float(env("INVENTORY_VARIANCE_APPROVAL_PCT", "2"))
# SSCCs on handling units: your GS1 company prefix (7-10 digits, from GS1 South Africa) and extension digit.
# Without a prefix, packing refuses to open handling units. "0000000" is for local development only.
GS1_COMPANY_PREFIX = env("GS1_COMPANY_PREFIX", "0000000" if DEBUG else "")
SSCC_EXTENSION_DIGIT = env("SSCC_EXTENSION_DIGIT", "0")
# Outbound webhooks to client systems. The two ALLOW flags exist for local testing only: never set them on a
# server, because they let webhooks reach internal addresses.
WEBHOOK_TIMEOUT_SECONDS = float(env("WEBHOOK_TIMEOUT_SECONDS", "10"))
WEBHOOK_MAX_ATTEMPTS = int(env("WEBHOOK_MAX_ATTEMPTS", "10"))
WEBHOOK_ALLOW_INSECURE = env_bool("WEBHOOK_ALLOW_INSECURE", False)
WEBHOOK_ALLOW_PRIVATE_NETWORKS = env_bool("WEBHOOK_ALLOW_PRIVATE_NETWORKS", False)
# Telematics. TELEMATICS_ADAPTER is the dotted path of a TelematicsPort implementation; empty means no polling.
# Until MiX Integrate credentials arrive, staging can use "apps.integrations.telematics.fake.FakeTelematics" with
# the fleet simulator. The webhook secret signs vendor pushes to /api/v1/integrations/telematics/webhook.
TELEMATICS_ADAPTER = env("TELEMATICS_ADAPTER", "")
TELEMATICS_WEBHOOK_SECRET = env("TELEMATICS_WEBHOOK_SECRET", "")
TELEMATICS_POLL_SECONDS = int(env("TELEMATICS_POLL_SECONDS", "60"))
# Positions older than this are deleted (POPIA: 13 months, as in the driver tracking notice).
TELEMATICS_RETENTION_DAYS = int(env("TELEMATICS_RETENTION_DAYS", "395"))
# Feature flag: mark a stop arrived when the trip's vehicle enters the site's geofence.
GEOFENCE_AUTO_ARRIVE = env_bool("GEOFENCE_AUTO_ARRIVE", True)
# Rough ETA until a routing engine is connected: straight-line distance x road factor at this average speed, plus
# a fixed time at each stop still ahead.
ETA_AVERAGE_KMH = float(env("ETA_AVERAGE_KMH", "40"))
ETA_ROAD_FACTOR = float(env("ETA_ROAD_FACTOR", "1.3"))
ETA_MINUTES_PER_STOP = int(env("ETA_MINUTES_PER_STOP", "15"))
# Endpoints that authenticate or deduplicate differently (token grant, vendor webhooks, offline sync).
IDEMPOTENCY_EXEMPT_PREFIXES = ["/api/v1/auth/", "/api/v1/integrations/", "/api/v1/sync/"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "apps.platform.logging.JsonFormatter",
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
