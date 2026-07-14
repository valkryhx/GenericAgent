# GA Ink UI 输入框位置跳动 — 诊断与 Codex 对齐方案

**日期：** 2026-07-14  
**状态：** **二期已落地（content-desired）** — slash 在 input 下 + idle 贴 Static / stream 随内容 capped；见 `docs/ga_ui_composer_layout_implementation_progress_2026-07-14.md`  
**范围：** 默认 inline scrollback 模式（`ga` / `ga ink`，`mouseMode !== 'full'`）；slash 面板顺序问题在 full 模式同样存在  
**证据图：** `截图/图1.png`（输入前）、`截图/图2.png`（输出后）  
**Codex 参考：** `D:\git_codes\codex\codex-rs\tui`（`bottom_pane`、`chat_composer`、`command_popup`、`custom_terminal.viewport_area`、`insert_history`、`app.rs` 的 `desired_height` 绘制）  
**实施进度：** `docs/ga_ui_composer_layout_implementation_progress_2026-07-14.md`  
**衍生问题：** Running 可见性阶段 1–3 已落地 — `docs/ga_ui_running_turn_visibility_diagnosis_2026-07-14.md` / `docs/ga_ui_running_visibility_implementation_progress_2026-07-14.md`

---

## 0. 结论（先读）

| 项 | 内容 |
|---|---|
| 用户感受 A（修前） | 图1 输入框偏下；图2 输出后输入框**贴着内容**又挤在中上部，下半屏空荡 |
| 用户感受 A'（一期后） | 输入框**始终在终端下方**了，但与对话内容**距离过大**；本轮 user 甚至要**向上滚**才看得见——不美观 |
| 用户感受 B | `/` 列表：GA 曾在输入框上；Codex 在输入框下（一期已对齐） |
| 根因 A（修前） | live 区 `none` 高度 0 → composer 贴 Static 尾 |
| 根因 A'（一期过修） | 始终消费满 `messageRows` 作 live 槽 → 把 **content-desired dock** 误做成 **full-height spacer** |
| 根因 B | `inputChromeSections` 曾把 slash 放在 input 前（一期已修） |
| Codex 真实模型 | **viewport 高度 = bottom_pane 内容期望高度**（小）；历史进 scrollback；内容增多时 **viewport 下移**，**贴底后 y 固定**，再增长只 scroll 历史区 |
| 推荐方向 | **二期**：dock 高度 content-desired + 触底固定；**不是**永远预留满屏 live 空白 |

一句话（更新）：

> **修前是“底栏塌缩贴内容”；一期修成“底栏永远贴终端底 + 中间大片空”**。  
> Codex 则是：**composer 紧贴最新内容下方；内容变多时整体下移；触底后 composer 固定，历史向上滚。**

### 0.1 用户观察是否成立？（对照 Codex 源码：**成立**）

用户描述：

1. Codex 输入框距离内容下方**不远**（贴内容，不是贴终端底中间留一大片空）  
2. 内容变多时，输入框**跟着下移**  
3. 内容多到阈值后，输入框**固定不动**（贴终端底），再往上看历史要滚动  

Codex 源码对应关系：

| 用户说法 | Codex 机制 | 关键代码 |
|---|---|---|
| dock 高度随内容/chrome 需要，而非永远满屏 | `desired_height = chat_widget.desired_height(width)`，再 `tui.draw(desired_height, …)` | `app.rs` `render_chat_widget_frame` |
| ChatWidget 期望高 ≈ active cell + bottom_pane（composer/status/popup），**不是** `terminal.rows - header` 整块 live | `FlexRenderable`：active_cell flex + `BottomPane` 固定高度 | `chatwidget/rendering.rs` |
| BottomPane 高度 = status/preview/composer/popup 的 `desired_height(width)` | content-desired | `bottom_pane/mod.rs` `desired_height` |
| 内容插入 scrollback 时，若 viewport **还没贴底**，则 **下移 viewport**（composer 跟着内容走） | `if area.bottom() < screen_size.height { … area.y += scroll_amount }` | `insert_history.rs` |
| 一旦 `area.bottom() == screen_height`，再插历史 **不再改 y**，只在 scroll region 顶部滚历史 | else 分支只写 scroll region `1..area.top()` | 同文件 ASCII 图 |
| 绘制时若 dock 变高导致 `bottom() > height`，则 scroll 上推并把 `area.y = height - area.height`（贴底） | `tui.draw` 内 viewport 扩展逻辑 | `tui.rs` `draw` |

ASCII（Codex，与 `insert_history.rs` 注释一致）：

```text
未贴底（内容尚少）：
┌─ 屏幕 ─────────────────────┐
│ history（scrollback 可见段）│
│ … 最新消息                  │
│╭─ viewport (dock) ────────╮│  ← y 随插入下移
││ status / active tail     ││
││ composer                 ││  ← 紧挨内容下方
││ popup?                   ││
│╰──────────────────────────╯│
│ （屏幕下方可能还有空行）      │
└────────────────────────────┘

已贴底（阈值后）：
┌─ 屏幕 ─────────────────────┐
│┌ scroll region（可滚历史） ┐│
│┆ 更早的 history            ┆│
│█───────────────────────────┘│
│╭─ viewport 贴底 ──────────╮│  ← y 固定 = H - dockH
││ composer…                ││
│╰──────────────────────────╯│
└────────────────────────────┘
```

**结论：** 用户对 Codex 的体感描述与实现一致。一期 GA 用「永远 `messageRows` 满高 live 槽」只学到了「贴底」，**没学到「content-desired + 触底前随内容下移」**。

---

## 1. 截图对照

### 1.1 图1 — 输入时（空会话 / ready）

- 顶栏：`GenericAgent` · `idle`
- 中部：大块**空白**
- 底部附近：状态 hint + 横线输入框（`>`）

视觉印象：输入区在**终端下半部分**，上方有呼吸空间。

### 1.2 图2 — 助手输出后

- 上方 scrollback：用户消息 + 助手 Markdown 回答（Static 已写入）
- 紧接：`GenericAgent running/idle` 顶栏 + 活动行（如 `Thinking` / 耗时）
- **再紧接** hint + 输入框
- 输入框**下方**到终端底边：大片空白

视觉印象：输入区贴在**内容尾巴**上，整体“悬浮”在屏幕中上部，下半屏浪费。

### 1.3 差异本质

| | 图1 | 图2 |
|---|---|---|
| Static 历史 | 无 / 少 | 有长回答 |
| live message 区高度 | ready 占位约 1 行（或等价空态） | `none`（0）或短 live |
| composer 相对终端底边 | 偏近底边 | **远离底边**（被顶上去） |
| 用户期望 | 两者都像图1：composer 稳在下半区 | — |

---

## 2. GA 当前布局模型（为何会跳）

### 2.1 默认 inline 树（简化）

```text
<Static items={staticTranscriptRows} />     // 不可撤销，进终端 scrollback
<Box width=canvas>                          // live 帧
  Header (1)
  MessageViewport?                          // 高度由 plan 决定：0 / 1 / messageRows
  BottomChrome (activity + hint + input + panels)
</Box>
```

关键代码：

- `layoutMetrics.ts`：`messageRows = rows - header - bottomRows`（为 full 模式算满高消息槽）
- `messageViewportPlan.ts`：

```ts
if (liveLineCount > 0) return { kind: 'live', height: messageRows }  // 占满剩余
if (!hasStaticMessages) return { kind: 'ready' }                     // 高度 1
return { kind: 'none' }                                              // 高度 0  ← 图2
```

- `App.tsx`：`inlineMessageRows` 在 `none` 时为 **0**；`liveViewportRows = header + inlineMessageRows + bottomRows`

### 2.2 状态机（默认模式）

| 场景 | hasStatic | liveLineCount | plan | 输入框相对终端 |
|---|---|---|---|---|
| 全新会话 idle | false | 0 | **ready** (h=1) | 偏下（图1） |
| 刚提交 user（仅 Static） | true | 0 | **none** (h=0) | **贴 Static 尾** |
| streaming 有 assistant | true | >0 | **live** (h≈messageRows) | 输入框又被顶到接近底（中间大 live 槽） |
| 回答完成 idle | true | 0 | **none** | **再次贴内容**（图2） |

因此用户看到的是：

1. 空会话：有 ready 占位 → 像底部输入  
2. 有历史且 idle：无占位 → 输入贴内容  
3. streaming：突然给满高 live → 输入又沉底  

**三态高度策略不一致**，造成“输入框位置乱跳”。

### 2.3 与用户双显修复的耦合

双显修复（Codex 对齐 partition）后：

- done user **不再**进入 `activeMessages`
- 大量时刻 `liveLineCount === 0` 且 `hasStaticMessages === true`
- 更频繁命中 `kind: 'none'`

双显修复方向正确，但**放大了**输入框贴内容的布局缺陷。布局应单独修，而不是回退 partition。

### 2.4 full mouse 模式对比

`GA_INK_MOUSE=full` 使用固定 `metrics.messageRows` 的单一 MessageViewport，composer 在 `BottomChrome` 内相对更稳；问题主要在**默认 inline**。

---

## 3. Codex 怎么做（源码要点）

### 3.1 两个纵向区域

| 区域 | 机制 | 内容 |
|---|---|---|
| 上方 history | `insert_history_lines` 写入**正常 scrollback** | 已 finalize 的 user/assistant/cells |
| 底部 viewport | `terminal.viewport_area` 矩形 | **BottomPane**：status 指示 + composer + 弹层 |

初始化注释（`tui.rs`）：

> Initialize the terminal (**inline viewport**; history stays in normal scrollback)

### 3.2 高度：content-desired，但锚点在底

- `BottomPane` / `ChatComposer` 提供 `desired_height(width)`
- 渲染层按 **终端底边向上** 切一块 `height = desired_height` 的 `viewport_area`
- history 插入时用 scroll region，把行塞进 **viewport 上方**，并可能下移 viewport 的 `y`（`insert_history.rs` 里对 `area.y` 的调整）

结果：

- 历史再长，**composer 仍在屏幕底部那条 dock 里**
- 空白出现在 **history 与 dock 之间的自然 scrollback**，而不是“dock 贴着最后一条 history 再留一截屏下空白”

### 3.3 与 GA 的概念映射

| Codex | GA 现状 | 目标映射 |
|---|---|---|
| insert_history → scrollback | Ink `<Static>` | 保持 |
| viewport_area（底） | live `Box` + BottomChrome（内容跟随） | **改为固定底栏高度策略** |
| desired_height(composer+status) | `bottomRows` 已近似 | 保留并强化 |
| viewport 内 active/stream | `MessageViewport` live | 放在底栏**之上固定槽**或并入底栏上方固定区 |
| 无 “none 高度 0” 塌缩 | `planMessageViewport none` | **删除/替换 none 塌缩** |

Codex **不会**在“有历史但无 stream”时把底部 pane 高度收成 0 并上贴 history。  
但 Codex **也绝不会**在 idle 时预留「几乎整屏空白 live 槽」把 composer 钉在终端底、与最近消息隔十几行——那是 GA 一期过修。

### 3.5 Codex「先下移、后固定」——源码级时序（用户描述的权威确认）

#### 3.5.1 每帧：viewport 高度 = 内容期望高

`app.rs`：

```text
desired_height = chat_widget.desired_height(width)
tui.draw(desired_height, |frame| chat_widget.render(frame.area(), …))
```

`ChatWidget::desired_height` → active streaming/hook cell 高度 + `BottomPane::desired_height`（composer + status + popup…）。  
**阈值 ≈ 终端高度**；未触顶时 viewport **可以很矮**，紧贴已写入的 history 下方。

#### 3.5.2 插入历史：未贴底则 `area.y += scroll_amount`

`insert_history.rs` 核心：

```rust
let mut area = terminal.viewport_area;
if area.bottom() < screen_size.height {
    // viewport 还不在屏幕底：向下挪，给上方腾出 history 行
    let scroll_amount = wrapped_lines.min(screen_size.height - area.bottom());
    // … SetScrollRegion + reverse index 等 …
    area.y += scroll_amount;
    should_update_area = true;
} else {
    // 已贴底：只改 scroll region 顶部，area.y 不变
}
// 在 1..area.top() 的 scroll region 里 Print 历史行
if should_update_area {
    terminal.set_viewport_area(area);
}
```

体感翻译：

| 阶段 | `viewport.bottom()` vs `screen.height` | composer 行为 | 与内容距离 |
|---|---|---|---|
| 启动 / 短会话 | bottom **<** height | **随 history 插入下移** | **近**（history 尾 ≈ viewport 顶） |
| 长会话 | bottom **==** height | **y 固定贴底** | 仍近（viewport 顶 = 可见 history 尾） |
| 再插更多 history | 已贴底 | 历史在上方 scroll region 滚动 | 仍近；旧内容进 scrollback |

#### 3.5.3 绘制时 dock 变高（多行输入 / 开 slash）

`tui.rs` `draw`：

```rust
area.height = height.min(size.height);
if area.bottom() > size.height {
    // dock 长高后溢出：上滚并贴底
    area.y = size.height - area.height;
}
```

即：**popup/多行 composer 变高时仍贴底**，不会把输入顶出屏幕。

#### 3.5.4 与 GA 一期实现的对照（为何「太远」）

| | Codex | GA 一期（当前） |
|---|---|---|
| live/viewport 高度 | ≈ bottom_pane + 少量 active cell | **始终 ≈ `messageRows`（近满屏）** |
| idle 有 Static | dock 矮，贴 history 尾；空白在 **屏幕底外/历史上方自然区** | dock 上方 **强制空白 messageRows**，composer 贴终端底 |
| 内容增多 | viewport **下移**直到贴底 | Static 进 scrollback，但 live 槽高度**不变**，composer **不跟内容走** |
| 贴底后 | y 固定 | y 一直固定（从 ready 起就固定） |
| 看本轮 user | 通常仍在 viewport 上方可见尾部 | user 在 Static，中间隔满高空槽 → **要向上滚** |

用户反馈「输入框倒是始终在下方，但离对话内容太远」= **一期过修的直接产物**，不是 Codex 目标态。

---

## 4. 问题定义（产品）

### 4.1 目标体验（修订：对齐 Codex 真实体感）

1. **Composer 紧贴最新可见内容下方**（小间距：activity/status 等 chrome），**不要**中间空十几行。  
2. **短会话 / 内容未满屏**：composer **随内容增多下移**（相对终端坐标变大），而不是一开始就钉死在最后一行。  
3. **长会话 / 内容触底后**：composer **固定贴终端底**；更早历史进入原生 scrollback，向上滚可看。  
4. **历史仍可原生滚动/复制**（inline scrollback）。  
5. **Streaming** 时增量在 composer 上方可见；dock 高度随 active 尾变化，但相对「内容尾」仍近。  
6. 不回退 user 双显修复（done user 仍只进 Static）。  
7. slash/panel 仍在 input **下**（一期 B 保留）。

### 4.2 非目标

- 完整 1:1 移植 Codex `insert_history` 的全部 ANSI 细节（可分阶段逼近）  
- 重做 full mouse 模式  
- 改主题色/字体

---

## 5. 方案比选

| 方案 | 做法 | 优点 | 风险 | 建议 |
|---|---|---|---|---|
| **A. 固定 live 槽 + 底栏** | 默认模式始终保留 `messageRows`（或 `minStableLiveRows`）高度的消息区；无内容时空白填充；BottomChrome 始终在其下 | 最接近 Codex“底 dock”；图1/图2 一致 | 空会话中间有意空白（正是图1 想要的） | **主推** |
| **B. 仅禁止 none** | `hasStatic && live==0` 时改为 `ready` 或固定 N 行 spacer | 改动小 | spacer 太小仍贴内容；streaming 仍可能跳 | 可作第一步 |
| **C. flex 顶对齐 + 底绝对** | 用 Ink 难以真正 absolute；需手工算 paddingTop | — | Ink 模型别扭 | 不优先 |
| **D. 全屏 alt + 内滚** | 回到 full 模式默认 | 位置稳 | 牺牲选中/原生 scrollback | **否** |
| **E. 完整 Codex viewport_area** | 自管 scroll region + 底矩形 | 最像 Codex | 工作量大、与 Ink 抢 stdout | 中长期 |

### 5.1 推荐：A（可分两期）

**一期（快）：消灭 `none` 塌缩**

- `planMessageViewport`：有 Static 且无 live 时，不再 `none`  
- 改为 `{ kind: 'live', height: stableRows }` 或 `{ kind: 'spacer', height: stableRows }`，空白填充  
- `stableRows` 建议：`max(1, messageRows)` 与现 `computeLayoutMetrics.messageRows` 对齐，保证 BottomChrome 落在终端下半区

**二期（稳）：语义对齐 Codex bottom dock**

- 明确分区：  
  - **Scrollback band**：仅 `<Static>`  
  - **Dock band**（固定贴终端底）：`[ optional live tail ≤ H_live ][ activity ][ hint ][ input ][ slash/panel 在 input 下 ]`  
- live tail 在 dock 内**顶部**滚动，高度上限 `H_live`（例如 6–12 行或 `messageRows` 的比例）  
- idle 时 live tail 为空但**保留高度**（或最小 spacer），避免 chrome 上移  
- **slash / 其它 panel 一律在 composer 下方**（见 §5.2、§6.6）

### 5.2 问题 B：`/` 命令列表应在输入框下方（Codex 对齐）

#### 5.2.1 用户观察

- Codex：输入 `/` 后，`/xxx` 候选列表出现在 **composer 下方**（列表在下、输入框在上）。  
- GA：候选出现在 **输入框上方**（列表在上、输入框在下）。

#### 5.2.2 GA 现状（证据）

`frontends/ink-ui/src/inputLayout.ts`：

```ts
return [
  ...(hasError ? ['error'] : []),
  ...(hasPanel ? ['panel'] : hasSlashSuggestions ? ['slashSuggestions'] : []),
  'hint',
  'input',
]
```

`App.tsx` 中 `BottomChrome` 按 `inputSections.map` **自上而下**渲染 → 顺序实际是：

```text
[error?]
[slashSuggestions | panel]   ← 在上
[hint]
[input]                      ← 在下
```

因此 `/` 菜单天然在输入框**上方**。

同文件逻辑里，`mcp` / `model` / `workflow` / `selector` 等 `panel` 也与 slash 共用 `panel` 槽，**同样在 input 之上**。

#### 5.2.3 Codex 源码（证据）

`codex-rs/tui/src/bottom_pane/chat_composer.rs` 布局：

```rust
let [composer_rect, popup_rect] =
    Layout::vertical([Constraint::Min(3), popup_constraint]).areas(area);
```

含义（ratatui `Layout::vertical` 自上而下切分）：

1. **上方** `composer_rect`：边框 + textarea（`Constraint::Min(3)`）  
2. **下方** `popup_rect`：  
   - 有 slash/file/skill/mention popup 时 → 画 `CommandPopup` 等  
   - 无 popup 时 → 该区域用于 **footer key hints**（`Constraint::Max(footer_total_height)`）

渲染时 `popup.render_ref(popup_rect, …)` 与 `render_footer_*` 都落在 **composer 之下**。

`BottomPane` 组装（`bottom_pane/mod.rs`）也是 flex 先推 status / preview，**最后** `flex2.push(composer)`，整块 dock 贴终端底；slash 属于 composer 内部下半区，不是塞进 history。

模块注释亦写明：status / pending preview 在 composer **上方**；popup 则在 composer 布局的 **下方矩形**。

#### 5.2.4 对照表

| 元素 | Codex（自上而下） | GA 现状 |
|---|---|---|
| status / activity | composer **上** | activity 在 BottomChrome 内、input 上（可保留） |
| composer / input | dock 主体 | `input` |
| slash / command popup | composer **下** | **slash 在 input 上** ❌ |
| footer key hints | 无 popup 时占 popup 区（composer **下**） | `hint` 在 input **上** |
| history | scrollback，与 dock 分离 | Static + 可变 live |

#### 5.2.5 修复方案（可与问题 A 同 PR 或紧随）

**最小改动（推荐先做）：** 调整 `inputChromeSections` 顺序为 Codex 族：

```ts
// 目标顺序（自上而下）
// error? → hint? → input → slashSuggestions|panel
return [
  ...(hasError ? ['error' as const] : []),
  'hint',
  'input',
  ...(hasPanel ? ['panel' as const] : hasSlashSuggestions ? ['slashSuggestions' as const] : []),
]
```

效果：

```text
[error?]
[hint]
[input]                 ← 输入框
[/help  /model  ...]    ← slash 在下（对齐 Codex）
```

**配套注意：**

1. **光标行计算**（`inputCursorPosition` / `visiblePanelRows`）  
   - 当前假设 panel 在 input 上，用 `panelRows` 参与“input 上方占用”。  
   - 顺序对调后，input 上方不再含 slash 高度，**必须同步改公式**，否则 IME 光标会偏到错误行。

2. **布局高度**  
   - `computeLayoutMetrics` 的 `bottomRows` 仍 = base + activity + panel，总和可不变。  
   - 变的是 **bottom 内部相对顺序**，不是总高度。

3. **其它 panel**  
   - 建议 mcp/model/workflow/selector 与 slash **同一策略：input 下**（一致 UX）。  
   - 若个别全屏选择器更适合“盖住上方”，可单独 `overlay` 模式，但默认 slash 必须在下。

4. **hint 文案位置**  
   - Codex 的 key hints 更偏 footer（composer 下）。  
   - GA 可二选一：  
     - **保守**：`hint` 仍在 input 上（只先挪 slash）  
     - **更贴 Codex**：`hint` 也挪到 input 下（无 slash 时 hint 在下；有 slash 时 hint 可与列表底提示合并）

5. **测试**  
   - 单测 `inputChromeSections`：有 slash 时顺序为 `… input … slashSuggestions`。  
   - App 测：注入 `/` 后，stdout 中 `>` 输入行出现在 `/help` 等建议行**之上**（或解析帧行号比较）。

#### 5.2.6 与问题 A 的关系

| | 问题 A（贴内容） | 问题 B（slash 上下） |
|---|---|---|
| 主文件 | `messageViewportPlan.ts` | `inputLayout.ts` + 光标公式 |
| 是否依赖对方 | 否 | 否 |
| 建议实施顺序 | 可并行；建议 **先 B（改动更小、体感即时）** 或同批 |

Dock 稳定后，slash 在 input 下会“贴在屏幕更底部”，更接近 Codex 整块底栏观感。

---

## 6. 详细设计（一期可实施）

### 6.1 改 `planMessageViewport`

当前：

```ts
if (liveLineCount > 0) return { kind: 'live', height: messageRows }
if (!hasStaticMessages) return { kind: 'ready' }
return { kind: 'none' }
```

建议：

```ts
// 始终为“底部 dock 上方”保留稳定高度，避免 composer 贴 Static 尾
if (liveLineCount > 0) {
  return { kind: 'live', height: Math.max(1, messageRows) }
}
// 无 live 内容时仍占位（空行 / ready 提示），高度与 live 槽一致
return { kind: 'live', height: Math.max(1, messageRows) }
// 或区分 ready 文案，但高度不要用 0
```

更保守的中间态：

```ts
if (liveLineCount > 0) return { kind: 'live', height: messageRows }
// 有历史也保留至少 minSpacer 行，而不是 0
const spacer = Math.max(minSpacerRows, Math.min(messageRows, preferredIdleGap))
return { kind: 'live', height: spacer }
```

产品可选参数（建议默认偏 Codex）：

| 参数 | 建议默认 | 含义 |
|---|---|---|
| `idleLiveSlotPolicy` | `fill-message-rows` | idle 时 live 槽 = 满 `messageRows` |
| 或 | `min-gap` | idle 至少 N 行（如 8）空白 |
| `streamingLiveSlotPolicy` | `fill-message-rows` | 与 idle 同高，避免 running 时再跳 |

**关键：idle 与 streaming 使用同一套高度公式**，只改槽内内容，不改槽高 → 输入框 y 坐标稳定。

### 6.2 `App.tsx` 渲染

- `inlineMessageViewportPlan.kind === 'none'` 分支删除或永不走到  
- 空 live 时 `MessageViewport` 仍以固定 height 渲染空白行（或 dim “Ready” 一行 + 其余空）  
- `liveViewportRows = header + stableMessageRows + bottomRows` 尽量接近 `terminal.rows - 安全边距`，使 chrome 贴底

### 6.3 与 `computeLayoutMetrics` 的关系

`bottomRows` 已含 input/activity/panel；`messageRows` 已是“除掉底栏后的剩余”。  
一期正确用法：

> **默认模式也始终消费 `messageRows` 作为 live/spacer 高度**，而不是仅在 `liveLineCount>0` 时才用。

这与 full 模式一致，只是内容来源仍是 active-only + Static history。

### 6.4 光标停放

`inputCursorPosition` 依赖 `messageRows`；稳定槽高后光标行计算更稳，IME 停车与 redraw 更少跳变。需回归：

- idle 输入中文  
- streaming 时输入框禁用态光标  
- Ctrl+C 清理 live 块高度与 `clearInlineLiveViewportSequence` 一致（高度变了要同步 geometry）

### 6.5 退出清理

`liveViewportGeometryRef.rows` 必须按**稳定后的** live 总高度计算，避免 Ctrl+C 清不干净或多清。

### 6.6 slash / panel 顺序（问题 B，一期建议同做）

#### 目标 DOM/渲染序（BottomChrome 内自上而下）

```text
activity（若有，可仍在 BottomChrome 外/上）
error?
hint?          // 可选：一期保留 input 上
input          // composer
slash|panel    // Codex: popup below composer
```

#### 代码落点

| 步骤 | 文件 | 动作 |
|---|---|---|
| 1 | `inputLayout.ts` | 调整 `inputChromeSections` 数组顺序 |
| 2 | `inputLayout.test.ts`（若无则新建） | 锁定 slash 在 input 后 |
| 3 | `App.tsx` `inputCursorPosition` 入参 | 重算：input 上方行数不再含 slash/panel |
| 4 | `layoutMetrics` / `visiblePanelRows` | 确认 bottom 总高仍容纳 panel；光标 y = header + message + activity + error + hint + inputTopBorder + cursorLine |
| 5 | `App.test.ts` | `/` 打开时建议行在输入边框**下方** |

#### 光标公式修正示意

当前（panel 在上）逻辑近似：

```text
cursorRow ≈ header + message + activity + error + panel + hint + border + cursorLine
```

改为 panel 在下后：

```text
cursorRow ≈ header + message + activity + error + hint + border + cursorLine
// panel 高度只增加 BottomChrome 总高与清理高度，不推高光标
```

`clearInlineLiveViewportSequence` / `liveViewportGeometryRef` 的 `rows` 仍应包含 panel，保证退出清整块 dock。

#### 可选二期：hint 下移

```ts
return [
  ...(hasError ? ['error'] : []),
  'input',
  ...(hasPanel ? ['panel'] : hasSlashSuggestions ? ['slashSuggestions'] : []),
  'hint',
]
```

更接近 Codex footer；需重新设计 hint 与 slash 底栏提示是否重复。

---

## 7. 二期（必须：从「满高 spacer」回到 Codex content-desired）

> 一期把「不要 none 塌缩」做成了「永远 messageRows」。二期目标改为：**dock 矮、贴内容；触底后固定**。

### 7.1 产品验收（人工 + 自测应对齐）

| # | 场景 | 期望（Codex 族） | 一期现状（过修） |
|---|---|---|---|
| 1 | 空会话 ready | composer 可在中下，上方可有呼吸空；**不必**强行贴最后一行 | 满高 ready 槽，composer 贴底 |
| 2 | 仅 1～2 轮短对话 idle | composer 在 **Static 最后一行下方不远处**（中间无大片空） | 中间 ≈ messageRows 空白 |
| 3 | 对话变长但未满屏 | 可见「内容尾 ↔ composer」间距稳定小；整体相对终端**下移** | 间距被 spacer 撑开 |
| 4 | 对话超过一屏 | composer **贴终端底**固定；向上滚可见本轮 user / 更早 history | 贴底，但本轮 user 常在 scrollback 更上方，中间仍隔 live 空槽 |
| 5 | streaming | 流式尾在 composer 上；不要无故把 composer 弹飞 | 满高 live 槽 |
| 6 | `/` | 列表在 input 下（一期 B，**保持**） | 已 OK |

### 7.2 架构方向（从易到难）

#### 方案 F — **content-desired live 槽**（Ink 内可先做，逼近体感）

不再：

```ts
height = messageRows  // 永远满
```

改为近似：

```ts
// dock 上方只保留「真正需要画的 live 行」+ 可选 minGap
const contentRows = liveLineCount > 0
  ? min(liveLineCount, maxLiveRows)
  : 0
const height = max(minGapRows, contentRows)  // minGap 建议 0～2，不要 messageRows
// idle + 仅有 Static：height → 0 或 1（占位），composer 贴 Static 尾
// 但要避免回到「running 前后高度乱跳」：streaming 与 idle 的规则要连续
```

要点：

- **Static 已是 history**：idle 时不必再在 live 区留满屏空。  
- **防贴飞**：用 `minGapRows`（0～2）代替满 `messageRows`。  
- **防跳动**：streaming 时 live 高度随行数增长，**上限** `maxLiveRows`（如 8～12），超出进 Static / 截尾（现有 `liveTranscriptViewportLines`）。  
- **触底感**：当 `header + liveH + bottomRows >= terminalRows` 时，live 截尾，composer 自然在底——模拟 Codex「贴底后固定」。

优点：仍用 Ink Static + Box，改动面小于完整 ANSI 引擎。  
风险：Ink 不会像 Codex 那样精确 `area.y += scroll`；短会话「整体下移」依赖 Static 自然推进 + 矮 dock，可能与 Codex 像素级路径不同，但**观感可接近**。

#### 方案 G — **真·Codex viewport_area + insert_history**（中长期）

1. 自管 `viewport_area = Rect { y, height: desired_height }`  
2. finalized 行走 `insert_history_lines`（scroll region + 可能 `area.y +=`）  
3. 每帧 `draw(desired_height)` 只重绘 dock  
4. 与 Ink 抢 stdout 需隔离（可能 dock 自绘 / 减小 Ink 职责）

工作量大，但是「先下移后固定」的**精确**实现。

#### 方案 H — **折中：idle 贴内容，streaming 有限高 live**

| 状态 | live 高度 |
|---|---|
| idle，有 Static | `0` 或 `1`（ready 文案可选） |
| idle，无 Static | `1`～`minGap` |
| running，有 stream | `min(streamLines, maxLive)` |
| running，尚无 stream | `minGap` 或 activity 占位，**不要** messageRows |

这是对一期 `fill-message-rows` 的直接回退修正，同时保留 slash 顺序与光标公式。

### 7.3 明确废弃的一期假设

| 一期假设 | 二期判定 |
|---|---|
| idle 与 streaming **同高 messageRows** 才能防跳 | **错误**。应同的是「相对内容尾的小间距策略」，不是同绝对满屏高 |
| 空 live 槽浪费屏幕 = 图1 有意留白 | **仅空会话**可有呼吸空；**有 Static 后**留白变成「离内容太远」 |
| 贴终端底 = 对齐 Codex | **只对触底后成立**；未触底时应贴**内容** |

### 7.4 仍可选的增强

1. 限制 streaming live 上限 + 超额 commit Static（类 Codex live commit）。  
2. 弱化 inline 模式重复 `GenericAgent` header。  
3. popup 高度 `Max(required)` + 内部滚动。  
4. hint 是否下移到 composer 下（Codex footer）——与间距问题独立。

---

## 8. 测试计划

### 8.1 单元

| 用例 | 期望 |
|---|---|
| `planMessageViewport({ hasStatic:true, liveLineCount:0, messageRows:17 })` | **不是** `none`；height 稳定为 17 或约定 spacer |
| `liveLineCount:5` 与 `0` 同 `messageRows` | **height 相同**（防跳动） |
| 无 Static 无 live | height 仍稳定（ready 与 idle 同高策略） |
| `inputChromeSections({ hasSlashSuggestions:true })` | 含 `input` 且 **`slashSuggestions` 下标大于 `input`** |
| 有 panel 无 slash | `panel` 在 `input` 之后（若采用统一策略） |

### 8.2 App 级（内存终端）

1. ready 帧：输入框 border 行号接近终端底（与现图1 类似）  
2. 注入长 Static 历史 + idle：输入框 border 行号与 (1) **相同或相差 ≤1**  
3. streaming 多行 assistant：输入框行号仍稳定  
4. 用户双显回归：Static 有 user、live 无 user 文本  
5. Ctrl+C 清理无残框  
6. **输入 `/`：帧内 `>` composer 行号 < 首条 slash 建议行号**（列表在下）  
7. **slash 打开时 IME/光标仍落在输入框内**（非落在列表行）

### 8.3 人工

1. 空会话看图1 体感  
2. 问一长句等完整回答后：输入框仍在下半屏，不贴回答末尾  
3. `/clear` 后回到稳定底栏  
4. 输入 `/`：列表在输入框**下方**，上下键选命令，Tab/Enter 补全  
5. 可选对照 Codex CLI 同终端高度截图（底栏 + slash）

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 空 live 槽“浪费”屏幕 | 这是图1 的有意留白；可用 `min-gap` 折中 |
| 长 stream 时 live 槽内滚动 | `liveTranscriptViewportLines` 已截尾；测 overflow |
| 清理序列高度不准 | 单测 `clearInlineLiveViewportSequence` + geometry |
| 与双显修复冲突 | 无：Static/user 策略不变，只加稳定高度 |
| slash 下移后光标错位 | 必须同步改 `inputCursorPosition` 入参；加 App 测 |
| 用户习惯了 slash 在上 | 文档说明对齐 Codex；可设临时 flag 回滚顺序 |

回滚：

- 问题 A：恢复 `planMessageViewport` 的 `none` 分支  
- 问题 B：恢复 `inputChromeSections` 旧顺序  

---

## 10. 实施步骤建议（TDD）

### 轨道 A — 输入框锚点

1. **RED**：`planMessageViewport` 有 Static、无 live 时 height 稳定。  
2. **RED**：App 长 history idle 与空会话输入 border 行接近。  
3. **GREEN**：改 `messageViewportPlan.ts` / App。  
4. 回归双显 grok + `npm test`。

### 轨道 B — slash 在输入框下（可先做，改动更小）

1. **RED**：`inputChromeSections` 断言 `index(slash) > index(input)`。  
2. **RED**：App 帧内 slash 行在 input 下；光标仍在 input。  
3. **GREEN**：改 `inputLayout.ts` + 光标公式。  
4. 人工 `/` 与 Codex 对照。

**预估：** 轨道 B 约 0.5 日；轨道 A 约 0.5–1 日；合计约 1–1.5 日。

---

## 11. 涉及文件

| 文件 | 角色 |
|---|---|
| `frontends/ink-ui/src/messageViewportPlan.ts` | **问题 A 主改**：取消 none 塌缩 / 统一槽高 |
| `frontends/ink-ui/src/App.tsx` | none 分支、liveViewportRows、cursor geometry；slash 打开时布局 |
| `frontends/ink-ui/src/layoutMetrics.ts` | 可能导出 `stableLiveRows` 策略 |
| `frontends/ink-ui/src/inputLayout.ts` | **问题 B 主改**：chrome 段顺序 |
| `frontends/ink-ui/src/inputLayout.test.ts` | 顺序单测（新建/扩展） |
| `frontends/ink-ui/src/terminalCursor.ts` / cursor helpers | 光标 y 与 panel 位置解耦 |
| `frontends/ink-ui/src/messageViewportPlan.test.ts` | 新建/扩展 |
| `frontends/ink-ui/src/App.test.ts` | 输入框纵向位置 + slash 上下关系 |
| Codex `chat_composer.rs` `Layout::vertical([composer, popup])` | slash 在下的权威参考 |
| Codex `bottom_pane/mod.rs` / `tui.rs` / `insert_history.rs` | 底 dock + scrollback 参考 |

---

## 12. 总结

- **现象 A（修前）**：`none` 塌缩 → 输入框贴 Static 尾、下半屏空。  
- **现象 A'（一期后）**：满高 live 槽 → 输入框永远贴终端底，**与对话内容过远**，本轮 user 常需上滚。  
- **现象 B**：slash 在 input 上 → 一期已改为 input 下。  
- **Codex 真实语义**（用户观察正确）：  
  - viewport **content-desired**（`desired_height`）；  
  - 未贴底时插入 history **下移** `area.y`；  
  - 贴底后 **y 固定**，历史在上方 scroll region 滚。  
- **一期有效部分**：禁止无策略塌缩；slash 顺序；光标/清理几何。  
- **一期错误部分**：把「稳定」理解成「永远 `messageRows`」。  
- **二期方向**：方案 F/H — content-desired 矮 dock + 触底固定；必要时再上方案 G 完整 `insert_history`。

### 12.1 文档修订记录

| 时间 | 变更 |
|---|---|
| 2026-07-14 | 初稿：诊断 A/B + 一期满高槽方案 |
| 2026-07-14 | 一期实施：稳定槽 + slash 下（进度文件） |
| 2026-07-14 | **用户反馈 A'** + Codex `desired_height` / `insert_history` 下移逻辑复核；修正目标与二期方案 |
| 2026-07-14 | **二期落地**：`planMessageViewport` content-desired（idle+static→none；stream min(lines,12,room)；ready=1）；253 tests + selfcheck PASS |
