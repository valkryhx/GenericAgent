# GA Ink UI 文本选择/复制与滚动保留修复计划

**日期：** 2026-07-14  
**范围：** `frontends/ink-ui` 默认终端 UI  
**参考源码：** `D:\git_codes\codex\codex-rs\tui`  
**当前状态：** 已按目标 2 实施默认 inline scrollback 修复，并保留 `GA_INK_MOUSE=full` 旧行为开关。

> **二次复核更新：** Codex 主聊天界面的“鼠标滚轮滚动”和“拖动右侧滚动条滑块滚动”主要来自终端原生 scrollback/scrollbar，而不是 Codex 捕获鼠标后实现的应用内滚动条。Codex 将已完成历史写入正常终端 scrollback，并把底部输入区作为 inline viewport 渲染；因此终端自己的滚轮、窗口滚动条拖拽、文本拖选可以同时工作。本文此前的 `1007` 方案仍适合作为 GA 维持 alt-screen 架构时的保守修复，但若目标是对齐 Codex 当前 CLI 体验，GA 更合适的目标架构应是 **默认 inline scrollback，alt-screen 仅用于 overlay/legacy/fullscreen 模式**。

## 结论

GA 的修复应分成两档：

**Codex 等价目标方案：默认 inline scrollback。**

1. 默认不进入 alternate screen，不启用 DEC `1000/1002/1003/1006` 鼠标捕获。
2. 将 finalized transcript 写入终端正常 scrollback，而不是放在 Ink 内部虚拟 viewport 中。
3. 底部输入框、状态栏、活动中的 streaming tail 和临时面板使用 inline viewport 固定在屏幕底部。
4. 鼠标滚轮和右侧滚动条滑块拖拽由终端原生处理；GA 不接收这些鼠标事件。
5. 普通鼠标拖拽选中文本同样由终端原生处理，因此用户提问和 GA 输出都可直接选中复制。

**保守过渡方案：继续 alt-screen，但默认关闭 mouse capture 并启用 `1007`。**

1. 默认不启用 DEC `1000/1002/1003/1006` 鼠标捕获，让终端继续拥有普通拖拽选区。
2. 默认启用 DEC `1007` alternate scroll，让支持该模式的终端在 alternate screen 中把滚轮转换为方向键事件。
3. GA 在合适上下文中把这些方向键事件映射为 transcript 滚动，因此滚动能力不能退化为“只能 PgUp/PgDn”。
4. 右侧滚动条默认只能作为位置指示器；可拖拽应用内滚动条只在 `GA_INK_MOUSE=full` 旧行为模式中启用。
5. `/copy` 作为补充能力，用于复制最后一条 assistant 回复全文；它不是原生拖选的替代品。

如果只做保守过渡方案，可以恢复当前可见文本的拖选，并在支持 `1007` 的终端中保留滚轮滚动；但它不能获得 Codex 主聊天那种“拖动终端窗口右侧滑块滚动历史”的体验。要做到这一点，GA 必须把主 transcript 从 alt-screen 内部虚拟滚动迁移到终端正常 scrollback。

## 本次实施结果

本次按目标 2 选择 Codex 等价路径，而不是 alt-screen `1007` 过渡路径：

1. 默认启动序列为空：不进入 alternate screen，不清屏，不启用 DEC `1000/1002/1003/1006` 鼠标捕获。
2. 已完成 transcript 通过 Ink `<Static>` 写入终端正常输出/scrollback；当前 running/stopping 任务的 user prompt、streaming assistant tail、composer、状态栏和面板保留在 live Ink viewport。
3. 默认模式不解析、不吞掉鼠标 press/drag/wheel reports；普通拖选、滚轮和终端窗口右侧 scrollbar 交给终端原生处理。
4. `GA_INK_MOUSE=full` 保留旧 alternate-screen + full mouse capture 路径，用于需要旧应用内滚轮和自绘滚动条拖拽的场景。
5. `history_replace`、`rewind_done`、`/clear` 这类非追加更新会重置 Static generation，避免等长历史替换时旧 scrollback 项阻止新历史重播。
6. 默认 inline 模式退出时会清理完整 live viewport，而不是只清当前行；否则 Ctrl+C 后旧输入框会留在 terminal scrollback 中，重启时出现在新 UI 上方。
7. IME 光标停靠使用 save/restore 包住 `CUP`，并在下一次 Ink redraw 前恢复 renderer 光标；否则 Ink/log-update 会从输入行开始清上一帧，导致旧输入框残留，看起来像上方多了一个输入框。
8. 已验证 `npm test` 239/239 通过，`npm run typecheck` 通过。

## Codex 源码发现

### 1. Codex 主 TUI 默认不捕获鼠标

`D:\git_codes\codex\codex-rs\tui\src\tui.rs` 的模式设置只启用 bracketed paste、raw mode、键盘增强和 focus change；未启用 `EnableMouseCapture`，也未写 DEC `1000/1002/1003/1006`。

仓内检索结果也印证了这一点：`codex-rs/tui/src` 中没有 `EnableMouseCapture`、`DisableMouseCapture`、`1000h`、`1002h`、`1003h`、`1006h` 的主路径启用逻辑。

### 2. Codex 主聊天使用终端正常 scrollback

`codex-rs/tui/src/tui.rs` 的初始化注释写明：

```rust
/// Initialize the terminal (inline viewport; history stays in normal scrollback)
```

`codex-rs/tui/src/insert_history.rs` 的文件注释更直接：

```rust
//! Inserts finalized history rows into terminal scrollback.
//!
//! Codex uses the terminal scrollback itself for finalized chat history, so inserting a history
//! cell is an escape-sequence operation rather than a normal ratatui render.
```

实现上，`insert_history_lines_with_wrap_policy()` 会把 finalized history lines 写到当前 inline viewport 上方，并用 scroll region 维护底部 viewport 的位置。也就是说，Codex 主聊天历史不是一个应用内滚动列表，而是真正进入了终端 scrollback。

这解释了用户观察到的两点：

- 鼠标滚轮滚动屏幕：由终端原生 scrollback 处理。
- 按住鼠标左键拖动右侧滑块滚动：拖的是终端窗口自己的 scrollbar，不是 Codex 渲染的 scrollbar。

Codex 源码中没有发现主聊天路径通过 DEC mouse reports 实现“应用内可拖动滚动条”的逻辑。

### 3. Codex 用 DEC `1007` 保留 alt-screen overlay 的滚轮语义

Codex 在 `tui.rs` 中定义了 `EnableAlternateScroll` / `DisableAlternateScroll`：

```rust
struct EnableAlternateScroll;

impl Command for EnableAlternateScroll {
    fn write_ansi(&self, f: &mut impl fmt::Write) -> fmt::Result {
        write!(f, "\x1b[?1007h")
    }
}

struct DisableAlternateScroll;

impl Command for DisableAlternateScroll {
    fn write_ansi(&self, f: &mut impl fmt::Write) -> fmt::Result {
        write!(f, "\x1b[?1007l")
    }
}
```

进入 alternate screen 时：

```rust
let _ = execute!(self.terminal.backend_mut(), EnterAlternateScreen);
// Enable "alternate scroll" so terminals may translate wheel to arrows
let _ = execute!(self.terminal.backend_mut(), EnableAlternateScroll);
```

离开 alternate screen 时：

```rust
// Disable alternate scroll when leaving alt-screen
let _ = execute!(self.terminal.backend_mut(), DisableAlternateScroll);
let _ = execute!(self.terminal.backend_mut(), LeaveAlternateScreen);
```

关键点：Codex 用 `1007` 让终端把滚轮翻译为键盘事件，而不是让应用接收鼠标 press/drag/wheel 报告。这是“滚动仍可用”和“终端原生选区仍可用”能够同时成立的基础。

但需要限定范围：`1007` 主要服务于进入 alternate screen 的 overlay/pager 场景。Codex 主聊天界面的滚轮和右侧滑块拖拽，核心仍然是正常终端 scrollback。

### 4. Codex 明确忽略鼠标事件

`codex-rs/tui/src/tui/event_stream.rs`：

```rust
/// Map a crossterm event to a [`TuiEvent`], skipping events we don't use (mouse events, etc.).
fn map_crossterm_event(&mut self, event: Event) -> Option<TuiEvent> {
    match event {
        Event::Key(key_event) => Some(TuiEvent::Key(key_event)),
        Event::Resize(_, _) => Some(TuiEvent::Resize),
        Event::Paste(pasted) => Some(TuiEvent::Paste(pasted)),
        ...
        _ => None,
    }
}
```

也就是说，Codex 的默认主聊天没有“鼠标事件进入应用后再分配给滚动或选区”的设计。主聊天场景下，鼠标拖拽、滚轮、终端右侧滑块主要由终端正常 scrollback 处理；进入 alternate screen 的 overlay/pager 后，滚轮才依赖 `1007` 尽量转成键盘滚动。

### 5. Codex 没有主 transcript 应用内可拖动滚动条

Codex 的 scroll/pager 主要是键盘模型：

- `pager_overlay.rs` 维护 `scroll_offset`，响应 up/down/page/home/end 等 keymap。
- `bottom_pane/scroll_state.rs` 是列表选择与可见窗口状态，仍然是键盘选择和滚动。
- pager 底部显示百分比等位置提示，不依赖应用内鼠标拖动滚动条。

因此，Codex 不是“同时启用应用鼠标捕获、应用内滚动条拖拽和终端原生选区”。它的取舍是：**主聊天把历史交给终端 scrollback；overlay 才进入 alt-screen，并用键盘/`1007` 滚动。**

### 6. Codex 另有 copy-friendly 路径

Codex 还提供两类补充能力：

- `/raw`：`slash_command.rs` 中描述为 “toggle raw scrollback mode for copy-friendly terminal selection”。
- `/copy` / copy hotkey：`clipboard_copy.rs` 提供本地剪贴板、tmux、OSC 52 等路径，用于复制最后回复。

对 GA 的启示是：默认恢复终端拖选以后，仍值得提供 `/copy`，因为 native selection 在 alternate screen 下通常只覆盖当前可见文本。

## GA 当前行为对照

GA 现在的问题点非常直接。

`frontends/ink-ui/src/mouseWheel.ts`：

```ts
export function mouseTrackingOn(): string {
  return '\u001B[?1000h\u001B[?1002h\u001B[?1003h\u001B[?1006h'
}
```

`frontends/ink-ui/src/terminalCleanup.ts`：

```ts
export const enterMainScreenTerminalSequence = `${enterAlternateScreen}${mouseTrackingOn()}`
```

`frontends/ink-ui/src/App.tsx` 的 `handleTerminalInput` 中，wheel、press、drag、release 只服务于 transcript scroll 或右侧滚动条拖拽，识别到鼠标事件后都会 `return`。正文区域的 press/drag 被吞掉，GA 又没有 selection state、选区高亮或 clipboard 写入。

结果是：

```text
终端原生选区被 DEC 鼠标捕获关闭
GA 吃掉 press/drag/release
GA 未实现应用内选区
用户无法拖选 UI 中显示的文本
```

## 修复方案

### 推荐目标：默认 inline scrollback，对齐 Codex 主聊天

这是真正同时满足“滚轮滚动、拖动终端滚动条滑块、自由选中复制”的方案。

默认 `ga` / `ga ink`：

- 不进入 alternate screen。
- 不启用 `1000/1002/1003/1006`。
- finalized user/assistant/system transcript rows 写入终端正常 scrollback。
- 当前 streaming assistant tail、输入框、状态栏、面板使用底部 inline viewport。
- 鼠标滚轮、终端右侧 scrollbar 滑块拖拽、普通文本选区全部交给终端。
- GA 不需要解析 mouse reports，也不需要实现鼠标选区。

关键架构变化：

```text
当前 GA:
  alternate screen
  └─ Ink MessageViewport 内部持有 transcriptScrollOffset
     ├─ 应用内滚轮/滚动条拖拽
     └─ 终端原生 scrollback/scrollbar/selection 被削弱

Codex-style GA:
  normal screen + terminal scrollback
  ├─ finalized transcript 写入真实 scrollback
  └─ 底部 inline viewport 渲染 composer / active tail / panels
     └─ 终端原生滚轮、右侧滑块、拖选同时可用
```

### 过渡模式：保留 alt-screen，但默认 selection-first

如果短期不改主架构，则采用此前 `1007` 方案：

- 进入 alternate screen：写 `1049h`，清屏定位。
- 启用 alternate scroll：写 `1007h`。
- 不启用 `1000/1002/1003/1006`。
- 普通鼠标拖拽由终端处理，用于选中当前可见 UI 文本。
- 滚轮在支持 `1007` 的终端中转成 Up/Down，GA 把它们映射为 transcript 滚动。
- PgUp/PgDn 继续作为可靠页面滚动。
- 右侧 GA 自绘滚动条继续显示当前位置，但默认不响应鼠标拖拽。

该模式不能提供 Codex 主聊天中的“拖动终端右侧 scrollbar 滑块滚动历史”。它只能保住滚轮和键盘滚动。

### Opt-in full mouse 模式：保持旧应用内拖拽

`GA_INK_MOUSE=full` 时：

- 启用现有 `1000/1002/1003/1006`。
- 保留 wheel 事件解析、滚动条点击/拖拽。
- 普通拖选会被 GA 捕获，终端原生选区通常不可用，除非终端支持 Shift-drag 等旁路。

这个模式是兼容旧交互的 escape hatch，不应作为默认。它与普通终端拖选天然冲突。

### 为什么不默认实现 Ink 内部选区

应用内选区要做完整屏幕 buffer、坐标 hit test、宽字符处理、软换行还原、选区高亮、滚动时锚点更新、clipboard/OSC 52 等。GA 当前 Ink UI 没有这套几何模型；直接做会扩大风险。

Codex 源码显示它也没有在默认主 TUI 路径里靠应用内鼠标选区解决这个问题，而是避开 mouse capture。对 GA 的过渡方案而言，最合适的低风险修复是先恢复 terminal-native selection，并用 `1007` 保持 alt-screen 内滚轮滚动；对 Codex 等价目标而言，应进一步改成 inline scrollback，让终端原生 scrollback 接管滚轮、滑块和选区。

## 方向键滚动路由

本节只适用于 alt-screen 过渡方案或未来的 overlay/pager，不适用于默认 inline scrollback 目标方案。

`1007` 的副作用是：终端把滚轮转换成方向键，应用层通常无法可靠区分“用户按了 Up/Down”还是“滚轮转成了 Up/Down”。GA 当前 Up/Down 在空输入时用于输入历史，这里必须明确取舍。

建议规则：

```text
如果存在 active panel / selector / slash suggestions / workflow status bar 可处理键：
  Up/Down 仍交给对应控件
否则如果 input.trim() === '':
  Up/Down 滚动 transcript
否则:
  Up/Down 维持输入历史或输入编辑的现有行为
```

这样默认模式下滚轮可滚动 transcript；面板、selector、slash suggestions 不被破坏；用户正在输入内容时也不抢键。

代价是：空输入状态下，物理 Up/Down 会优先滚动 transcript，而不再直接召回输入历史。若需要保留空输入历史召回，可在后续增加 `Ctrl+Up/Ctrl+Down` 或专门的 history picker，但不应牺牲本次目标里的滚轮滚动。

## 已实施文件级结果

### 1. `mouseWheel.ts`

新增鼠标模式解析：

```ts
export type MouseCaptureMode = 'off' | 'full'

const fullMouseTrackingOn = '\u001B[?1000h\u001B[?1002h\u001B[?1003h\u001B[?1006h'
const fullMouseTrackingOff = '\u001B[?1006l\u001B[?1003l\u001B[?1002l\u001B[?1000l'

export function resolveMouseCaptureMode(env = process.env): MouseCaptureMode {
  const value = env.GA_INK_MOUSE?.trim().toLowerCase()
  if (value === 'full' || value === '1' || value === 'true' || value === 'on') return 'full'
  if (value === 'off' || value === '0' || value === 'false' || value === 'none') return 'off'

  const legacy = env.GA_ENABLE_MOUSE_DRAG?.trim().toLowerCase()
  if (legacy === '1' || legacy === 'true' || legacy === 'on' || legacy === 'full') return 'full'
  return 'off'
}

export function mouseTrackingOn(mode: MouseCaptureMode): string {
  return mode === 'full' ? fullMouseTrackingOn : ''
}

export function mouseTrackingOff(): string {
  return fullMouseTrackingOff
}
```

解析 `parseMouseEvent` / `parseMouseWheel` 可以保留，但默认 off 模式下不会收到这些 mouse reports。

### 2. `terminalCleanup.ts`

默认进入序列：

```text
''  // no-op
```

默认不进入 alternate screen，不清屏，不包含：

```text
1000h / 1002h / 1003h / 1006h
```

`GA_INK_MOUSE=full` 时才进入旧路径：

```text
1049h + clear/home + 1000h + 1002h + 1003h + 1006h
```

退出序列必须防御性关闭两类模式：

```text
默认: 1006l + 1003l + 1002l + 1000l + reset + show cursor
full: 1006l + 1003l + 1002l + 1000l + reset + show cursor + 1049l
```

`reassertMouseTracking()` 也要改成按 mode 写入：off 模式不重断言 `1000/1002/1003/1006`，full 模式才恢复。

### 3. `App.tsx`

需要显式持有 resolved mouse mode：

- full 模式：保留当前 `parseMouseWheel`、`parseMouseEvent`、scrollbar drag 逻辑。
- off 模式：不要依赖鼠标事件；理论上不会收到 mouse reports，即使收到也不应吞掉正文 drag。

新增默认 inline scrollback 渲染：

- 使用 `splitStaticAndActiveMessages()` 拆分 finalized transcript 和 active task。
- 使用 Ink `<Static>` 输出 finalized transcript，让它进入终端正常 scrollback。
- 使用 `planMessageViewport()` 决定是否保留 live message viewport；无 active tail 且已有静态历史时，不再保留空的 transcript viewport。
- 使用 `liveTranscriptViewportLines()` 只显示 active task 的尾部输出。
- `history_replace`、`rewind_done`、`/clear` 重置 Static generation，处理非追加历史变化。

### 4. `transcriptScrollbar.ts` / `MessageViewport`

默认 off 模式下：

- inline 模式不渲染 GA 自绘 transcript scrollbar。
- 不处理 `shouldHandleScrollbarDrag()`，也不吞掉正文 mouse press/drag。
- 滚轮和右侧 scrollbar 由终端原生 scrollback/scrollbar 负责。
- 退出时通过 `clearInlineLiveViewportSequence()` 清除当前 live block，避免底部 composer/input frame 留在 scrollback 中。
- 输入框原生光标停靠必须恢复到 Ink 输出末尾后再允许下一次 redraw，避免破坏 Ink 的 `eraseLines()` 清屏基准。

full 模式下：

- 保留现有 `scrollOffsetForScrollbarClick()` 与 `shouldHandleScrollbarDrag()`。

这点很重要：**可拖动滚动条需要 press/drag mouse reports；它与普通拖选天然冲突。** 默认同时要求“自由选中文本”和“拖动滚动条”是不成立的，除非投入应用内选区引擎。

### 5. 新增 Codex-style inline scrollback 架构任务

如果要对齐 Codex 当前 CLI 体验，应新增一组更大的 UI 架构任务：

- 新增 `GA_INK_SCREEN=inline|alt` 或配置项。默认 `inline`，保留 `alt` 作为旧全屏模式。
- 拆分 transcript 状态：
  - finalized messages：只追加到终端 scrollback；
  - active streaming tail：仍在底部 inline viewport 内重绘；
  - panels/selectors/status：仍在底部 viewport 内重绘。
- 新建 `terminalHistory.ts`：
  - 将 `TranscriptLine[]` 序列化为 ANSI 文本行；
  - 支持按终端宽度预换行；
  - 支持把 finalized rows 插入当前 viewport 上方；
  - 保留样式但避免破坏复制，可提供 raw/plain 模式。
- 新建 inline viewport layout：
  - 根据 composer、状态栏、panel、active tail 计算底部 viewport 高度；
  - viewport 高度变化时清理旧区域并重绘；
  - finalized transcript 不再参与 `MessageViewport` 的内部 `scrollOffset`。
- resize/reflow：
  - 终端宽度变化后，从源消息重建 scrollback；
  - 对 scrollback 行数设置上限，避免无限增长；
  - reflow 时保留底部 viewport 和当前输入。
- 清屏/新会话：
  - 提供清理 visible screen + scrollback 的 ANSI 路径；
  - 重新绘制 header 和底部 viewport。
- overlay：
  - `/raw` 或全量 transcript overlay 可继续用 alternate screen；
  - overlay 使用 `1007` + keyboard pager，不启用 mouse capture。

这个任务量明显大于 `1007` 过渡修复，但它是同时获得 Codex 式滚轮、终端 scrollbar 拖拽、原生选区复制的正确方向。

### 6. `/copy` 补充命令

建议保留此前计划中的 `/copy`：

- `copyTranscript.ts`：从 `ChatMessage[]` 中取最后一条非空 assistant text。
- `clipboard.ts`：Windows 用 `clip.exe`，SSH/tmux/其他终端可回退 OSC 52。
- `inputController.ts` / `slashCommands.ts` / `App.tsx`：接入 `/copy`。

这不影响 terminal-native selection，只用于复制最后 assistant 回复全文。

## 测试计划

### 1. inline scrollback 目标方案测试

默认 inline 模式：

- startup sequence 不包含 `\u001B[?1049h`。
- startup sequence 不包含 `\u001B[?1000h`、`\u001B[?1002h`、`\u001B[?1003h`、`\u001B[?1006h`。
- finalized transcript append 会写入终端正常输出/scrollback 路径，而不是更新 `MessageViewport` 的 `transcriptScrollOffset`。
- active streaming tail 和 composer 仍在底部 viewport 中重绘。
- resize 后从源 messages 重建 scrollback 或标记 pending reflow。
- inline 模式下不渲染或不依赖 GA 自绘可拖动滚动条。

人工验收：

- Windows Terminal 中滚轮滚动历史。
- Windows Terminal 中拖动终端窗口右侧 scrollbar 滑块滚动历史。
- 普通左键拖选可选中历史和当前可见输出。

### 2. alt-screen 过渡方案 terminal sequence 单元测试

`GA_INK_SCREEN=alt` 或 legacy fullscreen 模式下：

- enter sequence 包含 `\u001B[?1049h`。
- enter sequence 包含 `\u001B[?1007h`。
- enter sequence 不包含 `\u001B[?1000h`、`\u001B[?1002h`、`\u001B[?1003h`、`\u001B[?1006h`。
- exit sequence 包含 `\u001B[?1007l`。
- exit sequence 仍包含 full mouse-off cleanup：`1006l/1003l/1002l/1000l`。

`GA_INK_MOUSE=full` 模式：

- enter sequence 包含 `1049h`。
- enter sequence 包含 `1000h/1002h/1003h/1006h`。
- enter sequence 不依赖 `1007h`；full 是旧应用内鼠标模式，不是 Codex overlay keyboard-scroll 模式。
- `reassertMouseTracking(stdout, 'full')` 写出 full mouse tracking。
- `reassertMouseTracking(stdout, 'off')` 不写出 full mouse tracking。

### 3. key-scroll 单元测试

该组测试只适用于 alt-screen 过渡方案或 overlay/pager。新增 helper 测试：

- input 为空、无 active panel、无 slash suggestions 时，Up/Down 返回 transcript scroll action。
- input 非空时，Up/Down 不触发 transcript scroll。
- selector/model/mcp/theme/workflow/footer/slash suggestions 活跃时，Up/Down 不触发 transcript scroll。
- PageUp/PageDown 保持现有 transcript page scroll。

### 4. mouse mode 行为测试

- 默认 `resolveMouseCaptureMode({}) === 'off'`。
- `GA_INK_MOUSE=full`、`1`、`true`、`on` 都进入 full。
- `GA_INK_MOUSE=off` 显式压过 legacy `GA_ENABLE_MOUSE_DRAG=true`。
- full 模式下现有 `parseMouseWheel` / `parseMouseEvent` 测试继续通过。

### 5. `/copy` 测试

- 复制最后一条非空 assistant message。
- 无 assistant 输出时给出可读错误。
- OSC 52 payload 构造和大小限制有单测。
- Windows `clip.exe` 路径可通过 spawn mock 或薄封装测试。

### 6. 人工验收

在 Windows Terminal 中：

1. 默认启动 `ga`。
2. 确认普通左键拖拽可选中欢迎文本、用户问题、assistant 输出。
3. 复制后粘贴，内容应包含选中文本。
4. inline scrollback 目标方案下，滚轮应由终端直接滚动历史。
5. inline scrollback 目标方案下，拖动终端窗口右侧 scrollbar 滑块应滚动历史。
6. 默认 inline 模式下，GA 不显示自绘 transcript scrollbar；使用终端窗口自己的 scrollbar。
7. `/copy` 若后续实现，应能复制最后一条 assistant 回复；当前不是目标 2 的必要条件。
8. 退出后 shell 中普通选择不受影响。

在 full 模式中：

```powershell
$env:GA_INK_MOUSE='full'
ga
```

验证：

- 滚轮和右侧滚动条拖拽保持旧行为。
- 普通拖选被应用鼠标捕获影响，这是该模式的已知取舍。
- 退出后 terminal mouse mode 被清理。

## 验收标准

- 默认启动不写 `1000h/1002h/1003h/1006h`。
- Codex 等价目标方案下，默认主聊天不进入 alternate screen，finalized transcript 进入终端正常 scrollback。
- Codex 等价目标方案下，鼠标滚轮和拖动终端右侧 scrollbar 滑块都能滚动历史。
- 默认模式下，用户可用普通鼠标拖选 GA UI 当前可见内容，包括用户提问和 GA 输出。
- 默认模式下，滚动能力由终端原生 scrollback 保留。
- inline 模式下不需要 GA 自绘右侧滚动条；`GA_INK_MOUSE=full` legacy 模式保留旧自绘滚动条和鼠标拖拽。
- `GA_INK_MOUSE=full` 可恢复旧的 mouse wheel / scrollbar drag 行为。
- `Ctrl+C` 仍为退出，不改成复制。
- `/copy` 是补充命令，不替代拖选。

## 风险与边界

- inline scrollback 方案会改变 GA 当前全屏布局模型，风险高于 `1007` 过渡修复。
- inline scrollback 需要处理 resize 后 scrollback reflow，否则历史行宽会与新终端宽度不一致。
- 终端 scrollback 有容量上限，GA 需要决定旧 transcript 行如何截断或何时 rebuild。
- `1007` 是终端相关能力。Windows Terminal、VS Code terminal、tmux、不同 shell 嵌套环境都需要人工验证。
- 在 `1007` 模式下，滚轮转成的 Up/Down 与物理 Up/Down 不一定可区分。为保留滚轮，GA 需要在空输入主界面把 Up/Down 优先解释为 transcript line scroll。
- native selection 只能选择当前屏幕上实际显示的字符；这不是 GA 独有问题，而是 alternate screen 的常见边界。
- alt-screen 虚拟 viewport 不能在不捕获鼠标的同时支持正文普通拖选和应用内右侧滚动条拖拽。应用内拖拽滚动条必须放在 `GA_INK_MOUSE=full`，或未来投入应用内选区引擎；inline scrollback 则依赖终端原生 scrollbar，不走 GA 自绘滚动条。

## 最终建议

本次目标是对齐用户观察到的 Codex CLI 主聊天体验，即“滚轮滚动 + 拖动右侧终端 scrollbar 滑块滚动 + 普通拖选复制”同时成立。因此最终实现选择：**默认 inline scrollback，把 finalized transcript 写入终端正常 scrollback，底部只保留 composer/active tail/panel 的 live viewport。**

`1007` alt-screen 过渡方案仍可作为未来 overlay/pager 的参考，但不应作为 GA 默认主聊天路径。默认主聊天路径不捕获鼠标，不进入 alternate screen，不做应用内选区引擎；`GA_INK_MOUSE=full` 仅作为旧应用内鼠标滚动和自绘滚动条拖拽的兼容开关。
