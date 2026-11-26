"""Deterministic business prerequisites shared by all confirmed write paths."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidRequestError
from app.repositories.models import RefundRow, ReturnRow
from app.schemas.order import Order

WRITE_TOOLS = {"cancel_order", "create_refund_request", "create_return_request"}
_ALLOWED = {
    "cancel_order": {"created", "paid"},
    "create_refund_request": {"paid", "shipped", "delivered"},
    "create_return_request": {"shipped", "delivered"},
}


def canonical(value: Any) -> str:
    """Canonical JSON for immutable arguments and snapshots."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def aware(value: datetime) -> datetime:
    """SQLite DateTime may return naive UTC; normalize explicitly."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def order_snapshot(order: Order) -> str:
    values = order.model_dump(mode="json")
    for key in ("created_at", "paid_at", "shipped_at", "delivered_at"):
        instant = getattr(order, key)
        values[key] = aware(instant).isoformat() if instant else None
    values["items"] = sorted(values["items"], key=canonical)
    return digest(values)


async def assert_business_permitted(db: AsyncSession, tool: str, order: Order) -> None:
    """Evaluate after obtaining the order write lock, never from model claims."""
    if order.status.value not in _ALLOWED.get(tool, set()):
        raise InvalidRequestError("订单当前状态不允许该操作。")
    for model in (RefundRow, ReturnRow):
        blocked = await db.scalar(
            select(model)
            .where(
                model.order_id == order.order_id,
                model.status.notin_(["rejected", "cancelled", "failed"]),
            )
            .limit(1)
        )
        if blocked is not None:
            raise InvalidRequestError("订单已有售后申请，不能重复提交。")
