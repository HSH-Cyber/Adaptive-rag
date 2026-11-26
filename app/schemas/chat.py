"""Chat request/response and action-proposal schemas."""

from datetime import datetime
from typing import Any, Literal

# 数据格式检查工具
from pydantic import BaseModel, Field

from app.core.enums import IntentLabel, QueryComplexity, RetrievalStrategy, ToolRisk
from app.schemas.actions import ActionView
from app.schemas.common import LatencyBreakdown, TokenUsage
from app.schemas.retrieval import Citation


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    name: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    user_id: str = Field(default="anonymous", min_length=1, max_length=64)
    stream: bool = False
    # 先返回一个待确认操作。然后用户确认时，再把对应的 action_id 传回来。
    confirm_action_id: str | None = None
    # 幂等键：同一个操作重复提交，不应该产生重复的业务效果。
    idempotency_key: str | None = Field(default=None, max_length=128)


class ActionProposal(BaseModel):
    action_id: str
    tool_name: str
    arguments: dict[str, Any]
    risk: ToolRisk
    summary: str
    expires_at: datetime | None = None
    policy_evidence: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    trace_id: str
    intent: IntentLabel | None = None
    complexity: QueryComplexity | None = None
    strategy: RetrievalStrategy | None = None
    citations: list[Citation] = Field(default_factory=list)
    # 等待用户确认的操作
    pending_action: ActionProposal | None = None
    action_result: ActionView | None = None
    refused: bool = False
    error_code: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    # 各阶段耗时
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
