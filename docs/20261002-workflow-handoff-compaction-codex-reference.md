# GA 工作流上游结果轻量 handoff 设计与 Codex 对照

日期：2026-10-02

## 背景

真实 `gpt-6-luna` Liu 国梁 workflow 暴露出一个重要的请求体放大问题：两个研究 child 的输入分别约 50k/30k tokens。旧版 planner 在渲染有依赖的 agent 时生成：

```javascript
上游结果：${JSON.stringify({researchA, researchB})}
```

这会把研究 child 的完整结果（通常包含长文本，且可能间接包含工具过程）直接拼进综合 child 的用户 prompt。综合 child 因此可能在真正发出请求前就出现请求体过大、初始化阻塞或超时。此前观察到 `agent_3` 长时间 `running` 且没有自己的 transcript/result，符合这一链路特征。

## Codex 源码对照

本次核对了 `D:\git_codes\codex\codex-rs`：

- `core/src/tools/handlers/multi_agents/spawn.rs` 的 spawn 返回的是 `agent_id`、nickname 等生命周期引用，而不是把另一个 agent 的完整 transcript 复制进当前工具调用结果。
- `core/src/tools/handlers/multi_agents/wait.rs` 以受控的状态快照等待 `AgentStatus` 变化；等待本身不等于读取完整历史。
- `core/src/agent/control/runtime_context.rs` 通过 agent path / thread id 等稳定引用构造子 agent 环境上下文。
- `analytics/src/client_tests.rs::realtime_handoff_tracks_only_marker_without_transcript` 明确验证 handoff 只记录 marker，不把私有 transcript 送入 analytics。
- `core/src/tools/handlers/multi_agents_spec.rs` 明确区分 spawn 的显式 message 与 `fork_context`；上下文是否传递是一个受控开关，不是把父 transcript 默认拼入每次请求。

结论：Codex 的可借鉴原则不是“永远不给下游上下文”，而是“默认传递小型控制面/结果引用；详细内容通过持久化对象或显式读取按需获得”。

## GA 目标契约

依赖 child 的请求只能收到以下 bounded handoff：

1. `status`：上游是否成功、失败或被跳过；
2. `summary`：长度受限的结论摘要；
3. `evidence` / `blockingIssues`：少量结构化证据；
4. `resultRef`：run artifact 中的逻辑结果引用，用于审计/恢复；
5. `handoffRef`：workspace-relative 的轻量 handoff 文件；
6. `artifactRefs`：workspace-relative 的研究/代码产物路径；
7. `transcriptRef`：仅审计引用，明确告知 child 不应默认读取。

禁止项：

- planner 生成 `JSON.stringify(upstreamResult)`；
- scheduler 将完整 `payload.text`、transcript events 或工具输出复制进依赖 prompt；
- 用绝对路径或 workspace 外路径作为下游读取路径；
- 通过 prompt 软约束来“希望模型不要带全文”，而没有宿主边界。

## 已实施改造

### Planner

有依赖的 agent prompt 改成固定的 bounded handoff 指令，不再插入 `JSON.stringify`。planner 提示词新增要求：大型研究结果应写入 workspace-relative JSON/Markdown 产物，下游按路径 `file_read`。

确定性 mixed planner 现在为研究阶段声明 `artifacts/research-sources.json`，要求研究 child 将详细来源写入并读回，再由后续 artifact child 使用。

### Scheduler

`workflow_scheduler.py` 新增：

- `_build_handoff(job, result)`：只构造摘要、证据、逻辑引用；
- `_build_dependency_handoff(job)`：依赖 child 启动前生成 bounded 列表；
- `downstream_result(job)`：RPC 返回给 workflow JS 的也是有界结果，即使脚本自行 `JSON.stringify(result)` 也不会拿到完整 transcript；
- `workflow-handoffs/<job_id>.json`：在 launch workspace 内持久化 handoff。该文件仅保存结果 payload 和引用，不保存 transcript events；详细研究数据应由 child 自己写入计划声明的 artifact。

缓存恢复路径同样生成 handoff，避免 resume 后重新退化为全文注入。

此外，scheduler 会在 child 启动前依据 `writeScope/deliverables` 预创建 workspace 内的父目录，避免模型反复猜测 `mkdir` 或因目录不存在而无法写入研究产物。artifact readback 也会搜索所有已完成 child 的路径匹配读取证据，而不再假设读取者必须被标记为 verification/review。

### Child prompt

`workflow_child_agent.py` 将 handoff 渲染为明确的 `Dependency handoff (bounded...)` 区块，并说明：

- 根据 workspace-relative `artifactRefs` / `handoffRef` 按需读取；
- `transcriptRef` 仅用于审计；
- 不期待完整 upstream transcript。

## 为什么不是只截断字符串

单纯在 prompt 上做字符截断仍然有三个问题：

1. 截断点可能落在 JSON/结构化证据中间，破坏可用性；
2. 任意用户/模型生成的 workflow script 仍可再次 `JSON.stringify`；
3. 结果引用、工作区边界和恢复语义没有统一契约。

因此 GA 同时在 planner、scheduler RPC 和 child prompt 三层收敛：planner 不生成全文注入；scheduler 返回有界对象；workspace artifact 负责按需读取。

## 回归验证

新增/更新测试覆盖：

- planner 渲染脚本不含依赖结果 `JSON.stringify`；
- child prompt 含摘要、`resultRef`、`artifactRefs`，不含大文本；
- scheduler 依赖 handoff 不包含 100k 字符 payload；
- workspace 中写出 `workflow-handoffs/agent_N.json`；
- 缓存 agent 的 handoff 与普通 agent 一致；
- 原有 workflow planner、validator、runtime、scheduler、child runner 测试继续通过。

## 后续真实验收

必须使用真实 `gpt-6-luna`，单独运行 workflow 用例（不并行），观察：

- 综合 child 的 request/transcript 是否在合理时间内出现；
