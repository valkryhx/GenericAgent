# GA Ink UI 文本无法选中/复制 排查报告

**排查日期：** 2026-07-14  
**范围：** `frontends/ink-ui` 主界面（默认 `ga` / `ga ink`）展示内容的鼠标选中与复制  
**修复状态：** 诊断结论已用于实施；当前默认修复已落地，本报告保留为根因依据。

> **2026-07-14 Codex 源码复核补充：** 后续只以 `D:\git_codes\codex\codex-rs\tui` 为主要参考。二次复核确认：Codex 主聊天的滚轮滚动和拖动右侧滑块滚动，主要来自终端正常 scrollback/scrollbar，而不是 Codex 捕获鼠标实现的应用内滚动条。`1007` alternate scroll 主要服务于进入 alternate screen 的 overlay/pager 场景。GA 已按该结论实施默认 inline scrollback：finalized transcript 写入终端 scrollback，底部只保留 composer/active tail/panel 的 inline viewport；`GA_INK_MOUSE=full` 仍保留旧应用内鼠标路径。详细结果见 `docs/ga_ink_ui_text_selection_copy_codex_fix_plan_2026-07-14.md`。

## 结论（先读）

| 项 | 结论 |
|---|---|
| 用户症状 | 在 GA UI 界面上无法用鼠标拖选 transcript 等内容，也无法把界面上已显示的文本复制出来 |
| 根因类别 | **终端鼠标捕获（mouse tracking）接管了本应由终端原生处理的选区**，不是 React/Ink 把文字“画成图片”，也不是剪贴板 API 写坏 |
| 直接根因 | 启动时无条件开启 DEC 鼠标模式 `1000/1002/1003/1006`，且输入处理对**所有**鼠标 press/drag/release 一律吞掉；应用层又**没有**实现自己的选区/复制 |
| 次要相关 | 全屏 alternate screen（`1049`）使原生 scrollback 不可用，选中只能依赖当前可见屏；这会放大“没法选中”的体验问题，但**单独不会**禁止选区 |
| 是否 bug | 是。当前行为是“为了滚轮/滚动条拖拽而开启鼠标报告”，副作用是**永久剥夺终端原生选中/复制**，且没有补偿路径 |

一句话：

> **鼠标事件被 GA 吃掉了，但 GA 又不做选区；所以用户既不能用终端原生选中，也不能用应用内选中。**

---

## 1. 问题与观察对象

### 1.1 用户描述

- 对象：本项目的 **GA UI**（默认 Ink 终端界面）
- 现象：界面上展示的内容**无法选中**来复制粘贴
- 主观感受：内容好像“不可复制”

### 1.2 范围界定（本报告默认）

| 路径 | 是否在本次范围内 |
|---|---|
| `frontends/ink-ui` + `frontends/ink_bridge.py`（`ga` / `ga ink`） | **是，主对象** |
| 输入框粘贴（bracketed paste → `[Copied text #N]` 折叠） | 否。那是**粘贴进输入框**，不是从 transcript 复制出去 |
| `ga cli` / Textual / Qt / Streamlit | 否。用户说的是当前默认 GA UI |

### 1.3 可复现步骤（人工）

在 Windows Terminal / VS Code 集成终端中：

1. 仓库根目录启动 `ga` 或 `ga ink`
2. 让 transcript 区域出现任意可见文本（欢迎信息、历史消息、assistant 输出均可）
3. 在消息区域用鼠标左键拖选一段文字
4. 尝试 `Ctrl+C` / 右键复制 / 终端“复制”动作

**预期（正常终端/多数 CLI）：**  
出现选区高亮，复制后可粘贴到别处。

**实际（当前 GA Ink UI）：**

- 拖选时**通常看不到**终端原生选区高亮
- 复制剪贴板中**得不到**选中的 transcript 文本
- 滚轮仍可滚动消息区；点右侧滚动条仍可拖动滚动位置（说明鼠标事件确实进了应用）

> 说明：部分终端在 **按住 Shift** 时会强制走“原生选区旁路”（override application mouse tracking）。若用户未按 Shift，则完全无法选中——这与根因一致，也解释了“好像永远不能复制”。

---

## 2. 排查方法与证据链

本次按 systematic debugging / diagnose 流程做**只读根因调查**，未改代码。

### 2.1 反馈环（用于验证“是不是鼠标捕获导致”）

| 层级 | 检查点 | 结果 |
|---|---|---|
| L1 启动序列 | 是否进入 alt-screen + 是否 enable mouse tracking | `enterMainScreenTerminalSequence` = alt-screen + `mouseTrackingOn()` |
| L2 鼠标模式集合 | 开了哪些 DEC 模式 | `1000h + 1002h + 1003h + 1006h`（按键/拖拽/任意移动 + SGR） |
| L3 输入分发 | 鼠标事件是否被应用消费 | `handleTerminalInput` 对 wheel / press / drag / release **全部 return**，不回落给终端 |
| L4 应用层能力 | 是否有 selection state / copy-on-select / OSC 52 | **无**（全仓 ink-ui 无 clipboard/selection 实现） |
| L5 对照参考 | Claude Code 同类全屏路径怎么做 | 同样开 mouse tracking，但 **Ink 内建 selection + copy**；并提供 `CLAUDE_CODE_DISABLE_MOUSE` 关闭捕获以恢复原生选中 |

自动化测试现状：

- 有测试锁定 **mouse tracking 必须开启**：`terminalCleanup.test.ts`、`mouseWheel.test.ts`
- **没有**任何测试断言“用户可以选中/复制 transcript”
- 因此现有测试通过**不能**证明复制可用；它们实际上在固化“鼠标被应用接管”的行为

建议后续 agent 的最小可机判信号（本报告不实施）：

1. 序列断言：若修复策略是“默认关闭 button/drag 捕获”或“提供旁路”，则 `mouseTrackingOn()` / 启动序列应可区分 wheel-only vs full capture。
2. 行为断言：press/drag 在非滚动条区域**不应**被无条件吞掉（或应进入 selection 状态机）。
3. 人工/半自动：启动后在 Windows Terminal 拖选可见文本，剪贴板非空。

### 2.2 代码证据

#### 证据 A：启动时无条件开启完整鼠标跟踪

`frontends/ink-ui/src/mouseWheel.ts`

```ts
export function mouseTrackingOn(): string {
  return '[?1000h[?1002h[?1003h[?1006h'
}
```

含义（xterm / DEC private mode）：

| 模式 | 作用 |
|---|---|
| `1000` | 报告按键 press/release 与滚轮 |
| `1002` | 按住按钮时的 drag（button-motion） |
| `1003` | 任意鼠标移动（即使未按键，hover） |
| `1006` | SGR 编码（`CSI < btn;x;y M/m`） |

其中 **`1000 + 1002`（再加 SGR）** 就会让终端把左键拖选变成“发给应用的鼠标事件序列”，而不是原生选区。`1003` 进一步扩大捕获面，对选中复制没有帮助，只会增加噪声事件。

#### 证据 B：进入主屏时把 mouse tracking 绑在 alt-screen 上

`frontends/ink-ui/src/terminalCleanup.ts`

```ts
export const enterMainScreenTerminalSequence =
  `${enterAlternateScreen}${mouseTrackingOn()}`
export const exitTerminalCleanupSequence =
  `${mouseTrackingOff()}[0m${showCursor}\r[2K${exitAlternateScreen}`
```

`App.tsx` 挂载时：

```ts
stdout.write(enterMainScreenTerminalSequence)
```

退出时会 `mouseTrackingOff()` + 退出 alt-screen，说明设计者知道这些模式需要成对清理；**但运行期间没有“临时让出鼠标给终端选区”的路径**。

#### 证据 C：输入处理吞掉所有鼠标事件，且只服务滚动

`frontends/ink-ui/src/App.tsx` → `handleTerminalInput`：

1. 若是 wheel → 改 `transcriptScrollOffset` → `return`
2. 若是任意 `parseMouseEvent` 命中：
   - `release` → 清 `scrollbarDragRef` → `return`
   - `wheel` → `return`
   - `press`/`drag` → 仅当命中滚动条列时更新 scroll offset → **无论是否命中，最后都 `return`**

关键语义：

```text
鼠标事件进入 GA
  → 永远不会落到“未处理输入”
  → 终端原生选区永远收不到这次 drag
  → GA 自己又不记录 anchor/focus 选区
  → 用户无法选中
```

滚动条命中逻辑见 `transcriptScrollbar.ts` 的 `shouldHandleScrollbarDrag` / `isScrollbarColumn`：  
**只有最右 1–2 列**用于拖滚动条；消息正文区域的 press/drag 被识别后直接丢弃。

#### 证据 D：应用层没有 selection / clipboard 补偿

在 `frontends/ink-ui/src` 内检索：

- 无 `selection` 状态机
- 无 `copy` / `clipboard` / OSC 52 写入
- 无“选中高亮”渲染层
- 现有 `paste.ts` 的 `Copied text #N` 是 **bracketed paste 折叠占位符**，方向是“系统 → 输入框”，不是“界面 → 系统剪贴板”

因此不存在“应用内选中后自动复制”的隐藏实现。

#### 证据 E：Ctrl+C 被占用为退出，不是复制

`App.tsx`：

```ts
if (key.ctrl && (rawInput === 'c' || rawInput === '')) {
  bridgeRef.current?.send({ type: 'shutdown' })
  exitCleanly()
  return
}
```

即使将来做了应用内选区，**Ctrl+C 当前语义是退出进程**，不能当“复制”。复制快捷键需要另定（例如许多 TUI 用 OSC 52 在 mouse-up 时自动 copy，或提供显式命令）。

#### 证据 F：测试在固化“必须开鼠标跟踪”

`terminalCleanup.test.ts`：

- 断言 `enterMainScreenTerminalSequence` 包含 `1006h`
- 断言 `reassertMouseTracking` 写出完整 `1000/1002/1003/1006`

`mouseWheel.test.ts`：

- 断言 `mouseTrackingOn()` 精确等于上述四模式

这些测试解释了为什么问题会“看起来像功能”：滚轮与滚动条拖拽被当作目标能力，**选中复制从未进入验收范围**。

### 2.3 参考实现对照（Claude Code 本地 fork）

路径：`D:\git_codes\claude-reviews-claude\claude-code-fork\src`

| 点 | Claude Code | GA Ink UI |
|---|---|---|
| alt-screen | 有（fullscreen 路径） | 有（默认全屏） |
| mouse tracking | 有 `1000/1002/1003/1006` | 相同集合 |
| 应用内 selection | **有**完整 `selection.ts` + highlight + drag-to-scroll 累积 | **无** |
| copy-on-select | **有**（注释明确提到 `useCopyOnSelect` / clipboard） | **无** |
| 关闭鼠标以恢复原生选中 | `CLAUDE_CODE_DISABLE_MOUSE=1`：保留 alt-screen + 虚拟滚动，**跳过 mouse capture**，让 tmux/kitty/终端原生 copy-on-select 工作 | **无对应开关** |
| 文档意识 | 明确写：mouse capture 会破坏 terminal-native copy-on-select | 设计文档只要求“保持鼠标滚动”，未讨论复制 |

Claude Code `fullscreen.ts` 关键注释（摘录意译）：

> `CLAUDE_CODE_DISABLE_MOUSE=1` 用于保留 alt-screen + 虚拟滚动，但**跳过鼠标捕获**，以便 tmux/kitty/终端原生 copy-on-select 继续工作。

这说明业界同类产品已经把本问题识别为 **mouse tracking 的已知副作用**，并用“应用内选区”或“可关闭捕获”两条路之一处理。GA 目前两条都没有。

---

## 3. 假设排序与证伪

| # | 假设 | 预测 | 结果 |
|---|---|---|---|
| H1 | 鼠标 tracking 导致终端不再做原生选区，且应用无补偿 | 启动序列含 1000/1002；press/drag 被吞；无 selection 模块 | **成立（主因）** |
| H2 | alternate screen 单独导致无法复制 | 仅 1049、无 mouse tracking 时仍应能选当前屏文字 | **不足以单独成立**；alt-screen 主要影响 scrollback，不取消当前屏选区 |
| H3 | 文本被渲染成不可选图形/特殊控件 | Ink 输出的是 ANSI 字符单元，不是 bitmap | **不成立** |
| H4 | 剪贴板权限/Windows 策略问题 | 同一终端里 `ga cli` 或关闭 mouse 后应同样失败 | **与当前代码路径不符**；未观察到应用尝试写剪贴板 |
| H5 | 只有滚动条逻辑 bug，正文 drag 应已可选 | 正文 drag 在 `if (mouseEvent) { ... return }` 内无条件返回 | **不成立**；正文区域也被吞 |

**已确认根因 = H1。**

---

## 4. 因果链（数据流）

```text
用户在 transcript 上按下并拖动鼠标
        │
        ▼
终端处于 DEC 1000/1002/1006（及 1003）
        │  不再启动原生选区，改为发送 SGR 鼠标报告
        ▼
stdin 收到 CSI <btn;x;y M/m
        │
        ▼
parseMouseEvent / parseMouseWheel
        │
        ├─ wheel → 滚动 transcript → return
        ├─ press/drag 在滚动条列 → 改 scrollOffset → return
        └─ press/drag 在正文区 → （什么都不做）→ return
        │
        ▼
无 selection state 更新
无高亮渲染
无 clipboard 写入
        │
        ▼
用户看不到选区，复制不到文本
```

并行事实：

- 输入框的 **粘贴** 工作（bracketed paste）→ 说明“键盘/粘贴通道”正常
- 滚轮与滚动条拖拽工作 → 说明“鼠标报告通道”正常且被应用占用
- 两者结合，精确定位到 **占用了鼠标但没用它做选区**

---

## 5. 与“粘贴”能力的易混淆点

| 能力 | 现状 | 文件 |
|---|---|---|
| 把系统剪贴板 **粘贴进** 输入框 | 支持（bracketed paste，多行折叠为 `[Copied text #N +K lines]`） | `paste.ts`, `inputController.ts` |
| 把界面 transcript **复制出** 系统剪贴板 | **不支持** | （缺失） |
| 终端原生拖选复制 | **被 mouse tracking 阻断** | `mouseWheel.ts`, `terminalCleanup.ts`, `App.tsx` |

用户说的“无法复制粘贴”在本问题上应拆成：

1. **复制出**界面内容 → 坏（本报告对象）
2. **粘贴进**输入框 → 好（不要误修）

---

## 6. 非根因 / 排查时排除的项

- Markdown 渲染 / `wrap="truncate-end"`：只影响显示样式与截断，不禁用选区。
- 安全画布少画一列（`terminalCanvasColumns`）：布局/换行问题，与选中无关。
- bridge / Python 后端：UI 选中发生在 Node/Ink 终端层，不经过 bridge。
- subagent 视图：与 2026-07-13 滚动条报告相同，subagent 文本也走主 transcript renderer；复制问题是**全局**的，不是 subagent 专属。

---

## 7. 修复方向建议（供 review，本报告不实施）

按侵入性从低到高。**推荐优先评估 A 或 B；完整体验对齐 Claude Code 则走 C。**

### 方案 A — 默认只保留滚轮，不捕获 click/drag（最小改动）

**做法概要：**

- `mouseTrackingOn()` 改为近似 **wheel-only**（实践中仍常需 `1000+1006` 才能收到 wheel；但可避免 `1002` button-motion / `1003` any-motion 的强捕获，或提供“仅在滚动条 press 时短暂开启 1002”）
- 更干净的工程做法：默认 **不 enable 1002/1003**；滚动条拖拽改为键盘/点击 jump，或接受“只能点轨道不能拖”
- 提供环境变量 / 配置：`GA_ENABLE_MOUSE_DRAG=1` 恢复当前行为

**收益：**

- 终端原生拖选/复制恢复（多数 Windows Terminal / WT / iTerm 场景）
- 改动面小，不必实现 selection 渲染

**代价：**

- 可能削弱或失去**拖动滚动条滑块**（点击 jump 可保留）
- `1000` 仍可能在部分终端干扰选区；需实机验证 Windows Terminal 是否要求“完全关闭 mouse tracking”才能原生选中

**风险点：**  
Windows Terminal 上“开着 1000 是否仍允许 Shift-select / 普通 select”存在终端差异，**必须以实机为验收**，不能只靠单元测试。

### 方案 B — 保留完整 mouse tracking，但增加“关闭开关”恢复原生复制（对齐 Claude Code 的 escape hatch）

**做法概要：**

- 增加 `GA_DISABLE_MOUSE=1`（或 `/config`）  
  - true：不写 `mouseTrackingOn()`，wheel 不可用时依赖 PgUp/PgDn  
  - false：保持现状
- 文档写明：需要复制长文本时关闭鼠标捕获

**收益：** 实现最快，行为可解释  
**代价：** 默认体验仍然不能复制；只是给高级用户出口

### 方案 C — 应用内 Text Selection + copy-on-select（完整体验，工作量大）

**做法概要（参考 Claude Code `ink/selection.ts`）：**

1. 维护 anchor/focus 选区状态（screen 坐标）
2. press 开始、drag 更新、release 结束
3. 渲染层对选中 cell 做 inverse/highlight
4. release 时把选中文本写入剪贴板：
   - 优先 OSC 52（远程友好）
   - Windows 可回退 `clip.exe` / PowerShell
5. 与滚动条 drag 抢事件时：滚动条列优先给 scrollbar；正文给 selection
6. **不要**把 Ctrl+C 改成复制（当前是退出）；copy-on-select 更合适

**收益：** 全屏 + 虚拟滚动 + 可复制，三者兼得  
**代价：**

- 需要可靠的 screen buffer 或“从当前可见 `TranscriptLine[]` + 布局几何反查字符”的 hit-test
- 当前 GA 用的是上游 `ink@5`，**没有** Claude Code fork 里那套 selection/screen API，不能直接抄文件；要在现有 React 树外自建几何模型
- 工作量显著高于 A/B；易引入 off-by-one、宽字符、markdown parts 选中边界 bug

### 方案 D — 混合（推荐产品形态）

1. **默认**：关闭 button-drag 捕获或默认 `GA_DISABLE_MOUSE` 类行为，保证原生复制  
2. **需要滚动条拖拽时**：显式开启  
3. **中长期**：若要坚持默认全功能鼠标，再投入方案 C  

这与 Claude Code 文档中的权衡一致：mouse capture 与 native copy-on-select 本质冲突，必须二选一或自建 selection。

---

## 8. 建议的验收标准（给实施 agent）

### 8.1 必须

1. 用户在 Windows Terminal 中，对 transcript 可见文本执行普通左键拖选，**能出现选区**（原生或应用内高亮二选一，但必须可见）。
2. 复制后，系统剪贴板内容包含所选文本（允许行尾空白规范化）。
3. 输入框粘贴多行文本的既有行为不回归（`paste.ts` 相关测试仍绿）。
4. 退出时仍清理 mouse tracking + alt-screen（防终端“粘住”鼠标报告）。

### 8.2 应该

5. PgUp/PgDn 滚动仍可用。
6. 若保留滚轮：滚轮仍滚动 transcript。
7. 若保留滚动条拖拽：与选区命中区域不互相误伤（右列 vs 正文）。
8. 文档/`--help`/README 中说明复制方式与相关环境变量。

### 8.3 明确不要做的

- 不要把“修复复制”做成删除整个 alt-screen（会带回 scrollback 与布局抖动问题）。
- 不要改 bridge 协议或 Python 后端来“修选中”。
- 不要破坏 bracketed paste 折叠。
- 不要在未实现 copy-on-select 前，把 Ctrl+C 从“退出”改成“复制”。

---

## 9. 关键文件清单

| 文件 | 角色 |
|---|---|
| `frontends/ink-ui/src/mouseWheel.ts` | 定义开启/关闭哪些鼠标模式；解析 SGR/X10 事件 |
| `frontends/ink-ui/src/terminalCleanup.ts` | 启动序列绑定 alt-screen + mouse on；退出清理 |
| `frontends/ink-ui/src/App.tsx` | `handleTerminalInput` 吞掉全部鼠标事件；Ctrl+C 退出 |
| `frontends/ink-ui/src/transcriptScrollbar.ts` | 仅滚动条列消费 press/drag |
| `frontends/ink-ui/src/paste.ts` | 仅输入方向粘贴；易与“复制”混淆 |
| `frontends/ink-ui/src/mouseWheel.test.ts` | 固化完整 mouse tracking 序列 |
| `frontends/ink-ui/src/terminalCleanup.test.ts` | 固化启动/重断言 mouse tracking |
| （缺失）selection / clipboard 模块 | 根因的另一半：无补偿实现 |

参考（只读对照，非本仓）：

- `claude-code-fork/src/utils/fullscreen.ts` — `isMouseTrackingEnabled` / disable mouse 以恢复 native copy
- `claude-code-fork/src/ink/selection.ts` — 应用内选区状态机
- `claude-code-fork/src/ink/termio/dec.ts` — 与 GA 相同的 1000/1002/1003/1006 组合
- `claude-code-fork/src/ink/components/AlternateScreen.tsx` — alt-screen 与 mouseTracking 可选开关

---

## 10. Review 时建议重点质疑的问题

实施前请 review agent 明确拍板：

1. **产品默认站哪边？**  
   - 默认可复制（牺牲部分鼠标手势）  
   - 还是默认可拖滚动条/全鼠标（必须自建 selection）
2. **Windows Terminal 实机**：方案 A 是否真能恢复普通拖选，还是仍需完全关闭 1000？
3. **是否需要 Shift-select 提示？**  
   即便短期不修，UI 底部提示 “Hold Shift to select text” 也能降低“坏了”的误判（部分终端支持）。
4. **远程/SSH 场景**是否要 OSC 52？若只保证本机 Windows，可先 `clip`。
5. 现有测试把 `1000+1002+1003+1006` 写死——修复必然改测试；review 应防止“为了让测试绿而保持无法复制”。

---

## 11. 总结

GA Ink UI 无法选中复制，**不是显示层把文字变成不可选控件**，而是：

1. 启动时开启了完整 DEC 鼠标报告；  
2. 输入循环把所有鼠标事件当作应用事件消费掉；  
3. 消费结果只服务于滚动，**从不建立选区，也不写剪贴板**；  
4. 因此终端原生选区与应用内选区同时缺失。

**根因已定位，可修；本报告故意止于诊断，供其他 agent review 后再实施。**

---

## 12. Codex 源码复核补充：必须同时恢复选区与滚动

本节为后续补充，覆盖第 7 节中“可接受滚动退化”的旧表述。用户明确要求：**修复后必须保持滚动能力**。

### 12.1 Codex 如何避免选区冲突

只读检查 `D:\git_codes\codex\codex-rs\tui` 后确认：

- Codex 主 TUI 默认没有启用 `EnableMouseCapture`，也没有写 DEC `1000h/1002h/1003h/1006h`。
- `tui.rs` 进入 alternate screen 时写 `\x1b[?1007h`，注释为 “Enable alternate scroll so terminals may translate wheel to arrows”。
- `tui.rs` 离开 alternate screen 时写 `\x1b[?1007l`。
- `tui/event_stream.rs` 明确把不使用的事件跳过，注释包含 “mouse events, etc.”；鼠标事件不会进入主应用交互模型。
- Codex 的 pager/list 滚动主要是键盘模型，不提供主 transcript 的鼠标拖动滚动条。

因此 Codex 的关键做法不是“捕获鼠标后自己实现选区”，而是：

```text
不捕获鼠标拖拽
  → 终端原生选区继续可用
启用 DEC 1007 alternate scroll
  → 支持该模式的终端把滚轮转成 Up/Down
应用把键盘事件用于滚动
  → 滚动仍可用
```

### 12.2 GA 推荐修复更新

默认模式：

1. 不启用 DEC `1000/1002/1003/1006`。
2. 启用 DEC `1007`。
3. 在无 active panel、无 selector、无 slash suggestions、输入为空时，把 Up/Down 解释为 transcript line scroll。
4. PgUp/PgDn 继续作为可靠 page scroll。
5. 右侧滚动条默认保留为视觉位置指示器，不处理鼠标拖拽。

兼容模式：

1. `GA_INK_MOUSE=full` 恢复当前 full mouse capture。
2. full 模式保留滚轮事件解析和右侧滚动条拖拽。
3. full 模式下普通拖选会被鼠标捕获影响，这是显式 opt-in 的取舍。

这能解决“用户提问和 GA 输出能否自由选中”的问题：默认模式下这些内容仍是终端字符，鼠标拖拽不再被 GA 接管，终端可以直接产生原生选区。滚动通过 `1007` 转换出的方向键、PgUp/PgDn 和后续 `/copy` 补充能力保留。

### 12.3 必须验证的边界

- `1007` 是否在 Windows Terminal、VS Code terminal、tmux 嵌套环境中都按预期把滚轮转成 Up/Down，需要人工验收。
- `1007` 产生的 Up/Down 与物理方向键通常无法可靠区分；如果要保证滚轮滚动，空输入状态下 Up/Down 需要优先给 transcript scroll，而不是输入历史。
- 默认模式不能同时支持正文普通拖选和右侧滚动条拖拽。拖拽滚动条必须放在 `GA_INK_MOUSE=full`，或未来实现完整应用内选区引擎。

---

## 13. Codex 二次复核：主聊天滚轮和右侧滑块来自终端 scrollback

用户补充观察：当前 Codex CLI 中，鼠标滚轮可以上下滚动屏幕，按住鼠标左键拖动右侧滚动条滑块也能上下滑动屏幕。基于源码二次确认后，需要修正一个关键判断：**这两项能力在 Codex 主聊天界面主要不是应用内鼠标交互，而是终端原生 scrollback 能力。**

### 13.1 源码证据

`codex-rs/tui/src/tui.rs` 初始化注释：

```rust
/// Initialize the terminal (inline viewport; history stays in normal scrollback)
```

`codex-rs/tui/src/insert_history.rs` 文件注释：

```rust
//! Inserts finalized history rows into terminal scrollback.
//!
//! Codex uses the terminal scrollback itself for finalized chat history, so inserting a history
//! cell is an escape-sequence operation rather than a normal ratatui render.
```

`insert_history_lines_with_wrap_policy()` 将 finalized history 写到 inline viewport 上方，并用 terminal scroll region 维护底部 viewport 位置。也就是说，Codex 主聊天历史实际进入了终端正常 scrollback。

同时：

- `tui.rs` 的 `set_modes()` 没有启用 `EnableMouseCapture` 或 DEC `1000/1002/1003/1006`。
- `tui/event_stream.rs` 注释明确跳过未使用事件，包括 mouse events。
- `pager_overlay.rs` 的 transcript overlay 是进入 alternate screen 后的 keyboard pager，只维护 `scroll_offset` 和底部百分比，不实现应用内可拖动滚动条。
- `tui.rs` 的 `1007h/1007l` 用于 alternate screen 场景，让终端把滚轮翻译为方向键；它不是主聊天右侧 scrollbar 滑块拖拽的来源。

### 13.2 对 GA 修复建议的影响

如果 GA 只关闭 mouse capture 并开启 `1007`：

- 能恢复普通拖选复制。
- 能在支持 `1007` 的终端中保留滚轮滚动。
- 不能得到 Codex 主聊天那种拖动终端窗口右侧 scrollbar 滑块滚动历史的体验，因为 GA 仍在 alternate screen 内维护自己的虚拟 transcript。

如果 GA 要对齐 Codex 当前 CLI 体验，应改为：

1. 默认不进入 alternate screen。
2. finalized transcript 写入终端正常 scrollback。
3. 底部输入框、状态栏、活动 streaming tail、面板维持一个 inline viewport。
4. 鼠标滚轮、终端右侧 scrollbar 拖拽、普通文本拖选全部交给终端原生处理。
5. 全量 transcript overlay 或 raw/copy-friendly view 可继续使用 alternate screen + `1007` + keyboard pager。

### 13.3 推荐分阶段

短期低风险：

- 默认关闭 `1000/1002/1003/1006`。
- alt-screen 中启用 `1007`。
- Up/Down 在空输入主界面路由到 transcript 滚动。
- `GA_INK_MOUSE=full` 保留旧应用内鼠标滚动和滚动条拖拽。

中期 Codex-parity：

- 新增默认 inline scrollback 模式。
- 将 finalized transcript 从 Ink `MessageViewport` 迁移到终端 scrollback。
- 保留底部 inline viewport。
- resize 时从源消息重建 scrollback，避免换宽后历史错位。
- 将当前右侧自绘滚动条降级为 alt-screen/legacy/full 模式功能；默认 inline 模式不需要应用内滚动条。
