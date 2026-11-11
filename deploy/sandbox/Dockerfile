# 沙箱基底镜像 yuan-sandbox-git（spec docs/sandbox-query.md §6）。
# alpine 固定版本 + git，无其他软件面：git_query 七件套只读命令的唯一执行环境。
# 构建位置必须是沙箱 Engine（Ubuntu2204 内 docker-ce，不是 Docker Desktop）：
#   wsl -d Ubuntu2204 -- docker build -t yuan-sandbox-git /mnt/d/VSCodeProject/Multi_Agent/deploy/sandbox
# 构建时 apk 从 alpine 3.22 仓库解析固定 git 版本（版本记录在构建日志，镜像可复现）。
# runsc 依赖 T08 已注册的 daemon.json runtime；镜像缺失时 git_query 装配/执行报
# sandbox_unavailable（无沙箱不执行，spec §2）。
FROM alpine:3.22
RUN apk add --no-cache git \
    && git --version
# 命令由 backend 以 argv 注入（["timeout","60","git",...]），工作目录经 bind 挂载
# 指向 /repo（只读）；rootfs 只读 + 断网 + tmpfs /tmp 由容器驱动统一施加。
WORKDIR /repo
