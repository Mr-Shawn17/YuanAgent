#!/usr/bin/env bash
# yuan-sandbox-net 创建与宿主防火墙出向封锁（spec v1.1 Q11，2026-11-16）
#
# 背景：OpenSandbox 命令执行是客户端直连沙箱内 execd（端口 44772），network_mode=none
# 与 internal 网络实测均使执行通道死路（断网与执行互斥）。断网由宿主侧实现：
#   1. 专用 bridge 网络 yuan-sandbox-net（enable_icc=false，固定 bridge 接口名）；
#   2. DOCKER-USER 链对 br-yuan-sandbox 入向 DROP：挡外网、跨容器访问发布端口、沙箱互访；
#   3. INPUT 链对 br-yuan-sandbox 入向 DROP：挡沙箱经网关直达宿主服务；
#   4. 宿主主动连入沙箱发布端口（执行通道）不受影响（不经上述两链的入向路径）。
#
# 前提：Engine 内 docker 可用（用户在 docker 组，无需 sudo）。幂等：重复执行无副作用。
# keepalive：WSL 重启后内核 iptables 状态清空，需在发行版拉起后重跑本脚本（见 README）。
set -euo pipefail

NET_NAME=yuan-sandbox-net
BRIDGE_IF=br-yuan-sandbox
HELPER_IMAGE=yuan-iptables-helper
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
RUNTIME_DIR="$SCRIPT_DIR/.runtime"

# 1) 专用 bridge（幂等）：固定 bridge 接口名，防火墙规则随之确定，不随 network id 漂移
if ! docker network inspect "$NET_NAME" >/dev/null 2>&1; then
  docker network create \
    --opt com.docker.network.bridge.name="$BRIDGE_IF" \
    --opt com.docker.network.bridge.enable_icc=false \
    "$NET_NAME"
  echo "已创建网络 $NET_NAME（bridge 接口 $BRIDGE_IF）"
fi

# 2) iptables 助手镜像（幂等构建）
if ! docker image inspect "$HELPER_IMAGE" >/dev/null 2>&1; then
  docker build -q -t "$HELPER_IMAGE" -f "$SCRIPT_DIR/Dockerfile.iptables-helper" "$SCRIPT_DIR"
fi

# 3) 出向封锁（自修复式安装）：先摘除本脚本的历史规则（防规则序错乱），再按
#    「放行 ESTABLISHED 回包 → DROP 新建出向」顺序安装。规则序必须 ACCEPT 在前，
#    否则执行通道（宿主主动连入沙箱发布端口）的返回路径被误杀（实测踩坑）。
docker run --rm --privileged --net=host "$HELPER_IMAGE" sh -c "
  while iptables -D DOCKER-USER -i $BRIDGE_IF -j DROP 2>/dev/null; do :; done
  while iptables -D DOCKER-USER -i $BRIDGE_IF -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null; do :; done
  while iptables -D INPUT -i $BRIDGE_IF -j DROP 2>/dev/null; do :; done
  while iptables -D INPUT -i $BRIDGE_IF -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null; do :; done
  iptables -I DOCKER-USER 1 -i $BRIDGE_IF -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A DOCKER-USER -i $BRIDGE_IF -j DROP
  iptables -A INPUT -i $BRIDGE_IF -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A INPUT -i $BRIDGE_IF -j DROP
"
echo "防火墙出向封锁已就位：DOCKER-USER / INPUT 对 $BRIDGE_IF 有状态 DROP（新建出向全断、执行通道回包放行）"

# 4) 渲染运行时配置：host_ip 每次启动重探（WSL IP 漂移，T06 坑 6 同源；可用环境变量
#    OPEN_SANDBOX_HOST_IP 覆盖，生产 backend 容器形态传 Engine 网关地址）；api_key 首次
#    生成后固化 .runtime/.api_key。compose 挂载渲染产物 .runtime/sandbox.toml。
mkdir -p "$RUNTIME_DIR"
if [ ! -s "$RUNTIME_DIR/.api_key" ]; then
  openssl rand -hex 24 > "$RUNTIME_DIR/.api_key"
  echo "已生成控制面 API key：$RUNTIME_DIR/.api_key（backend 侧 agent.yaml 需配同值）"
fi
HOST_IP="${OPEN_SANDBOX_HOST_IP:-$(hostname -I | awk '{print $1}')}"
sed -e "s/__HOST_IP__/$HOST_IP/g" \
    -e "s/__API_KEY__/$(cat "$RUNTIME_DIR/.api_key")/g" \
    "$SCRIPT_DIR/sandbox.toml.template" > "$RUNTIME_DIR/sandbox.toml"
echo "运行时配置已渲染：$RUNTIME_DIR/sandbox.toml（host_ip=$HOST_IP）"
