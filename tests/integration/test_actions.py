"""Confirmed writes use durable records and independent database connections."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.core.time import utcnow
from app.repositories.models import OrderRow, RefundRow, ReturnRow
from app.schemas.actions import ActionStatus
from app.schemas.chat import ActionProposal
from app.services.action_service import ActionService


def proposal(action_id="act_1", tool="create_refund_request", order="ORD10003"):
    args = {"order_id": order}
    if tool != "cancel_order":
        args["reason"] = "不想要了"
    return ActionProposal(
        action_id=action_id,
        tool_name=tool,
        arguments=args,
        risk=ToolRisk.WRITE,
        summary="确认后提交申请，不代表已到账。",
    )


async def propose(service, **kwargs):
    return await service.propose(
        proposal(**kwargs), user_id="u_demo", session_id="s1", trace_id="t1"
    )


async def confirm(service, action_id="act_1"):
    return await service.confirm(action_id, user_id="u_demo", session_id="s1", trace_id="t2")


@pytest.mark.asyncio
async def test_confirmation_survives_service_recreation(actions_db):
    await propose(ActionService(actions_db))
    result = await confirm(ActionService(actions_db))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.receipt["status"] == "requested"
    again = await confirm(ActionService(actions_db))
    assert again.receipt == result.receipt
    async with actions_db() as db:
        assert await db.scalar(select(func.count()).select_from(RefundRow)) == 1


@pytest.mark.asyncio
async def test_same_action_concurrent_confirmations_have_one_effect(actions_db):
    service = ActionService(actions_db)
    await propose(service)
    results = await asyncio.gather(*(confirm(ActionService(actions_db)) for _ in range(6)))
    assert all(r.status == ActionStatus.SUCCEEDED for r in results)
    assert len({r.receipt["refund_id"] for r in results}) == 1
    async with actions_db() as db:
        assert await db.scalar(select(func.count()).select_from(RefundRow)) == 1


@pytest.mark.asyncio
async def test_different_actions_cannot_create_duplicate_aftersales(actions_db):
    service = ActionService(actions_db)
    await propose(service, action_id="act_a")
    await propose(service, action_id="act_b")
    results = await asyncio.gather(confirm(service, "act_a"), confirm(service, "act_b"))
    assert sum(r.status == ActionStatus.SUCCEEDED for r in results) == 1
    async with actions_db() as db:
        assert await db.scalar(select(func.count()).select_from(RefundRow)) == 1


@pytest.mark.asyncio
async def test_changed_order_needs_new_confirmation(actions_db):
    service = ActionService(actions_db)
    await propose(service, tool="cancel_order")
    async with actions_db.begin() as db:
        await db.execute(
            update(OrderRow).where(OrderRow.order_id == "ORD10003").values(status="shipped")
        )
    result = await confirm(service)
    assert result.status == ActionStatus.REQUIRES_RECONFIRMATION
    async with actions_db() as db:
        assert (await db.get(OrderRow, "ORD10003")).status == "shipped"


@pytest.mark.asyncio
async def test_owner_and_session_are_checked_before_replay(actions_db):
    service = ActionService(actions_db)
    await propose(service)
    await confirm(service)
    for actor, session in [("other", "s1"), ("u_demo", "s2")]:
        with pytest.raises(InvalidRequestError):
            await service.confirm("act_1", user_id=actor, session_id=session, trace_id="bad")
        with pytest.raises(InvalidRequestError):
            await service.get("act_1", user_id=actor, session_id=session)


@pytest.mark.asyncio
async def test_expiry_rejects_pending_but_does_not_hide_success(actions_db):
    service = ActionService(actions_db, confirmation_ttl=timedelta(seconds=-1))
    await propose(service)
    assert (await confirm(service)).status == ActionStatus.EXPIRED
    good = ActionService(actions_db)
    await propose(good, action_id="good")
    done = await confirm(good, "good")
    from app.repositories.action_models import OperationRow

    async with actions_db.begin() as db:
        await db.execute(
            update(OperationRow)
            .where(OperationRow.action_id == "good")
            .values(expires_at=utcnow() - timedelta(days=1))
        )
    assert (await confirm(good, "good")).receipt == done.receipt


@pytest.mark.asyncio
async def test_existing_action_id_cannot_replace_arguments(actions_db):
    service = ActionService(actions_db)
    await propose(service)
    with pytest.raises(InvalidRequestError):
        await propose(service, tool="cancel_order")


@pytest.mark.asyncio
async def test_return_and_refund_are_mutually_exclusive(actions_db):
    service = ActionService(actions_db)
    await propose(service, action_id="ret", tool="create_return_request", order="ORD10001")
    assert (await confirm(service, "ret")).status == ActionStatus.SUCCEEDED
    await propose(service, action_id="ref", order="ORD10001")
    assert (await confirm(service, "ref")).status == ActionStatus.FAILED
    async with actions_db() as db:
        assert await db.scalar(select(func.count()).select_from(ReturnRow)) == 1
        assert await db.scalar(select(func.count()).select_from(RefundRow)) == 0


@pytest.mark.asyncio
async def test_failure_after_business_write_rolls_back_entire_effect(actions_db, monkeypatch):
    service = ActionService(actions_db)
    await propose(service)
    from app.repositories.order_repository import OrderRepository

    original = OrderRepository.create_refund

    async def broken(self, **kwargs):
        await original(self, **kwargs)
        raise RuntimeError("injected after business flush")

    monkeypatch.setattr(OrderRepository, "create_refund", broken)
    result = await confirm(service)
    assert result.status == ActionStatus.FAILED
    async with actions_db() as db:
        assert await db.scalar(select(func.count()).select_from(RefundRow)) == 0
        assert (await db.get(OrderRow, "ORD10003")).status == "paid"


@pytest.mark.asyncio
async def test_changed_policy_requires_new_confirmation(actions_db):
    from app.policies.engine import PolicyEngine

    policy = PolicyEngine.demo()
    service = ActionService(actions_db, policy=policy)
    await propose(service)
    changed = [
        r.model_copy(update={"return_days": 30}) if r.revision == "v2" else r for r in policy.rules
    ]
    service.policy = PolicyEngine(changed)
    result = await confirm(service)
    assert result.status == ActionStatus.REQUIRES_RECONFIRMATION
    assert result.error_code == "POLICY_CHANGED"
    async with actions_db() as db:
        assert await db.scalar(select(func.count()).select_from(RefundRow)) == 0


@pytest.mark.asyncio
async def test_unrelated_policy_publication_does_not_invalidate_confirmation(actions_db):
    from app.policies.engine import PolicyEngine

    policy = PolicyEngine.demo()
    service = ActionService(actions_db, policy=policy)
    await propose(service)
    unrelated = policy.rules[0].model_copy(update={"policy_id": "store-only", "channel": "store"})
    service.policy = PolicyEngine([*policy.rules, unrelated])
    assert (await confirm(service)).status == ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_policy_no_longer_applicable_requires_reconfirmation(actions_db):
    from app.policies.engine import PolicyEngine

    policy = PolicyEngine.demo()
    service = ActionService(actions_db, policy=policy)
    await propose(service)
    service.policy = PolicyEngine([r.model_copy(update={"channel": "store"}) for r in policy.rules])
    result = await confirm(service)
    assert result.status == ActionStatus.REQUIRES_RECONFIRMATION
    assert result.error_code == "POLICY_CHANGED"
