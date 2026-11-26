"""Temporal rules use explicit business facts and immutable source evidence."""

from datetime import UTC, datetime, timedelta

import pytest

from app.policies.engine import PolicyEngine
from app.schemas.order import Order, OrderItem


def order(**changes):
    values = dict(
        order_id="ORDP1",
        user_id="u",
        status="delivered",
        total_cents=100,
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
        paid_at=datetime(2026, 10, 20, tzinfo=UTC),
        delivered_at=datetime(2026, 11, 1, tzinfo=UTC),
        items=[OrderItem(sku="SKU-TEE", name="T恤", quantity=1, unit_price_cents=100)],
    )
    values.update(changes)
    return Order(**values)


def test_old_purchase_does_not_inherit_new_policy():
    engine = PolicyEngine.demo()
    result = engine.evaluate(order(), now=datetime(2026, 11, 10, tzinfo=UTC))
    assert result.verdict == "DENY"
    assert result.evidence[0].revision == "v1"
    newer = order(paid_at=datetime(2026, 11, 1, tzinfo=UTC))
    result = engine.evaluate(newer, now=datetime(2026, 11, 10, tzinfo=UTC))
    assert result.verdict == "ALLOW"
    assert result.evidence[0].revision == "v2"


def test_deadline_is_explicit_and_boundary_is_exclusive():
    engine = PolicyEngine.demo()
    delivered = datetime(2026, 11, 1, tzinfo=UTC)
    assert (
        engine.evaluate(
            order(), now=delivered + timedelta(days=7) - timedelta(microseconds=1)
        ).verdict
        == "ALLOW"
    )
    assert engine.evaluate(order(), now=delivered + timedelta(days=7)).verdict == "DENY"


def test_missing_delivery_time_cannot_authorize_return():
    result = PolicyEngine.demo().evaluate(
        order(delivered_at=None), now=datetime(2026, 11, 2, tzinfo=UTC)
    )
    assert result.verdict == "NEEDS_INFORMATION"


def test_customized_item_blocks_whole_order():
    original = order()
    original.items.append(
        OrderItem(sku="CUSTOM", name="定制杯", quantity=1, unit_price_cents=50, customized=True)
    )
    result = PolicyEngine.demo().evaluate(original, now=datetime(2026, 11, 2, tzinfo=UTC))
    assert result.verdict == "DENY"
    assert "定制" in result.reason


def test_same_priority_contradictory_policies_report_conflict():
    engine = PolicyEngine.demo()
    duplicate = engine.rules[0].model_copy(update={"policy_id": "other", "return_days": 3})
    result = PolicyEngine([*engine.rules, duplicate]).evaluate(
        order(), now=datetime(2026, 11, 2, tzinfo=UTC)
    )
    assert result.verdict == "CONFLICT"


def test_tampered_source_cannot_be_loaded(tmp_path):
    engine = PolicyEngine.demo()
    manifest = engine.export(tmp_path)
    source = tmp_path / engine.rules[0].source_path
    source.write_text("篡改政策", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        PolicyEngine.load(manifest)
