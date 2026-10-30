# fan_out 并行子 Agent 委派 · 使用说明

> 功能版本：2026-10-30 ｜ 对应代码：`backend/app/agents/subagents.py`、`config/agent.yaml`（routing 段）

## 一、这是什么

主 Agent 在判断用户问题**可拆分为多个互不依赖的子任务**时（典型如"对比 A、B 两家公司 2024 年营收"），调用 `fan_out` 工具一次性并行委派，各子任务在对应子 Agent 内同时执行，全部结果结构化回传后由主 Agent 汇总对比。

```
用户：对比 A、B 两家公司 2024 年营收
  → 主 Agent 推理：两个对象 × 独立查询 → 可并行
  → 调用 fan_out(calls=[
       {agent: "research", task: "查A公司2024年营收"},
       {agent: "research", task: "查B公司2024年营收"},
     ])
  → 两个 research 子 Agent 同时执行（各自内部 ReAct 循环调 rag__search 等）
  → 结果分节回传：【任务1·research】…【任务2·research】…
  → 主 Agent 基于两份结果输出对比结论
```

**与单任务委派的分工**（会自动提示模型，无需人工干预）：

| 场景 | 用法 |
| --- | --- |
| 多对象对比/统计、子任务互不依赖 | `fan_out` 并行 |
| 单对象任务、或任务间有依赖（先查后写） | `delegate_to_research` / `delegate_to_coding` 串行 |

## 二、配置（config/agent.yaml 的 routing.fan_out 段）

```yaml
fan_out:
  enabled: true       # 总开关；false 时主 Agent 无此工具，退化为串行委派
  max_parallel: 4     # 单批子任务上限（2~10），超出整批拒绝并提示模型拆批
  read_only: true     # true=仅只读子 Agent 可进批（免审批）；false=允许写档（触发整体审批）
```

**调整建议**：
- `max_parallel`：默认 4。若 LLM 限流充裕且常查 5+ 个对象可上调；模型限流紧张时调小。
- `read_only`：默认 true（安全护栏，并行写风险高）。确需并行写时改 false，批内最高档位触发一次审批弹窗。

## 三、启动与验证

```bash
# 前提：MySQL、RAG MCP（8100 端口 annual-report-rag）在线
python scripts/start_backend.py    # 后端 API（端口从 config/agent.yaml 的 server.backend_port 读取，默认 8000）
python scripts/start_frontend.py   # 前端（端口从 server.frontend_port 读取，默认 3000）
# 等价命令：make dev / make web
```

浏览器打开 `http://localhost:3000`，用以下问题验证（按场景覆盖）：

| # | 测试问题 | 预期行为 |
| --- | --- | --- |
| 1 | **对比平安银行和招商银行 2023 年的营收** | 出现「执行链路图」卡片：主 Agent 节点 → 2 个并行子任务泳道 → 汇总节点；两任务同时变"执行中"，各自显示耗时，完成后变绿 ● |
| 2 | 查一下万科 2023 年的净利润 | 无执行链路图（单对象走串行 delegate，行为与现状一致） |
| 3 | 对比保利发展、万科、华能国际、招商银行、平安银行 2023 营收（5 个） | 超过 max_parallel=4 → 主 Agent 自动拆成两批（4+1）或改串行，仍能给出答案 |
| 4 | 帮我查 A 公司营收并写个总结报告 | 不走 fan_out（任务有依赖），串行 research → coding |

### 前端「执行链路图」卡片说明

- **主 Agent 节点**（顶部/底部 indigo 条）：描述"判断拆分 → 委派 N 个子任务 → 汇总"的编排过程
- **子任务泳道**：每任务一行，含 `[agent 名]` 标签 + 任务描述 + 状态徽章
- **状态符号与颜色**：
  - ◔ 灰色「等待中」——批次已建、尚未轮到执行
  - ◐ 蓝色脉动「执行中」——正在运行（含耗时实时累计）
  - ● 绿色「完成 · 8.2s」——成功结束
  - ✕ 红色「失败」——附错误摘要（子任务失败不影响其他任务，主 Agent 会汇报部分结果）
- **内部工具调用**（可展开）：每个子任务泳道内可展开查看其内部工具链（如 `rag__search`）与各自耗时——这是 B+ 粒度：并行时能看清"任务 1 用了哪些工具"
- 批次运行中卡片自动展开，结束后可折叠（点击标题行）

## 四、工作机制（排障参考）

1. **拆分判断**：主 Agent 在 ReAct 循环内自行判断（routing_note 已注入"多对象对比优先 fan_out"的指引）；无结构强制，若模型偶尔不拆，结果仍正确只是串行较慢
2. **task_id 归属**：每个子任务协程通过 ContextVar 标记 `task_id`，子 Agent 内部工具事件（tool_start/tool_end）自动携带——前端据此把交错的事件归到正确泳道
3. **失败处理**：单任务失败不中断整批（错误转文本计入结果），`fanout_end` 事件的 `fail_count` 汇总
4. **审批语义**：read_only 模式下整批免审批（research 为 read 档）；若看到 fan_out 弹审批窗，说明批内混入了写档子 Agent 且 read_only=false
5. **审计**：所有子任务的工具调用照常写 `tool_audit` 表（origin=`subagent:<name>`），不受并行影响

## 五、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 对比类问题没出执行链路图 | 模型判断未拆分（走串行）。可强化提问（"分别查询后对比"）或观察是否 RAG 无相关数据 |
| fan_out 返回「超过单批上限」 | 子任务数 > max_parallel，模型会自动拆批重试，最终答案不受影响 |
| 子任务全部失败 | 优先查 RAG 服务（`curl http://127.0.0.1:8100/mcp`）与 LLM Key 是否有效 |
| 执行链路图缺失内部工具明细 | 正常——子任务可能直接由模型回答未调工具；或工具事件早于 fanout_start（理论不会，如出现请反馈） |

## 六、相关文档

- 架构总览：`docs/architecture-brief.md`（子 Agent 分流章节）
- 演进清单：`docs/roadmap.md`
