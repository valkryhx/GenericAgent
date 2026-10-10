# GA ink MCP 冷启动耗时：是 MCP 慢，还是 GA 在等？（2026-10-10）

## 用户疑问

> 处理 MCP 冷启动 37-82s 的问题，因为每次启动 GA ink 都会等 mcp 就绪。
> 看看 codex 是不是也这样，还是说 mcp 启动时间就真的是这么长。

## 结论（先给答案）

1. **MCP 冷启动确实慢，慢在远端端点，不在 GA。** 本机 6 个 server 逐个连接耗时之和
   在 50s 量级（`context7` 单个 9.6-26.7s 是主要拖累），并行后墙钟 9.6-14.8s。
   观测到的 37-82s 来自远端握手抖动 + 单 server 最长 30s 预算，不是 GA 的固定开销。
2. **GA 不阻塞首轮提问。** 实测：bridge `ready` 后立刻 submit，**首 token 在 0.16-0.19s 后**
   就出来了，而 MCP 还要 7-12s 才 `loading=false`。首轮对 MCP 的等待上限是
   `GA_MCP_DISCOVERY_BUDGET`（默认 2s）。
3. **Codex 也不等全部 MCP 就绪。** 它只等 `required = true` 的 server，且当该 server
   `allows_cached_startup()` 且有缓存 catalog 时直接跳过等待；其余后台连接，TUI 逐 server
   显示 `Starting/Ready/Failed`，超时 30s（`DEFAULT_STARTUP_TIMEOUT`），每 server 可用
   `startup_timeout_sec` 覆盖。

所以真正的问题是**观感**：GA ink 从 `ready` 起就在输入框下方用警告色显示
`MCP initializing · N/M connected`，且这行会一直挂到最后一个 server 连上（可能 30s+），
读起来像"GA 还在启动、先别输入"。本轮把它的表述改成后台进行 + 可直接输入。

## 证据 1：首轮不等 MCP（决定性）

探针 `frontends/ink-ui/scripts/_probe_mcp_firstturn.ts`：`ready` 后立即 submit，不等 MCP。
本机两次独立运行：

```
run A  readyMs 2079 | submit 2079 | firstDelta 2239 | answerDone 6016 | mcpReady 尚未就绪
run B  readyMs 2005 | submit 2005 | firstDelta 2193 | answerDone 17135 | mcpReady 14306
```

run A：答案 6.0s 交付时 MCP 还没就绪（晚了 3.6s 之后才 ready）。
run B：首 token 比 MCP 就绪早 12.1s。

对应代码：`mcp_runtime.discover_mcp_tools_cached_fast()` —— 有缓存（含过期缓存）立即返回并后台刷新；
无缓存时最多等 `GA_MCP_DISCOVERY_BUDGET`（默认 2s）就先把已连上的工具交出去。

## 证据 2：逐个 server 的耗时（同一份 mcp.json，本机）

### 串行 `reconnect_mcp_server()` 单个计时（2026-10-10）

| server | transport | 单次连接 |
|---|---|---|
| context7 | http | **26.73s** |
| tavily | http | 12.62s |
| fetch | stdio (npx) | 4.35s |
| exa | http | 2.85s |
| sequential-thinking | stdio (npx) | 1.82s |
| memory | stdio (npx) | 1.72s |

串行之和 ≈ **50.1s**。这就是"如果 GA 串行等 MCP"会看到的量级，也是 37-82s 的下限来源。

### 并行冷启动墙钟（探针 `_probe_mcp_startup.ts`，两次运行）

```
run A  ready 1984ms  mcpReady  9607ms  tools 25
       fetch 6166 | sequential-thinking 6166 | memory 6166 | exa 7492 | tavily 8015 | context7 9607
run B  ready 2093ms  mcpReady 14757ms  tools 25
       sequential-thinking 5718 | memory 6239 | fetch 6509 | exa 8375 | tavily 9960 | context7 14757
```

结论：**GA 已经是并行连接**（本地 stdio 走 `_MCP_LOCAL_CONNECT_CONCURRENCY_DEFAULT = 4` 的池，
远端 transport 不限流），墙钟 ≈ 最慢的那个 server，而不是总和。
`context7` 两次都是最后一个就绪者，两次把墙钟从 ~9.6s 拉到 ~14.8s —— 37s+ 的情况就是它撞上
远端抖动、甚至吃掉整个 30s 预算。

### 为什么上限是 30s 而不是 8s

`_MCP_DISCOVERY_TIMEOUT_DEFAULT = 30.0`，对齐 Codex 的 `DEFAULT_STARTUP_TIMEOUT = 30s`。
远端 server 还有最多 3 次 initialize 重试（`_MCP_REMOTE_CONNECT_ATTEMPTS = 3`，退避 0.25s/1.0s），
重试被同一个 deadline 包住，所以单个 server 最坏消耗 ≈ 30s。
（历史包袱：默认 8s 正好卡在远端握手分布的中间，"慢"被误判成"坏"，那是另一篇文档的主题。）

## 证据 3：Codex 的做法（`D:/git_codes/codex`）

| 关注点 | Codex | GA 现状 |
|---|---|---|
| 启动时等谁 | 只等 `required = true` 的 server（`codex-mcp/src/connection_manager/required.rs::validate_required_servers`） | 谁都不等；首轮最多 2s 预算 |
| 有缓存时 | `allows_cached_startup() && cached_startup_tools().is_some()` → **直接跳过等待** | `discover_mcp_tools_cached_fast()` 缓存命中立即返回 |
| 超时 | `DEFAULT_STARTUP_TIMEOUT = 30s`，可 `startup_timeout_sec` 覆盖 | 同样 30s，`GA_MCP_DISCOVERY_TIMEOUT` 可覆盖 |
| 其余 server | 后台连，逐个推 `McpStartupUpdate`（Starting/Ready/Failed/Cancelled） | 后台连，`mcp_progress` 逐个推 server 状态 |
| UI | `tui/src/chatwidget/mcp_startup.rs`：`Starting MCP servers` 头部 + **queued-input release points**（提示可以直接输入） | 输入框本来就可用，但文案没说明，见下节 |

即：两边语义一致（能力 ≠ 阻塞），差异只在**文案是否告诉用户"可以继续"**。

## 本轮改动

### 1. npx stdio server 跳过 registry 往返（`mcp_runtime.py`）

`npx` 即使包已在缓存里也会每次去 registry 解析。实测（上一轮，同一台机器）：

```
fetch             npx -y 7.97s / 3.75s   ->  npx -y --prefer-offline 2.44s / 2.44s
sequential-thinking 4.62s / 3.69s        ->  1.77s / 1.75s
memory            2.53s / 3.52s          ->  1.76s / 1.76s
```

本轮复测与之吻合：`memory 1.72s`、`sequential-thinking 1.82s`。

实现：`_prefer_offline_args()` 只对 `npx/npx.cmd/npx.exe/npx.ps1` 注入 `--prefer-offline`，
已有 `--offline`/`--prefer-offline` 则不重复注入，`uvx` 等不动；`_normalize_server_config()` 里统一套用。
**放在宿主代码而不是 mcp.json**：`mcp.json` 是用户配置且被 `.gitignore` 忽略（含真实 key），
改配置不能随仓库分发。`--prefer-offline` 在包真的缺失时仍会安装，所以新机器不会因此装不上。

### 2. 自愈只重连失败的 server（`frontends/ink_bridge.py`）

`_watch_mcp_status()` 原来发现 failed 时调 `start_background_discovery(retry_failed=True)` —— 全量重跑，
等于因为一个 flaky server 把整轮的 `initializing` 窗口翻倍。改成对 failed 列表逐个 `reconnect_mcp_server(name)`。

### 3. 启动状态文案不再像门禁（`frontends/ink-ui/src/mcpPanel.ts`）

```
- MCP initializing · N/M connected · X tools
+ MCP connecting in the background · N/M connected · X tools · you can type now
```

输入框从来没有被 MCP 状态禁用（`App.tsx` 里没有任何基于 `mcpStartupStatus` 的 disabled），
所以这只是把已经成立的事实说出来。per-server 行（`◌ context7 · connecting`）保持不变，
用户依然能看到"是哪一个 server 慢"。

## 不变量

- **首轮提问不得阻塞在 MCP 就绪上。** 上限是 `GA_MCP_DISCOVERY_BUDGET`（默认 2s）；
  缓存命中时立即返回并后台刷新。MCP 冷启动是后台观测，UI 逐 server 显示状态即可。
- **"慢"不得被判成"坏"。** 单 server 预算 30s（对齐 Codex），远端有界重试；
  失败者由定向自愈重连，而不是让整轮发现重跑。
- **不要在启动期做内联预热。** 试过：`main()` 里同步预热把 `ready` 从 1.9s 推到 3.7s；
  改成后台线程仍因 GIL 争用推到 3.65s。已回退，`ready` 保持 ~2.0s。

## 复现

```bash
node frontends/ink-ui/node_modules/tsx/dist/cli.mjs frontends/ink-ui/scripts/_probe_mcp_startup.ts
node frontends/ink-ui/node_modules/tsx/dist/cli.mjs frontends/ink-ui/scripts/_probe_mcp_firstturn.ts
python -m unittest tests.test_mcp_runtime tests.test_ink_bridge
cd frontends/ink-ui && npx tsx --test src/mcpPanel.test.ts src/App.test.ts
```

回归测试：`tests/test_mcp_runtime.py::test_npx_stdio_servers_are_spawned_with_prefer_offline`、
`tests/test_ink_bridge.py::test_mcp_watch_self_heal_reconnects_only_the_failed_server`、
`frontends/ink-ui/src/mcpPanel.test.ts`（启动行文案）、`frontends/ink-ui/src/App.test.ts`（启动状态渲染位置）。

## 未做 / 可选后续

- `context7` 是唯一的量级拖累（9.6-26.7s）。这是用户配置决策，本轮没有替用户禁用；
  要立刻见效，可以在 `mcp.json` 里给它加 `"disabled": true`，或让它只在 `/mcp` 手动启用。
- 没有把"某 server 连接超过 N 秒"单独标成 `slow` 状态：需要把计时穿进 manager state，
  收益只是换个词，per-server 行已经能看出是谁慢，暂不做。
- 相关文档：`docs/20261010-ga-mcp-startup-connect-failures.md`（8s 超时把慢误判成坏、失败即终态、stdio 串行）。
