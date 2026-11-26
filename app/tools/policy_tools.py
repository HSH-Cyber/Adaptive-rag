"""Read-only policy eligibility with explicitly scoped retrieval evidence."""

from typing import Literal

from pydantic import BaseModel, Field

from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.core.request_scope import get_db_session
from app.core.time import utcnow
from app.policies.engine import PolicyEngine
from app.retrieval.hybrid import HybridRetriever
from app.schemas.agent import ToolResult
from app.schemas.retrieval import PolicySourceRef, RetrievalScope
from app.services.action_rules import assert_business_permitted
from app.tools.base import Tool, ToolContext
from app.tools.deps import RepositoryBackedTool


class EligibilityInput(BaseModel):
    order_id: str = Field(min_length=3, max_length=32)
    tool_name: Literal["create_return_request", "create_refund_request"] = "create_return_request"


class CheckAftersaleEligibilityTool(RepositoryBackedTool, Tool):
    name = "check_aftersale_eligibility"
    description = "查询某订单能否申请售后，返回业务条件、适用政策、例外和证据。仅判断，不提交、不退款。参数 order_id；tool_name 指定 create_return_request（退货）或 create_refund_request（退款）。"
    input_model = EligibilityInput
    risk = ToolRisk.READ

    def __init__(self, policy: PolicyEngine, retriever: HybridRetriever) -> None:
        super().__init__()
        self.policy, self.retriever = policy, retriever

    async def execute(self, parsed: EligibilityInput, context: ToolContext) -> ToolResult:
        order = await self.repository.get_visible_order(parsed.order_id, context.user_id)
        decision = self.policy.evaluate(
            order,
            now=utcnow(),
            tool_name=parsed.tool_name,
            categories=await self.repository.categories_for_order(order),
        )
        try:
            await assert_business_permitted(get_db_session(), parsed.tool_name, order)
        except InvalidRequestError as exc:
            decision = decision.model_copy(update={"verdict": "DENY", "reason": exc.message})
        output = decision.model_dump(mode="json")
        scope = RetrievalScope(
            snapshot_id=decision.snapshot_id,
            document_ids=[e.document_id for e in decision.evidence],
            policy_versions=[e.revision for e in decision.evidence],
            evidence=[
                PolicySourceRef(
                    document_id=e.document_id,
                    policy_version=e.revision,
                    source_digest=e.source_digest,
                )
                for e in decision.evidence
            ],
        )
        chunks = (
            await self.retriever.retrieve("演示售后政策期限定制例外", top_k=3, scope=scope)
            if scope.document_ids
            else []
        )
        output["sources"] = [c.model_dump(mode="json") for c in chunks]
        return ToolResult(name=self.name, success=True, output=output)
