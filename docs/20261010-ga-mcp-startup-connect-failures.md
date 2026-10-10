# GA MCP 启动连接失败：根因与修复（2026-10-10）

## 现象

启动 GA ink 时，各 MCP server 会尝试 connect，但经常有 server 显示失败（`/mcp` 面板中的 `✕`）。
用户观感是"很多 MCP 经常连不上，过一会儿再查又好了"。

## 结论（根因）

GA 给每个 MCP server 的连接/初始化预算是 **8 秒**，而远程 HTTP server（`context7`、`tavily`）的
TLS + `initialize` 握手实测在 **3-12 秒**之间抖动。超过 8 秒即被判为 `failed`，UI 渲染成 `✕`，
并且**没有任何重试**：该 server 的工具会从 LLM schema 里消失，直到进程重启或用户手动 reconnect。

这三点叠加，把"慢"直接等同于"坏"：

1. **超时太紧**：默认 8 秒，正好卡在远程 server 连接耗时的 p70-p90 上。
2. **失败即终态、无重试**：`_connect_and_fetch_tools` 抛异常就置 `failed`，之后所有发现路径都跳过它。
3. **stdio 串行**：本地 server 用 `Semaphore(1)` 串行连接，`npx -y <pkg>` 冷启动 2.6-14 秒，
   把整轮发现拖到 12 秒以上。

## 量化证据（本机实测）

背景发现（`start_background_discovery()` + 轮询 `mcp_status_snapshot()`，成功的一次）：

```
 2.2s  fetch/tavily/sequential-thinking/memory/exa/context7   pending
 6.9s  fetch      connected 6
 7.2s  tavily     connected 5
 9.0s  exa        connected 2
 9.6s  sequential-thinking connected 1
 9.6s  context7   connected 2
12.2s  memory     connected 9   -> loading=False
```

逐个连接时抓到失败（同一台机器，同一份 `mcp.json`）：

```
fetch 5.3s connected 6 | sequential-thinking 2.6s connected 1 | memory 3.5s connected 9 | exa 2.2s connected 2
tavily   8.3s FAILED  RuntimeError: Client failed to connect: Failed to initialize
context7 8.2s FAILED  RuntimeError: Client failed to connect: Failed to initialize
```

把超时放宽到 25s 后采样 6 次，远程 server 的连接耗时分布正好压在 8 秒线上：

| server | min | 中位 | max | >8s 次数 |
|---|---|---|---|---|
| tavily | 4.5s | 5.6s | 7.2s（另一次 8.3s） | 偶发 |
| context7 | 3.0s | 6.2s | 11.8s | 2/6 |
| exa | 2.0s | 2.2s | 2.9s | 0 |

即 `context7` 大约 1/3 的启动会失败并显示 `✕`。失败是随机的，所以表现为"经常失败"。

## 代码根因定位

| 位置 | 问题 |
|---|---|
| `mcp_runtime._default_timeout(..., "GA_MCP_DISCOVERY_TIMEOUT", fallback=8)` | 默认预算 8 秒，卡在远程握手耗时的分布中部 |
| `McpManager._connect_and_fetch_tools` | 异常即 `status="failed"`、清空 tools，没有重试 |
| `McpManager._connect_state` | 本地 server 走 `asyncio.Semaphore(1)`，串行连接 |
| `McpManager.discover` -> `ensure_all_connected(retry_failed=False)` | 后台发现跳过 `failed` server，失败者永不恢复 |
| `_background_discovery_worker` | 复检时同样 `retry_failed=False`，等于没有自愈 |
| `frontends/ink-ui/src/mcpPanel.ts` `mcpStatusIcon` | 只有 `connected/failed/pending/disabled`，没有"重试中"态 |
| `_open_state_client` / `_mcp_client` | `*.stderr.log` 以 append 打开且无上限（累计 1.3MB） |

补充澄清：`pending` 渲染为 `○`、只有 `failed` 才是 `✕`，所以看到 `✕` 就是真失败，
不是"还没连上"。此前怀疑的"`mcp_status` 与后台发现并发抢连接"不成立——`snapshot()` 与
`available_mcp_tools()` 都是纯只读快照，不会发起连接。

## Codex 对照

| 机制 | Codex | GA（修复前） |
|---|---|---|
| 启动超时 | `DEFAULT_STARTUP_TIMEOUT = 30s`（`codex-mcp/src/rmcp_client.rs:106`） | 8s |
| 工具调用超时 | `DEFAULT_TOOL_TIMEOUT = 300s` | 60s |
| HTTP 初始化重试 | 最多 3 次，退避 `[250ms, 1000ms]`，只重试超时/429/5xx/连接错误；stdio 不重试（`rmcp-client/src/streamable_http_retry.rs`） | 无 |
| required/optional 分层 | required 必须就绪否则 session init 失败；optional 只给 `1s` grace，超时就用缓存目录并略过（`codex-mcp/src/mcp/mod.rs:197`、`connection_manager/tool_catalog.rs:296`） | 无分层 |
| 工具目录缓存 | TTL 30 分钟、LRU-32、按 server 身份做 key，命中可跳过等待（`codex-mcp/src/tool_catalog_cache.rs`） | 完整结果无 TTL，部分结果 60s |
| 启动事件 | `Starting/Ready/Failed{reason}/Cancelled` + summary；失败会 `reconnect_failed_startup()` | 只有 pending/failed |
| 并发 | `JoinSet` 全并发 | 本地 server `Semaphore(1)` 串行 |

核心差异：Codex 把"慢"和"坏"分开——慢就多等一会儿（30s）+ 退避重试，坏才报失败；
单个 optional server 失败不影响会话，也不会让它的工具永久消失。GA 把"8 秒没连上"直接当成"失败"。

## 修复方案（本次实施）

1. **默认启动预算 8s -> 30s**（对齐 Codex `DEFAULT_STARTUP_TIMEOUT`），保留 per-server
   `startup_timeout_sec` 覆盖与 `GA_MCP_DISCOVERY_TIMEOUT` 环境变量。
2. **远程 HTTP/SSE server 有界初始化重试**：最多 3 次、退避 `[250ms, 1.0s]`，整个重试受
   同一个启动预算约束；只对可重试错误重试（超时/连接/5xx/429/"Failed to initialize"），
   认证类错误（401/403/unauthorized...）不重试。stdio 不重试（对齐 Codex）。
3. **新增 `connecting` 状态**：`pending` = 从未尝试；`connecting` = 尝试/重试进行中；
   `failed` = 重试用尽。UI 用 `◌` 表示 `connecting`，`✕` 只在真正放弃后出现。
4. **失败自愈**：后台发现 worker 改为 `retry_failed=True`（它本就是自愈通道），
   同步的 `discover_mcp_tools` / `discover_mcp_tools_cached` 保持 `retry_failed=False`
   （不锤死一个已失败 server）。Ink 的启动 watch 在首轮结束后，若仍有 `failed` server，
   追加**一轮**有界自愈 pass。
5. **本地 server 并发连接**：`Semaphore(1)` -> 有界并发池（默认 4，`GA_MCP_LOCAL_CONNECT_CONCURRENCY`），
   对齐 Codex 的 `JoinSet` 并发启动。
6. **stderr 日志上限**：`temp/mcp_logs/*.stderr.log` 超过 256KiB（`GA_MCP_STDERR_LOG_MAX_BYTES`）时轮转，
   不再无限增长。

## 验证

- `tests/test_mcp_runtime.py`：新增默认超时、HTTP 重试次数、stdio 不重试、`connecting` 状态、
  stderr 轮转的回归用例；既有用例保持通过。
- `frontends/ink-ui/src/mcpPanel.test.ts`：`connecting -> ◌`。
- 真实链路：`frontends/ink_bridge.py` 发 `mcp_watch_start`，观察 6 个 server 的发现时间线不再出现
  随机 `✕`。
