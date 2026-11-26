"""Policy constraints apply before candidate limits and cache reuse."""

import pytest

from app.retrieval.bm25 import BM25Retriever
from app.retrieval.cache import MemoryRetrievalCache
from app.retrieval.vector import InMemoryVectorStore, VectorRecord
from app.schemas.retrieval import RetrievalScope, RetrievedChunk


def chunk(name, version="v1"):
    return RetrievedChunk(
        chunk_id=name,
        document_id=name,
        title=name,
        content="退货政策期限",
        score=1,
        policy_version=version,
        metadata={"snapshot_id": "s1"},
    )


def test_scope_filters_before_vector_top_k():
    store = InMemoryVectorStore()
    store.upsert(
        [
            VectorRecord(chunk("old"), [10.0]),
            VectorRecord(chunk("valid", "v2"), [1.0]),
        ]
    )
    scope = RetrievalScope(snapshot_id="s1", document_ids=["valid"], policy_versions=["v2"])
    hits = store.search([1.0], top_k=1, scope=scope)
    assert [h.document_id for h in hits] == ["valid"]


def test_scope_filters_before_bm25_top_k():
    retriever = BM25Retriever([chunk("old"), chunk("valid", "v2")])
    scope = RetrievalScope(snapshot_id="s1", document_ids=["valid"], policy_versions=["v2"])
    assert [h.document_id for h in retriever.search("退货", top_k=1, scope=scope)] == ["valid"]


def test_mixed_order_scope_does_not_cross_document_version_pairs():
    corpus = []
    for document, version, source_digest in [
        ("A", "v2", "a2"),
        ("A", "v1", "a1"),
        ("B", "v1", "b1"),
        ("B", "v2", "b2"),
    ]:
        corpus.append(
            chunk(document, version).model_copy(
                update={"metadata": {"snapshot_id": "s1", "source_digest": source_digest}}
            )
        )
    scope = RetrievalScope(
        snapshot_id="s1",
        document_ids=["A", "B"],
        policy_versions=["v1", "v2"],
        evidence=[
            {"document_id": "A", "policy_version": "v1", "source_digest": "a1"},
            {"document_id": "B", "policy_version": "v2", "source_digest": "b2"},
        ],
    )
    hits = BM25Retriever(corpus).search("退货", top_k=4, scope=scope)
    assert {(h.document_id, h.policy_version) for h in hits} == {("A", "v1"), ("B", "v2")}


def test_scope_rejects_same_document_version_with_wrong_content_digest():
    store = InMemoryVectorStore()
    old = chunk("A").model_copy(update={"metadata": {"snapshot_id": "s1", "source_digest": "old"}})
    good = old.model_copy(update={"metadata": {"snapshot_id": "s1", "source_digest": "good"}})
    old.chunk_id, good.chunk_id = "old", "good"
    store.upsert([VectorRecord(old, [10.0]), VectorRecord(good, [1.0])])
    scope = RetrievalScope(
        snapshot_id="s1",
        document_ids=["A"],
        evidence=[
            {"document_id": "A", "policy_version": "v1", "source_digest": "good"},
        ],
    )
    assert store.search([1.0], top_k=1, scope=scope)[0].chunk_id == "good"


@pytest.mark.asyncio
async def test_cache_separates_policy_scope_and_snapshot():
    cache = MemoryRetrievalCache(ttl_seconds=60)
    one = RetrievalScope(snapshot_id="s1", document_ids=["a"])
    two = RetrievalScope(snapshot_id="s1", document_ids=["b"])
    three = RetrievalScope(snapshot_id="s2", document_ids=["a"])
    await cache.set("退货", 1, [chunk("a")], scope=one)
    assert await cache.get("退货", 1, scope=two) is None
    assert await cache.get("退货", 1, scope=three) is None
    assert (await cache.get("退货", 1, scope=one))[0].document_id == "a"


def test_empty_scope_is_fail_closed():
    store = InMemoryVectorStore()
    store.upsert([VectorRecord(chunk("old"), [1.0])])
    assert (
        store.search([1.0], top_k=1, scope=RetrievalScope(snapshot_id="s1", document_ids=[])) == []
    )


def test_qdrant_receives_policy_scope_before_limit():
    from app.retrieval.vector import QdrantVectorStore

    class Client:
        def search(self, **kwargs):
            self.request = kwargs
            return []

    client = Client()
    store = QdrantVectorStore("http://unused", "test", 1)
    store._client = client
    scope = RetrievalScope(snapshot_id="s1", document_ids=["valid"], policy_versions=["v2"])
    assert store.search([1.0], top_k=1, scope=scope) == []
    conditions = {
        c.key: c.match.model_dump(exclude_none=True) for c in client.request["query_filter"].must
    }
    assert conditions == {
        "snapshot_id": {"value": "s1"},
        "document_id": {"any": ["valid"]},
        "policy_version": {"any": ["v2"]},
    }
    assert client.request["limit"] == 1


@pytest.mark.asyncio
async def test_cache_separates_exact_evidence_with_same_identifier_sets():
    cache = MemoryRetrievalCache(ttl_seconds=60)
    one = RetrievalScope(
        snapshot_id="s1",
        document_ids=["A"],
        evidence=[
            {"document_id": "A", "policy_version": "v1", "source_digest": "one"},
        ],
    )
    two = one.model_copy(
        update={"evidence": [one.evidence[0].model_copy(update={"source_digest": "two"})]}
    )
    await cache.set("退货", 1, [chunk("A")], scope=one)
    assert await cache.get("退货", 1, scope=two) is None


def test_qdrant_uses_or_of_exact_source_combinations():
    from app.retrieval.vector import QdrantVectorStore

    class Client:
        def search(self, **kwargs):
            self.request = kwargs
            return []

    client = Client()
    store = QdrantVectorStore("http://unused", "test", 1)
    store._client = client
    scope = RetrievalScope(
        snapshot_id="s1",
        document_ids=["A", "B"],
        evidence=[
            {"document_id": "A", "policy_version": "v1", "source_digest": "a1"},
            {"document_id": "B", "policy_version": "v2", "source_digest": "b2"},
        ],
    )
    store.search([1.0], top_k=1, scope=scope)
    branches = client.request["query_filter"].must[-1].should
    values = [
        {condition.key: condition.match.value for condition in branch.must} for branch in branches
    ]
    assert values == [
        {"document_id": "A", "policy_version": "v1", "source_digest": "a1"},
        {"document_id": "B", "policy_version": "v2", "source_digest": "b2"},
    ]
