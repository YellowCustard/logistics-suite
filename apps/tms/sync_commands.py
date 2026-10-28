"""Driver commands that can be queued offline. Each calls the matching API function, so the checks are exactly
those of the online endpoint. Evidence is uploaded before the POD that names it."""

import uuid

from ninja import Schema

from apps.platform import sync

from . import api


def _out(value) -> dict:
    return api.RoadTripOut.model_validate(value).model_dump(mode="json")


class StopCommand(Schema):
    trip_id: uuid.UUID
    stop_id: uuid.UUID


class ArriveCommand(api.ArriveIn, StopCommand):
    pass


class PodCommand(api.PodIn, StopCommand):
    pass


class FailedCommand(api.FailedIn, StopCommand):
    pass


class CompleteCommand(api.CompleteIn):
    trip_id: uuid.UUID


def _body(schema, cmd) -> Schema:
    return schema(**cmd.model_dump(exclude={"trip_id", "stop_id"}))


@sync.command("trip.arrive", ArriveCommand)
def arrive(request, cmd: ArriveCommand, command_id: uuid.UUID) -> dict:
    return _out(api.arrive(request, cmd.trip_id, cmd.stop_id, _body(api.ArriveIn, cmd)))


@sync.command("trip.pod", PodCommand)
def pod(request, cmd: PodCommand, command_id: uuid.UUID) -> dict:
    return _out(api.record_pod(request, cmd.trip_id, cmd.stop_id, _body(api.PodIn, cmd)))


@sync.command("trip.failed", FailedCommand)
def failed(request, cmd: FailedCommand, command_id: uuid.UUID) -> dict:
    return _out(api.record_failure(request, cmd.trip_id, cmd.stop_id, _body(api.FailedIn, cmd)))


@sync.command("trip.complete", CompleteCommand)
def complete(request, cmd: CompleteCommand, command_id: uuid.UUID) -> dict:
    return _out(api.complete(request, cmd.trip_id, _body(api.CompleteIn, cmd)))
