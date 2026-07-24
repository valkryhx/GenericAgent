# GA UI 用户输入重复显示 — 根因分析

**排查日期：** 2026-07-14
**范围：** 默认 Ink UI（`ga` / `ga ink`，inline scrollback 模式）
**证据图：** `截图/ga_ui重复显示用户输入.png`
**修复状态：** 已按 Codex 对齐方案修复（`messagePartition`：done user 只进 Static，live 仅 `!done`）
**复现/回归测试：** `frontends/ink-ui/src/grok_user_input_duplicate.test.ts` + `messagePartition.test.ts`
**实施进度：** `docs/ga_ui_user_input_duplicate_display_implementation_progress_2026-07-14.md`

---

## 结论（先读）

| 项 | 结论 |
|---|---|
| 用户症状 | 提交一句用户消息后，同一条 `> 你是谁` 在界面上出现两次 |
| 根因类别 | **渲染分区时序 bug**，不是 bridge 双发 user 事件，也不是 React state 里 messages 数组真的有两条 |
| 直接根因 | 默认 inline scrollback 下，新 user 消息在 `status` 仍为 `idle` 时被当成 “已完成任务” 写入 Ink `<Static>`（永久进入终端 scrollback）；随后 `status: running` 又把同一条 user 放进 live viewport 再画一次 |
| 触发条件 | 默认鼠标模式（非 `GA_INK_MOUSE=full`）+ bridge 实际事件顺序 `user` → `status:running` |
| 是否 state 双写 | **否**。`state.messages` 对同一 `taskId` 只有一条 `role:user` |
| 是否后端重复 emit | **否**。`ink_bridge.submit()` 每个任务只 emit 一次 `type:"user"` |
| 自动化核验 | **已锁定**。`grok_user_input_duplicate.test.ts` 5/5 pass：生产顺序双显，对照顺序不双显 |

一句话：

> **同一条用户消息先后进入了两个互不撤销的渲染通道：先被 Static 永久写进终端 scrollback，再被 active live 区重绘。**

---

## 1. 截图观察

截图时间点特征（对应 running 中）：

1. 上方历史对话正常（`> 你是谁` / 助手回答 / 工具块 / 最终回答）
2. 历史下方又出现一条灰色底的 `> 你是谁`
3. 紧接着是 live 区标题 `GenericAgent running`
4. live 区内再次出现灰色底的 `> 你是谁`
5. 下方是 `Thinking...` spinner 与输入框

也就是说：**重复发生在“本轮刚提交、助手尚未完成”的窗口期**，不是历史恢复后的整段重放。

视觉分层也吻合当前架构：

- 上方那条 `> 你是谁`：已进入终端 scrollback 的 Static 输出
- 标题以下那条 `> 你是谁`：live viewport 的 activeMessages 输出

---

## 2. 相关架构背景（为何会有两条渲染通道）

2026-07-14 为对齐 Codex 可复制/可原生滚动体验，默认改成 **inline scrollback**（提交 `95e5dc4`）：

| 通道 | 组件 | 内容 | 是否可撤销 |
|---|---|---|---|
| finalized / scrollback | Ink `<Static>` | 已完成的 user/assistant/system 等 | **不可**（写进终端正常输出后就留在 scrollback） |
| active / live | `MessageViewport` | 当前 running/stopping 任务的 user + streaming assistant + composer/panel | 可随 React 重绘消失 |

分区函数：

```ts
// App.tsx
const keepLatestTaskActive = state.status === 'running' || state.status === 'stopping'
const messagePartition = splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive })
```

`splitStaticAndActiveMessages` 语义（`messagePartition.ts`）：

1. 找到最后一个带 `taskId` 的任务
2. 若该任务仍有 `done:false` 的消息，或 `keepLatestTaskActive===true`：
   - 从该 task 起的消息 → `activeMessages`
   - 之前已完成消息 → `staticMessages`
3. 否则（任务已全部 done，且不要求 keep active）：
   - **全部 done 消息进 static**，`activeMessages=[]`

设计意图：

- running 时：最新 user 应只在 live 区，和 streaming 助手在一起
- idle 且任务完成时：整轮落到 Static，进入终端 scrollback，便于原生选中/滚动

---

## 3. 证据链

### 3.1 bridge 每个任务只 emit 一次 user

`frontends/ink_bridge.py` → `submit()`：

```python
self.emit({"type": "user", "taskId": task_id, "text": visible_text})
self.emit({"type": "status", "status": "running", "taskId": task_id})
# ... put_task + 消费 display_queue
```

后续 `_consume_display_queue` 只发：

- `assistant_delta`
- `assistant_done`
- `status: idle`
- `token_usage` / `error`

**没有第二条 `type:"user"`。**

### 3.2 前端 state 对 user 是追加且按 taskId 定 id

`state.ts`：

```ts
if (event.type === 'user') {
  return {
    ...state,
    messages: [...state.messages, {
      id: `u-${event.taskId}`,
      role: 'user',
      text: event.text,
      done: true,
      taskId: event.taskId,
    }],
  }
}
```

要点：

- user 消息一进来就是 `done: true`（用户输入本身不“流式未完成”）
- id 固定为 `u-${taskId}`，不是随机重复追加
- **没有** “status 变化时再插入一条 user” 的逻辑

因此：**逻辑消息列表不重复；重复是渲染侧的。**

### 3.3 生产事件顺序与 “keep active” 条件错位

| 时刻 | bridge 事件 | `state.status` | `keepLatestTaskActive` | 新 user 落在哪 |
|---|---|---|---|---|
| T0 提交前 | — | `idle` | `false` | — |
| T1 | `user`（新 taskId） | 仍是 `idle` | `false` | **staticMessages**（被当成已完成） |
| T2 | `status: running` | `running` | `true` | **activeMessages**（又画一次） |
| T3 | `assistant_delta...` | `running` | `true` | active（user+assistant） |
| T4 | `assistant_done` + `status: idle` | `idle` | `false` | 整轮再归 static |

T1 时分区为何把新 user 当 static：

- 最新 task 只有一条 `done:true` 的 user
- `hasPendingMessage === false`（还没有 assistant 流）
- `keepLatestTaskActive === false`（status 还没变成 running）
- 走默认分支：全部 done 消息进 static

T2 时：

- status 变为 running → keep active
- 同一条 user 从 static 数组里“逻辑移除”，进入 active
- 但 Static 已经把 T1 的那一行 **永久打印到终端 scrollback** 了

### 3.4 Ink `<Static>` 的不可撤销语义（关键）

`node_modules/ink/build/components/Static.js` 核心行为：

```js
const [index, setIndex] = useState(0)
// 只渲染 index 之后的新增 items
items.slice(index)
// items.length 变化后把 index 推到 length
setIndex(items.length)
```

含义：

1. Static **只追加**，不按 item 身份更新/删除
2. 一旦某行被 Static 渲染到 stdout，就进入终端 scrollback
3. 之后即使 React 里 `staticMessages` 缩短（user 被挪到 active），**终端上那一行不会消失**
4. 因此 “先 static 后 active” 必然肉眼双显

这与设计文档对 Static 的说明一致（`docs/ga_ink_ui_text_selection_copy_codex_fix_plan_2026-07-14.md`）：finalized 写入正常 scrollback，live 只保留 active tail。问题不在 Static 本身，而在 **过早把未真正 finalized 的 user 当成 finalized**。

### 3.5 默认模式才会走这条路径

`App.tsx`：

```ts
const staticTranscriptRows = mouseMode === 'full' ? [] : ...
const activeTranscriptRows = mouseMode === 'full' ? [] : ...
// full 模式用单一 transcriptRows + MessageViewport，不经 Static 分区
```

| 模式 | 是否拆 Static/Active | 本 bug |
|---|---|---|
| 默认 inline scrollback（`mouseMode !== 'full'`） | 是 | **会** |
| `GA_INK_MOUSE=full` | 否（单一 viewport） | **不会**（同文只画一次） |

截图是默认 UI，符合会复现的路径。

### 3.6 现有测试掩盖了生产顺序

`App.test.ts` 里 streaming 稳定性测试是：

```ts
eventSink({ type: 'status', status: 'running', taskId: 1 })
eventSink({ type: 'user', taskId: 1, text: '保持输入区稳定' })
```

即 **先 running 再 user**。
生产 bridge 是 **先 user 再 running**。

因此：

- 测试路径下 `keepLatestTaskActive` 在 user 到达时已是 true → user 直接进 active → 不触发过早 Static
- 生产路径触发 bug
- **现有自动化不会红，但用户会看到双显**

`messagePartition.test.ts` 虽覆盖了 `keepLatestTaskActive:true` 时 user 应进 active，但没有覆盖 “user 事件到达时 status 仍 idle” 这一真实时序下的 App 级渲染后果。

后续已用独立 `grok_*.test.ts` 补上生产顺序复现（见第 3.7 节）；该文件只服务核验，不修改产品逻辑。

### 3.7 自动化复现与核验结果（2026-07-14）

为验证上述推断，**未改产品代码**，仅新增只读复现测试：

| 项 | 内容 |
|---|---|
| 测试文件 | `frontends/ink-ui/src/grok_user_input_duplicate.test.ts` |
| 运行命令 | `cd frontends/ink-ui && npx tsx --test src/grok_user_input_duplicate.test.ts` |
| 结果 | **5/5 pass** |
| 是否调用 LLM / mykey | 否。纯前端 state / partition / Ink 渲染路径即可闭环 |

#### 用例与断言

| # | 用例名 | 验证点 | 结果 |
|---|---|---|---|
| 1 | `grok: production order leaves a lone done user in static before status:running` | `user` 后、`status:running` 前：`keepLatestTaskActive=false`，`u-N` 进入 `staticMessages`，`activeMessages=[]`；`messages` 仅 1 条 user | pass |
| 2 | `grok: reverse event order (running then user) keeps user only in active` | 对照顺序 `running → user`：user 只在 `activeMessages`，不进 static | pass |
| 3 | `grok: App production order paints the same user into Static scrollback AND live viewport` | App 级模拟 `ink_bridge.submit`：先 `user` 再 `status:running`；stdout 中出现 **不含 GenericAgent 的 Static chunk**（含用户文本）+ **含 GenericAgent/running 的 live 帧**（再次含同一用户文本）；合并输出命中次数 ≥ 2 | pass |
| 4 | `grok: App reverse order does not write user into Static before live` | 对照 App 顺序 `running → user`：无“仅含用户文本”的 Static chunk；用户文本只出现在 live 帧 | pass |
| 5 | `grok: partition matrix documents the idle-user premature static rule` | 矩阵锁定：裸 `user(done)` + `keepLatestTaskActive:false` → 整段落 static；`true` → 最新 user 进 active | pass |

#### 实测 stdout 特征（用例 3）

生产顺序下可稳定观察到：

1. 至少一个 **Static 风格 chunk**：含唯一探针文本，**不含** `GenericAgent`
2. 至少一个 **live frame**：同时含 `GenericAgent`、`running`、同一探针文本
3. 合并 ANSI 剥离后的输出中，探针文本出现次数 **≥ 2**
4. 同期 state 层 user 条数仍为 **1**（双显来自渲染通道，不是 messages 双写）

对照顺序（用例 4）下：Static 风格 chunk 数为 **0**，用户文本只在 live 帧出现。

#### 对根因推断的判决

| 原推断 | 自动化判决 |
|---|---|
| 不是 bridge 双发 `user` / 不是 state 双写 | **成立**（partition/state 断言：始终 1 条 user） |
| 生产顺序 `user → running` 会在 running 前把 user 放进 static | **成立**（用例 1、5） |
| 随后 running 把同一 user 放进 active，形成第二渲染通道 | **成立**（用例 1 后半、用例 3） |
| Static + live 双通道导致 stdout 可见双显 | **成立**（用例 3：Static chunk + live frame） |
| 既有测试常用的 `running → user` 不触发双显 | **成立**（用例 2、4） |

> 结论：根因分析可视为 **已用自动化复现锁定**。修复后应把用例 3 的“≥2 次”断言反转为“恰好 1 次 / 无 Static 过早写入”，作为回归门禁。

#### 运行日志摘要

```text
TAP version 13
ok 1 - grok: production order leaves a lone done user in static before status:running
ok 2 - grok: reverse event order (running then user) keeps user only in active
ok 3 - grok: App production order paints the same user into Static scrollback AND live viewport
ok 4 - grok: App reverse order does not write user into Static before live
ok 5 - grok: partition matrix documents the idle-user premature static rule
1..5
# tests 5
# pass 5
# fail 0
```

---

## 4. 时序图

```text
用户按 Enter
  │
  ▼
bridge.submit()
  ├─ emit user(taskId=N, text="你是谁")     ──► UI dispatch
  │     state.messages += user(done=true)
  │     state.status 仍为 idle
  │     keepLatestTaskActive = false
  │     partition: user → staticMessages
  │     <Static> 把 "> 你是谁" 永久写入终端 scrollback   ← 第 1 次显示
  │
  └─ emit status(running, taskId=N)         ──► UI dispatch
        state.status = running
        keepLatestTaskActive = true
        partition: user → activeMessages
        live MessageViewport 再画 "> 你是谁"            ← 第 2 次显示
        （Static 已写出行不会被擦掉）

之后 assistant_delta / Thinking spinner 只影响 active 区
```

---

## 5. 排除的假设

| 假设 | 为何排除 |
|---|---|
| bridge 对同一提交 emit 了两次 `user` | `submit()` 源码只有一次 emit；consume 循环不发 user |
| `state.messages` 里有两条相同 user | `applyBridgeEvent` 按 taskId 追加一条；无二次插入 |
| `history_replace` / resume 重放导致 | 截图是新提交 running 中；resume 路径会 `resetStaticTranscriptGeneration`，且截图无恢复提示 |
| 输入框 echo 与 transcript 混淆 | 输入框是底部 composer；重复的是 transcript 样式的 `> ` + 灰底 user 行 |
| 仅主题/换行导致“看起来像两条” | 两条之间夹着 `GenericAgent running` 标题，是两个布局区域，不是同一行折行 |
| full mouse 模式逻辑回归 | full 模式不走 Static 分区；默认 inline 才走 |

---

## 6. 根因归纳

### 6.1 主因（充分条件）

**“是否 keep 最新 task 为 active” 只看 `state.status`，但 `user` 事件在 `status:running` 之前到达。**

组合效应：

1. user 消息模型上永远是 `done:true`
2. 没有 pending assistant 时，默认分区会把 “仅 user 的最新 task” 视为 finalized
3. finalized 走 Static → 不可撤销写屏
4. 下一事件才把 status 打成 running → 同一 user 再进 live

所以根因不是单一 if 写错，而是：

> **分区判定依赖的“任务进行中”信号（status）与“任务已创建”信号（user 消息）之间存在一帧/一次 dispatch 的窗口；Static 的永久写屏把这个窗口放大成可见重复。**

### 6.2 放大因素

1. **Static 不可回滚**：中间态一旦进 Static 就无法靠 React 撤销
2. **user 恒 done**：无法靠 `message.done` 表示 “已提交但仍属当前 active 轮”
3. **事件顺序固定为 user→running**：生产路径稳定踩中窗口
4. **测试用 running→user**：回归网漏掉生产顺序

### 6.3 与设计意图的偏差

inline scrollback 设计文档期望：

> 当前 running/stopping 任务的 **user prompt + streaming assistant** 保留在 live viewport；只有 finalized 才进 Static。

实际在 T1：

> 最新 user 在 status 仍 idle 时被当成 finalized 进了 Static。

故属于 **设计意图正确、实现时序未闭环**，而不是产品要双显。

---

## 7. 复现步骤（人工）

前置：默认启动（不要设 `GA_INK_MOUSE=full`）

1. 仓库根目录运行 `ga` 或 `ga ink`
2. 输入任意短句（如 `你是谁`）回车
3. 在助手开始输出前的 running 阶段观察 transcript

**预期：** 本轮 user 只出现一次（应在 live 区，或至少全局只一次）
**实际：** scrollback 区与 live 区各出现一次同一条 `> ...`

可选对照：

- `GA_INK_MOUSE=full ga`：同一操作通常不双显（单一 viewport）
- 若能人工插桩把 bridge 改成先 `status:running` 再 `user`：预期双显消失（验证时序假设；本报告未改代码）

---

## 8. 修复方向建议（仅分析，不实施）

按侵入性从低到高。**任一方案的验收都应以生产事件顺序 `user → status:running` 为准。**

### 方案 A — 前端：user 到达时强制 keep 最新 task active（推荐优先评估）

- 分区条件不只看 `status===running|stopping`
- 若最新 task 只有 user、尚无对应 assistant_done，也视为 active
- 或：`user` 事件本地乐观地把 status 视为 running 再分区

优点：不改 bridge 协议；贴合 “刚提交的 user 属于当前轮”
风险：需定义 “孤儿 user / 失败 put_task” 如何最终落入 static

### 方案 B — bridge：先 `status:running` 再 `user`

- 调换 `submit()` 两行 emit 顺序
- 与现有部分 App 测试顺序一致

优点：改动面小
风险：其他消费 bridge 事件的逻辑若依赖 “先有 user 再 running” 需排查；且 React 批处理仍可能在极端情况下合并，但通常 running 先到更安全

### 方案 C — user 消息在 running 期间标记未 finalized

- 例如 user 先 `done:false`，assistant_done/status idle 时再 `done:true`
- 或增加 `finalized`/`active` 显式字段

优点：分区语义更清晰
风险：协议/状态机面更广，rewind/history_replace 都要对齐

### 方案 D — Static 追加策略改为“确认 finalized 后再灌入”

- 不在每次 render 用当前 staticMessages 全量驱动 Static
- 仅在 status 回到 idle / assistant_done 时把本轮追加进 Static 队列

优点：从机制上消灭“中间态写 Static”
风险：实现更接近 Codex insert_history 模型，工作量大

### 明确不建议

- 在 live 区对已 Static 的 id 做字符串去重而不修时序：治标，且 scrollback 与 live 仍可能短暂双显
- 为消双显重新默认打开 full mouse / alt-screen：会回退可复制体验

---

## 9. 回归用例

### 9.1 已完成的复现测试（当前应继续失败语义 / 锁定 bug）

文件：`frontends/ink-ui/src/grok_user_input_duplicate.test.ts`

| 类型 | 内容 | 状态 |
|---|---|---|
| partition + state | 生产顺序 `user → running` 会过早 static；对照 `running → user` 不会 | 已绿（锁定现状） |
| App 级 stdout | 生产顺序 Static+live 双写同一 user 文本（≥2 次） | 已绿（锁定 bug） |
| App 级对照 | 反序不产生 Static 过早写入 | 已绿 |

说明：这些用例是 **bug 复现器**，不是修复后的期望行为门禁。修复落地后应改写断言方向（见 9.2）。

### 9.2 修复后建议的回归门禁（待产品修复时改写/迁入）

1. **App 级（默认 mouse off，生产顺序）**
   `ready` → `user(taskId=1)` → `status:running` →
   - 用户探针文本在 stdout 中 **恰好 1 次**（或：无“仅含用户文本、不含 GenericAgent”的 Static chunk）
   - live 帧可含该 user；Static 不得在 assistant_done 前写入该 user

2. **App 级 streaming 中**
   再发 `assistant_delta`，user 仍保持单次

3. **partition 单测（按最终产品语义二选一）**
   - 若采用方案 A：裸 `user(done)` 在无 assistant 时也应 active
   - 若采用方案 B：保留当前 partition，但 App/bridge 测试必须用生产顺序且不得双显

4. **失败路径**
   `user` 后 `put_task_failed` / 立刻 `status:idle`：最终只保留一条 user，不残留双通道

---

## 10. 涉及文件索引

| 文件 | 角色 |
|---|---|
| `frontends/ink_bridge.py` | `submit()` 事件顺序：`user` → `status:running` |
| `frontends/ink-ui/src/state.ts` | user 入 messages（`done:true`，单次追加） |
| `frontends/ink-ui/src/messagePartition.ts` | static/active 分区规则 |
| `frontends/ink-ui/src/App.tsx` | `keepLatestTaskActive`、Static + live 双通道渲染 |
| `frontends/ink-ui/node_modules/ink/build/components/Static.js` | Static 只追加、不可撤销 |
| `frontends/ink-ui/src/App.test.ts` | 测试用 running→user，掩盖生产顺序 |
| `frontends/ink-ui/src/grok_user_input_duplicate.test.ts` | **新增**：生产顺序复现与对照测试（5/5 pass） |
| `docs/ga_ink_ui_text_selection_copy_codex_fix_plan_2026-07-14.md` | inline scrollback 设计意图 |
| `截图/ga_ui重复显示用户输入.png` | 用户现场证据 |

---

## 11. 总结

这不是“用户消息被后端存了两遍”，也不是输入框和 transcript 的简单 echo 混淆。

真正的因果链是：

1. inline scrollback 把 **finalized** 与 **active** 拆成 Static / live 两通道
2. 最新 user 是否 active 依赖 `status===running`
3. 生产上 `user` 早于 `status:running` 一个 dispatch
4. 该窗口内 user 被误判 finalized → Static 永久写屏
5. status 到达后 user 再进 live → **同一条输入显示两次**

上述链路已由 `grok_user_input_duplicate.test.ts` 在 partition 层与 App 渲染层 **自动化复现锁定**（5/5 pass）。

修复应保证：**从 user 事件到达起，到本轮真正结束前，该 user 不得进入 Static。**
在做到这一点之前，任何只改样式/间距的处理都无法消除根因。
