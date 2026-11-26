"""A committed remote effect survives response loss and local service recreation."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from app.providers.business_gateway import DemoBusinessGateway, GatewayReceipt
from app.repositories.action_models import OperationRow
from app.schemas.actions import ActionStatus
from app.services.action_service import ActionService
from tests.integration.test_actions import confirm, propose


class LoseResponseGateway(DemoBusinessGateway):
    """Commit a real independent effect before simulating transport failure."""

    async def submit(self, request):
        await super().submit(request)
        raise TimeoutError("response lost after external commit")


class UnreachableLookupGateway(DemoBusinessGateway):
    async def lookup(self, key):
        raise TimeoutError("cannot determine remote outcome")


@pytest.mark.asyncio
async def test_lost_response_recovers_original_receipt(actions_db, tmp_path):
    gateway_path = tmp_path / "external.db"
    service = ActionService(actions_db, gateway=LoseResponseGateway(gateway_path))
    await propose(service)
    unknown = await confirm(service)
    assert unknown.status == ActionStatus.UNKNOWN
    gateway = DemoBusinessGateway(gateway_path)
    restarted = ActionService(actions_db, gateway=gateway)
    recovered = await restarted.recover(limit=10)
    assert recovered[0].status == ActionStatus.SUCCEEDED
    assert recovered[0].receipt["status"] == "requested"
    assert await gateway.effect_count() == 1
    assert (await confirm(restarted)).receipt == recovered[0].receipt


@pytest.mark.asyncio
async def test_unknown_lookup_keeps_order_reserved(actions_db, tmp_path):
    path = tmp_path / "external.db"
    service = ActionService(actions_db, gateway=LoseResponseGateway(path))
    await propose(service)
    await confirm(service)
    restarted = ActionService(actions_db, gateway=UnreachableLookupGateway(path))
    await restarted.recover(limit=10)
    assert (
        await restarted.get("act_1", user_id="u_demo", session_id="s1")
    ).status == ActionStatus.UNKNOWN
    await propose(restarted, action_id="other")
    assert (await confirm(restarted, "other")).status == ActionStatus.FAILED
    assert await DemoBusinessGateway(path).effect_count() == 1


@pytest.mark.asyncio
async def test_expired_worker_cannot_overwrite_newer_recovery(actions_db, tmp_path):
    service = ActionService(actions_db, gateway=LoseResponseGateway(tmp_path / "external.db"))
    await propose(service)
    await confirm(service)
    await service.recover(limit=10)
    stale = await service.record_remote_result(
        "act_1",
        attempt=1,
        receipt=GatewayReceipt(outcome="FAILED", error="stale"),
        trace_id="stale",
    )
    assert stale.status == ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_restart_after_claim_queries_before_resending(actions_db, tmp_path):
    gateway = DemoBusinessGateway(tmp_path / "external.db")
    service = ActionService(actions_db, gateway=gateway)
    await propose(service)
    from app.core.time import utcnow
    from app.repositories.action_models import OrderReservationRow

    async with actions_db.begin() as db:
        await db.execute(
            update(OperationRow)
            .where(OperationRow.action_id == "act_1")
            .values(status="EXECUTING", attempt=1, lease_until=utcnow() - timedelta(seconds=1))
        )
        db.add(OrderReservationRow(order_id="ORD10003", action_id="act_1"))
    recovered = await service.recover(limit=10)
    assert recovered[0].status == ActionStatus.SUCCEEDED
    assert await gateway.effect_count() == 1


@pytest.mark.asyncio
async def test_recovered_projection_uses_confirmed_amount(actions_db, tmp_path):
    from sqlalchemy import select

    from app.repositories.models import OrderRow, RefundRow

    service = ActionService(actions_db, gateway=LoseResponseGateway(tmp_path / "external.db"))
    await propose(service)
    await confirm(service)
    async with actions_db.begin() as db:
        await db.execute(
            update(OrderRow).where(OrderRow.order_id == "ORD10003").values(total_cents=12345)
        )
    await service.recover(limit=10)
    async with actions_db() as db:
        refund = await db.scalar(select(RefundRow))
        assert refund.amount_cents == 7900


@pytest.mark.asyncio
async def test_recovery_does_not_resubmit_after_local_preconditions_change(actions_db, tmp_path):
    from app.core.time import utcnow
    from app.repositories.models import OrderReservationRow, OrderRow

    gateway = DemoBusinessGateway(tmp_path / "external.db")
    service = ActionService(actions_db, gateway=gateway)
    await propose(service, tool="cancel_order")
    async with actions_db.begin() as db:
        await db.execute(
            update(OperationRow)
            .where(OperationRow.action_id == "act_1")
            .values(status="EXECUTING", attempt=1, lease_until=utcnow() - timedelta(seconds=1))
        )
        await db.execute(
            update(OrderRow).where(OrderRow.order_id == "ORD10003").values(status="shipped")
        )
        db.add(OrderReservationRow(order_id="ORD10003", action_id="act_1"))
    result = (await service.recover(limit=10))[0]
    assert result.status == ActionStatus.UNKNOWN
    assert result.error_code == "RECOVERY_REVALIDATION_REQUIRED"
    assert await gateway.effect_count() == 0
