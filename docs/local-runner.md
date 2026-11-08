# 本地执行器（Local Runner）实施方案

> 版本：v1（2026-11-04）
> 状态：已确认口径，按本方案实施
> 定位：中心服务 + 用户 Windows 电脑上的本地执行器，让 coding 子 Agent 的文件工具真正落到用户本机磁盘

## 1. 背景与目标

当前 `read_file` / `write_file` / `list_directory` 由后端进程直接操作服务器上的 `data/workspace` 沙箱。多人使用后，用户期望 Agent 读写的是自己电脑上的文件，而不是服务器沙箱。

目标形态：

- 中心服务统一部署（FastAPI + MySQL + RAG + MCP），负责 Agent 编排、审批、审计
- 每个用户电脑上运行一个无窗口后台执行器（YuanRunner.exe），双击启动，保持 WSS 长连接
- 文件工具调用经中心服务转发到该用户的执行器，在用户本机沙箱内执行后回传结果

已确认的三条口径：

| 口径 | 决定 |
|---|---|
| 用户体系 | 单用户 + 设备令牌先行（devices 表含 user_id 字段，为多用户预留） |
| 工具接入 | 方案 A：现有工具加 mode 开关，工具名不变，模型无感知；交付配置默认 local |
| 交付形态 | runner/ 源码 + PyInstaller 打包脚本（--onedir --windowed）一起交付 |

## 2. 范围

### 2.1 第一版做什么

- devices 表 + 设备令牌签发/吊销 CLI 子命令
- 中心服务新增 /ws/runner WebSocket 端点 + LocalTaskHub（连接注册、任务下发、结果回传、超时）
- 文件三工具支持 mode: server|local，local 时经 hub 转发
- runner/ 独立包：WSS 客户端（重连 + 心跳）、三件套执行器 + 沙箱检查、单实例锁、文件日志、request_id 幂等
- PyInstaller spec 与构建说明
- 单测 + 端到端联调脚本 scripts/verify_local_runner.py

### 2.2 第一版不做什么

- 不做多用户登录/JWT/前端登录页（user_id 字段预留）
- 不做 CLI 命令执行、本地 MCP 转发（二期）
- 不做执行器侧二次审批（write 档审批仍在中心服务，批准即整体授权，与子 Agent 委派同思路）
- 不做容器级隔离（沿用路径包含检查 + 审批 + 审计的安全模型）
- 不做 Redis / 多实例部署（单实例 + 内存态 hub，够当前规模）

## 3. 总体架构

```text
用户浏览器（Web 前端）
   │ SSE / HTTP
   ▼
中心服务（FastAPI :8000）
   ├─ AgentRuntime ── build_run_agent ── 工具装配
   │     └─ ToolContext.local_router = LocalTaskHub
   ├─ ToolPermissionMiddleware：write 档审批（Web 弹窗）先于工具执行
   ├─ LocalTaskHub：request_id → Future，按 user_id 找连接
   └─ /ws/runner：设备令牌鉴权，注册连接，双向 JSON 消息
              ▲
              │ WSS（执行器主动外连，无需开端口）
              │
用户电脑 ── YuanRunner.exe（python -m runner）
   ├─ 配置：%LOCALAPPDATA%\YuanAgent\runner.yaml（server_url、workspace）
   ├─ 令牌：Windows 凭据管理器（keyring）
   ├─ 执行器：read_file / write_file / list_directory + 沙箱检查
   └─ 单实例锁 + 文件日志 + 断线重连 + request_id 去重
```

执行链路（以 write_file 为例）：

```text
coding 子 Agent 调用 write_file
→ 中心 ToolPermissionMiddleware 按 write 档弹审批
→ 用户批准 → 工具检测 mode=local 且 hub 在线
→ hub 生成 request_id，经 WS 发 tool_request
→ 执行器沙箱检查 → 写用户本机文件 → 回 tool_result
→ hub resolve Future → 工具返回结果文本 → 审计照常落库
```

## 4. 通信协议（JSON over WSS）

### 4.1 执行器 → 中心

```json
{"type": "hello", "device_id": "dev_xxx", "client": "yuan-runner/0.1"}
{"type": "ping"}
{"type": "tool_result", "request_id": "req_xxx", "ok": true, "output": "已写入 12 个字符到 demo.txt"}
{"type": "tool_result", "request_id": "req_xxx", "ok": false, "error": "禁止访问沙箱之外的路径：../x"}
```

### 4.2 中心 → 执行器

```json
{"type": "pong"}
{"type": "tool_request", "request_id": "req_xxx", "tool": "write_file",
 "args": {"path": "demo.txt", "content": "...", "mode": "overwrite"},
 "session_id": "s_xxx", "user_id": "me", "timeout_seconds": 120}
{"type": "cancel", "request_id": "req_xxx"}
```

约定：

- 鉴权：连接时 HTTP 头 `Authorization: Bearer <设备令牌>`，校验失败服务端关闭连接（code 4401）
- request_id 由中心生成（uuid4），执行器保留近期 id 的结果缓存做幂等：重复 id 直接回传缓存结果
- 工具白名单在执行器侧硬编码为三个文件工具，未知 tool 名直接拒绝
- 心跳间隔 30s，连续 3 次未收 pong 判定断线并重连（指数退避 1s → 60s）

## 5. 中心服务改造（文件级）

| 文件 | 改动 |
|---|---|
| backend/app/db/models.py | 新增 Device 表：id(uuid7 PK) / user_id / name / token_hash(sha256 唯一) / created_at / last_seen_at / revoked |
| migrations/versions/0003_add_devices.py | 建表迁移 |
| backend/app/db/device_repo.py | DeviceRepo：create / get_by_token_hash / list / revoke / touch_seen |
| backend/app/cli.py | 新增 device 子命令组：create（令牌只显示一次）/ list / revoke |
| backend/app/tools/base.py | 新增 LocalRouter 协议（async dispatch(tool, args, timeout) → str）；ToolContext 加 local_router 字段 |
| backend/app/api/local_hub.py | LocalTaskHub：连接注册表、dispatch 创建 Future、结果/断线/超时处理；无在线连接时返回可读错误文本 |
| backend/app/api/routes_runner.py | /ws/runner：令牌鉴权 → hello 注册 → 消息循环 → 断线清理 |
| backend/app/api/runtime.py | AgentRuntime 持有 hub；build_run_agent 注入 ToolContext.local_router |
| backend/app/api/main.py | lifespan 创建 hub、挂 runner 路由 |
| backend/app/tools/builtin/filesystem.py | 三工厂支持 entry.mode=local 且 router 可用 → 转发版工具；否则保持直连磁盘版 |
| backend/app/core/config.py | Settings 新增 local_runner_enabled / local_dispatch_timeout / local_heartbeat_seconds |
| config/tools.yaml(.example) | 三个文件工具显式 mode: local |
| config/agent.yaml.example | runtime 段新增 local_runner 三键说明 |

关键设计点：

- mode 为空或 server → 行为与现在完全一致（向后兼容，老配置不炸）
- CLI 入口不注入 hub → 即使 tools.yaml 写了 local，CLI 也直接读写本机（CLI 进程本来就在用户机器上，语义等价）
- 转发版工具保持同名同参数同档位，coding 子 Agent 白名单与路由提示词零改动
- hub 在线检查在工具调用时进行：执行器离线返回 `[local_runner_offline] ...` 文本，模型可转告用户，不抛异常炸图

## 6. 执行器设计（runner/ 独立包）

```text
runner/
├── __init__.py        # 版本号
├── __main__.py        # 入口：解析参数 → asyncio.run(main())
├── config.py          # %LOCALAPPDATA%\YuanAgent\runner.yaml 加载与默认值
├── auth.py            # keyring 存取设备令牌（环境变量 YUAN_RUNNER_TOKEN 兜底）
├── protocol.py        # Pydantic 消息模型（与服务端独立维护，双端解耦）
├── connection.py      # WSS 客户端：鉴权头、心跳、指数退避重连
├── executor.py        # 三件套执行 + resolve_in_sandbox 同款越界检查
├── singleton.py       # 单实例锁：Windows msvcrt.locking，POSIX fcntl.flock
├── logging_setup.py   # 文件日志（%LOCALAPPDATA%\YuanAgent\logs\runner.log，轮转）
└── packaging/
    └── YuanRunner.spec  # PyInstaller：--onedir --windowed，隐藏导入处理
```

约束：

- 依赖仅 websockets / pydantic / keyring，不引 langchain（打包体积与升级解耦）
- 打包：`pyinstaller runner/packaging/YuanRunner.spec` → dist/YuanRunner/YuanRunner.exe
- 以当前登录用户普通权限运行，不做 Windows 服务
- request_id 结果缓存上限 256 条（LRU），防重放重复执行

## 7. 安全设计

1. 令牌：secrets.token_urlsafe(32)，前缀 yrt_，库内只存 SHA-256；泄露可 revoke
2. 传输：生产建议 wss:// + 反代 TLS；内网 http 可接受（与现状 SSE 一致）
3. 执行器侧二次校验：工具白名单、路径包含检查、write 结果只回文本摘要不回读全文（由中心按需另调 read_file）
4. 审批：write 档在中心审批一次 = 整体授权本次工具调用（与 delegate 整体授权同模型），执行器不再逐条打断
5. 审计：中心 tool_audit 照常全量记录；执行器本地日志含 request_id、session_id、工具、结果状态
6. 单实例：重复双击直接退出新进程

## 8. 实施步骤（对应任务清单）

1. 方案文档（本文档）
2. Device 表 + 迁移 + DeviceRepo + CLI device 子命令
3. LocalTaskHub + /ws/runner 端点 + runtime/main 接线
4. 文件工具 mode 分支 + tools.yaml / agent.yaml.example 配置
5. runner/ 包完整实现
6. PyInstaller spec + 构建说明（并入本文档 §9）
7. 单测（hub 转发/超时/离线、工具 mode 分支、执行器沙箱拒绝）+ verify_local_runner.py 联调

## 9. 打包与部署

```bash
# 构建前安装执行器依赖
pip install -e ".[runner]"
# 打包（Windows）
pyinstaller runner/packaging/YuanRunner.spec
# 产物
dist/YuanRunner/YuanRunner.exe
```

用户机上线三步：

```bash
# ① 中心签发设备令牌（只显示一次，复制保存）
python -m app.cli device create --name my-laptop
# ② 写用户机配置 %LOCALAPPDATA%\YuanAgent\runner.yaml
server_url: ws://<中心地址>:8000/ws/runner
workspace: D:\user-workspace
# ③ 令牌写入凭据管理器后双击 YuanRunner.exe
python -m runner set-token yrt_xxx   # 或安装包引导
```

## 10. 验收标准

- [ ] 双实例启动：第二个进程立即退出，日志留痕
- [ ] 执行器离线时 Web 聊天可用，write_file 返回 offline 提示文本，不炸图
- [ ] 在线时 write_file 经审批 → 用户本机文件被写入 → 审计与执行链路正常
- [ ] 越界路径（../、绝对路径）在执行器侧被拒绝并回传错误
- [ ] 同一 request_id 重发不重复执行
- [ ] make check（ruff + mypy strict + 离线单测）通过

## 11. 实际启动与验证（本机闭环，已验证）

三进程协同启动后即可「网页对话 → 审批 → 本机文件落盘」：

```bash
# ① 后端（:8000，含 /ws/runner 端点）
python scripts/start_backend.py
# ② 前端（:3000，Next rewrites 代理 /v1/* 到后端）
cd frontend && npm run dev
# ③ 本地执行器（令牌已在凭据库，工作目录 ~/YuanAgent-workspace）
python -m runner run        # 或双击 dist/YuanRunner/YuanRunner.exe
```

验证链路：浏览器打开 http://localhost:3000 → 对话请求写文件 → 审批弹窗点允许
→ 文件落到用户电脑 `C:\Users\<user>\YuanAgent-workspace\`（runner 日志有 `tool_done tool=write_file`）。

关键接线（本期已修复）：
- 文件工具 `mode: local` 依赖 `ToolContext.local_router`，该注入必须在 `build_tools` 之前
  完成（`runtime.build_run_agent`），否则 mode=local 静默退化为服务器直连。
- `/ws/runner` 是 WebSocket 端点，不能走 `Depends(get_runtime)`（该依赖需 `Request`，
  WebSocket 上下文注入失败报 500），改从 `websocket.app.state.runtime` 取运行时。

## 12. 二期展望（不在本期）

- 多用户登录 + 按用户路由到各自执行器（hub 已按 user_id 建键，扩展点就绪）
- CLI 命令执行工具（白名单 + 超时 + 输出封顶）
- 本地 MCP Server 经执行器代理接入
- 托盘图标与开机自启引导

## 13. 开发工作流四件套（2026-11-08 新增）

执行器白名单从文件三件套扩展为 7 个工具，新增：`git_clone`（幂等 clone/pull）、
`install_deps`（固定模板装依赖）、`delegate_coding`（委派本机 codex CLI）、
`git_commit_push`（只推回 clone 登记的 remote）。要点：

- 命令全部由 runner 固定 argv 拼装，模型只传结构化参数，无自由命令；
- git 凭证：`python -m runner set-git-cred <host>` 录入 keyring（服务名
  YuanAgentRunner），执行时 `GIT_ASKPASS` 注入 + 输出剥离 PAT；
- 全 danger 档：审批在中心服务，弹窗展示将执行的操作；
- 配置新增 runner.yaml 三段：`git`（allowed_hosts/clone_depth）、`deps`（python）、
  `codex`（enabled/path/timeout_seconds）；
- 服务端新增 `local_devtools_dispatch_timeout` / `local_codex_dispatch_timeout`
  两个 Settings 键（git/deps 默认 600s、codex 默认 900s，覆盖原 120s 全局超时）。

离线自检：`python scripts/verify_codex_tools.py`（15 项）；
在线端到端：`python scripts/verify_codex_tools.py --online`（需代理 + codex 就绪）。
