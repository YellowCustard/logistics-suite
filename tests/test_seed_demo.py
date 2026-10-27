"""The staging demo data command."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.masterdata.models import Item, Site
from apps.oms.models import InboundNotice
from apps.platform.models import User
from apps.wms import ledger
from apps.wms.models import InventoryBalance, LocationKind

pytestmark = pytest.mark.django_db


def test_refuses_without_confirmation():
    with pytest.raises(CommandError):
        call_command("seed_demo")
    assert not Site.objects.filter(code="DEMO-JHB").exists()


def test_loads_stock_through_the_real_services_once():
    out = StringIO()
    call_command("seed_demo", "--not-production", stdout=out)
    assert "demo.operator@nuronteq.test" in out.getvalue()

    assert Item.objects.filter(client__code__in=["KAL", "BAO"]).count() == 9
    assert InboundNotice.objects.filter(status="closed").count() == 4
    assert InboundNotice.objects.filter(status="accepted").count() == 2
    stored = InventoryBalance.objects.filter(location__kind=LocationKind.STORAGE, qty_on_hand__gt=0)
    assert stored.exists()
    assert not InventoryBalance.objects.filter(location__kind=LocationKind.DOCK, qty_on_hand__gt=0).exists()
    assert not ledger.drift().exists()
    assert User.objects.get(email="demo.client@nuronteq.test").role_grants.get().client_ids

    again = StringIO()
    call_command("seed_demo", "--not-production", stdout=again)
    assert "already loaded" in again.getvalue()
    assert InboundNotice.objects.count() == 6
