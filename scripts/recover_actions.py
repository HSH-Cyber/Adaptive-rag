"""Reconcile a bounded batch of remote operations without loading any LLM."""

import argparse
import asyncio
import json

from app.config.settings import get_settings
from app.policies.engine import PolicyEngine
from app.providers.business_gateway import DemoBusinessGateway
from app.repositories.db import create_engine, create_session_factory
from app.services.action_service import ActionService


async def recover(limit: int) -> None:
    settings = get_settings()
    if settings.action_gateway != "demo":
        raise SystemExit("ACTION_GATEWAY=demo is required for this demo recovery command")
    engine = create_engine(settings)
    try:
        policy = (
            (
                PolicyEngine.load(settings.policy_manifest)
                if settings.policy_manifest
                else PolicyEngine.demo()
            )
            if settings.policy_enabled
            else None
        )
        service = ActionService(
            create_session_factory(engine),
            gateway=DemoBusinessGateway(settings.action_gateway_db),
            policy=policy,
        )
        results = await service.recover(limit=limit)
        print(
            json.dumps([r.model_dump(mode="json") for r in results], ensure_ascii=False, indent=2)
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(recover(args.limit))


if __name__ == "__main__":
    main()
