"""Explicit demo policy rules and source-backed eligibility decisions."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PolicyVerdict = Literal["ALLOW", "DENY", "NEEDS_INFORMATION", "CONFLICT"]


class PolicyRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    policy_id: str
    revision: str
    valid_from: datetime
    valid_to: datetime | None = None
    applicability_basis: Literal["paid_at_or_created_at"] = "paid_at_or_created_at"
    category: str = "*"
    channel: str = "online"
    priority: int = 0
    return_days: int = Field(gt=0, le=365)
    allow_customized: bool = False
    source_path: str
    source_document_id: str
    source_digest: str

    @model_validator(mode="after")
    def validate_interval(self) -> "PolicyRule":
        if self.valid_from.tzinfo is None or (self.valid_to and self.valid_to.tzinfo is None):
            raise ValueError("policy times require timezone")
        if self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("policy validity interval is empty")
        return self


class PolicyEvidence(BaseModel):
    policy_id: str
    revision: str
    document_id: str
    source_digest: str
    return_days: int


class PolicyDecision(BaseModel):
    verdict: PolicyVerdict
    reason: str
    snapshot_id: str
    evidence: list[PolicyEvidence] = Field(default_factory=list)
    facts: dict[str, str] = Field(default_factory=dict)
