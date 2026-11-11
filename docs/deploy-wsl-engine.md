# WSL 沙箱 Engine 部署指南（backend 容器化 + runsc 沙箱）

> 适用 spec：`docs/sandbox-query.md` §12（PR2）。运行形态：backend 以容器跑在 Ubuntu2204
> 沙箱 Engine 内，经 DooD socket 驱动 runsc 容器执行 git_query。
> 决策来源：wayfinder 图 `docs/wayfinder/2026-11-server-cli-sandbox/`（T05/T06/T08）。
> **真 Linux 服务器部署无 §3/§6 的 WSL 特有节，其余步骤相同。**

## 1. 拓扑

```
Windows 宿主                          Ubuntu2204（WSL2，Engine）
├─ YuanRunner（写路径，连 WSS）        ├─ yuan-backend 容器（backend，挂 docker.sock + mirrors rw）
├─ Docker Desktop：MySQL/Qdrant/      ├─ runsc 沙箱容器（git_query 专用，断网，每命令即焚）
│  Langfuse（基础设施）               └─ /var/lib/yuan-mirrors（Mirror 只读副本，bind 目录）
└─ 前端 dev（localhost:3000 → API）
```

- 写路径（clone 到工作区/commit/push）不变，仍走 YuanRunner（服务器薄，spec §1）。
- MySQL 由 Docker Desktop 承载，backend 容器经 `winhost`（WSL 网关 IP）跨界连接（T06 验证）。

## 2. 前提（T08 产物，已就位则跳过）

- Ubuntu2204 内 docker-ce 29.9.3 + runsc 已注册（`/etc/docker/daemon.json` 的
  `runtimes.runsc`；验证 `docker info | grep -A2 Runtimes`）。
- WSL DNS 固定（坑 5）：`/etc/resolv.conf` 写死 `223.5.5.5 / 119.29.29.29` 且
  `/etc/wsl.conf` 设 `generateResolvConf=false`——否则 buildkit 拉镜像间歇失败。
- systemd 已启用（`/etc/wsl.conf` 的 `[boot] systemd=true`）。

## 3. 钉住发行版（WSL 特有，坑 4——常驻服务必做）

WSL2 NAT 模式下最后一个交互会话断开 → vmIdleTimeout 到点 → systemd 优雅下电 →
**docker 全停，容器不自动复活**。Engine 跑常驻服务必须先钉住发行版：

```powershell
# Windows 计划任务（登录时触发，后台常驻 wsl 会话）：
schtasks /Create /TN "WSL-Engine-Keepalive" /SC ONLOGON /TR ^
  "wsl.exe -d Ubuntu2204 -u root --exec sleep infinity" /F
schtasks /Run /TN "WSL-Engine-Keepalive"
```

验证：`wsl -l -v` 显示 Ubuntu2204 `Running` 且退出 wsl 命令后 1 分钟仍 Running。

## 4. 一次性准备

```bash
# （Ubuntu2204 内执行；仓库位于 /mnt/d/VSCodeProject/Multi_Agent）

# 4.1 Mirror 目录（RepoSource clone/fetch 目标 + 沙箱 ro 挂载源）
mkdir -p /var/lib/yuan-mirrors && chmod 777 /var/lib/yuan-mirrors
#   chmod 说明：容器化形态由 appuser(uid 10001) 写入；Windows dev 形态经 \\wsl.localhost
#   的 9P 通道写入（映射非 root），777 是两种形态共用的最小放开。

# 4.2 沙箱镜像（git_query 硬依赖；缺失 = 工具报 sandbox_unavailable）
docker build -t yuan-sandbox-git /mnt/d/VSCodeProject/Multi_Agent/deploy/sandbox

# 4.3 采集 compose .env 参数
ip route show default | awk '{print $3}'        # → WINDOWS_HOST_IP（Windows 宿主，MySQL 所在）
getent group docker | cut -d: -f3               # → DOCKER_GID（docker.sock 属主组）
```

## 5. backend 容器化启动

```bash
cd /mnt/d/VSCodeProject/Multi_Agent/deploy/compose
cp .env.example .env && nano .env    # 填 GLM_API_KEY / WINDOWS_HOST_IP / DOCKER_GID
docker compose up -d --build         # 首建约 10-15 分钟（清华源 uv sync，增量 1-2 分钟）

curl -s http://127.0.0.1:8000/health   # 期望 {"status":"ok"}；容器内热启动实测 10-15s
```

配置注入（T06 坑 3 已实现 Settings env 覆盖层）：

- 密钥：`GLM_API_KEY` 经 `.env` 注入，不落 yaml；
- 容器态差异项：compose 已固定注入 `MYSQL_HOST=winhost`、`SANDBOX_QUERY__DOCKER_HOST=""`、
  `SANDBOX_QUERY__MIRROR_ROOT=""`、`SANDBOX_QUERY__MIRROR_ENGINE_ROOT=/var/lib/yuan-mirrors`
  （env 覆盖 dev 形态的同名字段；命名规则：字段路径大写、层级 `__` 连接）；
- 真实配置单文件挂载：`config/agent.yaml → /app/config/agent.yaml:ro`（该文件被
  .dockerignore 排除，密钥不进镜像层）。

runner 接入零改动：Windows 侧 YuanRunner 连 `wss://localhost:8000/ws/runner`
（WSL localhostForwarding 转发到容器，T06 验证全链路）。

## 6. git_query 冒烟验证

```bash
# 容器内直接驱动工具（不经 LLM）：验证 DooD、mirror、runsc 三环
docker exec yuan-backend python -c "
import sys; sys.path.insert(0, '/app/backend')
import asyncio
from app.core.config import load_settings
from app.tools.base import ToolContext
from app.tools.registry import build_tools, load_tools_config

settings = load_settings()
registry = build_tools(ToolContext(settings=settings), load_tools_config())
tool = next(t for t in registry.tools if t.name == 'git_query')
print(asyncio.run(tool.ainvoke({'repo_ref': 'Mr-Shawn17/YuanAgent',
                                'subcommand': 'log', 'argv': ['--oneline', '--max-count=5']})))
"
# 期望：[git log exit 0 · …ms · mirror /var/lib/yuan-mirrors/Mr-Shawn17/YuanAgent] + 提交历史
```

## 7. Windows dev 场景（backend 不容器化，.venv312 直跑）

适用：本机开发调试 git_query，backend 仍在 Windows、沙箱仍进 Ubuntu2204 Engine。

1. Engine 开 TCP（systemd drop-in，绑定 WSL 内网——NAT 模式不出宿主机）：

   ```bash
   # Ubuntu2204 (root)
   mkdir -p /etc/systemd/system/docker.service.d
   printf '[Service]\nExecStart=\nExecStart=/usr/bin/dockerd -H fd:// -H tcp://0.0.0.0:2375 --containerd=/run/containerd/containerd.sock\n' \
     > /etc/systemd/system/docker.service.d/tcp-dev.conf
   systemctl daemon-reload && systemctl restart docker
   ```

   验证（Windows 侧）：`curl http://127.0.0.1:2375/_ping` → OK。
   ⚠ 已知怪癖：**WSL localhost 转发中继在 dockerd 重启后可能失效**（TCP 通但被关闭）。
   兜底：直连发行版 IP（`hostname -I`），`docker_host` 配 `tcp://<IP>:2375`；IP 随重启漂移（坑 6）。

2. Mirror 目录：按 §4.1 准备；Windows 经 `\\wsl.localhost\Ubuntu2204\var\lib\yuan-mirrors` 直写。

3. `config/agent.yaml` 的 runtime 段：

   ```yaml
   sandbox_query:
     docker_host: "tcp://<Ubuntu2204-IP>:2375"   # localhost 转发失效时用 IP
     mirror_root: '\\wsl.localhost\Ubuntu2204\var\lib\yuan-mirrors'
     mirror_engine_root: /var/lib/yuan-mirrors
   ```

4. 钉住发行版（§3 同款，验收/调试期间必须；否则空闲挂起后连接被拒）。

## 8. 已知差异与坑（T06 清单，本仓库现状）

| 坑 | 现状 |
|---|---|
| 1 uv.lock 过期 | ✅ 已刷新（PR1/PR2），Dockerfile 直接 `uv sync --frozen` |
| 2 aiomysql/langmem 未声明 | ✅ 已升入主依赖（PR2） |
| 3 Settings 无 env 覆盖 | ✅ 已实现覆盖层（PR2，`_env_overrides`，仅声明字段路径可覆盖） |
| 4 WSL 发行版自动休眠 | ⚠️ 运维解：§3 keepalive；真服务器无此问题 |
| 5 WSL DNS stub 间歇故障 | ✅ resolv.conf 固化（一次性） |
| 6 Windows 宿主 IP / WSL IP 漂移 | ⚠️ 运维解：§4.3 每次重启后重探测；长期可评估 mirrored networking |
| 7 runner 无 stdout 日志 | 未做（headless 排障难），后续按需补最小 handler |
