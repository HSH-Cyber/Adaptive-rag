"""Durable confirmed writes with short transactions and receipt reconciliation."""

import logging
import time
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.core.time import utcnow
from app.observability.metrics import ACTION_CONFIRMATIONS, ACTION_LATENCY, ACTION_RECOVERIES
from app.policies.engine import PolicyEngine
from app.providers.business_gateway import BusinessGateway, GatewayReceipt, GatewayRequest
from app.repositories.models import (
    OperationEventRow,
    OperationRow,
    OrderReservationRow,
    OrderRow,
    RefundRow,
    ReturnRow,
)
from app.repositories.order_repository import OrderRepository
from app.repositories.session_repository import SessionRepository
from app.schemas.actions import ActionStatus, ActionView
from app.schemas.chat import ActionProposal
from app.schemas.order import Order
from app.services.action_rules import (
    WRITE_TOOLS,
    assert_business_permitted,
    aware,
    canonical,
    digest,
    order_snapshot,
)


class _WriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    reason: str | None = Field(default=None, min_length=1, max_length=200)


class ActionService:
    """Own business transactions, never the LLM/chat persistence transaction.

    Args:
        factory: Shared durable database, with independent sessions per call.
        gateway: None executes local writes atomically; a gateway uses recovery.
        confirmation_ttl: Pending confirmation validity, default fifteen minutes.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        gateway: BusinessGateway | None = None,
        confirmation_ttl: timedelta = timedelta(minutes=15),
        lease: timedelta = timedelta(seconds=30),
        policy: PolicyEngine | None = None,
    ) -> None:
        self.factory = factory
        self.gateway = gateway
        self.confirmation_ttl = confirmation_ttl
        self.lease = lease
        self.policy = policy

    @property
    def provider(self) -> str:
        return self.gateway.name if self.gateway else "local"

    async def propose(
        self,
        proposal: ActionProposal,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
    ) -> ActionProposal:
        """Freeze canonical arguments and facts before returning a confirmation."""
        if proposal.tool_name not in WRITE_TOOLS:
            raise InvalidRequestError("不支持的业务写操作。")
        try:
            args = _WriteArguments.model_validate(proposal.arguments).model_dump(exclude_none=True)
        except ValueError as exc:
            raise InvalidRequestError("业务操作参数无效。") from exc
        if proposal.tool_name != "cancel_order" and not args.get("reason"):
            raise InvalidRequestError("请提供售后申请原因。")
        fp = digest([user_id, session_id, proposal.tool_name, args])
        async with self.factory.begin() as db:
            await SessionRepository(db).get_or_create(session_id, user_id)
            existing = await db.get(OperationRow, proposal.action_id)
            if existing:
                if existing.payload_hash != fp:
                    raise InvalidRequestError("操作编号已绑定其他参数。")
                return self._proposal(proposal, existing)
            order = await OrderRepository(db).get_visible_order(args["order_id"], user_id)
            evidence = await self._policy_check(db, order, proposal.tool_name)
            now = utcnow()
            row = OperationRow(
                action_id=proposal.action_id,
                user_id=user_id,
                session_id=session_id,
                tool_name=proposal.tool_name,
                order_id=order.order_id,
                arguments_json=canonical(args),
                payload_hash=fp,
                order_snapshot_hash=order_snapshot(order),
                order_facts_json=canonical({"total_cents": order.total_cents}),
                status=ActionStatus.PENDING_CONFIRMATION.value,
                expires_at=now + self.confirmation_ttl,
                provider=self.provider,
                policy_evidence_json=canonical(evidence),
                attempt=0,
            )
            db.add(row)
            await db.flush()
            self._event(db, row, trace_id)
            return self._proposal(proposal, row)

    async def get(self, action_id: str, *, user_id: str, session_id: str) -> ActionView:
        """Read only the actor's own operation, including successful replay."""
        async with self.factory() as db:
            return self._view(await self._owned(db, action_id, user_id, session_id))

    async def confirm(
        self,
        action_id: str,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
    ) -> ActionView:
        """Return a committed result and record bounded-cardinality metrics."""
        started = time.perf_counter()
        try:
            view = await self._confirm(
                action_id, user_id=user_id, session_id=session_id, trace_id=trace_id
            )
            ACTION_CONFIRMATIONS.labels(provider=self.provider, status=view.status.value).inc()
            return view
        finally:
            ACTION_LATENCY.labels(provider=self.provider).observe(time.perf_counter() - started)

    async def _confirm(
        self,
        action_id: str,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
    ) -> ActionView:
        """Atomically execute locally, or durably claim before a remote call."""
        request: GatewayRequest | None = None
        try:
            async with self.factory.begin() as db:
                # UPDATE acquires a database lock before any reads. This also
                # prevents SQLite's deferred read->write upgrade race.
                await db.execute(
                    update(OperationRow)
                    .where(OperationRow.action_id == action_id)
                    .values(updated_at=OperationRow.updated_at)
                )
                row = await self._owned(db, action_id, user_id, session_id)
                if row.status != ActionStatus.PENDING_CONFIRMATION.value:
                    return self._view(row)
                if aware(row.expires_at) <= utcnow():
                    self._transition(
                        db, row, ActionStatus.EXPIRED, trace_id, "CONFIRMATION_EXPIRED"
                    )
                    return self._view(row)
                if row.provider != self.provider:
                    raise InvalidRequestError("业务网关配置与原操作不一致。")
                await db.execute(
                    update(OrderRow)
                    .where(OrderRow.order_id == row.order_id)
                    .values(status=OrderRow.status)
                )
                repo = OrderRepository(db)
                order = await repo.get_visible_order(row.order_id, user_id)
                reservation = await db.get(OrderReservationRow, row.order_id)
                if reservation and reservation.action_id != row.action_id:
                    self._transition(db, row, ActionStatus.FAILED, trace_id, "ORDER_BUSY")
                    return self._view(row)
                if order_snapshot(order) != row.order_snapshot_hash:
                    self._transition(
                        db, row, ActionStatus.REQUIRES_RECONFIRMATION, trace_id, "ORDER_CHANGED"
                    )
                    return self._view(row)
                try:
                    await assert_business_permitted(db, row.tool_name, order)
                    evidence = await self._policy_check(
                        db, order, row.tool_name, require_allow=False
                    )
                except InvalidRequestError:
                    self._transition(
                        db, row, ActionStatus.FAILED, trace_id, "BUSINESS_PRECONDITION_FAILED"
                    )
                    return self._view(row)
                import json

                if self._evidence_fingerprint(evidence) != self._evidence_fingerprint(
                    json.loads(row.policy_evidence_json)
                ):
                    self._transition(
                        db, row, ActionStatus.REQUIRES_RECONFIRMATION, trace_id, "POLICY_CHANGED"
                    )
                    return self._view(row)
                row.attempt += 1
                self._transition(db, row, ActionStatus.READY, trace_id)
                self._transition(db, row, ActionStatus.EXECUTING, trace_id)
                if self.gateway:
                    db.add(OrderReservationRow(order_id=row.order_id, action_id=row.action_id))
                    row.lease_until = utcnow() + self.lease
                    request = self._request(row)
                    attempt = row.attempt
                else:
                    local_receipt = await self._local_write(repo, row, trace_id)
                    row.receipt_json = canonical(local_receipt)
                    self._transition(db, row, ActionStatus.SUCCEEDED, trace_id)
                view = self._view(row)
        except InvalidRequestError:
            raise
        except Exception:
            logging.getLogger(__name__).exception(
                "action_transaction_failed", extra={"action_id": action_id, "trace_id": trace_id}
            )
            # A failed local transaction rolled back business + state together.
            # Record failure in a new transaction, without overwriting a winner.
            async with self.factory.begin() as db:
                await db.execute(
                    update(OperationRow)
                    .where(OperationRow.action_id == action_id)
                    .values(updated_at=OperationRow.updated_at)
                )
                row = await self._owned(db, action_id, user_id, session_id)
                if row.status == ActionStatus.PENDING_CONFIRMATION.value:
                    self._transition(
                        db, row, ActionStatus.FAILED, trace_id, "LOCAL_TRANSACTION_FAILED"
                    )
                view = self._view(row)
            return view
        if request is not None:
            assert self.gateway is not None
            try:
                receipt = await self.gateway.submit(request)
            except Exception:
                receipt = GatewayReceipt(outcome="UNKNOWN", error="TRANSPORT_OUTCOME_UNKNOWN")
            return await self.record_remote_result(
                action_id, attempt=attempt, receipt=receipt, trace_id=trace_id
            )
        return view

    async def record_remote_result(
        self,
        action_id: str,
        *,
        attempt: int,
        receipt: GatewayReceipt,
        trace_id: str,
    ) -> ActionView:
        """Fence stale workers; atomically project an authoritative remote result."""
        async with self.factory.begin() as db:
            claimed = await db.execute(
                update(OperationRow)
                .where(
                    OperationRow.action_id == action_id,
                    OperationRow.attempt == attempt,
                    OperationRow.status.in_(
                        [ActionStatus.EXECUTING.value, ActionStatus.UNKNOWN.value]
                    ),
                )
                .values(updated_at=utcnow())
                .returning(OperationRow.action_id)
            )
            row = await db.get(OperationRow, action_id)
            if row is None:
                raise InvalidRequestError("操作不存在。")
            if claimed.scalar_one_or_none() is None:
                return self._view(row)
            if receipt.outcome == "SUCCEEDED" and self._valid_receipt(row, receipt.receipt):
                await db.execute(
                    update(OrderRow)
                    .where(OrderRow.order_id == row.order_id)
                    .values(status=OrderRow.status)
                )
                await self._project_remote(db, row, receipt.receipt, trace_id)
                row.receipt_json = canonical(receipt.receipt)
                self._transition(db, row, ActionStatus.SUCCEEDED, trace_id)
                await db.execute(
                    delete(OrderReservationRow).where(OrderReservationRow.action_id == action_id)
                )
            elif receipt.outcome == "FAILED":
                self._transition(
                    db, row, ActionStatus.FAILED, trace_id, receipt.error or "REMOTE_REJECTED"
                )
                await db.execute(
                    delete(OrderReservationRow).where(OrderReservationRow.action_id == action_id)
                )
            else:
                self._transition(
                    db,
                    row,
                    ActionStatus.UNKNOWN,
                    trace_id,
                    receipt.error or "REMOTE_OUTCOME_UNKNOWN",
                )
            row.lease_until = utcnow() + self.lease if row.status == "UNKNOWN" else None
            return self._view(row)

    async def recover(self, *, limit: int = 20) -> list[ActionView]:
        """Reconcile bounded remote work; a lease expiry never means failure."""
        if self.gateway is None:
            return []
        limit = max(1, min(limit, 100))
        async with self.factory() as db:
            ids = list(
                (
                    await db.scalars(
                        select(OperationRow.action_id)
                        .where(
                            OperationRow.provider == self.provider,
                            or_(
                                OperationRow.status == "UNKNOWN",
                                (OperationRow.status == "EXECUTING")
                                & (OperationRow.lease_until <= utcnow()),
                            ),
                        )
                        .order_by(OperationRow.updated_at)
                        .limit(limit)
                    )
                ).all()
            )
        views = []
        for action_id in ids:
            async with self.factory.begin() as db:
                claimed = await db.execute(
                    update(OperationRow)
                    .where(
                        OperationRow.action_id == action_id,
                        OperationRow.status.in_(["UNKNOWN", "EXECUTING"]),
                        or_(OperationRow.lease_until <= utcnow(), OperationRow.status == "UNKNOWN"),
                    )
                    .values(
                        attempt=OperationRow.attempt + 1,
                        status="EXECUTING",
                        lease_until=utcnow() + self.lease,
                    )
                    .returning(OperationRow.action_id)
                )
                if claimed.scalar_one_or_none() is None:
                    continue
                row = await db.get(OperationRow, action_id)
                assert row is not None
                request, attempt = self._request(row), row.attempt
                self._event(db, row, "recovery")
            try:
                receipt = await self.gateway.lookup(request.key)
                if (
                    receipt.outcome == "NOT_ACCEPTED"
                    and self.gateway.authoritative_lookup
                    and self.gateway.idempotent_submit
                ):
                    blocked = await self._revalidate_recovery(action_id, attempt)
                    if blocked is not None:
                        views.append(blocked)
                        ACTION_RECOVERIES.labels(status=blocked.status.value).inc()
                        continue
                    receipt = await self.gateway.submit(request)
            except Exception:
                receipt = GatewayReceipt(outcome="UNKNOWN", error="LOOKUP_UNAVAILABLE")
            result = await self.record_remote_result(
                action_id, attempt=attempt, receipt=receipt, trace_id="recovery"
            )
            ACTION_RECOVERIES.labels(status=result.status.value).inc()
            views.append(result)
        return views

    async def _revalidate_recovery(self, action_id: str, attempt: int) -> ActionView | None:
        """Never resend a previously confirmed request against changed facts."""
        import json

        async with self.factory.begin() as db:
            await db.execute(
                update(OperationRow)
                .where(OperationRow.action_id == action_id)
                .values(updated_at=OperationRow.updated_at)
            )
            row = await db.get(OperationRow, action_id)
            assert row is not None
            if row.attempt != attempt or row.status != "EXECUTING":
                return self._view(row)
            await db.execute(
                update(OrderRow)
                .where(OrderRow.order_id == row.order_id)
                .values(status=OrderRow.status)
            )
            try:
                order = await OrderRepository(db).get_visible_order(row.order_id, row.user_id)
                await assert_business_permitted(db, row.tool_name, order)
                evidence = await self._policy_check(db, order, row.tool_name, require_allow=False)
                unchanged = order_snapshot(
                    order
                ) == row.order_snapshot_hash and self._evidence_fingerprint(
                    evidence
                ) == self._evidence_fingerprint(json.loads(row.policy_evidence_json))
            except InvalidRequestError:
                unchanged = False
            if unchanged:
                return None
            # A delayed original request could still exist: retain reservation.
            self._transition(
                db, row, ActionStatus.UNKNOWN, "recovery", "RECOVERY_REVALIDATION_REQUIRED"
            )
            row.lease_until = utcnow() + self.lease
            return self._view(row)

    async def _owned(
        self, db: AsyncSession, action_id: str, user_id: str, session_id: str
    ) -> OperationRow:
        row = await db.get(OperationRow, action_id)
        if row is None or row.user_id != user_id or row.session_id != session_id:
            raise InvalidRequestError("操作不存在或不属于当前会话。")
        return row

    async def _local_write(self, repo: OrderRepository, row: OperationRow, trace_id: str) -> dict:
        import json

        args = json.loads(row.arguments_json)
        if row.tool_name == "cancel_order":
            return await repo.cancel_order(row.order_id, trace_id=trace_id)
        if row.tool_name == "create_refund_request":
            return await repo.create_refund(
                order_id=row.order_id, reason=args["reason"], trace_id=trace_id
            )
        return await repo.create_return(
            order_id=row.order_id, reason=args["reason"], trace_id=trace_id
        )

    async def _project_remote(
        self, db: AsyncSession, row: OperationRow, receipt: dict, trace_id: str
    ) -> None:
        import json

        reason = json.loads(row.arguments_json).get("reason", "")
        if row.tool_name == "cancel_order":
            await db.execute(
                update(OrderRow).where(OrderRow.order_id == row.order_id).values(status="cancelled")
            )
        elif row.tool_name == "create_refund_request":
            order = await db.get(OrderRow, row.order_id)
            assert order is not None
            db.add(
                RefundRow(
                    refund_id=receipt["refund_id"],
                    order_id=row.order_id,
                    status="requested",
                    amount_cents=json.loads(row.order_facts_json)["total_cents"],
                    reason=reason,
                )
            )
            order.status = "refunding"
        else:
            db.add(
                ReturnRow(
                    return_id=receipt["return_id"],
                    order_id=row.order_id,
                    status="requested",
                    reason=reason,
                )
            )
        await OrderRepository(db).write_audit(trace_id, row.tool_name, row.order_id)

    async def _policy_check(
        self, db: AsyncSession, order: Order, tool: str, *, require_allow: bool = True
    ) -> dict:
        if self.policy is None or tool == "cancel_order":
            return {}
        categories = await OrderRepository(db).categories_for_order(order)
        decision = self.policy.evaluate(order, now=utcnow(), tool_name=tool, categories=categories)
        if require_allow and decision.verdict != "ALLOW":
            raise InvalidRequestError(decision.reason)
        return decision.model_dump(mode="json")

    @staticmethod
    def _evidence_fingerprint(evidence: dict) -> str:
        # Publishing an unrelated policy does not invalidate an existing order.
        return digest({k: v for k, v in evidence.items() if k != "snapshot_id"})

    @staticmethod
    def _request(row: OperationRow) -> GatewayRequest:
        import json

        return GatewayRequest(
            key=row.action_id,
            tool_name=row.tool_name,
            order_id=row.order_id,
            arguments=json.loads(row.arguments_json),
            expected_snapshot=row.order_snapshot_hash,
        )

    @staticmethod
    def _valid_receipt(row: OperationRow, receipt: dict) -> bool:
        if receipt.get("order_id") != row.order_id:
            return False
        if row.tool_name == "cancel_order":
            return receipt.get("status") == "cancelled"
        key = "refund_id" if row.tool_name == "create_refund_request" else "return_id"
        return (
            isinstance(receipt.get(key), str)
            and bool(receipt[key])
            and receipt.get("status") == "requested"
        )

    @staticmethod
    def _event(db: AsyncSession, row: OperationRow, trace_id: str) -> None:
        db.add(
            OperationEventRow(
                action_id=row.action_id,
                status=row.status,
                reason=row.error_code or "",
                trace_id=trace_id,
                attempt=row.attempt,
            )
        )

    def _transition(
        self,
        db: AsyncSession,
        row: OperationRow,
        state: ActionStatus,
        trace_id: str,
        error: str | None = None,
    ) -> None:
        row.status, row.error_code, row.updated_at = state.value, error, utcnow()
        self._event(db, row, trace_id)

    @staticmethod
    def _view(row: OperationRow) -> ActionView:
        import json

        state = ActionStatus(row.status)
        messages = {
            ActionStatus.PENDING_CONFIRMATION: "请核对操作并确认。",
            ActionStatus.SUCCEEDED: "订单已取消。"
            if row.tool_name == "cancel_order"
            else "售后申请已受理，不代表退款已到账。",
            ActionStatus.UNKNOWN: "操作结果待核实，请勿重复提交。",
            ActionStatus.EXECUTING: "操作处理中，请查询原操作结果。",
            ActionStatus.FAILED: "操作未完成，请核对订单状态或已有售后申请。",
            ActionStatus.EXPIRED: "确认已过期，请重新发起操作。",
            ActionStatus.REQUIRES_RECONFIRMATION: "订单或政策依据已变化，请重新发起并确认。",
        }
        return ActionView(
            action_id=row.action_id,
            tool_name=row.tool_name,
            order_id=row.order_id,
            status=state,
            expires_at=aware(row.expires_at),
            receipt=json.loads(row.receipt_json or "{}"),
            error_code=row.error_code,
            message=messages.get(state, "操作等待执行。"),
            policy_evidence=json.loads(row.policy_evidence_json or "{}"),
        )

    @staticmethod
    def _proposal(proposal: ActionProposal, row: OperationRow) -> ActionProposal:
        import json

        return proposal.model_copy(
            update={
                "arguments": json.loads(row.arguments_json),
                "risk": ToolRisk.DESTRUCTIVE
                if row.tool_name != "create_return_request"
                else ToolRisk.WRITE,
                "expires_at": aware(row.expires_at),
                "policy_evidence": json.loads(row.policy_evidence_json),
            }
        )
