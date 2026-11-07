# 使用手册

面向本项目的使用者与二次开发者，覆盖从环境准备、安装配置到 CLI / Web 双入口使用、RAG 知识库接入、子 Agent 分流、测试门禁与常见问题排查的完整流程。内部实现原理与设计决策不在本文范围，见 [architecture.md](./architecture.md)。

> 更新：2026-11-05（新增第 6 章「本地执行器与工作目录」）｜ 快速概览见仓库根 [README](../README.md)

---

## 目录

1. [这个项目能做什么](#1-这个项目能做什么)
2. [环境要求](#2-环境要求)
3. [首次安装](#3-首次安装)
4. [使用 CLI 终端聊天](#4-使用-cli-终端聊天)
5. [使用 Web 界面](#5-使用-web-界面)
6. [本地执行器与工作目录（网页操作本机文件）](#6-本地执行器与工作目录网页操作本机文件)
7. [接入 RAG 知识库](#7-接入-rag-知识库)
8. [子 Agent 分流与 fan_out 并行](#8-子-agent-分流与-fan_out-并行)
9. [技能 Skills：教 Agent 按你的流程做事](#9-技能-skills教-agent-按你的流程做事)
10. [配置参考](#10-配置参考)
11. [HTTP API 速览](#11-http-api-速览)
12. [测试与质量门禁](#12-测试与质量门禁)
13. [Langfuse 可观测（trace 追踪 + Prompt 版本化）](#13-langfuse-可观测trace-追踪--prompt-版本化)
14. [常见问题](#14-常见问题)
15. [目录速查](#15-目录速查)

---

## 1. 这个项目能做什么

「阿元」是一个日常聊天 Agent，具备以下能力：

- **流式对话**：CLI 终端打字机效果，或浏览器 Web 界面 SSE 流式输出
- **工具调用**：时间、计算器、联网搜索、沙箱文件读写、知识库检索等内置工具，外加 MCP 协议接入的第三方工具
- **子 Agent 分流**：主 Agent 只做路由与汇总——调研任务委派给 research 子 Agent，沙箱写文件委派给 coding 子 Agent；多对象对比类问题经 `fan_out` 并行拆分执行（`config/agent.yaml` 一键开关，见[第 8 章](#8-子-agent-分流与-fan_out-并行)）
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

### 5.3 界面功能

- **左侧会话列表**：点击「＋ 新对话」开新会话；历史会话显示标题、时间与消息数，点击即恢复。
- **流式气泡**：回答逐字输出，底部显示本轮 token 用量（↑ 输入 / ↓ 输出）。
- **工具轨迹折叠面板**：每轮回答上方显示「工具轨迹（N）」，点开可见每次工具调用的档位、状态（待审批 / 完成 / 失败）、耗时、入参与结果；子 Agent 的调用带 `[subagent:research]` / `[subagent:coding]` 前缀。
- **引用卡片**：知识库检索命中后，回答下方渲染编号引用卡片，点击展开全文片段与来源。
- **审批弹窗**：触发 `write` / `danger` 档工具时弹出「需要你的确认」，展示工具名、档位与完整调用参数；弹窗期间输入框禁用（阻断生效），点「拒绝」或「允许执行」后流程才继续。
- **执行链路图**：`fan_out` 并行委派时渲染主 Agent → 并行子任务泳道 → 汇总节点的可视化卡片。

预期结果（可自测的四项验收）：流式输出无卡顿；工具轨迹可展开；引用卡片可点开看全文；发送「请记住：我的生日是 3 月 12 日。」会弹审批窗，拒绝后工具不执行、允许后轨迹显示「完成」且回复确认已记住。

### 5.4 服务日志（文件）

前后端日志均落盘到项目根 `log/` 目录（已被 gitignore），按天一文件，仅保留近 7 天（启动时自动清理过期文件）：

| 文件 | 内容 | 采集方式 |
| --- | --- | --- |
| `log/backend-YYYY-MM-DD.log` | 后端全部运行日志（API / MCP / 审计 / 异常堆栈），与终端同链路、密钥自动脱敏 | structlog 文件 handler |
| `log/frontend-YYYY-MM-DD.log` | 前端 npm 子进程全部输出（编译状态 / 请求报错 / 堆栈），每次启动追加带时间戳分节 | 启动脚本 tee 捕获 |

可调配置（`config/agent.yaml` 的 runtime 段）：`log_dir`（默认 `log`）与 `log_retention_days`（默认 7，设 0 为永久保留）。

---

## 6. 本地执行器与工作目录（网页操作本机文件）

> 更新：2026-11-05。本功能让 Web 端的文件读写真正落到**用户本机电脑**，而不是服务器沙箱。完整设计见 [local-runner.md](./local-runner.md) 与 [workspace.md](./workspace.md)。

### 6.1 一句话原理

文件工具（`read_file` / `write_file` / `list_directory`）默认 `mode: local`。调用时经中心服务的 `LocalTaskHub`，通过 WSS 长连接转发到运行在用户电脑上的无窗口后台进程 **YuanRunner**，在其绑定的工作目录下执行后回传结果。

```text
网页 :3000 → 后端 :8000 → 文件工具 mode=local → LocalTaskHub → /ws/runner (WSS)
  → 本机 YuanRunner → 按绑定的工作目录执行 → 回传结果
```

### 6.2 启动三进程（本机闭环）

三个进程缺一不可，建议各开一个终端：

```bash
# ① 后端（:8000，含 /ws/runner 与 device 设备管理）
python scripts/start_backend.py

# ② 前端（:3000，Next rewrites 把 /v1/* 代理到后端）
cd frontend && npm run dev

# ③ 本机执行器（无窗口后台进程，保持 WSS 长连接）
python -m runner run          # 或双击打包后的 dist/YuanRunner/YuanRunner.exe
```

### 6.3 首次配置执行器（只需一次）

```bash
# 1) 生成示例配置到 %LOCALAPPDATA%\YuanAgent\runner.yaml
python -m runner init-config
#    按需改 server_url（默认 ws://127.0.0.1:8000/ws/runner）与 workspace（默认 ~/YuanAgent-workspace）

# 2) 中心签发设备令牌（令牌只显示一次，立即复制保存）
python -m app.cli device create --name my-laptop

# 3) 令牌写入本机凭据库（Windows 凭据管理器 / keyring）
python -m runner set-token yrt_xxx...

# 4) 启动执行器
python -m runner run
```

执行器子命令一览：

| 命令 | 作用 |
| --- | --- |
| `python -m runner run` | 启动执行器（默认命令，双击 exe 即此） |
| `python -m runner init-config` | 生成示例 `runner.yaml` |
| `python -m runner set-token <令牌>` | 保存设备令牌到系统凭据库 |
| `python -m runner status` | 显示版本 / 配置 / 服务地址 / 工作目录 / 令牌来源 |
| `python -m runner clear-token` | 删除已保存的令牌 |

设备管理子命令（后端侧）：

| 命令 | 作用 |
| --- | --- |
| `python -m app.cli device create --name <备注>` | 签发设备令牌（只显示一次） |
| `python -m app.cli device list` | 列出设备（ID / 备注 / 状态 / 最近在线） |
| `python -m app.cli device revoke <id前缀>` | 吊销令牌（前缀匹配） |

### 6.4 网页上选择工作目录

浏览器打开 http://localhost:3000，顶栏右侧「📁 选择工作目录」按钮：

- **下拉**：列出执行器上报的常用目录（主目录 / 桌面 / 文档 / 下载等）
- **手填**：输入任意绝对路径（如 `E:\agent_test`），执行器校验存在后加入可用列表
- **绑定**：选中即绑定到**当前会话**并持久化到 `sessions.meta`；不同会话可各绑一个目录、并行多开、动态热切换

补充语义：

- 新会话默认未绑定目录，首次选择后绑定；次日重开该会话自动恢复绑定
- 执行器离线时仍可聊天，文件操作返回 `[local_runner_offline]` 提示文本，不会中断对话
- 绑定接口 `PUT /v1/sessions/{id}/workspace`；会话列表 `GET /v1/sessions` 返回各自 `workspace`；可用目录列表 `GET /v1/workspaces`

### 6.5 安全模型（命令沙箱 + 可选 VM 档）

| 机制 | 说明 |
| --- | --- |
| 设备令牌鉴权 | `secrets.token_urlsafe(32)`（`yrt_` 前缀），库内只存 SHA-256，可吊销 |
| 传输 | WSS 长连接，执行器主动外连，无需在用户电脑开端口 |
| 路径沙箱 | 执行器对每个 workspace 做 `is_relative_to` 越界检查，`../` 与绝对路径一律拒绝 |
| 工具白名单 | 执行器侧只认 `read_file` / `write_file` / `list_directory` + 命令类 `run_command` / `delegate_coding` / `git_clone|pull|push` |
| 命令沙箱 | `run_command`（danger 档）：命令白名单（多命令段逐段校验）+ 危险模式黑名单 + 环境变量白名单（绝不透传 API key）+ 超时杀整棵进程树 + 输出封顶 |
| VM 档（可选） | Windows 沙盒功能启用后 `command.backend: auto` 自动走一次性 VM（workspace 映射 + 便携工具链），未启用回退策略层 |
| git 凭证托管 | PAT 存系统凭据库（`python -m runner set-git-cred <host>`），命令由 runner 拼装 + `GIT_ASKPASS` 注入，凭证不进模型上下文/命令行/输出 |
| 审批 + 审计 | write/danger 档在中心服务弹审批窗，批准后下发；`tool_audit` 全量落库 |
| 单实例锁 | 重复双击执行器直接退出新进程 |

⚠️ 默认（策略层）是「白名单 + 路径约束 + 人工审批」的**软件层防护**；需要系统级隔离时启用 Windows 沙盒功能走 VM 档（详见 `docs/local-runner.md` §13 命令沙箱）。编程任务委派 `delegate_coding` 走用户本机 codex CLI 自己的沙箱与账号。

---

## 7. 接入 RAG 知识库

知识库与 Agent 已解耦：真实检索能力由外部混合检索 RAG 服务提供（Qdrant 稠密 + BM25 稀疏 → RRF 融合 → rerank），本项目经 MCP over streamable-http 调用。**不需要真实知识库时可跳过本节，用 stub 模式（本地模拟数据）即可跑通全部链路。**

### 7.1 stub / mcp 两种模式

| 模式 | 说明 | 适用 |
| --- | --- | --- |
| `stub` | 本地语料模拟检索（`data/kb_stub/chunks.json`） | 开发调试、无 Qdrant 环境 |
| `mcp` | 经 MCP 调用外部真实 RAG 服务 | 生产 / 真实问答 |

两处开关：

1. **内置 stub 工具**：`config/tools.yaml` 的 `search_knowledge_base.enabled`（当前默认 `false`，因真实 RAG 已接入）
2. **MCP rag Server**：`config/mcp.yaml` 的 `rag` 条目（`url` 指向你的 RAG 服务，默认 `http://127.0.0.1:8100/mcp`；不需要时设 `enabled: false`）

MCP 模式下工具带 `rag__` 命名空间前缀：`rag__search` / `rag__list_companies` / `rag__get_chunk_detail` / `rag__health`，全 `read` 档免审批。

### 7.2 启动你自己的 RAG MCP 服务

本项目自带一个最小 RAG MCP 服务（供联调参考）：

```bash
python scripts/rag/serve_mcp.py     # 监听 http://127.0.0.1:8100/mcp
```

首次使用可灌入示例知识库：

```bash
python scripts/rag/seed_agent_knowledge.py   # 幂等：灌入年报语料到 agent_knowledge collection
```

> 生产环境建议部署独立的混合检索服务（Qdrant + embedding + reranker），并把 `config/mcp.yaml` 的 `url` 指过去。若你的 RAG 服务端是旧版 MCP SDK（协议 2024-11-05），需在 rag 条目加 `mode: legacy` 强制旧版握手。

### 7.3 跑金标准评测（可选）

RAG 服务启动后：

```bash
python -m pytest tests/eval -q -m rag_service -s
# 等价 make eval
```

20 条金标准用例，指标门槛 Recall@5 / MRR / 引用正确率。

---

## 8. 子 Agent 分流与 fan_out 并行

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

## 9. 技能 Skills：教 Agent 按你的流程做事

Skill = 一包可插拔的领域操作指南：一个目录、一份 `SKILL.md`（YAML frontmatter + markdown 指南正文），可选携带模板资源与 Python 脚本。Agent 平时只在系统提示里看到技能清单（名称 + 一行描述），任务匹配时才调 `load_skill` 读指南全文，再按指南组合现有工具执行。这套做法叫**渐进式披露**，好处是技能再多也不撑爆上下文。

### 9.1 用起来：加目录 → 重启 → 说一句话

三步，不用写任何代码：

1. 技能目录放进 `skills/`，一个技能一个子目录，里面至少一份 `SKILL.md`
2. 重启 CLI 或后端。技能只在启动时扫描一次，暂不支持热加载
3. 正常聊天，用自然语言提任务。要不要加载由 Agent 自己判断，判断依据就是 `SKILL.md` 里的 `description`

先确认加载状态。CLI 里输入 `/skills`：

```text
> /skills
技能            描述                              脚本
weekly-report   撰写周报的流程指南。当用户提到…    gen_skeleton.py

未加载 draft-helper：解析失败：缺少 frontmatter
```

表里是已加载的技能，没进表的会在下面逐条列出原因。

然后是实际对话。以仓库自带的周报技能为例，一轮完整交互长这样：

```text
你 > 帮我写本周周报，这周主要在搞技能功能

阿元 > load_skill(skill_name='weekly-report')        ← read 档，不打断你
       今天是 2026-11-02 周一，本周范围 10-26 至 11-01。先按这个范围整理：
       本周完成：
       - 技能功能：跑通渐进式披露与脚本执行，单测全绿
       ...
       内容和范围确认后我生成骨架并写入文件。

你 > 内容没问题，生成吧

阿元 > [工具执行确认] execute_skill_script · 危险操作
       execute_skill_script(skill_name='weekly-report', script_name='gen_skeleton.py', …)
       允许执行吗？[y/N]
你 > y

阿元 > 骨架已生成：D:\...\skills\weekly-report\scripts\weekly_report.md（10-26 ~ 11-01），
       我接着按你口述的内容填充。
```

两个工具在体验上的区别就一句话：`load_skill` 是 read 档，读指南不打断你；`execute_skill_script` 是 danger 档，每次跑脚本都要你点头。Web 界面上对应的是审批弹窗，和 CLI 共用同一套权限中间件，所以审批记录、`tool_audit` 审计、工具轨迹面板两边一致。

脚本的工作目录是它自身所在的 `scripts/` 目录，所以 `--output` 传相对路径时文件落在 `scripts/` 下；脚本会打印解析后的绝对路径，想写到别处直接传绝对路径。

### 9.2 触发不生效怎么查

按下面顺序排查，绝大多数问题在第一步就能看到：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/skills` 里没有这个技能 | frontmatter 写错、缺 `---` 分隔符、`name` 与目录名不一致 | 看 `/skills` 下面的未加载原因，按提示改 |
| 同上，但原因是被白名单挡住 | `skills.allow_list` 非空且不含该技能 | 加进白名单，或把 `allow_list` 置空 |
| 已加载但 Agent 不调用 | `description` 没写清触发场景 | `description` 是模型路由的唯一依据，把用户可能说的说法写进去 |
| 脚本拒绝执行 | 脚本不在 `scripts/` 内，或不在 `allowed_scripts` 白名单里 | 移到 `scripts/` 下，或补白名单 |
| 脚本超时被杀 | 单次执行超过 `timeout_seconds`（默认 60s） | 拆小脚本，或在 frontmatter 里调 `script_timeout` |
| 输出被截断 | stdout/stderr 合计超过 `max_output_chars`（默认 20000） | 脚本自己写文件，只往 stdout 打摘要 |

### 9.3 写一份自己的技能

```text
skills/
└── weekly-report/            # 技能名 = 目录名
    ├── SKILL.md              # 必需：frontmatter + 指南正文
    ├── template.md.j2        # 可选：静态资源（指南内相对引用）
    └── scripts/
        └── gen_skeleton.py   # 可选：可执行脚本（白名单约束）
```

frontmatter 字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 必须与目录名一致，小写字母、数字、连字符、下划线 |
| `description` | 是 | 一句话说清这个技能干什么、什么时候用。模型靠它决定要不要加载，写清用户可能说的说法 |
| `allowed_scripts` | 否 | 脚本白名单；不写等于 `scripts/` 下全部 `.py` 都允许 |
| `script_timeout` | 否 | 本技能脚本的单次超时（秒），覆盖全局默认值 |

一份最小可用的 `SKILL.md`：

```markdown
---
name: weekly-report
description: 撰写周报的流程指南。当用户提到"周报/本周总结/写周报/汇报本周进展"等时使用。
---
# 周报撰写指南

1. 用 `get_current_time` 确认今天日期，算出本周的周一到周日
2. 询问用户本周做了什么，没提到的一律不编造
3. 按模板组织内容，用 `write_file` 写入沙箱
```

写指南的经验：

- **写成流程，不要写百科**。Agent 需要的是先做什么、再做什么、什么情况停下来问用户，领域知识的堆砌反而挤占上下文
- **点名可用工具**。直接写调用 `get_current_time` 或 `write_file`，比让模型自己猜更稳
- **写清边界**。比如用户说简单点时省略哪些小节，比事后返工省事
- **脚本能省则省**。能用现有工具组合完成的就别写脚本，脚本每跑一次都要过审批，打断节奏

指南正文里引用脚本时，参数名就是工具的真实参数名：

```text
execute_skill_script(skill_name="weekly-report", script_name="gen_skeleton.py",
                     script_args="--week 2026-W44 --output weekly_report.md")
```

写完重启一次，用 `/skills` 确认加载成功，再用一句自然语言试触发。仓库自带的 `skills/weekly-report/` 是完整范本，照着抄结构最快。

### 9.4 两个内置工具与权限

| 工具 | 档位 | 行为 |
| --- | --- | --- |
| `load_skill` | `read` | 读技能指南全文，免审批，写审计 |
| `execute_skill_script` | **`danger`** | 运行技能脚本，**每次执行必须用户审批**（终端询问 / Web 弹窗） |

脚本执行的安全模型（多层防护，非容器级隔离，个人场景够用）：

1. 路径钉死：只能运行该技能 `scripts/` 目录内的脚本，`../`、绝对路径一律拒绝
2. 白名单前置：`allowed_scripts` 之外的脚本直接拒绝
3. 独立进程 + 超时：`sys.executable` 子进程执行，超时（默认 60s）终止
4. 输出封顶：stdout/stderr 合计超 20000 字符截断
5. danger 档审批 + 全量审计：每次执行落 `tool_audit` 表

### 9.5 配置开关（config/agent.yaml 的 skills 段）

```yaml
skills:
  enabled: true          # 总开关：false 时技能不加载、两个工具不装配
  dir: skills            # 技能根目录（相对路径按项目根解析）
  allow_list: []         # 白名单：空 = 全部加载；非空 = 只加载列表内技能
  script:
    enabled: true        # 脚本执行总开关：false 一键降回纯知识型技能
    timeout_seconds: 60
    max_output_chars: 20000
```

仓库自带示例技能 `skills/weekly-report/`（周报撰写 + 骨架生成脚本），可直接作为编写新技能的范本。

### 9.6 与子 Agent 的配合

`load_skill` 已加入 `routing.main_agent.keep_tools` 与 research / coding 子 Agent 的 `allowed_tools`，主 Agent 与子 Agent 都能按技能指南执行；`execute_skill_script` 在 coding 子 Agent 白名单内，委派审批一次后内部可直接运行。

---

## 10. 配置参考

配置分两层：密钥与环境相关进环境变量（`*_api_key_env` 指定变量名），行为策略进 `config/*.yaml`。

### 10.1 config/agent.yaml（七段）

| 段 | 管什么 |
| --- | --- |
| `persona` | 人设、说话风格、边界（名字、语言、时区等） |
| `limits` | 单轮熔断限额：`max_tool_calls_per_turn`（默认 12）/ `max_model_calls_per_turn`（默认 15） |
| `history` | 上下文压缩：摘要触发阈值（默认 12000 tokens）、保留消息数（默认 20） |
| `server` | 启动脚本读取：Python 环境根目录、后端/前端端口 |
| `runtime` | 运行时配置（原 .env 内容）：模型三档（main / fast / fallback）、MySQL、知识库、第三方 Key 环境变量名、CORS、Web 鉴权、日志目录与保留天数、Langfuse 可观测 |
| `routing` | 子 Agent 分流：总开关、主 Agent 保留清单、research / coding 定义、fan_out 参数 |
| `skills` | 技能：总开关、目录、白名单、脚本执行约束（超时/输出上限） |

### 10.2 config/tools.yaml

工具白名单（`default_allow: false`，未登记一律禁用）、每个工具的权限档位、文件工具沙箱根目录（`data/workspace`，沙箱外路径一律拒绝）、KB 模式开关。

### 10.3 config/mcp.yaml

MCP Server 清单：transport（stdio / streamable_http）、权限档位继承、工具白名单、超时。**接入新 Server 只加一段配置，不改代码。**

### 10.4 密钥环境变量

| 环境变量 | 用途 |
| --- | --- |
| `GLM_API_KEY` / `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` 等 | LLM 密钥，变量名由 `*_api_key_env` 指定，留空按 `DASHSCOPE_API_KEY` → `OPENAI_API_KEY` → `GLM_API_KEY` 顺序回退 |
| `MYSQL_PASSWORD` | MySQL 密码（`mysql_password` 留空时回退读取） |
| `TAVILY_API_KEY` | 联网搜索；缺失时该工具自动禁用而非启动报错 |

### 10.5 权限档位

| 档位 | 行为 | 当前成员 |
| --- | --- | --- |
| `read` | 直接执行，仅审计 | 时间、计算器、联网搜索、目录列表、读文件、知识库检索、`load_skill`、全部 MCP 工具、`delegate_to_research` |
| `write` | 执行前需审批 | `write_file`、`remember`、`delegate_to_coding`（批准一次即整体授权子 Agent 内部操作） |
| `danger` | 执行前需审批（更醒目的提示） | `execute_skill_script`（运行技能脚本） |

---

## 11. HTTP API 速览

后端（默认 8000 端口）对外提供三组端点，前端即基于它们工作：

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/v1/chat` | POST | SSE 流式对话。请求体：`message`（必填）、`session_id`（空则新建会话）、`user_id`。事件流包含 `tool_start` / `approval_required` / `approval_decided` / `tool_end` / `citation` / token 与错误事件 |
| `/v1/chat/{run_id}/approve` | POST | 审批回调。请求体：`call_id`、`decision`（`allow` / `deny`） |
| `/v1/sessions` | GET | 会话列表（thread_id、标题、消息数、更新时间、绑定的 `workspace`） |
| `/v1/workspaces` | GET | 在线执行器上报的可用工作目录列表（网页目录选择器用） |
| `/v1/sessions/{id}/workspace` | GET/PUT | 读取 / 绑定会话的工作目录（PUT body：`{"workspace": "D:\\路径"}`） |
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

## 12. 测试与质量门禁

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
| `make eval` | RAG 金标准评测 | RAG MCP 服务（见第 7 节） |
| `make check` | lint + typecheck + test 一键门禁 | 无 |

端到端验收脚本（真实 LLM + MySQL）：

```bash
python scripts/verify_p4_memory.py       # 记忆系统全链路
python scripts/verify_p6_subagents.py    # 子 Agent 分流
```

---

## 13. Langfuse 可观测（trace 追踪 + Prompt 版本化）

> 更新：2026-11-02（接入 Langfuse 自托管，补齐带 `trace_id` 的跨进程追踪与 Prompt 版本化管理）

本项目已接入 **Langfuse（自托管）** 作为可观测层，与前端已有的「执行链路图谱」**并存互补**：

- 前端聊天界面里的实时执行链路图（`ExecutionGraph`）继续给**最终用户**看执行过程，保持不变；
- Langfuse 是**后端旁路**的开发者工具，负责补上两块能力：**标准 `trace_id` 的跨进程追踪**（打通主 Agent → 子 Agent → 工具 → RAG MCP 服务）、以及 **Prompt 版本化管理**（发布 / 回滚 / 灰度）。

它是**可选**的：`langfuse_enabled: false` 时完全关闭，不影响对话等任何既有功能。

### 13.1 部署 Langfuse（自托管）

部署文件在 `deploy/langfuse/`，用 Docker Compose 一键拉起（Postgres + ClickHouse + Redis + Langfuse Web/Worker）：

```bash
cd deploy/langfuse
docker compose --env-file .env up -d          # 首次会拉取镜像，约 2-3 分钟
docker compose --env-file .env ps             # 看各容器状态，等待全部 Healthy/Started
```

面板默认监听 `http://localhost:3030`（避开前端 Next.js 的 3000 端口）。

> **登录账号与 API Key**：`deploy/langfuse/.env` 里已通过 headless 初始化预置了账号、组织、项目与 API Key（含注释标记）。浏览器打开 `http://localhost:3030` 用该邮箱/密码登录即可。密钥均走 `.env`（已被 gitignore），不会进版本库。

停止 / 升级：

```bash
docker compose --env-file .env down                              # 停止（保留数据）
docker compose --env-file .env pull && docker compose --env-file .env up -d   # 升级镜像
```

### 13.2 配置（config/agent.yaml 的 langfuse 段）

```yaml
runtime:
  langfuse_enabled: true            # 总开关：false 时完全关闭，优雅降级为 no-op
  langfuse_host: http://localhost:3030
  langfuse_public_key: "pk-lf-..."  # 面板 Project 设置里可查到
  langfuse_secret_key: "sk-lf-..."  # 同上
```

三个 `langfuse_*` 字段均已预填 `deploy/langfuse/.env` 里生成的真实值。改了配置需重启后端。

### 13.3 查看 trace 与 Prompt 版本

登录 `http://localhost:3030`，进入 Project 后：

| 想看的 | 面板位置 | 说明 |
| --- | --- | --- |
| **调用链路 trace** | 左侧导航 **Tracing / Traces** | 每次对话产生一条 trace，点开可见 `trace_id` / `session_id` / `user_id`、耗时、token 用量，以及「主 Agent → 子 Agent → 工具 → RAG 检索」的 span 嵌套层级 |
| **Prompt 版本** | 左侧导航 **Prompts** | 找到 `agent-system`，查看版本历史、对比差异；`production` 这个 label 指向的版本即当前生产实际使用的版本 |

### 13.4 Prompt 版本化管理

system 主提示词已迁到 Langfuse 管理（模板 `agent-system`，`production` label）。运行时 `render_system_prompt` 优先从 Langfuse 拉取该 label 指向的版本编译渲染，**拉取失败自动回退本地 `backend/app/agents/prompts/system.j2`**，因此 Langfuse 挂了也不影响对话。

- **发布新版本**：改完模板后重新运行 `python scripts/init_langfuse_prompts.py`，会创建一个新版本并把 `production` label 指过去。
- **回滚**：在 Langfuse 面板把 `agent-system` 的 `production` label 指回旧版本即可，无需改代码、无需重新部署。

> 注意：Langfuse 文本 Prompt 用 `{{ 变量 }}` 语法（Mustache 风格），**不支持** Jinja2 的 `{% if %}` 等控制流——条件拼接逻辑已移到 Python 端（`builder.py`）。当前仅 system 主提示词接入（试点）；persona 与子 Agent 的 `system_prompt` 仍留在 `config/agent.yaml`，后续可渐进迁移。

---

## 14. 常见问题

**网页点「绑定」工作目录报 HTTP 404**

通常是当前会话还没生成 session id（页面刚打开、尚未发送过消息）。刷新页面即可——前端会在初始化时自动预生成会话 id，之后再绑定就不会 404。若仍报错，确认后端已重启到包含 `/v1/sessions/{id}/workspace` 端点的版本。

**执行器连不上 / 文件操作返回 `[local_runner_offline]`**

`python -m runner status` 自查：令牌是否已写入（`set-token`）、`runner.yaml` 的 `server_url` 是否指向后端、后端 `/ws/runner` 是否可达。执行器日志在 `%LOCALAPPDATA%\YuanAgent\logs\runner.log`。执行器离线不影响聊天，只是文件操作落到 offline 提示。

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

## 15. 目录速查

```
YuanAgent/
├── backend/app/          # Python 主应用（cli.py 终端入口 / main.py Web 入口 / api/ HTTP 层）
├── frontend/             # Next.js 15 Web 前端
├── runner/               # 本地执行器（YuanRunner）：WSS 客户端 + 文件三工具 + 单实例锁（见第 6 章）
├── config/               # agent.yaml(.example) / tools.yaml / mcp.yaml
├── scripts/              # init_mysql.py、start_backend.py、start_frontend.py、
│                         # rag/（serve_mcp.py、seed_agent_knowledge.py）、
│                         # verify_p4_memory.py、verify_p6_subagents.py、
│                         # verify_local_runner.py、verify_workspace.py、
│                         # init_langfuse_prompts.py
├── deploy/langfuse/      # Langfuse 自托管（docker-compose.yml + .env，见第 13 章）
├── tests/                # unit（离线）/ integration / eval（RAG 金标准）
├── data/                 # kb_stub 语料 + workspace 沙箱
├── docs/                 # architecture.md、architecture-brief.md、design.md、
│                         # fanout-guide.md、local-runner.md、workspace.md、
│                         # roadmap.md、本文
├── Makefile              # 常用命令入口
└── LICENSE               # MIT
```

各模块职责、数据流水线、安全矩阵与已知差异清单，见 [architecture.md](./architecture.md)。
