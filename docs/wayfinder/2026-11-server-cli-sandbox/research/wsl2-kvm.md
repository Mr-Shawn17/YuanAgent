# T02 调研：WSL2 能否透出 /dev/kvm（Firecracker / Kata 微 VM 备查）

- 票：wayfinder T02（`wayfinder:research`，不阻塞当前 gVisor 落地，不做选型决策）
- 日期：2026-11-10
- 分支：`research/wsl2-kvm`

## 结论（先读这里）

**可行 —— 且已在本机实测验证通过。**

本机（Intel i7-10870H / Win11 10.0.26200.9711 / WSL 2.7.10.0 / 内核 6.18.33.2-2）当前状态下：

1. CPU 虚拟化扩展**已经默认透出**：`/proc/cpuinfo` 含 `vmx` 标志，dmesg 报 `Hyper-V: Nested features: 0x3e0101`（无需任何 `.wslconfig`，官方文档默认值即 `nestedVirtualization=true`）。
2. WSL 官方内核**自带 KVM 模块**（`kvm.ko` / `kvm-intel.ko` / `kvm-amd.ko`，均 `=m` 编译），`/lib/modules/6.18.33.2-microsoft-standard-WSL2/` 里已就位。
3. `/dev/kvm` 之所以不存在，**唯一缺口是模块未加载**。实测 `sudo modprobe kvm-intel` 后 `/dev/kvm` 立即出现（`crw------- root:root`），dmesg 报 `kvm_intel: Using Hyper-V Enlightened VMCS`（KVM 走 Hyper-V 照明路径，接近原生的嵌套虚拟化路径）。测试后已 `rmmod` 还原现场。

有条件说明（条件均满足或低成本可满足）：

| 条件 | 本机状态 |
| --- | --- |
| Windows 11（`nestedVirtualization` 仅 Win11 可用，Win10 不支持） | 满足（10.0.26200） |
| Hypervisor/VirtualMachinePlatform 已启用 | 满足（systeminfo「已检测到虚拟机监控程序」） |
| Intel VT-x+EPT 或 AMD SVM | 满足（i7-10870H，Comet Lake，含 VT-x+EPT） |
| 非 ARM（ARM 无嵌套虚拟化） | 满足（x86_64） |
| KVM 模块加载（一次性 modprobe 或持久化配置） | **当前缺口**，两条官方路径可持久化（见 §4） |

风险注记：2026-10 的 KB5127648（build .9711）曾在 **AMD** 平台回归掉嵌套虚拟化（`Nested features: 0x0`），微软已通过 [KB5127936（2026-10）](https://support.microsoft.com/en-us/servicing/os/windows-11/2026/10/kb5127936-windows-11-25h2-26h2-update) 修复（更新说明原文："This update fixes an issue where nested virtualization might not be available in WSL2 on devices with AMD processors"）。本机为 Intel，实测不受影响；若未来换 AMD 机器需确认已装该修复。

---

## 1. WSL 官方对 nested virtualization 的支持状态

- 官方文档 [wsl-config（更新于 2026-10-16）](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)：`[wsl2]` 段 `nestedVirtualization`，类型 boolean，**默认 `true`**，"Boolean to turn on or off nested virtualization, enabling other nested VMs to run inside WSL 2."，脚注 ¹ "Only available on Windows 11"。文档 git 历史（MicrosoftDocs/WSL）显示 **2024-12 时默认值已是 `true`**。
- 底层机制：即 Hyper-V 的 `Set-VMProcessor -ExposeVirtualizationExtensions $true`（见 [Hyper-V 嵌套虚拟化文档](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/enable-nested-virtualization)）。WSL 把该开关作用于 WSL2 utility VM。
- 硬件/OS 要求（按 Hyper-V 文档）：Intel VT-x+EPT 需 Win10+/Server2016+；**AMD SVM 需 Win11+/Server2022+**。本机 Intel + Win11 满足。
- WSL 版本要求：当前主线 WSL（2.x/3.x）均支持；WSL 3.0.4（2026-10-20）release notes 还有 "Warn when nested virtualization capability detection fails"（PR #41942）——官方在持续维护该能力检测。
- 不支持面：Windows 10 不生效（open PR [microsoft/WSL#41914](https://github.com/microsoft/WSL/pull/41914) "Honor nestedVirtualization on Windows 10"）；ARM 不支持（[microsoft/WSL#13796](https://github.com/microsoft/WSL/issues/13796)，closed as not supported on ARM）。

## 2. `nestedVirtualization=true` 语义与内核 KVM 支持

### 2.1 语义与生效条件

- 语义：让 WSL2 utility VM（L1）看到宿主（L0 Hyper-V）暴露的 CPU 虚拟化扩展（Intel `vmx` / AMD `svm`），从而 L1 内核的 KVM 可创建硬件加速的 L2 虚机。**它不负责创建 `/dev/kvm` 设备节点**——那需要 L1 内核加载 `kvm` + `kvm-intel`（或 `kvm-amd`）模块。
- 生效条件：`.wslconfig` 位于 `%UserProfile%`；改动需 `wsl --shutdown` 后重启发行版生效（官方"8 秒规则"，docs 同页）。**不需要重启 Windows**（前提是 hypervisor 已启用，本机已启用）。
- 本机实证：无 `.wslconfig`（全默认）时 `vmx` 标志已在 `/proc/cpuinfo` 中 → 2.7.10 上默认值就是开。

### 2.2 内核配置（第一手证据，直接查 config-wsl）

来源：[microsoft/WSL2-Linux-Kernel](https://github.com/microsoft/WSL2-Linux-Kernel)，`Microsoft/config-wsl`，**tag `linux-msft-wsl-6.18.33.2`**（与本机内核 6.18.33.2-2 精确对应；WSL 2.7.9 release notes 写明 "Update Microsoft.WSL.Kernel version to 6.18.33.2-2"）：

```
CONFIG_KVM=m
CONFIG_KVM_X86=m
CONFIG_KVM_INTEL=m
CONFIG_KVM_AMD=m
CONFIG_KVM_HYPERV=y        # KVM 的 Hyper-V 照明支持（嵌套场景关键）
CONFIG_KVM_SMM=y
CONFIG_VIRTUALIZATION=y
CONFIG_LOCALVERSION="-microsoft-standard-WSL2"
```

- 6.6.y 分支的 config 同样是 `CONFIG_KVM=m / KVM_INTEL=m / KVM_AMD=m` —— 官方内核已连续两个大版本（6.6 → 6.18）携带 KVM 模块，非临时行为。
- 本机 `/lib/modules/6.18.33.2-microsoft-standard-WSL2/kernel/arch/x86/kvm/` 实测存在 `kvm.ko`、`kvm-intel.ko`、`kvm-amd.ko`。
- **无需自编译内核**。自编译只是逃生通道：官方支持 `.wslconfig` 的 `kernel=` / `kernelModules=` 指定自编内核与模块 VHD（wsl-config 文档），仓库提供标准构建流程；按社区经验首次构建约 1–2 小时（本课题不需要走到这一步）。

## 3. 本机实测记录（2026-11-10，全程可逆）

```
$ wsl -d Ubuntu2204 -e sh -c "grep -o -m1 -E ' vmx| svm' /proc/cpuinfo"
 vmx                                  # 嵌套扩展默认已透出
$ ls /dev/kvm                          # 不存在
$ lsmod | grep kvm                     # 空（模块未加载）
$ ls /lib/modules/6.18.33.2-microsoft-standard-WSL2/kernel/arch/x86/kvm/
kvm-amd.ko  kvm.ko  kvm-intel.ko      # 模块在盘

$ wsl -d Ubuntu2204 -u root -e sh -c "modprobe kvm_intel"
$ ls -la /dev/kvm
crw------- 1 root root 10, 232 ... /dev/kvm     # 立即出现
$ lsmod | grep kvm
kvm_intel  368640 0
kvm       1003520 1 kvm_intel
$ dmesg | grep -iE 'kvm|nested'
[    0.000000] Hyper-V: Nested features: 0x3e0101
[   45.464426] kvm_intel: Using Hyper-V Enlightened VMCS   # 走照明路径

（验证后 rmmod kvm_intel kvm 已还原，/dev/kvm 随之消失）
```

要点：全程未改任何持久配置、未重启 WSL、未动 Windows；结论可直接复现。

## 4. 持久化 / 升档路径（若将来启用）

两条官方路径任选：

1. `.wslconfig`（对整个 utility VM 生效，含 docker-desktop 发行版）：
   ```ini
   [wsl2]
   nestedVirtualization=true        # 本机默认已开，可省
   loadKernelModules=kvm,kvm-intel  # 官方键：启动时附加加载内核模块
   ```
   然后 `wsl --shutdown`（一次性成本，所有发行版重启）。
2. 发行版内 systemd 自动加载（microsoft/WSL#13262 中 benhillis 确认的路径）：Ubuntu2204 已启用 systemd 时，`/etc/modules-load.d/kvm.conf` 写入 `kvm-intel`。

注意 `/dev/kvm` 权限为 `600 root:root`（WSL 历史 issue #7149 即报过权限问题）；Firecracker 官方建议 root + jailer 运行，普通用户需 `setfacl -m u:<user>:rw /dev/kvm` 或 kvm 组。

## 5. Firecracker 最小可行清单（若 KVM 已透出）

依据官方 [getting-started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)：

| 组件 | 获取渠道 | 说明 |
| --- | --- | --- |
| firecracker 二进制 | GitHub Releases：`.../releases/download/<tag>/firecracker-<tag>-x86_64.tgz` | 免编译；生产用 jailer |
| guest 内核镜像 | 官方 CI 产物（`spec.ccfc.min` S3 桶的 `vmlinux-*`）或按其 kernel 配置自编 | 要求非压缩内核镜像（vmlinux） |
| rootfs | 任意 ext4 镜像（官方 quickstart 从 Ubuntu squashfs 构建 1G ext4） | 自行注入 ssh key/agent |
| 前置条件 | `/dev/kvm` 读写 + KVM 模块 | 本机已验证可满足 |
| 网络 | TAP 设备（`/dev/net/tun`） | WSL 默认加载 `tun`/`ip_tables`/`br_netfilter`（wsl-config 文档 `loadDefaultKernelModules` 默认 true），恰好齐备 |

## 6. Kata Containers 等价清单

依据官方 [install README](https://github.com/kata-containers/kata-containers/blob/main/docs/install/README.md) 与 [hypervisors 文档](https://github.com/kata-containers/kata-containers/blob/main/docs/hypervisors.md)：

- 前提原文："Kata Containers requires nested virtualization or bare metal."（KVM 型 runtime 依赖 `/dev/kvm`）。
- 支持的 hypervisor：QEMU、Cloud Hypervisor、Firecracker、Dragonball、StratoVirt（Firecracker 是受支持的 Kata hypervisor 之一）。
- 发行物：GitHub Releases 的 **`kata-static-<ver>-amd64.tar.zst`**（4.3.1，2026-10-21，约 973MB）——自包含 runtime（containerd-shim-v2）、QEMU/Cloud Hypervisor、guest 内核与 rootfs 镜像；无需自编内核。K8s 场景首选 kata-deploy Helm chart；单机场景将 runtime 注册进 containerd/Docker（`configuration.toml` + runtime class）。

## 7. 成本与副作用

| 项 | 结论 | 依据 |
| --- | --- | --- |
| 启用是否重启整机 | **不需要**。改 `.wslconfig` 后 `wsl --shutdown` 即可（约 8 秒）；仅当宿主 hypervisor 未开时才需启用功能+重启，本机已开 | wsl-config 文档"8 second rule" |
| 对 Docker Desktop | **无功能性影响**。Docker Desktop 自身不消费嵌套虚拟化；docker-desktop 发行版与 Ubuntu 共用同一 utility VM/内核，加载 kvm 模块后两者都能看到 `/dev/kvm`，Docker 引擎不使用它。社区有人在 Docker Desktop 的 WSL VM 里跑 KVM 虚机（#41955 评论：dockur/macos KVM VM） | 机制分析 + #41955 |
| 一次性代价 | `wsl --shutdown` 会杀掉所有发行版（含 docker-desktop），Docker Desktop 会在下次使用时自动拉起；容器按 restart 策略恢复 | WSL 行为 |
| 嵌套虚拟化性能损耗量级 | 只有真正运行 L2 VM 时才付代价。学术基准（Turtles 项目，Ben-Yehuda 2010）：**带照明路径的嵌套开销约 6–8%**（相对单层虚拟化）——本机 dmesg 确认走的正是 `Hyper-V Enlightened VMCS`；Red Hat "Inception"（2017）：L2 接近原生，L3 才明显劣化；单层 KVM 本身约宿主 96% 性能 | 见来源列表 |
| 资源竞争 | microVM 与 Docker 共享 utility VM 的内存/CPU（默认上限 50% 宿主内存，可用 `memory=` 约束） | wsl-config 文档 |
| Windows 更新风险 | 存在先例：KB5127648 曾致 AMD 掉嵌套（已由 KB5127936 修复）；Intel 未受影响 | #41917/#41955 |

## 8. 社区实锤（近一年为主，注明版本）

| 案例 | 结果 | 环境 |
| --- | --- | --- |
| 本机实测（本次调研，2026-11-10） | **成功**：`modprobe kvm-intel` → `/dev/kvm` 出现，Enlightened VMCS | WSL 2.7.10 / 内核 6.18.33.2-2 / Win11 26200.9711 / Intel i7-10870H |
| [#13262](https://github.com/microsoft/WSL/issues/13262)（2025-07，closed） | 成功+排障结论：`nestedVirtualization=true` 下 `/dev/kvm` 缺失 = `kvm_intel` 未自动加载；zcobol 在 2.5.7.8 确认 `lsmod` 有 kvm_intel 且 `/dev/kvm` 正常；WSL 成员 benhillis 给出 systemd modules-load.d 持久化方案 | WSL 2.5.7.8 / 2.5.9.0，内核 6.6.87.x，Intel |
| [#41955](https://github.com/microsoft/WSL/issues/41955)（2026-10，closed as dupe） | 失败→已修复：KB5127648 后 `Nested features: 0x0`、无 `svm`、无 `/dev/kvm`；同 issue 评论证明此前 KVM VM 可跑在 Docker Desktop 的 WSL VM 内 | WSL 2.7.14 / 内核 6.18.33.2-2 / **AMD** Ryzen 5 7600 + 7950X |
| [#41917](https://github.com/microsoft/WSL/issues/41917)（2026-10-31 汇总，closed） | 失败→已修复：根因为 HCS `ProcessorCapabilities` 查询在 .9711 上对 AMD 返回错误；社区确认 **KB5127936 修复**、26220.9836 上恢复工作 | WSL 3.0.3 / 内核 6.18.54.1 / **AMD** Zen 2/Zen 4 |
| [#41968](https://github.com/microsoft/WSL/issues/41968)（2026-11，closed） | AMD Ryzen 上 WSL 3.0.3 嵌套回归（同上批次修复） | WSL 3.0.3 / AMD |
| [#13796](https://github.com/microsoft/WSL/issues/13796)（2025-12，closed） | ARM 平台不支持嵌套虚拟化（负面边界） | ARM |
| [#41914](https://github.com/microsoft/WSL/pull/41914)（open PR） | Win10 不生效（负面边界；官方仅 Win11） | Win10 |

## 9. 升档成本评估（gVisor → Firecracker / Kata，仅估不决策）

- **打通 KVM（前置）**：约 10 分钟 —— 写 2 行 `.wslconfig`（或发行版 modules-load.d）+ `wsl --shutdown`，验证 `/dev/kvm`。零内核工作。
- **Firecracker 路线**：约 0.5–1 人日 —— 下载二进制 + CI 内核镜像 + 自建 ext4 rootfs（ hardened 需裁剪内核/镜像则再加 1–2 日）；TAP 网络 WSL 已具备默认模块。与标准 Linux 上的 Firecracker API 完全一致（/dev/kvm 为真 KVM）。
- **Kata 路线**：约 1 人日 —— kata-static tarball（~1GB，自带 guest 内核/rootfs/QEMU）+ containerd/Docker runtime 注册；K8s 可走 kata-deploy。
- **回滚**：删除 `.wslconfig` 两行 + `wsl --shutdown`，即回到现状；无宿主侧残留。
- **长期维护面**：内核模块随 WSL 自动更新（官方 config 已连续携带）；主要外部风险是 Windows CU 引入的嵌套回归（有 AMD 先例、约 2 周内被修复；Intel 无先例）。

## 来源

- [WSL wsl-config 官方文档（2026-10-16）](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
- [Hyper-V 嵌套虚拟化（要求与机制）](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/enable-nested-virtualization)
- [WSL2-Linux-Kernel config-wsl @ linux-msft-wsl-6.18.y / tag 6.18.33.2](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.y/Microsoft/config-wsl)
- [microsoft/WSL releases（2.7.9 内核 6.18.33.2-2；3.0.4 检测告警）](https://github.com/microsoft/WSL/releases)
- [microsoft/WSL issues：#13262 / #41917 / #41955 / #41968 / #13796 / PR#41914](https://github.com/microsoft/WSL/issues/13262)
- [KB5127936 修复 AMD WSL2 嵌套虚拟化（support.microsoft.com）](https://support.microsoft.com/en-us/servicing/os/windows-11/2026/10/kb5127936-windows-11-25h2-26h2-update)
- [Firecracker getting-started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)
- [Kata Containers install README / hypervisors.md / releases](https://github.com/kata-containers/kata-containers/blob/main/docs/install/README.md)
- 性能：[Turtles 项目论文（Ben-Yehuda 2010，6–8%）](http://www.cs.columbia.edu/~cdall/candidacy/pdf/BenYehuda2010.pdf)、[Red Hat "Inception"（2017）](https://www.redhat.com/en/blog/inception-how-usable-are-nested-kvm-guests)、[Level1Techs KVM 基准](https://forum.level1techs.com/t/how-fast-is-kvm-host-vs-virtual-machine-performance/110192)
