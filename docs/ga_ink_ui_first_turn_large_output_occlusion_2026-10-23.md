# GA Ink UI：首轮大输出（code_run）上半部分被遮挡 / 覆盖 —— 分析报告

- 日期：2026-10-23
- 范围：`frontends/ink/ui`（Ink 前端）+ `genericagent/core/handler.py`（工具结果文本）
- 约束：**本报告只做定位分析，未修改任何源码**
- 结论置信度标注：`[已证实]` = 有代码或实测证据；`[高度可疑]` = 证据强但未做终局实验；`[待验证]` = 推测

---


> **状态（2026-10-23 晚）：本报告是当时的第一阶段定位稿，已经过期。源码修复与最新验证见 [`20261023-GA-ink-code_run结果通道与CC303对照修复.md`](./20261023-GA-ink-code_run结果通道与CC303对照修复.md)。**

## 0. TL;DR

数据**没有丢**。首轮大输出最终会完整进入终端 scrollback（这也解释了"第二轮之后往上拉能看到完整的第一轮内容"）。
出问题的是**首轮进行中/结束瞬间的屏幕绘制**：大输出被拆成「已经写进 scrollback 的 Static 段」+「一个最高只有 12 行的 live band」两部分，而 live band 每帧只用**相对光标移动**擦除上一帧。

三个环节叠加导致"上半部分看起来被盖住/被截断"：

1. **live band 硬上限 12 行**（`DEFAULT_MAX_LIVE_ROWS`），大输出的当前可见部分永远只显示尾部 → 视觉上"上面被切掉"。`[已证实]`
2. **streaming commit 是 fence-safe 的，可能整段拒绝提交**，此时 live 文本无限增长，但视口仍然只画最后 12 行，中间的差异完全不可见。`[已证实]`（机制）／`[高度可疑]`（是本次现象的主因）
3. **ink 的 log-update 用 `eraseLines(previousLineCount)` 相对擦除**，而 GA 在它之上又叠了 `<Static>` 增量打印和 `cursorPark` 的上下移动；三者交替时擦除基准与实际屏幕行数容易失配，失配就会擦到属于 Static/历史的上方行 → "被覆盖"。`[已证实]`（机制）／`[高度可疑]`（是本次现象的放大因素）

另有一处**不是 bug**：图 1 里的 `[omitted long output]` 是后端工具结果截断（`smart_format(max_str_len=600)`），不是 UI 渲染问题。

---

## 1. 现象与截图判读

用户描述：输入问题后，**第一轮**回复若是大块 `code_run` 输出，UI 里"上面部分似乎被覆盖/挡住"；**第二轮之后往上滚**能看到完整的第一轮内容。

三张截图（`bug/20261023_*.png`）实测判读：

| 图 | 关键观察 | 判读 |
|---|---|---|
| 图 1（1115×686） | 输出列表中间夹 `[omitted long output]`，前后都有内容 | **后端截断**，见 §4.4，非渲染 bug |
| 图 2（1287×437） | JSON script 块折行、`Exit Code: 0` 之后 4 行 git log，完整 | 正常 |
| 图 3（991×395） | 直接从 `&& message.done` 开始，**开头的 `import type ...` 十几行、以及 `Exit Code: 0` 都不见了** | 典型的"live 视口只画尾部" |

图 3 是核心证据：缺的是**卡片前部 + 中部的状态行**，不是尾部。尾部（`if (commits.length === 0) return ''` 等）完好 —— 这正是"只保留最后 N 行"的特征，而不是"数据丢失"。

---

## 2. UI 架构（默认 inline 模式）

默认 `GA_INK_MOUSE` 未开启 → `mouseMode === 'inline'`，渲染分三层：

```
<Static items={staticTranscriptRows}>      ← 已完成的轮次，永久写入终端 scrollback
<Box>                                       ← 主帧（每帧重绘，相对擦除）
  <MessageViewport height={live}>           ← live band：当前未完成轮次，最高 12 行
  <BottomChrome>                            ← 输入框 + 提示条
</Box>
```

数据流：

```
bridge event (assistant_delta)
  → state.messages 里 a-{taskId} 累积
  → commitStreamingAssistantMessages(): 溢出行切成 done 段 a-{taskId}-c{n}
  → messagePartition: done → staticMessages / 未完成 → activeMessages
  → Static 打印 done 段；live band 只画 activeMessages 的最后 N 行
```

---

## 3. 逐环节证据

### 3.1 live band 高度硬上限 12 行 —— `[已证实]`

`src/messageViewportPlan.ts:7`
```ts
export const DEFAULT_MAX_LIVE_ROWS = 12
```
`src/messageViewportPlan.ts:29-34`
```ts
const maxLive = Math.max(1, Math.floor(input.maxLiveRows ?? DEFAULT_MAX_LIVE_ROWS))
const liveCount = Math.max(0, Math.floor(input.liveLineCount))
if (liveCount > 0) {
  const height = Math.min(liveCount, maxLive, room)
  return { kind: 'live', height }
}
```
调用点 `src/App.tsx:1648-1658`：`liveLineCount = activeTranscriptRows.length`，`inlineMessageRows = plan.height`。

**含义**：不管当前未完成轮次有多少行，live band 最多占 12 行。超出部分只有在被 commit 成 Static 之后才会出现在 scrollback 里。

### 3.2 live 视口只保留尾部 —— `[已证实]`

`src/App.tsx:1659-1663`
```ts
const liveTranscriptRows = useMemo(() => (
  inlineMessageViewportPlan.kind === 'live'
    ? liveTranscriptViewportLines(activeTranscriptRows, inlineMessageViewportPlan.height)
    : []
), [activeTranscriptRows, inlineMessageViewportPlan])
```
`src/messageWindow.ts:128` `liveTranscriptViewportLines(lines, maxRows)`：当 `lines.length > max` 时，只保留最后 `max` 行，并额外把**最后一个 user prompt 块**钉住（pin）在开头。

**这正好解释图 3**：pin 住的是用户问题行，之后直接跳到尾部 12 行 —— 中间那段（含 `Exit Code: 0`）不渲染。

### 3.3 streaming commit 可能整段放弃 —— `[已证实]（机制）`

`src/streamCommit.ts:4`
```ts
export const DEFAULT_STREAM_LIVE_TAIL_LINES = 8
```
`commitStreamingAssistantMessages()`：
```ts
const overflowCount = findFenceSafeOverflowCount(lines, maxTail)
if (overflowCount <= 0) return messages        // ← 整段不提交
```
`findFenceSafeOverflowCount()` 的规则：切点必须让 head 处于"围栏外"（fence balanced）；找不到就 `return 0`，**这一次 delta 完全不提交**。

`code_run` 的 assistant 文本形态通常是：

    ● code_run
    ```json
    { "script": "..." }        ← 很长，几十行
    ```
    Running python in GenericAgent: ...
    Exit Code: 0
    <大段 stdout>

只要 ` ```json ` 这段围栏还开着，切点就落在围栏内 → `findFenceSafeOverflowCount` 一路回退到 0 → **整块 args 期间一行都不提交**。注释里也承认过这个历史症状（`streamCommit.ts` 中 2026-10-03 的注释："live text grew to ~59 lines while the live viewport only paints the last ~12"）。

**结果**：live 文本涨到几十行，live band 仍只画 12 行 → 用户看到"上半部分被挡住"。

### 3.4 擦除是相对行数，且与 Static / cursorPark 交替 —— `[已证实]（机制）`

`node_modules/ink/build/log-update.js:17`
```js
stream.write(ansiEscapes.eraseLines(previousLineCount) + output);
previousLineCount = output.split('\n').length;
```
`node_modules/ink/build/ink.js:127-133`
```js
if (hasStaticOutput) {
    this.log.clear();                                   // 擦掉上一主帧
    this.options.stdout.write(staticOutput);            // 追加到 scrollback（可能让终端滚动）
    this.log(output);                                   // 重画主帧
}
if (!hasStaticOutput && output !== this.lastOutput) {
    this.throttledLog(output);
}
```
`src/stdoutCursorPark.ts:69-73 / 100-106 / 108-127`：每次 write 先 `unpark()`（`\x1b[{parkedUp}B\r` 相对下移回帧底），写完后在 microtask 里 `park`（`\x1b[{up}A\x1b[{col}G` 相对上移到 caret）。

`src/App.tsx:1715-1727`：`up = Math.max(0, liveViewportRows - inputCursor.row)`，其中 `liveViewportRows = headerRows + messageRowsForCursor + bottomRows`（`App.tsx:1700`）。

**风险点**：
- 光标在最后一行时，`\x1b[NB`（下移）**不会触发滚动**，是无效移动 → 下一次 `eraseLines(N)` 从错误的绝对行开始擦 → 擦到上方 Static 的内容。这是"上半部分被覆盖"最经典的成因。
- Static 追加 K 行会让终端整体上滚 K 行，此时如果 park 正处于"光标停在帧中部"的状态，几何就偏了。
- 三者（log-update 的 relative erase、Static 的增量打印、park 的 relative move）共享同一个光标，没有统一的绝对坐标锚点。

### 3.5 `[omitted long output]` 来自后端 —— `[已证实]`，非 UI bug

`genericagent/core/handler.py:145`
```python
output_snippet = smart_format(stdout_str, max_str_len=600, omit_str='\n\n[omitted long output]\n\n')
```
`smart_format()` 保留首 300 字符 + 尾 300 字符，中间用标记替换。图 1 就是它。**这是设计行为**，不是渲染 bug（但对"大输出"体验不友好，见 §7）。

---

## 4. 已排除项

| 假设 | 结论 | 依据 |
|---|---|---|
| ink 走了 `clearTerminal` 分支导致清屏丢内容 | **排除** | `ink.js:121` 条件是 `outputHeight >= stdout.rows`；探针实测 `clear2=0, clear3=0`（未出现 `\x1b[2J`/`\x1b[3J`） |
| 数据在 state 里丢了 | **排除** | 120 行 payload 首轮结束后 `missing: 0, duplicated: 0`，scrollback 142 行完整 |
| Static 重挂导致重复打印 | **排除（常规路径）** | `Static key = staticGeneration + ':' + staticTranscriptGeneration`（`App.tsx:1823`），两者只在 rewind / 历史替换时递增（`state.ts:223/230`） |
| `[omitted long output]` 是 UI 折叠 | **排除** | 来自 `handler.py:145`；UI 侧的 `foldToolOutput` 用的是 `... (+N more lines)` 文案 |

---

## 5. 复现与实测

探针脚本（本次新建，未改源码）：

- `frontends/ink/ui/scripts/_probe_firstturn_clobber.ts` —— 真实 `<App>` + 虚拟终端，统计残留/重复
- `frontends/ink/ui/scripts/_probe_frame_geometry.ts` —— 逐帧解析 `erase=N` 与帧高
- `frontends/ink/ui/scripts/_probe_overwrite.ts` —— 逐行核对 payload 缺失/重复
- `frontends/ink/ui/scripts/_probe_dump.ts` —— dump 每次 stdout.write 的原始转义序列

运行方式（`npx` 在本机不可用，直接调 tsx CLI）：

```
cd frontends/ink/ui
node node_modules/tsx/dist/cli.mjs scripts/_probe_overwrite.ts
```

### 5.1 完整性实测（24 行 × 100 列，首轮 120 行输出）

```
TOTAL visual rows: 142
missing: 0
duplicated: 0
ExitCode line present: true
omitted marker: false
```

→ **数据链路完整**，问题在视口/重绘。

### 5.2 帧擦除实测

16 帧中 12 帧出现 `erase` 与前一帧实际行数不一致，且存在连续 `H=0`（纯擦除、不写内容）的帧：

```
erase=7  H=7 prevH=-  prevErase=-
erase=8  H=7 prevH=7  prevErase=7  <<ERASE_MISMATCH
erase=8  H=0 prevH=7  prevErase=8
erase=15 H=0 prevH=0  prevErase=8  <<ERASE_MISMATCH
...
ERASE_MISMATCH count = 12
```

`H=0` 的帧对应 `ink.js:128` 的 `this.log.clear()`。**注意**：本探针的 mismatch 判定是"erase 值 ≠ 当前帧行数"，属于粗判，不能单独作为根因证据；但它确认了"擦除基准与帧高频繁不等"这一事实。

---

## 6. 根因排序

| 排序 | 成因 | 置信度 | 对"上半部分被挡"的贡献 |
|---|---|---|---|
| **H1** | live band 上限 12 行 + 只画尾部（§3.1/3.2） | `[已证实]` | **直接原因**：首轮大输出必然只显示尾部 |
| **H2** | fence-safe commit 在 ` ```json ` 未闭合期间整段不提交（§3.3） | `[高度可疑]` | **放大**：让 H1 的窗口从"几帧"拉长到"整个 args 期间" |
| **H3** | relative erase / Static 增量 / cursorPark 三者共用光标无绝对锚点（§3.4） | `[高度可疑]` | **恶化**：让被截断的观感变成"被覆盖"，并可能擦到上方历史行 |

三者关系：H1 决定"看不全"，H2 决定"看不全的持续时间"，H3 决定"看起来像被覆盖而不是像被滚动走了"。

---

## 7. 可判定的实验（下一步，仍未改代码）

1. **H1 终局验证**：在 `planMessageViewport` 里临时把 `DEFAULT_MAX_LIVE_ROWS` 改成 40，跑一次同样的 120 行首轮。若"上半部分被挡"消失 → H1 坐实。
2. **H2 终局验证**：在 `findFenceSafeOverflowCount` 里加日志，记录每次 `return 0` 时的 `total/maxTail/fenceOpenState`。若首轮大输出期间出现连续 `return 0` 且 live 行数 > 20 → H2 坐实。
3. **H3 终局验证**：在 `stdoutCursorPark.write()` 里记录 `(parkedUp, 该帧行数, 光标是否已在最后一行)`；若出现 `parkedUp > 0` 且光标位于终端最后一行 → `\x1b[NB` 无效，H3 坐实。

## 8. 修复方向（建议，未实施）

- **H1**：让 live band 在"运行中"可以临时超过 12 行（例如 `min(liveCount, messageRows)`），或对 `code_run` 结果做"折叠成 3 行摘要 + `Ctrl+O` 展开"，而不是靠视口裁切。
- **H2**：把 fence 平衡的判定从"必须闭合"放宽为"允许切在围栏内，但提交段自带围栏补全"，避免整段停滞。
- **H3**：给主帧引入**绝对行锚点**（每帧开始先 `\x1b[{row};1H` 定位，而不是纯相对擦除），或禁止在 Static 追加的那一帧做 park。
- **体验侧**：`handler.py` 的 `max_str_len=600` 对 `code_run` 偏小；图 1 的 `[omitted long output]` 会让用户误以为 UI 出问题，可考虑改为"保留头 N 行 + 尾 M 行 + 显式展开入口"。

---

## 附录：本次分析涉及的关键文件

| 文件 | 作用 |
|---|---|
| `frontends/ink/ui/src/messageViewportPlan.ts` | live band 高度决策（12 行上限） |
| `frontends/ink/ui/src/messageWindow.ts` | 转录行生成、`liveTranscriptViewportLines` 尾部裁切 |
| `frontends/ink/ui/src/streamCommit.ts` | 流式溢出提交、fence-safe 切点 |
| `frontends/ink/ui/src/messagePartition.ts` | done → Static / 未完成 → live 的切分 |
| `frontends/ink/ui/src/stdoutCursorPark.ts` | 光标 park/unpark（相对移动） |
| `frontends/ink/ui/src/layoutMetrics.ts` | 行数预算（`rows - 1`、`bottomRows`、`messageRows`） |
| `frontends/ink/ui/src/App.tsx` | 装配：Static + live band + BottomChrome，park 几何 |
| `frontends/ink/ui/node_modules/ink/build/{ink,log-update}.js` | 相对擦除与 Static 增量打印 |
| `genericagent/core/handler.py` | `smart_format` 工具结果截断 |
