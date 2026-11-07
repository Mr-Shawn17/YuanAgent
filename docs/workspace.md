# 多工作目录（Workspace）实施方案

## 1. 目标与背景

当前本地执行器只支持一个全局工作目录：执行器启动时读 `runner.yaml` 里的
`workspace`，所有对话的文件操作都落在同一个目录。本方案把「单目录」升级为
「按会话绑定的多工作目录」，对标 WorkBuddy / Codex 的多文件夹工作模式。

具体目标：

- 网页上能选择工作目录（下拉常用目录 + 手填绝对路径）。
- 支持同时打开多个不同的工作目录（不同会话各绑一个）。
- 目录变更动态热切换，无需重启执行器。
- 会话关闭后重开，自动恢复之前绑定的目录（持久化）。

## 2. 核心语义：按会话绑定

「会话」= 左侧栏的一次对话（有独立 history，存在数据库）。

- 每个会话记住自己绑定的工作目录，存 `sessions.meta` JSON 字段，持久化。
- 新建会话默认不绑定目录，首次选择后绑定。
- 关闭浏览器、次日重开该会话，自动恢复绑定，无需重选。
- 不同会话可绑定不同目录，互不干扰，可同时打开。
- 在会话内切换目录，只影响当前会话（热切换）。

对比「全局单一目录」：后者所有会话共用一个目录，无法并行多项目，不满足目标。

## 3. 为什么网页不能直接浏览磁盘

工作目录是「用户本机电脑上的文件夹」，只有运行在用户电脑上的执行器
（YuanRunner）能扫描真实磁盘。中心服务（网页后端）无法枚举用户磁盘。

因此目录来源只能由执行器上报：

- 执行器启动时扫描本机常用目录（主目录、桌面、文档、下载、代码目录等），
  通过 WSS 握手后的 `workspaces` 消息上报给服务端。
- 用户在网页上可额外手填绝对路径；执行器校验存在后加入可用列表。

## 4. 总体架构

```text
用户电脑（YuanRunner）                        中心服务                           网页
┌─────────────────────┐      WSS      ┌──────────────────────┐      HTTP      ┌──────────┐
│ 扫描常用目录          │ ────────────▶ │ LocalTaskHub          │ ◀───────────── │ 目录选择器│
│ 校验手填目录          │   workspaces  │  (user_id,workspace)  │   目录列表      │ 下拉+手填 │
│ 在 workspace 执行工具 │ ◀──────────── │   → 连接路由表         │   会话绑定      │          │
└─────────────────────┘  tool_request └──────────────────────┘              └──────────┘
                              ▲                    ▲
                              └── tool_result ─────┘
```

关键改动点：

1. **执行器**：扫描 + 上报目录列表；工具请求携带 workspace，在其下执行。
2. **hub**：路由维度从 `user_id` 升级为 `(user_id, workspace)`。
3. **会话**：`sessions.meta.workspace` 存绑定；提供绑定读写接口。
4. **工具**：文件工具 local 模式转发时携带当前会话的 workspace。
5. **前端**：新增 WorkspacePicker 组件。

## 5. 通信协议扩展

### 5.1 执行器 → 服务端：目录上报

```json
{
  "type": "workspaces",
  "workspaces": [
    {"path": "C:\\Users\\Administrator", "name": "主目录", "kind": "home"},
    {"path": "C:\\Users\\Administrator\\Desktop", "name": "桌面", "kind": "desktop"},
    {"path": "C:\\Users\\Administrator\\Documents", "name": "文档", "kind": "documents"},
    {"path": "C:\\Users\\Administrator\\Downloads", "name": "下载", "kind": "downloads"}
  ]
}
```

### 5.2 服务端 → 执行器：工具请求携带 workspace

```json
{
  "type": "tool_request",
  "request_id": "req_xxx",
  "workspace": "C:\\Users\\Administrator\\Desktop",
  "tool": "write_file",
  "args": {"path": "demo.txt", "content": "..."},
  "timeout_seconds": 120
}
```

执行器收到后，在该 workspace 下执行（替代原先启动时固定的单一 workspace）。

## 6. 服务端路由：hub 按 workspace 路由

`LocalTaskHub` 当前用 `user_id → _Connection` 单连接映射。改造为：

```text
(user_id, workspace) → _Connection
```

一个执行器（device）可以承载多个 workspace，但每条连接仍对应一个
`(user_id, workspace)` 组合。执行器若要在多个 workspace 下工作，可对每个
workspace 维持一条连接，或单连接内支持 workspace 参数。

第一版采用「单连接 + workspace 参数」：连接注册时记录该连接所属的
workspace 集合，`dispatch` 时按 `(user_id, workspace)` 找到对应连接。

简化落地：执行器默认只上报并服务「用户当前选中的工作目录」，即
`runner.yaml` 的 workspace 作为默认，用户手填的新目录执行器在校验后
动态加入可用集合并可服务。

## 7. 会话绑定存储

`Session.meta`（JSON）新增字段：

```json
{"workspace": "C:\\Users\\Administrator\\Desktop"}
```

- 无绑定：`meta` 不含 `workspace` 或为空串。
- 绑定接口：`PUT /v1/sessions/{id}/workspace`，body `{"workspace": "..."}`。
- 会话列表接口 `GET /v1/sessions` 返回 `workspace` 字段（前端展示）。

无需数据库迁移（复用现有 JSON 列）。

## 8. 工具携带 workspace

文件工具（read/write/list）在 local 模式下转发时，从 `ToolContext` 取当前
会话绑定的 workspace，随 args 一起传给 `router.dispatch`。

`ToolContext` 新增 `workspace: str` 字段；`build_run_agent` 装配时从会话
meta 读 workspace 注入。工具工厂据此把 workspace 传给 hub.dispatch。

## 9. 前端 WorkspacePicker

- 位置：顶栏 header（标题右侧）。
- 内容：下拉列出执行器上报的常用目录 + 手填输入框。
- 交互：选择后调用绑定接口，即时生效并持久化。
- 无绑定会话：显示「选择工作目录」占位，未选时文件操作回退到执行器默认目录。

## 10. 实施步骤

1. 方案文档（本文）。
2. 执行器：目录扫描 + workspaces 上报 + 手填校验 + 按 workspace 执行。
3. hub：`(user_id, workspace)` 路由 + 目录列表查询接口。
4. 会话绑定：meta.workspace 读写接口 + 会话列表返回 workspace。
5. 工具：ToolContext.workspace 注入 + 文件工具携带 workspace 转发。
6. 前端：WorkspacePicker 组件 + 绑定接口接线。
7. 联调：单测 + 端到端（两会话不同目录写文件）。

## 11. 边界与安全

- 目录越界：执行器对每个 workspace 各自做 `is_relative_to` 沙箱检查。
- 手填目录校验：执行器校验目录真实存在才加入可用列表，避免幻影路径。
- 目录隔离：不同会话的 workspace 天然隔离；同 workspace 多会话共享（符合预期）。
- 权限：写操作仍走审批流（中心审批后下发执行器执行）。

## 12. 不在本期范围

- 执行器对任意目录的实时递归扫描（仅扫描固定常用位置，避免性能与隐私问题）。
- 目录级的细粒度权限（读写/只读）配置。
- 多用户登录体系（延续「单用户 + 设备令牌」口径）。
