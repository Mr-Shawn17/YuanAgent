# opensandbox-server 部署指南（沙箱 Engine，Ubuntu2204 WSL）

> 适用 spec：`docs/opensandbox-migration.md` v1.1（本地文档，不入库；§10 装配、§11 部署差异、§7.4 Q11 断网实现）。
> 真实 Linux 服务器部署时，README 中 WSL 特有节（keepalive、localhost 转发）不适用，其余相同。

## 1. 拓扑（相对 docker-py 直连的差异）

```
Windows 宿主                              Ubuntu2204（WSL2，Engine）
├─ backend（dev: .venv312 / 生产: 容器）   ├─ yuan-opensandbox-server（compose，DooD 挂 docker.sock）
│    └─ 经 OpenSandbox SDK 调 server      │    └─ DOCKER_HOST → 本机 docker.sock
│       + 直连沙箱 execd（发布端口）       ├─ yuan-sandbox-exec 沙箱（runsc，会话级常驻）
├─ MySQL/Qdrant/Langfuse（Docker Desktop）│       网络 = yuan-sandbox-net（专用 bridge）
└─ YuanRunner（写路径不变）               └─ /var/lib/yuan-mirrors（mirror，沙箱 ro 挂载）
```

- docker.sock 控制面从 backend 移交 opensandbox-server；backend 只经 SDK 说话。
- 沙箱断网由宿主防火墙实现（Q11）：外网、其他容器、宿主服务、沙箱互访四向出向全断，
  仅保留宿主主动连入沙箱发布端口（execd 44772 执行通道）。

## 2. 一次性准备（Engine 内执行；仓库位于 /mnt/d/VSCodeProject/Multi_Agent）

```bash
# 2.1 沙箱网络 + 防火墙出向封锁（幂等，可重复执行）
bash /mnt/d/VSCodeProject/Multi_Agent/deploy/opensandbox/prepare-network.sh

# 2.2 沙箱镜像（execd 基底 + git/rg；digest 钉定见 §5；FROM 为远端镜像，上下文用小目录即可）
docker build -t yuan-sandbox-exec \
  -f /mnt/d/VSCodeProject/Multi_Agent/deploy/sandbox/Dockerfile.exec \
  /mnt/d/VSCodeProject/Multi_Agent/deploy/sandbox

# 2.3 mirror 目录（沿用 docs/deploy-wsl-engine.md §4.1，已就位则跳过）
mkdir -p /var/lib/yuan-mirrors && chmod 777 /var/lib/yuan-mirrors
```

## 3. 启动 server

```bash
cd /mnt/d/VSCodeProject/Multi_Agent/deploy/opensandbox
docker compose up -d

# 控制面探活（回环发布）
curl -s http://127.0.0.1:8080/health
# 列沙箱（确认 API 可用）
curl -s http://127.0.0.1:8080/v1/sandboxes
```

server 日志启动期会校验 runsc 可用性（`secure_runtime.docker_runtime=runsc` 缺失则拒绝启动，
该行为即 Q7 硬不变量的 server 侧保障之一）。

## 4. WSL keepalive（发行版钉住 + 规则固化）

发行版钉住（Windows 计划任务，T06 坑 4 的解法，未建必建——实测缺失时每次 wsl 命令唤醒
VM 都伴随 dockerd/server 重启，全部链路间歇失败）：

```powershell
schtasks /Create /TN "WSL-Engine-Keepalive" /SC ONLOGON /TR "wsl.exe -d Ubuntu2204 -u root --exec sleep infinity" /F
schtasks /Run /TN "WSL-Engine-Keepalive"
```

Engine 侧固化为开机自启（wsl.conf 的 boot command 以 root 执行，无需交互 sudo）：

```bash
# /etc/wsl.conf 追加（已存在则合并）：
# [boot]
# command = bash /mnt/d/VSCodeProject/Multi_Agent/deploy/opensandbox/prepare-network.sh
#           && cd /mnt/d/VSCodeProject/Multi_Agent/deploy/opensandbox && docker compose up -d
```

说明：iptables 是内核状态，WSL 重启后清空，必须开机重装（prepare-network.sh 幂等）；
server 容器 restart=unless-stopped 只覆盖 docker daemon 存活场景，WSL 重启后由 boot
command 重新拉起。真 Linux 服务器对应为 systemd unit 或 rc.local。

## 5. 镜像供应链（digest 钉定）

- execd 基底与 server 镜像均为 umbrella 版本，无 Cosign 签名（上游 issue #2463），
  身份锚点只有 BOM digest，两处均已钉定：
  - `deploy/sandbox/Dockerfile.exec` 的 FROM（execd @ sha256:fe7cc04a...）；
  - `deploy/opensandbox/docker-compose.yml` 的 image（server @ sha256:07f9b39a...）；
  - `deploy/opensandbox/sandbox.toml` 的 `runtime.execd_image`（同 execd digest）。
- 升级流程：三处同步换 digest → 重建 yuan-sandbox-exec → 重跑 TC-10~TC-15 回归。

## 6. backend 接入配置（config/agent.yaml 的 runtime.sandbox_query）

```yaml
sandbox_query:
  driver: opensandbox            # 回滚开关：切 docker-py 走旧直连路径
  opensandbox_server_url: http://127.0.0.1:8080
  idle_ttl_seconds: 300
```

- Windows dev：`http://127.0.0.1:8080`（WSL localhost 转发，若失效改 Engine IP），
  `sandbox.toml` 的 `host_ip=127.0.0.1` 使 execd 端点 URL 同走 localhost 转发。
- 生产 backend 容器：server 与 backend 共享一个 compose 外部网络（如 yuan-opensandbox-net），
  `opensandbox_server_url` 填服务名；`host_ip` 改配 Engine 网关地址（如 host.docker.internal
  对应的 host-gateway），使 endpoint URL 从 backend 容器可达。
- 回滚：`driver: docker-py` + 停 server（`docker compose down`），旧直连路径不受影响。

## 7. 验证清单（对应功能测试方案）

| 项 | 命令/方法 | 期望 |
|---|---|---|
| server 控制面 | `curl http://127.0.0.1:8080/health` | 200 |
| 沙箱网络归属 | `docker inspect <sandbox容器> --format '{{.HostConfig.NetworkMode}}'` | yuan-sandbox-net |
| gvisor 生效 | 同上 `{{.HostConfig.Runtime}}` | runsc |
| pids 限制 | 同上 `{{.HostConfig.PidsLimit}}` | 64 |
| hardening | 同上 CapDrop / SecurityOpt | 九项 cap + no-new-privileges |
| 四向出向封锁 | TC-14（经 execd 注入验证） | 外网/跨容器/宿主/跨沙箱全不可达 |
| 执行通道 | 宿主 curl 沙箱发布端口 /ready | initialized:true |
