"""Explicit remote receipt contract and independently committed demo gateway."""

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.services.action_rules import canonical, digest


class GatewayRequest(BaseModel):
    key: str
    tool_name: str
    order_id: str
    arguments: dict[str, Any]
    expected_snapshot: str


class GatewayReceipt(BaseModel):
    outcome: Literal["SUCCEEDED", "PENDING", "NOT_ACCEPTED", "UNKNOWN", "FAILED"]
    receipt: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class BusinessGateway(Protocol):
    name: str
    idempotent_submit: bool
    authoritative_lookup: bool

    async def submit(self, request: GatewayRequest) -> GatewayReceipt: ...
    async def lookup(self, key: str) -> GatewayReceipt: ...


class DemoBusinessGateway:
    """Independent SQLite ledger, not a payment provider or cross-system lock.

    The optional remote snapshot can be set by a demo warehouse event. When no
    remote order exists, the first request establishes its initial snapshot.
    """

    name = "demo"
    idempotent_submit = True
    authoritative_lookup = True

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS effects (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS remote_orders (order_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15)

    async def submit(self, request: GatewayRequest) -> GatewayReceipt:
        return await asyncio.to_thread(self._submit, request)

    def _submit(self, request: GatewayRequest) -> GatewayReceipt:
        fingerprint = digest(request.model_dump())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT fingerprint, receipt FROM effects WHERE key=?", (request.key,)
            ).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    return GatewayReceipt(outcome="FAILED", error="IDEMPOTENCY_CONFLICT")
                return GatewayReceipt(outcome="SUCCEEDED", receipt=json.loads(existing[1]))
            db.execute(
                "INSERT OR IGNORE INTO remote_orders VALUES (?, ?)",
                (request.order_id, request.expected_snapshot),
            )
            actual = db.execute(
                "SELECT snapshot FROM remote_orders WHERE order_id=?", (request.order_id,)
            ).fetchone()
            if actual[0] != request.expected_snapshot:
                return GatewayReceipt(outcome="FAILED", error="REMOTE_ORDER_CHANGED")
            receipt: dict[str, Any] = {"order_id": request.order_id, "status": "requested"}
            if request.tool_name == "cancel_order":
                receipt["status"] = "cancelled"
            else:
                kind = "refund_id" if request.tool_name == "create_refund_request" else "return_id"
                receipt[kind] = "demo_" + digest(request.key)[:16]
            db.execute(
                "INSERT INTO effects VALUES (?, ?, ?)",
                (request.key, fingerprint, canonical(receipt)),
            )
            return GatewayReceipt(outcome="SUCCEEDED", receipt=receipt)

    async def lookup(self, key: str) -> GatewayReceipt:
        def read() -> GatewayReceipt:
            with closing(self._connect()) as db, db:
                row = db.execute("SELECT receipt FROM effects WHERE key=?", (key,)).fetchone()
            return (
                GatewayReceipt(outcome="SUCCEEDED", receipt=json.loads(row[0]))
                if row
                else GatewayReceipt(outcome="NOT_ACCEPTED")
            )

        return await asyncio.to_thread(read)

    async def set_order_snapshot(self, order_id: str, snapshot: str) -> None:
        """Simulate an independent warehouse changing the remote order."""

        def write() -> None:
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO remote_orders VALUES (?, ?)", (order_id, snapshot)
                )

        await asyncio.to_thread(write)

    async def effect_count(self) -> int:
        def count() -> int:
            with closing(self._connect()) as db, db:
                return int(db.execute("SELECT COUNT(*) FROM effects").fetchone()[0])

        return await asyncio.to_thread(count)
