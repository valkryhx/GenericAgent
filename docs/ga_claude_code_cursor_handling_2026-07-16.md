# Claude Code 光标处理分析 — 对 GA Ink UI 的参考

**日期：** 2026-07-16
**参考源码：** `D:\git_codes\claude-reviews-claude\claude-code-fork\src\ink`
**相关文档：**
- `docs/superpowers/specs/2026-07-15-ga-self-managed-terminal-design.md`（自管 TerminalOwner 方案）
- `docs/ga_ui_absolute_cup_ghost_composer_rollback_2026-07-14.md`（GA 绝对 CUP 失败实证）

---

> **补记（2026-07-16 晚，落地后）：** 本文正确定位了「路径 A：wrapped stdout 收口 + 相对 park」，并已落地。但本文（及路径 A 描述）通篇沿用了一个**未言明的错误前提**——「原生光标保持隐藏，靠 inverse caret 提供可见光标」。落地后发现这正是中文 IME 漂移的真正根因：**Windows Terminal 的 IME 候选框锚定在「可见」的原生光标 cell 上**，隐藏光标无法锚定。CC 的 `frame.cursor.visible: true` 不只是「顺带可见」，而是 IME 贴合的**必要条件**。完整根因分析见 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`。本文 §2.4 引用的 CC `cursor.visible` 字段当时被当作「可见性无关紧要」，实为关键。

## 0. 结论先行

1. **Claude Code（CC）也用 React + Ink，但用的是一份完全自包含、fork 重写的 Ink**，位于 `src/ink`，**0 处 import npm `ink`**。它只依赖底层库：`react`、`react-reconciler`、`scheduler`、`yoga-layout`、`chalk`、`strip-ansi` 等。
2. **GA 用的是 npm 上游 `ink@^5.0.1`，不是 CC 的 fork。** 因此 **无法直接 import CC 的组件或 hook** —— 这是关键约束，也是用户点出的核心问题。
3. **CC 的光标处理 = Codex "frame 声明 cursor + 单一 writer + cursor 最后写" 模型的 Ink 实现。** 它证明了这套模型**可以在 Ink 内部实现，不必弃用 Ink**（不同于我们 2026-07-15 方案里假设的"退役 Ink 主路径"）。
4. **落地路径有三条**（见 §5），从低成本到高成本：借鉴思路打补丁 / 移植 CC 的 ink fork / 自建 TerminalOwner。CC 的存在让"移植成熟 fork"成为比自建更省的现实选项，但移植也有真实成本（见 §5.2 风险）。

---

## 1. GA 当前 bug 的根因（复述）

GA live 区有**两套 stdout writer**：
- Ink（npm `ink@5`）用 `log-update` 的 `eraseLines` 从**当前原生光标位置**往回擦、重画整帧。
- App 层在 `useLayoutEffect` 里**旁路** `stdout.write(CUP/DECSTBM)` 定位光标 —— 第二套 writer。

两者冲突 → 原生光标不在真实 caret：IME 候选框偏右、打字整框左右晃、延迟绝对 CUP 产生 ghost composer。三次在 Ink path 上修 CUP 时序均失败。

---

## 2. CC 的解法：frame 声明 cursor（源码实证）

### 2.1 组件只"声明"相对坐标，绝不碰 stdout

`src/ink/components/CursorDeclarationContext.ts`：
```ts
type CursorDeclaration = {
  relativeX: number   // 节点内显示列（终端 cell 宽度，非 byte offset）
  relativeY: number   // 节点内行号
  node: DOMElement    // 提供 yoga 布局绝对原点的 ink-box 节点
}
```
声明是**相对某个 DOM 节点**的 (line, column)，不是绝对屏幕坐标。绝对坐标在渲染阶段由该节点的 yoga rect 加相对偏移算出。

`src/ink/hooks/use-declared-cursor.ts`：`useDeclaredCursor({ line, column, active })` 返回一个 ref callback 挂到输入 Box 上，在 `useLayoutEffect`（**无 deps**）里：
- `active && node` → `setCursorDeclaration({ relativeX: column, relativeY: line, node })`
- 否则 → `setCursorDeclaration(null, node)`（**条件清除**：只在当前声明 node 匹配时才清）

源码注释点明两个必须照抄的细节：
- **无 deps**：每次 commit 都重新声明，让活跃实例能在别的实例 unmount-cleanup 或兄弟节点焦点转移把声明置 null 后重新夺回。
- **条件清除**：解决两个竞态 —— (1) memo 化的活跃实例本次没重渲染时，不活跃实例不能清掉它的声明；(2) 兄弟节点焦点转移时，新失活项的 effect 在新激活项 set 之后跑，没有 node 检查会误清。

### 2.2 声明存进 ref，不进 state

CC 在 `ink.tsx` 根部提供 `setCursorDeclaration`，写进一个 **ref**（非 state）。原因：声明变化不该触发额外 React 重渲染，它只在 onRender 时被读。

### 2.3 时序：queueMicrotask 保证读到 fresh 声明

`reconciler` 的 `resetAfterCommit` → `scheduleRender` 用 **`queueMicrotask`** 延迟 `onRender`，使 onRender 在**所有 layout effects commit 之后**跑，第一帧就能读到 `useDeclaredCursor` 刚写的声明 —— **无 one-keystroke 延迟**。测试环境用 `onImmediateRender`（同步），测试需显式调 `ink.onRender()`。

### 2.4 onRender 把声明解析为绝对坐标，塞进 Frame

`src/ink/frame.ts`：
```ts
type Frame = {
  screen: Screen
  viewport: Size
  cursor: Cursor            // { x, y, visible } —— 最终绝对光标
  scrollHint?: ScrollHint | null
}
```
onRender：DOM → yoga 布局 → screen buffer；从 `cursorDeclarationRef` 取声明；用 node 的 nodeCache rect 得到绝对原点；`cursor = { x: rect.x + relativeX, y: rect.y + relativeY, visible: true }`。**cursor 只是 frame 的一个声明字段，onRender 里不写任何 CUP。**

### 2.5 log-update 是唯一 stdout writer，cursor 最后写（硬顺序）

`src/ink/log-update.ts` 的 `render(prev, next)`（**已逐行读实，非推断**）：
1. screen diff → `Patch[]`（含内容/clear/style patch）
2. 渲染内容 slice
3. **末尾（第 414–451 行）才恢复 cursor**：
   - alt-screen：no-op（下帧以 CSI H 归零）
   - `next.cursor.y >= screen.height`：CR + 若干 NEWLINE 造出目标行（光标移动无法造新行）
   - 否则：`moveCursorTo(screen, next.cursor.x, next.cursor.y)`
   - 全部用**相对操作**（CR + 相对 dy），每帧**一次**最终定位

源码注释原文（log-update.ts:414）：*"Restore cursor. ... Main screen: if cursor needs to be past the last line of content ... emit \n to create that line since cursor movement can't create new lines."*

**关键不变量：cursorMove 一定在所有内容 patch 之后，每帧只有一次最终定位。** 原生光标落到 caret，IME/屏幕阅读器跟随。绝不在内容 patch 之间插 CUP —— 这正是 GA 双写打架的反面。

---

## 3. CC ↔ Codex ↔ GA 映射

| Codex (Rust ratatui) | Claude Code (fork Ink) | GA 现状 |
|---|---|---|
| `Frame::set_cursor_position`（只记录） | `useDeclaredCursor` → `cursorDeclarationRef`（只记录） | App `useLayoutEffect` 直接 `stdout.write(CUP)` ✗ |
| `try_draw`：先 flush 帧再 set cursor | `log-update.render`：内容 diff 后末尾 restore cursor | 无此顺序，两套 writer 竞争 ✗ |
| 唯一 terminal owner | `log-update` 是唯一 stdout writer | Ink + App 双 writer ✗ |
| cursor = area.x/y + 相对偏移 | cursor = nodeRect.x/y + relativeX/Y | 固定几何公式算绝对行列 △ |
| sync update 内串行 draw | onRender via queueMicrotask，layout effect 后 | 依赖 React 重渲染 + 旁路 CUP ✗ |

---

## 4. 依赖闭包核实（能否移植的前提）

对 `src/ink` 全目录 import 扫描结果：
- **npm `ink`：0 处** —— 完全不依赖上游 ink。
- 第三方：`react`、`react-reconciler`、`scheduler`、`yoga-layout`、`chalk`、`strip-ansi`、`wrap-ansi`、`get-east-asian-width`、`emoji-regex`、`cli-boxes`、`bidi-js`、`signal-exit`、`type-fest`、`lodash-es`、`semver`、`usehooks-ts`、`stack-utils`、`code-excerpt`、`supports-hyperlinks` 等（均为可 npm 安装的标准库）。
- **逃逸到 CC 业务层的 import 仅 4 处**：
  - `src/utils/debug.js`
  - `src/utils/log.js`
  - `src/bootstrap/state.js`
  - `src/native-ts/yoga-layout/index.js`（CC 自己的 yoga 封装）

**结论：`src/ink` 是一份自包含的 Ink 完整重写**，移植时只需替换/桩掉那 4 个业务依赖。规模：`ink.tsx` 1722 行、`log-update.ts` 773 行、`reconciler.ts` 512 行、`screen.ts` 1486 行、`render-node-to-output.ts` 1462 行等，`src/ink` 全目录约数千行 —— 移植是**真实工程量**，非拷贝即用。

---

## 5. GA 的三条落地路径

### 5.1 路径 A：不改 ink 源码，用 wrapped stdout 收口 cursor（已验证可行，用户选定）

**先验证 npm ink@5 的实际机制（已逐行读实 `node_modules/ink/build/ink.js` + `log-update.js`）：**

1. `Ink.onRender`（`throttle(this.onRender, 32)`）是**唯一**把帧写进 stdout 的地方，经 `log-update` 完成。
2. `log-update.render(str)`：一开始 `cliCursor.hide()`（**原生光标默认隐藏**），然后 `stream.write(eraseLines(prevN) + output + '\n')` —— `eraseLines` **从当前光标位置往上擦 N 行**，即它**假设上一帧结束时光标停在 output 末尾**。
3. `<Static>` 走 `this.log.clear()` + 直接 `stdout.write(staticOutput)`，不经 throttle。

**关键结论：上游 ink@5 没有任何 "cursor declaration / 帧末尾写 cursor" 的 hook** —— 无法照抄 CC 的 `useDeclaredCursor` 注入 ink 内部。**但 ink 暴露了一个合法注入点：`render(node, { stdout })` 接受自定义 stdout。** 这就是不改 ink 源码的路径 A 落点。

**GA 当前 bug 的精确机制**（结合上面）：
- log-update 每帧假设光标在 output 末尾并 `hide()` 了它；
- GA 在 `App.tsx:1131-1142` 的 `useLayoutEffect` 里**旁路** `stdout.write(save + CUP(caret))` 把光标挪到框内 caret；
- 这个 useLayoutEffect 在 React commit 后**同步**跑，而 ink 的 `onRender` 被 `throttle(32ms)` **异步**跑 —— 两者时序不确定；
- 一旦光标停在 caret（output 中间）而非末尾，下一帧 log-update 的 `eraseLines(prevN)` 就**从 caret 往上擦** → 错位、ghost、整框晃。

**路径 A 的正解：把 cursor park 从 useLayoutEffect 移进 wrapped stdout，与 ink 的每次 write 严格串行。**

```
wrappedStdout.write(chunk):
  if parkedAtCaret:               # 上一帧末尾我们把光标移到了 caret
    realWrite(restoreToFrameEnd)  # 先恢复到 ink 期望的 output 末尾（ESC 8 / 记录的末尾）
    parkedAtCaret = false
  realWrite(chunk)                # ink 的 eraseLines+output，此刻光标在正确末尾
  if 本次是主帧(非 Static) 且 有 caret 目标:
    realWrite(saveFrameEnd + cursorShow + CUP(caretTarget))  # cursor 最后写、每帧一次
    parkedAtCaret = true
```

- caret 目标由 App 每次渲染写进一个共享 ref（模块级/闭包），wrapped stdout 在 write 时读取 —— 相当于 CC 的 `cursorDeclarationRef`，只是消费点从 ink 内部换到 stdout wrapper。
- **单一 writer**：App 不再直接 `stdout.write`（除退出 cleanup），全部经 wrapper。
- **cursor 最后写、每帧一次**：紧贴 ink 的帧内容 write 之后，天然满足 CC 的硬顺序，且与 ink 的 throttle 节奏同步（因为是被 ink 的 write 触发，不是独立 effect）。
- **删除** `App.tsx:1131-1142` 的旁路 CUP useLayoutEffect。

**路径 A 待实测的风险（诚实标注，落地前必须验证）：**
1. **save/restore 跨 write 的可靠性**：GA 之前在 useLayoutEffect 里用 `ESC 7/8` 失败过。移进 wrapper 后是否可靠，取决于 ink 两次 write 之间没有别的写入破坏保存槽 —— wrapper 收口后应满足，但需实测。若终端 DECSC 单槽不可靠，改用"记录 output 行数 + 相对 CUP 回末尾"替代 save/restore。
2. **`<Static>` 写入**：ink 对 Static 单独 `stdout.write`，wrapper 需识别并**不**在 Static 写入后 park caret（Static 是滚动历史，不含 composer）。
3. **throttle 合并**：ink 32ms 节流可能合并多帧，wrapper 每次 write 都按"帧"处理即可，caret ref 取最新值。
4. **width/行数计算**：CUP 目标行列仍用现有 `inputCursorPosition` 几何；改动只是"何时/何处写"，不是"写到哪"。

### 5.2 路径 B：移植 CC 的 `src/ink` fork（中成本，能力最强）

把 `src/ink` 整目录 vendored 进 GA（`frontends/ink-ui/src/vendor/ink/`），替换 4 个业务依赖为 GA 版桩，`package.json` 补齐第三方依赖，`App.tsx` 从 `import { ... } from 'ink'` 改为 vendored 路径。
- **收益**：直接获得 frame-declared cursor、单一 writer、cursor 最后写的成熟实现，且已被 CC 生产验证。
- **风险**：(a) 版本漂移 —— vendored fork 脱离上游 ink 维护；(b) API 差异 —— CC fork 的组件 props/behavior 可能与 GA 现用 ink@5 API 不完全一致，`App.tsx` 需适配；(c) 体量 —— 数千行 vendored 代码进 GA 维护面；(d) 那 4 个业务依赖需要正确桩掉，尤其 `native-ts/yoga-layout`。

### 5.3 路径 C：自建 TerminalOwner（最高成本，2026-07-15 原方案）

完全自绘 buffer/diff/owner，不用 Ink。CC 的存在**弱化了 C 的必要性** —— 既然 CC 已经在 Ink 内做出同构解，移植（B）比从零自建（C）更省。C 仅在 B 的移植风险不可接受时才考虑。

---

## 6. 无论走哪条路，必须照抄的四个不变量

1. **cursor 只声明、不旁路写**：组件声明相对坐标，绝不 `stdout.write(CUP)`。
2. **单一 stdout writer**：所有 stdout 写出（含 cursor）收进同一个 diff/flush 出口。
3. **cursor 最后写、每帧一次**：内容 diff 全部落地后，才作为同一次 write 的最后一步定位光标；绝不在内容之间插 CUP。
4. **声明存 ref（非 state）+ 无 deps 的 useLayoutEffect + 条件清除 + queueMicrotask 时序**：避免额外重渲染、one-keystroke 延迟、memo/兄弟节点竞态。

此外：**cursor 相对节点而非绝对屏幕** —— GA 现用固定几何公式算绝对行列，改成相对 composer 节点会更稳、少受上方内容高度变化影响（这也是本次 `/help` 面板高度变化引发布局问题的同源教训）。

---

## 7. 分析边界（诚实标注）

- 依赖闭包（§4）、cursor 声明链（§2.1–2.4）、log-update 末尾 cursor restore 顺序（§2.5，log-update.ts:405–451）均**已读实源码**确认。
- 未逐行覆盖：`screen.ts` 的 VirtualScreen diff 细节、`reconciler.ts` 的完整 commit 流程、alt-screen/scrollback/flicker 全部分支、`optimize()` 是否会重排 cursor patch（落地前需精读 `writeDiffToTerminal` + `optimize` 确认 cursor patch 不被移到内容之前）。
- 路径 A 的可行性（上游 ink@5 是否提供帧末尾 cursor hook）**未验证**，是走 A 前的第一个待验证点。
- 移植（B）的 4 个业务依赖桩接工作量、CC fork 组件与 ink@5 的 API 差异**未逐项评估**。

---

## 8. 路径 A 落地计划（用户选定：不改 ink 源码）

**可行性已验证**：ink@5 无 cursor-declaration hook，但 `render(node, { stdout })` 接受自定义 stdout —— 这是不改 ink 源码的合法注入点。

实施步骤（未开工，待动手）：
1. 新增 `wrappedStdout`：包裹 `process.stdout`，实现 §5.1 的 "restore-to-frame-end → write → park-caret" 逻辑；caret 目标经共享 ref 传入。
2. `main.tsx`：`render(<App/>, { stdout: wrappedStdout })`。
3. `App.tsx`：**删除** 1131-1142 的旁路 CUP useLayoutEffect；改为每次渲染把 caret 行列写进共享 ref（running/stopping 时写 null → wrapper 不 park，光标留在末尾或隐藏）。
4. 先实测 §5.1 的风险点 1（save/restore 跨 write 可靠性）：用最小 composer echo 验证连续输入 + 中文 IME 贴 caret + 无 ghost + 无左右晃。不可靠则改"相对 CUP 回末尾"。
5. 处理 `<Static>` 写入：wrapper 区分主帧与 Static，Static 后不 park caret。
6. 回归：现有 Ink UI 全量测试 + 新增 wrapper 单元测试（断言 write 顺序：restore → content → caret，每帧一次）。

如路径 A 实测风险不可控，回退到路径 B（移植 CC fork）或 C（自建 TerminalOwner）。
