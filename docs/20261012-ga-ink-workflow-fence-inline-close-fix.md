# GA Ink Workflow：Turn 3/4 `**LLM Running ...**` 字面量渲染修复

**日期：** 2026-10-12
**现象截图：** `bug/1.png`、`bug/2.png`、`bug/3.png`
**真实会话：** `temp/sessions/session_a0df2e7356e54111b99aa8eccf4ada56.jsonl`

## 现象

用户执行 `/workflow` 并行调研任务时：

- Turn 1 / Turn 2 的 `LLM Running` 正常加粗；
- Turn 3 / Turn 4 显示为字面量 `**LLM Running (Turn N) ...**`；
- 后续 `🔨 Tool: ...` 和工具参数 fence 看起来与 Turn 标题混在同一段；
- 实际 workflow 仍在后台运行，问题只发生在 Ink transcript 渲染层。

## 真实 transcript 证据

取会话第 2 条 `turn` 的 `assistant_text`，Turn 2 的 `workflow` 工具结果附近为：

```text
`````
[Workflow] launched model-availability-research (...); use /workflows to monitor.`````
```

注意最后一行：结束的 5 个反引号没有独立成行，而是直接拼在 `[Workflow]` 正文之后。

随后是：

```text
**LLM Running (Turn 3) ...**

🔨 Tool: `update_working_checkpoint`  📥 args:
````text
```

## 根因

### 1. 工具输出没有结尾换行

`ga.py:767` 的 `do_workflow()` 原先输出：

```python
yield f"[Workflow] launched {workflow_name} ({run_id}); use /workflows to monitor."
```

没有结尾 `\n`。

`agent_loop.py::_run_single_tool()` 的 verbose 包装是：

```python
yield "`````
"
yield first
...
yield "`````
"
```

正常情况下应该生成：

```text
`````
[Workflow] ...
`````
```

缺少换行后实际生成：

```text
`````
[Workflow] ... monitor.`````
```

### 2. stream-commit 可以从这行 fence opener 开始切段

长 `workflow` 工具参数块被 `commitStreamingAssistantMessages()` 切成多个 `a-{taskId}-c*` 段。其中一段正好从：

```text
`````
```

开始，然后包含：

```text
`````
[Workflow] ... monitor.`````

**LLM Running (Turn 3) ...**

🔨 Tool: `update_working_checkpoint`  📥 args:
````text
```

`formatAssistantText()` 会把 4/5 反引号统一压成 3 反引号，但无法识别“结束 fence 和正文在同一行”。`marked.lexer` 最终只看到：

```markdown
```
[Workflow] ... monitor.```

**LLM Running (Turn 3) ...**
```

第一个 ` ``` ` 被当作 code fence 开启，后面没有独立闭合行。于是 Turn 3、Turn 4 以及后续 Tool 行全部落入 code block，`**LLM Running ...**` 被原样显示。

## 复现与验证

用真实 `assistant_text` 按 16 字符 delta 重放 `applyBridgeEvent`，结果：

```text
Turn 2: LLM Running (Turn 2) ...       → bold ✅
Turn 3: **LLM Running (Turn 3) ...**   → code/gray，字面量 ❌
Turn 4: **LLM Running (Turn 4) ...**   → code/gray，字面量 ❌
```

只把：

```text
monitor.`````
```

改成：

```text
monitor.
`````
```

再重放：

```text
Turn 3: LLM Running (Turn 3) ...       → bold ✅
Turn 4: LLM Running (Turn 4) ...       → bold ✅
```

## 修复

### 后端：关闭 fence 前保证换行

`agent_loop.py`：

- 顺序工具 `_run_single_tool()`：记录最后一个非空输出 chunk；关闭 ` ````` ` 前，如果该 chunk 不以 `
` 结尾，先补 `
`。
- 并行工具 `_run_parallel_tools()`：worker 在最后一个输出 chunk 后补 `\n`，再发送 outcome；这样关闭 fence 永远落在独立行。

`ga.py`：

- `do_workflow()` 的 `[Workflow]` 和 `[Warn]` 输出显式补结尾 `\n`，从源头修正 workflow 工具输出。

### 前端：兼容历史 transcript

`frontends/ink-ui/src/messageFormat.ts` 在通用 fence 归一化前，修复已经落盘的畸形形式：

```text
`````
[Workflow] ...`````
```

以及对应的 `[Warn]` 形式，将其规范为：

```text
`````
[Workflow] ...
`````
```

这样即使用户 resume 或回看旧会话，也不会再因为历史 transcript 中的畸形 fence 把后续 Turn 吞进 code block。

### stream-commit：按 Markdown 语义识别 fence

`frontends/ink-ui/src/streamCommit.ts` 原先的 fence 状态机只要看到 3 个以上反引号就 toggle，不区分：

- opener 的 fence 长度；
- 4 反引号 ````text` opener 在 5 反引号块内只是内容，不能关闭 5 反引号块；
- 行尾内联关闭（`[Workflow] ...````` `）是 closer，不是普通文本。

因此它误以为 `[Workflow]` 后面的 ````text` 已经关闭了 5 反引号块，于是允许在 `[Info]` 状态块的真正 closer 前切段。下一段以这个 closer 开头，Markdown 会把它当成 opener，再次吞掉 Turn 4。

修复后 `fenceOpenState()`：

- 记录当前 opener 的 fence 长度；
- 只有长度 >= opener、且位于独立 fence 行（或 GA legacy 行尾内联关闭）的行才关闭；
- 短 fence、带 info string 的 fence 在已打开块内都视为内容。

这样 stream-commit 的切点不会把真正的 closer 留给下一段当 opener。

## 回归测试

- `tests/test_agent_loop_parallel_tools.py`：工具输出不带结尾换行时，关闭 fence 仍独立成行。
- `frontends/ink-ui/src/messageWindow.test.ts`：真实畸形 `[Workflow] ...````` ` 段经 `transcriptLines()` 后，`LLM Running (Turn 3)` 必须识别为 bold，不能是 code/gray 字面量。
- `frontends/ink-ui/src/streamCommit.test.ts`：legacy 内联关闭 + 嵌套 ````text` 场景下，stream-commit 不得把 `[Info]` closer 留给下一段，Turn 4 必须仍为 bold。

## 验收命令

```bash
python -m unittest tests.test_agent_loop_parallel_tools
python -m unittest discover -s tests
npm --prefix frontends/ink-ui test
npm --prefix frontends/ink-ui run typecheck
```

实测结果（2026-10-12）：

- `python -m unittest discover -s tests`：**1325 OK (skipped=3)**；
- `npm --prefix frontends/ink-ui test`：**407 pass / 0 fail**；
- `npm --prefix frontends/ink-ui run typecheck`：通过；
- 真实 `session_a0df2e7356e54111b99aa8eccf4ada56.jsonl` 按 16 字符 delta 重放：Turn 3、Turn 4 均恢复为 `bold: true`。

## 不涉及

- 不修改 workflow runtime / scheduler / 子 agent 语义。
- 不引入 Claude Code 云端会话实现；云端 `isolation:"remote"` / `Ho()` 仍不在复刻范围。
