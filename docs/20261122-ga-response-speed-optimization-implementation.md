# GA 响应速度优化实施记录

## 本轮结果

Ink 收到首个非空增量后立即派发，后续增量共用一个 24ms 调度窗口。处理 thinking、重试、审批、状态转换和结束事件时，候选调度器先交付已收到的文本。`assistant_done` 仍使用完整输出做对账。活动转录复用已完成前缀的格式化和折行结果，选区仍可访问完整历史。

| 本地测量 | 旧路径 p50 / p90 | 候选 p50 / p90 |
|---|---:|---:|
| Ink 增量抵达 → App 派发，10 次 | 92.36 / 95.48 ms | 0.03 / 0.05 ms |
| Ink 增量抵达 → stdout 首帧，10 次 | 98.12 / 101.33 ms | 4.48 / 10.80 ms |
| 1,000 条历史、512 个短片段突发，首片段 → Python bridge `done`，5 次 | 29.80 / 34.80 ms | 4.00 / 6.72 ms |

首帧数据来自真实 App 的内存终端，使用 `debug: true` 去掉 Ink 自身的绘制节流。突发探针走真实 Python 队列和 JSONL 编码，双方均发出 32 条增量。旧路径对每条增量估算一次上下文，候选每 50ms 至多估算一次，并在非纯增量事件上刷新。

Python 仍保留 16 字符合并。实验性逐块转发曾在一次真实长回答中省掉约 152ms 的 Python 等待，使同类回答从 86–94 条 JSONL 增量涨到 304 条。评审发现现有脱敏按事件处理，逐块转发会增加敏感内容被拆开漏检的机会，因此撤回该实验。消除这层缓冲需要先有可跨片段工作的脱敏边界和回归测试。

## DeepSeek 4.1 Flash 真实对照

使用项目根目录 `llm.yaml` 的 `deep` profile（`ecloud`、`moma/deepseek-v4.1-flash`、Chat Completions、`high`）。两条固定中文任务，分别交错运行 3 对，共 12 次调用。探针使用真实 provider 和 Python bridge 的内存 JSONL 输出，无工具挂载。下节记录独立的 ConPTY 全链路对照。

| 任务 / 指标 | 旧路径 p50 / p90 | 候选 p50 / p90 |
|---|---:|---:|
| 月相，HTTP 请求 → 首个已解析文本 | 488.84 / 533.02 ms | 483.19 / 498.38 ms |
| 月相，首文本 → bridge 增量 | 0.347 / 0.549 ms | 0.318 / 0.373 ms |
| 月相，提交 → bridge `done` | 494.08 / 814.81 ms | 486.44 / 502.65 ms |
| 二分查找，HTTP 请求 → 首个已解析文本 | 436.12 / 532.83 ms | 431.88 / 572.42 ms |
| 二分查找，首文本 → bridge 增量 | 0.220 / 0.532 ms | 0.210 / 0.451 ms |
| 二分查找，提交 → bridge `done` | 632.73 / 693.98 ms | 631.98 / 763.99 ms |

12 次均通过末尾标记、增量拼接和完整文本校验。月相每次报告 178 个输出 token、2 条 bridge 增量，二分查找每次报告 1,306 个输出 token、86 条 bridge 增量。两组均报告 0 个缓存读取 token。每档只有 3 个样本，p90 就是最大值。请求到首正文及整轮时间的变化落在本次样本波动内。质量校验覆盖这两条任务的输出完整性，答案内容评分、工具任务和长历史追问未测。

## Windows ConPTY 全链路对照

通过 ConPTY 启动实际 `ga ink`，从输入框提交任务，通过 pyte 重放屏幕。保留 Ink 正常的绘制节流和光标停放。只切换 `GA_STREAM_SCHEDULER`，活动转录、schema 和 history 均使用默认路径。探针使用精简的系统提示、独立工作目录和文本任务，禁用 hooks、MCP 及模型工具调用。真实组使用同一 `deep` profile，并在请求前恢复配置的 `high`，因为 `/model` 会继承上一个模型支持的推理档位。

| 指标，p50 / 最大值 | 旧路径 | 候选 |
|---|---:|---:|
| 离线，回车 → 首段正文 | 339.8 / 422.9 ms | 316.7 / 337.3 ms |
| 离线，首段已解析正文 → 屏幕 | 147.9 / 240.3 ms | 134.0 / 146.3 ms |
| 离线，每轮最长正文更新间隔 | 113.5 / 131.6 ms | 116.0 / 116.6 ms |
| 离线，回车 → 屏幕末尾标记 | 557.1 / 638.4 ms | 556.1 / 654.8 ms |
| DeepSeek `high`，回车 → 首段正文 | 672.4 / 29,273.4 ms | 577.8 / 578.4 ms |
| DeepSeek `high`，HTTP 请求 → 首段已解析正文 | 447.4 / 29,091.1 ms | 415.1 / 415.8 ms |
| DeepSeek `high`，首段已解析正文 → 屏幕 | 134.6 / 181.6 ms | 119.8 / 121.8 ms |
| DeepSeek `high`，回车 → 屏幕末尾标记 | 672.4 / 29,618.3 ms | 578.4 / 675.2 ms |

离线 10/10、真实 6/6 通过。每轮检查一次请求、六段正文可见、末尾标记、最终文本和 bridge idle。首帧指标按屏幕出现首段正文计时，跳过运行横幅和 thinking。真实组旧路径有一次 HTTP 首正文等待 29.1 秒，保留在样本中。真实组仅两个样本能从屏幕看到多次正文更新，每档各一个，无法据此比较持续输出间隔。离线同输入下，候选首段正文较早出现，最长更新间隔的中位数仍约 116 ms。内存终端约 94 ms 的首帧差异在正常绘制链路里缩小了。

复查离线时间线，前六段正文在两组中均约每 45–47 ms 到达 bridge。追加 `--trace-writes` 测量 Node 写入时间，10/10 通过正文及事件顺序校验。每档跑 5 次，按同一段编号匹配 stdout 与屏幕出现时间。

| 离线追加测量，p50 | 旧路径 | 候选 |
|---|---:|---:|
| 首条正文 bridge 增量 → stdout 写入 | 108.5 ms | 53.8 ms |
| 每轮最长正文章节写入间隔 | 125.6 ms | 71.5 ms |
| 每轮最长正文章节屏幕更新间隔 | 215.5 ms | 115.0 ms |
| 首段已解析正文 → 屏幕 | 134.5 ms | 136.9 ms |

这轮 stdout 写入更早、间隔更短，首段在屏幕上的中位数则接近。首写到回读的中位数为 23.8 → 81.2 ms。两档的回读时间波动都很大，单独的首写延迟会受终端输出与探针回读合并影响。候选的屏幕最长更新间隔中位数在两轮离线对照里均约 115–116 ms，旧路径分别约 113 和 215 ms。当前结果支持候选减少 GA 侧等待，屏幕端持续输出收益仍需更长的真实文本流和真实终端采样。追加数据位于 `tests/probes/captures/ink-conpty-latency-km6v18m_/`。

原始指标位于 `tests/probes/captures/ink-conpty-latency-eaj6titx/`（离线）和 `tests/probes/captures/ink-conpty-latency-jyxtife3/`（真实 `high`）。另有一轮 `medium` 探索样本，未纳入上表。ConPTY 测的是终端输出可见性，Windows Terminal 的实际合成、IME 和手动滚动体验仍需真机验收。

## 交付范围和开关

- `GA_STREAM_SCHEDULER=frame` 启用 Ink 低延迟调度和 bridge 上下文估算采样。默认 `legacy`。Python 回合横幅也占用首个立即派发位置，紧接横幅的正文最多仍需等待一个调度窗口。当前 Python 的 thinking/retry 通知与正文来自不同路径，UI 只能排序已收到的事件，跨线程全序仍待验证。
- `GA_LIVE_TRANSCRIPT_RENDER=active_tail` 复用静态前缀的格式化和折行。默认 `legacy`。全量选区仍要拼接内容帧，成本随历史长度增长。
- `GA_SCHEMA_CACHE=on` 只缓存静态 schema 资产，动态角色、MCP 和权限/capability 过滤继续按原路径执行。默认 `off`。当前深拷贝版本在单项微基准慢于直接读取和解析，暂不推广。工具 JSON 缓存和动态快照仍待单独测量。文件缓存通过文件元数据判断版本，保留时间和大小的原地修改可能需要重启。
- `GA_HISTORY_PROJECTION=incremental` 优化 native 纯文本历史快照。工具、图片或 thinking 等复杂结构使用既有深拷贝。默认 `full`，真实长历史请求收益未测。
- `GA_LATENCY_TRACE=1` 在 stderr 输出工具批次构造、执行和打包耗时。执行段包括生成显示文本的时间，终端实际绘制由 Ink 探针单测。工具并行分类沿用现有规则。

## 回归与复测

默认路径及候选路径各通过 908 个 Ink 测试，TypeScript typecheck 通过。Python 全量 2,852 个用例，8 个跳过，3 个 Windows 长路径诊断失败。三者在旧路径基线也失败，均因测试预期 `OSError` 而系统实际允许写入。独立回跑自动压缩转录顺序用例通过，该用例在一次较早的并行测试运行中未收到 `history_replace`。

```bash
python -m tests.probes.python.stream_latency_baseline --burst --mode legacy
python -m tests.probes.python.stream_latency_baseline --burst --mode frame
python -m tests.probes.python.zhanlu_ds_latency_ab --rounds 3 --real
python -m tests.probes.python.ink_conpty_latency --rounds 5
python -m tests.probes.python.ink_conpty_latency --trace-writes --rounds 5
python -m tests.probes.python.ink_conpty_latency --rounds 3 --real
(cd frontends/ink/ui && npx tsx probes/stream_latency_timeline.ts legacy 10)
(cd frontends/ink/ui && GA_LIVE_TRANSCRIPT_RENDER=active_tail npx tsx probes/stream_latency_timeline.ts frame 10)
```

本机脱敏样本保存在 `.superpowers/sdd/2026-11-22-ga-response-speed-optimization/zhanlu-final.jsonl`。`zhanlu-ab.jsonl` 属于已撤回的逐块转发实验。

## 验收决定

保留候选开关供试用，默认继续使用旧路径。当前证据支持减少局部调度等待和长历史突发时的 bridge 开销。下一轮流畅度验收需要更长的真实正文，并在同一条时间线上加入 App 派发标记、真实 Windows Terminal 的屏幕采样。现有证据已经把约 115 ms 的候选屏幕空档缩小到显示与回读链路。逐块转发要先通过跨片段脱敏和 thinking/retry 顺序验证。静态前缀复用还需隔离选区线性成本，schema 与 history 仍缺真实长会话验证，工具批次仍缺独立的显示耗时和真实工具任务验收。
