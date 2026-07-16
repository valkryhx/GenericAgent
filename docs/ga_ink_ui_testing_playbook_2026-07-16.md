# React + Ink 终端 UI 的「无截图」测试方法论 —— GA Ink UI 实战 playbook

**日期：** 2026-07-16
**适用范围：** `frontends/ink-ui/src/*.test.ts`（Node test runner + tsx）
**本文定位：** 沉淀「不靠人眼截图、用程序化手段感知 UI bug」的测试经验，作为后续 GA Ink UI 开发/测试的**参考文档**。所有模式都来自 `95e5dc4..2a6898c` 这段区间真实修复中沉淀的测试代码，可直接照抄套用。

---

## 0. 核心思想（先读这一句）

> **终端 UI 的 bug 最终都表现为「写进 stdout 的字节」或「布局出来的行列几何」出了问题。这两样都是确定性的、可被程序读取和断言的——所以绝大多数 UI bug 不需要人眼看截图，可以被降维成单元测试里的不变量。**

人眼在截图里看到的「光标漂了」「输入框塌了」「整框在晃」「颜色不对」「重复显示」，本质上都是下面某一层的确定性事实：

| 人眼看到的现象 | 降维成的可断言事实 | 断言手段 |
|---|---|---|
| 光标/IME 漂到错位置 | stdout 里的光标定位字节序列不对 | 字节级 ANSI 断言 / 虚拟终端追踪 |
| 输入框「整框晃」/ghost | 连续帧的 `eraseLines` 擦错了行 | 虚拟终端追踪器判定擦除行区间 |
| 输入框被压塌 | 帧里两条边框行之间没有 caret 行 | 内存渲染 + 帧行号解析 |
| 中文/emoji 处换行错乱 | 某行显示宽度超过画布宽度 | `string-width` 宽度断言 |
| 消息重复显示 / 双显 | 同一条消息同时进了 static 和 live 分区 | 纯函数分区 + 计数断言 |
| 颜色/样式不对 | 渲染出的 part 的 color 字段不对 | 结构化 parts 断言 |

**截图只应作为「发现新现象」的入口；一旦定位到是哪一层的问题，就应该写一个能复现它的确定性测试，把它钉死。** 本文给出 GA 已经在用的 7 类手段。

---

## 1. 手段一：虚拟终端光标追踪器（最巧妙，能测「看不见的相对光标算术」）

**文件：** `cursorParkModel.ts` + `cursorParkModel.test.ts`

**解决的问题：** ink 用**纯相对光标移动**重绘每一帧（`eraseLines` 从当前光标位置往上擦 N 行）。GA 的 cursor park 又在帧末把光标相对移动到 caret。这套「相对移动算术」一旦错位，就会 ghost composer / 整框漂移——但**你无法用字符串包含断言测「相对移动对不对」**，因为它依赖光标的历史累积位置。

**手段：** 写一个**极简虚拟终端**（`CursorTracker`），它解释 ANSI 序列并维护一个 `{row, col}` 光标状态 + 记录被 `\x1b[2K` 擦除的行号：

```ts
class CursorTracker {
  cursor = { row: 0, col: 0 }
  erasedRows: number[] = []
  feed(seq: string): void {
    // 解析 \x1b[<n>A/B/C/D（相对移动）、\x1b[<n>G（定列）、
    // \x1b[<r>;<c>H（CUP）、\x1b[2K（擦行）、\n、\r ...
    // 每个序列更新 cursor / 追加 erasedRows
  }
}
```

然后**确定性地**喂入「连续多帧 = ink 的 eraseLines+output + park 的相对移动」，断言一个**几何不变量**：

```ts
// 不变量：每帧 ink 的 eraseLines 必须覆盖上一帧内容的顶行
// （否则擦错行 → ghost / 整框漂移）
const covered = erasedTop <= prevFrameTopRow && erasedBottom >= prevFrameTopRow
if (!covered) return { ok: false, detail }
```

**这套手段最强的地方——它能同时做「复现 bug」和「验证 fix」两件事**，用同一个模拟器跑不同策略：

```ts
// cursorParkModel.test.ts
test('REPRO current SCO-park races ink: eraseLines erases the WRONG rows', () => {
  const { ok } = simulateFrames('current-sco', 5, BODY, CARET_ROW, CARET_COL)
  assert.equal(ok, false)   // ← 锁定 bug 存在：旧策略必然违反不变量
})
test('FIX wrapper-relative: reset caret→bottom before each frame keeps invariant', () => {
  const { ok } = simulateFrames('wrapper-relative', 5, BODY, CARET_ROW, CARET_COL)
  assert.ok(ok)             // ← 证明 fix：新策略永远满足不变量
})
```

**为什么值得学：** 当 bug 的根因是「一串相对操作的累积效果」而不是「某个静态输出」时，**建一个能吃 ANSI、吐光标状态的迷你解释器**，就能把肉眼看不出、终端才暴露的时序/几何 bug 变成 5 分钟跑完的确定性断言。这也是本次能快速排除「park 算术」嫌疑、把注意力转向「可见性」的关键——算术被单测钉死了，就不必反复怀疑它。

> **陷阱提醒（本次踩过）：** 追踪器的 ANSI 解析器要跟上被测代码新引入的序列。本次 park 加了 `\x1b[?25h/l`（DECTCEM 显隐光标），追踪器的 `[0-9;]` 参数扫描不认 `?`，会把 `?25h` 误当内容污染光标——必须同步教它**跳过 DEC private-mode 序列**。**虚拟终端的保真度 = 它认识的转义序列集合；被测代码扩序列，模拟器要同步扩。**

---

## 2. 手段二：字节级 stdout 断言（测「写了什么控制序列、以什么顺序」）

**文件：** `stdoutCursorPark.test.ts`、`App.test.ts` 的光标测试、`terminalCursor.test.ts`、`insertHistory.test.ts`

**手段：** 不 strip ANSI，直接对 stdout 写出的原始字符串做**转义序列的正则匹配 + 顺序断言**。

**2.1 断言「写了正确的序列、没写错误的序列」：**

```ts
// App.test.ts：park 用纯相对移动，绝不能再出现绝对 CUP / SCO save
const combined = stdout.chunks.join('')
assert.match(combined, /\x1b\[4G/)              // 必须有：相对定列到 caret
assert.doesNotMatch(combined, /\x1b\[\d+;\d+H/) // 禁止：绝对 CUP
assert.doesNotMatch(combined, /\x1b\[s/)        // 禁止：SCO save
```

`doesNotMatch` 这类**负向断言**特别适合锁定「我们废弃的旧机制不许复活」——回归防线。

**2.2 断言「序列之间的相对顺序」（时序不变量）：**

用 `indexOf` / `lastIndexOf` 在 chunk 流里定位锚点，切片检查两个事件**之间**发生了什么：

```ts
// App.test.ts：下一帧 ink 擦除(\x1b[2K)之前，必须先 unpark(相对下移 \x1b[<n>B)
const parkedCaret = combined.lastIndexOf('\x1b[4G')
const nextInkErase = combined.indexOf('\x1b[2K', parkedCaret + 1)
const between = combined.slice(parkedCaret, nextInkErase)
assert.match(between, /\x1b\[\d+B/)   // park 与下次擦除之间，必有回帧底
assert.doesNotMatch(between, /\x1b\[u/) // 且不再用旧的 SCO restore
```

**2.3 字节级断言 = 契约锁定。** `stdoutCursorPark.test.ts` 对 writer 的每一步输出做**逐字节** deepEqual：

```ts
assert.deepEqual(out, ['clear', 'static', 'main-frame', '\x1b[2A\x1b[6G\x1b[?25h'])
//                                                        ↑ 上移2行 ↑定列 ↑SHOW
```

这样任何人改动 park 的输出序列，测试立刻红——**它把「光标可见性契约」这种口头约定变成了机器强制的契约**。本次修复正是靠更新这些字节级断言，明确记录了「park 后必 SHOW、unpark 前必 HIDE」的新契约。

**为什么值得学：** 终端 UI 的正确性**就是**写出的字节流。与其渲染到真终端再用眼睛看，不如直接断言字节流本身。正则匹配抓「写了什么」，`indexOf`+切片抓「顺序」，deepEqual 抓「精确契约」。

---

## 3. 手段三：内存终端 + App 级渲染（测「真实组件树布局出来的帧几何」）

**文件：** `App.test.ts`、`grok_user_input_duplicate.test.ts`、`workflowList.app.test.ts`

这是最接近「端到端」的手段：**用假的 stdin/stdout 驱动真实的 `<App/>`，捕获它写出的帧，解析成行，断言行列几何**——全程不进真终端。

**3.1 基础设施（三件套，可直接复制）：**

```ts
class CaptureWriteStream extends EventEmitter {   // 假 stdout：把每次 write 存进 chunks[]
  columns = 80; rows = 24; chunks: string[] = []
  write(chunk: unknown): boolean { this.chunks.push(String(chunk)); return true }
}
class FakeReadStream extends EventEmitter {        // 假 stdin：能 setRawMode + send() 模拟按键
  isTTY = true
  setRawMode() { return this } /* ...ref/unref/resume/pause 全 stub... */
  send(text: string) { this.queue.push(text); this.emit('readable') }
}
// 假 bridge：不连真后端，用回调手动 emit 协议事件
const startBridgeClient = (_p, _b, onEvent) => {
  setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
  return { send() {}, stop() {} }
}
render(React.createElement(App, { python, bridgeScript, startBridgeClient }), {
  stdout: stdout as any, stderr: stderr as any, stdin: stdin as any,
  patchConsole: false, debug: true,   // debug:true → ink 同步写帧，去掉 32ms 节流不确定性
})
```

**关键点：**
- `debug: true` 让 ink **同步**渲染（关掉 `throttle(onRender, 32)`），测试不必和节流定时器赛跑。
- `startBridgeClient` 注入假 bridge，**用回调手动 emit 协议事件**（`user` / `assistant_delta` / `status` / `history_replace`…），精确编排任意会话时序。
- `stdin.send('x')` 模拟真实按键，能测输入 → 重渲染 → 光标的完整闭环。

**3.2 帧解析工具（把 chunk 流还原成「一帧的多行文本」）：**

```ts
function stripAnsi(text)      // 去掉所有 ANSI，得到肉眼可读的纯文本帧
function completeFrames(...)  // 从 chunks 里筛出「真正的 live 帧」（含 composer/边框特征）
function inputBorderRows(frame): number[]  // 返回输入框上下边框(─────)所在的行号
function emptyLiveGapAboveChrome(frame): number  // composer 上方的空行数
```

**3.3 用几何断言复现「肉眼 bug」——三个实战例子：**

**(a) 输入框被 flex 压塌（`/help` 高面板 bug）：**
```ts
// 现象：/help 打开后输入框只剩两条挨着的边框，中间没有 ">" caret 行
const borders = inputBorderRows(frame)         // 两条边框行号
const caretRow = lines.findIndex((l, i) => i > borders[0]! && i < borders[1]! && l.includes('>'))
assert.ok(caretRow > borders[0]! && caretRow < borders[1]!, 'input caret row collapsed')
// ↑ caret 行必须夹在两条边框之间。塌陷时找不到 → 断言失败，精确复现肉眼所见。
```

**(b) composer 离内容太远 / 中间大片空白（布局过修 bug）：**
```ts
const idleGap = emptyLiveGapAboveChrome(idleFrame)
assert.ok(idleGap <= 1, `idle still has tall empty live gap: ${idleGap}`)
// ↑ 把「中间空了十几行」这个观感，量化成「chrome 上方空行数」的上界断言。
```

**(c) 流式输出时输入框「整框跳动」：**
```ts
for (const frame of streamingFrames) {
  const borders = inputBorderRows(frame)
  assert.equal(borders[1]! - borders[0]!, 2)   // 输入框自身高度恒为 2（框不会变形）
  assert.equal(frame.split('\n').every(l => stringWidth(l) < 40), true) // 不超宽
}
assert.ok(lastBorders[0]! >= midBorders[0]!)   // 边框行号单调下移（内容增多合理下推，不乱跳）
```

**为什么值得学：** `inputBorderRows` / `emptyLiveGapAboveChrome` 这类**帧几何解析器**是关键——它们把「帧的纯文本」翻译成「结构化的行号/间距数字」，于是「输入框塌了」「空太多」「在跳」全都变成**数字比较**。写这类小解析器的成本很低，回报是把整类布局 bug 纳入回归。

---

## 4. 手段四：纯函数化 UI 决策（把「布局/状态逻辑」从渲染里拆出来单测）

**文件：** `messageViewportPlan.test.ts`、`insertHistory.test.ts`、`layoutMetrics.test.ts`、`messagePartition.test.ts`、`inputLayout.test.ts`

**手段：** 凡是「决定 UI 长什么样」的逻辑，都抽成**输入 → 输出的纯函数**，脱离 React/ink 单测。渲染层只负责把纯函数的结果画出来。

**4.1 布局决策纯函数** —— `planMessageViewport({hasStatic, liveLineCount, messageRows, maxLiveRows})`：
```ts
// 「idle+有历史 → 不留满高空槽」这条产品规则，直接是一个纯函数断言
assert.deepEqual(planMessageViewport({ hasStaticMessages: true, liveLineCount: 0, messageRows: 18 }),
                 { kind: 'none' })
// 「流式增长到上限就封顶」（触底固定）
assert.deepEqual(planMessageViewport({ hasStaticMessages: true, liveLineCount: 40, messageRows: 18, maxLiveRows: 12 }),
                 { kind: 'live', height: 12 })
```

**4.2 视口滚动几何纯函数** —— `insertHistory.ts` 把 Codex「先下移、触底固定」的滚动模型做成纯状态机，用**多轮模拟 + 单调性断言**验证：
```ts
// advanceViewportForHistory 反复插历史，dock 的 y 单调下移直到贴底后冻结
const ys = [vp.areaY]
for (let turn = 0; turn < 20; turn++) { const r = advanceViewportForHistory(vp, 3); vp = r.state; ys.push(vp.areaY) }
assert.equal(isBottomAligned(vp), true)
for (let i = 1; i < ys.length; i++) assert.ok(ys[i]! >= ys[i-1]!)  // 单调不减
assert.equal(ys.at(-1), ys.at(-2))                                 // 触底后冻结
```

**为什么值得学：** 布局/滚动/分区逻辑一旦离开 React，就能用最快、最稳、零时序依赖的纯函数单测覆盖全部边界（终端太矮、内容溢出、历史触底…）。**渲染留给 App 级测试少量把关，绝大多数分支在纯函数层就穷尽了。** 这也让 bug 定位天然分层：先看纯函数测试是否覆盖，没覆盖就补，覆盖了还错就往渲染层查。

---

## 5. 手段五：显示宽度不变量（测 CJK / emoji / 组合字符的换行）

**文件：** `messageWindow.test.ts`、`App.test.ts`（soft-wrap 测试）

终端里「一个字符占几列」不等于 `.length`。中文占 2 列、emoji（含 ZWJ 👨‍💻）占 2 列、组合重音 `é` 占 1 列。换行算错就会**串行/超出画布右边界导致终端自己乱折行**——这是肉眼一眼能看到、但极易在代码里写错的一类 bug。

**手段：** 用 `string-width` 断言**显示宽度**，而非字符串长度：

```ts
// 换行结果的每一行显示宽度都不超过画布宽
assert.equal(wrapped.every(line => stringWidth(line.text) <= 4), true)
// 宽字符按显示宽度换行
assert.deepEqual(wrapTranscriptLines([{ id, text: '中文测试' }], 4).map(l => l.text), ['中文', '测试'])
// emoji / 组合字素不被从中间劈开
assert.deepEqual(wrapTranscriptLines([{ id, text: 'ab👨‍💻écd' }], 4).map(l => l.text), ['ab👨‍💻', 'écd'])
```

App 级也用它守「软换行绝不写进物理最后一列」（否则终端自动折行破坏帧几何）：
```ts
assert.equal(frame.split('\n').every(line => stringWidth(line) < columns), true)
```

**为什么值得学：** 涉及非 ASCII 文本的终端 UI，**所有宽度相关断言必须用 `string-width`，永远不要用 `.length`**。这是一条硬性纪律。

---

## 6. 手段六：数据分区/去重不变量（测「同一内容不会重复显示」）

**文件：** `messagePartition.test.ts`、`grok_user_input_duplicate.test.ts`、`streamCommit.test.ts`

**背景：** GA 的消息分两个区渲染——`<Static>`（进终端 scrollback，不可撤销）和 live（每帧重画）。**同一条消息若同时进两个区，用户就看到重复。** 这是纯数据层的不变量，根本不用渲染就能测。

**6.1 分区纯函数断言**（`splitStaticAndActiveMessages`）：
```ts
// 未完成的当前轮 user 只在 live；一旦 finalize 就只在 Static，绝不同时存在
assert.deepEqual(openSplit.activeMessages.map(m => m.id), ['u-2'])
assert.equal(doneSplit.activeMessages.length, 0)
```

**6.2 端到端「行级去重」断言**（`streamCommit.test.ts`）—— 跑完整个流式会话后，断言最终文本里**每一行只出现一次**：
```ts
for (const line of full.trim().split('\n')) {
  assert.equal(allAssistant.split('\n').filter(l => l === line).length, 1, `duplicated line ${line}`)
}
```

**6.3 App 级双通道计数**（`grok_user_input_duplicate.test.ts`）—— 用 `countOccurrences` 在实际 stdout 里数某段唯一探针文本出现的次数，配合「区分 live 帧 vs static chunk」的过滤器，断言 running 中 user 不过早进 Static：
```ts
const staticChunks = staticChunksWithText(plain, UNIQUE_USER_TEXT)  // 非 live-chrome 的 chunk
const liveWithUser = liveFramesWithText(plain, UNIQUE_USER_TEXT)
assert.equal(staticChunks.length, 0, 'running 中 user 不得过早进 Static')
assert.ok(liveWithUser.length > 0, 'running 中 live 必须可见本轮 user')
```

**为什么值得学：** 「重复显示」是终端 UI 高频 bug（尤其 Static + live 双区架构）。**用唯一探针文本（`核验Running可见-user-probe-9c2e` 这种不会误撞的字符串）+ 计数断言**，能把它稳稳钉住。探针文本要足够独特，避免和 chrome/其它内容碰撞。

---

## 7. 手段七：确定性时序控制（把异步/microtask 变同步可测）

**文件：** `stdoutCursorPark.test.ts`（manualScheduler）、各 App 测试的 `debug:true`

终端 UI 充满异步：ink 的 32ms 节流、park 的 microtask、bridge 事件。**不控制时序，测试就得靠 `sleep` 赛跑，既慢又 flaky。** 两个去异步化手段：

**7.1 可注入的调度器** —— park writer 的 microtask 调度做成构造参数，测试传一个「手动 flush」的假调度器：
```ts
function manualScheduler() {
  const queue: Array<() => void> = []
  return { schedule: (cb) => queue.push(cb), flush: () => queue.splice(0).forEach(cb => cb()) }
}
const w = new CursorParkWriter((c) => out.push(c), schedule)
w.write('frame'); assert.equal(w.parkedUp, null)  // 同步阶段：还没 park
flush();          assert.equal(w.parkedUp, 2)      // 手动触发 microtask：park 发生
```
这样能精确断言「一轮 onRender 的多次同步写入之后，只 park 一次」这种时序契约。

**7.2 `debug:true` 关掉 ink 节流** —— App 测试里让渲染同步发生，配合 `waitForFrame` 轮询（带 2s 超时兜底）等待目标帧，而不是猜一个 `sleep` 时长。

**为什么值得学：** **把「什么时候执行」做成可注入依赖**（调度器、时钟），是让异步 UI 逻辑变成确定性单测的通用招法。能同步就别赛跑。

---

## 8. 快速决策表：遇到一个 UI bug，该用哪种手段？

| 你观察到的现象 | 首选手段 | 参考文件 |
|---|---|---|
| 光标/IME 位置错，或怀疑相对光标算术 | ①虚拟终端追踪器 + ②字节级断言 | `cursorParkModel.test.ts`、`stdoutCursorPark.test.ts` |
| 写了不该写的控制序列 / 顺序错 | ②字节级 stdout 断言（正则 + indexOf 切片 + 负向断言） | `App.test.ts` 光标测试 |
| 输入框塌陷 / 位置跳 / 空白过多 | ③内存终端 + 帧几何解析器 | `App.test.ts` |
| 布局规则（何时留槽、多高、触底） | ④纯函数化 + 边界穷举 | `messageViewportPlan.test.ts`、`insertHistory.test.ts` |
| CJK/emoji 换行错乱、超宽 | ⑤`string-width` 宽度不变量 | `messageWindow.test.ts` |
| 消息重复 / 双显 / 该进 Static 没进 | ⑥数据分区 + 唯一探针计数 | `messagePartition.test.ts`、`grok_user_input_duplicate.test.ts` |
| 异步/节流导致 flaky | ⑦可注入调度器 + `debug:true` | `stdoutCursorPark.test.ts` |
| 颜色/样式不对 | 结构化 parts 断言（part.color/bold） | `messageWindow.test.ts` transcriptLines 系列 |

---

## 9. 通用纪律（写 Ink UI 测试的硬规则）

1. **降维优先。** 先问「这个肉眼现象背后是哪一层的确定性事实」，把它测在最低的那一层（纯函数 > 字节流 > 帧几何 > 真终端）。真终端截图只用于发现新现象，不用于回归。
2. **能复现才算定位。** 修 bug 前，先写一个**会失败**的测试复现它（如 `simulateFrames('current-sco')` 断言 `ok===false`）。修完这个测试转绿，且旧策略的复现测试仍锁定问题存在。
3. **正向 + 负向都要断言。** 「必须写 X」用 `assert.match`；「绝不能再写废弃的 Y」用 `assert.doesNotMatch`。后者是防旧 bug 复活的回归防线。
4. **宽度一律 `string-width`，长度一律不信 `.length`。**（非 ASCII 场景）
5. **唯一探针文本。** 计数/存在性断言用不会误撞的独特字符串（带随机后缀），别用 `'hi'` 这种会和 chrome 碰撞的词。
6. **去异步化。** 调度器可注入、`debug:true` 关节流、`waitForFrame` 带超时兜底——不要 `sleep` 赛跑。
7. **模拟器要跟上被测代码。** 虚拟终端追踪器认识的 ANSI 序列集合 = 它的保真度。被测代码引入新序列（如本次的 `\x1b[?25h/l`），追踪器必须同步支持，否则测试会假绿或误红。
8. **帧几何解析器是投资。** `inputBorderRows` / `emptyLiveGapAboveChrome` 这类小工具写一次，之后整类布局 bug 都能纳入回归——值得为每个「结构化几何特征」写一个解析器。

---

## 10. 局限（诚实标注：这套手段测不到什么）

- **测不到真实终端的渲染差异。** 我们模拟的是「写出的字节」和「布局的几何」，但**不同终端对同一串字节的实际显示**（IME 候选框锚点、DECTCEM 行为、字体宽度表差异）测不到。本次 IME bug 的根因「Windows Terminal 的 IME 锚定可见原生光标」——就是**这套单测覆盖不到、只能靠真机截图发现**的那一类（详见 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`）。
- **结论：** 单测负责把「字节流正确、几何正确、逻辑正确」钉死，覆盖绝大多数回归；**真机截图负责发现「字节正确但终端表现异常」这一小类跨系统行为 bug。** 两者互补，不可偏废。发现新的跨系统现象后，仍应尽量把它降维（如本次把「可见性」变量补进字节级契约测试）。

---

## 11. 涉及文件索引

| 文件 | 承载的手段 |
|---|---|
| `cursorParkModel.ts` / `.test.ts` | ①虚拟终端追踪器 + 几何不变量 + 复现/修复对照 |
| `stdoutCursorPark.test.ts` | ②字节级契约 deepEqual + ⑦可注入调度器 |
| `App.test.ts` | ②字节序列顺序断言 + ③内存终端帧几何 + ⑤宽度不变量 |
| `grok_user_input_duplicate.test.ts` | ③内存终端 + ⑥唯一探针计数 + live/static 帧区分 |
| `streamCommit.test.ts` | ⑥端到端行级去重 + fence 平衡不变量 |
| `messageViewportPlan.test.ts` | ④布局决策纯函数 |
| `insertHistory.test.ts` | ④视口滚动状态机 + 单调性模拟 |
| `messageWindow.test.ts` | ⑤宽度换行 + 结构化 parts 颜色断言 |
| `messagePartition.test.ts` | ⑥分区不变量 |
| `terminalCursor.test.ts` | ②光标坐标纯函数字节断言 |
