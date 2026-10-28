"""Client self-service: a client admin manages the portal users and API credentials of their own client.

There is no email in Phase 1. Inviting a user returns a one-time setup link that the admin passes on; the link
carries Django's password-reset token, so it expires after PASSWORD_RESET_TIMEOUT and stops working once a
password is set. The token travels in the URL fragment, which browsers never send to a server or in a Referer.
"""

import uuid

from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from . import audit
from .apiutils import Problem
from .models import CLIENT_ROLES, RoleGrant, User

SETUP_PATH = "/portal/welcome"


def client_users(client_id: uuid.UUID) -> QuerySet:
    """Users who belong to this client only: a client role pinned to it, no other grant, not staff."""
    members = RoleGrant.objects.filter(role__in=CLIENT_ROLES, client_ids=[client_id]).values("user_id")
    elsewhere = RoleGrant.objects.exclude(role__in=CLIENT_ROLES, client_ids=[client_id]).values("user_id")
    return (
        User.objects.filter(pk__in=members, is_staff=False, is_superuser=False)
        .exclude(pk__in=elsewhere)
        .prefetch_related("role_grants")
    )


def role_of(user: User) -> str:
    return next((g.role for g in user.role_grants.all() if g.role in CLIENT_ROLES), "")


def setup_link(user: User) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return f"{SETUP_PATH}#u={uid}&t={default_token_generator.make_token(user)}"


@transaction.atomic
def invite(client_id: uuid.UUID, *, email: str, full_name: str, role: str) -> tuple[User, str]:
    email = User.objects.normalize_email(email.strip()).lower()
    try:
        validate_email(email)
    except ValidationError:
        raise Problem(422, "Invalid email", "Enter a valid email address.") from None
    if role not in CLIENT_ROLES:
        raise Problem(422, "Invalid role", "A portal user is a client_admin or a client_user.")
    # One message whether the address is a user of another client or of staff, so nothing more is revealed.
    taken = Problem(409, "Email in use", "That email address already has an account. Ask support if it should move.")
    if User.objects.filter(email__iexact=email).exists():
        raise taken
    try:
        with transaction.atomic():
            user = User.objects.create_user(email=email, password=None, full_name=full_name.strip())
    except IntegrityError:
        raise taken from None
    RoleGrant.objects.create(user=user, role=role, client_ids=[client_id])
    return user, setup_link(user)


@transaction.atomic
def new_setup_link(user: User, client_id: uuid.UUID) -> str:
    """A fresh link. For someone who already has a password this is a reset: the old password stops working and
    their open sessions end (Django ties a session to the password hash)."""
    if user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=["password"])
        audit.record(action="password_reset", entity_type="platform.user", entity_id=user.pk, client_id=client_id)
    return setup_link(user)


@transaction.atomic
def update(user: User, client_id: uuid.UUID, *, acting_user_id: uuid.UUID, changes: dict) -> User:
    if user.pk == acting_user_id and ("role" in changes or changes.get("is_active") is False):
        raise Problem(409, "Not on yourself", "Another admin must change your role or deactivate you.")
    if "full_name" in changes:
        user.full_name = changes["full_name"].strip()
    if "is_active" in changes:
        user.is_active = changes["is_active"]  # an inactive user's session stops authenticating at once
    user.save()
    if "role" in changes:
        if changes["role"] not in CLIENT_ROLES:
            raise Problem(422, "Invalid role", "A portal user is a client_admin or a client_user.")
        grant = RoleGrant.objects.select_for_update().get(user=user, role__in=CLIENT_ROLES, client_ids=[client_id])
        if grant.role != changes["role"]:
            grant.role = changes["role"]
            grant.save()
    return client_users(client_id).get(pk=user.pk)


def complete_setup(uid: str, token: str, password: str) -> User:
    """Sets the password of a portal user from a setup link."""
    invalid = Problem(400, "Link not valid", "This link has expired or was already used. Ask your admin for a new one.")
    try:
        user_id = uuid.UUID(force_str(urlsafe_base64_decode(uid)))
    except (ValueError, TypeError):
        raise invalid from None
    user = User.objects.filter(pk=user_id, is_active=True, role_grants__role__in=CLIENT_ROLES).first()
    if user is None or not default_token_generator.check_token(user, token):
        raise invalid
    try:
        validate_password(password, user)
    except ValidationError as exc:
        raise Problem(422, "Password not accepted", " ".join(exc.messages)) from None
    user.set_password(password)
    user.save(update_fields=["password"])
    return user
