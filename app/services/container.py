"""Dependency-injection container and runtime composition."""

from __future__ import annotations

import asyncio
import logging

from app.config.settings import Settings
from app.infra.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from app.infra.redis_client import connect_redis
from app.mcp.adapter import merge_mcp_tools
from app.mcp.runtime import LOCAL_CATALOG
from app.policies.engine import PolicyEngine
from app.providers.base import LLMProvider
from app.providers.business_gateway import DemoBusinessGateway
from app.providers.deepseek import DeepSeekProvider
from app.providers.embeddings import build_embedding_provider
from app.providers.fake import OfflineLLMProvider
from app.providers.reranker import CrossEncoderRerankProvider
from app.repositories.db import create_engine, create_session_factory, init_schema
from app.retrieval.cache import MemoryRetrievalCache, RedisRetrievalCache
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.ingest import ingest_knowledge
from app.retrieval.reranker import DocumentReranker
from app.retrieval.vector import VectorRetriever, build_records
from app.services.action_service import ActionService
from app.services.chat_service import ChatService
from app.services.seed import seed_demo_data
from app.tools.factory import build_tool_registry
from app.tools.policy_tools import CheckAftersaleEligibilityTool

logger = logging.getLogger(__name__)


class AppContainer:
    def __init__(
        self,
        settings: Settings,
        llm: LLMProvider,
        chat_service: ChatService,
        engine,
        session_factory,
        retriever: HybridRetriever,
        redis=None,
        vector_store=None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.chat_service = chat_service
        self.engine = engine
        self.session_factory = session_factory
        self.retriever = retriever
        self.redis = redis
        self.vector_store = vector_store

    async def aclose(self) -> None:
        closer = getattr(self.llm, "aclose", None)
        if closer:
            await closer()
        if self.redis is not None:
            closer = getattr(self.redis, "aclose", None)
            if closer:
                await closer()
        await self.engine.dispose()


async def build_infrastructure(settings: Settings):
    """Create the database session factory and optional Redis connection."""
    engine = create_engine(settings)
    await init_schema(engine)
    session_factory = create_session_factory(engine)
    redis = await connect_redis(settings)
    return engine, session_factory, redis


# 主要的检索基础设施在服务器启动阶段就创建好了。
async def build_retrieval(settings: Settings, redis, policy: PolicyEngine | None = None):
    """Build the knowledge ingestion and hybrid retrieval components."""
    embedder = build_embedding_provider(settings)
    cache = (
        RedisRetrievalCache(redis, ttl_seconds=settings.retrieval_cache_ttl_seconds)
        if redis is not None
        else MemoryRetrievalCache(ttl_seconds=settings.retrieval_cache_ttl_seconds)
    )
    bm25, store, corpus = await ingest_knowledge(
        settings.knowledge_dir,
        embedder,
        settings=settings,
        qdrant_url=settings.qdrant_url,
        qdrant_collection=settings.qdrant_collection,
        embedding_dim=getattr(embedder, "dim", settings.embedding_dim),
    )
    if policy is not None:
        policy_chunks = policy.chunks()
        bm25.build([*corpus, *policy_chunks])
        records = build_records(
            policy_chunks, await embedder.embed([c.content for c in policy_chunks])
        )
        await asyncio.to_thread(store.upsert, records)
    retriever = HybridRetriever(
        bm25, VectorRetriever(store, embedder), rrf_k=settings.rrf_k, cache=cache
    )
    reranker = DocumentReranker(CrossEncoderRerankProvider(settings))
    return retriever, reranker, store


async def build_container(settings: Settings, seed: bool = False) -> AppContainer:
    """Compose runtime services from independently testable infrastructure builders."""
    llm: LLMProvider = (
        DeepSeekProvider(settings) if settings.llm_configured else OfflineLLMProvider()
    )
    engine, session_factory, redis = await build_infrastructure(settings)
    rate_limiter = (
        RedisRateLimiter(redis, limit=settings.rate_limit_per_minute)
        if redis is not None
        else InMemoryRateLimiter(limit=settings.rate_limit_per_minute)
    )
    policy = None
    if settings.policy_enabled:
        policy = (
            PolicyEngine.load(settings.policy_manifest)
            if settings.policy_manifest
            else PolicyEngine.demo()
        )
        policy.archive(settings.policy_snapshot_dir)
    retriever, reranker, store = await build_retrieval(settings, redis, policy)
    registry = build_tool_registry(retriever)
    if policy is not None:
        registry.register(CheckAftersaleEligibilityTool(policy, retriever))
    # 启动时：把 MCP 工具注册进工具池
    if settings.mcp_enabled:
        specs = list(LOCAL_CATALOG)
        if settings.mcp_remote_url:
            specs.append(
                {
                    "name": "mcp_remote_echo",
                    "description": "Remote MCP tool via HTTP (same Function Calling schema)",
                    "transport": "http",
                    "endpoint": settings.mcp_remote_url.rstrip("/") + "/mcp/call",
                }
            )
        merge_mcp_tools(registry, specs)
    chat_service = ChatService(
        settings=settings,
        llm=llm,
        retriever=retriever,
        reranker=reranker,
        session_factory=session_factory,
        rate_limiter=rate_limiter,
        registry=registry,
        actions=ActionService(
            session_factory,
            gateway=DemoBusinessGateway(settings.action_gateway_db)
            if settings.action_gateway == "demo"
            else None,
            policy=policy,
        ),
    )
    if seed:
        async with session_factory() as session:
            await seed_demo_data(session)
            await session.commit()
    logger.info(
        "runtime_ready database=%s cache=%s vector_store=%s llm=%s local_embedding=%s cross_encoder=%s",
        "sqlite" if settings.use_sqlite else "postgresql",
        "redis" if redis is not None else "memory",
        type(store).__name__,
        "deepseek" if settings.llm_configured else "offline",
        settings.local_embedding_configured,
        settings.cross_encoder_configured,
    )
    return AppContainer(
        settings=settings,
        llm=llm,
        chat_service=chat_service,
        engine=engine,
        session_factory=session_factory,
        retriever=retriever,
        redis=redis,
        vector_store=store,
    )
