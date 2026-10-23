# GA Ink 工具结果渲染修复：code_run 参数、stdout 结果通道与 CC 303 对照

- 日期：2026-10-23
- 范围：`D:/git_codes/GenericAgent`
- 对照源码：`D:/git_codes/claudecode283_hack/reconstructed303/deminified/`
- 触发材料：`bug/20261023_coderun_蓝色和黑色混合是哪里解析的问题嘛.png`、`bug/20261023_coderun_下面的黑色是啥能否讲讲.png`、`bug/20261023_coderun_中间有时候有灰色有时候没有.png`、`bug/20261023_claude_ui.png`

> 说明：这些异常截图拍摄于上一轮“首轮大输出遮挡”修复之前；本轮用**当前源码 + 真实 `<App>` 虚拟终端**复现，不依赖重新打包 GA。

---

## 0. 结论

三个现象是同一套“文本协议被当作 Markdown 混合渲染”问题的三个断面：

1. **蓝色 JSON + 黑色状态行**：`get_pretty_json()` 把 JSON 字符串内部换行替换成真实换行，参数块变成非法 JSON；再叠加 5 反引号围栏不匹配、stream-commit 把工具头与参数块拆开，导致原始 JSON 被 Markdown 当代码块绘制。
2. **`[Stdout]` 后正文黑色**：只有 `[Stdout]` 标记行被转成灰色 `⎿`，正文没有继承 result 通道，被 Markdown 当普通 assistant 正文渲染。
3. **`[Status]` 中间有时有灰、有时没有**：同一个逻辑结果块被 stream-commit 切成多条消息后，每条单独渲染；期间混入的行没有稳定的 result 通道状态，且每条消息各追加空行，出现断裂和双倍行距。

修复后同一段 `code_run` 在源码层渲染为：

```text
● code_run(script: import os print(1), timeout: 60, type: python)
  ⎿  Running python in GenericAgent: import os print(1)
  ⎿  Exit Code: 0
  ⎿  line one
      line two
```

不再出现青色原始 JSON、黑色 stdout、孤立空 `⎿`。

---

## 1. CC 303 的实际做法

CC 303 不是用一条正则解析 assistant 纯文本，而是**结构化 tool use / tool result 节点**：

- `m1556_0x1006d7b6.js`：按工具分发 `renderToolResultMessage()`；结果行使用 `"  ⎿  "` 前缀，正文用 `marginLeft: 5` 子块，整体 `dimColor`。
- `m2184_0x10ddcbdf.js`：工具进度只保留最后若干 tool use；每条 tool use 用 `height: 1, overflow: "hidden"` 的盒模型，传 `style: "condensed"`、`isStatic: true`。
- 工具名与结果分处不同节点，工具名 `bold`，结果通道单独缩进/变暗；不会发生“结果正文被 Markdown 当普通正文”的问题。

因此完全对齐 CC 需要 GA 把 `tool_use` / `tool_result` 从文本协议升级成事件节点。当前修复是**显示层归一化**：在不改 bridge 协议的前提下，把 GA 现有文本稳定映射到 CC 这套视觉语义。

---

## 2. 根因与修复

### 2.1 `get_pretty_json()` 破坏 JSON 语义

旧实现：

```python
data["script"] = data["script"].replace("; ", ";\n  ")
return json.dumps(...).replace("\n", "\n")
```

两个问题：

- `\n -> 真实换行` 让多行脚本的 JSON 非法；
- `"; " -> ";\n  "` 会改写脚本字符串内容，甚至可能改变 Python 源码语义。

现在只做 `json.dumps(..., ensure_ascii=False)`，保证参数块合法且与真实参数完全一致；折叠、摘要由显示层负责。

### 2.2 工具块识别

`messageFormat.ts`：

- 完整工具块匹配 3 个以上反引号，并要求闭合围栏长度不少于开启围栏；
- 兼容历史拆分形状：`● code_run` 后跟独立 ````text` 参数块；
- 历史 `get_pretty_json()` 还会把脚本里的字面 `
` 变成“反斜杠 + 真实换行”；`repairJsonStringNewlines()` 同时修复这种半解码形状，避免摘要里泄漏原始换行；
- `markStdoutBodiesInFences()` 只把 **4 个及以上反引号** 的状态围栏当作 GA 工具围栏；普通 3 反引号 Markdown 代码块即使正文出现 `[Status]` 也原样保留，不再被误剥；
- 新增 `foldOrphanToolArgsFence()`：无工具头的孤立参数块不再作为青色 JSON 代码块进入上下文；
- 流式未闭合参数块仍只保留一行工具 chip。

### 2.3 `[Stdout]` result 通道

`messageFormat.ts` 在剥离 4–5 反引号状态围栏**之前**，给 `[Stdout]` 的每个正文行加私用区标记 `STDOUT_BODY_PREFIX`。这样即使 stdout 中间有空行、`[omitted long output]`，渲染层也知道这些行属于 result 通道，而不是普通 assistant Markdown。

`messageWindow.ts`：

- `[Action]` / `[Status]` / `[Stdout]` 统一转为灰色 dim 的 `  ⎿  body`；
- `[Stdout]` 第一行绑定到 `⎿`，后续行使用 6 空格续行缩进；
- 空 `[Stdout]` 不再产生孤立空 `⎿`；
- `[omitted long output]` 保持灰色 result 样式。

### 2.4 跨 stream-commit 消息的结果通道

`transcriptLines()` 现在跨连续 assistant 消息维护 `ResultChannel`：

- 上一条消息以 `[Stdout]` 结束，下一条消息的正文继续按 stdout result 渲染；
- 跨消息时不再每段各插一个空行；只删除“前后都是 result 行”的分隔空行，保留正文空行和 stdout 内部的空行；
- `[Stdout]` 所在围栏关闭时写入内部结束哨兵 `STDOUT_BODY_END`，确保围栏之后紧跟的 assistant 正文不会继续被当成 stdout；
- 普通 assistant Markdown 代码围栏（如 ```` ```python ````）原样保留，不被误当成 GA 工具状态围栏剥掉；
- 遇到真实 `**LLM Running (Turn N) ...**` / `**Turn N ...**` 时也会退出 stdout 模式，下一轮正文恢复普通 Markdown。

此外，`transcriptLinesWithChannel()` 会返回渲染结束时的 result-channel 状态。`App.tsx` 渲染 Static 与 live 两段时把该状态从 Static 传给 live，解决“`[Stdout]` 标记恰好在 Static 末尾、正文在 live 开头”时仍被当成普通正文的问题。

这里有意**不**用宽泛的 `Summary:` / `Turn ...` 文本判断：命令行输出完全可能打印这些词，宽匹配会把正常 stdout 误判为下一轮。

---

## 2.5 H3：长 stdout 每个提交段丢首行（已定位并修复）

### 现象

120 行 stdout 在虚拟终端里固定丢 8 行；缺失位置为
`L006 / L022 / L037 / L052 / L068 / L083 / L098 / L112`，每次提交段只丢 **1 行**，
且恰好是**新提交段的首行**。PARK 开/关都出现，说明与 caret 定位无关。

### 排除项

- 数据层完整：`applyBridgeEvent` 之后 `a-1-c38 = L111`，live tail 从 `L112` 到 `L119`，
  一行不缺；
- 不是 Ink `eraseLines` 擦到 scrollback：`_probe_scrollback_vt.ts` 使用固定 24 行屏幕
  与不可变 scrollback，仍复现同样的 1 行丢失；
- 不是 `cursorPark`：`PARK=0` 与 `PARK=1` 缺失位置仅第一项不同，模式完全一致。

### 真正根因：`<Static>` 增量游标 + 非 append-only 渲染

Ink `<Static>` 的实现是：

```js
const itemsToRender = items.slice(index)
useLayoutEffect(() => setIndex(items.length), [items.length])
```

它假设 `items` **只追加**。但 `transcriptLinesWithChannel()` 原来在**整段消息合并后**
调用 `collapseResultSeparators(rows)`；每个 assistant 段末尾都会先压一个空行，
下一段到达后，折叠逻辑回头删除上一段末尾的那个空行：

```text
第 N 段渲染:  [... L005, " " ]              // 12 行
第 N+1 段:    [... L005, L006, L007, ...]  // 13 行，但第 12 行 " " 变了
```

已提交前缀长度从 12 缩成 11，`Static` 的 `index` 仍为 12 → `slice(12)` 跳过新段第一行。
这也解释了为什么每个 commit 恰好少一行，且都是段首行。

### 修复

`messageWindow.ts`：

1. 每条消息**独立渲染并独立折叠**后再拼接，保证已渲染前缀永远不变；
2. result/stdout 通道内不再追加段尾空行；
3. 空行改为在**下一个块开头**补（`appendResultBoundaryBlank`），保持 append-only，
   视觉上与原先一致：工具结果与后续用户输入/正文之间仍有空行；
4. 只有显式退出 result 通道（`STDOUT_BODY_END`、下一轮 header、孤立闭围栏）或
   下一条非 assistant 消息时才补边界空行，连续 stdout 正文保持紧凑。

### 确定性证据

```text
scripts/_tmp_prefix_series.ts（修复前）
n=4 prev=6 now=9  common=5 SHIFT   # 上一段尾部空行被删除
n=5 prev=9 now=12 common=8 SHIFT
n=6 prev=12 now=15 common=11 SHIFT

scripts/_tmp_prefix_series.ts（修复后）
n=4 prev=6 now=9  common=6 OK
n=5 prev=9 now=12 common=9 OK
n=6 prev=12 now=15 common=12 OK
```

回归测试新增：

```text
transcriptLines keeps the rendered prefix append-only across stream-commit segments
```

虚拟终端 120 行探针（固定屏幕 24 行 + 不可变 scrollback）：

```text
PARK=1  missing: []  dup: []   history: 107  screen: 24
PARK=0  missing: []  dup: []   history: 107  screen: 24
```

修复前同样探针：

```text
PARK=1  missing: L006,L022,L037,L052,L068,L083,L098,L112
PARK=0  missing: L009,L022,L037,L052,L068,L083,L098,L112
```

### 结论

H3 的“少行”不是终端擦除，也不是光标 park，而是 **Static 的 append-only 合约被
跨段折叠破坏**。修复后：
- 数据层、渲染层、终端层三层行数一致；
- 120 行 stdout 全量可见、无重复；
- 该修复同时消除“每个工具状态段首行偶发消失”的同类问题。

### 2.6 跟随修复：result 段间距与 `ctrl+o`

逐段折叠后暴露出两个相邻问题，已一并收口：

1. **`[Status]` 与下一条 `[Stdout]` 之间被插空行**
   分隔空行现在统一归属**下一条消息的开头**；前后两块都是 result 行时不插。
   这样既保持 Static append-only，又符合 CC 303 连续 tool-result 无空行的视觉。
2. **`ctrl+o` 展开/折叠工具参数会破坏 Static**
   展开状态改变会重排已写入 `<Static>` 的行；仅改 React state 会让旧行留在
   scrollback、新行被跳过。现在 `ctrl+o` 先 `resetTranscriptViewport()` 整屏重置，
   再 `resetStaticTranscriptOutput()` 重挂 `<Static>`，整段历史只重印一次。

live viewport 的 pin 逻辑也同步修正：提示词后的分隔行归属 prompt header，
但至少保留一行给 assistant 输出，避免小视口里提示词把答案挤掉。

---

## 3. 代码落点

| 文件 | 变更 |
| --- | --- |
| `genericagent/core/loop.py` | `get_pretty_json()` 不再改写参数内容，输出合法 JSON |
| `frontends/ink/ui/src/messageFormat.ts` | 3+ 反引号工具块、历史拆块重接、孤立参数块折叠、stdout body 标记 |
| `frontends/ink/ui/src/messageWindow.ts` | result 通道渲染、跨消息状态延续、`⎿` 首行/续行规则；**逐消息折叠以保证 Static append-only**；result 边界空行前移 |
| `frontends/ink/ui/src/streamCommit.ts` | 提交边界不得把 `[Stdout]` 与其正文拆到两条消息 |
| `frontends/ink/ui/src/App.tsx` | Static 与 live 分段渲染时传递 result-channel 状态；`ctrl+o` 整屏重置 + 重挂 Static |
| `frontends/ink/ui/src/messageFormat.test.ts` | 5 反引号、拆块重接、历史非法 JSON、isolated args 回归 |
| `frontends/ink/ui/src/messageWindow.test.ts` | stdout 正文 dim、无空 `⎿`、跨 message 的 stdout 延续、下一轮 header 退出 stdout、**Static 前缀 append-only 回归** |
| `tests/test_agent_loop_parallel_tools.py` | `get_pretty_json()` 合法性与“不改写脚本字符串”回归 |

### 3.1 与 CC 303 的映射边界

| CC 303 结构 | GA 当前实现 |
| --- | --- |
| 独立 `ToolUse` 节点，工具名 bold | 文本正则识别 `⏺ Tool: ...`，输出 `● tool(args)` |
| `renderToolResultMessage()` 按工具分发 | 统一 `[Action]/[Status]/[Stdout]` result 通道 |
| `marginLeft: 5` 子块 + `dimColor` | 6 空格续行 + `color: muted` + `dimColor` |
| tool use `height: 1, overflow: hidden` | 折叠为一行 chip，展开由 `ctrl+o` 控制 |
| `ellipsis +N lines` | `... (+N more lines) (ctrl+o to expand)` |

**边界要说清**：当前不是“完全复刻 CC 结构化渲染”，而是“在 GA 文本协议上做到视觉与语义一致”。若后续要把工具结果升级成事件节点，本层的 result-channel 判定与折叠逻辑可以原样复用。

---

## 4. 验证

源码测试（不依赖打包）：

```text
cd frontends/ink/ui
npx tsc --noEmit                # passed
npm test                        # 462 passed / 0 failed
```

Python：

```text
python -m pytest -q tests/test_agent_loop_parallel_tools.py
# 15 passed
```

真实 `<App>` + 虚拟终端探针：

```text
npx tsx scripts/_probe_exact_screenshot.ts
> 跑一下
● code_run({ "script": "import os,re ...") (ctrl+o to expand)
  ⎿  Running python in GenericAgent: ...
  ⎿  Exit Code: 0
  ⎿  (./worktrees/..., 52)
      (./worktrees/..., 52)
      (./worktrees/..., 52)
```

```text
npx tsx scripts/_probe_stdout_visual3.ts
> 跑一下
我先执行一个脚本。
● code_run(script: print(1))
  ⎿  Running python in GenericAgent: print(1)
  ⎿  Exit Code: 0
  ⎿  60e4340 feat(packaging): ship installable GA wheel
      dc07912 feat(hooks): implement Claude Code compatible hook runtime
```

说明：

- 探针 1 使用的是**历史非法 JSON**，所以摘要显示为 `{ "script": ...` 的兼容路径；新 runtime 产出的合法 JSON 会显示 `script: ..., timeout: ..., type: ...`。
- 两个探针都确认没有青色原始 JSON、没有黑色 stdout、没有孤立空 `⎿`。

---

## 5. 剩余风险与后续

1. **完全结构化**：当前仍是文本协议上的归一化。若 GA 后续暴露 `tool_use` / `tool_result` 事件，应把 result 通道改为显式节点，避免任何文本歧义。
2. **`[omitted long output]`**：这是 `genericagent/core/handler.py` 的后端截断（`max_str_len=600`），不是 UI bug；本次只保证它按 result 样式显示。
3. **历史 transcript**：老会话里已保存的非法 JSON 仍由前端兼容解析；新会话不再产生非法 JSON。
4. **打包验证**：本轮只验证源码与虚拟终端；重新打包 GA 后应再用真实终端跑一次 `code_run` 大脚本 + 多行 stdout + 下一轮对话。

---

## 6. 验收清单

- [x] 工具参数 JSON 合法，`json.loads()` 可还原原始参数
- [x] `code_run` 多行脚本不再显示原始青色 JSON
- [x] `[Stdout]` 正文、`[omitted long output]` 均为灰色 result 样式
- [x] 空 `[Stdout]` 不产生孤立 `⎿`
- [x] stream-commit 拆分后 stdout 仍是同一 result 通道（提交边界也禁止切在 `[Stdout]` 之后）
- [x] 围栏结束后正文、下一轮 `LLM Running (Turn N)` 均恢复普通正文样式
- [x] 开围栏在 Static、闭围栏在 live 的拆分段也能退出 stdout（孤立闭围栏不落成正文）
- [x] 连续 `[Status]` 行之间不再双倍空行
- [x] Static/live 分段边界的 stdout 状态可延续
- [x] 每段提交后已渲染前缀不变（Static append-only），长 stdout 不再每个 commit 丢首行
- [x] 120 行 stdout 在 PARK=1 / PARK=0 的 scrollback 虚拟终端中 `missing: [] / dup: []`
- [x] `[Status]` 与后续 `[Stdout]` 连续、不插空行；跨 commit 的 result 块仍无幻影空行
- [x] `ctrl+o` 切换工具展开时整屏重置 + 重挂 Static，scrollback 不重复、不丢行
- [x] 普通 3 反引号 Markdown 代码块中的 `[Status]` 不被误剥
- [x] 历史“反斜杠 + 真实换行”参数不会泄漏到工具摘要
- [x] `npx tsc --noEmit`、`npm test`（458）、Python 回归全绿
