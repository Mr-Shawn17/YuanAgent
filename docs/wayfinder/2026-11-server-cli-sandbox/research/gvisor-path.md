# T01 调研：gVisor（runsc）落地路径

- 票号：wayfinder T01「gVisor 落地路径研究」（research）
- 调研日期：2026-11-10
- 范围：runsc 作为「服务端容器内只读命令（git show/log/diff、grep/ripgrep 等）」防逃逸运行时，在本机（Win11 + Docker Desktop WSL2 后端 + Ubuntu2204 发行版）的可行载体与落地路径
- 非目标：本文只呈现事实与依据，**不做最终选型决策**（选型归 grilling 票 / T05）

## 0. 结论速览（事实层面）

1. Docker Desktop（Windows）**至今没有官方支持**的自定义 OCI runtime 路径：feature request [docker/desktop-feedback#364](https://github.com/docker/desktop-feedback/issues/364)（2026-05-09 提出，仍 open，无 Docker 官方回应）。社区存在两类 hack（往 VM 内塞 runsc + 每次启动后补丁 daemon 配置），均非持久、非官方。
2. gVisor 官方 Docker 指南只面向 Linux 宿主（要求 Docker ≥ 17.09、Linux 内核 ≥ 5.6）；本机 WSL2 内核 6.18.33.2 满足内核要求，**在 Ubuntu2204 发行版内装原生 docker-ce + runsc 是官方文档直接覆盖的路径**。
3. runsc 二进制获取：npmmirror（`registry.npmmirror.com/-/binary/`）**没有** gvisor/runsc 镜像；官方渠道是 GCS bucket + apt（apt 源也挂在 storage.googleapis.com）；GitHub Releases **现已附带** `gvisor-x86_64.tar.zstd/.bz2` 资产与 SHA256/SHA512 校验文件（最新 release-20261102.0），可走 GitHub 代理前缀下载。
4. 平台选型在本机没有悬念：无 `/dev/kvm` → KVM 平台不可用；systrap 是 2023 年中以来的默认平台（ptrace 已弃用）；官方性能指南明确建议 "users should use Systrap for best performance in most cases"。
5. 性能量级：gVisor collaborator 认为当前启动耗时 ~300ms 是合理估计；syscall 微基准开销 2–100 倍（第三方数据），CPU 密集任务无额外开销；大目录冷遍历（ripgrep 类）经 gofer 有可观开销（官方正在优化，PR #17274 冷跑 -80%）。

## 1. 本机环境基线（票面实测事实）

| 项 | 值 |
|---|---|
| OS | Windows 11 10.0.26200 |
| Docker Desktop | client 29.8.0，WSL2 后端，守护进程当前未运行（用户手动启停） |
| DD 数据目录 | `D:\ProgramData\Docker\wsl\DockerDesktopWSL` |
| WSL | 2.7.10，内核 `6.18.33.2-microsoft-standard-WSL2` |
| 发行版 | docker-desktop + Ubuntu2204；两者均无 `/dev/kvm`（无 KVM） |
| hypervisor | 已激活 |

对照 gVisor 官方要求「supports x86_64 and ARM64, and requires Linux 5.6+」（[安装文档](https://gvisor.dev/docs/user_guide/install/)）：WSL2 内核 6.18 满足；无 KVM 排除 KVM 平台。

## 2. 事实清单（按票面子问题）

### S1 Docker Desktop（Windows）对 runsc 的支持现状

- **F1.1 官方不支持，且无版本支持过。** [docker/desktop-feedback#364](https://github.com/docker/desktop-feedback/issues/364)（2026-05-09 开，由 docker/roadmap 转入，open，无官方回应）：「Docker Desktop for Windows does not appear to provide a supported, documented way to install and persist a custom OCI runtime such as gVisor runsc」。截至 2026-11-10 无 assignee、无 milestone。→ 「哪个版本起」的答案：**没有任何版本起**。
- **F1.2 gVisor 官方 Docker 指南不含 Docker Desktop 路径。** 指南面向 Linux 宿主：「This guide requires Docker version 17.09.0 or greater」（[gVisor Docker 指南](https://gvisor.dev/docs/user_guide/docker/)），全文未提 Windows/Docker Desktop。
- **F1.3 daemon.json 的官方配置面。** Docker Desktop GUI 的 Settings → Docker Engine 面板可编辑 JSON：「Configure the Docker daemon using a JSON configuration file… located at `$HOME/.docker/daemon.json`」（[DD settings 文档](https://docs.docker.com/desktop/settings/)）。DD 内 VM 中 dockerd 实际以 `--config-file /run/config/docker/daemon.json` 启动（[rimelek 实测](https://dev.to/rimelek/using-gvisors-container-runtime-in-docker-desktop-374m)）。
- **F1.4 核心障碍：DD 的 VM rootfs 只读，runsc 没有可持久落盘的位置。** 社区方案把 runsc 放进 Docker volume：`runsc-runtime-binaries` → VM 内 `/var/lib/docker/volumes/runsc-runtime-binaries/_data/`，再让 daemon.json 的 `runtimes.runsc.path` 指向它（[rimelek](https://dev.to/rimelek/using-gvisors-container-runtime-in-docker-desktop-374m)）。
- **F1.5 Windows 下不持久：每次重启 Docker Desktop 都要重打补丁。** rimelek：「you will have to run the installer every single time you restart Docker Desktop」；且菜单 Restart 不生效，需完全 Quit 后重开（macOS/Linux 场景）；错误形态包括 `unknown or invalid runtime name: runsc`、shim 报 `no such file or directory`（同上）。
- **F1.6 NVIDIA/GPU 运行时注入会整体覆盖 runtimes 段。** Windows+WSL2 下 DD 注入 NVIDIA runtime 配置时「completely overrides the 'runtimes' section in the daemon config instead of adding itself」，导致 GUI 里看到的 runsc 实际不可用（[rimelek](https://dev.to/rimelek/using-gvisors-container-runtime-in-docker-desktop-374m)）。本机无 NVIDIA GPU，此坑不触发，但说明了 DD 对 runtimes 段的再生产行为不受控。
- **F1.7 features-overrides.json 不是 runtime 注入通道。** 它只用于开关 DD 内部实验特性（如 WhaleMenuRedesign、IdleShutdown），路径在 DD 配置目录，属社区/博客文档化机制而非正式文档，从未提供「注入自定义 OCI runtime/任意二进制」的能力（[Docker Blog 4.5](https://www.docker.com/blog/new-docker-menu-improved-release-highlights-with-docker-desktop-4-5)、[docker/for-win#12573](https://github.com/docker/for-win/issues/12573)、[docker/desktop-feedback#142](https://github.com/docker/desktop-feedback/issues/142)）。
- **F1.8 2026 年的自动化 workaround。** [diogovalada/gvisor-docker-desktop-windows](https://github.com/diogovalada/gvisor-docker-desktop-windows)（unofficial PowerShell 工具包，#364 提交者的自愈方案）：Windows 计划任务每 10 分钟校验，installer 容器（`docker run`，`--pid=host` + bind mount daemon.json）向 DD 发行版注入 runsc 并 patch daemon 配置；幂等、带 SHA-512 校验；作者明示「Docker updates may wipe the changes」需自愈，且不推荐 `-DefaultRuntime` 全局接管。
- **F1.9 已知用户失败样例。** Reddit：「runsc is visible on PATH inside WSL but Docker Desktop doesn't recognize it」（[r/docker](https://www.reddit.com/r/docker/comments/1fm8hlk/docker_desktop_will_not_recognize_gvisor/)）——DD 的 daemon 在自己的 docker-desktop 发行版里，宿主/Ubuntu 侧 PATH 上的 runsc 对它不可见。

### S2 WSL 发行版内装原生 Docker Engine + runsc（备选载体）

- **F2.1 标准步骤（官方文档直接覆盖）。** Ubuntu 内按 [Docker Engine 官方安装文档](https://docs.docker.com/engine/install/ubuntu/)：卸载冲突包 → 加 Docker GPG key/keyring → 加 apt repo → 安装 `docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin` → `docker run hello-world` 验证。然后加 gVisor apt 源装 runsc（见 F3.2）或手动放置二进制到 `/usr/local/bin/runsc`，`sudo runsc install` 写入 `/etc/docker/daemon.json` 并 `systemctl restart docker`（[gVisor Docker 指南](https://gvisor.dev/docs/user_guide/docker/)）。社区已有 WSL2 Ubuntu 全流程落地记录（[romanklis/openclaw-contained GVISOR_SETUP.md](https://github.com/romanklis/openclaw-contained/blob/main/docs/GVISOR_SETUP.md)）。
- **F2.2 官方对「同一发行版共存」的立场是警告。** DD WSL 文档：「Before installing Docker Desktop, uninstall any version of Docker Engine or the Docker CLI that was installed directly inside a WSL Linux distribution. Running both can cause conflicts.」（[DD WSL 文档](https://docs.docker.com/desktop/wsl/)）。注意措辞是"inside a WSL Linux distribution"——即**不要在同一个发行版里同时有 DD 集成与原生 engine**。
- **F2.3 共存的关键开关：按发行版关闭 WSL integration。** Settings → Resources → WSL Integration 可逐发行版启停；开启「gives you direct access to `docker` commands from that distribution's terminal」，关闭后该发行版终端里的 `docker` 不再指向 DD（[DD WSL 文档](https://docs.docker.com/desktop/wsl/)）。Ubuntu2204 内跑原生 engine 时应关闭其 integration，`docker` CLI 便走发行版内本地 socket，与 Windows 侧 `docker.exe`（走 DD context）互不干扰。
- **F2.4 端口/资源冲突面（机制推断 + 官方留白）。** 官方 WSL 文档未讨论端口转发细节。机制上：两套 daemon 各在各的发行版文件系统内（各自的 `docker.sock`/数据目录）；DD 默认不监听 2375/2376（仅显式开启 "Expose daemon on tcp://localhost:2375" 时）；所有 WSL2 发行版共享同一个 utility VM 的内存/CPU 池（受 `.wslconfig` 总量约束）——即**资源是叠加计费，端口仅在两侧容器发布同一宿主端口时冲突**。（此条为基于官方文档空白的机制说明，非直接引用。）
- **F2.5 WSL 特有的 runsc 已知问题。** gVisor issue 检索（2026-11-10，GitHub API）：[#17066](https://github.com/google/gvisor/issues/17066)（open，2026-11）：WSL2 宿主时钟步进导致 `/proc/[pid]/stat` starttime 与 `/proc/uptime` 跟随 realtime clock，曾致容器 PID 1 被杀——WSL 特定触发；[#11370](https://github.com/google/gvisor/issues/11370)（closed）：初次在 WSL 装 runsc 报 runtime path 缺失，属配置问题。无「runsc 在 WSL2 原理上不可用」的报告。
- **F2.6 systemd 前提。** Ubuntu2204 内 `systemctl enable docker` 需要 WSL systemd 支持（`/etc/wsl.conf` 中 `[boot] systemd=true`，WSL 0.67.6+；[Microsoft WSL systemd 文档](https://learn.microsoft.com/en-us/windows/wsl/systemd)）。否则需手动/脚本拉起 dockerd。

### S3 runsc 二进制的国内获取方式

- **F3.1 npmmirror 没有 gvisor。** 实测抓取 `https://registry.npmmirror.com/-/binary/` 全量列表（2026-11-10）：`g` 开头仅有 `geckodriver/ git-for-windows/ gl/ grpc-tools/ grpc/ gifsicle-bin/ guetzli-bin/`，**无 gvisor/runsc**。票面假设「npmmirror 可替代」不成立。
- **F3.2 官方渠道全部指向 GCS。** apt 源：`deb [signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main`（key 在 `https://gvisor.dev/archive.key`）；tarball 直链：`https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/gvisor.tar.zstd`；`runsc install` 的 sidecar 自动下载也走 storage.googleapis.com，且该自动下载机制将于 2026 年 9 月底移除（[gVisor 安装文档](https://gvisor.dev/docs/user_guide/install/)）。GCS 域名在本机网络通常不可直连（票面：GitHub 直连亦不稳）。
- **F3.3 GitHub Releases 现已附带二进制资产（可走代理）。** 最新 release-20261102.0（2026-11-02）资产含 `gvisor-x86_64.tar.zstd`（58,844,112 B）、`gvisor-x86_64.tar.bz2`（75,332,313 B）、`SHA256SUMS`、`SHA512SUMS` 等 6 个文件，下载 URL 形如 `https://github.com/google/gvisor/releases/download/release-20261102.0/gvisor-x86_64.tar.zstd`（[GitHub Releases](https://github.com/google/gvisor/releases)，经 [GitHub API](https://api.github.com/repos/google/gvisor/releases/latest) 核实）。GitHub 直连不稳时，此类 release 资产 URL 可加社区 gh 代理前缀（如 gh-proxy 类镜像）下载，再以官方 SHA 校验。注意：2024-11 时 rimelek 曾记录 GitHub releases 页「shows that there is no release at all」——资产是后来才补挂的，属较新变化。
- **F3.4 tarball 内容即 runsc（+ shim 等组件）。** 安装文档的 tarball 安装路径为解压后放置 `/usr/local/bin` 并 `runsc install`（[gVisor 安装文档](https://gvisor.dev/docs/user_guide/install/)）。

### S4 runsc 平台选择与网络模式

- **F4.1 平台格局。** 官方支持 KVM / systrap / ptrace 三种；「systrap replaced ptrace as the default gVisor platform in mid-2023」；ptrace「is no longer supported and is expected to eventually be removed entirely」；KVM「will provide the best performance」但仅适合裸机，VM 内「the systrap platform will often provide better performance…due to the overhead of nested virtualization」，并推荐 systrap「when running inside a VM, or on a machine without virtualization support」（[Platforms 文档](https://gvisor.dev/docs/architecture_guide/platforms/)）。本机两个发行版均无 `/dev/kvm` → **KVM 平台不可用，systrap（默认）是唯一现实选择**；性能指南亦明确「users should use Systrap for best performance in most cases」（[性能指南](https://gvisor.dev/docs/architecture_guide/performance/)）。systrap 机制为 seccomp `SECCOMP_RET_TRAP` + SIGSYS 拦截。
- **F4.2 网络模式三种。** 默认 `--network=sandbox`（自研 netstack，全部网络处理在 Sentry 内，loopback 也在沙箱内）；`--network=host` 直接用宿主网络栈，「this mode decreases the isolation to the host」；`--network=none`「To completely isolate the host and network from the sandbox, external networking can be disabled」，沙箱内仅剩 netstack 提供的 loopback（[Networking 文档](https://gvisor.dev/docs/user_guide/networking/)）。
- **F4.3 对「断网 / 出口白名单」的含义。** 断网最直接：docker 层 `docker run --network none`（不给沙箱配外接网卡）或 runtimeArgs 加 `--network=none`。出口白名单：runsc **没有内置 L3/L4 出口过滤开关**——文档只提供实验性 `--network-proxy-path`（外部 L4 proxy，「The external peer is responsible for any egress filtering」，仅 sandbox 模式）与 qdisc TBF 限速（`--qdisc=tbf --qdisc-tbf-rate --qdisc-tbf-burst`）；沙箱内 iptables 仅部分支持（[Networking](https://gvisor.dev/docs/user_guide/networking/)、[Compatibility](https://gvisor.dev/docs/user_guide/compatibility/)）。实践含义：细粒度出口白名单要在宿主（发行版）netfilter/DOCKER-USER 层做，或依赖上游编排。
- **F4.4 注册示例（官方形态）。** daemon.json：`{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}}`，由 `sudo runsc install` 生成；可选 `--runtime runsc-debug -- --debug --strace ...` 注册调试用第二 runtime（[gVisor Docker 指南](https://gvisor.dev/docs/user_guide/docker/)）。指定平台：`runtimeArgs: ["--platform=systrap"]`（默认即 systrap，通常无需显式指定）。

### S5 已知限制（git / grep 类只读工具视角）

- **F5.1 总体兼容立场。** 「The only real way to know if it will work is to try」；gVisor「implements a large portion of the Linux surface」，未实现部分「mostly comprised of alternatives to existing syscalls that gVisor does support」；每个 release 通过 Python/Java/Node.js/PHP/Go 主流运行时回归测试（[Compatibility](https://gvisor.dev/docs/user_guide/compatibility/)）。
- **F5.2 io_uring 默认禁用**，启用后也仅限基础 I/O 操作（[Compatibility](https://gvisor.dev/docs/user_guide/compatibility/)）。git 与 ripgrep 均不依赖 io_uring，不受影响。
- **F5.3 结构性限制**（[Compatibility](https://gvisor.dev/docs/user_guide/compatibility/)）：块设备文件系统（ext4 等）不能在沙箱内挂载；自定义硬件设备文件一般不支持（例外 NVIDIA GPU/TPU）；沙箱内使用 KVM 不支持；**沙箱内进程间的 cgroup 资源限制不强制执行**；SELinux label 不支持（[FAQ](https://gvisor.dev/docs/user_guide/faq/)，Ubuntu 无 SELinux 无碍）。
- **F5.4 目录缓存一致性坑。** 「For performance reasons, gVisor caches directory contents, and therefore it may not realize a new file was copied to a given directory」，workaround 为启用 shared root filesystem（[FAQ](https://gvisor.dev/docs/user_guide/faq/)）。对本用例（仓库以只读方式挂入）影响有限，但需注意挂载后宿主侧新增文件可能不可见。
- **F5.5 git 已知案例（均为历史问题）。** GitHub issue 检索（2026-11-10，API）：[#3345](https://github.com/google/gvisor/issues/3345)（2020，VFS2 时代 `git clone` internal error，已修）；[#2233](https://github.com/google/gvisor/issues/2233)（2020，credential 文件打开失败致 git credential 初始化卡死，已修）。**未发现近期 `git show/log/diff/status` 在 runsc 下崩坏的 open issue**；亦无「大仓库 git 操作系统性损坏」报告——性能层面见 S6。
- **F5.6 ripgrep/grep 已知案例。** 无「不工作」报告。性能相关：[#17274](https://github.com/google/gvisor/pull/17274)（open PR，2026-11-09）为 gofer 增加 dentry 分批驱逐（`dcache-eviction-batch=N`），基准是「eight-thread ripgrep over an approximately 18,000-file source tree」，冷跑时间约 -80%——反证**当前版本大目录冷遍历（首次 rg 全树扫描）经 gofer 存在可观开销**；目录缓存驻留后热查询开销显著降低。

### S6 性能量级

- **F6.1 CPU 密集零额外开销。** 「There is no runtime cost imposed for CPU operations」「gVisor does not introduce any additional costs with respect to raw memory accesses」——计算主导负载影响小（[性能指南](https://gvisor.dev/docs/architecture_guide/performance/)）。
- **F6.2 syscall 密集是重灾区。** 官方定性：拦截开销按 syscall 计，syscall-bound 应用（高性能数据存储、静态网络服务）受影响最大；ptrace 平台结构性开销最高；小粒度操作（redis 小命令、node/ruby 模板渲染）相对开销高，单次做功越多相对影响越小（[性能指南](https://gvisor.dev/docs/architecture_guide/performance/)）。第三方量级：UW 2023 课堂基准 ≥2.2x syscall、小文件读 11x（[WoC 2023 slides](http://faculty.washington.edu/wlloyd/slides/WoC-2023-slides.pdf)）；社区 GKE 实测 syscall 微基准可达 ~100x（[HN 讨论](https://news.ycombinator.com/item?id=19924036)）——微基准数字分布极宽，与 syscall 模式强相关，只宜作量级参考。
- **F6.3 启动开销 ~300ms 量级。** [#13182](https://github.com/google/gvisor/issues/13182)（closed as answered，2026-08）：ARM64 单核 pinned 实测 runsc 全生命周期（create→run→delete）平均 308ms；gVisor collaborator EtiennePerot：「startup time on the order of 300ms seems like a reasonable approximation of the current state of gVisor startup performance」，并明确对博客流传的「50–100ms 典型启动」持怀疑（「not aware of gVisor ever hitting that」）。第三方（厂商博客）报告 pod 启动 200–500ms，认为明显快于 Kata、适合 serverless（[Safeguard.sh](https://safeguard.sh/resources/blog/gvisor-runtime-security-deep-dive)）。官方另指出 Docker 层面测得的启动差异大头来自 Docker 自身而非 runsc（[性能指南](https://gvisor.dev/docs/architecture_guide/performance/)）。对本用例：**每次冷起一个只读命令容器约数百 ms 额外开销，长驻容器 + exec 模式可摊薄**。
- **F6.4 文件系统。** 原始磁盘 I/O「no significant fundamental overhead」（瓶颈回到磁盘）；开销来自 VFS/gofer 实现层：每次文件操作跨界固定小开销；静态小文件服务（10 万文件目录 Apache）「predictably poor results」（内部串行化点）；tmpfs 挂载可绕开 VFS 开销；ffmpeg 类混合负载影响小（[性能指南](https://gvisor.dev/docs/architecture_guide/performance/)）。对本用例：**大仓库目录树经 bind mount（gofer）遍历是主要开销点**；仓库打包进镜像层（overlay）或放 tmpfs 可显著缓解——官方对 gofer 目录遍历的优化（F5.6）也在持续进行。

## 3. 载体对比：DD 直接挂 runsc vs Ubuntu2204 内 Engine + runsc

| 维度 | A. Docker Desktop 直接挂 runsc（hack） | B. Ubuntu2204 内原生 docker-ce + runsc |
|---|---|---|
| 官方支持 | 无（F1.1，#364 open）；随时可能被 DD 升级破坏（F1.8） | 完全落在官方文档覆盖内：Engine 安装 + gVisor Docker 指南（F2.1） |
| runsc 存放位置 | DD VM rootfs 只读 → 塞 Docker volume `/var/lib/docker/volumes/runsc-runtime-binaries/_data/`（F1.4） | `/usr/local/bin/runsc`，发行版文件系统内，持久（F2.1） |
| daemon 配置 | GUI daemon.json 理论可写 runtimes，但需 patch VM 内 `/run/config/docker/daemon.json` + `pkill -HUP dockerd`；NVIDIA 注入会覆盖 runtimes 段（F1.5/F1.6） | `/etc/docker/daemon.json`，`runsc install` 一步生成，`systemctl restart docker` 生效（F2.1） |
| 重启/升级持久性 | 每次重启 DD 需重跑 installer；需计划任务自愈（F1.5/F1.8） | systemd `enable docker` 一劳永逸（F2.6）；runsc 随 apt/手动升级 |
| 与 DD 共存 | 即 DD 本身，无共存问题 | 同发行版内禁止共存（官方警告 F2.2）；跨发行版共存需关 Ubuntu2204 的 WSL integration（F2.3）；两侧 CLI 靠 context 区分 |
| 端口/资源 | 单一 VM 资源池 | 同一 utility VM 内再跑一个 dockerd，内存/CPU 叠加；仅同宿主端口才冲突（F2.4，机制推断） |
| 网络出口控制点 | 宿主=DD VM（不可运维） | 宿主=Ubuntu2204 发行版，可在其 netfilter/DOCKER-USER 层做白名单（F4.3） |
| 运维成本 | 高：自愈脚本 + DD 版本升级风险 + 排障面在闭源 VM 内 | 中：多维护一套 engine（apt 升级、磁盘、systemd）；排障在标准 Linux 环境 |
| 已知坑 | `unknown or invalid runtime name: runsc`、shim 路径错、GPU 注入覆盖（F1.5/F1.6/F1.9） | WSL 时钟步进 #17066（F2.5）、systemd 依赖（F2.6）、DD 误开 integration 时 CLI 语义混乱（F2.3） |
| 风险等级 | 高（依赖未公开内部行为） | 低（全官方路径），主要新风险是双引擎共存面 |

## 4. 留给选型票（grilling）的待决问题

1. 可否接受「每次 DD 启动后由计划任务自动补丁」的 A 路线运维模式（对应 diogovalada 工具包），还是要求全官方支持面（B 路线）？
2. B 路线下 DD 与 Ubuntu2204 内 engine 的长期共存策略：DD 是否仍承担日常镜像构建职责？资源池（`.wslconfig`）如何分配？
3. 仓库数据面：bind mount（gofer 开销，F6.4）vs 打入镜像层 vs 预拷贝到发行版 ext4 再挂载，对本用例（git show/log/diff、rg 冷/热查询）需实测选型；`/mnt/d`（drvfs）上再叠 gofer 的组合性能未见公开数据，需 spike。
4. 断网策略：`docker run --network none`（容器级）vs runtimeArgs `--network=none`（runtime 级全局）的边界；是否需要出口白名单（当前只有宿主 netfilter 一条现实路径，F4.3）。
5. runsc 获取流水线：GitHub release 资产 + gh 代理 + SHA 校验的固化脚本（F3.3），是否满足可重复部署要求。

## 5. 来源索引

官方文档：[gVisor 安装](https://gvisor.dev/docs/user_guide/install/) / [gVisor Docker 指南](https://gvisor.dev/docs/user_guide/docker/) / [Platforms](https://gvisor.dev/docs/architecture_guide/platforms/) / [Networking](https://gvisor.dev/docs/user_guide/networking/) / [Compatibility](https://gvisor.dev/docs/user_guide/compatibility/) / [FAQ](https://gvisor.dev/docs/user_guide/faq/) / [性能指南](https://gvisor.dev/docs/architecture_guide/performance/) / [DD Settings](https://docs.docker.com/desktop/settings/) / [DD WSL](https://docs.docker.com/desktop/wsl/) / [Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/) / [WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd)
GitHub：[desktop-feedback#364](https://github.com/docker/desktop-feedback/issues/364) / [for-win#12573](https://github.com/docker/for-win/issues/12573) / [desktop-feedback#142](https://github.com/docker/desktop-feedback/issues/142) / [gvisor#13182](https://github.com/google/gvisor/issues/13182) / [gvisor#17274](https://github.com/google/gvisor/pull/17274) / [gvisor#3345](https://github.com/google/gvisor/issues/3345) / [gvisor#2233](https://github.com/google/gvisor/issues/2233) / [gvisor#17066](https://github.com/google/gvisor/issues/17066) / [gvisor#11370](https://github.com/google/gvisor/issues/11370) / [gvisor releases](https://github.com/google/gvisor/releases) / [diogovalada/gvisor-docker-desktop-windows](https://github.com/diogovalada/gvisor-docker-desktop-windows) / [romanklis GVISOR_SETUP.md](https://github.com/romanklis/openclaw-contained/blob/main/docs/GVISOR_SETUP.md)
社区/第三方：[rimelek dev.to](https://dev.to/rimelek/using-gvisors-container-runtime-in-docker-desktop-374m) / [Reddit DD+gVisor](https://www.reddit.com/r/docker/comments/1fm8hlk/docker_desktop_will_not_recognize_gvisor/) / [Docker Blog 4.5](https://www.docker.com/blog/new-docker-menu-improved-release-highlights-with-docker-desktop-4-5/) / [WoC 2023 slides](http://faculty.washington.edu/wlloyd/slides/WoC-2023-slides.pdf) / [HN GKE 讨论](https://news.ycombinator.com/item?id=19924036) / [Safeguard.sh](https://safeguard.sh/resources/blog/gvisor-runtime-security-deep-dive)
直接核验：[npmmirror binary 列表](https://registry.npmmirror.com/-/binary/)（2026-11-10 抓取，无 gvisor）、[GitHub API latest release](https://api.github.com/repos/google/gvisor/releases/latest)（20261102.0 资产清单）
