# 使用手册

面向本项目的使用者与二次开发者，覆盖从环境准备、安装配置到 CLI / Web 双入口使用、RAG 知识库接入、子 Agent 分流、测试门禁与常见问题排查的完整流程。内部实现原理与设计决策不在本文范围，见 [architecture.md](./architecture.md)。

> 更新：2026-10-30（配置体系迁移至 `config/agent.yaml` 后）｜ 快速概览见仓库根 [README](../README.md)

---

## 目录

1. [这个项目能做什么](#1-这个项目能做什么)
2. [环境要求](#2-环境要求)
3. [首次安装](#3-首次安装)
4. [使用 CLI 终端聊天](#4-使用-cli-终端聊天)
5. [使用 Web 界面](#5-使用-web-界面)
6. [接入 RAG 知识库](#6-接入-rag-知识库)
7. [子 Agent 分流与 fan_out 并行](#7-子-agent-分流与-fan_out-并行)
8. [配置参考](#8-配置参考)
9. [HTTP API 速览](#9-http-api-速览)
10. [测试与质量门禁](#10-测试与质量门禁)
11. [常见问题](#11-常见问题)
12. [目录速查](#12-目录速查)

---

## 1. 这个项目能做什么

「阿元」是一个日常聊天 Agent，具备以下能力：

- **流式对话**：CLI 终端打字机效果，或浏览器 Web 界面 SSE 流式输出
- **工具调用**：时间、计算器、联网搜索、沙箱文件读写、知识库检索等内置工具，外加 MCP 协议接入的第三方工具
- **子 Agent 分流**：主 Agent 只做路由与汇总——调研任务委派给 research 子 Agent，沙箱写文件委派给 coding 子 Agent；多对象对比类问题经 `fan_out` 并行拆分执行（`config/agent.yaml` 一键开关，见[第 7 章](#7-子-agent-分流与-fan_out-并行)）
- **RAG 知识库**：检索命中后回答带 `[1] [2]` 引用编号，Web 端渲染可展开的引用卡片
- **三级权限审批**：`read` 档工具直接执行；`write` / `danger` 档执行前必须经你确认（CLI 终端询问，Web 弹窗阻断）
- **长期记忆**：说「请记住……」即写入记忆，跨会话可召回；长对话自动摘要压缩
- **会话持久化**：全部会话、消息、审计、记忆落在 MySQL，随时恢复历史会话

---

## 2. 环境要求

| 依赖 | 版本 / 说明 | 必需性 |
| --- | --- | --- |
| 操作系统 | Windows / Linux / macOS 均可 | — |
| Python | 3.12+ | 必需 |
| Node.js | 18+（Web 前端 Next.js 15 需要） | 仅 Web 端 |
| MySQL | 8.0（`127.0.0.1:3306` 或其他可达实例） | 必需 |
| LLM API Key | 任意 OpenAI 兼容网关：智谱 GLM / 阿里云百炼 DashScope / DeepSeek 等 | 必需 |
| Tavily API Key | 联网搜索工具用 | 可选（缺 Key 该工具自动禁用，不报错） |
| Qdrant + 混合检索服务 | RAG 检索用（外部独立部署） | 仅真实 RAG 场景 |

---

## 3. 首次安装

### 3.1 克隆并安装 Python 依赖

```bash
git clone https://github.com/Mr-Shawn17/YuanAgent.git
cd YuanAgent

python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev,tools,memory]"
```

等价于 `make install`（Makefile 中 `install` 目标）。国内网络可加清华镜像：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[dev,tools,memory]"`。

### 3.2 配置 config/agent.yaml

配置分两层：**行为策略进 `config/*.yaml`**（进版本库，可 review），**密钥走环境变量**（绝不进版本库）。

```bash
cp config/agent.yaml.example config/agent.yaml
```

编辑 `config/agent.yaml`，必改两处（其余保持默认即可）：

**① runtime 段——模型网关与密钥**（按所用服务商三选一）：

```yaml
runtime:
  # 智谱 GLM（example 模板默认）
  llm_base_url: https://open.bigmodel.cn/api/coding/paas/v4
  llm_api_key_env: GLM_API_KEY      # 密钥从该环境变量读取，绝不写进 YAML
  llm_model: glm-5.3-flash

  # 或：阿里云百炼 DashScope
  # llm_base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  # llm_model: qwen-plus
  # llm_api_key_env: DASHSCOPE_API_KEY

  # 或：DeepSeek
  # llm_base_url: https://api.deepseek.com/v1
  # llm_model: deepseek-chat
  # llm_api_key_env: DEEPSEEK_API_KEY
```

注意：`*_api_key_env` 填的是**环境变量的名字**，密钥本身通过环境变量注入（见 3.3）。留空时按 `DASHSCOPE_API_KEY` → `OPENAI_API_KEY` → `GLM_API_KEY` 顺序回退探测。

**② runtime 段——MySQL 密码**：

```yaml
runtime:
  mysql_host: 127.0.0.1
  mysql_port: 3306
  mysql_user: root
  mysql_password: "你的密码"   # 留空则回退读环境变量 MYSQL_PASSWORD
  mysql_database: agent
```

`agent.yaml` 已被 gitignore（含本地密码），不会误提交；仓库里保留 `agent.yaml.example` 模板。

### 3.3 注入密钥环境变量

```bash
# Windows PowerShell（仅当前会话生效；持久化用系统环境变量设置）
$env:GLM_API_KEY = "sk-你的密钥"

# Linux / macOS
export GLM_API_KEY=sk-你的密钥
```

变量名必须与 `llm_api_key_env` 配置的一致（`fast` / `fallback` 模型同理）。密钥与网关必须配套——拿 A 网关的 Key 请求 B 网关会 401。

### 3.4 初始化数据库

```bash
python scripts/init_mysql.py
```

等价于 `make db-init`。脚本幂等，可重复执行：

- 建库 `agent`（不存在才建，utf8mb4 字符集默认 collation）
- 触发 LangGraph checkpointer / store 自管表（6 张）的 setup
- 业务表（sessions / messages / tool_audits）由 Alembic 迁移管理

### 3.5 验证安装

```bash
python -m app.cli ping      # 发一次 1 token 请求验证模型连通
python -m app.cli config    # 显示当前生效配置（密钥已脱敏）
python -m app.cli version
```

预期结果：`ping` 输出模型响应正常；`config` 回显的 base_url / model 与你填写的一致。

### 3.6 安装前端依赖（仅 Web 端需要）

```bash
cd frontend
npm install
```

---

## 4. 使用 CLI 终端聊天

前置条件：完成第 3 节全部步骤，MySQL 在运行。

```bash
python -m app.cli chat
```

等价于 `make chat`。进入终端流式对话界面后：

1. 直接输入问题，回车发送，回答以打字机效果流式输出。
2. 工具执行过程以轨迹形式实时渲染（工具名、档位、耗时、参数、结果）。
3. 触发 `write` / `danger` 档工具时，终端会同步询问是否允许，输入确认后才执行。
4. 知识库命中时，回答末尾带 `[1] [2]` 引用编号与来源列表。

会话内可用的斜杠命令：

| 命令 | 作用 |
| --- | --- |
| `/new` | 新建一个会话 |
| `/sessions` | 列出历史会话（可恢复） |
| `/use <id前缀>` | 切换到指定会话 |
| `/model` | 显示当前模型配置 |
| `/help` | 显示帮助 |
| `/exit` | 退出 |

`Ctrl+C` 在生成过程中中断本轮，在输入过程中退出程序。退出时程序会等待在途的记忆抽取任务（最长 30 秒），属正常现象。

CLI 的其余子命令：

```bash
python -m app.cli sessions   # 列出历史会话（等价 make sessions）
```

预期结果：对话可用、审批可阻断、退出后重进能用 `/use` 恢复刚才的会话。

---

## 5. 使用 Web 界面

需要两个终端分别启动后端与前端（端口在 `config/agent.yaml` 的 `server` 段配置，默认 8000 / 3000）。

### 5.1 启动后端 API

```bash
python scripts/start_backend.py            # 默认热重载
python scripts/start_backend.py --no-reload  # 生产模式
```

等价于 `make dev`。**启动前会自动执行环境预检**（`scripts/preflight.py`）：

- 必须项（任一失败 → 红字报错并终止启动）：agent.yaml 存在、Python 解释器可用、核心依赖已安装、MySQL 可连接（真实试连）、LLM API Key 环境变量已设置
- 可选项（失败 → 黄字提示，不阻断）：各 MCP Server 真实握手（mock / rag / mcd / aihot）

排查环境问题时可单独运行 `python scripts/preflight.py --scope backend`。加 `--skip-check` 可跳过预检（不建议）。

启动日志中应看到：

- `MCP Server mock 已连接，接入 3 个工具`（echo / add / now）
- rag Server 的连接日志（若已启用且 RAG 服务在线；未启动时该 Server 连接失败会被隔离，不影响其余功能）
- `Application startup complete` + `Uvicorn running on http://127.0.0.1:8000`

### 5.2 启动前端

```bash
python scripts/start_frontend.py
# 或手动：
cd frontend && npm run dev
```

同样内置环境预检，前端额外检查：Node.js / npm 在 PATH、`frontend/node_modules` 已安装（均必须项）。

等价于 `make web`。浏览器打开 `http://localhost:3000`。

### 5.4 服务日志（文件）

前后端日志均落盘到项目根 `log/` 目录（已被 gitignore），按天一文件，仅保留近 7 天（启动时自动清理过期文件）：

| 文件 | 内容 | 采集方式 |
| --- | --- | --- |
| `log/backend-YYYY-MM-DD.log` | 后端全部运行日志（API / MCP / 审计 / 异常堆栈），与终端同链路、密钥自动脱敏 | structlog 文件 handler |
| `log/frontend-YYYY-MM-DD.log` | 前端 npm 子进程全部输出（编译状态 / 请求报错 / 堆栈），每次启动追加带时间戳分节 | 启动脚本 tee 捕获 |

可调配置（`config/agent.yaml` 的 runtime 段）：`log_dir`（默认 `log`）与 `log_retention_days`（默认 7，设 0 为永久保留）。

### 5.3 界面功能

- **左侧会话列表**：点击「＋ 新对话」开新会话；历史会话显示标题、时间与消息数，点击即恢复。
- **流式气泡**：回答逐字输出，底部显示本轮 token 用量（↑ 输入 / ↓ 输出）。
- **工具轨迹折叠面板**：每轮回答上方显示「工具轨迹（N）」，点开可见每次工具调用的档位、状态（待审批 / 完成 / 失败）、耗时、入参与结果；子 Agent 的调用带 `[subagent:research]` / `[subagent:coding]` 前缀。
- **引用卡片**：知识库检索命中后，回答下方渲染编号引用卡片，点击展开全文片段与来源。
- **审批弹窗**：触发 `write` / `danger` 档工具时弹出「需要你的确认」，展示工具名、档位与完整调用参数；弹窗期间输入框禁用（阻断生效），点「拒绝」或「允许执行」后流程才继续。
- **执行链路图**：`fan_out` 并行委派时渲染主 Agent → 并行子任务泳道 → 汇总节点的可视化卡片。

预期结果（可自测的四项验收）：流式输出无卡顿；工具轨迹可展开；引用卡片可点开看全文；发送「请记住：我的生日是 3 月 12 日。」会弹审批窗，拒绝后工具不执行、允许后轨迹显示「完成」且回复确认已记住。

---

## 6. 接入 RAG 知识库

知识库与 Agent 已解耦：真实检索能力由外部混合检索 RAG 服务提供（Qdrant 稠密 + BM25 稀疏 → RRF 融合 → rerank），本项目经 MCP over streamable-http 调用。**不需要真实知识库时可跳过本节，用 stub 模式（本地模拟数据）即可跑通全部链路。**

### 6.1 stub / mcp 两种模式

| 模式 | 说明 | 适用 |
| --- | --- | --- |
| `stub` | 本地语料模拟检索（`data/kb_stub/chunks.json`） | 开发调试、无 Qdrant 环境 |
| `mcp` | 经 MCP 调用外部真实 RAG 服务 | 生产 / 真实问答 |

两处开关：

1. **内置 stub 工具**：`config/tools.yaml` 的 `search_knowledge_base.enabled`（当前默认 `false`，因真实 RAG 已接入）
2. **MCP rag Server**：`config/mcp.yaml` 的 `rag` 条目（`url` 指向你的 RAG 服务，默认 `http://127.0.0.1:8100/mcp`；不需要时设 `enabled: false`）

MCP 模式下工具带 `rag__` 命名空间前缀：`rag__search` / `rag__list_companies` / `rag__get_chunk_detail` / `rag__health`，全 `read` 档免审批。

### 6.2 启动你自己的 RAG MCP 服务

本项目自带一个最小 RAG MCP 服务（供联调参考）：

```bash
python scripts/rag/serve_mcp.py     # 监听 http://127.0.0.1:8100/mcp
```

首次使用可灌入示例知识库：

```bash
python scripts/rag/seed_agent_knowledge.py   # 幂等：灌入年报语料到 agent_knowledge collection
```

> 生产环境建议部署独立的混合检索服务（Qdrant + embedding + reranker），并把 `config/mcp.yaml` 的 `url` 指过去。若你的 RAG 服务端是旧版 MCP SDK（协议 2024-11-05），需在 rag 条目加 `mode: legacy` 强制旧版握手。

### 6.3 跑金标准评测（可选）

RAG 服务启动后：

```bash
python -m pytest tests/eval -q -m rag_service -s
# 等价 make eval
```

20 条金标准用例，指标门槛 Recall@5 / MRR / 引用正确率。

---

## 7. 子 Agent 分流与 fan_out 并行

主 Agent 只做路由与汇总：检索调研类任务委派给 **research** 子 Agent，沙箱文件编写委派给 **coding** 子 Agent；多对象对比类问题（如「对比 A、B 两家公司 2024 年营收」）由 `fan_out` 工具并行拆分执行。对使用者的影响：

- CLI 启动头部会列出「子 Agent：research（N 工具）、coding（M 工具）」；主 Agent 工具清单相应收缩。
- 工具轨迹里子 Agent 的工具调用带 `[subagent:research]` / `[subagent:coding]` 前缀（Web 端 `tool_start` 帧含 `origin` 字段），与主 Agent 自己的调用可区分。
- **审批语义**：`delegate_to_research` 与纯只读的 `fan_out` 批次直接执行不弹窗；`delegate_to_coding` 是写档、弹一次审批窗——你批准的是「整次委派」，批准后子 Agent 内部（如连续写多个文件）不再逐条打断，但每一步仍写审计并出现在轨迹里。

开关与子 Agent 定义全在 `config/agent.yaml` 的 `routing` 段（字段说明见 `config/agent.yaml.example` 顶部注释）：

```yaml
routing:
  enabled: true          # false 时主 Agent 恢复全工具形态，无需改代码
  fan_out:
    enabled: true        # fan_out 并行委派总开关
    max_parallel: 4      # 单批子任务上限（2~10）
    read_only: true      # 安全护栏：仅只读子 Agent 可进批，免审批
```

端到端验收脚本（真实 LLM + MySQL）：

```bash
python scripts/verify_p6_subagents.py
```

三项全查：闲聊不委派、调研走 research 免审批、写文件走 coding 审批一次 + 沙箱落盘 + 审计落库。fan_out 详细说明见 [fanout-guide.md](./fanout-guide.md)。

---

## 8. 配置参考

配置分两层：密钥与环境相关进环境变量（`*_api_key_env` 指定变量名），行为策略进 `config/*.yaml`。

### 8.1 config/agent.yaml（六段）

| 段 | 管什么 |
| --- | --- |
| `persona` | 人设、说话风格、边界（名字、语言、时区等） |
| `limits` | 单轮熔断限额：`max_tool_calls_per_turn`（默认 12）/ `max_model_calls_per_turn`（默认 15） |
| `history` | 上下文压缩：摘要触发阈值（默认 12000 tokens）、保留消息数（默认 20） |
| `server` | 启动脚本读取：Python 环境根目录、后端/前端端口 |
| `runtime` | 运行时配置（原 .env 内容）：模型三档（main / fast / fallback）、MySQL、知识库、第三方 Key 环境变量名、CORS、Web 鉴权、日志目录与保留天数 |
| `routing` | 子 Agent 分流：总开关、主 Agent 保留清单、research / coding 定义、fan_out 参数 |

### 8.2 config/tools.yaml

工具白名单（`default_allow: false`，未登记一律禁用）、每个工具的权限档位、文件工具沙箱根目录（`data/workspace`，沙箱外路径一律拒绝）、KB 模式开关。

### 8.3 config/mcp.yaml

MCP Server 清单：transport（stdio / streamable_http）、权限档位继承、工具白名单、超时。**接入新 Server 只加一段配置，不改代码。**

### 8.4 密钥环境变量

| 环境变量 | 用途 |
| --- | --- |
| `GLM_API_KEY` / `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` 等 | LLM 密钥，变量名由 `*_api_key_env` 指定，留空按 `DASHSCOPE_API_KEY` → `OPENAI_API_KEY` → `GLM_API_KEY` 顺序回退 |
| `MYSQL_PASSWORD` | MySQL 密码（`mysql_password` 留空时回退读取） |
| `TAVILY_API_KEY` | 联网搜索；缺失时该工具自动禁用而非启动报错 |

### 8.5 权限档位

| 档位 | 行为 | 当前成员 |
| --- | --- | --- |
| `read` | 直接执行，仅审计 | 时间、计算器、联网搜索、目录列表、读文件、知识库检索、全部 MCP 工具、`delegate_to_research` |
| `write` | 执行前需审批 | `write_file`、`remember`、`delegate_to_coding`（批准一次即整体授权子 Agent 内部操作） |
| `danger` | 执行前需审批（更醒目的提示） | 预留档位，当前无成员 |

---

## 9. HTTP API 速览

后端（默认 8000 端口）对外提供三组端点，前端即基于它们工作：

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/v1/chat` | POST | SSE 流式对话。请求体：`message`（必填）、`session_id`（空则新建会话）、`user_id`。事件流包含 `tool_start` / `approval_required` / `approval_decided` / `tool_end` / `citation` / token 与错误事件 |
| `/v1/chat/{run_id}/approve` | POST | 审批回调。请求体：`call_id`、`decision`（`allow` / `deny`） |
| `/v1/sessions` | GET | 会话列表（thread_id、标题、消息数、更新时间） |
| `/v1/chat/completions` | POST | OpenAI 兼容端点。请求体：`model`、`messages`、`stream`（true 走 SSE 增量帧）、`session_id`、`user` |
| `/health` | GET | 健康检查（鉴权豁免） |

可选鉴权：`runtime.api_key` 留空关闭；配置后所有请求需带 `X-API-Key` 头（`/health` 豁免）。

OpenAI 兼容端点调用示例（可接任何 OpenAI SDK，把 base_url 指到本服务）：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "agent", "messages": [{"role": "user", "content": "你好"}], "stream": false}'
```

---

## 10. 测试与质量门禁

提交代码前跑一键门禁（ruff + mypy strict + 离线单测）：

```bash
make check
# 或分步：
python -m ruff check backend tests scripts
python -m mypy
python -m pytest -q
```

各测试档位：

| 命令 | 覆盖范围 | 前置条件 |
| --- | --- | --- |
| `make test` | 离线单测（FakeChatModel，排除 online / mysql / rag_service 标记） | 无 |
| `make test-online` | 触网集成测试 | 真实 API Key |
| `make test-mysql` | 审计 / 记忆落库集成测试 | MySQL 实例 |
| `make eval` | RAG 金标准评测 | RAG MCP 服务（见第 6 节） |
| `make check` | lint + typecheck + test 一键门禁 | 无 |

端到端验收脚本（真实 LLM + MySQL）：

```bash
python scripts/verify_p4_memory.py       # 记忆系统全链路
python scripts/verify_p6_subagents.py    # 子 Agent 分流
```

---

## 11. 常见问题

**`make` 命令不识别**

Makefile 只是命令清单，任何目标都能用 Python 直接执行（等价命令见上文各节）。Windows 未装 make 时按各节给出的 `python ...` 命令执行即可。

**模型请求 401**

密钥与网关不配套。检查 `llm_base_url`、`llm_api_key_env` 指向的环境变量与模型名是否成对、是否属于同一服务商。

**模型请求 404（model not found）**

模型名在所选网关上不存在。每个网关的可用模型名不同，对照服务商文档修改 `llm_model`。

**模型请求 403**

套餐到期或该模型未购买（常见于个人版套餐网关）。续费，或换用公共网关（智谱 / DashScope / DeepSeek 均可）。

**启动日志出现 rag MCP 连接失败**

RAG 服务未启动，或 Qdrant / embedding 服务不可用。单个 MCP Server 连接失败会被隔离，不影响 CLI / Web 的其余功能；不需要真实知识库时：

- 方式一：`config/mcp.yaml` 中 rag 条目设 `enabled: false`
- 方式二：`config/tools.yaml` 启用 stub 版 `search_knowledge_base`（`enabled: true`，`mode: stub`）

**联网搜索工具不存在**

`TAVILY_API_KEY` 未配置时该工具自动禁用（设计如此，不报错）。配上 Key 重启即恢复。

**文件工具报「路径超出沙箱」**

读写文件类工具被限制在 `data/workspace` 沙箱内，`../` 与绝对路径一律拒绝。把目标文件放进沙箱，或改 `config/tools.yaml` 的 `sandbox_root`。

**npm 命令不识别（Windows PowerShell）**

PowerShell 下用 `npm.cmd`（同理 `npx.cmd`），或在 cmd / Git Bash 中直接用 `npm`。

**前端请求后端报 CORS 错误**

`runtime.cors_origins` 默认只放行 `http://localhost:3000`。前端换端口后需同步修改并重启后端。

**改了配置不生效**

确认改的是 `config/agent.yaml`（实际生效）而不是 `agent.yaml.example`（模板）；后端需重启（热重载只覆盖代码变更，配置文件变更需手动重启）。

---

## 12. 目录速查

```
YuanAgent/
├── backend/app/          # Python 主应用（cli.py 终端入口 / main.py Web 入口 / api/ HTTP 层）
├── frontend/             # Next.js 15 Web 前端
├── config/               # agent.yaml(.example) / tools.yaml / mcp.yaml
├── scripts/              # init_mysql.py、start_backend.py、start_frontend.py、
│                         # rag/（serve_mcp.py、seed_agent_knowledge.py）、
│                         # verify_p4_memory.py、verify_p6_subagents.py
├── tests/                # unit（离线）/ integration / eval（RAG 金标准）
├── data/                 # kb_stub 语料 + workspace 沙箱
├── docs/                 # architecture.md、architecture-brief.md、design.md、
│                         # fanout-guide.md、roadmap.md、本文
├── Makefile              # 常用命令入口
└── LICENSE               # MIT
```

各模块职责、数据流水线、安全矩阵与已知差异清单，见 [architecture.md](./architecture.md)。
