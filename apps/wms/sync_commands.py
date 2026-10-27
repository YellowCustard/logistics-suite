"""Scanner commands that can be queued offline. Each one calls the matching API function, so the checks
(roles, site and client scope, business rules) are exactly those of the online endpoint."""

import uuid

from ninja import Schema

from apps.platform import sync
from apps.platform.apiutils import Problem, principal_of

from . import api
from .models import Receipt


def _out(schema, value) -> dict:
    value = getattr(value, "value", value)  # unwrap ninja Status
    return schema.from_orm(value).model_dump(mode="json")


def _with_command(cmd, command_id: uuid.UUID):
    return cmd.model_copy(update={"command_id": command_id})


class ReceiptLineCommand(api.ReceiptLineIn):
    inbound_notice_id: uuid.UUID
    dock_code: str


class ReceiptCompleteCommand(Schema):
    inbound_notice_id: uuid.UUID


class TaskCommand(Schema):
    task_id: uuid.UUID


class PutawayConfirmCommand(api.PutawayConfirmIn):
    task_id: uuid.UUID


class CountLineCommand(api.CountLineIn):
    count_id: uuid.UUID


def _receipt_for_notice(request, inbound_notice_id: uuid.UUID) -> Receipt | None:
    principal = principal_of(request)
    return api._staff_scoped(principal, Receipt.objects.all()).filter(inbound_notice_id=inbound_notice_id).first()


@sync.command("receipt.line", ReceiptLineCommand)
def receipt_line(request, cmd: ReceiptLineCommand, command_id: uuid.UUID) -> dict:
    """Scan at the dock against an ASN. The first line opens the receipt, so the device never needs its id."""
    receipt = _receipt_for_notice(request, cmd.inbound_notice_id)
    if receipt is None:
        body = api.ReceiptIn(inbound_notice_id=cmd.inbound_notice_id, dock_code=cmd.dock_code)
        receipt = api.open_receipt(request, body).value
    body = api.ReceiptLineIn(**cmd.model_dump(exclude={"inbound_notice_id", "dock_code"}))
    line = api.add_receipt_line(request, receipt.pk, _with_command(body, command_id))
    return _out(api.ReceiptLineOut, line) | {"receipt_id": str(receipt.pk)}


@sync.command("receipt.complete", ReceiptCompleteCommand)
def receipt_complete(request, cmd: ReceiptCompleteCommand, command_id: uuid.UUID) -> dict:
    receipt = _receipt_for_notice(request, cmd.inbound_notice_id)
    if receipt is None:
        raise Problem(409, "Nothing received", "Scan at least one line before completing the receipt.")
    return _out(api.ReceiptOut, api.complete_receipt(request, receipt.pk))


@sync.command("putaway.claim", TaskCommand)
def putaway_claim(request, cmd: TaskCommand, command_id: uuid.UUID) -> dict:
    return _out(api.TaskOut, api.claim_task(request, cmd.task_id))


@sync.command("putaway.confirm", PutawayConfirmCommand)
def putaway_confirm(request, cmd: PutawayConfirmCommand, command_id: uuid.UUID) -> dict:
    body = api.PutawayConfirmIn(**cmd.model_dump(exclude={"task_id"}))
    return _out(api.TaskOut, api.confirm_putaway(request, cmd.task_id, _with_command(body, command_id)))


@sync.command("count.line", CountLineCommand)
def count_line(request, cmd: CountLineCommand, command_id: uuid.UUID) -> dict:
    body = api.CountLineIn(**cmd.model_dump(exclude={"count_id"}))
    return _out(api.BlindLineOut, api.record_count_line(request, cmd.count_id, _with_command(body, command_id)))


@sync.command("inventory.move", api.MoveIn)
def inventory_move(request, cmd: api.MoveIn, command_id: uuid.UUID) -> dict:
    return _out(api.MovementOut, api.move_stock(request, _with_command(cmd, command_id)))


class PickConfirmCommand(api.PickConfirmIn):
    task_id: uuid.UUID


class PickShortCommand(api.PickShortIn):
    task_id: uuid.UUID


@sync.command("pick.claim", TaskCommand)
def pick_claim(request, cmd: TaskCommand, command_id: uuid.UUID) -> dict:
    return _out(api.PickTaskOut, api.claim_task(request, cmd.task_id))


@sync.command("pick.confirm", PickConfirmCommand)
def pick_confirm(request, cmd: PickConfirmCommand, command_id: uuid.UUID) -> dict:
    body = api.PickConfirmIn(**cmd.model_dump(exclude={"task_id"}))
    return _out(api.PickTaskOut, api.confirm_pick(request, cmd.task_id, _with_command(body, command_id)))


@sync.command("pick.short", PickShortCommand)
def pick_short(request, cmd: PickShortCommand, command_id: uuid.UUID) -> dict:
    body = api.PickShortIn(**cmd.model_dump(exclude={"task_id"}))
    return _out(api.PickTaskOut, api.short_pick(request, cmd.task_id, _with_command(body, command_id)))
