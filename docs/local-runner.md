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
- ~~CLI 命令执行工具（白名单 + 超时 + 输出封顶）~~ → **已落地，见 §13 命令沙箱**
- 本地 MCP Server 经执行器代理接入
- 托盘图标与开机自启引导
- WSL / Docker 命令后端（接口已预留，`CommandBackend` 协议新增实现即可）
- SSH git 认证（当前 HTTPS + PAT）

## 13. 命令沙箱（已落地）

### 13.1 工具清单（全部 danger 档 + mode: local，经审批/审计链路）

| 工具 | 用途 | 实现 |
|---|---|---|
| `run_command` | 工作目录内执行 CLI 命令 | 策略层 + 命令后端 |
| `delegate_coding` | 编程任务整段委派本机 codex CLI | `runner/coding.py`（用户自己的 codex 账号） |
| `git_clone` / `git_pull` / `git_push` | 私有仓库拉取/推送（凭证托管） | `runner/gitops.py` |
| （git add/commit/status 等） | 无专用工具，模型经 `run_command` 执行 | — |

### 13.2 架构分层

- `runner/command.py`：`CommandBackend` 协议 + 共用策略层——命令白名单（多命令段 `&&`/管道逐段查首 token）+ 危险模式黑名单（rm -rf 根目录 / del /s / format / reg / powershell -enc / taskkill /f 等）+ cwd 钉死 workspace（`resolve_in_sandbox`）+ **环境变量白名单**（敏感名二次过滤，绝不透传 API key）+ 超时 psutil 杀整棵进程树 + 输出封顶
- `runner/backends/shell.py`：ShellBackend（默认档，cmd.exe /d /c，零依赖）
- `runner/backends/windows_sandbox.py`：WindowsSandboxBackend（**VM 档**）——动态生成 .wsb（workspace 可写映射 `C:\ws` + bridge 与工具链只读映射）、沙箱内 PowerShell bridge 轮询映射文件夹任务队列、宿主 200ms 轮询取结果；会话懒启动、workspace 变化重建、空闲 10 分钟回收
- `runner/sandbox_tools.py`：便携工具链（PortableGit / python embeddable / node，npmmirror 镜像一次性下载到 `%LOCALAPPDATA%\YuanAgent\sandbox-tools\`，bridge 注入 PATH；也可手动放置并自建 `.ready` 跳过下载）
- backend 侧（CLI 场景直跑）：`backend/app/tools/builtin/shell.py|coding.py|gitops.py`，与 runner **同规则复制**（改动双向同步）

### 13.3 后端选择（runner.yaml `command.backend`）

- `auto`（默认）：Windows 沙盒功能已启用 → VM 档；否则策略层直跑
- `shell`：强制策略层
- `windows_sandbox`：强制 VM 档；功能未启用时**报错不静默降级**（避免误以为在 VM 中执行）

启用 Windows 沙盒：「启用或关闭 Windows 功能」勾选「Windows 沙盒」并重启（需 Win Pro+）。

### 13.4 git 私有仓库认证

- 凭证录入：`python -m runner set-git-cred github.com`（交互输入用户名 + PAT，存系统凭据管理器，仅本机）
- clone 按地址取 host 查凭证；pull/push 自动从 `remote.origin.url` 推断 host
- 凭证经 `GIT_ASKPASS` 临时脚本以环境变量注入（命令由 runner 拼装，LLM 只提供地址/分支）；PAT 从回传输出剥离；URL 字符白名单校验防注入
- **凭证永不进入**：模型上下文、命令行文本、工具输出、审计参数、服务端

### 13.5 已知边界

- VM 档映射文件夹小文件 I/O 慢（`npm install` 级操作明显变慢）；映射目录之外的宿主文件对沙箱不可见（隔离设计如此）
- git 凭证经任务 json 在映射文件夹短暂明文落盘（用后即删；本机磁盘级威胁不在模型内）
- VM 档会话绑定单一 workspace，变化即重建（启动约 10~20s）
- 沙箱为干净系统：宿主已装工具链不可见，依赖便携工具链（见 13.2）
