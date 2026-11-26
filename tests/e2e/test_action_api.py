"""Chat confirmation and dedicated action endpoints share durable receipts."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.main import create_app
from app.services.seed import seed_demo_data


@pytest.fixture
def client(tmp_path, monkeypatch, request):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("POLICY_ENABLED", "true" if getattr(request, "param", False) else "false")
    monkeypatch.setenv("POLICY_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    clear_settings_cache()
    with TestClient(create_app(), raise_server_exceptions=False) as client:

        async def seed():
            async with client.app.state.container.session_factory.begin() as db:
                await seed_demo_data(db)

        client.portal.call(seed)
        yield client
    clear_settings_cache()


def pending(client):
    response = client.post(
        "/api/v1/chat", json={"message": "取消订单 ORD10003", "user_id": "u_demo"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending_action"]
    return body["session_id"], body["pending_action"]["action_id"]


def test_chat_and_action_api_share_receipt(client):
    session, action = pending(client)
    body = {"user_id": "u_demo", "session_id": session}
    first = client.post(f"/api/v1/actions/{action}/confirm", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "SUCCEEDED"
    second = client.post(
        "/api/v1/chat",
        json={
            **body,
            "message": "确认",
            "confirm_action_id": action,
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["action_result"]["receipt"] == first.json()["receipt"]
    assert "取消" in second.json()["answer"]


def test_confirmed_sse_contains_authoritative_action_result(client):
    session, action = pending(client)
    response = client.post(
        "/api/v1/chat/stream",
        json={
            "user_id": "u_demo",
            "session_id": session,
            "message": "确认",
            "confirm_action_id": action,
        },
    )
    payloads = [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    final = next(p["data"] for p in payloads if p["event"] == "answer")
    assert final["action_result"]["status"] == "SUCCEEDED"
    assert "".join(p["text"] for p in payloads if p["event"] == "token") == final["answer"]


def test_other_user_cannot_read_action_or_history(client):
    session, action = pending(client)
    response = client.get(
        f"/api/v1/actions/{action}", params={"user_id": "other", "session_id": session}
    )
    assert response.status_code == 400
    response = client.post(
        "/api/v1/chat", json={"user_id": "other", "session_id": session, "message": "你好"}
    )
    assert response.status_code == 400


def test_successful_action_survives_chat_persistence_failure(client, monkeypatch):
    session, action = pending(client)

    async def fail(*args, **kwargs):
        raise RuntimeError("chat store unavailable")

    monkeypatch.setattr(client.app.state.container.chat_service, "_persist", fail)
    client.post(
        "/api/v1/chat",
        json={
            "user_id": "u_demo",
            "session_id": session,
            "message": "确认",
            "confirm_action_id": action,
        },
    )
    status = client.get(
        f"/api/v1/actions/{action}", params={"user_id": "u_demo", "session_id": session}
    )
    assert status.status_code == 200
    assert status.json()["status"] == "SUCCEEDED"


@pytest.mark.parametrize("client", [True], indirect=True)
def test_policy_enabled_chat_returns_rule_and_verified_source(client):
    response = client.post(
        "/api/v1/chat", json={"message": "订单 ORD10002 能退货吗", "user_id": "u_demo"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending_action"] is None
    assert "定制" in body["answer"]
    assert body["citations"]
    assert body["citations"][0]["source_digest"]


@pytest.mark.parametrize("client", [True], indirect=True)
def test_read_only_refund_eligibility_accepts_paid_unshipped_order(client):
    async def evaluate():
        from app.core.request_scope import request_session
        from app.tools.base import ToolContext

        service = client.app.state.container.chat_service
        async with request_session(client.app.state.container.session_factory):
            tool = service.registry.get("check_aftersale_eligibility")
            return await tool.run(
                {"order_id": "ORD10003", "tool_name": "create_refund_request"},
                ToolContext(user_id="u_demo", session_id="s", trace_id="t"),
            )

    result = client.portal.call(evaluate)
    assert result.output["verdict"] == "ALLOW"


@pytest.mark.parametrize("client", [True], indirect=True)
def test_read_only_eligibility_rejects_already_cancelled_order(client):
    from sqlalchemy import update

    from app.repositories.models import OrderRow

    async def cancel_for_fixture():
        async with client.app.state.container.session_factory.begin() as db:
            await db.execute(
                update(OrderRow).where(OrderRow.order_id == "ORD10003").values(status="cancelled")
            )

    client.portal.call(cancel_for_fixture)
    response = client.post(
        "/api/v1/chat", json={"message": "订单 ORD10003 能退货吗", "user_id": "u_demo"}
    )
    assert response.status_code == 200
    assert "不允许" in response.json()["answer"]


@pytest.mark.parametrize("client", [True], indirect=True)
def test_chat_can_propose_and_confirm_refund_with_policy_enabled(client):
    response = client.post(
        "/api/v1/chat", json={"message": "申请退款 ORD10003，原因是重复购买", "user_id": "u_demo"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending_action"] is not None
    assert body["pending_action"]["tool_name"] == "create_refund_request"
    action = body["pending_action"]["action_id"]
    done = client.post(
        f"/api/v1/actions/{action}/confirm",
        json={"user_id": "u_demo", "session_id": body["session_id"]},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "SUCCEEDED"
    assert done.json()["receipt"]["status"] == "requested"
