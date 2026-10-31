# daily-chat-agent 未尽事项清单

> 更新：2026-10-26 ｜ 来源：`docs/architecture.md` §14「现状差异清单」+ 源码逐项实查
> 说明：本清单记录「已定义/已预留，但当前未接线或未强制执行」的演进空间，均为有意保留，**不是 bug**。
> 每条标注对应代码/配置位置，便于逐条推进。

---

## 一、官方已记录的现状差异（architecture.md §14）

| # | 现象 | 现状 | 位置 |
| --- | --- | --- | --- |
| 1 | `KB_MODE` 环境变量 | 死配置，无业务代码读取 | `backend/app/core/config.py` |
| 2 | `KB_STUB_DATA` 配置项 | 未被消费，stub 路径硬编码 | `backend/app/rag/stub_data.py` |
| 3 | `tool_budget` 字段 | 只写入 state，中间件从不读取校验 | `backend/app/agents/state.py` |
| 4 | `max_model_calls_per_turn` | 仅展示，无强制熔断 | `configs/agent.yaml` limits |
| 5 | `make up` / `make down` | 入口悬空，指向不存在的 docker-compose | `Makefile` |
| 6 | 无 `Dockerfile` / `docker-compose.yml` | 容器化尚未引入 | 仓库根 |

---

## 二、源码层补充核查出的未尽事项

### 1. 限额熔断未落地（P0）
`configs/agent.yaml` 的 `limits.max_tool_calls_per_turn: 12`、`max_model_calls_per_turn: 15` 均已定义，但 `tool_budget` 字段「只存不校验」——模型若陷入工具死循环，**目前没有结构性闸门拦住它**。这是当前最实质的功能缺口。

- 涉及：`backend/app/agents/state.py`（tool_budget）、`backend/app/middleware/tool_permission.py`（应读取校验）

### 2. `danger` 档位空占位
三档权限里 `danger` 档当前无任何工具成员，仅预留更醒目的审批提示。未来引入删除/发送/扣费等不可逆工具时需真正启用。

- 涉及：`backend/app/tools/permissions.py`

### 3. 业务表 `messages` 写入路径单一
对话正文权威副本在 LangGraph `checkpoints`，业务表 `messages` 主要靠审计触发写入，CLI 路径下无显式 `SessionRepo.add_message()`。业务表完整消息流水与 checkpoint 存在信息差。

- 涉及：`backend/app/db/session_repo.py`、`backend/app/cli.py`

### 4. 记忆生命周期管理缺失
`AsyncMyStore` 无 `adelete_namespace`，记忆只能「写 + 查 + 逐 key 删」，没有过期机制，也没有用户主动删除/清空记忆的入口，长期运行会单调膨胀。

- 涉及：`backend/app/memory/extractor.py`、`backend/app/tools/builtin/memory.py`

### 5. 多用户 / 多租户未做
`default_user_id = "me"` 全局单值，无登录、无鉴权、无用户隔离；Web 端点无认证，任何能访问 8000 端口者均可调用。这是从「个人工具」走向「可交付产品」的最大鸿沟。

- 涉及：`backend/app/core/config.py`（default_user_id）、`backend/app/api/`（无 auth）

### 6. 观测与可运维性偏弱
有 `TelemetryMiddleware`（打点延迟/token）、`tool_audit` 审计、structlog 日志，但无集中 metrics 导出（Prometheus）、无告警、无 tracing；`messages` 表的 `tokens_in/tokens_out` 字段缺少聚合看板。

- 涉及：`backend/app/middleware/telemetry.py`、`backend/app/db/models.py`

---

## 三、按优先级排序的推进建议

```
P0（功能正确性，最该补）
  ├─ 工具/模型调用次数熔断真正生效（补 tool_budget 校验）
  └─ danger 档工具落地时的审批强化（等首个危险工具进来一并做）

P1（从"能用"到"可交付"）
  ├─ Web 端鉴权 + 多用户隔离（default_user_id 是硬编码单值）
  ├─ 容器化部署（Dockerfile + docker-compose 编排 MySQL/Qdrant/RAG/embedding/reranker）
  └─ messages 业务表完整写入路径（闭合与 checkpoint 的信息差）

P2（体验与健壮性）
  ├─ 记忆生命周期管理（过期/删除/清空入口）
  ├─ 清理死配置（KB_MODE、KB_STUB_DATA，或真正接线）
  └─ 观测升级（metrics 导出 / 成本聚合看板 / tracing）

（P3 已裁撤：原"并行/协作编排"中的并行委派已由 fan_out 落地（2026-10-30），
 delegate + fan_out 已满足本项目个人聊天场景；子 Agent 间协作编排——结果互喂 /
 反馈循环 / 共享状态——需求不成立，后续有真实场景再做，不在计划内。）
```

---

## 四、RAG MCP 集成状态（2026-10-26 已完成）

| 项 | 状态 |
| --- | --- |
| 8100 端口服务 | ✅ 实为 `annual-report-rag`（60 份年报 / 20 家公司），非仓库 `serve_mcp.py` |
| 协议版本 | 旧 SDK（`2024-11-05`）vs 客户端 `mcp 2.2.0`（`2026-07-28`），已用 `mode: legacy` 降级兼容 |
| `configs/mcp.yaml` rag 段 | ✅ `mode: legacy`，`allow_tools: [search, list_companies, get_chunk_detail, health]` |
| `configs/tools.yaml` | ✅ 内置 stub `search_knowledge_base` 已 `enabled: false`（避免与真实 RAG 语义重叠） |
| 工具命名空间 | ✅ `rag__search` / `rag__list_companies` / `rag__get_chunk_detail` / `rag__health`，全 `read` 免审批 |
| 真实检索验证 | ✅ `search`「平安银行2023年末不良贷款率」top-1 命中 000001_2023 年报 |
| 遗留 | ⚠️ 等 `annual-report-rag` 服务端 SDK 升级后，应把 `mode` 改回 `auto` |

---

## 五、限额熔断落地（2026-10-26 已完成）

| 项 | 状态 |
| --- | --- |
| 工具调用熔断 | ✅ 复用官方 `ToolCallLimitMiddleware(run_limit=max_tool_calls_per_turn, exit_behavior="end")` |
| 模型调用熔断 | ✅ 复用官方 `ModelCallLimitMiddleware(run_limit=max_model_calls_per_turn, exit_behavior="end")` |
| 装配位置 | ✅ `backend/app/api/runtime.py`、`backend/app/cli.py`，置于中间件列表最外层 |
| 死字段清理 | ✅ `tool_budget` 从 `AgentState` 与 `initial_state_fields` 移除（3 处调用方同步清理） |
| 测试 | ✅ `tests/unit/test_limits.py`（7 用例），离线全量 335 passed |

> 原 §一 第 3、4 项、§二 第 1 项（限额熔断未落地）已闭环。

---

## 六、P1 三项落地（2026-10-26 已完成）

| P1 项 | 状态 | 说明 |
| --- | --- | --- |
| Web 端轻量 API Key 鉴权 | ✅ | `API_KEY`（.env）留空关闭；配了则要求 `X-API-Key` 头，health 豁免 |
| messages 业务表完整写入 | ✅ | 轮次结束 diff 新增 user/assistant 消息写库（Web+CLI 两端），tokens 记最后一条 assistant |
| 容器化部署 | ✅ | `docker-compose.yml` 编排 backend+frontend+MySQL；RAG 作外部依赖经 host.docker.internal 访问 |

**鉴权细节**：后端 `app/api/auth.py` 全局中间件（`/health` 豁免），常量时间比较；
前端 `api.ts` 统一注入 `X-API-Key`（密钥走 `NEXT_PUBLIC_API_KEY`）。

**messages 同步细节**：`app/db/message_sync.py` 提供 `diff_new_rows` + `persist_new_messages`；
历史回放仍读 checkpoint（未改数据源），messages 表定位为「列表/检索/统计」冗余副本。

**容器化细节**：`backend/Dockerfile`（python:3.12-slim）+ `frontend/Dockerfile`（node:20-alpine，
Next standalone 输出）+ `docker-compose.yml`；`make up/down` 悬空入口已修复（指向真实 compose 文件）。
RAG 服务 `annual-report-rag` 源码不在本仓库，故不在编排内，经 `RAG_MCP_URL` 外部指向。
