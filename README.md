# YuanAgent（阿元）

> 以日常聊天为主场景的个人 AI Agent —— LangChain/LangGraph 全家桶实践：工具调用 + RAG 知识库 + MCP 生态 + 长期记忆 + 子 Agent 分流，并给每一次工具调用套上「三档权限 + 人工审批 + 全量审计」的安全外壳。

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python\&logoColor=white)

![LangChain](https://img.shields.io/badge/LangChain-1.4+-1C3C3C?logo=langchain\&logoColor=white)

![LangGraph](https://img.shields.io/badge/LangGraph-1.0+-1C3C3C)

![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi\&logoColor=white)

![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=next.js\&logoColor=white)

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

---

## 目录

- [项目亮点](#项目亮点)
- [它能做什么](#它能做什么)
- [总体架构](#总体架构)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [本地执行器与工作目录（网页操作本机文件）](#本地执行器与工作目录网页操作本机文件)
- [配置说明](#配置说明)
- [HTTP API](#http-api)
- [测试与质量门禁](#测试与质量门禁)
- [目录结构](#目录结构)
- [深入文档](#深入文档)
- [Roadmap](#roadmap)
- [常见问题](#常见问题)
- [License](#license)

---

## 项目亮点

| 亮点                       | 说明                                                                                                                      |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| 🔐 **三档权限 + 人工审批**       | 工具按 `read / write / danger` 分档：`write` 及以上执行前必须经用户确认（CLI 终端询问 / Web 弹窗阻断），所有调用无条件写审计表，模型无法静默「动手」                        |
| 🧩 **MCP 生态即插即用**        | 任意第三方 MCP Server 只需在 `config/mcp.yaml` 加一段配置即可接入（stdio / streamable-http 双传输），不改一行代码                                    |
| 🤖 **子 Agent 分流 + 并行委派** | 主 Agent 只做路由与汇总：调研任务委派 `research`、写文件委派 `coding`；多对象对比类问题自动 `fan_out` 并行拆分执行                                            |
| 🧰 **技能 Skills**          | 一目录一技能（SKILL.md 指南 + 可选脚本）：清单渐进式披露不撑上下文，脚本执行走 danger 档强制审批——加目录即生效，CLI `/skills` 可查                       |
| 📚 **RAG 与 Agent 解耦**    | 真实检索由独立 RAG 服务经 MCP 提供（Qdrant 混合检索），主项目只认一个 10 字段的 `KnowledgeChunk` 契约，无 Qdrant 也能用 stub 模式跑通全链路                        |
| 🧠 **长期记忆**              | 说「请记住……」即写入记忆，跨会话可召回；长对话自动摘要压缩；记忆抽取放后台不阻塞回复                                                                             |
| 💬 **双入口 + OpenAI 兼容**   | CLI 终端（Typer + Rich 打字机）与 Web 界面（SSE 流式 / 工具轨迹 / 引用卡片 / 审批弹窗 / 执行链路图）；另暴露 `/v1/chat/completions` 兼容端点，任何 OpenAI SDK 可直连 |
| 💻 **本地执行器（网页操作本机文件）** | 双击本机 `YuanRunner.exe` 后台进程，Web 端 `read_file` / `write_file` / `list_directory` 经 WSS 转发到用户电脑执行，文件真正落在本机磁盘而非服务器沙箱；设备令牌鉴权 + 断线重连 + 单实例锁 |

## 它能做什么

- **流式对话**：CLI 打字机效果，或 Web 界面 SSE 逐字输出，底部显示 token 用量
- **工具调用**：时间、计算器（SymPy 精确计算）、联网搜索（Tavily）、沙箱文件读写、知识库检索等内置工具，外加 MCP 协议第三方工具
- **技能（Skills）**：`skills/` 目录放 SKILL.md 即可教 Agent 按固定流程做事（渐进式披露）；技能可带脚本，经 danger 档审批后执行
- **RAG 知识库问答**：检索命中后回答带 `[1] [2]` 引用编号，Web 端渲染可展开的引用卡片
- **长期记忆**：跨会话记住你的偏好与事实（如生日），自动抽取 + 合并
- **会话持久化**：全部会话、消息、审计、记忆落在 MySQL，随时恢复历史会话
- **限额熔断**：单轮工具调用 / 模型调用次数超限自动熔断，防工具死循环

## 总体架构

```
CLI（Typer + Rich） ─┐
                     ├─→ Agent 主进程（LangGraph create_agent + 8 层中间件栈）
Web 前端（Next.js 15）─┘         │
   ↑ SSE                        │  中间件：限额熔断 → 工具权限审批 → 人设强化
   │                            │          → 记忆召回 → 遥测打点 → 防注入 → 摘要压缩
FastAPI 后端 (:8000) ←──────────┤
                                │
        ┌───────────────────────┼───────────────────────────┐
        ▼                       ▼                           ▼
  MySQL 8（全量持久化）    MCP Server 集群             外部 API
  checkpoint / store      ├─ mock（stdio，链路自检）   OpenAI 兼容 LLM
  sessions / messages     └─ rag（streamable-http）    （GLM / Qwen / DeepSeek）
  tool_audits                 → 独立 RAG 服务            Tavily 搜索
                              → Qdrant + BM25 + RRF
                              → rerank
```

**子 Agent 分流**（主 Agent 只保留路由与汇总）：

```
用户问题 → 主 Agent（路由）
            ├─ 闲聊/时间/记忆 ──→ 自己处理
            ├─ 检索调研类 ──────→ delegate_to_research（只读，免审批）
            ├─ 沙箱写文件 ──────→ delegate_to_coding（写档，整体审批一次）
            └─ 多对象对比 ──────→ fan_out 并行委派（默认上限 4 路，纯只读免审批）
```

## 技术栈

| 层         | 选型                                                                                  |
| --------- | ----------------------------------------------------------------------------------- |
| 编排        | LangChain ≥1.4（mcp extra）+ LangGraph ≥1.0（`create_agent` + middleware）              |
| LLM 接入    | `langchain-openai` 兼容协议，主模型 + fast 模型双档，支持任意 OpenAI 兼容网关（GLM / Qwen / DeepSeek…）    |
| 工具生态      | 内置工具（Typer 风格工厂）+ MCP 客户端（`fastmcp` ≥4.0，stdio / streamable-http）                   |
| 记忆        | `langmem`（记忆抽取）+ LangGraph Store（MySQL 落库）+ SummarizationMiddleware                 |
| Web 后端    | FastAPI + uvicorn，SSE 流式，OpenAI 兼容端点，可选 API Key 鉴权                                  |
| Web 前端    | Next.js 15（React 19 / App Router / Tailwind 4）                                      |
| 持久化       | MySQL 8（LangGraph checkpoint/store + 业务表，SQLAlchemy 2.0 async + asyncmy，Alembic 迁移） |
| RAG（外部进程） | Qdrant 稠密 + BM25 稀疏 → RRF 融合 → rerank，经 MCP over HTTP 对接                            |
| 工程质量      | ruff + mypy strict + pytest（online / mysql / rag_service 分档标记）                      |

## 快速开始

详细步骤（含 RAG 接入、子 Agent 配置、常见问题）见 **[docs/usage.md](./docs/usage.md)**，以下为最小路径。

### 1. 环境要求

| 依赖             | 版本                                          | 必需性               |
| -------------- | ------------------------------------------- | ----------------- |
| Python         | 3.12+                                       | 必需                |
| MySQL          | 8.0                                         | 必需                |
| Node.js        | 18+                                         | 仅 Web 端           |
| LLM API Key    | 任意 OpenAI 兼容网关（GLM / DashScope / DeepSeek…） | 必需                |
| Tavily API Key | 联网搜索用                                       | 可选（缺 Key 该工具自动禁用） |

### 2. 安装依赖

```bash
git clone https://github.com/Mr-Shawn17/YuanAgent.git
cd YuanAgent

python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev,tools,memory]"
```

### 3. 配置

```bash
cp config/agent.yaml.example config/agent.yaml
```

编辑 `config/agent.yaml`，改两处：

```yaml
runtime:
  # ① 填你的 LLM 网关（示例为智谱 GLM；也可换 DashScope / DeepSeek）
  llm_base_url: https://open.bigmodel.cn/api/coding/paas/v4
  llm_api_key_env: GLM_API_KEY      # ← 密钥从该环境变量读取，绝不写进 YAML
  llm_model: glm-5.3-flash

  # ② 填本机 MySQL root 密码（也可留空，回退读环境变量 MYSQL_PASSWORD）
  mysql_password: "你的密码"
```

然后设置密钥环境变量：

```bash
# Windows PowerShell
$env:GLM_API_KEY = "sk-你的密钥"
# Linux / macOS
export GLM_API_KEY=sk-你的密钥
```

### 4. 初始化数据库

```bash
python scripts/init_mysql.py    # 幂等：建库 agent + LangGraph checkpoint/store 自管表，可重复执行
```

### 5. 验证 & 开聊

```bash
python -m app.cli ping     # 发一次 1 token 请求验证模型连通
python -m app.cli config   # 查看当前生效配置（密钥已脱敏）
python -m app.cli chat     # 进入终端聊天
```

CLI 会话内斜杠命令：`/new` 新建会话 ｜ `/sessions` 列出历史 ｜ `/use <id前缀>` 切换会话 ｜ `/model` 模型配置 ｜ `/skills` 已加载技能 ｜ `/exit` 退出。

想试技能：对阿元说一句帮我写本周周报，它会先 `load_skill` 读 `skills/weekly-report/` 的指南，再按流程收集内容；跑脚本时终端会先问一次是否允许。往 `skills/` 放新目录、重启后自动生效，完整说明见 [docs/usage.md §8](./docs/usage.md#8-技能-skills教-agent-按你的流程做事)。

### 6. 启动 Web 界面（可选）

```bash
# 终端 1：后端（默认 :8000，端口在 config/agent.yaml 的 server 段可改）
python scripts/start_backend.py

# 终端 2：前端（默认 :3000）
cd frontend && npm install && npm run dev
```

浏览器打开 <http://localhost:3000>：会话列表、流式气泡、工具轨迹折叠面板、引用卡片、审批弹窗、fan_out 执行链路图。

## 本地执行器与工作目录（网页操作本机文件）

> 目标：在浏览器里操作你本机电脑上的文件。文件工具默认 `mode: local`，把读写转发到运行在用户电脑上的无窗口后台进程 **YuanRunner**，文件真正落在本机磁盘，而不是服务器沙箱。完整方案见 [docs/local-runner.md](./docs/local-runner.md) 与 [docs/workspace.md](./docs/workspace.md)。

### 一句话架构

```
网页 :3000 (Next rewrites /v1/*) → 后端 :8000 (FastAPI)
  → 文件工具 mode=local → LocalTaskHub → /ws/runner (WSS)
  → 本机 YuanRunner 进程 → 按绑定的工作目录执行 → 回传结果
```

### 启动三步（本机闭环）

```bash
# ① 后端（:8000，含 /ws/runner 与设备管理）
python scripts/start_backend.py

# ② 前端（:3000，Next rewrites 代理 /v1/* 到后端）
cd frontend && npm run dev

# ③ 本机执行器（无窗口后台进程，保持 WSS 长连接）
python -m runner run          # 或双击打包后的 dist/YuanRunner/YuanRunner.exe
```

### 首次配置执行器（只需一次）

```bash
# 1) 生成示例配置 %LOCALAPPDATA%\YuanAgent\runner.yaml
python -m runner init-config
#    按需改 server_url（默认 ws://127.0.0.1:8000/ws/runner）与 workspace

# 2) 中心签发设备令牌（令牌只显示一次，立即复制保存）
python -m app.cli device create --name my-laptop

# 3) 令牌写入本机凭据库（keyring）
python -m runner set-token yrt_xxx...

# 4) 启动执行器
python -m runner run
```

执行器状态自检：`python -m runner status`（看版本 / 配置 / 服务地址 / 工作目录 / 令牌来源）。

### 网页上选择工作目录

浏览器打开 http://localhost:3000，顶栏右侧「📁 选择工作目录」按钮：

- **下拉**列出执行器上报的常用目录（主目录 / 桌面 / 文档 / 下载等）
- **手填**任意绝对路径（执行器校验存在后加入可用列表）
- 选中即绑定到**当前会话**并持久化，不同会话可各绑一个目录、并行多开、动态热切换

执行器离线时仍可聊天，文件操作返回 `[local_runner_offline]` 提示文本，不会中断对话。绑定接口为 `PUT /v1/sessions/{id}/workspace`，会话列表 `GET /v1/sessions` 返回各自绑定的 `workspace`。

### 安全模型（非容器级隔离）

- **设备令牌鉴权**：`secrets.token_urlsafe(32)`，库内只存 SHA-256，可吊销（`device revoke <id>`）
- **传输**：WSS 长连接，执行器主动外连（无需在用户电脑开端口）
- **路径沙箱**：执行器侧对每个 workspace 做 `is_relative_to` 越界检查，`../` 与绝对路径一律拒绝
- **工具白名单**：执行器侧硬编码只认 `read_file` / `write_file` / `list_directory` 三个工具
- **审批 + 审计**：write 档仍在中心服务弹审批窗，批准后下发；`tool_audit` 全量落库
- **单实例锁**：重复双击执行器直接退出新进程

⚠️ 这是「白名单 + 路径约束 + 人工审批」的软件层防护，不是操作系统级沙箱；执行器以当前登录用户普通权限运行，文件权限即该用户账户权限。

## 配置说明

配置分两层：**行为策略进 `config/*.yaml`（进版本库）**，**密钥走环境变量（绝不进版本库）**。`config/agent.yaml` 含真实密码，已被 gitignore；模板见 `config/agent.yaml.example`。

| 文件                  | 管什么                                                                                                                                                |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config/agent.yaml` | 七段：`persona`（人设）/ `limits`（熔断限额）/ `history`（摘要阈值）/ `server`（端口与 Python 环境）/ `runtime`（模型、MySQL、密钥环境变量名等，原 .env 内容）/ `routing`（子 Agent 分流与 fan_out）/ `skills`（技能目录、白名单、脚本执行约束） |
| `config/tools.yaml` | 工具白名单（默认全禁用）、每个工具的权限档位、文件工具沙箱根目录、KB stub/mcp 开关                                                                                                    |
| `config/mcp.yaml`   | MCP Server 清单：transport、权限档位继承、工具白名单、超时；接新 Server 只加一段配置                                                                                           |

**权限档位**（安全模型核心）：

| 档位       | 行为            | 成员示例                                                    |
| -------- | ------------- | ------------------------------------------------------- |
| `read`   | 直接执行，仅审计      | 时间、计算器、联网搜索、读文件、知识库检索、`load_skill`、全部 MCP 工具、`delegate_to_research`  |
| `write`  | 执行前需审批        | `write_file`、`remember`、`delegate_to_coding`（批准一次即整体授权） |
| `danger` | 执行前需审批（更醒目提示） | `execute_skill_script`（技能脚本执行）                      |

## HTTP API

后端默认 `:8000`，前端即基于这些端点工作：

| 端点                          | 方法   | 说明                                                                                   |
| --------------------------- | ---- | ------------------------------------------------------------------------------------ |
| `/v1/chat`                  | POST | SSE 流式对话；事件流含 `tool_start` / `approval_required` / `tool_end` / `citation` / token 等 |
| `/v1/chat/{run_id}/approve` | POST | 审批回调：`call_id` + `decision`（allow / deny）                                            |
| `/v1/sessions`              | GET  | 会话列表                                                                                 |
| `/v1/workspaces`            | GET  | 在线执行器上报的可用工作目录列表（网页目录选择器用）                                                          |
| `/v1/sessions/{id}/workspace` | GET/PUT | 读取 / 绑定会话的工作目录（PUT body：`{"workspace": "D:\\路径"}`）                                |
| `/v1/chat/completions`      | POST | **OpenAI 兼容端点**，任何 OpenAI SDK 把 `base_url` 指到本服务即可                                   |
| `/health`                   | GET  | 健康检查（鉴权豁免）                                                                           |

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "agent", "messages": [{"role": "user", "content": "你好"}], "stream": false}'
```

可选鉴权：`runtime.api_key` 留空关闭；配置后所有请求需带 `X-API-Key` 头。

## 测试与质量门禁

```bash
make check         # ruff + mypy strict + 离线单测，一键门禁
make test          # 离线单测（FakeChatModel，无需真实 Key）
make test-online   # 需真实 API Key
make test-mysql    # 需真实 MySQL 实例
make eval          # RAG 金标准评测（20 条：Recall@5 / MRR / 引用正确率）
```

端到端验收脚本（真实 LLM + MySQL）：

```bash
python scripts/verify_p4_memory.py       # 记忆系统全链路
python scripts/verify_p6_subagents.py    # 子 Agent 分流
```

## 目录结构

```
YuanAgent/
├── backend/app/          # Python 主应用
│   ├── cli.py            #   CLI 入口（Typer）
│   ├── main.py           #   Web 入口（uvicorn）
│   ├── api/              #   HTTP 层：SSE / 审批回调 / OpenAI 兼容端点
│   ├── agents/           #   Agent 构建 + 子 Agent 分流（delegate / fan_out）
│   ├── middleware/       #   权限审批 / 人设 / 记忆召回 / 遥测 / 防注入
│   ├── memory/           #   记忆 schema / 召回 / 后台抽取
│   ├── mcp/              #   MCP 连接管理 / 档位继承 / 白名单
│   ├── rag/              #   KnowledgeChunk 契约 + stub 检索
│   ├── tools/            #   工具注册表 + 内置工具（沙箱文件系统等）
│   ├── llm/              #   双档模型工厂（main / fast）
│   ├── db/               #   ORM / 引擎 / 仓储
│   └── core/             #   配置 / 事件总线 / 日志脱敏 / 错误层级
├── frontend/             # Next.js 15 前端
├── runner/               # 本地执行器（YuanRunner）：WSS 客户端 + 文件三工具 + 单实例锁
├── skills/               # 技能目录（SKILL.md + 可选脚本，加目录即生效）
├── config/               # agent.yaml(.example) / tools.yaml / mcp.yaml
├── scripts/              # init_mysql / start_backend / start_frontend / rag/ / verify_*
├── data/                 # kb_stub 示例语料 + workspace 文件沙箱
├── docs/                 # 架构详解 / 设计规范 / 使用手册 / Roadmap
└── Makefile              # 常用命令入口
```

## 深入文档

| 文档                                                         | 内容                                            |
| ---------------------------------------------------------- | --------------------------------------------- |
| [docs/usage.md](./docs/usage.md)                           | 使用手册：安装配置 → CLI / Web → RAG 接入 → 子 Agent → 技能 Skills → Langfuse 可观测 → 排错 |
| [docs/architecture.md](./docs/architecture.md)             | 架构详解：逐文件讲解、流水线、安全矩阵、设计契约                      |
| [docs/architecture-brief.md](./docs/architecture-brief.md) | 架构速览：10-20 分钟读懂全貌                             |
| [docs/design.md](./docs/design.md)                         | 设计规范                                          |
| [docs/fanout-guide.md](./docs/fanout-guide.md)             | fan_out 并行委派使用说明                              |
| [docs/local-runner.md](./docs/local-runner.md)             | 本地执行器（YuanRunner）实施方案：架构 / 协议 / 安全 / 打包部署 |
| [docs/workspace.md](./docs/workspace.md)                   | 多工作目录方案：按会话绑定 / 目录上报 / 动态热切换               |
| [docs/roadmap.md](./docs/roadmap.md)                       | 演进路线与现状差异清单                                   |

## Roadmap

- [x] MCP 生态接入（mock + RAG 双 Server，stdio / streamable-http）
- [x] 限额熔断（工具 12 次 / 模型 15 次，超限自动收尾）
- [x] Web 端 API Key 鉴权
- [x] messages 业务表完整写入（与 checkpoint 闭合）
- [x] 容器化部署（backend / frontend / MySQL 编排）
- [x] fan_out 并行子 Agent 委派 + 前端执行链路图
- [x] 观测接入 Langfuse（带 trace_id 的跨进程追踪 + Prompt 版本化）
- [ ] 记忆生命周期管理（过期 / 删除 / 清空入口）
- [ ] 观测升级（metrics 导出 / 成本聚合看板）

## 常见问题

**Q：模型请求 401 / 403？**  
密钥与网关不配套（如 `sk-sp-` 开头的 Key 只对 token-plan 网关有效）。检查 `llm_base_url` 与密钥是否成对，且模型名在所选网关上真实存在。

**Q：启动日志出现 rag MCP 连接失败？**  
RAG 服务未启动或 Qdrant / embedding 服务不可用。单个 MCP Server 失败会被隔离，不影响其余功能；不需要真实知识库时把 `config/mcp.yaml` 中 rag 条目 `enabled: false`，或在 `config/tools.yaml` 启用 stub 版 `search_knowledge_base`。

**Q：文件工具报「路径超出沙箱」？**  
读写文件类工具被限制在 `data/workspace` 沙箱内，`../` 与绝对路径一律拒绝。可改 `config/tools.yaml` 的 `sandbox_root`。

**Q：联网搜索工具不存在？**  
未配置 `TAVILY_API_KEY` 时该工具自动禁用（设计如此，不报错）。配上重启即恢复。

**Q：`make` 命令不识别？**  
Makefile 只是命令清单，任何目标都可用 Python 直接执行，等价命令见 [docs/usage.md](./docs/usage.md)。

## License

[MIT](./LICENSE) © Mr-Shawn17
