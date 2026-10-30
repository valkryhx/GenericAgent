# GA goal 与 overlay：是否在面板内保留 goal 摘要行（设计决策）

日期：2026-10-30
范围：Ink UI（`frontends/ink/ui/src`），goal 卡片与 `/workflows`、`/team` 等 overlay 面板的共存。
结论先行：**不在 overlay 内保留完整 goal 卡片，但把完整卡片折叠成一行紧凑摘要**
（`◎ Goal active · running … · N turns · … tokens`）。overlay 关闭后恢复完整两行卡片。

---

## 1. 问题从哪来

GA 的 goal 卡片是一个**被动常驻元数据条**，挂在底部 secondary 带里，与 workflow 状态栏同层。
当前实现里 `showGoalCard` 与 `showWorkflowStatusBar` 都带 `!activePanel`：

```
showGoalCard = (...) && !activePanel && slashItems.length === 0
```

于是只要打开任一 overlay（`/workflows`、`/team`、hooks、model 等），goal 卡片就整块消失。
用户开着面板时，看不到「当前挂的是哪个 goal、跑了多久、几轮、花了多少 token」。

问题就是：**overlay 打开时，goal 上下文要不要保留？保留多少？**

## 2. CC 303 是怎么处理的（源码证据）

参考仓库 `D:/git_codes/claudecode283_hack`，303 反混淆产物。

### 2.1 `/goal` 是 local-jsx 命令，不是常驻状态栏

`reconstructed303/deminified/m1247_0xfbd7712.js:20-27`：

```
var e = { type: "local-jsx", name: "goal", description: "...", immediate: !0 }
```

命令 handler 在 `m2146_0x10d36021.js`（`var At = async (u,c,a) => {...} export { At as call }`）：

- 无参数时返回 `e(v, {...})`，即一个**命令结果 JSX**；
- Esc 触发 `onDone: () => u(void 0, { display: "skip" })`，即**关掉就不留痕**；
- active goal 分支用 `be` 组件渲染出 `"◎ Goal active"` 卡片（title/subtitle/inputGuide/children），
  达成态用 `be` 渲染 `"Goal achieved"`。

### 2.2 `be` 是 dialog 组件，不是一个内联行

`reconstructed303/deminified/m1369_0xfcd8bd9.js:353` 的 `function be(o)` 有 `onCancel`、
`inputGuide`、`exitOnCtrlCD`、并根据 dialog 高度裁剪 viewport（`rows - K`）。
它是占据一块 UI 区域的 dialog，不是 transcript 里的常驻一行。

### 2.3 关键：所有 immediate 命令结果共用同一个 `bottom` 槽位

`reconstructed303/deminified/m1635_0x101da730.js:67066`：

```
[lh.kind]: { layout: (h, R) => R === "fullscreen" ? "modal" : mN(h)?.immediate ? "bottom" : "inline" }
```

即：命令结果 → `fullscreen` 走 `modal`；`immediate` 走 `bottom`；否则 `inline`。

而 `/workflows`（`m1241_0xfbcf141.js:118-125`）、`/tasks`（`m0459:80180-80190`）同样是
`type: "local-jsx" + immediate: !0`，因此和 `/goal` **抢同一个 `bottom` 槽位**。
换句话说，CC 里打开 `/workflows` 就等于用 workflows 的命令结果**替换**掉 goal 的命令结果，
goal 卡片自然消失；关掉后也不会残留一行 goal 摘要。

### 2.4 子 agent 没有自己的 goal

`reconstructed303/deminified/m0671_0xf53ce11.js:3011`：

```
B = toolUseContext_4.agentId ? void 0 : toolUseContext_4.getAppState().activeGoal
```

`agentId` 存在（即子 agent）时 activeGoal 直接为 `undefined`。goal 只挂在主会话上，
子 agent / workflow 都是替主会话干活的执行体。这一点与 GA 目前的 goal 语义一致。

## 3. 结论：CC 不在 overlay 内保留常驻 goal 摘要行

理由链：

1. CC 的 goal 卡本身是 `/goal` 对话框，不是底栏；
2. 对话框与 `/workflows`、`/tasks` 共用唯一 `bottom` 槽位，天然互斥；
3. Esc/关闭即 `display:"skip"`，不产生残留；
4. 终态 goal 通过 transcript 内联 `goal_status` badge 保留（`m1556:7324`），不依赖 overlay；
5. 没有证据表明存在一个「在 overlay 里显示 goal 摘要」的常驻组件。

## 4. 为什么不直接照抄 CC（GA 的形态不同）

- **CC 的 goal 是对话框**：用户主动敲 `/goal` 才出现，看完按 Esc 就没了，本来就不承担「常驻可见」职责。
- **GA 的 goal 是被动元数据条**：用户在普通对话里设了 goal 之后，这条卡片是长期可见的 go-to signal，
  用户靠它确认「我现在还在为哪个条件跑」。它不是靠敲某个命令才出来。

如果 GA 直接采用「overlay 打开 → goal 卡片彻底消失」，那么用户一开 `/workflows` 或 `/team`
就丢失 goal 上下文。对 GA 这种「goal + 多智能体长跑」的主用法，这个丢失代价比 CC 大。

但也不该在 overlay 里塞整张两行卡：那会压缩面板空间、并让 overlay 语义变糊。

因此取折中：**overlay 打开时保留 1 行摘要，关闭后恢复整卡。**

## 5. 采纳的实现（最小、可测）

### 5.1 模型层 `goalCard.ts`

- 抽出 `goalStatusLine(meta, now)`：`◎ Goal active · running … · N turns · … tokens`；
- `goalCardLines` 复用它，行为不变（仍是两行）；
- 新增 `goalSummaryLine(meta, now): string | null`：有 goal 返回那一行，无 goal 返回 `null`。

### 5.2 渲染层 `App.tsx`

```
const showGoalCard = (...) && !activePanel && slashItems.length === 0
const collapsedGoalSummary = !showGoalCard && activePanel ? goalSummaryLine(state.goalMetadata, now) : null
const goalRows = showGoalCard ? (整卡行数) : collapsedGoalSummary ? 1 : 0
```

渲染时 secondary 带的顺序仍是「goal 卡 → workflow 状态栏 → collapsed 摘要 → activity」，
overlay 打开时 `showGoalCard=false`、落在一行的 `collapsedGoalSummary`。

### 5.3 布局预算 `layoutMetrics.ts`

不需要改代码：`goalRows` 已经参与 `activityRows = 1 + goalRows` 和面板可用高度计算。
overlay 打开时 `goalRows=1`，面板自动让出 1 行，仍按 `min(需求, 可用)` 裁剪，不会溢出。

## 6. 测试（真实渲染，不是纯字符串断言）

`frontends/ink/ui/src/goalMultiAgentUiRender.test.ts`：

- `team overlay collapses the goal card to one summary line; closing restores it`
  - 打开 `/team` overlay 后：帧里出现 `◎ Goal active`，**不出现**第二行 `Goal: …`；
  - 每行宽度 < 100；Esc 关闭后完整卡片（第二行）恢复。
- `/workflows overlay collapses the goal card to one summary line; closing restores it`（新增场景）
  - 打开 `/workflows` 后：出现 `Dynamic workflows` + `◎ Goal active`，不出现 `Goal: …`；
  - 宽度不溢出；Esc 关闭后完整卡片恢复。

`frontends/ink/ui/src/goalCard.test.ts`：
- `goalStatusLine` 与 `goalSummaryLine` 与整卡首行一致；摘要单行、无第二行、无 goal 时为 null。

`frontends/ink/ui/src/layoutMetrics.test.ts`：
- overlay 下 `goalRows=1` 恰好多占 1 行 bottom、messageRows 少 1 行，面板不溢出。

回归结果（2026-10-30）：`npm test` 全量 **510/510 通过**，`npm run typecheck` 通过。

## 7. 明确不做的部分

- 不在 GG 里给 goal 造一个「和 workflow/team 抢 bottom 槽位的对话框」——那是 CC 的形态，GA 不适配。
- 不在 overlay 里堆整卡（两行 + 动效统计），只留一行 header。
- 不改子 agent goal 语义 / defer 判定，本次纯 UI 折叠。

## 8. 后续可选加深（不在本次范围）

- 折叠行可加一个轻量 `Esc/关闭面板` 之外的入口提示（若形成习惯再评估）。
- 若未来 `/goal` 改成命令对话框模型，则退回「单槽位互斥」的 CC 方案，删除折叠行即可。

---

## 9. 追问复评：这一行到底该不该显示（2026-10-30 二次评估）

用户追问「总要让用户知道 goal 在执行，这一行是否隐藏」。结论：**不隐藏**。理由与补强如下。

### 9.1 判断标准

只要 goal 处于 active 或刚达成，**任何界面状态下用户都必须能一眼看到它**，否则会出现
「不知道还在为哪个条件跑」「跑完了没人告诉我」这两类体验缺陷。

### 9.2 实测发现两个此前遗漏的盲区（已修复）

用真实 `<App>` + fake bridge 抓帧验证（`temp` 探针，逻辑并入正式测试）：

- **盲区 A：输入 `/` 弹出命令补全时，goal 卡整块消失。**
  因为 `showGoalCard` 带 `slashItems.length === 0`，一敲 `/` 就置空，而补全弹窗不携带 goal。
  实测 `BEFORE_HAS_GOAL=true → AFTER_SLASH_HAS_GOAL=false`。
- **盲区 B：overlay 打开期间 goal 刚好达成，终态徽标也不显示。**
  原折叠只处理 active goal（`goalSummaryLine`），没处理终态；而终态徽标又被 `!activePanel` 挡掉。
  实测 overlay 打开时 `HAS_GOAL_DONE=false`。

### 9.3 修复：统一为「紧凑单行」`goalCompactLine`

- 新增 `goalCompactLine(meta, terminal, now)`：有 active goal → 返回 `◎ Goal active · …` 状态行；
  否则有终态 → 返回 `✓ Goal met · …` / `✗ Goal failed`；都没有 → `null`。
- `App.tsx`：`compactGoalLine = !showGoalCard ? goalCompactLine(...) : null`，
  于是 overlay 打开**和** `/` 补全弹出**任一**情况下，都用这 1 行保证 goal 可见；
  终态徽标沿用与整卡相同的 `running/stopping` 门控，避免新回合误显示旧徽标；成功/失败用 success/error 色。
- `goalRows=1` 只在确实有紧凑行时占 1 行，面板仍自动让位。

### 9.4 为什么不干脆隐藏（回应「隐藏」选项）

- goal 是 GA 的**常驻语义**（不是 CC 那种 `/goal` 命令对话框），隐藏就等于用户在开面板/打字时丢失目标；
- 保留代价只有 1 行，且不与面板/补全争位、不越界（已有宽度断言）；
- 因此采用「完整卡 ↔ 一行摘要在不同界面状态下切换」的渐进呈现，而不是二选一隐藏。

### 9.5 验证

`frontends/ink/ui` 全量 `npm test` **513/513 通过**，`npm run typecheck` 通过。新增/加强用例：
- `goal stays visible while the slash-completion popup is open`
- `goal reached while the team overlay is open still shows a badge`
- `goalCompactLine prefers active status, falls back to terminal badge`
- `/team`、`/workflows` overlay 折叠用例（上一节）
