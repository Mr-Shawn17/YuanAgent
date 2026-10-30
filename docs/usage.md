# 使用手册

面向本项目的最终使用者与二次开发者，覆盖从环境准备、安装配置到 CLI / Web 双入口使用、RAG 知识库接入、测试门禁与常见问题排查的完整流程。内部实现原理与设计决策不在本文范围，见 [architecture.md](./architecture.md)。

最后更新：2026-10-17（P6' 子 Agent 分流落地后）。

---

## 1. 这个项目能做什么

「阿元」是一个日常聊天 Agent，具备以下能力：

- **流式对话**：CLI 终端打字机效果，或浏览器 Web 界面 SSE 流式输出
- **工具调用**：时间、计算器、联网搜索、沙箱文件读写、知识库检索等内置工具，外加 MCP 协议接入的第三方工具
- **子 Agent 分流**：主 Agent 只做路由与汇总——调研任务委派给 research 子 Agent，沙箱写文件委派给 coding 子 Agent（`configs/agents.yaml` 一键开关，见[第 7 章](#7-子-agent-分流p6)）
- **RAG 知识库**：检索命中后回答带 `[1] [2]` 引用编号，Web 端渲染可展开的引用卡片
- **三级权限审批**：`read` 档工具直接执行；`write` / `danger` 档执行前必须经你确认（CLI 终端询问，Web 弹窗阻断）
- **长期记忆**：说"请记住……"即写入记忆，跨会话可召回；长对话自动摘要压缩
- **会话持久化**：全部会话、消息、审计、记忆落在 MySQL，随时恢复历史会话

---

## 2. 环境要求

| 依赖 | 版本 / 说明 | 必需性 |
| --- | --- | --- |
| 操作系统 | 本文以 Windows + PowerShell 为准（Makefile 命令均给出 PowerShell 等价写法） | — |
| Python | 3.12（项目内置 `.venv312`，基于 `D:/software/miniconda3/envs/py312` 创建） | 必需 |
| Node.js | 18+（Web 前端 Next.js 15 需要；npm 在 PowerShell 中写作 `npm.cmd`） | 仅 Web 端 |
| MySQL | 8.0（基础环境，独立管理，`127.0.0.1:3306`） | 必需 |
| Qdrant | 向量库（RAG 服务使用，`agent_knowledge` collection） | 仅 RAG mcp 模式 |
| LLM API Key | 阿里云百炼 dashscope，或 token-plan 个人版网关 | 必需 |
| Tavily API Key | 联网搜索工具用 | 可选（缺 Key 该工具自动禁用，不报错） |

---

## 3. 首次安装

### 3.1 安装 Python 依赖

前置条件：`.venv312` 已存在（仓库自带约定；若不存在，先用 py312 创建：`D:/software/miniconda3/envs/py312/python.exe -m venv .venv312 --system-site-packages`）。

```powershell
cd D:\VSCodeProject\Multi_Agent
.venv312\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[dev,tools,memory]"
```

等价于 `make install`。预期结果：pip 以可编辑模式安装 `app` 包及 dev / tools / memory 三个可选组，无红色报错。

### 3.2 配置 .env

前置条件：仓库根目录存在 `.env.example`。

1. 复制 `.env.example` 为 `.env`（`.env` 已被 gitignore，不会提交）。
2. 按所用网关二选一填写模型配置：

   **公共 dashscope**（默认）：

   ```ini
   LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
   LLM_MODEL=qwen-plus
   LLM_API_KEY=sk-你的密钥
   ```

   **token-plan 个人版网关**：

   ```ini
   LLM_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
   LLM_MODEL=qwen3.6-flash
   FAST_LLM_MODEL=qwen3.6-flash
   LLM_API_KEY=sk-sp-你的密钥
   ```

   密钥与网关必须配套：`sk-sp-` 开头的 Key 只对 token-plan 网关有效，拿去请求公共 dashscope 会 401；反之亦然。token-plan 网关的可用模型随套餐变动（2026-10 起 `qwen3.8-flash` 已下架、套餐内模型全部返回 403，需续费后才可用，详见[第 11 章](#11-常见问题)）。

3. 填 MySQL 密码：`MYSQL_PASSWORD=你的密码`（本机 MySQL 的 root 密码）。
4. 其余变量保持默认即可，全部变量含义见 [8.1 节](#81-env-变量参考)。

注意 dotenv 的一个坑：值为空的行若要加注释，注释必须独占一行——「值为空 + 行内注释」会把整段注释当成值。

### 3.3 初始化数据库

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
.venv312\Scripts\python.exe scripts\init_mysql.py
```

等价于 `make db-init`。脚本幂等：建库 `agent`（不存在才建）、建 3 张业务表（sessions / messages / tool_audits）、触发 LangGraph checkpointer / store 自管表的 setup。可重复执行。

预期结果：日志显示建库建表完成，无异常栈。

### 3.4 验证安装

```powershell
.venv312\Scripts\python.exe -m app.cli ping      # 发一次 1 token 请求验证模型连通
.venv312\Scripts\python.exe -m app.cli config    # 显示当前生效配置（密钥已脱敏）
.venv312\Scripts\python.exe -m app.cli version
```

预期结果：`ping` 输出模型响应正常；`config` 回显的 base_url / model 与你填写的一致。

### 3.5 安装前端依赖（仅 Web 端需要）

```powershell
cd D:\VSCodeProject\Multi_Agent\frontend
npm.cmd install
```

---

## 4. 使用 CLI 终端聊天

前置条件：完成第 3 节全部步骤，MySQL 容器在运行。

```powershell
cd D:\VSCodeProject\Multi_Agent
.venv312\Scripts\python.exe -m app.cli chat
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

```powershell
.venv312\Scripts\python.exe -m app.cli sessions   # 列出历史会话（等价 make sessions）
```

预期结果：对话可用、审批可阻断、退出后重进能用 `/use` 恢复刚才的会话。

---

## 5. 使用 Web 界面

需要两个终端分别启动后端与前端。

### 5.1 启动后端 API

```powershell
cd D:\VSCodeProject\Multi_Agent\backend
..\.venv312\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

等价于 `make dev`（需在仓库根执行）。启动日志中应看到：

- `MCP Server mock 已连接，接入 3 个工具`（echo / add / now）
- `MCP Server rag 已连接，接入 1 个工具：rag__search_knowledge_base`（需 RAG 服务已启动，见第 6 节；未启动时该 Server 连接失败会被隔离，不影响其余功能）
- `Application startup complete` + `Uvicorn running on http://127.0.0.1:8000`

### 5.2 启动前端

```powershell
cd D:\VSCodeProject\Multi_Agent\frontend
npm.cmd run dev
```

等价于 `make web`。浏览器打开 `http://localhost:3000`。

### 5.3 界面功能

- **左侧会话列表**：点击「＋ 新对话」开新会话；历史会话显示标题、时间与消息数，点击即恢复。
- **流式气泡**：回答逐字输出，底部显示本轮 token 用量（↑ 输入 / ↓ 输出）。
- **工具轨迹折叠面板**：每轮回答上方显示「工具轨迹（N）」，点开可见每次工具调用的档位、状态（待审批 / 完成 / 失败）、耗时、入参与结果。
- **引用卡片**：知识库检索命中后，回答下方渲染编号引用卡片，点击展开全文片段与来源。
- **审批弹窗**：触发 `write` / `danger` 档工具时弹出「需要你的确认」，展示工具名、档位与完整调用参数；弹窗期间输入框禁用（阻断生效），点「拒绝」或「允许执行」后流程才继续。

预期结果（可自测的四项验收）：流式输出无卡顿；工具轨迹可展开；引用卡片可点开看全文；发送「请记住：我的生日是 3 月 12 日。」会弹审批窗，拒绝后工具不执行、允许后轨迹显示「完成」且回复确认已记住。

---

## 6. 接入 RAG 知识库（mcp 模式）

知识库与 Agent 已解耦：真实检索能力由外部「混合检索RAG」项目提供，本项目经 MCP over streamable-http 调用。不需要真实知识库时可跳过本节，用 stub 模式（本地模拟数据）即可跑通全部链路。

### 6.1 切换 stub / mcp 模式

模式开关在 `configs/tools.yaml`（不是环境变量，`.env` 里的 `KB_MODE` 是历史遗留死配置，改它不生效）：

```yaml
tools:
  search_knowledge_base:
    enabled: true
    tier: read
    mode: mcp    # stub = 本地模拟数据；mcp = 经 MCP 调真实 RAG 服务
```

`mode: mcp` 时本地 stub 工具工厂返回 `None`，同名工具由 MCP rag server 提供（带 `rag__` 前缀）。

### 6.2 启动 RAG MCP 服务

前置条件：`D:\VSCodeProject\混合检索RAG` 项目存在；Qdrant 与 embedding / reranker 服务可用（该项目的 `.env` 自行配置）。

```powershell
cd D:\VSCodeProject\Multi_Agent
.venv312\Scripts\python.exe scripts\rag\serve_mcp.py
```

服务监听 `http://127.0.0.1:8100/mcp`（与 `configs/mcp.yaml` 的 rag 条目、`.env` 的 `RAG_MCP_URL` 对应）。

### 6.3 灌入 Agent 专属知识库（首次）

```powershell
.venv312\Scripts\python.exe scripts\rag\seed_agent_knowledge.py
```

幂等脚本：把 9 份年报语料灌入独立 collection `agent_knowledge`（约 5231 个点）。重复执行不会产生重复数据。

### 6.4 跑金标准评测（可选）

RAG 服务启动后：

```powershell
.venv312\Scripts\python.exe -m pytest tests/eval -q -m rag_service -s
```

等价于 `make eval`。20 条金标准用例，指标门槛 Recall@5 / MRR / 引用正确率，当前基线：20/20 命中，Recall@5=1.000、MRR=0.821、引用正确率=0.700。

---

## 7. 子 Agent 分流（P6）

主 Agent 只做路由与汇总：检索调研类任务委派给 **research** 子 Agent，沙箱文件编写委派给 **coding** 子 Agent。对使用者的影响：

- CLI 启动头部会列出「子 Agent：research（N 工具）、coding（M 工具）」；主 Agent 工具清单相应收缩（默认只保留时间与 remember + 两个委派工具）。
- 工具轨迹里子 Agent 的工具调用带 `[subagent:research]` / `[subagent:coding]` 前缀（Web 端 `tool_start` 帧含 `origin` 字段），与主 Agent 自己的调用可区分。
- **审批语义**：`delegate_to_research` 是只读档、直接执行不弹窗；`delegate_to_coding` 是写档、弹一次审批窗——你批准的是「整次委派」，批准后子 Agent 内部（如连续写多个文件）不再逐条打断，但每一步仍写审计并出现在轨迹里。

开关与子 Agent 定义全在 `configs/agents.yaml`（字段说明见文件顶部注释）。`enabled: false` 时主 Agent 恢复全工具形态（P5' 行为），无需改代码。

端到端验收脚本（真实 LLM + MySQL）：

```powershell
.venv312\Scripts\python.exe scripts\verify_p6_subagents.py
```

三项全查：闲聊不委派、调研走 research 免审批、写文件走 coding 审批一次 + 沙箱落盘 + 审计落库。

---

## 8. 配置参考

配置分两层：密钥与环境相关进 `.env`（不进版本库），行为策略进 `configs/*.yaml`（进版本库、可 review）。

### 8.1 .env 变量参考

| 分组 | 变量 | 说明 |
| --- | --- | --- |
| 主模型 | `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` / `LLM_TEMPERATURE` / `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | OpenAI 兼容协议；`LLM_API_KEY` 留空自动回退读系统环境变量 `DASHSCOPE_API_KEY` |
| 快模型 | `FAST_LLM_BASE_URL` / `FAST_LLM_API_KEY` / `FAST_LLM_MODEL` / `FAST_LLM_TEMPERATURE` | 用于摘要、记忆抽取等后台任务，省钱；地址留空回退主模型 |
| 备用模型 | `FALLBACK_LLM_*` | 主模型失败时由降级中间件切换 |
| MySQL | `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` / `MYSQL_POOL_SIZE` / `MYSQL_MAX_OVERFLOW` / `MYSQL_ECHO` | 会话、消息、审计、checkpoint、记忆 store 全量入库 |
| 知识库 | `KB_MODE`（死配置，勿用）/ `KB_COLLECTION` / `KB_STUB_DATA` / `RAG_MCP_URL` | 模式切换见 6.1；`RAG_MCP_URL` 默认 `http://127.0.0.1:8100/mcp` |
| 第三方 | `TAVILY_API_KEY` | 缺 Key 时联网搜索工具自动禁用而非启动报错 |
| 运行时 | `APP_ENV` / `LOG_LEVEL` / `LOG_JSON` / `DATA_DIR` / `DEFAULT_USER_ID` / `CORS_ORIGINS` | `CORS_ORIGINS` 默认放行 `http://localhost:3000`（前端） |

### 8.2 configs 目录

| 文件 | 管什么 |
| --- | --- |
| `agent.yaml` | 人设、说话风格、边界、限额（摘要触发阈值等） |
| `tools.yaml` | 工具白名单（`default_allow: false`，未登记一律禁用）、每个工具的权限档位、KB 模式开关、文件工具沙箱根目录（`data/workspace`，沙箱外路径一律拒绝） |
| `agents.yaml` | 子 Agent 分流（第 7 章）：总开关、主 Agent 保留清单、research / coding 子 Agent 的提示词与工具白名单 |
| `mcp.yaml` | MCP Server 清单：transport（stdio / streamable_http）、权限档位继承、工具白名单、超时。接入新 Server 只加一段配置，不改代码 |

### 8.3 权限档位

| 档位 | 行为 | 当前成员 |
| --- | --- | --- |
| `read` | 直接执行，仅审计 | 时间、计算器、联网搜索、目录列表、读文件、知识库检索、全部 MCP 工具、`delegate_to_research`（P6 委派，档位按子 Agent 工具集自动推导） |
| `write` | 执行前需审批 | `write_file`、`remember`、`delegate_to_coding`（P6 委派，批准一次即整体授权子 Agent 内部操作） |
| `danger` | 执行前需审批（更醒目的提示） | （预留档位，当前无成员） |

---

## 9. HTTP API 速览

后端（默认 8000 端口）对外提供三组端点，前端即基于它们工作：

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/v1/chat` | POST | SSE 流式对话。请求体：`message`（必填）、`session_id`（空则新建会话）、`user_id`。事件流包含 `tool_start` / `approval_required` / `approval_decided` / `tool_end` / `citation` / token 与错误事件 |
| `/v1/chat/{run_id}/approve` | POST | 审批回调。请求体：`call_id`、`decision`（`allow` / `deny`） |
| `/v1/sessions` | GET | 会话列表（thread_id、标题、消息数、更新时间） |
| `/v1/chat/completions` | POST | OpenAI 兼容端点。请求体：`model`、`messages`、`stream`（true 走 SSE 增量帧）、`session_id`、`user` |

OpenAI 兼容端点调用示例（可接任何 OpenAI SDK，把 base_url 指到本服务）：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "agent", "messages": [{"role": "user", "content": "你好"}], "stream": false}'
```

---

## 10. 测试与质量门禁

提交代码前跑一键门禁（ruff + mypy strict + 离线单测）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$py = ".venv312\Scripts\python.exe"
& $py -m ruff check backend tests scripts     # = make lint
& $py -m mypy                                  # = make typecheck（strict，54 个文件）
& $py -m pytest -q                             # 全量测试
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

```powershell
.venv312\Scripts\python.exe scripts\verify_p4_memory.py      # P4' 记忆系统
.venv312\Scripts\python.exe scripts\verify_p6_subagents.py   # P6' 子 Agent 分流
```

---

## 11. 常见问题

**`make` 命令不识别（The term 'make' is not recognized）**
本机未装 make。Makefile 只是命令清单，任何目标都能用 PowerShell 等价执行（见第 10 节示例），执行前设 `$env:PYTHONPATH = "backend"`（在仓库根直接跑 pytest 时 pyproject 已配置 pythonpath，无需手动设）。

**测试全部通过但退出码是 1，且输出大量 CLIXML / .pyc 写入错误**
`.venv312` 继承了 py312 的只读 site-packages，pytest 尝试写 `.pyc` 失败会把退出码改成 1。解决：执行前设 `$env:PYTHONDONTWRITEBYTECODE = "1"`（Makefile 第 6 行已全局导出）。判断成败以 `$LASTEXITCODE` 为准，别只看屏幕红字。

**模型请求 401**
密钥与网关不配套。`sk-sp-` 开头的 Key 只对 token-plan 网关有效，公共 dashscope 的 Key 不能用于 token-plan。检查 `LLM_BASE_URL` 与 `LLM_API_KEY` 是否成对。

**模型请求 404（model not found）**
token-plan 网关可用模型名由网关决定，且会随套餐变动；填网关上不存在的模型名会 404。

**模型请求 403（AccessDenied.Unpurchased）**
token-plan 个人版套餐到期或该模型不在套餐内，网关对所有模型统一返回 403 Unpurchased（2026-10 实测 `qwen3.8-flash` 已下架，`/v1/models` 列出的其他模型同样未购买不可用）。解决：续费套餐，或改 `.env` 换到公共百炼 dashscope（`LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1` + `LLM_MODEL=qwen-plus` + `sk-` 标准密钥），或换任何 OpenAI 兼容网关（如 DeepSeek：`https://api.deepseek.com/v1` + `deepseek-chat`）。

**启动日志出现 rag MCP 连接失败**
`scripts/rag/serve_mcp.py` 未启动，或 Qdrant / embedding 服务不可用。单个 MCP Server 连接失败会被隔离，不影响 CLI / Web 的其余功能；不需要真实知识库时把 `configs/tools.yaml` 的 `search_knowledge_base.mode` 改回 `stub`，或将 `configs/mcp.yaml` 中 rag 条目 `enabled: false`。

**改了 `.env` 的 `KB_MODE` 但知识库模式没变**
`KB_MODE` 是历史遗留死配置，无代码读取。实际开关在 `configs/tools.yaml` 的 `search_knowledge_base.mode`（见 6.1）。

**联网搜索工具不存在**
`TAVILY_API_KEY` 未配置时该工具自动禁用（设计如此，不报错）。配上 Key 重启即恢复。

**文件工具报「路径超出沙箱」**
读写文件类工具被限制在 `data/workspace` 沙箱内，`../` 与绝对路径一律拒绝。把目标文件放进沙箱，或改 `configs/tools.yaml` 的 `sandbox_root`。

**npm 命令不识别**
PowerShell 下用 `npm.cmd`（同理 `npx.cmd`）。

**前端请求后端报 CORS 错误**
`.env` 的 `CORS_ORIGINS` 默认只放行 `http://localhost:3000`。前端换端口后需同步修改并重启后端。

---

## 12. 目录速查

```
Multi_Agent/
├── backend/app/          # Python 主应用（cli.py 终端入口 / main.py Web 入口 / api/ HTTP 层）
├── frontend/             # Next.js 15 Web 前端
├── configs/              # agent.yaml / tools.yaml / agents.yaml（子 Agent 分流）/ mcp.yaml
├── scripts/              # init_mysql.py、rag/（serve_mcp.py、seed_agent_knowledge.py）、
│                         # verify_p4_memory.py、verify_p6_subagents.py
├── tests/                # unit（离线）/ integration / eval（RAG 金标准）
├── data/                 # kb_stub 语料 + workspace 沙箱
├── docs/                 # architecture.md（架构详解）、design.md、本文
├── .env                  # 密钥与环境变量（不进版本库，模板见 .env.example）
└── Makefile              # 常用命令入口
```

各模块职责、数据流水线、安全矩阵与已知差异清单，见 [architecture.md](./architecture.md)。
