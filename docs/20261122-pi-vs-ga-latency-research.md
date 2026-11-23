# Pi 与 GA 响应速度对比调研

日期：2026-11-22（Asia/Shanghai）

## 1. 调研目标

用户实测 Pi agent 在执行时 token 输出非常快，像流水一样连续；GenericAgent（GA）虽然已经支持流式输出，但在终端中更容易出现 token 一段一段出现、短暂停顿或看起来卡住的感觉。

本调研回答三个问题：

1. Pi 的“快”和“丝滑”分别来自模型/网络，还是来自 agent loop 与终端显示链路？
2. GA 当前哪些环节会人为增加首个可见 token 和后续 delta 的等待？
3. 哪些优化可以在保留 GA 的权限、MCP、workflow、会话和多前端能力的前提下落地？

本轮只做源码分析，不调用真实模型、不修改生产代码。后续真实 API 验收限定使用本项目已有的 `zhanlu ds` 配置。

## 2. 结论先行

### 2.1 最大的体感差距在显示链路

Pi 的关键路径是：

```text
provider stream event
  -> agent-loop message_update
  -> interactive assistant component update
  -> TUI requestRender
  -> 约帧率合并 + 差分终端绘制
```

GA 的关键路径是：

```text
provider stream chunk
  -> Python _drain_agent_stream 累积
  -> 满 16 字符才放入 display_queue
  -> Ink bridge 转 JSONL assistant_delta
  -> React App 再等待 80ms 合并
  -> reducer 修改 messages
  -> 活动消息格式化 / 换行 / 视口计算
  -> Ink 重绘
```

因此，GA 即使已经从上游收到 token，也可能因为两层批处理暂时不显示。当前最明确的体感瓶颈是：

- Python 侧 `STREAM_FLUSH_CHARS = 16` 的大小门槛；
- Ink UI 侧 `assistant_delta` 的固定 80ms 节流；
- 活动消息更新带来的 React 状态更新和转录计算。

### 2.2 Pi 不是“每个 token 都立即重画”

Pi 也做节流，但节流位置更靠近渲染器，并且以帧为单位合并：

- `packages/agent/src/agent-loop.ts:424-450`：provider 事件到达时立即更新 partial assistant message，并发出 `message_update`。
- `packages/coding-agent/src/modes/interactive/interactive-mode.ts:3691-3748`：收到 `message_start` / `message_update` 后更新流式 assistant 组件，并调用 `ui.requestRender()`。
- `packages/tui/src/tui.ts:1143-1210`：`requestRender()` 只请求一次待渲染帧，按 `MIN_RENDER_INTERVAL_MS` 合并更新，然后调用差分渲染。

这形成了“事件低延迟进入 UI、渲染按帧合并、终端只写变化部分”的路径。GA 应借鉴这个分层，而不是简单地把所有缓冲和节流删除。

### 2.3 GA 的 MCP 冷启动已经不是唯一主因

GA 已经实现了 MCP 后台发现：

- `genericagent/mcp/runtime.py:1188-1223` 的 `discover_mcp_tools_cached_fast()` 命中缓存立即返回，未命中最多等待有限 budget，然后后台发现；
- `genericagent/core/app.py:527-543` 启动 MCP schema warmup；
- `frontends/ink/bridge.py:1158-1205` 以独立线程观察 MCP 状态。

这解决了过去“每次用户提问同步等 MCP discovery”的主要冷启动问题。但 `genericagent/core/app.py:2160-2162` 仍在每个用户 turn 重新调用 `load_tool_schema(..., include_mcp_tools=True)`，所以工具 schema 的重复读取、能力过滤和 prompt 构造仍应在第二阶段测量和优化。

## 3. 端到端延迟分解

用户看到的等待至少可以分为：

```text
T_submit_to_provider
  = 命令入队
  + 自动压缩 / hook / prompt 准备
  + MCP/schema 准备
  + history 投影与序列化
  + HTTP 连接与服务端排队

T_provider_to_first_delta
  = 模型首 token / 首个 tool-call delta

T_first_delta_to_visible
  = provider parser
  + GA stream batching
  + display queue
  + JSONL bridge
  + frontend batching
  + layout/render

T_task_complete
  = 所有 model calls
  + tool execution
  + loop continuation
  + transcript persistence
  + final render
```

本轮没有真实 API timing，因此不能把任何秒数差异归因给模型或网络。源码足以确认的是 `T_first_delta_to_visible` 中存在 GA 自己引入的等待；`T_submit_to_provider` 和 `T_provider_to_first_delta` 需要后续使用 `zhanlu ds` 实测。

## 4. Pi 源码证据

### 4.1 provider stream 事件立即进入 agent loop

坐标：`D:/git_repos/pi/packages/agent/src/agent-loop.ts:390-477`。

`streamAssistantResponse()` 对 provider 返回的异步事件逐个消费：

- `start` 时把 partial message 放入 context；
- `text_delta`、`thinking_delta`、`toolcall_delta` 等事件到达时更新 partial message；
- 每次更新都发出 `message_update`；
- `done` / `error` 时再发 `message_end`。

这里没有一个“先积累到固定字符数再交给 UI”的字符门槛。模型流的粒度直接保留到 agent event 层。

### 4.2 TUI 以 requestRender 合并，而不是让业务层等待字符数

坐标：`D:/git_repos/pi/packages/coding-agent/src/modes/interactive/interactive-mode.ts:3691-3748`、`D:/git_repos/pi/packages/tui/src/tui.ts:1143-1210`。

收到 assistant 更新后，Pi 立即调用 `ui.requestRender()`。TUI 内部负责：

- 同一轮事件只保留一个待渲染请求；
- 如果距离上次渲染不足 `MIN_RENDER_INTERVAL_MS`，使用一个定时器推迟到下一帧；
- 渲染时统一生成当前帧，并做差分输出；
- 用户输入等需要抢占的场景使用 `renderNow()` / immediate render。

因此 Pi 的延迟上限主要由渲染帧间隔决定，而不是由“累积多少字符”决定。短 token、标点和模型停顿不会被固定 16 字符门槛额外吞掉。

### 4.3 Pi 的流式 Markdown 组件复用渲染槽

坐标：`D:/git_repos/pi/packages/coding-agent/src/modes/interactive/components/assistant-message.ts:123-136`。

`AssistantMessageComponent.updateContent()` 会复用同类型的 Markdown slot，避免每次流更新都重新创建所有 Markdown 组件。它仍会更新当前内容，但已经有组件级缓存和局部复用。

## 5. GA 源码证据

### 5.1 Python 层有固定 16 字符门槛

坐标：`genericagent/core/app.py:97`、`genericagent/core/app.py:373-374`、`genericagent/core/app.py:1868-1895`。

当前逻辑：

```python
STREAM_FLUSH_CHARS = 16

while stream:
    chunk = next(gen)
    full_resp += chunk
    if should_flush_display_delta(full_resp, last_pos, chunk):
        display_queue.put({"next": full_resp[last_pos:]})
```

只有满足以下任一条件才进入显示队列：

- 自上次显示累计至少 16 个字符；
- chunk 匹配 turn banner。

这会直接造成两个体验问题：

1. 模型输出短句、标点或中文小片段时，已经收到的内容不会立即显示；
2. 模型输出速度低于 16 字符/批次时，显示会等到下一个较大 chunk，用户感知为卡顿。

该门槛原本是为了减少跨线程和前端更新次数，但它把“传输批处理”与“渲染帧批处理”混在了一起。

### 5.2 bridge 负责转发，但没有独立的低延迟调度层

坐标：`frontends/ink/bridge.py:3887-3923`。

`_consume_display_queue()` 从队列取得 `next` 后立即发出：

```python
self.emit({"type": "assistant_delta", "taskId": task_id, "text": ...})
```

这是正确的边界：bridge 没有再次故意等待。但它只能消费 Python 已经放入的批次，所以 Python 的 16 字符门槛已经决定了最早可见粒度。

### 5.3 Ink UI 又增加了固定 80ms 的 delta 窗口

坐标：`frontends/ink/ui/src/App.tsx:1562-1572`、`frontends/ink/ui/src/App.tsx:1804-1813`。

当前逻辑把收到的 delta 存入 `pendingDeltas`，第一次收到后启动 80ms 定时器，定时器触发时才 dispatch `assistant_delta`。这相当于在 Python 16 字符门槛之后又增加一层固定时间等待。

这层等待的收益是减少 React commit 次数，但代价是：

- 首个可见 token 可能额外等待近 80ms；
- 模型 token 速度低于窗口时，界面明显“一顿一顿”；
- Python 和前端各自批处理，两个窗口叠加后产生不稳定的显示延迟。

### 5.4 reducer 将活动 assistant 作为完整 message 持续追加

坐标：`frontends/ink/ui/src/state.ts:510-525`。

每次 `assistant_delta` 都会：

1. 找到当前 assistant message；
2. 创建新的 `messages` 数组；
3. 拼接完整的 assistant 文本；
4. 调用 `finalizeOpenUserMessages()`；
5. 调用 `commitStreamingAssistantMessages()`。

这个协议能保证 `<Static>` 顺序和长文本分段，是必要的稳定性设计，但每个 dispatch 都会触发 App 重新计算依赖 `state.messages` 的派生值。

### 5.5 App 的转录派生计算仍有较大范围

坐标：`frontends/ink/ui/src/App.tsx:2831-2893`。

App 已经有 `splitStaticAndActiveMessages()`，这是正确方向：

- `messagePartition.staticMessages` 使用静态内容；
- `messagePartition.activeMessages` 使用当前活动内容；
- inline 模式的活动视口只处理 `activeMessages`。

但 App 同时还维护完整 `transcriptRows`，并在 `state.messages` 变化时重新进行完整转录格式化和换行。fullscreen、选区、滚动和 frame model 还会依赖这条完整路径。短文本时影响不明显，长历史 + 高频 delta 时会放大。

## 6. 工具执行差异

Pi 默认允许独立工具并行执行：

- `packages/agent/src/agent-loop.ts:518-533` 根据配置或工具 `executionMode` 决定串行/并行；
- `packages/agent/src/agent-loop.ts:592-660` 对可并行工具使用 `Promise.all()`。

GA 已经实现了受控并行：

- `genericagent/core/loop.py:82-109` 定义安全工具和 MCP 工具的并行模式；
- `genericagent/core/loop.py:180-238` 使用线程执行并行批次；
- 有副作用、共享 cwd、审批和子代理控制类工具保持串行。

因此“工具一定串行”已经不是 GA 当前的普遍事实；仍需测量的是工具批次划分、线程启动开销和工具结果序列化。

## 7. prompt / history 路径的待测疑点

### 7.1 每轮都会重新加载工具 schema

坐标：`genericagent/core/app.py:2160-2162`。

每个用户 turn 都会调用 `load_tool_schema(..., include_mcp_tools=True)`。即使 MCP 缓存命中，也仍可能经历：

- schema 文件读取与 JSON 解析；
- MCP 工具合并；
- capability profile 计算；
- goal proposal 过滤；
- 后续工具 JSON 构造。

这类时间目前没有按阶段记录，需要在第二阶段加入 timing。

### 7.2 legacy / 非原生 provider 每轮会重建工具 prompt

坐标：`genericagent/llm/core.py:1408-1454`。

`ToolClient.chat()` 会复制 tools，并调用 `_build_protocol_prompt()`；`_prepare_tool_instruction()` 会对 tools 做 `json.dumps()`，再根据 `last_tools` 决定是否复用简短工具状态。

这对必须使用文本协议的 provider 有意义，但应确认：

- `tools_json` 是否每轮字节稳定；
- 是否可以缓存 `tools_json` 和稳定协议前缀；
- `last_tools` 是否因工具顺序、动态 MCP 工具或 schema 过滤频繁失效。

### 7.3 原生 provider 每轮仍会深复制 history

坐标：`genericagent/llm/core.py:1174-1193`、`genericagent/llm/core.py:1317-1335`。

`BaseSession.ask()` 和 `NativeClaudeSession.ask()` 都会复制当前 history，再做裁剪、消息规整和 provider 转换。深复制是隔离与重试安全的重要手段，不能直接删除；更合适的方向是：

- 先测量复制/转换耗时；
- 对不变前缀使用稳定快照；
- 仅对新增尾部做增量投影；
- compaction / resume / model switch 时显式失效缓存。

## 8. 已确认事实与待验证假设

### 已确认事实

- GA Python 层存在 16 字符显示门槛。
- GA Ink UI 层存在 80ms assistant delta 合并窗口。
- Pi 把 provider 增量直接转成 message update，再由 TUI 按帧合并。
- GA bridge 本身不是主要的额外等待点，它及时转发队列项。
- GA 已有 MCP 后台发现和受控工具并行，不能再把所有延迟归咎于 MCP。

### 仍需使用 `zhanlu ds` 验证

- GA 与 Pi 的 provider 首 token是否来自不同模型/端点/推理设置。
- `zhanlu ds` 下 GA 的 provider 首 delta 到 Python queue、bridge emit、Ink reducer 和实际 stdout 的各段耗时。
- 降低批处理后，完整任务时间是否变化，还是只改善首个可见输出。
- 活动 transcript 重算在长历史下是否成为 CPU 热点。
- schema/history/prompt 优化是否降低首 token，是否影响 cache hit 和回答质量。

## 9. 优化方向

按用户选择的路线 C：

1. **阶段一：显示链路流畅化**
   - 用“首 chunk 立即 + 短时间窗口合并”替代固定 16 字符门槛；
   - 将前端 80ms 改为与渲染帧协调的低延迟窗口；
   - 保持长文本 stream commit，不让每个 delta 重印整个静态历史；
   - 增加端到端显示时间线指标。
2. **阶段二：请求链路提速**
   - schema 缓存与变更失效；
   - provider 工具 JSON / 稳定 prompt 前缀缓存；
   - history 增量投影；
   - prompt cache 命中与每阶段耗时可观测；
   - MCP 仅按 direct / deferred / codemode 的实际需求进入模型上下文。

详细实施边界、验收和回滚规则见 `docs/20261122-ga-response-speed-optimization-design.md`。
