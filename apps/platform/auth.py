"""Principals, scoping and API tokens.

A Principal is what every API endpoint and service sees: who is acting and which clients and sites
they may touch. Scoping is applied in one place (`Principal.scope`) so a missing filter fails closed.
"""

import secrets
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.db.models import QuerySet
from django.utils import timezone
from ninja.security import APIKeyCookie, HttpBearer

from .context import Actor, actor_var
from .models import CLIENT_ROLES, ApiClient, Role, User

TOKEN_SALT = "platform.api-token.v1"  # noqa: S105 - signing namespace, not a secret


@dataclass(frozen=True, slots=True)
class Principal:
    kind: str  # "user" | "api_client"
    id: uuid.UUID
    label: str
    roles: frozenset[str]
    all_clients: bool
    client_ids: frozenset[uuid.UUID]
    all_sites: bool
    site_ids: frozenset[uuid.UUID]

    @property
    def idempotency_scope(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def is_client_scoped(self) -> bool:
        """True for client users and client systems: they only ever see one client's data."""
        return self.kind == "api_client" or bool(self.roles & CLIENT_ROLES)

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles)) or Role.SYSTEM_ADMIN in self.roles

    def can_see_client(self, client_id: uuid.UUID | None) -> bool:
        return self.all_clients or (client_id is not None and client_id in self.client_ids)

    def can_see_site(self, site_id: uuid.UUID | None) -> bool:
        return self.all_sites or (site_id is not None and site_id in self.site_ids)

    def scope(self, qs: QuerySet, *, client_field: str | None = "client_id", site_field: str | None = None) -> QuerySet:
        if client_field and not self.all_clients:
            qs = qs.filter(**{f"{client_field}__in": self.client_ids})
        if site_field and not self.all_sites:
            qs = qs.filter(**{f"{site_field}__in": self.site_ids})
        return qs


def principal_for_user(user: User) -> Principal:
    if user.is_superuser:
        return Principal(
            "user", user.pk, user.email, frozenset({Role.SYSTEM_ADMIN}), True, frozenset(), True, frozenset()
        )
    roles: set[str] = set()
    client_ids: set[uuid.UUID] = set()
    site_ids: set[uuid.UUID] = set()
    all_clients = all_sites = False
    for grant in user.role_grants.all():
        roles.add(grant.role)
        all_clients |= grant.all_clients
        all_sites |= grant.all_sites
        client_ids.update(grant.client_ids)
        site_ids.update(grant.site_ids)
    if roles & CLIENT_ROLES:
        # A client user never gets cross-client visibility, whatever else they hold.
        all_clients = False
    return Principal(
        "user",
        user.pk,
        user.email,
        frozenset(roles),
        all_clients,
        frozenset(client_ids),
        all_sites,
        frozenset(site_ids),
    )


def principal_for_api_client(api_client: ApiClient) -> Principal:
    return Principal(
        "api_client",
        api_client.pk,
        api_client.name,
        frozenset({"client_system"}),
        False,
        frozenset({api_client.client_id}),
        True,
        frozenset(),
    )


# --- API client credentials and tokens ------------------------------------------------------------


def create_api_client(*, name: str, client_id: uuid.UUID, scopes: list[str] | None = None) -> tuple[ApiClient, str]:
    """Returns the ApiClient and its plaintext secret, which is shown once and never stored."""
    secret = secrets.token_urlsafe(32)
    api_client = ApiClient.objects.create(
        name=name,
        client_id=client_id,
        key_id=f"lk_{secrets.token_hex(8)}",
        secret_hash=make_password(secret),
        scopes=scopes or [],
    )
    return api_client, secret


def rotate_secret(api_client: ApiClient) -> str:
    """A new secret for the same key id; the old secret stops working at once. Tokens already issued stay valid
    until they expire (API_TOKEN_TTL_SECONDS); revoke the credential to cut them off immediately."""
    secret = secrets.token_urlsafe(32)
    api_client.secret_hash = make_password(secret)
    api_client.save(update_fields=["secret_hash", "updated_at"])
    return secret


def issue_token(key_id: str, secret: str) -> tuple[str, int] | None:
    api_client = ApiClient.objects.filter(key_id=key_id, is_active=True).first()
    if api_client is None:
        make_password(secret)  # run the hasher anyway so unknown keys take as long as wrong secrets
        return None
    if not check_password(secret, api_client.secret_hash):
        return None
    ApiClient.objects.filter(pk=api_client.pk).update(last_used_at=timezone.now())
    token = signing.dumps({"sub": str(api_client.pk)}, salt=TOKEN_SALT, compress=True)
    return token, settings.API_TOKEN_TTL_SECONDS


def api_client_from_token(token: str) -> ApiClient | None:
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=settings.API_TOKEN_TTL_SECONDS)
        api_client_id = uuid.UUID(data["sub"])
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        return None
    return ApiClient.objects.filter(pk=api_client_id, is_active=True).first()


# --- Ninja auth classes -------------------------------------------------------------------------------


class BearerTokenAuth(HttpBearer):
    def authenticate(self, request, token: str) -> Principal | None:
        api_client = api_client_from_token(token)
        if api_client is None:
            return None
        principal = principal_for_api_client(api_client)
        actor_var.set(Actor(kind="api_client", id=api_client.pk, label=api_client.name))
        return principal


class PrincipalSessionAuth(APIKeyCookie):
    """Django session auth (with Ninja's CSRF check) that yields a Principal."""

    param_name = settings.SESSION_COOKIE_NAME

    def authenticate(self, request, key: str | None) -> Principal | None:
        user = request.user
        if not user.is_authenticated or not user.is_active:
            return None
        return principal_for_user(user)
