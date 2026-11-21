# GA 响应速度优化设计

日期：2026-11-22（Asia/Shanghai）
状态：已进行分阶段验证。实际保留的改动、数据和待验证项见[实施记录](20261122-ga-response-speed-optimization-implementation.md)。

## 1. Intent

让 GenericAgent 在执行时更接近 Pi agent 的体验：首个可见输出尽快出现，后续 token 连续、稳定、少停顿；长回答、长会话和多工具任务不会因为 UI 重算或 prompt 构造而出现“卡住”的感觉。

核心判断：优化不能只追求模型调用更快。用户感知的是：

```text
提交问题 -> 首个可见输出 -> 持续增量显示 -> 工具/下一轮继续 -> 最终完成
```

因此本方案先优化显示链路，再优化请求链路。真实模型验收只使用本项目的 `zhanlu ds` 配置，采用同一任务的 A/B 对照；未测量前不宣称秒数收益。

## 2. Goals

1. 缩短从 provider 首个可用 delta 到用户看到它的延迟。
2. 让低速、短 chunk、中文短片段和标点输出也能平滑显示。
3. 保持长文本输出的 `<Static>` 顺序、滚动、选区、光标、Markdown fence 和 resize 行为。
4. 降低长历史下活动 transcript 的重复格式化与换行计算。
5. 降低每轮 schema、工具 JSON、history 投影和 prompt 构造的额外时间。
6. 建立可复核指标，区分模型/网络等待、bridge 延迟、UI 调度延迟和终端绘制耗时。
7. 保留 GA 已有的权限、MCP、workflow、subagent、compaction、resume 和多 provider 行为。

## 3. Non-goals

- 不把 GA 改成 Pi 的 TypeScript 架构。
- 不删除权限校验、审批、路径 ACL、工具结果预算或工作流契约。
- 不默认关闭 thinking、降低模型质量或静默减少 `max_tokens`。
- 不把所有工具都改成并行执行；有副作用或共享状态的工具继续串行。
- 不把 MCP 后台发现改回启动/提问关键路径。
- 不在没有 A/B 测量和质量检查时宣称“已经比 Pi 快”。
- 不在本设计阶段调用真实 API；真实测量阶段才使用 `zhanlu ds`，并记录其 profile、endpoint、model、reasoning effort 和配置快照。

## 4. Constraints and invariants

### 4.1 项目约束

- Python 流程：`genericagent/core/app.py`、`genericagent/core/loop.py`、`genericagent/llm/core.py`。
- Ink bridge：`frontends/ink/bridge.py`，stdout 必须继续保持一行一个 JSONL event。
- Ink UI：`frontends/ink/ui/src/App.tsx`、`state.ts`、`streamCommit.ts`、`messagePartition.ts`。
- 测试：Python `unittest`；Ink 使用 Node test runner + `tsx`。
- 诊断脚本必须保留到 `frontends/ink/ui/probes/`，不得写完即删。

### 4.2 行为不变量

1. `assistant_done` 仍是本轮最终完整 display text；前端不能把已显示 delta 再拼一次。
2. `assistant_delta` 只能携带新增文本，不得退回每次发送完整 `full_resp`。
3. `Static` 只能接收严格前缀；活动消息不能插入到已经打印的静态行之前。
4. stream commit 不能把 Markdown code fence、表格、工具 header/结果切成非法片段。
5. `assistant_thinking` 仍与 prose 分离，并保持在产生它的 prose 之前。
6. stdout protocol 事件顺序保持：`ready`、`user/status`、delta/thinking、done、idle 等既有契约。
7. MCP 工具发现是后台观察通道，不能因优化显示链路重新阻塞首轮。
8. schema/history 缓存必须在模型切换、MCP 变更、权限变更、skills 变更、compaction、resume 时正确失效。
9. 真实 API 测量只使用 `zhanlu ds`，不把 Claude Code 或其他 profile 的 credential 混入结果。

## 5. Approaches considered

### A. 只调小两个节流常量

把 Python 的 16 字符阈值和前端的 80ms 改小。

优点：改动小，短期体感可能变好。

缺点：两个层级各自批处理，仍然不知道谁负责帧调度；更新次数可能暴涨；长历史下 React/Ink 重算压力会增加；没有可观测性，无法知道收益来自哪一层。

结论：不单独采用，只作为阶段一中的实验变量。

### B. 直接改成每个 provider chunk 立即 React dispatch

取消 Python 和前端所有批处理，让每个 chunk 都进入 reducer。

优点：实现直观，首个 token 最快。

缺点：把 provider chunk 粒度直接暴露给 React；高 token rate 下会产生大量 commit；容易放大 Markdown、换行、选区和终端 diff 的 CPU/IO；与 Pi 的设计也不一致，因为 Pi 仍有 TUI 帧合并。

结论：不采用。

### C. 综合路线：低延迟增量 + 单帧合并 + live/static 分离 + 请求链路缓存

第一阶段让数据尽快到达 UI，再由一个明确的调度层按 16-33ms 帧窗口合并；第二阶段优化 schema、工具 prompt 和 history 投影；每个改变单独测量和回归。

优点：符合 Pi 的真实结构；改善首个可见 token，同时控制渲染压力；能分别度量“模型慢”和“GA 显示慢”；风险可分阶段收敛。

结论：采用路线 C。

## 6. Target architecture

下文保留原始设计目标。本轮实际取消了逐片转发，保留 Python 16 字符门槛以避免缩小现有事件级脱敏窗口。已实现的显示、转录和请求路径见实施记录，各候选开关仍为显式启用。

### 6.1 第一阶段：显示链路

目标链路：

```text
provider chunk
  -> parser
  -> stream event queue
  -> bridge immediately forwards delta
  -> frontend frame scheduler (16-33ms)
  -> reducer updates one live tail
  -> active-only layout / static prefix unchanged
  -> Ink frame diff
```

职责重新划分：

- Python：只负责在“首 chunk 到达”或短时间窗口到期时提交增量，不用固定字符数决定是否显示。
- bridge：保持无缓冲转发；只负责 protocol 编码和 flush。
- Ink App：只保留一个明确的低延迟 frame scheduler；不再额外叠加 80ms 的固定等待。
- reducer：继续负责消息顺序与 stream commit；不改变 transcript 语义。
- 布局：静态 prefix 不因 live delta 变化而重新格式化；活动区域只重算 live tail。

### 6.2 第二阶段：请求链路

目标链路：

```text
turn boundary
  -> obtain stable schema snapshot
  -> obtain stable prompt sections / tool JSON
  -> project only history delta when safe
  -> provider request
```

缓存/增量投影必须显式带版本或 fingerprint：

- `tool_schema_fingerprint`：内置 schema、MCP 工具、capability profile、权限模式、目标 provider/model。
- `prompt_sections_fingerprint`：base/project/memory/skills/role/permission sections。
- `history_projection_anchor`：backend history 长度、最后一条消息的稳定 ID/摘要 hash、model/provider。
- `context_epoch`：compaction、resume、rewind、model switch、MCP schema change 时递增。

任何 fingerprint 不匹配都放弃复用，走现有正确路径；缓存优化不能改变工具集合或历史语义。

## 7. Implementation phases

### Phase 0：可观测性基线

新增一个关闭默认行为的 latency trace 开关，例如 `GA_LATENCY_TRACE=1`。只记录脱敏的时间和大小，不记录 API key、Authorization、完整 prompt 或完整用户文本。

至少记录：

- `submit_at`
- `task_accepted_at`
- `provider_request_start`
- `provider_first_chunk`
- `python_first_display_enqueue`
- `bridge_first_emit`
- `frontend_event_received`
- `frontend_dispatch`
- `render_commit`（可用 UI 帧 trace / 测试 harness 记录）
- `assistant_done`
- `idle`
- 每轮 input/output/cache token usage
- prompt chars、tools count、history message count
- schema build / history projection / JSON serialization 耗时

阶段 0 的产物：

- 一条可重放的 synthetic stream probe；
- 一条 bridge-to-Ink 的事件时间线 probe；
- 一条长历史下活动 delta 的渲染计数 probe；
- `zhanlu ds` 真实 A/B 测量脚本接口，但默认不运行真实请求。

### Phase 1：消除双重固定等待

候选实现：

1. Python 侧将固定字符阈值改为“首 chunk 立即 + 时间窗口/大小阈值”：
   - 首个非空 chunk 立即入队；
   - 后续 chunk 在短窗口内合并；
   - 以时间作为主要上限，大小作为吞吐保护；
   - turn banner、tool header、done 仍有立即路径。
2. 前端把 80ms 定时器改为单一 frame scheduler：
   - 目标窗口 16-33ms，具体值由 synthetic benchmark 和真实 `zhanlu ds` A/B 决定；
   - 新事件到达时只保留一个待调度 timer；
   - `assistant_done`、thinking boundary、status transition 强制 flush；
   - 不允许多个独立 timer 同时竞争同一 delta buffer。
3. 加入首 token 与低速 chunk 测试：
   - 第一个 chunk 为 1 字符；
   - 后续每 5-20ms 一个短 chunk；
   - provider 停顿后恢复；
   - tool-only / thinking-only / empty chunk；
   - 确认不丢、不重、不延迟到 done。

成功判据：

- synthetic stream 中首个可见 delta 不再等待满 16 字符；
- event-to-dispatch 延迟由固定约 80ms 降为一个渲染窗口级别；
- `assistant_done` 与 stream prefix 完全一致；
- 现有 Ink 测试与新增程序化帧测试通过。

### Phase 2：活动转录局部化

1. 明确 static prefix 与 active tail 的数据结构边界。
2. 对活动 assistant 使用可追加的 live buffer 或 memoized projection，避免每次都从完整 `state.messages` 重新格式化静态历史。
3. 保留 `state.messages` 作为语义真相，不能为了性能让 UI 自己维护一份不可对账的 transcript。
4. 为 fullscreen、inline、选区、滚动、resize 分别建立性能和正确性断言。
5. 继续使用 `streamCommit.ts` 的 fence-safe 分段策略；不把长文本优化改成简单截断。

成功判据：

- 长历史下，每次 delta 的静态 transcript render count 不增加；
- live tail render 只随当前活动段变化；
- 选区文本、滚动位置、工具展开状态和 resize 输出不变；
- 100KB 级 assistant 输出不会因每个 delta 产生线性增长的重复格式化。

### Phase 3：schema 与工具描述缓存

1. 将 `load_tool_schema()` 的静态 schema 读取与动态 MCP 合并拆开。
2. 为静态资产、MCP snapshot、capability filter 建立分层缓存。
3. 当 MCP 后台发现完成时，以新 fingerprint 原子替换 schema snapshot，不在用户 turn 内重复读取文件。
4. `ToolClient` 缓存稳定的 `tools_json` 和稳定协议前缀；工具集合变化时失效。
5. 保持 `last_tools` 现有语义作为兼容回退，但让 fingerprint 替代不稳定的字符串重算。

验证重点：

- MCP 新工具出现后下一轮可见；
- MCP 工具消失/禁用后不会继续暴露；
- permission/capability 变更立即失效；
- model switch 的 `_cn` schema 与 provider-specific tool shape 不串用。

### Phase 4：history 增量投影与 prompt 稳定性

1. 记录 backend history 的增量 anchor。
2. 对不变的历史前缀保留已规整的 provider message projection。
3. 只对新增 user/tool/assistant tail 做转换；
4. compaction、resume、rewind、model switch、provider API mode 变化时递增 `context_epoch`，放弃旧投影；
5. 审计 system prompt sections 的动态内容，避免无意义时间戳、排序不稳定或重复 JSON 破坏 cache prefix；
6. 记录 cache read / cache creation / cache miss 原因。

安全边界：

- 不直接修改 backend history 的事实内容；
- 不把 thinking、tool_use/tool_result 配对规则交给缓存猜测；
- provider 需要完整 history 时，增量投影只作为构造优化，最终 payload 仍必须符合 provider schema；
- 任何无法确认前缀有效的情况都回退到现有 full projection。

### Phase 5：工具批次与完成时间优化

1. 为每个 tool batch 记录准备、执行、序列化、显示四段耗时。
2. 检查 GA 现有 `_batch_tool_calls()` 是否因安全分类过度切分独立工具。
3. 只对已证明无共享状态的工具扩大并行范围；不改变权限检查顺序和副作用工具串行语义。
4. MCP 工具可以并发，但保持结果与 tool call ID 一一对应且按模型可读格式返回。
5. workflow/subagent 启动只在真正独立时并发，避免线程启动和上下文复制抵消收益。

## 8. Measurement plan

### 8.1 不调用 API 的本地测试

先用确定性 fake stream 验证：

- 1 字符首 chunk；
- 低速短 chunk；
- burst chunk；
- thinking -> prose；
- prose -> tool call；
- stream interruption / retry；
- long history + long output；
- resize / selection / scroll。

输出：

- first enqueue delay；
- first bridge emit delay；
- first frontend dispatch delay；
- render count；
- static render count；
- max queue depth；
- duplicate/missing text verdict。

### 8.2 `zhanlu ds` 真实 A/B

只在用户明确开始真实测量后运行。每组使用同一个 profile、同一个 model、同一个 endpoint、同一套工具和同一批任务，交错运行 baseline 与 candidate，避免按时间段比较服务波动。

建议任务集：

1. 纯文本短回答：观察首个可见输出和短 chunk 体验；
2. 中等代码解释：观察持续 token 速度与完整时间；
3. 单工具任务：观察 tool call 前后显示；
4. 多个独立只读工具任务：观察并行与 UI 连续性；
5. 长历史追问：观察 schema/history/prompt 开销；
6. thinking + tool + final 的多轮任务：观察 thinking/prose 边界。

每个任务至少记录：

- time to first provider chunk；
- time to first visible output；
- provider first chunk -> visible delta；
- time to last token；
- output tokens / input tokens / cache read tokens；
- model calls / tool calls；
- tool execution total；
- render count / max render time；
- final output correctness invariant。

不要把“显示更快”误报为“模型生成更快”。报告必须分列：

| 指标 | baseline | candidate | 含义 |
|---|---:|---:|---|
| Provider 首 delta |  |  | 模型/网络/服务端等待 |
| 首个可见输出 |  |  | 用户首次看到内容 |
| 首 delta 到可见 |  |  | GA 自身显示链路 |
| 最后 token |  |  | 完整模型/工具任务时间 |
| 输出速度 |  |  | token 生成速度 |
| cache read |  |  | prompt cache 是否命中 |
| 正确性 |  |  | 质量不下降证据 |

### 8.3 质量与回归门槛

每个候选改动必须同时满足：

- 目标延迟指标改善；
- 输出文本无丢失、重复、乱序；
- 工具调用参数和结果不变；
- 现有 Python focused tests 通过；
- Ink UI focused tests、typecheck 通过；
- 长文本、Markdown fence、thinking、resize、选区测试通过；
- 真实 `zhanlu ds` 任务的结果质量不低于 baseline，除非用户明确允许质量下降。

## 9. Rollback and flags

每个阶段先放在独立 feature flag 下：

- `GA_STREAM_SCHEDULER=legacy|frame`
- `GA_LIVE_TRANSCRIPT_RENDER=legacy|active_tail`
- `GA_SCHEMA_CACHE=off|on`
- `GA_HISTORY_PROJECTION=full|incremental`

默认切换顺序：

1. synthetic tests 和 focused tests 验证；
2. 本地手动/伪 TTY 验证；
3. `zhanlu ds` A/B；
4. 用户确认后再改变默认值；
5. 稳定后再移除旧路径。

出现以下任一情况立即回退对应 flag：

- 首 token 更快但输出重复/缺失；
- `assistant_done` 造成历史重复；
- stream fence 破坏后续 Markdown；
- resize/selection/cursor 回归；
- MCP 工具 schema 过期或越权暴露；
- cache 命中下降且无法解释；
- 完整任务时间或质量显著恶化。

## 10. Recommended implementation order

1. Phase 0 trace + deterministic stream probes。
2. Phase 1 消除 16 字符 + 80ms 双重等待。
3. Phase 2 active tail render isolation。
4. 用 `zhanlu ds` 做显示阶段 A/B，确认体感收益。
5. Phase 3 schema/tool prompt cache。
6. Phase 4 history incremental projection。
7. Phase 5 tool batch timing and safe parallel expansion。
8. 全量回归、真实 A/B、更新文档和实施记录。

## 11. Definition of done

本方案完成的标准不是“改了几个常量”，而是：

- 用户看到的首个输出延迟明显下降，且有时间线数据证明；
- token 输出连续，低速短 chunk 不再出现固定停顿；
- 长历史下 UI 不随静态 transcript 规模线性变慢；
- 首 token、cache、history、schema、tool execution 的时间可分别观测；
- `zhanlu ds` A/B 显示响应改善，同时回答质量、工具正确性和终端契约保持；
- 任何阶段都可以通过 flag 独立回退。
