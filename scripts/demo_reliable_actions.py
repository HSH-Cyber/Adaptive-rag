"""Run local, concurrent, lost-response and temporal-policy demos without an LLM."""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import OrderStatus, ToolRisk
from app.policies.engine import PolicyEngine
from app.providers.business_gateway import DemoBusinessGateway
from app.repositories.db import init_schema
from app.repositories.models import OrderRow, RefundRow
from app.repositories.order_repository import OrderRepository
from app.schemas.chat import ActionProposal
from app.services.action_service import ActionService
from app.services.seed import seed_demo_data


class ResponseLossDemoGateway(DemoBusinessGateway):
    """Fault injection: the external ledger commits, then the response is lost."""

    async def submit(self, request):
        await super().submit(request)
        raise TimeoutError("demo: response lost after commit")


@asynccontextmanager
async def database(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        await seed_demo_data(db)
    try:
        yield factory
    finally:
        await engine.dispose()


def proposal(action_id: str, tool: str = "create_refund_request") -> ActionProposal:
    args = {"order_id": "ORD10003"}
    if tool != "cancel_order":
        args["reason"] = "演示申请"
    return ActionProposal(
        action_id=action_id,
        tool_name=tool,
        arguments=args,
        risk=ToolRisk.DESTRUCTIVE,
        summary="请确认操作。",
    )


async def run_demo(root: Path) -> dict:
    """Use fresh file databases and return observable scenario outcomes."""
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("演示目录必须为空，避免覆盖已有数据库。")
    identity = {"user_id": "u_demo", "session_id": "demo", "trace_id": "demo"}
    report = {}
    async with database(root / "local.db") as factory:
        service = ActionService(factory)
        await service.propose(proposal("demo_duplicate"), **identity)
        results = await asyncio.gather(
            *(service.confirm("demo_duplicate", **identity) for _ in range(6))
        )
        async with factory() as db:
            count = await db.scalar(select(func.count()).select_from(RefundRow))
            order = await OrderRepository(db).get_visible_order("ORD10003", "u_demo")
        report["duplicate_confirmation"] = {
            "requests": 6,
            "business_effects": count,
            "receipt_ids": sorted({r.receipt["refund_id"] for r in results}),
            "passed": count == 1,
        }
        policy = PolicyEngine.demo()
        old_order = order.model_copy(
            update={
                "status": OrderStatus.DELIVERED,
                "paid_at": datetime(2026, 10, 20, tzinfo=UTC),
                "created_at": datetime(2026, 10, 20, tzinfo=UTC),
                "delivered_at": datetime(2026, 11, 1, tzinfo=UTC),
            }
        )
        new_order = old_order.model_copy(update={"paid_at": datetime(2026, 11, 1, tzinfo=UTC)})
        now = datetime(2026, 11, 10, tzinfo=UTC)
        old, new = policy.evaluate(old_order, now=now), policy.evaluate(new_order, now=now)
        report["temporal_policy"] = {
            "old_purchase": old.model_dump(mode="json"),
            "new_purchase": new.model_dump(mode="json"),
            "passed": old.verdict == "DENY" and new.verdict == "ALLOW",
        }
    async with database(root / "changed.db") as factory:
        service = ActionService(factory)
        await service.propose(proposal("demo_changed", "cancel_order"), **identity)
        async with factory.begin() as db:
            await db.execute(
                update(OrderRow).where(OrderRow.order_id == "ORD10003").values(status="shipped")
            )
        result = await service.confirm("demo_changed", **identity)
        report["order_changed_before_confirmation"] = {
            "status": result.status.value,
            "passed": result.status.value == "REQUIRES_RECONFIRMATION",
        }
    async with database(root / "recovery.db") as factory:
        remote_path = root / "independent_remote.db"
        service = ActionService(factory, gateway=ResponseLossDemoGateway(remote_path))
        await service.propose(proposal("demo_lost_response"), **identity)
        initial = await service.confirm("demo_lost_response", **identity)
        restarted = ActionService(factory, gateway=DemoBusinessGateway(remote_path))
        recovered = await restarted.recover(limit=10)
        count = await restarted.gateway.effect_count()
        report["remote_success_response_lost"] = {
            "initial_status": initial.status.value,
            "recovered_status": recovered[0].status.value,
            "remote_business_effects": count,
            "passed": initial.status.value == "UNKNOWN"
            and recovered[0].status.value == "SUCCEEDED"
            and count == 1,
        }
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", help="Empty directory to keep databases and report; default is temporary"
    )
    args = parser.parse_args()
    if args.directory:
        report = asyncio.run(run_demo(Path(args.directory)))
    else:
        with TemporaryDirectory(prefix="reliable-agent-") as folder:
            report = asyncio.run(run_demo(Path(folder)))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(case["passed"] for case in report.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
