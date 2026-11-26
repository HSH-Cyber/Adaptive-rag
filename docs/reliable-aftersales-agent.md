# 可靠售后 Agent：运行、故障演示与面试讲解

本次二次开发围绕两个问题：业务操作怎样可靠执行；订单应该适用哪一版政策。
取消、退款申请、退货申请由数据库事务和后端规则控制，模型负责理解与规划。
远程连接使用独立持久化的演示网关，没有调用真实支付或物流系统。

## 一条命令看完整演示

在项目根目录的 PowerShell 中执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_reliable_actions
```

不需要模型、API Key、Redis、Qdrant。脚本使用临时文件数据库，展示四种情况：

1. 六个并发确认请求，只产生一条退款申请，并返回同一个回执。
2. 用户确认取消前订单已发货，原操作进入 REQUIRES_RECONFIRMATION。
3. 独立网关已成功但响应丢失，先得到 UNKNOWN，重建服务后核对回执恢复为 SUCCEEDED，远程业务效果仍为一条。
4. 相同签收时间的订单，因为购买时间不同分别适用 v1 的 7 天和 v2 的 15 天演示政策。

每种场景输出真实的 passed、状态与业务记录数；失败时脚本返回非零退出码。
若要保留数据库和报告，使用一个新的空目录：

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_reliable_actions --directory data/local/interview-demo-01
```

再次演示可省略目录参数，或换一个空目录；脚本不会覆盖已有文件。
脚本中的响应丢失是明确的故障注入，不是一次真实网络故障实测。

## 启动 API 演示

以下设置只作用于当前 PowerShell 会话，不修改 `.env`。使用独立演示数据库，不覆盖你已有的业务库。

```powershell
$env:APP_ENV = "testing"
$env:DATABASE_URL = "sqlite+aiosqlite:///./data/local/interview.db"
$env:DEEPSEEK_API_KEY = ""
$env:EMBEDDING_API_KEY = ""
$env:REDIS_URL = ""
$env:QDRANT_URL = ""
$env:POLICY_ENABLED = "true"
$env:ACTION_GATEWAY = "local"
.\.venv\Scripts\python.exe -m scripts.migrate
.\.venv\Scripts\python.exe -m scripts.seed_demo
.\.venv\Scripts\python.exe -m uvicorn app.asgi:app --port 8002
```

APP_ENV=testing 让本地模型/解析器保持离线降级；空模型 API Key 防止加载开发者配置中的远程模型。
此配置是本地演示模式，不是生产环境的权限配置。

沿用现有前端的确认按钮，也可以访问 `/docs` 使用接口：

- `POST /api/v1/chat`，例如 `{"message":"取消订单 ORD10003","user_id":"u_demo"}`，获得 session_id 与 pending_action.action_id。
- `POST /api/v1/actions/{action_id}/confirm`，请求体 `{"user_id":"u_demo","session_id":"上一步的会话编号"}`。
- `GET /api/v1/actions/{action_id}?user_id=u_demo&session_id=...` 查询已提交状态和原始回执。
- 也可以继续 `POST /api/v1/chat`，携带原 session_id、user_id、confirm_action_id 和 message="确认"。
- `POST /api/v1/chat/stream` 的最终 answer 事件同样含有 action_result。

确认请求不会重新让模型决定执行什么；工具名、参数和事实来自已入库的操作记录。
退款/退货成功仅表示申请受理，不能把 SUCCEEDED 解读成资金到账。

## 状态和恢复

PENDING_CONFIRMATION 经合法确认后进入 READY、EXECUTING，最终为 SUCCEEDED 或明确的 FAILED。
待确认操作默认有效 15 分钟；已成功回执即使超过确认期限也可以重放。
订单或政策依据改变时旧操作进入 REQUIRES_RECONFIRMATION，需要重新发起一个新操作。
UNKNOWN 表示业务结果不确定，不表示执行失败；仍占用订单，不能靠创建新 action_id 绕过。

默认 ACTION_GATEWAY=local，所有业务效果与成功回执同事务提交。
设为 demo 时，独立网关数据库由 ACTION_GATEWAY_DB 指定（默认 data/local/demo_gateway.sqlite3）。
外部提交之前持久化 EXECUTING，网络调用期间不持有订单数据库锁。

恢复入口：

```powershell
$env:ACTION_GATEWAY = "demo"
.\.venv\Scripts\python.exe -m scripts.recover_actions --limit 20
```

或者调用 `POST /api/v1/admin/actions/recover?limit=20`，该接口使用现有管理员权限校验。
生产模式无管理员令牌时接口关闭；配置令牌后通过 x-admin-token 提交。
恢复命令只加载数据库、网关和可选政策，不加载模型或检索器。

恢复先查询原稳定 key 的回执，已成功则保存原回执；查不到且上游明确支持可靠查询与幂等提交时，重新校验本地前置条件后才可使用原 key 重发。
租约过期不能证明上游没执行。查询不可用，或恢复时订单/政策条件变化，继续保留 UNKNOWN 和订单占用。
UNKNOWN 不会被后台无限自动重试写入；恢复为显式、单批至多 100 条的操作。

## 数据库与并发保证

核心代码入口：

- `app/services/action_service.py`：提案、确认、短事务、回执与恢复。
- `app/services/action_rules.py`：业务矩阵、参数规范化与订单快照。
- `app/repositories/models.py`：operations、operation_events、order_reservations。
- `app/providers/business_gateway.py`：稳定 key、独立网关账本及上游订单快照检查。
- `app/services/chat_service.py`：历史准备、工具读取、操作提交、聊天保存分离。
- `alembic/versions/0003_actions.py`：新增表迁移；Alembic 环境支持同步 SQLite 与异步 SQLite/PostgreSQL URL。

本地确认先锁操作记录，再锁订单，在锁内读取最新状态和已有售后记录。
同一操作去重与同一订单业务互斥分别处理；不能只去重 HTTP 请求。
SQLite 使用先写再读避免延迟事务升级竞争；PostgreSQL 的行更新取得订单行锁。
业务效果、SUCCEEDED 回执、审计事件一起提交。中途异常整体回滚，然后用新短事务记录失败。
聊天保存或模型失败不会回滚已经提交的业务效果，客户端仍可以查询原操作。
远程成功后的本地投影使用确认时冻结的金额，避免恢复时误读变更后的订单金额。

首版整单业务矩阵：

- 取消：created/paid，且无阻塞售后记录。
- 退款申请：paid/shipped/delivered，且无阻塞退款或退货记录；成功后状态为 refunding。
- 退货申请：shipped/delivered，且无阻塞退款或退货记录；不自动退款。
- rejected/cancelled/failed 的售后记录不阻塞重新申请；其他已知及未知状态均保守阻塞。
- 不同时开启退货和退款两个独立申请；未实现“退货完成后自动退款”的完整审核流程。

远程执行的 attempt 与条件更新可阻止过时工作者覆盖新的操作状态；订单占用在结果未知时不能释放。
这不是全局 exactly-once。外部服务必须提供业务幂等与可靠查询，外部状态变化需要上游原子校验。
演示网关仅模拟这些契约，不能证明真实支付系统也满足契约。

旧库升级前先备份并确认 Alembic 当前版本；`scripts.migrate` 不会自动 stamp 已有的 create_all 数据库。
首次使用未受 Alembic 管理的旧库时需要人工核对现有结构与迁移版本；不要直接运行初始化迁移覆盖已有表。
本次未自动迁移你的开发者数据库。

## 政策判断与检索

POLICY_ENABLED 默认 false，以保留原演示行为；开启后所有受支持售后写入口均调用同一政策服务。
`app/policies/demo.json` 与 sources/ 是明确的演示业务配置，不是真实商家政策或法律结论。
支付时间（缺失时创建时间）决定适用版本，签收时间决定申请期限，生效区间和申请期限均采用明确的左闭右开规则。
定制例外、缺失信息和同优先级冲突分别得到 DENY、NEEDS_INFORMATION、CONFLICT，只有 ALLOW 才能创建售后确认提案。

`check_aftersale_eligibility` 是只读工具，通过 tool_name 区分退款与退货，返回业务前置条件、规则判断与已验证源文档。
采购时间、签收时间、SKU、定制属性来自订单；品类来自商品仓储，不能由用户一句话覆盖。
模型不会把政策拒绝改成允许，确定性政策回答携带版本与内容摘要。
提案冻结政策证据；执行时重新计算，只有影响该订单的依据变化才使旧确认失效。

检索通过 RetrievalScope 传播 snapshot_id 和准确的文档、版本、内容摘要证据组合：

- BM25 和内存向量在截取 top_k 前筛选；Qdrant 请求携带对应 payload filter。
- 缓存键包含知识快照及适用范围；空文档范围不退回全库检索。
- 混合订单的 (A,v1) 与 (B,v2) 不会交叉放行 (A,v2)/(B,v1)，同文档同版本也不能匹配另一个内容摘要。
- chunk_id 包含快照，防止新发布覆盖旧快照中的向量标识。
- 引用包含 policy_version 与 source_digest；政策源文件摘要不匹配时拒绝加载。

发布一个新快照：

```powershell
.\.venv\Scripts\python.exe -m scripts.publish_policy --destination data/local/policy-release-01
$env:POLICY_MANIFEST = "data/local/policy-release-01/manifest.json"
```

重新启动 API 后使用新快照；每个实例内的政策对象保持固定。启动还会原子归档完整快照到 POLICY_SNAPSHOT_DIR，供审计追溯。
本轮没有实现全知识库的跨集群热发布平台，普通知识问答仍保留原检索路径。

## 验证与面试材料

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts alembic
.\.venv\Scripts\python.exe -m mypy
```

Windows 沙箱若限制本机 socketpair，异步测试需要允许在沙箱外运行；这不代表测试会调用真实业务接口。
PostgreSQL 可选测试使用 ACTION_TEST_POSTGRES_URL（postgresql+asyncpg），只在专用测试库建立随机 action_test_ 前缀 schema，最后删除该 schema。
未配置该地址时明确 skip，不能把 SQLite 通过描述为 PostgreSQL 并发已实测。

新增指标：cs_action_confirmations_total（含成功回执重放）、cs_action_confirmation_seconds、cs_action_recoveries_total。
不以 user_id/action_id 作为 Prometheus 标签。

面试可以依次演示：六次并发一条效果 → 确认前发货被拦 → 响应丢失保留 UNKNOWN → 重启后查原回执 → 新旧政策适用差异。
重点解释：为什么“先查幂等键再写”不够；为什么超时不是失败；为什么聊天事务应与业务事务分开；为什么相关性不能代替政策适用性。
使用真实测试输出说明结果，不把原项目的 RAG 评测数字当成本次改造收益。

原工作区学习注释导致的全库格式检查差异保留在以下文件：agents/graph.py、nodes/intent_overlay.py、nodes/retrieval.py、nodes/router.py、api/routes/chat.py、asgi.py、core/enums.py、providers/embeddings.py、retrieval/chunking.py、retrieval/ingest.py（均位于 app/）。新增功能文件的格式检查及全库 Ruff lint、mypy 已通过；没有为消除这些既有差异而重写学习注释。原 retrieval 节点注释的尾部空白也保留。

当前身份仍来自演示 user_id，新增了会话与操作所属校验，但没有完成生产身份认证。真实部署需要可信认证层绑定用户身份。

## 本地验证记录（2026-11-25）

Windows / Python 3.11：208 passed、1 skipped（未提供专用 PostgreSQL 地址），app + eval 语句覆盖率 76.80%，超过 CI 的 70% 门槛。Ruff lint、mypy（133 个源文件）、compileall、pip check 通过。一键演示四个场景全部通过。独立审阅发现的证据交叉匹配和退款/退货类型歧义已复现、修复并复核。保留一条上游 LangGraph pending-deprecation warning。全库格式检查的既有差异见上文。以上不是在线模型问答质量或真实支付系统的实验结果。
