"""Retrieved chunk and citation schemas."""

from pydantic import BaseModel, Field

from app.core.enums import DocumentCategory


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    content: str
    score: float
    rank: int | None = None
    source: str = "hybrid"
    category: DocumentCategory = DocumentCategory.FAQ
    section: str | None = None
    chunk_index: int = 0
    policy_version: str = "v1"
    metadata: dict[str, str] = Field(default_factory=dict)


class Citation(BaseModel):
    document_id: str
    title: str
    chunk_id: str
    section: str | None = None
    policy_version: str | None = None
    source_digest: str | None = None


class PolicySourceRef(BaseModel):
    """One exact applicable source, rather than independent identifier sets."""

    document_id: str
    policy_version: str
    source_digest: str


class RetrievalScope(BaseModel):
    """A policy snapshot and its allowed documents, applied before top_k."""

    snapshot_id: str
    document_ids: list[str]
    policy_versions: list[str] | None = None
    evidence: list[PolicySourceRef] | None = None

    def allows(self, chunk: RetrievedChunk) -> bool:
        return (
            chunk.metadata.get("snapshot_id") == self.snapshot_id
            and chunk.document_id in self.document_ids
            and (self.policy_versions is None or chunk.policy_version in self.policy_versions)
            and (
                self.evidence is None
                or any(
                    ref.document_id == chunk.document_id
                    and ref.policy_version == chunk.policy_version
                    and ref.source_digest == chunk.metadata.get("source_digest")
                    for ref in self.evidence
                )
            )
        )


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    category: DocumentCategory | None = None
