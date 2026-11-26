# Reliable Aftersales Agent Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline in the existing checkout. Steps use checkbox syntax for tracking.

**Goal:** Deliver durable confirmed business actions, fault recovery, and evidence-backed policy eligibility.

**Architecture:** A database-owned action service isolates business transactions from chat/LLM persistence. Immutable policy snapshots constrain eligibility and retrieval. Remote calls use stable keys, durable order reservations, and receipt reconciliation.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 async, SQLite/PostgreSQL, Pydantic 2, existing LangGraph 0.2.x.

**Spec:** ../specs/2026-11-25-reliable-aftersales-agent-design.md (approved by user).

## Global Constraints

- Preserve the existing dirty checkout, comments, JSON/SSE APIs; no reset or automatic commits.
- Demo gateway only; do not invoke real payment/logistics services or read/modify developer secrets.
- UTC persisted timestamps, integer cents, owner/session binding, no model-authorized writes.
- UNKNOWN must retain its reservation; lease expiry is not proof of non-execution.
- One whole-order aftersales flow; explicit business matrix in the spec. No framework upgrade.
- Use current `my-dev` checkout per existing workspace preference. Implement continuously following user approval; no additional plan approval.

## Review Focus

- Different action IDs for the same order must serialize and check active business records (Task 2).
- External commit followed by lost response must reconcile without resubmission (Task 3).
- A completed operation must survive chat persistence/model failure and expiry (Task 4).
- Evidence changes between proposal and confirmation must prevent execution (Task 5).
- Invalid-policy candidates must not crowd out valid candidates before top_k (Task 6).

### Task 1: Durable models and migration

Files: `app/schemas/actions.py`, `app/repositories/action_models.py`, `app/repositories/models.py`, `alembic/versions/0003_actions.py`, `tests/integration/test_actions.py`.
Interfaces: `ActionStatus`, `ActionView`, `ActionService.propose/get/confirm` as specified below.
- [x] Write database-backed tests: proposed action survives service recreation; action retrieval rejects wrong owner/session; expired proposal cannot execute.
- [x] Run tests and observe missing-feature failure.
- [x] Implement operation/event/reservation models and migration, retaining existing schema.
- [x] Verify new schema creation plus upgrade/downgrade on a disposable database.

### Task 2: Atomic local execution

Files: `app/services/action_service.py`, `app/services/action_rules.py`, `app/repositories/order_repository.py`, `tests/integration/test_actions.py`.
Interfaces: `ActionService(factory, ...).propose(proposal, user_id, session_id, trace_id) -> ActionProposal`; `confirm(action_id, user_id, session_id, trace_id) -> ActionView`; `get(action_id, user_id, session_id) -> ActionView`.
- [x] Add failing tests for same-action/different-action concurrency, state changes, business matrix and atomic rollback.
- [x] Implement action CAS and order serialization using short sessions; immutable arguments and snapshots; deterministic receipts.
- [x] Verify file SQLite independent-connection concurrency and local rollback; provide optional PostgreSQL execution.

### Task 3: Remote receipt recovery

Files: `app/providers/business_gateway.py`, `app/services/action_service.py`, `scripts/recover_actions.py`, `tests/integration/test_action_recovery.py`.
Interfaces: `BusinessGateway.submit(request) -> GatewayReceipt`, `lookup(key) -> GatewayReceipt`; `ActionService.recover(limit) -> list[ActionView]`.
- [x] Add lost-response, crash/expired-lease, unknown lookup, conflicting order action and stale-worker tests.
- [x] Implement independent persistent demo gateway, stable provider keys, reservations, lease/attempt CAS and bounded recovery.
- [x] Verify only authoritative non-acceptance allows resubmit; query/network uncertainty never does.

### Task 4: Chat, tools and action APIs

Files: `app/services/chat_service.py`, `app/agents/nodes/executor.py`, `app/tools/aftersale_tools.py`, `app/api/routes/actions.py`, `app/main.py`, `app/services/container.py`, `app/schemas/chat.py`, `app/repositories/session_repository.py`, `tests/e2e/test_action_api.py`.
- [x] Write failing end-to-end tests for persisted proposal/confirmation, JSON/SSE receipts, direct write bypass, failed chat persistence after success and cross-session history access.
- [x] Wire the single action service into chat and independent action routes. Separate preparation/read, graph work, and persistence sessions; bypass model inference on confirmations.
- [x] Keep read fallback; reject unconfirmed writes; protect confirmed receipt presentation from model rewriting.
- [x] Run existing confirmation/order tests and adjust obsolete in-memory tests to the durable contract.

### Task 5: Deterministic temporal policy eligibility

Files: `app/policies/engine.py`, `app/schemas/policy.py`, `app/policies/demo.json`, `knowledge/demo_policies/`, `app/tools/policy_tools.py`, `app/services/action_service.py`, `tests/unit/test_policy.py`, `tests/integration/test_actions.py`.
Interfaces: `PolicyEngine.evaluate(order, products, now) -> PolicyDecision`; decision includes snapshot/evidence and ALLOW/DENY/NEEDS_INFORMATION/CONFLICT.
- [x] Add boundary-time, custom-product, missing-fact, conflicting-rule and mixed-order tests; evidence-change confirmation tests.
- [x] Implement immutable validated policy snapshots and matching source digests; read-only eligibility tool and policy gate at proposal/execution.
- [x] Keep explicit opt-in policy flag, offline correctness and immutable old evidence.

### Task 6: Scoped retrieval and citations

Files: `app/schemas/retrieval.py`, `app/retrieval/bm25.py`, `app/retrieval/vector.py`, `app/retrieval/hybrid.py`, `app/retrieval/cache.py`, `app/retrieval/ingest.py`, `tests/unit/test_policy_retrieval.py`.
Interfaces: `RetrievalScope(snapshot_id, document_ids, policy_versions)` propagated into both candidate generators and cache keys.
- [x] Add tests for invalid-candidate crowding, scope/cache separation, Qdrant filter request and evidence digests.
- [x] Implement optional scope with backward-compatible defaults, versions/digests in citations and safe policy publication/loading command.
- [x] Verify unscoped existing retrieval behavior and offline demo.

### Task 7: Verification, demo and interview guide

Files: `scripts/demo_reliable_actions.py`, `docs/reliable-aftersales-agent.md`, `app/observability/metrics.py`, README/limitations pointers, tests above.
- [x] Add reusable fault-demo CLI showing duplicate confirmation, changed order and lost response recovery; no fake measured claims.
- [x] Add low-cardinality action metrics; document migration, policy switch, gateway boundaries and demo identity limitations.
- [x] Run targeted tests, full pytest, Ruff and mypy; distinguish existing failures from introduced failures.
- [x] Dispatch fresh final reviewer, fix important findings with regression tests, rerun affected checks.

## Execution ledger

- Planning: complete. Native execution authorized by the user's confirmation; no additional approval round.
- Baseline: Python 3.11 available; pytest/Ruff/mypy not installed in host interpreter. Creating local .venv.
- Existing changes: 18 tracked files, mostly learning comments. Existing trailing whitespace in retrieval node is recorded, not silently overwritten.
- Git writes: no commits or branch changes; `.git` is read-only in the supplied permission profile.
- Environment: local .venv installed with project [dev] dependencies after approved network escalation. Async pytest requires escalation because Windows sandbox blocks socketpair; no change to the test event-loop policy to work around this.
- Baseline: 163 original tests passed before integration changes (one upstream LangGraph pending-deprecation warning).
- Tasks 1–6: implemented; latest full regression 201 passed, 1 optional PostgreSQL test skipped because no test URL was supplied. SQLite sync/async migration upgrade/downgrade verified.
- Task 7: one-command demo verified all four scenarios; independent final review in progress.
- Ruling: retain existing checkout and perform no commits, preserving the original dirty worktree. Source formatting is limited to changed/new feature files; pre-existing learning-comment formatting outside the feature remains.
- Ruling: READY is an audited intermediate transition inside the confirmation transaction; EXECUTING is committed before remote I/O. No separate dispatch queue is introduced, so there is no crash window with a stranded durable READY operation.
- Ruling: no online generation benchmark is run and no new RAG quality improvement is claimed. This delivery validates deterministic policy decisions and action reliability with fixed regression cases and real temporary databases; original model evaluation results remain unchanged.
- Red/green defects caught: policy no longer applicable must request reconfirmation; recovery projection must use frozen amount; SQLite connections must be explicitly closed on Windows; recovery cannot resend after local facts change. Regression tests added and fixes implemented.
- Final independent review: one Important (cross-product policy evidence filtering) and one Minor (read-only refund/return ambiguity); both reproduced, regression-tested, fixed and independently rechecked as resolved. Exact evidence triples now flow through memory/BM25/Qdrant/cache; read-only eligibility uses the same business preconditions.
- Formatting: whole-repository format check still lists ten original learning-comment files. Feature file formatting and full Ruff lint pass; original comment formatting is preserved, with file names recorded in the run guide.

- Final verification (2026-11-25): 208 passed, 1 optional PostgreSQL test skipped; coverage 76.80% (CI threshold 70%). Ruff lint, mypy (133 source files), compileall and pip check pass. Feature formatting passes; ten original comment-format differences remain as documented. One upstream LangGraph pending-deprecation warning. Final four-scenario demo passes.
- All tasks complete within the recorded scope/rulings. Independent reviewer rechecked the two reported findings as resolved; final chat refund E2E also verifies the skill exposes the refund tool. No commits, merge, deployment or developer database migration performed.
