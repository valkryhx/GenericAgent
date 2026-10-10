# GA Ink UI：终端放大/缩小后重复信息（resize reflow）根因与修复

**日期：** 2026-10-10
**现象证据：** `屏幕截图 2026-10-10 092005.png`（同一行 `Worked for 9s • Oct 10 at 09:18 • ...` 重复 5 次、
提示行 `Enter send · Alt+Enter newline · ...` 重复 3 次，输入框本身正常）
**复现测试：** `frontends/ink-ui/src/resizeReflow.test.ts`（配套模型 `src/resizeReflowModel.ts`）
**状态：** 已修复；`npx tsx --test src/*.test.ts` 397/397 通过（含新增 6 个用例）

---

## 1. 结论（一句话）

**终端宽度变化会触发终端对已写出缓冲区做 reflow（长行重新折行），而 ink 的 `log-update` 下一帧仍然
用「改尺寸之前」的 `previousLineCount` 做相对擦除（`eraseLines`），于是擦不干净 → 旧帧顶部若干行
残留 → 每次缩放都多叠一份「活动行 + 提示行」。** 修复方式与 Claude Code / Codex 一致：**不猜 reflow
之后的几何，直接整屏重置 + 全量重绘。**

---

## 2. 根因链（逐层）

1. **活动帧是每帧重绘的**，不是 `<Static>` 历史。`ActivityView`（`App.tsx` 的 `BottomChrome` 内）渲染
   `Worked for ...`，与提示行、输入框一起属于 ink 的 live 输出；`<Static>` 只承载已完成的消息历史。
   截图里重复的正是 live 帧的**顶部两行**，历史行没有重复 —— 这一点决定了根因不在 Static 分区。
2. **ink 用纯相对擦除重绘**：`ink/build/log-update.js` 每帧写
   `eraseLines(previousLineCount) + output`，`eraseLines(n)` = 擦当前行 + 上移擦 n-1 行 + 回列 0。
   它假设「上一帧占用的行数 == previousLineCount」，且光标停在上一帧最后一行的下一行。
3. **GA 的 cursor-park 包裹流维持了这个不变量**（写入前 `unpark` 回帧底），所以**在不 resize 的情况下
   它是自洽的** —— 这也解释了为什么这个 bug 只在缩放终端时出现。
4. **宽度变化破坏了这个不变量**：真实终端（Windows Terminal 等）在变宽/变窄时会对缓冲区 reflow。
   例如 80 列时的 79 字符边框行，变窄到 40 列后占 2 行；上一帧在屏幕上**实际**占用 8 行，而
   `previousLineCount` 仍是 5。ink 从光标位置往上擦 5 行 → 顶部 3 行（活动行 + 折行后的提示行）
   留下来。新的 live 帧随后写在残留行下方 → 视觉上「重复叠加」。反复缩放 → 反复叠加（截图里 5 份）。
5. **变宽同样有问题**（反向）：擦除行数偏多会擦到可见历史行，留下空白洞。所以 Claude Code 的判定是
   `next.viewport.height < prev.viewport.height || next.viewport.width !== prev.viewport.width`，
   即「变窄或变矮 → 整屏重置」。

**参考实现对照**

| 实现 | 位置 | 做法 |
|---|---|---|
| Claude Code | `src/ink/log-update.ts:146` | viewport 变窄/变矮 → `fullResetSequence_CAUSES_FLICKER` = `clearTerminal` + 整帧重画（不预测 reflow 后的布局） |
| Claude Code | `src/ink/clearTerminal.ts` | `clearTerminal` = `ESC[2J` + `ESC[3J`（清 scrollback）+ `ESC[H`（归位） |
| Codex CLI | `codex-rs/tui/src/tui.rs:1197`、`:1422` | `update_inline_viewport_for_resize_reflow` 重算 viewport 区域 → `clear_after_position(旧顶与新顶的较小值)` → `invalidate_viewport()` 全量重绘 |
| ink 5（GA 在用） | `ink/build/ink.js` | 只有「帧高 ≥ 终端高」时才走 `clearTerminal + fullStaticOutput + output` 全量重绘；普通 resize 走 `eraseLines`（就是本 bug） |

---

## 3. 复现（把真机现象降维成确定性断言）

新增 `frontends/ink-ui/src/resizeReflowModel.ts`：一个**reflow 感知的最小虚拟终端**（playbook 手段一）。

- 缓冲区保存**逻辑行**，可视行按当前 `columns` **派生**折行 → `columns` 一变，reflow 自动发生；
- 光标锚在 (逻辑行, 行内偏移)，与真实终端一致（reflow 时光标跟着文本走）；
- 解释 ink / GA 实际写出的 CSI：`eraseLines` 组合、`CSI n A/B/C/D`、`CSI n G`、`CSI r;c H`、
  `CSI 2K`、`CSI 2J`、`CSI 3J`、`CSI ?25h/l`、CR、LF；
- 保真度边界写在文件头（按 1 字符 1 列折行，不模拟宽字符宽度表/备用屏/滚动区域），测试只用 ASCII 探针。

`src/resizeReflow.test.ts` 用真实的 `<App/>` + 真实 ink（非 debug，走 `log-update`）驱动这个虚拟终端，
断言不变量：**一次宽度变化之后，屏幕上每条内容行只应出现一次**（探针：历史两行、活动行、提示行、
流式三行）。

**红（修复前，App.tsx 回退）：**

```
not ok 3 - App: terminal width change must not duplicate the live frame (resize reflow)
  变窄之后: 屏幕上 "Enter send" 应只出现一次
  11|  Worked for <1s • Oct 10 at 09:37          <- 旧帧残留（reflow 后没被擦掉）
  13|  Enter send · Alt+Enter newline · PgUp/P    <- 旧帧残留
  14|  Worked for <1s • Oct 10 at 09:37          <- 新帧
  15|  Enter send · Alt+Enter newline · PgU…      <- 新帧
  2 !== 1
```

残留行的形状与真机截图完全一致（活动行 + 提示行重复，输入框正常），因此这条测试**真的复现了真机现象**，
不是「为了通过而写」的断言。

**绿（修复后）：** 3/3 通过；整个 ink-ui 套件 397/397 通过。

---

## 4. 修复（3 处改动，均为硬约束，不依赖提示词）

1. `src/terminalCleanup.ts::resetViewportSequence()`
   产出「清屏 + 清 scrollback + 归位」序列：现代终端 `CSI 2J CSI 3J CSI H`；老 Windows 控制台
   （build < 10586）退化为 `CSI 2J CSI 0f`，判定与 ansi-escapes 的 `clearTerminal` 一致。
2. `src/stdoutCursorPark.ts`
   - `CursorParkWriter.resetViewport(sequence)`：写出重置序列，并**清掉 `parkedUp`**（光标已被归位到
     屏幕左上，下一次写入若还按旧几何 `unpark` 下移就会把整帧写歪）；
   - 新增 `parkEpoch`：作废「已排队但尚未执行」的 park microtask（否则它会从 home 再上移，把光标锚到
     错误位置）。
   - `CursorParkController.resetViewport()` 暴露给 App。
3. `src/App.tsx` resize 监听
   - 用 `terminalSizeRef` 判断宽/高是否真的变了；
   - **先** `setTerminalSize(next)`（React legacy root 同步提交，此后所有写入都按新宽度折行）；
   - 若**宽度**变了：`cursorPark.resetViewport()`（未启用 park 时直接 `stdout.write(resetViewportSequence())`）
     → `resetStaticTranscriptOutput()` 重挂 `<Static>`，让 ink 把历史整段重发，随后写出新帧。
     顺序很重要：重置必须发生在「最后一次全量重绘」之前，否则重置后仍会写出一帧旧宽度内容。

---

## 5. 取舍与已知边界（诚实标注）

- **会清 scrollback（`CSI 3J`）**：不这样做，重印的历史会在 scrollback 里出现两份（用户上滚会看到重复）。
  代价是 GA 启动前的 shell 输出会被清掉，与 ink 自身的全量重绘分支、Claude Code 的行为一致。
- **每次宽度变化都会重印整段历史**：拖拽窗口边缘会触发多次（每次都是「清屏 + 重印」）。这是与
  Claude Code / Codex 相同的取舍（缩放是低频事件）。副作用：ink 内部的 `fullStaticOutput` 会随之累积，
  但 GA inline 布局下 live 帧恒 < 终端高，不会走到使用它的分支（`outputHeight >= stdout.rows`）。
- **高度变化（rows 变）不触发重置**：高度变化不会引起 reflow，相对擦除仍然成立；触发重置反而可能因为
  「帧内容没变 → ink 不写新帧」而留下空屏。只有宽度变化才走整屏重置。
- **本模型测不到真机终端的实际显示**（宽度表差异、IME 锚点等），单测钉住的是「写出的字节 + 屏幕几何」。
  真机仍需人工确认一次（缩放几次，确认无重复、无空屏、历史仍在 scrollback 里）。

---

## 6. 验证命令

```
cd frontends/ink-ui
npx tsc --noEmit
npx tsx --test src/resizeReflow.test.ts src/terminalCleanup.test.ts src/stdoutCursorPark.test.ts src/App.test.ts
npx tsx --test src/*.test.ts        # 全量 ink-ui：397 通过
```

新增用例：
- `resizeReflow.test.ts`：reflow 模型自检、相对擦除漏行的机制验证、App 级 resize 不变量（idle + 流式 + 变窄/变宽）、
  字节级契约（宽度变化必须写整屏重置、重置后不得再写旧宽度帧）。
- `terminalCleanup.test.ts`：`resetViewportSequence` 必须清屏 + 清 scrollback + 归位，且不得隐藏光标。
- `stdoutCursorPark.test.ts`：`resetViewport` 写出序列、清空 `parkedUp`、作废已排队的 park。
