"""Public durable action states and authoritative receipts."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActionStatus(str, Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    READY = "READY"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REQUIRES_RECONFIRMATION = "REQUIRES_RECONFIRMATION"
    UNKNOWN = "UNKNOWN"


class ActionView(BaseModel):
    action_id: str
    tool_name: str
    order_id: str
    status: ActionStatus
    expires_at: datetime
    receipt: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    message: str = ""
    policy_evidence: dict[str, Any] = Field(default_factory=dict)


class ActionConfirmRequest(BaseModel):
    """Demo identity; production must bind the actor through trusted authentication."""

    user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
