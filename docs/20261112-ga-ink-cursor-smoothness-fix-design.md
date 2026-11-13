# GA Ink 光标「丝滑度」修复设计方案

**日期：** 2026-11-12
**前置分析：** `docs/20261112-ga-ink-cursor-smoothness-vs-cc.md`（含已证伪的假设与二次修正）
**涉及文件：** `frontends/ink/ui/src/stdoutCursorPark.ts`、`frontends/ink/ui/src/syncOutput.ts`（新增）、`frontends/ink/ui/src/main.tsx`、`frontends/ink/ui/src/stdoutCursorPark.test.ts`

---

## 0. 先划清「改什么」和「明确不改什么」

前置分析把三个观感归到两处真实成因（第三条已被证伪）：

| 现象 | 已证实的成因 | 本方案是否处理 |
|---|---|---|
| 光标「下穿 / 压到输入框下侧线」 | 无同步输出，终端画出「擦除全段 → 重写全段」的中间态 | ✅ P0 |
| 「卡顿、不跟手」 | 每帧一次独立 write、逐帧 `HIDE→SHOW` 翻转、帧成本随流式上涨 | ✅ P0 + P1 |
| 「回退更卡」 | 与上一条同源（流式中每帧 1442B vs 空闲 669B） | ✅ 随 P0/P1 消失 |

**明确不做（附理由）**：

1. **不把帧率从 31fps 提到 62fps。** ink 5.2.1 的 `throttle(onRender, 32)` 是硬编码的，要改必须 fork 或反射改私有字段。更关键的是：**GA 每帧是整段重写（~1.4KB），CC 能用 62fps 是因为它每帧只写 diff 的几十字节。** 在字节量没降下来之前提高帧率 = 把 1.4KB×31 变成 1.4KB×62，终端负载翻倍，观感只会更糟。帧率是「diff 化」的收益，不是它自己的收益。
2. **不改 `inputCursorPosition` / `layoutMetrics` 的任何算术。** 探针已证明光标 100% 落在框内、行号列号逐帧与内容一致（`inside = 38/38`、`列不一致 0%`）。动这里只会改坏本来正确的模块。
3. **不改 IME 的可见性决策（park 时 SHOW）。** 那是 2026-07-16 修过的真实缺陷，本方案只减少不必要的翻转，不取消 SHOW。

---

## 1. P0：把「一帧」变成一次原子写入

### 1.1 现状

一条流式输出链路里，ink 的 `log-update` 每次渲染写一次，`CursorParkWriter` 把它拆成三段独立的底层 write：

```
write(unpark)   →  base.write(HIDE + ESC[nB + \r)
write(frame)    →  base.write(eraseLines(N) + output)      ← 动态区此刻是空的
schedulePark →  base.write(ESC[nA + ESC[mG + SHOW)        ← 光标此刻才落位
```

三个独立 write 之间，终端有机会刷新屏幕。于是「动态区被擦掉但还没重画」的中间态会被画出来 —— 这就是「光标下穿到下侧线」的观感来源（`probes/cursor_toggle_cost.ts` 实测 `2026h/l = 0/0`，即从未用过同步输出）。

### 1.2 改法

把整个 onRender 突发（ink 可能连写 2~3 次：`log.clear` / `<Static>` / 主帧）攒进一个缓冲区，在 microtask 里**一次性**写出，并用 DEC 2026 同步输出包住：

```
BSU + [unpark] + [clear] + [static] + [frame] + [park] + ESU     ← 一次 base.write
```

终端要么整块显示、要么整块不显示，中间态不可见。

### 1.3 为什么 microtask 是对的时机

这正是 `CursorParkWriter` 现有的调度方式（`schedulePark` 已在 microtask 里）。microtask 在「本轮所有同步写入之后、下一次宏任务之前」执行，所以：

- ink 一次 `onRender` 内的**全部**同步写入都已入缓冲；
- React 的 layout effect（`setPark` 所在）也已在同一 commit 的同步阶段完成 —— park 规格是**本次** commit 的，不存在代次滞后；
- 一次突发只产生**一次** `base.write`。

> 前置分析初版以为这里存在「park 规格陈旧」缺陷，实测为 0%。原因就是这套 microtask 缓冲（当时只用于 park）。P0 把它的适用范围从「park」扩大到「整帧」。

### 1.4 终端支持探测

新增 `syncOutput.ts`，照搬 CC `src/ink/terminal.ts:70 isSynchronizedOutputSupported()` 的判定（排除 tmux，识别 Windows Terminal / VTE / kitty / iTerm / WezTerm / ghostty / xterm.js 等），并支持显式覆盖：

```
GA_UI_SYNC_OUTPUT=0   → 强制关闭（不支持的终端上排查用）
GA_UI_SYNC_OUTPUT=1   → 强制开启
```

不支持的终端上：**仍然一次 write**（这本身就是收益），只是不包 BSU/ESU。

### 1.5 风险与回退

| 风险 | 处置 |
|---|---|
| 某终端对 2026 支持不佳，出现整块不刷新 | `GA_UI_SYNC_OUTPUT=0` 立即回退，无需重编译 |
| 缓冲导致某次写到盘延迟 | 延迟上限是一个 microtask（同一 tick），与现有 park 一致 |
| 非字符串/Buffer 写入 | 保持原防御分支：先 flush 缓冲，再原样透传 |

---

## 2. P1：同步输出可用时，取消逐帧 `HIDE→SHOW`

### 2.1 现状

`unpark` 无条件先发 `HIDE`（`\x1b[?25l`），`park` 再发 `SHOW`（`\x1b[?25h`）。实测流式中 **8.4~11.2 次翻转对/秒** —— 肉眼即「光标一直在闪」。

### 2.2 为什么原子提交后就可以不翻转

`HIDE` 的唯一职责是「别让人看见光标从 caret 滑回帧底」。**原子提交之后，帧底位置永远不会被显示**，这个职责自然消失。

### 2.3 改法

- `sync` 生效时：`unpark` 不发 `HIDE`；`park` 只在「光标当前不可见」时发一次 `SHOW`，并用 `cursorShown` 记账避免逐帧重复。
- `sync` 不生效时：保持现有 `HIDE → SHOW`（回退路径行为不变）。
- 不变量：**任何时刻只要光标停在 caret，它就是可见的**（IME 锚点，2026-07-16 的修复不能被削弱）。

---

## 3. 判定标准（改完怎么算成功）

全部是字节级、可断言的事实，不靠目视：

| 判据 | 现状 | 目标 |
|---|---|---|
| 一次 onRender 突发的 `base.write` 次数 | 3 | **1** |
| BSU(`\x1b[?2026h`) 与 ESU 配对完整 | 0 / 0 | **n / n 且 n > 0** |
| 擦除(`\x1b[2K`)是否全部落在 BSU…ESU 内 | 0 / 760 | **100%** |
| 流式中 `HIDE` 次数 | 8.4 次/秒 | **0**（sync 开） |
| 光标是否仍落在输入框内 | 38/38 | **38/38 不退化** |
| 行号/列号与内容一致 | 0% 不一致 | **不退化** |

回归测试加在 `stdoutCursorPark.test.ts`（字节契约）+ `syncOutput.test.ts`（探测逻辑）。

---

## 4. 实施顺序

1. `syncOutput.ts` + 单测（纯函数，先立判定标准）
2. 重写 `CursorParkWriter` 为「缓冲 + 单次原子写」
3. 更新 `stdoutCursorPark.test.ts` 的字节契约
4. `main.tsx` 接线（默认自动探测）
5. `npm run test` / `npm run typecheck`
6. 用 `probes/cursor_toggle_cost.ts` 复测六项判据

---

## 5. 实施记录（2026-11-12 已完成）

### 5.1 落地文件

| 文件 | 改动 |
|---|---|
| `src/syncOutput.ts` | **新增**。DEC 2026 探测（照搬 CC 判定表 + `GA_UI_SYNC_OUTPUT` 覆盖）+ `wrapSynchronizedUpdate` |
| `src/syncOutput.test.ts` | **新增**。8 项：覆盖优先级、tmux 强制排除、白名单、VTE 版本、老 conhost、包络配对 |
| `src/stdoutCursorPark.ts` | **重写**。`write()` 只入缓冲；`flush()` 合并成一次 `sink`；新增 `cursorShown` 记账；`unpark` 改为 `flush(false)` |
| `src/stdoutCursorPark.test.ts` | **重写**。12 项字节契约，含「一次突发 = 一次 write」「sync 下零翻转」「parked ⟺ visible」不变量 |
| `src/main.tsx` | 补注释说明同步输出默认开启与覆盖方式 |
| `src/App.tsx` | `cleanupTerminalOnce` 加两次 `cursorPark?.flush()` —— 缓冲模型下不 flush 会丢最后几帧与清理序列 |
| `probes/fix_acceptance.ts` | **新增**。六项判据一键复测，可切 `GA_UI_SYNC_OUTPUT=0` 出基线 |

### 5.2 验收结果（`probes/fix_acceptance.ts`，100x30 假终端）

**流式 2.5 秒：**

| 判据 | 基线（SYNC OFF） | 修复后（SYNC ON） | 结论 |
|---|---|---|---|
| BSU / ESU | 0 / 0 | **21 / 21** | ✅ 配对 |
| 事务内擦除占比 | 0 / 660 | **630 / 630（100%）** | ✅ 全部原子 |
| HIDE 次数（流式） | 22（**7.9 次/秒**） | **0** | ✅ 零翻转 |
| HIDE 次数（流式+打字） | 20（15.2 次/秒） | **0** | ✅ 零翻转 |
| 光标落在输入框内 | 44 / 44 | **41 / 41** | ✅ 未退化 |
| park 列与内容一致 | 44 / 44 | **41 / 41** | ✅ 未退化 |

**一次 onRender 突发的 write 次数：3 → 1**（单测 `one onRender burst becomes exactly ONE write` 钉死；这一项无法用环境变量出基线，因为它由缓冲模型决定，与同步输出开关无关）。

**测试**：`npm run test` → **701 pass / 0 fail**；`npm run typecheck` → clean。

### 5.3 未做的两项，以及为什么

- **帧率 31fps → 62fps：没做。** §0 已说明：GA 每帧整段重写 ~1.4KB，CC 能用 62fps 是因为它每帧只写 diff 的几十字节。
- **每帧 diff 化：没做。** 需要 fork ink 的 `log-update`，侵入性远超本次范围。

这两项后面合并成一件事来评估了，结论见 §6。

### 5.4 一个测量上的坑（记录备查）

写验收探针时踩了两次**探针自身**的错，都曾短暂指向错误的结论：

1. 判据 3 的比较方向写反了（把「事务外的擦除」当成「事务内的」输出），一度显示 `0/690 ❌`。实际算的是补集。
2. 检测 park 时用 `\x1b[?25h` 定位 —— 但修复后**正常情况就不再发 SHOW**，于是采样数变成 0，看起来像「几何无从验证」。改为直接匹配 `ESC[<n>A + ESC[<m>G` 后，41/41 全部正常。

**教训与 §4.1 相同：判据写错时，「修复看起来失败了」和「探针坏了」输出长得一模一样。** 每次看到红色判据，先确认它在已知正确的输入上也是绿的。

---

## 6. 「diff 化到底要不要改 ink 源码？」——调研结论

这是「每帧整段重写」那条的延伸问题。结论：**不需要 fork，而且收益的大头可以在 GA 自己的 stdout 包裹层拿到。**

### 6.1 ink 5.2.1 为什么没有 diff

`renderer.js` 内部的 `Output` **确实建了完整 cell 网格**，但 `output.get()` 立刻把它摊平成
`styledCharsToString(...).join('\n')`，返回的是字符串：

```js
const { output: generatedOutput, height: outputHeight } = output.get()
return { output: generatedOutput, outputHeight, staticOutput }
```

`log-update.js` 也只认识 `eraseLines(previousLineCount) + output`。**diff 需要的中间表示
（cell 网格）在交到调用方之前就被销毁了** —— 所以 diff 在 ink 里没有挂点，这是结构性的，
不是配置问题。

CC 的做法是 fork ink，把 `Frame { screen: Screen, cursor }` 一路保留到 `log-update`，
再在那里做 cell 级 `diffEach` → `Patch[]`。那条路要改 `renderer` / `output` / `log-update` 三个文件。

### 6.2 但 GA 有 CC 没有的东西：stdout 包裹层

GA 已经有 `createCursorParkStdout` 这个 Proxy，它每帧能看到**完整帧字符串**。
「上一帧」与「本帧」的行数组都在手里，因此可以在这一层做**行级 diff**，完全不碰 ink。

`probes/line_diff_verify.ts` 实测（100x30，36 帧真实回放，网格+光标逐帧比对）：

| 场景 | 现状 | 行级 diff | 节省 |
|---|---|---|---|
| 流式输出 | 757 B/帧 | **254 B/帧** | 66% |
| 打字 + 回退 | 839 B/帧 | **29 B/帧** | 97% |
| 多行输入 | 835 B/帧 | **224 B/帧** | 73% |
| **合计** | 29 639 B | **5 613 B** | **81%** |

**正确性：36/36 帧网格与光标完全一致。**

边界情况（`probes/line_diff_edges.ts` + `probes/line_diff_shrink.ts`）：

| 边界 | 结论 |
|---|---|
| 撑满视口（内容 ≥ 终端行数） | ✅ 一致（ink 此时走 `clearTerminal` 路径，必须**原样透传**，不能 diff） |
| 动态区变高 | ✅ 一致 |
| 动态区**变矮** | ⚠️ 必须**退化全量重写**（`next.length < prev.length → null`） |
| 等高、行内变化 | ✅ 一致 |
| 带 ANSI 样式的行 | ✅ 一致（行首需补 `\x1b[0m`，实测 3.4% 的行行末未 reset） |

合成帧 9 个用例全部通过，含两条退化路径。

### 6.3 行级 diff 拿不到的两项（必须诚实标注）

1. **「一行里只改几个字符」** —— 行级 diff 会重写整行。打字时这已经无所谓（29 B/帧），
   但长行首中尾都变时不如 cell 级。
2. **滚动整屏用硬件滚动（DECSTBM）** —— CC 在 alt-screen 下用 `CSI top;bot r` + `CSI n S/T`
   把整屏上移交给终端做。行级 diff 只能一行行重画。GA 默认是 inline 模式，这条收益本就较小。

### 6.4 帧率：不改源码也能改

`node_modules/ink/build/instances.js` 的 `instances` 是个 `WeakMap`，可以用文件 URL 直接导入
（绕过 `exports` 限制）拿到 `Ink` 实例，`rootNode.onRender` 是 lodash `throttle` 包装过的函数，
**带 `.cancel()` / `.flush()`**。因此可以不改源码地重设节流间隔：

```js
const { default: instances } = await import(pathToFileURL(
  path.resolve('node_modules/ink/build/instances.js')).href)
const ink = instances.get(stdout)
ink.rootNode.onRender = throttle(ink.onRender, 16, { leading: true, trailing: true })
```

`probes/_probe_ink_access.ts` 已验证 `instance: FOUND / onRender.cancel: function`。

**但这一步建议放在行级 diff 之后做。** 现在的 1.4KB/帧 × 31fps 已经让终端吃紧；
先降到 ~250 B/帧，再提帧率才是净收益，否则只是把负载翻倍。

### 6.5 建议的下一步（按收益/风险排序）

| 顺序 | 改动 | 预估收益 | 风险 | 位置 |
|---|---|---|---|---|
| 1 | 行级 diff（含 `clearTerminal` 原样透传、变高/变矮退化） | **实测 0%（见 §7）** | 低 | `lineDiff.ts` + `stdoutCursorPark.ts` |
| 2 | 帧率 32ms → 16ms（反射改 `onRender`） | 帧率 ×2 | 中，依赖 ink 私有结构 | `main.tsx` |
| 3 | cell 级 diff / DECSTBM 滚动 | 额外 −10~20% | 高，必须 fork ink | 不建议 |

> **注意顺序**：1 在 2 前面不是偏好，是必要条件。先提帧率会放大每帧字节，**观感会更糟**。

---

## 7. 第 1 步已实施：行级 diff（2026-11-12）

### 7.1 落地文件

| 文件 | 内容 |
|---|---|
| `src/lineDiff.ts` | **新增**。`parseFrameRows` / `looksLikeFrame` / `buildLineDiff`，纯函数 |
| `src/lineDiff.test.ts` | **新增**。25 项：解析边界、行数变化退化、虚拟终端回放等价性 |
| `src/stdoutCursorPark.ts` | 接入：`prevRows` 记账 + 首次/解析失败/变矮时原样透传 |
| `src/stdoutCursorPark.test.ts` | 新增 3 项端到端：等价性、变矮退化、多帧突发透传 |

### 7.2 ⚠️ 关键结论：在当前布局下收益为 0（诚实报告）

**行级 diff 完整实现了、也正确，但在 GA 当前布局下永远不会生效。**

原因在 `layoutMetrics.ts:55`：

```ts
const rows = Math.max(1, terminalRows(input.rows) - 1)
```

动态区的高度被设计成**恰好等于 `terminalRows - 1`**。而 ink 每帧写 `body + '\n'`，所以一帧占 `contentRows + 1 = terminalRows` 行 —— **正好等于视口高度，必然触底**。

触底意味着：全量重写的末尾换行会滚动屏幕，而行级 diff 不写那些空行、不滚动。两者不可能等价，`buildLineDiff` 因此每次都返回 `null`（退化）。

实测（`probes/line_diff_acceptance.ts`，单次运行双回放）：

| 终端高度 | diff 生效帧 | 退化帧 | 字节节省 | 屏幕不一致 |
|---|---|---|---|---|
| 100x30 | **0** | 26 | **0.0%** | 0 |
| 100x40 | **0** | 27 | **0.0%** | 0 |

对比：把守卫放宽成 `next.length + 1 > terminalRows` 时，30 行终端里 26 帧能 diff、省 73.8%，但**屏幕不一致 25/31，光标全部错位**（`cursor (25,3) vs (29,0)`）。这直接证明守卫不能放宽。

**所以这一步的真实产出是「一个正确但当前不生效的优化」+「一条被证伪的收益预估」。** 我在 §6.2 报的 81% 是在**没有考虑滚动语义**的模型上测的，那个数字不成立。

### 7.3 要让它生效，需要改前置条件（不属本次范围）

行级 diff 要产生收益，动态区必须**留出至少 1 行余量**（`contentRows + 1 < terminalRows`）。可选做法：

- 把 `layoutMetrics.ts:55` 的 `-1` 改成 `-2`（牺牲 1 行可视区）；
- 或只在「帧高 < 视口」时启用 diff（现在就是这样，等价于永不用）。

前者要动布局，会影响所有既有截图与布局测试；后者就是现状。**这个取舍需要你来定，我没有动。**

### 7.4 仍然真实的收益

`syncOutput` + 单次原子写 + 取消逐帧翻转（§5）是**独立于布局**的收益，已经生效：

| 判据 | 基线 | 现在 |
|---|---|---|
| BSU/ESU 配对 | 0 / 0 | 20 / 20 ✅ |
| 事务内擦除 | 0 / 660 | 277 / 277（100%）✅ |
| HIDE（流式） | 7.9 次/秒 | **0** ✅ |
| 光标在框内 | 44/44 | 44/44 未退化 ✅ |

**测试**：`npm run test` → 727 pass；`npm run typecheck` → clean。

### 7.5 这一轮我在探针上错了 6 次（记录，避免重犯）

全部是「探针错 → 看起来像产品错」：

| # | 错法 | 表现 | 教训 |
|---|---|---|---|
| 1 | `\x1b[0G` 当成分隔符 | 帧解析全 null | ink 5.2.1 用 `\x1b[G` |
| 2 | 擦除计数漏了 ink 的 `N+1` | 虚拟终端每帧漂一行 | `previousLineCount = output.split('\n').length` |
| 3 | 跨两次运行比对 | 比的是随机动词 + 计时 | 要么冻随机，要么单次运行双回放 |
| 4 | `virtualScreen` 不模拟滚动 | 27 帧「不一致」全是模型缺陷 | 帧高触顶时必须用带滚动的终端 |
| 5 | 整块替换成 diff | 丢了 park 段 → 光标全错 | 只替换帧内容，前后缀原样保留 |
| 6 | 判据 3 比较方向写反 | `0/690 ❌` | 算的是补集 |

**共同点：判据坏掉时，「修复失败」和「探针坏了」的输出完全一样。** 每次看到红色判据，先在一个已知正确的输入上确认探针是绿的。

