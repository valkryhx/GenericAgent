# GA workflow：会话模式（standing opt-in）对寒暄轮误触发编排 —— 根因与修复

> 触发场景：用户此前开过 `/workflow on`（持久 session 模式）。之后只输入一句寒暄
> （实测为 `wondeful!`，拼写如此），GA 却把这句话判为 workflow 请求，重新启动了一次
> workflow 编排（用户困惑：「我不记得让它续跑核验第二次」）。
> 本文记录证据、根因、与 Claude Code 2.1.283 的对照，以及修复。
>
> 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。

---

## 1. 用户现象

用户在一次 workflow 测试后，只打了一句 `wondeful!`（口语致谢），期望主代理简单回应，
结果 GA 又跑了一整轮 workflow（`workflow_handoff` 第二次出现），用户并未主动要求「再核验一次」。

## 2. 会话证据（turn 时间线）

会话文件：`temp/sessions/session_23910f598e26450f9683b238a1c54c44.jsonl`（2026-10-14）。

| turn | source | 时间 | 内容摘要 |
|---|---|---|---|
| 1 | user | 17:41:31 | `[WORKFLOW OPT-IN]…[/WORKFLOW OPT-IN]` + 「使用 workflow 分别写 3 个 python 代码…」（signals: `workflow_mention`） |
| 2 | workflow_handoff | 17:42:33 | workflow run 完成，交回主代理作答 |
| 3 | **user** | 17:43:16 | **`[WORKFLOW OPT-IN]`（reason=explicit session workflow mode）+「wondeful!」** ← 用户实际只打了 `wondeful!` |
| 4 | workflow_handoff | 17:46:50 | 又一次 workflow run 完成 → 即用户困惑的「第二次续跑核验」 |

结论：第 3 轮把纯寒暄包装成 `[WORKFLOW OPT-IN]` 前缀并注入了 opt-in reminder，主代理据此
去直写脚本、启动了第 2 次 workflow。

## 3. 根因

`workflow_activation.py::resolve_workflow_activation()` 的 **session 分支**（修复前）：

```python
if session_enabled:
    return _decision("session", "requested", text, confidence=0.95,
                     signals=tuple(signals), reason="explicit session workflow mode",
                     fanout=True, phases=True)
```

只要 session 模式为 on，**任何一条消息**（包括 `wondeful!` 这种寒暄）都会命中
`session/requested`；随后 `frontends/ink_bridge.py`（约 335-358 行）在
`action == "requested"` 时注入 `workflow_opt_in_reminder()` 前缀，主代理就去编排 workflow。

也就是说：GA 的 standing opt-in **缺少 CC 的「寒暄轮不编排」门禁（conversational-turn gate）**。

## 4. Claude Code 2.1.283 对照证据

`claudecode283/extracted/workflow-authoring-reference.md` 第 16 行（**Ultracode** 段）：

> When a system-reminder confirms ultracode is on, that opt-in is standing: author and run a
> workflow for **every substantive task** by default. … **Solo only on conversational turns or
> trivial mechanical edits.**

即 CC 的 standing opt-in 明确把「conversational turns（寒暄/闲聊）」和「trivial mechanical edits
（琐碎机械改动）」排除在自动编排之外。GA 的 session 分支漏了这条 gate，所以把寒暄也编排了。

## 5. 修复

在 `workflow_activation.py` 的 session 分支加 conversational-turn gate，并新增判定 helper
（对齐 CC「只对 substantive task 编排」）：

```python
if session_enabled:
    if _is_conversational_turn(text):
        return _decision("none", "none", text, confidence=0.0, signals=tuple(signals),
                         reason="conversational turn: standing workflow mode does not auto-orchestrate chat")
    return _decision("session", "requested", text, confidence=0.95, ...)
```

`_is_conversational_turn(text)` 的判定顺序（保守、只拦明确的寒暄）：

1. 空串 / 去标点后为空 → 寒暄（True）。
2. `core`（去标点、小写）命中 `_CONVERSATIONAL_WORDS`（英文 ack + 中文客套词表）→ 寒暄（True）。
3. `core` 命中 `_TASK_TOKENS`（重构/优化/修复/refactor/fix…）→ 非寒暄（False，避免误杀短任务词）。
4. `core` 长度 ≤16、**不含 CJK**、且原文无 task signal（`_SEARCH`/`_ARTIFACT`/`_VERIFY`/`_SYNTHESIZE`）
   → 视为单词式寒暄（True，覆盖 `wondeful!` 这类拼写变体）。
5. 其余 → 非寒暄（False）。**短 CJK 词一律交给模型判断**，避免把「重构」类真任务误杀。

关键点：**只作用于 session 分支**。显式 `/workflow <task>`、自然语言 `用 workflow …`、
`_EXPLICIT_FANOUT`、以及 `len(signals)>=2 且有序列词` 的 semantic 分支都在此 gate **之前**返回，
不受影响。

## 6. 测试（focused）

`tests/test_workflow_activation.py` 新增 3 例：

- `test_session_mode_ignores_conversational_turns`：`wondeful!` / `wonderful` / `好的` / `thanks`
  在 session 模式下 → `('none', 'none')`。
- `test_session_mode_still_orchestrates_substantive_tasks`：`generate report` / `重构这个模块`
  在 session 模式下 → 仍 `requested`。
- `test_explicit_slash_workflow_unaffected_by_conversational_gate`：`/workflow generate report`
  → 仍 `('explicit', 'requested')`。

运行：

```
python -m pytest tests/test_workflow_activation.py -q
# 7 passed, 6 subtests passed

python -m pytest tests/test_ink_bridge.py -q -k "opt_in or workflow"
# 73 passed, 53 deselected, 3 subtests passed
```

`test_ink_bridge.py` 的既有断言（`test_submit_does_not_inject_opt_in_for_plain_task`、
`test_submit_injects_opt_in_reminder_for_explicit_workflow_request`）保持通过，未被本 gate 破坏。

## 7. 结论 / 不变量

- **能力 ≠ 授权**：session opt-in 只是「允许用 workflow」，不等于「每轮都编排」。
- **standing opt-in 只对 substantive task 生效**；寒暄轮 solo 回应（对齐 CC ultracode）。
- gate **只改 session 分支**，显式/自然语言/fan-out/semantic 入口行为不变。
- 云端不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
