# GA Ink UI 中文输入法候选框漂移 —— 最终根因分析与排查纪实

**日期：** 2026-07-16
**状态：** ✅ 已修复并验证（305 tests + typecheck PASS，真机效果优于原生 Claude Code CLI）
**核心文件：** `frontends/ink-ui/src/stdoutCursorPark.ts`
**参考实现：** Claude Code 的 ink fork（`D:\git_codes\claude-reviews-claude\claude-code-fork\src\ink`）
**本文定位：** 这是该 UI bug 的**权威结论文档**。此前 2026-07-14 / 07-15 的多份诊断把根因判错，其结论已在 §8 逐条修正。作为团队排查典范阅读时，请以本文为准。

---

## 0. 结论先行（一句话根因）

> **旧设计把「可见光标」和「原生光标」拆成了两个东西：可见性交给自绘的反显块，原生光标全程隐藏。但 Windows Terminal 的输入法候选框锚定的是「可见的原生光标 cell」——原生光标一旦隐藏，候选框就失去锚点、漂到屏幕右下角。**

- 英文正常，是因为反显块是 GA 自绘的，看起来「有光标」；
- 中文合成异常，是因为输入法要问系统「原生光标在哪」，而它被隐藏了 → 候选框漂走。
- park 的**相对移动算术从头到尾都是对的**——光标每帧都精确停在了 caret cell，只是**不可见**，所以输入法看不到它。

修复 = 让原生光标在「已停到 caret」时**可见（SHOW）**，在「写帧/回帧底」时**隐藏（HIDE）**。这正是 Claude Code 的做法（`frame.cursor.visible: true` + 帧末 `cursorShow`）。

---

## 1. 症状（三张截图的对照）

| 场景 | 现象 | 原生光标状态 | 谁在提供「可见光标」 |
|---|---|---|---|
| GA 中文合成 | `>` 后为空，`ni'shi'shui` 候选框钉在**屏幕右下角** | 隐藏 | 无（反显块此刻还没字符）→ 输入法无锚点 |
| GA 英文输入 | `who are you` + 反显块光标，位置正确 | 隐藏 | 反显块（GA 自绘）✅ |
| Claude Code | `hello`，光标紧贴 caret | **可见** | 原生光标 ✅ |

**关键观察（这是解题的钥匙）：** 英文「正常」是假象——它正常不是因为原生光标对，而是因为反显块顶替了可见光标的职责。一旦引入「需要系统级锚点」的输入法候选框，反显块顶替不了，真实的缺陷才暴露：**原生光标全程隐藏**。

---

## 2. 背景：路径 A 的 stdout 包裹与 cursor park

GA Ink UI 用上游 npm `ink@5.2.1`（**不是** Claude Code 的 fork）。ink 每帧通过 `log-update` 写：

```
eraseLines(prevN) + output + '\n'
```

`eraseLines` 从**当前原生光标位置**往上擦 N 行，因此 ink 假设「每帧写完，光标停在帧底」（= 最后一行内容的下一行、col 0），下一帧从那里往上擦。

为了让输入法/可见光标贴到 composer 里的 caret，GA 采用**路径 A**（见 `docs/ga_claude_code_cursor_handling_2026-07-16.md` §5.1）：用一个包裹 `process.stdout` 的 writer（`stdoutCursorPark.ts`），把「内容写入」和「caret 定位」串成同一 writer 的两步：

- **UNPARK**（每次写入前）：用相对移动把光标从 caret 移回帧底，保证 ink 的 `eraseLines` 起点正确；
- **写内容**（ink 的帧）；
- **PARK**（microtask，本轮 onRender 的最后一次同步写入之后）：用相对移动 `ESC[<up>A` + `ESC[<col+1>G` 把光标从帧底上移到 caret。

这套机制的**几何正确性**由 `cursorParkModel.test.ts` 的确定性虚拟终端模拟验证——连续多帧下 `eraseLines` 始终从帧底往上擦，无 ghost、无整框漂移。**这部分一直是对的，本次修复一行没动它的算术。**

---

## 3. 排查过程（如何从「时序竞态」的旧假设里跳出来）

### 3.1 先证伪旧假设

历史文档（07-14、07-15）反复把根因归到 **「stdout 所有权 / 双 writer 竞态」**：Ink 的 `eraseLines` 与 App 层旁路写的绝对 CUP 打架 → ghost composer / 整框晃 / IME 偏移。基于此得出的**强制结论**是：

> 「原生光标由 Ink `cli-cursor.hide()` 隐藏；**不要再 park 原生光标去对齐 IME**。可见光标只用 inverse caret。」——`ga_ui_ime_cursor_park_timing_fix_2026-07-15.md`

路径 A 落地后，「双 writer 竞态」这个根因**已经被消除**了：不再有旁路 CUP，所有写入（含 caret 定位）都收口到同一个 writer，且 park 的几何被单测证明正确。**但中文候选框依然漂到右下角。** 这说明：**「竞态」不是 IME 漂移的根因**——它只是 ghost/晃动的根因。两个症状被历史文档错误地合并成了一个。

### 3.2 定位到「可见性」这个被忽略的变量

既然 park 几何正确（光标停在了 caret），那 IME 为什么找不到它？回到第一性问题:**输入法候选框到底锚定在哪个「光标」上?**

- 终端的输入法（IME）候选框由**终端**根据 DECTCEM 状态下的**可见原生光标**定位；
- GA 从 ink 继承了 `cli-cursor.hide()`，并在 `stdoutCursorPark.ts` 的文件头注释里**明确写下了错误前提**：
  > 「原生光标保持隐藏……IME 跟随隐藏的原生光标所在 cell。」

这句话是整个 bug 的源头——它假设「隐藏的光标仍能锚定 IME」。在 Windows Terminal 上这是**假的**。

### 3.3 用参考实现交叉验证

读 Claude Code 的 ink fork 源码，逐条确认它对光标可见性的处理：

- `src/ink/frame.ts:32`：`cursor: { x: 0, y: 0, visible: true }` —— frame 默认光标**可见**；
- `src/ink/ink.tsx:996`：inline/主屏 blank frame 也是 `visible: true`；
- `src/ink/log-update.ts:118`：帧末 `return [{ type: 'cursorShow' }]`；
- `src/ink/termio` → `cursorShow` 落为 `SHOW_CURSOR`（`\x1b[?25h`）。

**结论：Claude Code 在主屏/inline 模式全程让原生光标可见并停在 caret，输入法据此锚定。** GA 与它的唯一实质差异，就是「原生光标可见 vs 隐藏」。这一条差异**完全解释**了症状矩阵（§1）。

至此根因确证:**不是时序、不是坐标、不是所有权——是原生光标的「可见性」。**

---

## 4. 根因（精确表述）

**旧设计的前提错误：**「输入法跟随隐藏的原生光标 cell」。

真相：**Windows Terminal 的 IME 候选框锚定在 DECTCEM 置位（`\x1b[?25h`）的可见原生光标 cell 上。** 原生光标被 `\x1b[?25l` 隐藏时，输入法失去锚点，候选框回退到终端默认位置（右下角）。

GA 旧实现把「可见光标」的职责整个交给了 `InputView` 自绘的反显块（`renderInputLine` 的 `inverse` 段），让原生光标全程隐藏。于是：

- **英文**：反显块顶替了可见光标，肉眼看正常；输入法此时不介入，缺陷不暴露。
- **中文合成**：输入法候选框必须锚到系统级的可见原生光标——它是隐藏的——于是漂到右下角。

park 的相对算术无辜：它每帧都把（隐藏的）原生光标精确停到了 caret cell。**缺的不是位置，是可见性。**

---

## 5. 修复

在 `stdoutCursorPark.ts` 中，让原生光标可见性与「是否已停在 caret」严格同步：

```ts
const SHOW_CURSOR = '\x1b[?25h'   // DECTCEM set   —— 输入法据此锚定
const HIDE_CURSOR = '\x1b[?25l'   // DECTCEM reset

// PARK（microtask，ink 帧写完之后）：上移到 caret 行、定列，然后 SHOW
this.sink(`${up}\x1b[${spec.col + 1}G${SHOW_CURSOR}`)

// UNPARK（下一次写入前）：先 HIDE，再下移回帧底
this.sink(this.parkedUp > 0 ? `${HIDE_CURSOR}\x1b[${this.parkedUp}B\r` : `${HIDE_CURSOR}\r`)
```

不变量：**原生光标只在「已停在 caret」时可见；在帧底、在写入过程中一律隐藏，因此不会闪现或在屏上滑动。** 这与 Claude Code 的「frame 声明 cursor.visible + 帧末统一定位并 show」同构，只是消费点从 ink 内部换到了 stdout 包裹层（路径 A 的既定架构）。

### 5.1 配套改动

| 文件 | 改动 | 原因 |
|---|---|---|
| `stdoutCursorPark.ts` | park 后 SHOW、unpark 前 HIDE；重写文件头注释（记录正确前提） | 主修复 |
| `cursorParkModel.ts` | `CursorTracker.feed` 跳过 DEC private-mode 序列（`\x1b[?…h/l`） | 否则端到端测试会把 `?25h` 的 `25` 误当内容，污染光标追踪 |
| `stdoutCursorPark.test.ts` | 字节级断言补上 HIDE/SHOW 码 | 锁定新的可见性契约 |

### 5.2 反显块的去留

`InputView` 的反显块**保留**，作为 `GA_UI_CURSOR_PARK=off` 回退路径下的唯一可见 caret。park 生效时，可见原生光标与反显块落在**同一个** caret cell 上、二者重合，不会显示成两个光标。

> **待真机目视确认的一点：** 若在极端主题/终端下二者重合观感不佳，最干净的后续做法是 park 生效时抑制反显块（完全对齐 Claude Code——它不画反显块，只靠原生光标）。当前保留是出于回退安全。

---

## 6. 验证

```bash
cd frontends/ink-ui
npm run test        # 305 pass / 0 fail
npm run typecheck   # clean
```

人工（Windows Terminal，`ga ink`）：

1. 中文输入法合成拼音 → 候选框**紧贴 `>` 后的 caret**，不再漂右下角；
2. 英文输入 → caret 位置正确，无回退；
3. 连续输入 / 删除 / 换行 → 无 ghost composer、无整框左右晃（路径 A 的既有保证）。

真机效果经用户确认**优于原生 Claude Code CLI**。

---

## 7. 方法论沉淀（给同事的排查心法）

1. **一个「正常」的对照组可能是假象。** 英文「正常」误导了三轮排查——它正常是因为反显块顶替了可见光标，而不是因为光标机制对。**当两个场景一个正常一个不正常，先问「这两个场景到底走了不同的哪条代码/系统路径」**，而不是假设它们共享同一个待修的机制。

2. **症状合并是根因误判的头号来源。** 「IME 漂移」和「ghost/整框晃」被早期文档当成同一个根因（stdout 所有权竞态）。实际上是两个独立根因：竞态导致 ghost/晃动；可见性导致 IME 漂移。**路径 A 修好了前者，却没碰后者**——因为它们从来不是一回事。修完一个症状没消失，要警惕「其实有两个 bug」。

3. **把「显然为真」的注释当嫌疑人。** 根因就写在 `stdoutCursorPark.ts` 的文件头注释里：「IME 跟随隐藏的原生光标所在 cell」。它以陈述句伪装成事实，从未被验证。**代码注释里的因果断言，尤其是关于外部系统（终端/输入法/OS）行为的，都应视为待验证假设。**

4. **参考实现是「差异定位器」，不是「抄袭源」。** 不需要移植 Claude Code 的整个 ink fork。只需逐条对比它和 GA 在**可疑维度**上的差异——最终锁定到「一行 `visible: true`」。当你有一个已知正确的对照实现时，排查从「猜机制」变成「找差异」。

5. **让算术正确性可被单测证明，就能在排查时把它排除。** `cursorParkModel.test.ts` 的确定性虚拟终端模拟，让我能在 5 分钟内确信「几何是对的」，从而把注意力从坐标/时序转向可见性。**能被单测钉死的部分，排查时可以快速排除，不必反复怀疑。**

---

## 8. 对旧文档结论的修正（重要）

以下历史文档的部分结论**基于错误根因**，现予修正。这些文档保留作为「失败尝试」的历史记录，但结论以本文为准：

### 8.1 `ga_ui_ime_cursor_park_timing_fix_2026-07-15.md`

| 旧结论 | 修正 |
|---|---|
| 「原生光标由 Ink `cli-cursor.hide()` 隐藏；**不要再 park 原生光标去对齐 IME**。」 | **错误。** 恰恰相反：必须 park 原生光标到 caret **并让它可见**，否则 IME 无锚点。旧结论把「隐藏光标」当成了正确终态，直接导致 IME bug 长期无解。 |
| 「可见光标只用 inverse caret。」 | **不完整。** inverse caret 只能骗过肉眼，骗不过需要系统级锚点的输入法。可见光标必须由**可见的原生光标**提供（inverse 块仅作回退）。 |
| 「要 Codex/CC 级 IME 贴合，必须①自管 terminal owner 或②与 Ink 官方 cursor API 协调。」 | **过度悲观。** 第三条路成立且已落地：**用包裹 stdout 的 writer 在帧末相对定位光标并 SHOW**（路径 A），无需自管 terminal，也无需 ink 提供 cursor API。 |

### 8.2 `ga_ui_absolute_cup_ghost_composer_rollback_2026-07-14.md` / `ga_ui_ime_cursor_absolute_park_fix_2026-07-14.md`

这两份对 **ghost composer / 整框晃** 的根因判断（绝对 CUP 与 ink `eraseLines` 打架 = stdout 所有权竞态）**是对的**，路径 A 正是据此消除了竞态。**需修正的是其隐含假设**：它们把「IME 漂移」也归到同一根因，认为回滚绝对 CUP、隐藏原生光标即可。实际上 IME 漂移是**独立的可见性根因**，回滚竞态并不能修复它——这也是为什么路径 A 落地后 IME 仍漂，直到本次补上 SHOW/HIDE。

### 8.3 `stdoutCursorPark.ts` 原文件头注释

原注释「原生光标保持隐藏……IME 跟随隐藏的原生光标所在 cell」**已删除并改写**，新注释记录了正确前提（IME 锚定可见原生光标）与 SHOW/HIDE 时序。

---

## 9. 分析边界（诚实标注）

- 「Windows Terminal 的 IME 候选框锚定可见原生光标」经真机验证成立；**未在** Windows Console (conhost)、其它终端（WezTerm/Alacritty/iTerm 等）逐一验证。若某终端表现异常，可用 `GA_UI_CURSOR_PARK=off` 回退到纯反显块。
- 可见原生光标与反显块重合的观感为逻辑推断 + 用户初步确认；**未在**所有主题/终端下逐一目视（见 §5.2 待确认项）。
- 修复只改「原生光标何时可见」，未改 park 的几何算术（后者由 `cursorParkModel.test.ts` 持续保证）。

---

## 10. 涉及文件

| 文件 | 角色 |
|---|---|
| `frontends/ink-ui/src/stdoutCursorPark.ts` | **主修复**：park SHOW / unpark HIDE + 正确前提注释 |
| `frontends/ink-ui/src/cursorParkModel.ts` | `CursorTracker` 跳过 DEC private-mode 序列 |
| `frontends/ink-ui/src/stdoutCursorPark.test.ts` | 字节级契约测试（含 HIDE/SHOW） |
| `frontends/ink-ui/src/promptChrome.ts` | 反显块 caret（`renderInputLine`，回退路径） |
| `frontends/ink-ui/src/App.tsx:1132-1141` | 每帧把 caret 相对帧底位置写入 `cursorPark.setPark` |
| `docs/ga_claude_code_cursor_handling_2026-07-16.md` | 路径 A 架构与 CC 参考分析 |
| CC `src/ink/frame.ts:32` / `log-update.ts:118` / `ink.tsx:996` | 「cursor.visible + 帧末 cursorShow」权威参考 |
