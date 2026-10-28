"""Invite links for subcontracted drivers: one trip, no password, expires on its own."""

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.platform.apiutils import Problem
from apps.platform.models import Role, RoleGrant, User

from .models import CarrierLink, Trip, TripStatus

MAX_VALID = timedelta(days=7)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def live() -> Q:
    return Q(revoked_at__isnull=True, expires_at__gt=timezone.now())


@transaction.atomic
def create(trip_id: uuid.UUID, *, valid_for: timedelta, driver_name: str, created_by: uuid.UUID | None):
    """Returns (link, token). The token is shown once; a new link revokes the trip's earlier ones."""
    trip = Trip.objects.select_for_update().filter(pk=trip_id).first()
    if trip is None:
        raise Problem(404, "Not found")
    if trip.carrier_id is None:
        raise Problem(409, "Not a carrier trip", "Assign a carrier before inviting their driver.")
    if trip.status not in (TripStatus.PLANNING, TripStatus.DISPATCHED):
        raise Problem(409, "Trip closed", f"{trip.number} is {trip.status}.")
    if not timedelta(minutes=10) <= valid_for <= MAX_VALID:
        raise Problem(422, "Invalid validity", "A link lasts between 10 minutes and 7 days.")
    now = timezone.now()
    for old in CarrierLink.objects.select_for_update().filter(live(), trip=trip):
        _revoke(old, now)
    token = secrets.token_urlsafe(32)
    link = CarrierLink.objects.create(
        trip=trip,
        carrier_id=trip.carrier_id,
        token_sha256=_hash(token),
        driver_name=driver_name.strip(),
        expires_at=now + valid_for,
        created_by=created_by,
    )
    return link, token


def _revoke(link: CarrierLink, now) -> None:
    link.revoked_at = now
    link.save()
    if link.user_id:
        User.objects.filter(pk=link.user_id).update(is_active=False)  # ends any session on the next request


@transaction.atomic
def revoke_all(trip_id: uuid.UUID) -> int:
    now = timezone.now()
    links = list(CarrierLink.objects.select_for_update().filter(live(), trip_id=trip_id))
    for link in links:
        _revoke(link, now)
    return len(links)


@transaction.atomic
def redeem(token: str) -> tuple[CarrierLink, User]:
    link = CarrierLink.objects.select_for_update().filter(token_sha256=_hash(token)).first()
    if link is None or link.revoked_at is not None or link.expires_at <= timezone.now():
        raise Problem(401, "Link not valid", "This link has expired or was replaced. Ask the dispatcher for a new one.")
    if link.trip.status not in (TripStatus.PLANNING, TripStatus.DISPATCHED):
        raise Problem(401, "Link not valid", "This trip is finished.")
    if link.user_id is None:
        user = User.objects.create_user(email=f"carrier-link-{link.pk}@links.invalid", password=None)
        RoleGrant.objects.create(user=user, role=Role.CARRIER_DRIVER)
        link.user_id, link.redeemed_at = user.pk, timezone.now()
        link.save()
    else:
        user = User.objects.get(pk=link.user_id)
    return link, user


def trips_for(user_id: uuid.UUID):
    return Trip.objects.filter(pk__in=CarrierLink.objects.filter(live(), user_id=user_id).values("trip_id"))
