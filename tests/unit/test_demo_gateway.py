"""The independent ledger releases file handles after each transaction."""

import pytest

from app.providers.business_gateway import DemoBusinessGateway, GatewayRequest


@pytest.mark.asyncio
async def test_gateway_releases_windows_file_handles(tmp_path):
    path = tmp_path / "remote.db"
    gateway = DemoBusinessGateway(path)
    await gateway.submit(
        GatewayRequest(
            key="a", tool_name="cancel_order", order_id="ORD1", arguments={}, expected_snapshot="v1"
        )
    )
    await gateway.lookup("a")
    assert await gateway.effect_count() == 1
    path.unlink()
    assert not path.exists()
