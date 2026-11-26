"""Document retrieval node."""

from __future__ import annotations

import time

from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.query_rewrite import QueryRewriter
from app.retrieval.reranker import DocumentReranker
from app.retrieval.self_rag import SelfRAGLoop
from app.schemas.common import LatencyBreakdown
from app.schemas.retrieval import RetrievedChunk


async def retrieval_node(
    state: AgentState,
    *,
    retriever: HybridRetriever,
    reranker: DocumentReranker,
    rewriter: QueryRewriter,
    self_rag: SelfRAGLoop,
    settings: Settings,
) -> dict:
    started = time.perf_counter()
    query = state["user_query"]
    #如果是 MULTI，就使用复杂问题的检索数量。
    strategy = state.get("strategy")
    #如果是复杂问题（MULTI），系统去知识库里找 retrieval_top_k_complex
    top_k = (
        settings.retrieval_top_k_complex
        if strategy == RetrievalStrategy.MULTI
        else settings.retrieval_top_k_simple
    )
    #如果是 MULTI，就进入 Self-RAG自我反思检索）。 检索流程。
    if strategy == RetrievalStrategy.MULTI:
        chunks = await self_rag.retrieve(query, top_k=top_k)
        rewritten = [query]
    else:
        #重写 + 混合检索 + 重排
        #重写成规范的问题形式
        suggestions = await rewriter.rewrite(query)
        #去重并限制数量
        #去重神技。因为用两个不同的词搜，可能会搜到同一篇文章。它用文章的 ID 作为字典的 Key，如果这个 Key 不存在，就存进去；如果已经存在了，就忽略。这样就保证了找出来的资料没有重复。
        rewritten = list(dict.fromkeys([query, *suggestions]))[:2]
        by_id: dict[str, RetrievedChunk] = {}
        #混合检索合并：这里用刚才得到的最多 2 个查询词，分别去数据库里搜。
        for search_query in rewritten:
            for candidate in await retriever.retrieve(search_query, top_k=top_k):
                by_id.setdefault(candidate.chunk_id, candidate)
        #挑出最最匹配的 top_n 篇（比如挑出前 3 篇），交给后面的大模型去读。      
        chunks = await reranker.rerank(query, list(by_id.values()), top_n=settings.rerank_top_n)
    latency = state.get("latency") or LatencyBreakdown()
    latency.retrieval_ms = (time.perf_counter() - started) * 1000
    return {"retrieved_chunks": chunks, "rewritten_queries": rewritten, "latency": latency}
