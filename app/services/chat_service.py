"""Chat orchestration: graph execution, streaming and persistence."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.graph import build_graph
from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.exceptions import AppError, GuardrailError, RateLimitError
from app.core.ids import new_id
from app.core.request_scope import request_session
from app.infra.rate_limit import RateLimiter
from app.observability.metrics import REQUEST_LATENCY, REQUESTS, ROUTING, STAGE_LATENCY, TOKEN_USAGE
from app.providers.base import LLMProvider
from app.repositories.session_repository import SessionRepository
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.reranker import DocumentReranker
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.common import LatencyBreakdown, TokenUsage
from app.services.action_service import ActionService
from app.tools.factory import build_tool_registry
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMProvider,
        # 检索器：
        retriever: HybridRetriever,
        reranker: DocumentReranker,
        # 创建数据库操作窗口
        session_factory: async_sessionmaker[AsyncSession],
        rate_limiter: RateLimiter,
        registry: ToolRegistry | None = None,
        actions: ActionService | None = None,
    ) -> None:
        # _ 是 Python 中的命名约定。通常表示：这是类内部使用的属性，不建议外部直接随意操作。
        self._settings = settings
        self._llm = llm
        self._retriever = retriever
        self._reranker = reranker
        self._session_factory = session_factory
        self._rate_limiter = rate_limiter
        self.registry = registry or build_tool_registry(retriever)
        self.actions = actions or ActionService(session_factory)
        # ChatService 在初始化时，就创建好了 LangGraph 工作流。
        self._graph = build_graph(
            llm=llm,
            settings=settings,
            retriever=retriever,
            reranker=reranker,
            registry=self.registry,
        )

    # 不负责亲自思考如何回答问题，它负责组织整个回答过程
    async def chat(self, request: ChatRequest, *, trace_id: str) -> ChatResponse:
        await self._enforce_rate_limit(request.user_id)
        started = time.perf_counter()
        session_id = request.session_id or new_id("sess_")
        if request.confirm_action_id:
            result_action = await self.actions.confirm(
                request.confirm_action_id,
                user_id=request.user_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            response = ChatResponse(
                answer=result_action.message,
                session_id=session_id,
                trace_id=trace_id,
                action_result=result_action,
                error_code=result_action.error_code,
            )
            await self._save(request, response)
            self._observe(response, started)
            return response
        # async with是异步上下文管理器的用法。进入一个资源使用区域，执行完之后自动进行清理。数据库
        async with request_session(self._session_factory) as db:
            # 准备用户问题和聊天历史
            state = await self._initial_state(db, request, session_id, trace_id)
            await db.commit()
        async with request_session(self._session_factory):
            # 把问题交给 LangGraph
            # ChatService 把用户的问题和相关上下文交给 LangGraph，让它自己按照工作流完成处理。
            result = await self._invoke(state)
            # 把工作流结果转换成响应
            response = self._to_response(result, session_id=session_id, trace_id=trace_id)
        # 保存待确认操作
        await self._remember_pending(request, response)
        # 保存聊天记录，业务操作与模型推理不再共用此事务。
        await self._save(request, response, summary=result.get("summary") or "")
        # 记录运行指标
        self._observe(response, started)
        return response

    async def stream(self, request: ChatRequest, *, trace_id: str) -> AsyncIterator[dict]:
        """SSE: report progress, then send only the validated user-facing answer."""
        if request.confirm_action_id:
            response = await self.chat(request, trace_id=trace_id)
            yield {"event": "started", "trace_id": trace_id}
            for index in range(0, len(response.answer), 48):
                yield {"event": "token", "text": response.answer[index : index + 48]}
            yield {"event": "answer", "data": response.model_dump(mode="json")}
            yield {"event": "done", "trace_id": trace_id}
            return
        await self._enforce_rate_limit(request.user_id)
        started = time.perf_counter()
        session_id = request.session_id or new_id("sess_")
        async with request_session(self._session_factory) as db:
            state = await self._initial_state(db, request, session_id, trace_id)
            await db.commit()
        async with request_session(self._session_factory):
            yield {"event": "started", "trace_id": trace_id}
            queue: asyncio.Queue[dict | None] = asyncio.Queue()
            holder: dict = {"merged": dict(state), "guardrail": None, "error": None}

            async def _run_graph() -> None:
                try:
                    async for update in self._graph.astream(state, stream_mode="updates"):
                        for node, payload in update.items():
                            if isinstance(payload, dict):
                                holder["merged"].update(payload)
                            event: dict = {"event": "node", "node": node}
                            if isinstance(payload, dict) and payload.get("intent"):
                                event["intent"] = payload["intent"].label.value
                            if isinstance(payload, dict) and payload.get("strategy"):
                                event["strategy"] = payload["strategy"].value
                            await queue.put(event)
                except GuardrailError as exc:
                    holder["guardrail"] = exc
                except Exception as exc:  # graph failures surface after the queue drains
                    holder["error"] = exc
                finally:
                    await queue.put(None)

            task = asyncio.create_task(_run_graph())
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
                await task
            finally:
                if not task.done():
                    task.cancel()

            if holder["guardrail"] is not None:
                exc = holder["guardrail"]
                yield {
                    "event": "answer",
                    "data": ChatResponse(
                        answer=exc.message,
                        session_id=session_id,
                        trace_id=trace_id,
                        refused=True,
                        error_code=exc.error_code.value,
                    ).model_dump(mode="json"),
                }
                yield {"event": "done", "trace_id": trace_id}
                return
            if holder["error"] is not None:
                raise holder["error"]
            merged: AgentState = holder["merged"]
            response = self._to_response(merged, session_id=session_id, trace_id=trace_id)
        await self._remember_pending(request, response)
        await self._save(request, response, summary=merged.get("summary") or "")
        self._observe(response, started)
        # Never stream the LLM's JSON envelope or an answer that may still be
        # replaced by the faithfulness check. Only the final answer is public.
        for index in range(0, len(response.answer), 48):
            yield {"event": "token", "text": response.answer[index : index + 48]}
        yield {"event": "answer", "data": response.model_dump(mode="json")}
        yield {"event": "done", "trace_id": trace_id}

    async def _enforce_rate_limit(self, user_id: str) -> None:
        allowed = await self._rate_limiter.allow(f"user:{user_id}")
        if not allowed:
            raise RateLimitError()

    async def _initial_state(
        self,
        db: AsyncSession,
        request: ChatRequest,
        session_id: str,
        trace_id: str,
    ) -> AgentState:
        sessions = SessionRepository(db)
        await sessions.get_or_create(session_id, request.user_id)
        history = await sessions.recent_messages(session_id)
        return {
            "trace_id": trace_id,
            "session_id": session_id,
            "user_id": request.user_id,
            "user_query": request.message,
            "messages": history,
            "summary": "",
            "retrieved_chunks": [],
            "tool_results": [],
            "citations": [],
            "refused": False,
            "step_count": 0,
            "token_usage": TokenUsage(),
            "latency": LatencyBreakdown(),
            "confirm_action_id": request.confirm_action_id,
            "confirmed_action": None,
            "idempotency_key": request.idempotency_key,
        }

    async def _remember_pending(self, request: ChatRequest, response: ChatResponse) -> None:
        action = response.pending_action
        if action is not None:
            response.pending_action = await self.actions.propose(
                action,
                session_id=response.session_id,
                user_id=request.user_id,
                trace_id=response.trace_id,
            )

    async def _save(
        self, request: ChatRequest, response: ChatResponse, *, summary: str = ""
    ) -> None:
        async with request_session(self._session_factory) as db:
            await self._persist(db, request, response, summary=summary)
            await db.commit()

    async def _invoke(self, state: AgentState) -> AgentState:
        try:
            # LangGraph widens the state to dict[str, Any]; the graph contract is AgentState.
            # ChatService 把用户的问题和相关上下文交给 LangGraph，让它自己按照工作流完成处理。
            return cast(AgentState, await self._graph.ainvoke(state))
        except GuardrailError as exc:
            return {
                **state,  # 字典解包，把原来 state 字典中的内容全部复制过来
                "final_answer": exc.message,
                "refused": True,
                "error": exc.error_code.value,
            }
            # 如果遇到其他应用级错误，就继续把异常向上抛出。
        except AppError:
            raise

    async def _persist(
        self,
        db: AsyncSession,
        request: ChatRequest,
        response: ChatResponse,
        *,
        summary: str = "",
    ) -> None:
        sessions = SessionRepository(db)
        await sessions.append_message(response.session_id, "user", request.message)
        await sessions.append_message(response.session_id, "assistant", response.answer)
        if summary:
            await sessions.save_summary(response.session_id, summary)

    def _to_response(
        self,
        result: AgentState,
        *,
        session_id: str,
        trace_id: str,
    ) -> ChatResponse:
        answer = result.get("final_answer") or "暂时无法回答，请稍后重试。"
        strategy = result.get("strategy")
        intent = result.get("intent")
        return ChatResponse(
            answer=answer,
            session_id=session_id,
            trace_id=trace_id,
            intent=intent.label if intent else None,
            complexity=result.get("query_complexity"),
            strategy=strategy,
            citations=result.get("citations") or [],
            pending_action=result.get("pending_action"),
            refused=bool(result.get("refused")),
            error_code=result.get("error"),
            token_usage=result.get("token_usage") or TokenUsage(),
            latency=result.get("latency") or LatencyBreakdown(),
        )

    # 记录系统运行情况，负责监控一次聊天请求
    def _observe(self, response: ChatResponse, started: float) -> None:
        # 计算从请求开始到现在一共经过了多少毫秒
        total_ms = (time.perf_counter() - started) * 1000
        response.latency.total_ms = total_ms
        REQUESTS.labels(
            strategy=(response.strategy.value if response.strategy else "unknown"),
            intent=(response.intent.value if response.intent else "unknown"),
            status="ok" if not response.refused else "refused",
        ).inc()
        if response.strategy:
            ROUTING.labels(strategy=response.strategy.value).inc()
        REQUEST_LATENCY.observe(total_ms / 1000)
        STAGE_LATENCY.labels(stage="intent").observe(response.latency.intent_ms / 1000)
        STAGE_LATENCY.labels(stage="retrieval").observe(response.latency.retrieval_ms / 1000)
        STAGE_LATENCY.labels(stage="llm").observe(response.latency.llm_ms / 1000)
        TOKEN_USAGE.labels(kind="total").inc(response.token_usage.total_tokens)
        logger.info(
            "chat_completed",
            extra={
                "trace_id": response.trace_id,
                "strategy": response.strategy.value if response.strategy else None,
                "intent": response.intent.value if response.intent else None,
                "latency_ms": round(total_ms, 1),
            },
        )
