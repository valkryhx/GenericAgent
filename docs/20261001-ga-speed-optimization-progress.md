# GA 响应速度优化进度（参照 Pi agent）

- 日期：2026-10-01（Asia/Shanghai）
- 目标：按 `docs/20261001-pi-agent-speed-reference.md` 列出的候选顺序，逐项优化 GA 的启动延迟与响应速度，并逐项记录证据。
- 约束：每项都先写回归测试、再改实现、跑完整 `tests/` 套件，最后单独提交，便于回滚。

## 优化项总览

| # | 项目 | 状态 | 备注 |
|---|---|---|---|
| 1 | MCP 发现移出提问关键路径 + 缓存策略修正 | 已完成 | 提问路径 ~0ms，冷启动发现转入后台 |
| 2 | 同轮工具并行执行（对齐 Pi 的 `executeToolCallsParallel`） | 未开始 | |
| 3 | prompt cache 预热与 miss 统计 | 未开始 | |
| 4 | HTTP 层自适应超时 | 未开始 | 仅当 GA 使用多线路由时才需要 |

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

待开始。

## 优化项 3：prompt cache 预热与 miss 统计

待开始。

## 优化项 4：HTTP 层自适应超时

待开始。
