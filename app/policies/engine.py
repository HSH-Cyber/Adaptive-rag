"""Immutable policy snapshots: temporal applicability precedes explanation."""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.schemas.order import Order
from app.schemas.policy import PolicyDecision, PolicyEvidence, PolicyRule, PolicyVerdict
from app.services.action_rules import aware, canonical, digest


class PolicyEngine:
    """Evaluate only explicit rules; LLM scores cannot authorize a write."""

    def __init__(self, rules: list[PolicyRule], sources: dict[str, str] | None = None) -> None:
        self.rules = tuple(rules)
        self.sources = dict(sources or {})
        self.snapshot_id = digest([rule.model_dump(mode="json") for rule in self.rules])

    @classmethod
    def demo(cls) -> "PolicyEngine":
        return cls.load(Path(__file__).with_name("demo.json"))

    @classmethod
    def load(cls, manifest: str | Path) -> "PolicyEngine":
        """Verify immutable policy sources, including containment and content digest."""
        path = Path(manifest).resolve()
        values = json.loads(path.read_text(encoding="utf-8"))
        rules = [PolicyRule.model_validate(value) for value in values["rules"]]
        sources = {}
        identities: dict[tuple[str, str], str] = {}
        for rule in rules:
            identity = (rule.source_document_id, rule.revision)
            if identity in identities and identities[identity] != rule.source_digest:
                raise ValueError("policy document revision has conflicting source digest")
            identities[identity] = rule.source_digest
            source = (path.parent / rule.source_path).resolve()
            if not source.is_relative_to(path.parent):
                raise ValueError("policy source escapes manifest directory")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != rule.source_digest:
                raise ValueError("policy source digest mismatch")
            sources[rule.source_path] = content.decode("utf-8")
        engine = cls(rules, sources)
        if values.get("snapshot_id", engine.snapshot_id) != engine.snapshot_id:
            raise ValueError("policy snapshot digest mismatch")
        return engine

    def export(self, destination: str | Path) -> Path:
        """Publish into a new directory; existing snapshots cannot be overwritten."""
        root = Path(destination).resolve()
        root.mkdir(parents=True, exist_ok=True)
        manifest = root / "manifest.json"
        if manifest.exists():
            raise ValueError("policy snapshot already exists")
        for name, content in self.sources.items():
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError("policy source escapes publication directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="") as out:
                out.write(content)
        with manifest.open("x", encoding="utf-8") as out:
            json.dump(
                {
                    "snapshot_id": self.snapshot_id,
                    "rules": [r.model_dump(mode="json") for r in self.rules],
                },
                out,
                ensure_ascii=False,
                indent=2,
            )
        # Publication isn't usable unless both rules and sources verify.
        self.load(manifest)
        return manifest

    def archive(self, root: str | Path) -> Path:
        """Atomically make a complete snapshot visible to concurrent instances."""
        base = Path(root).resolve()
        base.mkdir(parents=True, exist_ok=True)
        target = base / self.snapshot_id
        if target.exists():
            self.load(target / "manifest.json")
            return target / "manifest.json"
        with TemporaryDirectory(prefix=".policy-", dir=base) as staging:
            prepared = Path(staging) / "snapshot"
            self.export(prepared)
            try:
                prepared.rename(target)
            except OSError:
                # Another publisher may have won; trust only a fully valid result.
                self.load(target / "manifest.json")
        return target / "manifest.json"

    def evaluate(
        self,
        order: Order,
        *,
        now: datetime,
        tool_name: str = "create_return_request",
        channel: str = "online",
        categories: dict[str, str] | None = None,
    ) -> PolicyDecision:
        """Apply purchase-version and delivery-deadline rules to every item."""
        basis = aware(order.paid_at or order.created_at)
        facts = {
            "order_id": order.order_id,
            "applicable_at": basis.isoformat(),
            "delivered_at": aware(order.delivered_at).isoformat() if order.delivered_at else "",
            "tool_name": tool_name,
            "channel": channel,
            "categories": canonical(categories or {}),
        }
        evidence: list[PolicyEvidence] = []

        def result(verdict: PolicyVerdict, reason: str) -> PolicyDecision:
            return PolicyDecision(
                verdict=verdict,
                reason=reason,
                snapshot_id=self.snapshot_id,
                evidence=evidence,
                facts=facts,
            )

        if not order.items:
            return result("NEEDS_INFORMATION", "缺少商品明细，不能判断售后条件。")
        if basis > aware(now):
            return result("NEEDS_INFORMATION", "订单时间晚于当前时间，请核对业务数据。")
        for item in order.items:
            candidates = [
                r
                for r in self.rules
                if r.channel == channel
                and r.valid_from <= basis
                and (r.valid_to is None or basis < r.valid_to)
                and (r.category == "*" or (categories or {}).get(item.sku) == r.category)
            ]
            if not candidates:
                return result("NEEDS_INFORMATION", "缺少适用于该订单的政策或商品类别。")
            priority = max(r.priority for r in candidates)
            chosen = [r for r in candidates if r.priority == priority]
            if len({(r.return_days, r.allow_customized) for r in chosen}) > 1:
                return result("CONFLICT", "同优先级政策存在冲突，需要人工核实。")
            rule = sorted(chosen, key=lambda r: (r.policy_id, r.revision))[0]
            entry = PolicyEvidence(
                policy_id=rule.policy_id,
                revision=rule.revision,
                document_id=rule.source_document_id,
                source_digest=rule.source_digest,
                return_days=rule.return_days,
            )
            if entry not in evidence:
                evidence.append(entry)
            if item.customized and not rule.allow_customized:
                return result("DENY", "订单含定制商品，不适用本演示无理由售后政策。")
            if tool_name == "create_refund_request" and order.status.value == "paid":
                continue
            if order.delivered_at is None:
                return result("NEEDS_INFORMATION", "缺少签收时间，暂不能判断售后期限。")
            if aware(order.delivered_at) > aware(now):
                return result("NEEDS_INFORMATION", "签收时间晚于当前时间，请核对业务数据。")
            if aware(now) >= aware(order.delivered_at) + timedelta(days=rule.return_days):
                return result("DENY", "已超过适用政策的售后申请期限。")
        return result("ALLOW", "满足演示政策的申请条件；提交不代表审核通过或退款到账。")

    def chunks(self):
        """Build only verified source chunks, isolated by immutable snapshot ID."""
        from app.schemas.retrieval import RetrievedChunk

        return [
            RetrievedChunk(
                chunk_id=f"{self.snapshot_id}:{r.source_document_id}:{r.source_digest[:12]}",
                document_id=r.source_document_id,
                title=f"演示政策 {r.revision}",
                content=self.sources[r.source_path],
                score=0,
                policy_version=r.revision,
                metadata={"snapshot_id": self.snapshot_id, "source_digest": r.source_digest},
            )
            for r in self.rules
            if r.source_path in self.sources
        ]
