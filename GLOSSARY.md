# GLOSSARY

本项目领域术语表。只放概念与边界，不放实现细节。

## 系统角色

- **服务端（backend）**：运行 Agent 运行时的 FastAPI 进程。持有对话状态、工具注册表与审批/审计，本身不持有 git 凭证、不承担写路径执行。
- **YuanRunner（执行器 / runner）**：用户本机的常驻代理，经 WSS 连入服务端，用设备令牌鉴权。所有涉及用户环境的操作（clone 到工作区、提交、推送、装依赖、codex 委派）由它以固定 argv 模板在本机执行；git 凭证只存在于 runner 侧的系统凭据库。
- **服务器薄、边缘重**：架构原则——服务端只做编排与状态，一切触碰用户文件与凭证的动作下沉到 runner（边缘）。

## 沙箱体系

- **沙箱（Sandbox）**：在服务端侧、以 runsc（gVisor 用户态内核）运行的一次性容器，用于执行不受信任或仅限只读的命令。防逃逸档：容器内进程无法触碰宿主内核。
- **沙箱 Engine**：承载 backend 容器与沙箱容器的专用 Docker Engine（Ubuntu2204 发行版内原生 docker-ce + runsc）。与 Docker Desktop（仅承载 MySQL/Qdrant/Langfuse 等基础设施）物理分离。
- **只读命令工具（git_query）**：服务端侧唯一的命令执行面——白名单 git 查询命令在沙箱内对只读副本执行。read 档免审批的前提是每次执行都在沙箱内；**无沙箱不执行，绝不回退宿主直跑**。
- **只读副本（Mirror）**：服务端为查询持有的公开仓库完整 clone，对沙箱只读挂载。副本的获取与刷新（clone/fetch）只发生在 backend 侧（有网络），沙箱容器永远断网。

## 仓库来源

- **RepoSource**：向工具供给「某一仓库的本地只读副本」的抽象。MVP 实现为匿名 clone 公开仓库；预留 runner 经 WSS 推 git bundle 的实现（凭证留边缘，公私通吃）。
- **写路径**：clone 到用户工作区、提交、推送的完整链路，永远经 YuanRunner；与服务端 Mirror 无关，两者不互写。

## 权限与审批

- **权限档位（read / write / danger）**：read 免审批但全量审计；write、danger 执行前需用户确认。委派给子 Agent 时取其工具集最高档位整体审批。
- **审计**：每次工具调用（无论档位）落 MySQL 的记录，含调用方、参数摘要与执行结果。
