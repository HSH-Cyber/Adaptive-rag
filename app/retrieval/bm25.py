"""BM25 lexical retriever over a jieba-tokenised inverted index."""

# 它可以把中文句子切成一个个词语。
from __future__ import annotations

from collections import defaultdict

from rank_bm25 import BM25Okapi

from app.retrieval.tokenizer import tokenize
from app.schemas.retrieval import RetrievalScope, RetrievedChunk


# BM25 是一种基于关键词匹配的文本检索算法。
class BM25Retriever:
    """Lexical retriever. Complements dense search on exact policy numbers / SKUs."""

    def __init__(self, corpus: list[RetrievedChunk] | None = None) -> None:
        self._chunks: list[RetrievedChunk] = []
        self._bm25: BM25Okapi | None = None
        if corpus:
            self.build(corpus)

    def build(self, corpus: list[RetrievedChunk]) -> None:
        # 保存全部文本块。
        self._chunks = list(corpus)
        # 把每个文本块拆成关键词。
        tokenized = [tokenize(chunk.content) for chunk in self._chunks]
        # BM25Okapi requires a non-empty corpus; keep None if ingest has not run.
        # 建立 BM25 检索索引。
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

    def search(
        self, query: str, *, top_k: int, scope: RetrievalScope | None = None
    ) -> list[RetrievedChunk]:
        if self._bm25 is None or not self._chunks:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        candidates = [
            i for i, chunk in enumerate(self._chunks) if scope is None or scope.allows(chunk)
        ]
        ranked = sorted(candidates, key=lambda i: scores[i], reverse=True)[:top_k]
        results: list[RetrievedChunk] = []
        for rank, index in enumerate(ranked, start=1):
            # BM25 IDF can be negative on tiny corpora; still return the ranked docs.
            chunk = self._chunks[index].model_copy(deep=True)
            chunk.score = float(scores[index])
            chunk.rank = rank
            chunk.source = "bm25"
            results.append(chunk)
        return results


class InvertedIndex:
    """Tiny helper used in tests to inspect token coverage."""

    def __init__(self, corpus: list[RetrievedChunk]) -> None:
        self.postings: dict[str, set[str]] = defaultdict(set)
        for chunk in corpus:
            for token in tokenize(chunk.content):
                self.postings[token].add(chunk.chunk_id)
