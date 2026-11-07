# 沙箱升级执行期决策日志

执行期间需要用户拍板的问题与最终决策记录。格式：问题 / 选项 / 决策 / 时间。

---

## 决策#1（已决）Windows Sandbox 功能启用时机

- **时间**：2026-11-07
- **问题**：本机 Windows Sandbox 功能未启用（`Containers-DisposableClientVM` State=Disabled），启用需管理员命令 + 重启电脑。
- **选项**：
  - A：现在启用并重启，VM 档尽早实测
  - B：先跳过，策略层全链路完成后再启用（推荐，符合实施顺序）
- **决策**：**B**（用户选定）——暂不重启，先用策略层档；VM 档代码已就绪（auto 回退已验证），启用功能后做 Spike① 实测。

---

## 决策#2（已决，无需改动）编程引擎用 npm 版 codex CLI

- **时间**：2026-11-07
- **问题**：用户确认"coding 能力用 codex cli 而不是 codex（桌面应用）"。
- **核实**：`delegate_coding` 实现检测的 `codex` 命令解析到 `C:\Users\Administrator\AppData\Roaming\npm\codex.cmd`（npm 全局安装的 **codex-cli 0.167.0**），并非 Codex Desktop——**无需改动**。
- **模型对接现状**：用户 `~/.codex/config.toml` 已配置 custom provider（bailian `qwen3.7-max`，经 cc-switch 本地代理 `127.0.0.1:15721/v1`）——"对接国内大模型"已配置好，测试时需 cc-switch 在运行。

## 决策#3（信息确认）git 真实测试目标

- 项目 remote：`https://github.com/Mr-Shawn17/YuanAgent.git`（HTTPS），当前分支 `feature/local-runner` 已推送过——说明用户平时访问 GitHub 走代理工具（本次会话中直连被 reset）。
- **真实 git 工具测试**（回家后）：开代理 + `python -m runner set-git-cred github.com`（若仓库私有）→ 用本项目地址测 `git_clone`。

---

## 实施记录（非决策，备查）

- 2026-11-07 环境探测：codex-cli 0.167.0 已装；git 2.55 / python 3.12.7（conda py312，项目环境）；Windows Sandbox 功能未启用。
- 2026-11-07 Spike③（codex exec）：`--json` JSONL 事件格式确认（thread.started / item.completed / turn.started / error）；注意 codex 会尝试读 stdin（我们已用 stdin=DEVNULL 规避）。**用户 codex 配置为自定义 provider（bailian qwen3.7-max，经本地代理 `http://127.0.0.1:15721/v1`，cc-switch 管理）**——delegate_coding 依赖该代理在运行；本次实测时代理未启动导致 Connection failed，非集成问题。
- 2026-11-07 用户的 codex 已启用 Windows sandbox（config.toml `[windows] sandbox = "elevated"`，配有 Codex Desktop），印证 VM 档路线在本机可行。
- 2026-11-07 策略层全链路验收：`scripts/verify_command_sandbox.py` 16/16 通过（白名单/黑名单同构、超时杀树、env 不泄露、越界 cwd 拒绝、git URL 消毒、backend 装配 danger 档）。
- 2026-11-07 VM 档代码完成（backends/windows_sandbox.py + sandbox_tools.py）：auto 档在功能未启用时自动回退 shell（已验证）；显式 windows_sandbox 报错不静默降级（已验证）。待功能启用后做 Spike① 实测。
- 2026-11-07 文档同步完成：local-runner.md §13 命令沙箱、README 安全模型、usage.md §6.5、roadmap（token 分层 + skills runner 洞已修记录）。

