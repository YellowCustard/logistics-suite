"""M2 exit criterion: 20 orders a day for a simulated week pick and pack with zero manual database fixes.

Everything after master data goes through the HTTP API exactly as the client system, the scanner and the
supervisor would use it: ASNs, receipts and putaway; orders (with retried requests and cancellations); waves;
picking online and through offline scanner sync; short picks and the counts that follow them; a replenishment
that tops up backorders; packing into SSCC-labelled cartons and pallets; and outbound webhooks. The outbox
worker runs between steps as it would in production. Nothing touches the database directly, and at the end
the ledger, allocations, handling units, exception queue and webhook log all have to be consistent.
"""

import json
import random
import uuid
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db.models import Sum
from django.utils import timezone

from apps.masterdata.models import Item, ItemBarcode, ItemUom, Site
from apps.oms.models import OrderLine, SalesOrder
from apps.platform import outbox, webhooks
from apps.platform.models import EventDelivery, ExceptionCase, Role, WebhookDelivery
from apps.wms import sscc
from apps.wms.models import HandlingUnit, HandlingUnitContent, InventoryBalance, InventoryMovement, Location, PickTask

from .conftest import Api, api_for, make_user

pytestmark = pytest.mark.django_db

D = Decimal
DAYS = 7
ORDERS_PER_DAY = 20
SHORT_PICK_DAYS = {2, 5}
REPLENISH_DAY = 4
SEED = 20261027

# sku, description, case factor (None = eaches only), lot and expiry tracked
ITEMS = [
    ("WATER-500", "Still water 500ml", 24, False),
    ("JUICE-1L", "Orange juice 1l", 12, True),
    ("CHIPS-125", "Salted chips 125g", None, False),
    ("SODA-330", "Cola 330ml", 6, False),
    ("BAR-45", "Protein bar 45g", None, True),
]
STORES = [
    {"name": "Sandton Store", "address": "1 Rivonia Rd, Sandton"},
    {"name": "Rosebank Store", "address": "15 Cradock Ave, Rosebank"},
    {"name": "Midrand Store", "address": "9 New Rd, Midrand"},
    {"name": "Fourways Store", "address": "3 William Nicol Dr, Fourways"},
]


class StrictApi(Api):
    """Fails the test on any server error, so a 5xx can never hide behind a retry loop."""

    def _check(self, response):
        assert response.status_code < 500, response.content
        return response

    def get(self, path, **params):
        return self._check(super().get(path, **params))

    def post(self, path, json=None, headers=None):
        return self._check(super().post(path, json, headers))

    def delete(self, path, headers=None):
        return self._check(super().delete(path, headers))


def strict(api: Api) -> StrictApi:
    return StrictApi(api.client, api.headers)


def ok(response, status=200):
    assert response.status_code == status, (response.status_code, response.content)
    return response.json()


def page_all(api: Api, path: str, **params) -> list[dict]:
    items, cursor = [], None
    while True:
        query = params | ({"cursor": cursor} if cursor else {})
        body = ok(api.get(path, limit=200, **query))
        items += body["items"]
        cursor = body["next_cursor"]
        if not cursor:
            return items


class Warehouse:
    """The simulated site: actors, the outbox worker, and what is physically on the shelves."""

    def __init__(self, make_client, bearer_api, settings):
        settings.GS1_COMPANY_PREFIX = "6009876"
        self.sdb = make_client("SDB", "4111111111")
        self.site = Site.objects.create(code="TEAK", name="Teakwood", kind="warehouse")
        Location.objects.create(site=self.site, code="DOCK-1", kind="dock", pallet_capacity=0)
        for aisle in "ABC":
            for n in range(1, 11):
                Location.objects.create(
                    site=self.site,
                    code=f"{aisle}-{n:02d}",
                    kind="storage",
                    pick_sequence=ord(aisle) * 100 + n,
                    pallet_capacity=2,
                )
        self.items = {}
        for sku, description, case, tracked in ITEMS:
            item = Item.objects.create(
                client=self.sdb, sku=sku, description=description, lot_tracked=tracked, expiry_tracked=tracked
            )
            ItemUom.objects.create(item=item, uom="EA", factor=1)
            if case:
                ItemUom.objects.create(item=item, uom="CS", factor=case)
            ItemBarcode.objects.create(item=item, client=self.sdb, barcode=f"600{len(self.items):010d}", uom="EA")
            self.items[sku] = (item, case, tracked)

        self.client_system = strict(bearer_api(self.sdb))
        self.client_admin = strict(api_for(make_user("it@sdb.example", Role.CLIENT_ADMIN, client_ids=[self.sdb.pk])))
        grant = {"site_ids": [self.site.pk], "all_clients": True}
        self.supervisor = strict(api_for(make_user("sup@teak.example", Role.WAREHOUSE_SUPERVISOR, **grant)))
        self.manager = strict(api_for(make_user("mgr@teak.example", Role.SITE_MANAGER, **grant)))
        self.pickers = [
            strict(api_for(make_user(f"op{n}@teak.example", Role.WAREHOUSE_OPERATOR, **grant))) for n in (1, 2)
        ]
        self.phantom: Counter = Counter()  # (bin, sku, lot) -> units the ledger shows but the shelf doesn't have
        self.sent: list[dict] = []
        self.lots = Counter()

    # --- Infrastructure ------------------------------------------------------------------------------------------

    def run_worker(self):
        """What `run_outbox` does in a loop, until there is nothing left to do."""
        for _ in range(50):
            published = outbox.publish_pending(batch_size=500)
            delivered = outbox.deliver_pending(max_deliveries=500)
            sent = webhooks.send_due(limit=500)
            assert delivered.retried == delivered.dead == 0, delivered
            assert sent.retried == sent.dead == 0, sent
            if not published and not delivered.delivered and not delivered.duplicates and not sent.delivered:
                return
        raise AssertionError("outbox did not drain")

    # --- Inbound -------------------------------------------------------------------------------------------------

    def receive(self, ref: str, need: dict[str, int], day: int) -> None:
        """The client announces an ASN, the dock receives it pallet by pallet, operators put it all away."""
        lines = []
        for sku, qty in need.items():
            _, _, tracked = self.items[sku]
            remaining = qty
            while remaining > 0:
                pallet = min(remaining, 400)
                remaining -= pallet
                line = {"sku": sku, "qty": str(pallet)}
                if tracked:
                    self.lots[sku] += 1
                    line |= {
                        "lot": f"{sku[:3]}{self.lots[sku]}",
                        "expiry": (timezone.localdate() + timedelta(days=90 + 30 * self.lots[sku])).isoformat(),
                    }
                lines.append(line)
        notice = ok(
            self.client_system.post(
                "/inbound-notices",
                {"site_code": "TEAK", "expected_at": timezone.now().isoformat(), "client_ref": ref, "lines": lines},
            ),
            201,
        )
        receiver = self.pickers[day % 2]
        receipt = ok(receiver.post("/receipts", {"inbound_notice_id": notice["id"], "dock_code": "DOCK-1"}), 201)
        for expected, line in zip(notice["lines"], lines, strict=True):
            ok(
                receiver.post(f"/receipts/{receipt['id']}/lines", line | {"inbound_notice_line_id": expected["id"]}),
                201,
            )
        ok(receiver.post(f"/receipts/{receipt['id']}/complete"))
        self.run_worker()

        storage = [loc.code for loc in Location.objects.filter(site=self.site, kind="storage").order_by("code")]
        for task in page_all(receiver, "/tasks", site_id=str(self.site.pk)):
            target = task["suggested_location"]
            body = {"to_location_code": target} if target else {"to_location_code": storage[0], "reason": "no space"}
            ok(receiver.post(f"/putaway-tasks/{task['id']}/confirm", body))
        assert page_all(receiver, "/tasks", site_id=str(self.site.pk)) == []
        self.run_worker()

    # --- Outbound ------------------------------------------------------------------------------------------------

    def place(self, ref: str, store: dict, lines: list[dict]) -> tuple[dict, str]:
        key = f"sdb-erp-{ref}"
        body = {"site_code": "TEAK", "client_ref": ref, "ship_to": store, "lines": lines}
        return ok(self.client_system.post("/orders", body, headers={"Idempotency-Key": key}), 201), key

    def wave_and_pick(self, day: int) -> None:
        allocated = page_all(self.supervisor, "/orders", status="allocated", site_id=str(self.site.pk))
        if not allocated:
            return
        wave = ok(
            self.supervisor.post(
                "/waves", {"site_id": str(self.site.pk), "sales_order_ids": [o["id"] for o in allocated]}
            ),
            201,
        )
        ok(self.supervisor.post(f"/waves/{wave['id']}/release"))
        shorted = day not in SHORT_PICK_DAYS
        rounds = 0
        while tasks := page_all(self.pickers[0], "/pick-tasks", wave_id=wave["id"]):
            rounds += 1
            assert rounds < 10, "picking never finished"
            online, offline = tasks[::2], tasks[1::2]
            for task in online:
                qty = D(task["qty"])
                if not shorted and qty >= 2:
                    shorted = True
                    self.short(task, qty)
                    continue
                body = {"location_code": task["from_location"], "item_code": task["sku"], "qty": task["qty"]}
                ok(self.pickers[0].post(f"/pick-tasks/{task['id']}/confirm", body))
            self.sync_picks(offline, device=f"TC52-{day}-{rounds}")
            self.run_worker()
        completed = ok(self.supervisor.get(f"/waves/{wave['id']}"))
        assert completed["status"] == "completed", completed

    def short(self, task: dict, qty: D) -> None:
        """One unit is missing from the bin: pick what is there and report the rest."""
        found = qty - 1
        body = {"location_code": task["from_location"], "qty_found": str(found), "reason": "1 unit damaged, binned"}
        ok(self.pickers[0].post(f"/pick-tasks/{task['id']}/short", body))
        self.phantom[(task["from_location"], task["sku"], task["lot"])] += 1

    def sync_picks(self, tasks: list[dict], device: str) -> None:
        """The second picker works offline; the scanner pushes the queue when it reconnects (and once more)."""
        if not tasks:
            return
        commands = []
        for task in tasks:
            commands.append({"command_id": str(uuid.uuid4()), "type": "pick.claim", "payload": {"task_id": task["id"]}})
            payload = {
                "task_id": task["id"],
                "location_code": task["from_location"],
                "item_code": task["sku"],
                "qty": task["qty"],
            }
            commands.append({"command_id": str(uuid.uuid4()), "type": "pick.confirm", "payload": payload})
        for start in range(0, len(commands), 100):
            batch = {"device_id": device, "commands": commands[start : start + 100]}
            first = ok(self.pickers[1].post("/sync/push", batch))["results"]
            assert [r["status"] for r in first] == ["applied"] * len(batch["commands"]), first
            again = ok(self.pickers[1].post("/sync/push", batch))["results"]  # lost acknowledgement, resent
            assert all(r["replayed"] for r in again)

    def pack(self) -> None:
        packer = self.pickers[1]
        bench = ok(packer.get("/pack-orders", site_id=str(self.site.pk)))
        for order in bench:
            assert order["picking_done"], order
            lines = [line for line in order["lines"] if D(line["remaining"]) > 0]
            kind = "pallet" if len(lines) > 2 else "carton"
            unit = ok(packer.post("/handling-units", {"sales_order_id": order["sales_order_id"], "kind": kind}), 201)
            for line in lines:
                body = {"item_code": line["sku"], "qty": line["remaining"], "lot": line["lot"]}
                ok(packer.post(f"/handling-units/{unit['id']}/contents", body), 201)
            close = {"weight_kg": str(D("1.5") * len(lines))} | ({"cartons": len(lines)} if kind == "pallet" else {})
            ok(packer.post(f"/handling-units/{unit['id']}/close", close))
            label = packer.client.get(f"/api/v1/handling-units/{unit['id']}/label?format=zpl")
            assert label.status_code == 200 and unit["sscc"].encode() in label.content
        self.run_worker()
        assert ok(packer.get("/pack-orders", site_id=str(self.site.pk))) == []

    # --- Inventory control ---------------------------------------------------------------------------------------

    def count_shorted_bins(self) -> None:
        """The supervisor counts every bin with an open short-pick case; a manager approves big variances."""
        cases = page_all(self.supervisor, "/exceptions", status="open", kind="inventory.short_pick")
        if not cases:
            return
        bins = sorted({c["detail"]["location"] for c in cases})
        count = ok(self.supervisor.post("/cycle-counts", {"site_id": str(self.site.pk), "location_codes": bins}), 201)
        counter = self.pickers[0]
        for row in page_all(self.supervisor, "/inventory", site_id=str(self.site.pk)):
            if row["location_code"] not in bins:
                continue
            on_shelf = D(row["qty_on_hand"]) - self.phantom[(row["location_code"], row["sku"], row["lot"])]
            body = {
                "location_code": row["location_code"],
                "sku": row["sku"],
                "qty": str(on_shelf),
                "lot": row["lot"],
                "expiry": row["expiry"],
            }
            ok(counter.post(f"/cycle-counts/{count['id']}/lines", body), 201)
        submitted = ok(self.supervisor.post(f"/cycle-counts/{count['id']}/submit"))
        if submitted["status"] == "awaiting_approval":
            submitted = ok(self.manager.post(f"/cycle-counts/{count['id']}/approve", {"note": "Damaged on pick"}))
        assert submitted["status"] == "posted", submitted
        for key in [k for k in self.phantom if k[0] in bins]:
            del self.phantom[key]
        for case in cases:
            body = {"resolution": f"Counted in {count['number']}"}
            ok(self.supervisor.post(f"/exceptions/{case['id']}/resolve", body))
        self.run_worker()

    def end_of_day(self) -> None:
        call_command("reconcile_inventory")
        drift = ExceptionCase.objects.filter(kind__in=["inventory.ledger_drift", "inventory.allocation_drift"])
        assert not drift.exists(), list(drift.values("kind", "summary"))


def generate_orders(rng: random.Random) -> list[list[tuple[str, int, str]]]:
    """Every order of the week, decided up front so stock can be planned against true demand."""
    week = []
    for _ in range(DAYS * ORDERS_PER_DAY):
        skus = rng.sample([sku for sku, *_ in ITEMS], k=rng.choice([1, 1, 2, 2, 3, 4]))
        lines = []
        for sku in skus:
            case = next(c for s, _, c, _ in ITEMS if s == sku)
            if case and rng.random() < 0.4:
                lines.append((sku, rng.randint(1, 3), "CS"))
            else:
                lines.append((sku, rng.randint(1, 30), "EA"))
        week.append(lines)
    return week


def base_units(line: tuple[str, int, str]) -> int:
    sku, qty, uom = line
    case = next(c for s, _, c, _ in ITEMS if s == sku)
    return qty * (case if uom == "CS" else 1)


def test_simulated_week_picks_and_packs_with_zero_manual_fixes(make_client, bearer_api, settings, monkeypatch):
    rng = random.Random(SEED)  # noqa: S311 - a reproducible week, not a secret
    wh = Warehouse(make_client, bearer_api, settings)

    # The client's ERP listens for webhooks; every call is checked for a valid signature.
    def receiver(url, body, headers):
        wh.sent.append({"headers": headers, "body": json.loads(body)})
        assert webhooks.verify(secret, headers["Nuronteq-Signature"], body)
        return 200, "ok"

    monkeypatch.setattr(webhooks, "post", receiver)
    hook = ok(
        wh.client_admin.post(
            "/webhooks",
            {
                "url": "https://erp.sdb.example/nuronteq",
                "event_types": [
                    "oms.sales_order.accepted",
                    "oms.sales_order.cancelled",
                    "oms.sales_order.packed",
                    "wms.allocation.shorted",
                    "wms.handling_unit.closed",
                ],
            },
        ),
        201,
    )
    secret = hook["secret"]

    week = generate_orders(rng)
    demand = Counter()
    for order in week:
        for line in order:
            demand[line[0]] += base_units(line)
    # Half the week's demand on the shelves at the start, the rest (plus margin) arrives mid-week.
    wh.receive("PO-OPENING", {sku: demand[sku] // 2 for sku in demand}, day=0)
    received = Counter({sku: demand[sku] // 2 for sku in demand})

    placed: dict[str, dict] = {}
    cancelled: set[str] = set()
    backordered_seen = 0
    for day in range(1, DAYS + 1):
        todays = week[(day - 1) * ORDERS_PER_DAY : day * ORDERS_PER_DAY]
        for n, order_lines in enumerate(todays):
            ref = f"SDB-{day}{n:02d}"
            lines = [{"sku": sku, "qty": str(qty), "uom": uom} for sku, qty, uom in order_lines]
            order, key = wh.place(ref, STORES[n % len(STORES)], lines)
            placed[ref] = order
            if n == 7:  # the ERP's request timed out and it sent the order again with the same key
                replay = wh.client_system.post(
                    "/orders",
                    {"site_code": "TEAK", "client_ref": ref, "ship_to": STORES[n % len(STORES)], "lines": lines},
                    headers={"Idempotency-Key": key},
                )
                assert replay.status_code == 201 and replay.json()["id"] == order["id"]
                assert replay["Idempotent-Replayed"] == "true"
        wh.run_worker()

        # One customer cancels an order each morning before it is waved.
        victim = f"SDB-{day}13"
        ok(wh.client_system.post(f"/orders/{placed[victim]['id']}/cancel", {"reason": "Store closed for stocktake"}))
        cancelled.add(victim)
        wh.run_worker()

        backordered_seen += len(page_all(wh.supervisor, "/orders", status="backordered", site_id=str(wh.site.pk)))
        if day == REPLENISH_DAY:
            topup = {sku: demand[sku] - demand[sku] // 2 + demand[sku] // 10 for sku in demand}
            wh.receive("PO-REPLENISH", topup, day=day)
            received += topup
            ok(wh.supervisor.post("/allocations:run", {"site_id": str(wh.site.pk)}))
            wh.run_worker()

        wh.wave_and_pick(day)
        wh.pack()
        wh.count_shorted_bins()
        wh.end_of_day()

    # --- The week is over: everything has to add up ---------------------------------------------------------------

    orders = {o.client_ref: o for o in SalesOrder.objects.filter(client=wh.sdb)}
    assert len(orders) == DAYS * ORDERS_PER_DAY  # retries created nothing extra
    statuses = Counter(o.status for o in orders.values())
    assert statuses == {"packed": DAYS * ORDERS_PER_DAY - DAYS, "cancelled": DAYS}, statuses
    assert {ref for ref, o in orders.items() if o.status == "cancelled"} == cancelled
    assert backordered_seen > 0, "the plan should have run short before the replenishment"

    # Picked equals packed for every order line, and every unit is closed with a valid, unique SSCC.
    picked = defaultdict(D)
    for task in PickTask.objects.all():
        picked[task.allocation.order_line_id] += task.qty_picked
    packed = defaultdict(D)
    for row in HandlingUnitContent.objects.values("order_line_id").annotate(qty=Sum("qty")):
        packed[row["order_line_id"]] = row["qty"]
    assert {k: v for k, v in picked.items() if v} == dict(packed)
    units = list(HandlingUnit.objects.all())
    assert all(u.status == "closed" and sscc.is_valid(u.sscc) for u in units)
    assert len({u.sscc for u in units}) == len(units)

    # Every packed line got what was ordered, except what was short-picked with nowhere else to take it from.
    lines = OrderLine.objects.filter(order__status="packed")
    short_lines = [ln for ln in lines if packed[ln.pk] != ln.base_qty]
    assert all(packed[ln.pk] < ln.base_qty for ln in short_lines)

    # Stock: received minus picked minus counted losses, per item; nothing left reserved; ledger matches balances.
    for sku, (item, _, _) in wh.items.items():
        on_hand = InventoryBalance.objects.filter(item=item).aggregate(q=Sum("qty_on_hand"))["q"]
        moved = InventoryMovement.objects.filter(balance__item=item).aggregate(q=Sum("qty_delta"))["q"]
        issued = sum(v for line_id, v in picked.items() if OrderLine.objects.get(pk=line_id).item_id == item.pk)
        counted = (
            InventoryMovement.objects.filter(balance__item=item, kind="adjust", source_type="wms.cyclecount").aggregate(
                q=Sum("qty_delta")
            )["q"]
            or 0
        )
        assert on_hand == moved == D(received[sku]) - issued + counted, sku
    assert InventoryBalance.objects.aggregate(q=Sum("qty_allocated"))["q"] == 0
    assert not wh.phantom

    # Nothing left for a human to clean up, and no event or webhook was lost.
    assert list(ExceptionCase.objects.exclude(status="resolved").values_list("kind", "summary")) == []
    assert not EventDelivery.objects.exclude(status="delivered").exists()
    assert not WebhookDelivery.objects.exclude(status="delivered").exists()
    packed_hooks = {m["body"]["data"]["number"] for m in wh.sent if m["body"]["type"] == "oms.sales_order.packed"}
    assert packed_hooks == {o.number for o in orders.values() if o.status == "packed"}
    types = Counter(m["body"]["type"] for m in wh.sent)
    assert types["oms.sales_order.accepted"] == DAYS * ORDERS_PER_DAY
    assert types["oms.sales_order.cancelled"] == DAYS
    assert types["wms.handling_unit.closed"] == len(units)
