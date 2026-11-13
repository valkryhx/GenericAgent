# GA Ink 光标「不如 Claude Code 丝滑」——机制差异定位

**日期：** 2026-11-12
**状态：** 已定位，未改代码（用户问的是「差在哪里」，本文回答这个问题）
**对比对象：** Claude Code fork 的 ink（`D:\git_repos\claude-code-fork\src\ink`）vs GA 现装的 npm `ink@5.2.1`
**探针：** `frontends/ink/ui/probes/` 下 6 个可复跑脚本（见 §6）

---

## 0. 结论先行

用户的三个观感各自有**独立的、可量化的成因**，不是同一个问题的三种表现：

| 观感 | 真实成因 | 量化事实 |
|---|---|---|
| 「光标不如 CC 丝滑、有卡顿」 | **帧率天花板 31fps 且整段重写**（CC 62fps + 只写 diff） | GA 8.4 帧/秒、每帧 1425 字节；CC `FRAME_INTERVAL_MS=16` |
| 「有时光标下穿到输入框下侧线」 | **没有同步输出（DEC 2026）**，终端把「擦除全段 → 重写全段」的中间态画出来 | GA 探针：`2026h/l = 0 / 0`（从未使用） |
| 「回复中输入后回退，卡顿更严重」 | 与打字同源：**流式中每帧成本更高**（1442 vs 669 字节）且**每帧一次 DECTCEM 翻转** | 流式中按键延迟 p90=85ms（空闲 62ms） |

**一句话：几何算术是对的（光标 100% 落在框内、行号列号与内容逐帧一致），差的是「怎么把这一帧写到终端上」——帧率、字节量、原子性、以及逐帧的光标开合。**

> **2026-11-12 二次修正：** 本文初版曾断言「20% 的帧 park 偏 1 列，根因是 onRender 早于 layout effect」。**那条结论是错的**——错在探针：它把 `> ` 里的 gutter 空格也 trim 掉了，于是空输入行算出的期望列比真实 caret 少 1。修正公式后重测：**stale 帧 = 0（0.0%，67 帧）**。§4 已重写为「此假设已证伪」。
>
> 我**没有**改动任何产品代码以外的坐标逻辑。

---

## 1. 先修正一条流传中的错误前提

仓库里的 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md` §3.3 断言：

> 「Claude Code 在主屏/inline 模式全程让原生光标可见……`log-update.ts:118`：帧末 `return [{ type: 'cursorShow' }]`」

**这条对 CC 源码的解读不完整。** 逐行核对后的事实：

- CC `renderer.ts:174` 主屏取值是 `visible: !isTTY || screen.height === 0` —— TTY 且**有内容时是 `false`（隐藏）**，只有空帧才可见；
- `log-update.ts:114 getRenderOpsForDone()` 的 `cursorShow` 只被 `renderPreviousOutput_DEPRECATED()` 调用，而后者唯一的调用点是 `ink.tsx:1469` 的 **unmount 收尾**；
- 稳态渲染路径 `LogUpdate.render()` 里**没有任何** `cursorShow` / `cursorHide` 分支（全仓库 grep 只有 `frame.ts:85` 的类型声明和 `optimizer.ts:79-80` 的抵消规则）。

也就是说：**CC 在稳态下并不每帧 SHOW 光标**；它在 `components/App.tsx:184` mount 时 HIDE 一次，帧内不再翻转，靠 `useDeclaredCursor` 的相对移动把（隐藏的）光标停到 caret。

**这对 GA 有直接后果**：GA 的 `stdoutCursorPark.ts` 每帧 `HIDE → 写帧 → PARK(SHOW)`，实测 **8.4~11.2 次/秒的 DECTCEM 翻转对**。这个翻转是 GA 为了修 IME 主动加的（IME 需要可见光标锚点，那次诊断是对的），但它和 CC 的做法不一致——CC 从不翻转。翻转频率与帧率成正比，且**在原子提交（§3）落地后完全可以省掉**，见 §5 #2。

---

## 2. 「卡顿」的量化成因：帧率与字节

### 2.1 帧率天花板不同

```
npm ink@5.2.1  node_modules/ink/build/ink.js:
    this.rootNode.onRender = throttle(this.onRender, 32, { leading:true, trailing:true })
    → 上限 31fps

CC  src/ink/constants.ts:
    export const FRAME_INTERVAL_MS = 16        → 上限 62fps
    src/ink/ink.tsx:212:
    const deferredRender = () => queueMicrotask(this.onRender)
    this.scheduleRender = throttle(deferredRender, FRAME_INTERVAL_MS, ...)
```

**CC 是 GA 的两倍帧率**，且在 `throttle` 之外多套了一层 `queueMicrotask`（下一节说明为什么这层很关键）。

### 2.2 每帧不是 diff，是整段重写

```
npm ink@5.2.1  node_modules/ink/build/log-update.js:
    stream.write(ansiEscapes.eraseLines(previousLineCount) + output)
    → eraseLines(prevN) 擦掉上一帧全部 N 行，再整块重写全量 output

CC  src/ink/log-update.ts: render() → Patch[] diff
    → 只写变化的 cell；terminal.ts:203 再套 BSU/ESU
```

实测（`probes/cursor_toggle_cost.ts`，100x30 终端，含输入框与状态行）：

| 场景 | GA 帧率 | GA 字节/秒 | 单帧字节 |
|---|---|---|---|
| 空闲（无输入） | 0 帧 | 0 | — |
| 空闲逐字输入 | 5.1 帧/秒 | 3.4 KB/s | ~669 |
| 流式输出 | **8.4 帧/秒** | 10.2 KB/s | 1425（max 1516） |
| 流式 + 打字/回退 | 8.4 帧/秒 | 10.4 KB/s | — |

一次按键 = **3 次 write**（unpark + 整帧 + park）、**~670 字节**，空闲时延迟 p50=23ms / p90=62ms；流式中延迟 p50=29ms / p90=85ms。CC 在同场景下每帧只写变化的几十字节。

**这就是「卡顿感」的主体**：31fps 上限 + 每帧 1.4KB 全量重写。终端要在 32ms 内吃掉 1.4KB 并重排整块动态区，键入的回显因此有肉眼可感的拖尾。

### 2.3 帧间隔实测分布

`probes/park_stale_race.ts`（高频连打）：

```
n=79  p10=38ms  p50=51ms  p90=115ms  max=208ms
```

`p90=115ms` 说明**高峰期单帧要 115ms**——远超 32ms 的节流窗口。这不是节流的错，是渲染本身（React 全树 reconcile + yoga + `eraseLines` 重写）已经超过了窗口预算。CC 用屏幕 diff 把这段成本压到很低。

### 2.4 一个必须说明的测量现象：渲染成本会反过来拖慢事件循环

上面几张表里的输入速率是我用 `delay(20)` 之类排的，但实测**实际耗时是标称的约 2 倍**：

```
cursor_toggle_cost.ts  流式场景：预期 100 × 20ms = 2000ms，实测 4281ms
frame_cost_and_caret_row.ts：预期 100 × 10ms = 1000ms，实测 2990ms
```

也就是说，每帧 1.4KB 的全量重写 + React/yoga 重算，**把驱动它的定时器本身拉长了一倍**。这形成一个正反馈：

```
渲染慢 → 定时器被推迟 → 帧更稀疏、单帧内容更大 → 渲染更慢
```

这条对「卡顿感」的贡献可能比单纯的帧率数字更直接：用户感受到的是**输入响应被渲染阻塞**，而不是「只有 31fps」。所以在 §5 的修法排序里，**减少每帧字节（#4）虽然在最后，但它同时也是唯一能打断这个正反馈的一项**——只是侵入性最大，需要动 ink 的 `log-update`。

---

## 3. 「下穿到下侧线」的成因：没有原子提交

GA **完全没使用** DEC 2026 同步输出：

```
probes/cursor_toggle_cost.ts →  同步输出 2026h/l = 0 / 0     （四个场景全为 0）
```

这意味着终端会**逐字节/逐块把中间态画出来**。而 GA 每帧的动作是：

```
eraseLines(N)         ← 这一瞬间，动态区是空的/部分空的
+ 整块 output          ← 再把 N 行重新画出来
+ park(SHOW)          ← 最后才把光标停到 caret
```

流式输出时这个「先空一下再出现」每秒重复 8~11 次。视觉上的表现，就是用户描述的「光标像往下穿了一下 / 压到下侧线上」——**那不是光标算错了位置，是它在动画中间态里被看见了。**

CC 的做法（`src/ink/terminal.ts:203`）：

```ts
const useSync = !skipSyncMarkers
let buffer = useSync ? BSU : ''
...  // 攒完所有 patch
if (useSync) buffer += ESU
terminal.stdout.write(buffer)     // 一次性提交，终端要么全画要么全不画
```

`probes/sync_output.ts` 已证明这个包装在 GA 的 stdout 层是可行的（给含 `\x1b[2K` 的写包上 BSU/ESU），只是**还没接进产品**。

### 3.1 几何本身是干净的（重要：排除误判）

必须先把「光标坐标算错」这个假设排除掉。`probes/park_absolute_row.ts` 用 `virtualScreen` 回放真实写入串成字符网格（不依赖 GA 自己的布局模型），在真实网格里定位输入框边框，判定 park 落点：

```
终端 100x30：总配对帧=38    inside=38    下穿（落在下边框/更下）= 0 / 38
终端  60x12：总配对帧=25    inside=25    下穿 = 0 / 25
终端  70x16：同类结果，inside 全部
```

`probes/park_staleness.ts` 同时验证列号：

```
配对帧 30   列不一致 0 (0%)   行不一致 0
```

**结论：光标从来没有真的落到下侧线上或框外。** 用户看到的「下穿」是 §3 的中间态曝光。这个区分很重要——按「坐标算错了」去改 `inputCursorPosition` 只会改坏一个本来正确的模块。

---

## 4. 「回退时更卡」的成因：与打字同源，只是流式中每帧更贵

### 4.1 一个已被证伪的假设（保留记录）

我初版曾把这条归因为「`onRender` 在 layout effect 之前同步触发 → park 规格落后一次 commit」，并用探针测出「20% 的帧偏 1 列」。**这个结论是错的**，两处都得纠正：

**错误一：探针公式错了。** 我的期望列公式把 `> ` 里的 **gutter 空格**一起 trim 了：

```ts
// 错：` > ` 后跟 1 个 gutter 空格，然后才是输入文本。
// 空输入时 slice(gt+1).trimEnd() 会把 gutter 空格 trim 掉 → 期望列少 1。
const text = raw.slice(gt + 1).replace(/\s+$/, '')
const expectCol = gt + 1 + text.length + 1
```

16 个「stale」帧的 `text` **全部是 `""`**，正是这个公式必然算错的情形。改成跳过 gutter 后：

```ts
const text = raw.slice(gt + 2).replace(/\s+$/, '')   // 跳过 gutter 空格
const expectCol = gt + 3 + text.length
```

重测结果（`probes/park_stale_race.ts`，12 轮 × 5 字符 + 5 次回退，字符间隔 5ms）：

```
写出的帧 = 67   带 park 的帧 = 67
stale 帧 = 0  (0.0%)
  光标超前 = 0   光标落后 = 0   最大偏移 = 0 列
```

**stale 归零。** 同时 `probes/park_staleness.ts` 独立复测也是 `列不一致 0 (0%) / 行不一致 0`（28 帧）。

**错误二：机制推理跳了一步。** 即使 ink 的 `onRender` 确实由 `resetAfterCommit` 同步触发，park 也**不是在那一瞬被消费**的——`CursorParkWriter.schedulePark()` 把 park 放进 **microtask**：

```ts
// stdoutCursorPark.ts:112  this.schedule(() => { ... this.sink(...) })
const defaultScheduler: Scheduler = (cb) => { queueMicrotask(cb) }
```

microtask 在**本轮所有同步写入之后、下一次宏任务之前**执行，而 React 的 layout effect（`setPark` 所在）也在同一个 commit 的同步阶段内完成。所以 park 消费时读到的已经是本次 commit 写入的规格。**GA 无意中用 microtask 达到了 CC 用 `queueMicrotask(onRender)` 达到的同一个效果。**

> **这条假设留着是有价值的**：它说明「同一组字节证据，换一个公式就可能得出相反的结论」。若将来真要怀疑代次问题，必须先让探针在一个**已知正确**的空输入帧上报出一致，再去看可疑帧。

### 4.2 真正的成因：流式中每帧更贵 + 每帧翻转光标

| | 空闲按键 | 流式中按键 |
|---|---|---|
| 字节/按键 | 669 | **1442**（2.2×） |
| 延迟 p50 | 23ms | 29ms |
| 延迟 p90 | 62ms | **85ms** |

流式时每一帧都要重写整块动态区（1.4KB，因为 transcript 在长），而回退/打字都必须等这一帧写完才轮到自己。**「回退更卡」= 键入事件和流式帧在抢同一个 32ms 节流窗口，后者更贵。**

再叠上每帧一次 `HIDE→SHOW`：流式中 8.4 次/秒翻转到 11.2 次/秒，视觉上是光标在开合。用户把「光标不跟手 + 它在闪」合并感知成了「卡顿更严重」。

**结论：`#4` 不构成一个独立缺陷。** 修好 §2（帧率/字节）与 §3（原子性）后，这一项随之消失。

---

## 5. 差异清单（可直接按此排序动手）

按「收益 / 改动面」排序。**都不需要 fork ink。**

| # | 差异 | CC 的做法 | GA 现状 | 修法 | 收益 |
|---|---|---|---|---|---|
| 1 | **同步输出 / 单次原子写** | `BSU + patches + ESU` 单次 write | **✅ 已修**：一次突发合并成一次 write 并用 BSU/ESU 包住 | 见 `20261112-ga-ink-cursor-smoothness-fix-design.md` | 消除「下穿」观感；流式 HIDE 22→0 |
| 2 | **DECTCEM 翻转** | mount 时 HIDE 一次，帧内不翻转 | **✅ 已修**：`cursorShown` 记账，只在「不可见→可见」时 SHOW | 同上 | 7.9~15.2 次/秒 → 0 |
| 3 | **帧率** | `FRAME_INTERVAL_MS = 16` | 32ms（ink 内部写死） | **未做**：须先降字节，否则负载翻倍 | — |
| 4 | **每帧字节** | 屏幕 diff（Patch[]） | `eraseLines(N) + 全量 output` | **未做**：需 fork ink `log-update` | 大，但成本高 |

---

## 6. 保留的探针（用户要求保留）

全部在 `frontends/ink/ui/probes/`，`npx tsx` 直接跑，无外部依赖（用 `FakeReadStream` + `CaptureWriteStream` 驱动，不需要真 LLM）。

| 探针 | 回答什么问题 | 关键输出 |
|---|---|---|
| `caret_lag_probe.ts` | 打字/回退时 park 落后几个字符、每次按键多少字节 | 分段帧数/字节/park 列序列 |
| `frame_cost_and_caret_row.ts` | 流式时的帧率与字节；park 行是否落在帧内 | 帧/秒、单帧字节、越界计数 |
| `caret_row_truth.ts` | 声明的 up 与「内容推出的期望 up」逐帧对比 | delta 分布 |
| `park_staleness.ts` | 列/行是否与该帧内容一致 | 不一致百分比 |
| `park_absolute_row.ts` | **权威**：virtualScreen 回放出网格，判定 park 是否落在输入框边框内 | `inside` / `on-bottom-border` / `below-composer` 分布 |
| `park_stale_race.ts` | 高频连打下的 stale 帧比例、帧间隔分布 | stale %、p50/p90/max 帧间隔 |
| `park_ordering.ts` | setPark / frame / park 的时间轴顺序 | 陈旧 vs 同代计数 |
| `cursor_toggle_cost.ts` | HIDE/SHOW 频率、BSU/ESU 有无、KB/s | 每场景的翻转次数与字节率 |
| `key_latency.ts` | 单次按键从 stdin 到 park 落定的延迟与字节 | 空闲 vs 流式中的 p50/p90/max |

**注意（方法论）**：`caret_row_truth.ts` 第一版把「多行输入时 caret 在最后一行、不在 `>` 行」算成了偏差（`delta=-1`），那是**探针自己的错**。已修正为取「输入块的最后一行」。修正后 E/F 场景仍各剩 1 帧 `delta=-1`，但那两帧恰好是**切换瞬间**（输入块从 1 行变 2 行 / 通知行插入使 bottom +1），属于 §4 的同代性问题，不是坐标错误——用 `park_absolute_row.ts` 的网格判定交叉验证过：那两帧的 park 仍落在框内。这类误判是这个仓库反复踩过的坑（见 AGENTS.md 关于「测试自己在骗人」的记录）：**先用一个能交叉验证的第二判据确认探针本身可信，再用它判定可疑帧。**

---

## 7. 分析边界（诚实标注）

- 帧率、字节、DECTCEM 翻转、BSU/ESU 缺失：**全部实测**，可复跑。
- 「下穿是中间态曝光」：由 `sync_output.ts` 证明 BSU/ESU 可包装 + §3 的擦除/重写结构推出。**未**在真机上用高速摄像/录屏逐帧比对确认（探针只能证明字节层面的中间态存在）。
- stale 帧 20%：在 100x30 假终端 + 5ms 连打下测得。**真机人手速（~100ms/字符）下实际比例会低**，但「回退时慢半拍」的形态一致。
- 本文**未做任何产品代码改动**。
- CC 那一侧只验证到 `src/ink` 层；`BaseTextInput.tsx` 的 `useDeclaredCursor` 消费方式已读，但未验证 CC 在 Windows Terminal 下的实际 DECTCEM 行为（CC 源码里没有每帧 SHOW，因此 GA 现有的每帧 SHOW 是**为 IME 主动加的**，不是抄 CC 抄错）。
