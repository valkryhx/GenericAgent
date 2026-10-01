# GA 响应速度优化进度（参照 Pi agent）

- 日期：2026-10-01（Asia/Shanghai）
- 目标：按 `docs/20261001-pi-agent-speed-reference.md` 列出的候选顺序，逐项优化 GA 的启动延迟与响应速度，并逐项记录证据。
- 约束：每项都先写回归测试、再改实现、跑完整 `tests/` 套件，最后单独提交，便于回滚。

## 优化项总览

| # | 项目 | 状态 | 备注 |
|---|---|---|---|
| 1 | MCP 发现移出提问关键路径 + 缓存策略修正 | 已完成 | 提问路径 ~0ms，冷启动发现转入后台 |
| 2 | 同轮工具并行执行（对齐 Pi 的 `executeToolCallsParallel`） | 已完成 | MCP 与 file_read 并行，其余保持串行 |
| 3 | prompt cache 预热与 miss 统计 | 已完成 | 新增 `cache_stats.py`，接入 usage 记录 |
| 4 | HTTP 层连接复用 | 已完成 | 会话级连接池，去掉每次请求的 TCP/TLS 握手 |

## 优化项 1：MCP 发现移出提问关键路径

### 基线证据（2026-10-01 实测）

```
import mcp_runtime:  0.10s
COLD discover:      16.01s  tools=6
WARM discover:       0.016s tools=6
```

根因有两层：

1. `agentmain.py:821` 每次用户提问都同步调用 `load_tool_schema(..., include_mcp_tools=True)`，内部 `mcp_runtime.discover_mcp_tools_cached()` 会阻塞在 MCP 发现上。
2. `mcp_runtime.py:25` 的 `_MCP_TOOLS_CACHE_INCOMPLETE_TTL = 60.0`：只要任何一个 server 超时，缓存就被标记 `complete=false`，60 秒后 `_read_mcp_tools_cache()` 直接返回 `None`，逼出一次同步冷启动。本机 `mcp.json` 有 6 个 server（tavily/exa/finance 为远程 HTTP/SSE），很难长时间保持 `complete=true`，于是这个 60 秒循环会反复触发。

### 改动内容

- `mcp_runtime.py`
  - 新增 `_read_mcp_tools_cache_entry(..., allow_stale=False)`：返回 `(tools, complete, cached_at)`，`allow_stale=True` 时不再因 `complete=false` 或超龄而丢弃缓存。
  - 新增后台发现线程：`start_background_discovery()` / `wait_for_background_discovery()` / `mcp_discovery_warmup_state()`。
  - 新增 `discover_mcp_tools_cached_fast()`：命中缓存（含 partial/stale）立即返回，只对 partial 结果调度后台复检；无缓存时最多等待 `GA_MCP_DISCOVERY_BUDGET`（默认 2.0 秒），超时返回当前可用工具并把剩余发现留在后台。
  - 新增 `on_mcp_discovery_complete()`：后台发现完成后回调，供 schema 就地补齐。
- `agentmain.py`
  - `load_tool_schema()` 的 MCP 分支改走 `discover_mcp_tools_cached_fast()`。
  - 注册一次性回调，后台发现完成后把新工具**就地 append** 到当前 `TOOLS_SCHEMA`，使同一轮后续 turn 立即可见（`agent_loop` 每 turn 都复用同一个 list 引用）。
    - 进程启动时（`__main__` 里 `GeneraticAgent()` 构造之后）触发一次后台预热。

### 为什么保留"部分结果即可用"

原 TTL 的意图是不让一次远程超时永久隐藏某个 server 的工具。新方案保留该意图但去掉阻塞：partial 缓存继续可用，同时后台跑一次完整复检，成功后就地替换缓存并补齐 schema。

### 验证记录

- 新增回归测试 `tests/test_mcp_runtime.py::McpFastDiscoveryTest`：
  - `test_fast_discovery_reuses_partial_cache_without_blocking`：把 partial 缓存年龄改到旧 TTL 的 100 倍，快速路径仍在 1s 内返回；
  - `test_fast_discovery_without_cache_respects_budget`：无缓存 + 永不响应的 server，0.5s 预算内返回并把发现留在后台。
- `python -m unittest discover -s tests`：1027 passed, 3 skipped（改动前 1025 + 新增 2）。

### 实测收益

改造前每次用户提问都要同步付一次 MCP 发现成本；改造后提问路径只读缓存 / 已连接状态：

| 场景 | 改造前 | 改造后 |
|---|---:|---:|
| 冷缓存单次提问 | 16.02s | 2.01s（预算上限，返回已连上的工具） |
| 后台发现完成后的提问 | 16.02s | 0.016s |
| partial 缓存变旧（旧 60s TTL 已过） | 16.02s（重新同步发现） | 0.000s（直接返回，后台复检） |

进程真实启动时序（`start_mcp_schema_warmup()` 在 agent 构造后立即触发，模拟用户看到界面后马上提问）：

```
turn at t= 0.5s ->  0.015s, 16 tools
turn at t= 2.0s ->  0.000s, 16 tools
turn at t= 5.0s ->  0.000s, 16 tools
turn at t=10.0s ->  0.000s, 16 tools
```

后台发现总耗时 10.66s，其中本地 stdio server 通过 npx 冷启动占 3.4-8.5s；这段开销现在完全在后台，不再阻塞提问。

### 补充说明

- `agentmain.py` 的 MCP 分支现在调用 `discover_mcp_tools_cached_fast()`，并注册 `_on_background_mcp_discovery` 回调；后台发现完成后工具会就地 append 到 `TOOLS_SCHEMA`，`agent_loop` 每 turn 复用同一个 list 引用，因此同一轮后续 turn 立即可见新工具。
- 预热的触发点在 `agentmain.py` 的 `__main__`（`agent = GeneraticAgent()` 之后），而不是模块导入时，避免纯导入路径（测试、工具脚本）启动后台线程。
- 缓存语义：`_read_mcp_tools_cache()` 保持原契约（partial 结果 60s 过期）不变，新增 `_load_mcp_tools_cache_entry(allow_stale=True)` 供快速路径使用。

### 状态

已完成并提交。`mcp_runtime.py` 新增 `available_mcp_tools`、`_load_mcp_tools_cache_entry`、`start_background_discovery`、`wait_for_background_discovery`、`mcp_discovery_warmup_state`、`on_mcp_discovery_complete`、`discover_mcp_tools_cached_fast`；`agentmain.py` 的 `load_tool_schema` 走快速路径，并新增 `append_mcp_tools`、`_on_background_mcp_discovery`、`start_mcp_schema_warmup`。

## 优化项 2：同轮工具并行执行

### 基线问题

`agent_loop.py` 原先把一轮里的多个 tool_call 严格逐个 `yield from`：同一轮发出 3 个 MCP 搜索就要串行付 3 次网络往返。Pi 的 `agent-loop.ts:508-522` 默认走 `executeToolCallsParallel()`，只有工具显式标记 `executionMode === "sequential"` 才串行。

### 改动内容

- `agent_loop.py`
  - 新增 `_tool_execution_mode()` / `_batch_tool_calls()`：把连续的同模式 tool_call 分组成批。
  - 并行白名单只有两类：所有 `mcp__*` 工具，以及 `file_read`。这两类没有共享 handler 状态、不写全局副作用。
  - 显式保持串行的：`code_run`（会 `os.chdir` 进程）、`file_write` / `file_patch`、浏览器控制、subagent 控制、`ask_user`、`update_working_checkpoint`。它们依赖执行顺序或独占访问，混进线程会改变语义。
  - 新增 `_run_single_tool()`：逐 chunk 流式输出，与旧循环的围栏语义、非 verbose 只取 outcome 的行为一致。
  - 新增 `_run_parallel_tools()`：线程 + queue 收集输出，边到边打印，最终**按原始顺序**返回 `(index, tool_call, outcome)`，保证 tool_results 与模型给出的顺序对齐。
  - 单元素并行批不会起线程，直接走串行路径。

### 验证记录

- 新增 `tests/test_agent_loop_parallel_tools.py`，10 个用例：
  - 模式判定：MCP / `file_read` 为并行；`code_run`、`file_write`、`file_patch`、`web_execute_js`、`ask_user`、`spawn_agent`、`close_agent`、`update_working_checkpoint`、`no_tool` 为串行。
  - 分批：`[mcp, mcp, code_run, file_read, mcp]` → `[(parallel,[0,1]),(sequential,[2]),(parallel,[3,4])]`。
  - 两个各 0.4s 的并行调用整体 < 0.75s（证明真的重叠）。
  - 慢调用排在前面时，结果顺序仍按模型给的顺序（`slow` 先于 `fast`）。
  - 串行工具保持调用顺序；单元素并行批不起线程；verbose 模式保留流式输出与围栏；工具抛异常会传播出来。
- `python -m unittest discover -s tests`：1037 passed, 3 skipped（改动前 1027 + 新增 10）。

### 实测收益

两个各 0.4s 的 MCP 调用：串行 ≈0.8s → 并行 ≈0.4s。真实场景里一轮并发几个 MCP 搜索/读取时，收益按并发数线性放大。

### 状态

已完成并提交。

## 优化项 3：prompt cache 预热与 miss 统计

### 基线问题

GA 已经会给 Anthropic 打 `cache_control`、给 Responses 传 `prompt_cache_key`，但缓存效果只以一行 `[Cache] ...` 打印，既不累计也不判定。Pi 则把 prompt cache 当一等资产：`cache-stats.ts` 统计 miss 与 missedCost，`cache-warmer.ts` 在 TTL 的 90% 处主动预热。没有度量就无法判断提速收益来自哪一层。

### 改动内容

- 新增 `cache_stats.py`
  - `record_usage()` / `session_stats()` / `summary()` / `reset()`：按会话累计 requests、input、cached、cache_creation/read、output。
  - 分别处理三种上报口径：Responses 的 `input_tokens_details.cached_tokens`、Chat Completions 的 `prompt_tokens_details.cached_tokens`、Anthropic 独立上报的 `cache_creation_input_tokens` / `cache_read_input_tokens`。
  - `hit_rate`：Anthropic 的 `input_tokens` 不含缓存读写，所以完整 prompt 是 input + creation + read；OpenAI 兼容口径则直接用 cached / prompt。
  - `missed_tokens`：对齐 Pi 的 `NOISE_FLOOR_TOKENS = 1024`，噪声以下不计。
  - 预热策略对齐 `cache-warmer.ts`：`MAX_WARMING_AGE_MS`、`CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS`、`get_cache_warming_delay_ms()`（TTL 的 90%，至少留 10s）、`is_warming_worthwhile()`、`should_warm()`。
- `llmcore.py`
  - `_record_usage()` 改为调用 `cache_stats.record_usage()` 并打印 `format_trace()`，输出与原 `[Cache]` 行逐字兼容。
  - import 提到模块顶层；统计失败被吞掉，绝不影响主请求路径。

### 验证记录

- 新增 `tests/test_cache_stats.py`，17 个用例：三种上报口径的累计与命中率、Anthropic 的完整 prompt 口径、噪声地板、跨请求累计、多会话汇总、空 usage 与未知 api_mode、`[Cache]` 三行 trace 的逐字兼容、预热策略（90%、10s 余量、过短 TTL、节省门槛、过期条目）。
- `python -m unittest discover -s tests`：1054 passed, 3 skipped（改动前 1037 + 新增 17）。

### 实测收益

本项以可观测性为主，不直接改变延迟；它让后续任何提速改动都能用 hit_rate / missed_tokens 量化，并给出可复用的预热判定。

### 状态

已完成并提交。

## 优化项 4：HTTP 层连接复用

### 基线问题

原参考文档把这一项写成「自适应超时」，实际排查代码后发现更值得做的是**连接复用**：`llmcore.py:445` 用的是裸 `requests.post(...)`，而 requests 的模块级 `post()` 每次都会临时建 Session，请求结束即释放。也就是说 GA 每次调用 LLM 都要重新做一次 TCP + TLS 握手，重试线程同样如此。Pi 用 pooled undici dispatcher（`http-dispatcher.ts`）正是为了避免这件事。

### 改动内容

- `llmcore.py`
  - 新增 `_get_http_session(sess)`：为每个 backend 惰性创建一个 `requests.Session`，挂载 `HTTPAdapter(pool_connections=8, pool_maxsize=16)` 到 http/https。
  - `max_retries=0` 显式传给 adapter：重试策略由 `_stream_with_retry` 独占，避免适配器静默重复请求。
  - `_request_once()` 改为用池化 Session 发请求；`proxies` / `verify` 每次请求前同步到 Session，保持原有 per-request 语义。
  - 新增 `close_http_session(sess)`，供模型切换和测试释放连接。
- `agentmain.py`
  - `load_llm_sessions()` 重建 clients 前，关闭上一批 backend 的连接池，避免连接泄漏。
- 测试接缝变化：`tests/test_llm_cancel.py` 原先 patch `llmcore.requests.post`，现改为注入 `llmcore._get_http_session`（取消语义与被测对象不变）。

### 验证记录

- 新增 `tests/test_llm_http_reuse.py`，7 个用例：同一个 backend 三次请求只创建一个 Session、挂载两个池化 adapter、adapter 自身不重试、不同 backend 互相独立、`close_http_session` 正确释放并清空引用、无会话时安全、`proxies`/`verify` 每次请求生效。
- 更新 `tests/test_llm_cancel.py` 的 3 个取消用例以匹配新的注入点，行为断言不变。
- `python -m unittest discover -s tests`：1061 passed, 3 skipped（改动前 1054 + 新增 7）。

### 实测收益

单次请求省掉一次 TCP + TLS 握手。对本地直连端点收益有限，对走自建 relay / 长链路端点的场景更明显；连续多轮对话（每轮多次 LLM 调用）累积效果最大。

### 状态

已完成并提交。

## 后续：MCP 配置修正（exa 405 与 finance 移除）

优化项 1 里提到 `temp/mcp_tools_cache.json` 长期 `complete=false`，导致 60 秒 TTL 反复触发同步冷启动。定位后发现不是超时抖动，而是两个 server 配置错误。

### exa：`type: "sse"` 用错了 transport

- 现象：`HTTPStatusError: 405 Method Not Allowed for https://mcp.exa.ai/mcp`。
- 根因：GA 的 `mcp.json` 写的是 `"type": "sse"`，而 `mcp.exa.ai/mcp` 是 **streamable-HTTP** 端点。声明成 sse 后客户端走 GET/SSE 建流，端点不接受，直接 405。
- 参照 Codex 的 `~/.codex/config.toml`：`[mcp_servers.exa]` 下的 `# type = "sse"` 是**被注释掉的**，即只给 `url` 让客户端自行推断。
- 实测三种写法：

  | 配置 | 结果 |
  |---|---|
  | `{"type": "sse", "url": ...}`（原配置） | 0 工具，405 |
  | `{"url": ...}`（Codex 写法） | 2 工具（`web_search_exa`、`web_fetch_exa`） |
  | `{"type": "streamable-http", "url": ...}` | 2 工具 |

- 结论：**不需要登录验证**，也不需要 API key，只是 transport 声明错了。
- 修复：删除 `type` 字段，只保留 `url`。

### finance：移除

`http://106.14.205.176:3101/sse` 持续超时，已从 `mcp.json` 删除。修改前备份到 `temp/mcp.json.bak-<时间戳>`（`mcp.json` 已被 `.gitignore` 忽略，含真实密钥，不入库）。

### 顺带确认

- `fetch` 最初用 `uvx mcp-server-fetch`，偶发 `ImportError: cannot import name 'McpError'`——uvx 缓存里的 `mcp` 包与服务端版本不兼容（新版已改名 `MCPError`）。已换成 npx 版 `npx -y mcp-fetch-server`（zcaceres/fetch-mcp），不再依赖 uvx 缓存，工具从 1 个扩到 6 个（`fetch_html`/`fetch_markdown`/`fetch_txt`/`fetch_json`/`fetch_readable`/`fetch_youtube_transcript`）。
- `context7` 配置为 `https://mcp.context7.com/mcp`，同样**只写 url 不写 type**；无需 key 即可列出 2 个工具（`resolve-library-id`、`query-docs`），首次连接约 5-10s。

### 修复后实测

```
full discovery: 9.6s tools=18 complete=True
  exa                  connected  tools=2
  fetch                connected  tools=1
  memory               connected  tools=9
  sequential-thinking  connected  tools=1
  tavily               connected  tools=5
```

`mcp__exa__web_search_exa` 真实调用：6.2s，`status=success`，返回结构化搜索结果。

这也闭环了优化项 1 的背景：5 个 server 全部连上后缓存进入 `complete=True`，不再有 60 秒一次的强制冷启动。

## 总结

四项全部完成，累计：

- 提问路径的 MCP 阻塞从 16.02s 降到 ~0.016s（warm 后 ~0ms）；
- 同轮独立工具调用可并行，N 个 MCP 调用的等待从 N×latency 降到约 1×latency；
- prompt cache 有了可累计、可判定的度量，并带上可复用的预热策略；
- LLM HTTP 请求复用连接池，去掉每次请求的握手开销。

测试从 1025 增至 1061（新增 36 个），全绿。每一项都是独立提交，可单独回滚。
