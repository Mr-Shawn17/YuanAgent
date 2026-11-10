# backend 容器化 spike findings（wayfinder T06，2026-11-10）

> PROTOTYPE 结论存档。载体：T08 产物 Ubuntu2204 沙箱 Engine（docker-ce 29.9.3）；分支 `spike/backend-container`（一次性，勿合并）；本分支文件 `Dockerfile.spike` / `compose.spike.yml` / `.dockerignore` 即全部原型代码。

## 验证结果（四项全绿）

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 镜像构建 | ✅ 首建 12m51s / 增量 1-2min / 体积 **910MB**（python:3.12-slim + uv sync 清华源） | buildkit 日志 |
| 2 | 容器内探活 | ✅ 热启动 **10-15s** 出 `/health 200`（**比 Windows 宿主直跑的 60-90s 快 4-6 倍**——MCP 串行连接在容器内更快） | compose healthcheck |
| 3 | MySQL 跨界（WSL 容器→Windows DD 容器） | ✅ 容器内 asyncmy 实连 `winhost:3306` → `('8.0.46','agent')`；3306 绑 0.0.0.0，WSL 网关 172.28.144.1 直连，**防火墙未拦** | 容器内 SELECT VERSION() |
| 4 | runner WSS 全链路 | ✅ Windows runner → `localhost:8000`（WSL localhostForwarding）→ 容器：`WebSocket accepted` → `runner_registered`（设备令牌经共享 MySQL 验证）→ `runner_workspaces count=6` → 60s 后 `runner_dropped` 干净断开 | 服务端 local_hub 日志 |

附加绿灯：MCP 三件在容器内全通——mock（stdio 子进程拉起）✅；mcd/aihot 外网 MCP ✅（29+10 工具）；rag 变体禁用（快速失败不阻塞）✅。

## 坑清单（按严重度排序，spec 必须吸收）

1. **uv.lock 过期（2026-09-29）**：缺 `asyncmy`、`langgraph-checkpoint-mysql`、`langfuse`（均为 pyproject 后加依赖）。--frozen 构建直接缺包。**生产镜像前必须 `uv lock` 刷新**；本 spike 用显式 `uv pip install` 补丁绕过。
2. **pyproject extras 与启动现实不符**：`langmem`（memory extra）在 `MemoryExtractor.__init__` 启动即硬 import；`aiomysql`（`langgraph.store.mysql` 运行时依赖）完全未声明。**依赖审计**：这些要么进主依赖、要么改成真可选（启动时降级）。Windows .venv312 是 pip editable 全装所以从未暴露。
3. **Settings 无环境变量覆盖能力**（纯 BaseModel，无 SettingsConfigDict）：容器里改配置只能挂载 config 目录。spike 变体 `spike-config/`（mysql_host→winhost、rag 禁用）。生产形态必须解决密钥注入（mysql_password 明文在 yaml；`MYSQL_PASSWORD` env 回退仅对密码生效）——建议给 Settings 补 env 覆盖层。
4. **WSL 发行版自动休眠**：最后一个交互会话断开 → vmIdleTimeout 到点 → systemd 优雅下电 → docker 全停（ExitCode 0，容器不自动复活）。**WSL Engine 跑常驻服务必须钉住发行版**（keepalive 会话/任务计划）；真 Linux 服务器无此问题——这是 WSL 模拟的最大 prod-parity 差异。
5. **WSL DNS stub 间歇故障**（10.255.255.254 解析 mirror 域名失败，buildkit 对 mirror 顺序降级不健壮）：已在 `/etc/resolv.conf` 固定 223.5.5.5/119.29.29.29 + `generateResolvConf=false`。
6. **Windows 宿主 IP 不稳定**：WSL NAT 网关 IP（172.28.144.1）重启后可能漂移 → compose 的 `WINDOWS_HOST_IP` 每次需重注入（或改用 mirrored networking / 固定 netsh portproxy）。spec 需提供自动探测（`ip route show default`）。
7. **runner 无 stdout 日志**（logger 无 handler，info 全吞）：headless 运维时静默=成功、出错也难看。建议 runner 加最小 stdout/file handler。

## 复现命令

```bash
# WSL Ubuntu2204 内（Engine 已就绪，见 T08）
cd /mnt/d/VSCodeProject/ma-wf-spike
wsl.exe -d Ubuntu2204 -e sh -c 'exec sleep infinity'   # 先钉住发行版（坑4）
WINDOWS_HOST_IP=$(ip route show default | awk '{print $3}')  # 在 WSL 内探测
docker compose -f compose.spike.yml --env-file .env.spike up -d --build
# Windows 侧访问 http://127.0.0.1:8000/health；runner 直接 python -m runner run
```

## 对 T07 spec 的输入

- backend 容器化可行，生产 Dockerfile 以本 spike 为骨架 + `uv lock` 刷新 + 依赖审计 + 非 root 用户已就位
- 配置注入方案（坑3）与密钥管理需单列一节
- WSL keepalive 机制写进部署文档；真服务器部署无此节
- runner/服务端零代码改动即接入容器化 backend——「服务器薄」架构完整幸存
