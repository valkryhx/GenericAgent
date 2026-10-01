# Pi agent 响应速度分析与 GA 借鉴指南

- 日期：2026-10-01（Asia/Shanghai）
- 目的：分析 `D:\git_codes\pi`（Pi agent harness，作者 Armin Ronacher 等）为什么启动与响应明显快于 Codex，并沉淀成 GA 需要提速时可直接参考的机制清单。
- 范围：本文只做只读分析与建议，不修改 GA 运行时代码。`AGENTS.md` 只保留 pi 的路径与一句话定位，细节以本文为准。

## 一、Pi 项目概况

- 本地路径：`D:\git_codes\pi`
- 形态：TypeScript monorepo（npm workspaces），`packages/coding-agent` 版本 `0.99.1`
- 入口：`dist/bundle/cli.js`，另有 `scripts/build-binaries.sh` 产出 standalone 二进制（无 Node 启动开销）
- 包划分：`packages/agent`（pi-agent-core，agent 循环）、`packages/ai`（多 provider LLM API）、`packages/coding-agent`（CLI/运行时）、`packages/tui`、`packages/codemode`、`packages/mcp`、`packages/durable`、`packages/chord`、`packages/telemetry`、`packages/server`、`packages/session-backends`
- 重要差异：Pi 没有内置权限系统，隔离靠容器化（`packages/coding-agent/docs/containerization.md`）。它的速度优势不是靠牺牲安全换来的，但 GA 有 permission/workflow 门禁，借鉴时要保留自己的授权层。

## 二、Pi 快的六个机制（按对 GA 的可借鉴价值排序）

### 1. MCP 连接完全异步化，首个 prompt 不再等所有 server

Pi 的做法（`packages/coding-agent/src/extensions/mcp/index.ts`，commit `e029c3ed0` stop waiting for MCP servers on the first prompt）：

- 首个 prompt 只等待 `exposure: "direct"` 的 server，且默认上限 `DEFAULT_STARTUP_WAIT_MS = 10_000`。
- 其余 server 在后台连接。codemode 脚本、`tool_search`、resource tools 各自在真正需要时才等自己依赖的 server。
- server 列表挪进 system prompt 的 `mcp_servers` 段（`renderServersSection()`，上限 `MAX_SERVERS_SECTION_CHARS = 4096`），在 `before_agent_start` 注入。
- 关键设计：工具声明不再随 server 连接状态变化，因此不会破坏 prompt cache；新增 server 只以「对话追加」的形式进入上下文。

exposure 模型（`packages/coding-agent/src/core/mcp-servers.ts`）：

| exposure | 含义 | 首 prompt 是否等待 |
|---|---|---|
| `codemode`（默认） | 脚本内用 `searchTools()` / `describeNamespace()` 查找并调用 | 否 |
| `deferred` | 用 `tool_search` 显式加载 | 否 |
| `direct` | 像内置工具一样声明给模型 | 是（最多 10s） |
| `hidden` | 不可达 | 否 |

GA 对应现状：`agentmain.py:821` 每次用户提问都同步调用 `load_tool_schema(..., include_mcp_tools=True)`，内部 `mcp_runtime.discover_mcp_tools_cached()` 会阻塞在 MCP 发现上。2026-10-01 实测（本机 `mcp.json` 共 6 个 server，其中 tavily/exa/finance 为远程 HTTP/SSE）：

```
cold discover: 16.01s  tools=6
warm discover:  0.016s tools=6
```

本机 `temp/mcp_tools_cache.json` 当前 `complete=false`（有 server 超时），而 `mcp_runtime.py:25` 对不完整结果只给 `_MCP_TOOLS_CACHE_INCOMPLETE_TTL = 60.0` 秒。因此每 60 秒就会再付一次十几秒的冷启动代价，且这笔开销正好落在「用户提问 → 首个 token」之间。

可借鉴动作（收益最大、改动最小）：

1. 把 MCP 工具发现挪出提问关键路径：命中缓存立刻返回；未命中则先用已有工具发起本轮，发现结果在后台完成后追加进 schema（对齐 pi 的「appended to the conversation instead of changing tool declarations」）。
2. 给 `discover_mcp_tools_cached()` 增加「部分结果即可用」语义：不完整缓存也沿用完整 TTL，另设后台刷新，不再让 60 秒 TTL 逼出同步冷启动。
3. 区分 direct 与间接工具：只有被模型直接调用的 MCP 工具需要进首轮 schema，其余走按需加载。
4. 启动阶段做一次后台预热（`agentmain.py:369` 已用 `include_mcp_tools=False` 启动，可在此之后再起一个后台发现线程）。

### 2. 运行时按需 lazy load

Pi 的范式是一个两行文件：

```ts
/** Loads the MCP client, transports, and OAuth sign-in on first use (see runtime.ts). */
export const loadMcpRuntime = () => import("./runtime.ts");
```

- `packages/coding-agent/src/extensions/mcp/runtime.lazy.ts` → `runtime.ts`（465 行，含 MCP client / transports / OAuth）
- `packages/coding-agent/src/extensions/codemode/execute.lazy.ts` → `execute.ts`
- MCP runtime 只在 `mcp.json` 配了 server 时才加载；连接在 TUI 首帧渲染之后才开始。

GA 对应现状：`import mcp_runtime` 本身只要 0.10s（已实测），所以 GA 的瓶颈不是 import 而是发现过程；但同一范式适用于 GA 里任何「重依赖 + 低频使用」的模块。

### 3. 启动期工作全部延后到首帧之后

Pi 的相关 commit：`c889eb880`（defer startup model catalog refresh）、`b14250412`（defer until after TUI startup）、`faecac2ca`（reduce bundled startup work）、`2b0a123de`（remove themes section from startup banner）、`dd01f5b24`（speed up recent session discovery）、`590144609`（reduce fuzzy search latency）、`304275f50` / `40c256ccc`（defer extension runtime / loader deps）、`cec3a91c0`（defer uncommon syntax grammars）。

结论：Pi 把「用户看到界面」和「系统准备完毕」拆成两个时刻，凡是首帧渲染不需要的工作都推到之后。

GA 对应现状：`import agentmain` 实测 0.50s，启动阶段本身不算慢；GA 的问题集中在提问路径上的同步 MCP 发现（见第 1 条）。

### 4. prompt cache 是一等公民

- `packages/coding-agent/src/core/cache-warmer.ts`：主动预热。`MAX_WARMING_AGE_MS = 60min`、`MAX_IDLE_WARMING_AGE_MS = 30min`、`CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS = 0.05` 美元、`IDLE_CONTINUATION_PROBABILITY = 0.15`；`getCacheWarmingDelayMs()` 在 TTL 的 90% 处刷新并保留至少 10s 余量；`isReplayable()` 对 Anthropic budget thinking 做例外处理（重放会改变 `budget_tokens`，命中不了缓存）。
- `packages/coding-agent/src/core/cache-stats.ts`：`CACHE_TTL_MS = 5min`、`NOISE_FLOOR_TOKENS = 1024`，统计 cache miss 与 missedCost。

GA 对应现状：GA 已有 prompt cache 打标（`llmcore.py` 的 `cache_control`、`prompt_cache_key`、`cached_tokens` 统计，约 374 / 418 / 425 / 529 / 771 / 779 / 852 / 855 / 862 行），但没有主动预热与 miss 成本统计。可借鉴的是「预热 + 可观测」：把 cache miss 变成可观测指标，才能在提速时判断收益来自哪一层。

### 5. 流式 + 并行工具执行

`packages/agent/src/agent-loop.ts`（940 行）：

- push-based `EventStream`，`streamAssistantResponse()` 逐事件 emit `text_delta` / `thinking_delta` / `toolcall_delta`。
- `executeToolCalls()` 默认走 `executeToolCallsParallel()`；只有工具标记 `executionMode === "sequential"` 或 config 指定才串行（第 517-522 行）。
- `DeclareToolChanges`：工具加载变化作为 system message delta 声明，保持 transcript 可重放。
- 支持 steering messages（模型等待期间用户输入可被拾取）、follow-up、`prepareNextTurn`。

GA 对应现状：GA 的流式已实现（`llmcore.py:_stream_with_retry` 后台线程 + queue 消费 chunk，`llmcore.py:430-517`），但 `agent_loop.py:68` 的工具执行是严格串行的——同一轮多个 tool_call 逐个 `yield from`。对 GA 提速这是第二个明确候选点（尤其 subagent 并发启动和多 MCP 调用场景）。

### 6. HTTP 层调优

`packages/coding-agent/src/core/http-dispatcher.ts` 用 undici：`DEFAULT_HTTP_IDLE_TIMEOUT_MS = 300_000`，并把 `DEFAULT_AUTO_SELECT_FAMILY_ATTEMPT_TIMEOUT_MS` 从 Node 默认 250ms 提到 2_000ms（注释：Node 的 250ms 默认会误杀高延迟路由上的合法连接尝试）。

GA 对应现状：GA 用 `requests`（`llmcore.py`），没有等价的自适应 family 超时。若 GA 走自建端点的多线路由，可参考该常量语义。

## 三、关键 commit 哈希（备查）

| commit | 内容 |
|---|---|
| `e029c3ed0` | stop waiting for MCP servers on the first prompt（首 prompt 不再等所有 MCP server） |
| `de3491ff1` | load MCP and codemode runtimes on first use（lazy load） |
| `c889eb880` | defer startup model catalog refresh |
| `b14250412` | defer until after TUI startup |
| `faecac2ca` | reduce bundled startup work |
| `2b0a123de` | remove themes section from startup banner |
| `dd01f5b24` | speed up recent session discovery |
| `590144609` | reduce fuzzy search latency |
| `304275f50` / `40c256ccc` | defer extension runtime / loader dependencies |
| `cec3a91c0` | defer uncommon syntax grammars |
| `1ef250772` | split MCP deferred exposure into tool_search and codemode modes |

## 四、GA 后续提速候选（按性价比排序，需用户拍板后再动代码）

1. MCP 发现异步化 + 缓存策略修正（`agentmain.py:821`、`mcp_runtime.py:723-760`）：预计消除每 60 秒一次的约 16 秒提问阻塞。
2. 同轮工具并行执行（`agent_loop.py:68`）：对多工具轮次和 subagent 场景直接生效。
3. prompt cache 预热与 miss 统计：把提速收益量化，避免凭感觉优化。
4. HTTP 层自适应超时：仅当 GA 使用多线路由 / 自建端点时才需要。

## 五、不适用的部分

- Pi 不做权限系统，GA 的 `workflow_permissions.py`、subagent 权限档、approval gate 不能照搬 Pi 的「无门禁」形态。
- Pi 的 `direct` 首 prompt 等待上限是 10s；GA 若采用类似策略，需要先决定「哪些 MCP 工具值得进首轮 schema」，否则会退化成本文第一条描述的问题。
