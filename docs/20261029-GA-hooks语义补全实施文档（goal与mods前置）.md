# GA hooks 语义补全实施文档（goal 与 mods 的前置地基）

日期：2026-10-29
状态：Stage A–D 已实现并提交（Stop 门禁 + 会话级动态 hook + 守卫 fail-closed + 结构化输出）；阶段 E–G 仍为设计稿
范围：GenericAgent 本地 hooks 运行时的语义补全；为 goal 模式提供“回合内可阻塞 Stop”，为后续 mods 提供“可动态装载的事件面”。
关联材料：
- GA 侧差异分析：`docs/20261028-GA-hooks机制与CC303差异分析.md`（提交 `4099a2c`）
- CC 侧权威坐标：`claudecode303/extracted/20261020-hooks入口分析.md`、`20261020-mods入口分析.md`
- goal 调研：`claudecode283_hack` 提交 `606be7a1`（CC goal 实现调研）、`f1a69959`（求值器 JSON 失败实证）
- GA goal 方案：`docs/20261028-GA-goal模式设计与实施方案.md`

---

## 1. 结论先行

1. GA 的 hooks **执行面已经对齐 CC**（33 事件词表、5 种 handler、matcher/if、http 策略、async、审计），**语义面没对齐**。
2. 最关键的缺口只有一条会“卡死 goal”：**`Stop` 不是“模型这轮想停”的门禁，而是“用户回合收尾”的观察式通知，且返回值被丢弃。**
3. 另有三处“看起来有、实际没接通”的假象，必须先在文档层面点破：
   - `STOP_LIKE_EVENTS` 常量存在但**从未被使用**（只 import，无读取）；
   - `HookAggregate.prevent_continuation` 会被解析赋值，但 **core 从不读取**；
   - `handler._done_hooks`（空轮续跑队列）**只被初始化与读取，全仓库没有任何地方写入** → 是死机制，不是现成挂点。
4. 因此本次是一个**分层 feat**，不是一个补丁：先补 hook 语义底座，再把 goal 作为“一条会话级动态 Stop hook”落上去，最后让 mods 作为“动态装载 hooks 的来源”接进来。顺序不能倒。

一句话：**hook 语义是地基，goal 是第一层楼，mods 是第二层楼。本文只打地基并预留两层接口。**

---

## 2. 相关提交复核（逐一对应到源码证据）

| 提交 | 仓库 | 性质 | 复核结论 |
| --- | --- | --- | --- |
| `4099a2c` | GenericAgent | 仅文档（+210 行） | 结论与当前代码一致（经全文核对，无 `_done_hooks` 相关表述）；其“Stop 单点缺失”的判断成立 |
| `606be7a1` | claudecode283_hack | 仅文档（CC goal 577 行） | CC goal 机制 = 一条会话级动态 Stop hook + 求值器；坐标可用 |
| `f1a69959` | claudecode283_hack | 仅文档（+137/+5） | 弱模型求值器严格 JSON 会失败（14 条 `hook_non_blocking_error`）；GA 必须给结构化通道 |
| `dc07912` | GenericAgent | feat：CC 兼容 hook runtime | 五 handler / matcher / 审计 / async 落地 |
| `9a65cfc` | GenericAgent | feat：async rewake + HTTP 策略 | 与 CC `asyncRewake`、http allowlist 对齐 |
| `16d9484` | GenericAgent | feat：PermissionRequest 接入 ask | 权限决策闭环（工具级）已有 |
| `e0c6b8e` | GenericAgent | fix：Stop 用最终 assistant 文本 | 说明 Stop 只在 `run()` 收尾发，至今未升级为门禁 |

> 关键：`4099a2c` 是**只读分析**，没有任何 hook 代码改动。本轮所有修复都还没开始。

---

## 3. 源码复核结论（本轮实测，非引用）

以下每条都附文件:行，便于 review 与回归。

### 3.1 `Stop` 在用户回合收尾发射，且结果被丢弃 —— 已复核成立

- 发射点：`genericagent/core/app.py:1263`（`Stop`）、`:1298`（`StopFailure`），位于 `GenericAgent.run()` 的 `while True` 任务循环内，**每个 task 跑完发一次**。
- 丢弃证据：该调用 `run_hook_event(...)` **未接收返回值**，没有 `if agg.blocked` 之类的消费；`Stop`/`StopFailure` 不在 `GUARD_EVENTS`（`hooks/models.py:51`）。
- 对照 CC：Stop 挂在 `blockable_turn_end` / `loop_tick` / `fork_dispatch`（`m0671:3351-3474`），可 `blockingError` + `preventContinuation` 让回合继续。

### 3.2 “模型这轮想停”的真空点 —— 已复核成立

- `genericagent/core/loop.py:201-286` `agent_runner_loop`：模型返回 `no_tool` → `next_prompts` 为空 → `loop.py:277` `break`。
- 该处**没有任何 hook 调用**；`_run_post_tool_batch_hook`（`loop.py:340`）只发 `PostToolBatch`，不是 Stop 语义。

### 3.3 `_done_hooks` 是死机制 —— 更正我此前的说法

- 仅三处引用：`handler.py:377`（初始化为 `[]`）、`loop.py:277`（读长度）、`loop.py:278`（`pop(0)`）。
- 全仓库 **无任何 append/写入**，因此永远为空，`loop.py:277` 那个分支从不生效。
- 结论：**它不是可复用的“空轮续跑挂点”**。goal 若需要“空轮注入下一轮输入”，应在 `loop.py` 显式新增“门禁返回 next_prompt”的路径，而不是依赖 `_done_hooks`。

### 3.4 `STOP_LIKE_EVENTS` 与 `prevent_continuation` 是“半接线” —— 已复核成立

- `hooks/models.py:56` 定义 `STOP_LIKE_EVENTS`，`runtime.py:21` 仅 import，**无任何读取/判断**。
- `HookAggregate.prevent_continuation`（`models.py:238`）会被 `models.py:312-313` 从 `preventContinuation` 解析赋值，`blocked` 属性（`models.py:272`）也会带上它；但**core 从不读取**（全仓库只有 models 内部引用）。
- 结论：语义解析已就绪，只差“core 消费”。这正是本次要补的最小闭环。

### 3.5 守卫 fail-closed 是“opt-in”而非默认 —— 已复核成立

- 只有显式 `onFailure=block` 且事件属于 `GUARD_EVENTS` 时才升级为 blocking（`runtime.py:381-391`），runner 侧对 `onFailure` 也有忽略集（`runner.py:21`）。
- 配置解析失败（`config.py:104-167`）只 `registry.add_issue(...)`，**从不变阻塞**；`issues` 仅用于 `status()` 展示（`runtime.py:286`）。
- 对照 CC：PreToolUse/PermissionRequest 配置/运行错误**默认 fail-closed**。

### 3.6 prompt hook 结构化输出缺失 —— 已复核成立

- `_parse_stdout(..., allow_text=True)`（`runner.py:93-120`）把非 JSON 一律当 `additionalContext` 吞掉；`_prompt_execute` 走的就是这条（`runner.py:231`）。
- 后果与 `f1a69959` 实证一致：求值器输出不合法 JSON 时不会报错，而会静默退化成“附带上下文”。
- 对照 CC：求值器走严格 `JSON.parse` + schema 校验（`m0459`），失败即 `hook_non_blocking_error`。

### 3.7 事件发射 17/33 —— 已复核成立

- 实测发射：`SessionStart`、`UserPromptSubmit`、`PreCompact`、`PostCompact`、`Stop`、`StopFailure`、`PreToolUse`、`PostToolUse`、`PostToolUseFailure`、`PostToolBatch`、`PermissionRequest`、`PermissionDenied`、`TaskCreated`、`TaskCompleted`、`SubagentStart`、`SubagentStop`、`TeammateIdle`。
- 其余 16 个：`SessionEnd`、`UserPromptExpansion`、`PreModelSwitch`、`PostModelSwitch`、`Notification`、`Setup`、`Elicitation`、`ElicitationResult`、`ConfigChange`、`WorktreeCreate`、`WorktreeRemove`、`InstructionsLoaded`、`CwdChanged`、`FileChanged`、`DirectoryAdded`、`MessageDisplay`。
- `event_status()`（`runtime.py:294+`）自报状态需要与本表一致，避免“自称 integrated 实则未发”。

---

## 4. 目标架构（三层，禁止倒序）

```
第 3 层  mods（可分发插件）
         ├─ manifest / 来源 / 启用状态 / managed 优先级
         └─ 通过第 2 层 API 动态注册 hooks（含 hooks module）
                    ▲
第 2 层  有语义的 hooks 运行时（本文要打的地基）
         ├─ Stop 门禁：回合内可阻塞 / 可阻止续跑
         ├─ 会话级动态注册表：add / remove（session scoped）
         ├─ 守卫 fail-closed（PreToolUse / PermissionRequest / UserPromptSubmit）
         └─ 结构化输出通道（prompt / agent 支持 json_schema）
                    ▲
第 1 层  goal（第一层楼，消费第 2 层）
         └─ goal = 一条会话级动态 Stop hook（条件 + 求值器）
```

对齐 CC 的映射（供 review）：
- 会话级动态 hook ≈ CC `sessionHooksRegistry.add(sessionId, "Stop", handler)`（`m0274`）；
- Stop 门禁 ≈ CC `blockable_turn_end` + `blockingError` / `preventContinuation`（`m0671:3351-3474`）；
- 求值器结构化 ≈ CC `outputFormat.json_schema`（`m0459`）；
- mods ≈ CC plugin 提供 `hooks.json` + hooks module（见 `20261020-mods入口分析.md` §0/§1）。

---

## 5. 详细设计

### 5.1 Stop 门禁（P0，goal 的唯一硬依赖）

**触发点**：`agent_runner_loop` 内 `loop.py:277` 那次 `break` 之前，改为“先门禁、再决定是否 break”。

**语义（严格对齐 CC 三类结果）**：

| aggregate 结果 | GA 行为 |
| --- | --- |
| 无 blocked / 无 prevent_continuation | 正常 `break`（保持现状） |
| `blocked`（`decision:block` 或 `continue:false`） | 不 break：把 `reason` 作为下一轮输入注入（走新增显式路径），清 `exit_reason`，`continue` |
| `prevent_continuation`（`preventContinuation:true`） | `break`，但把 `reason` 作为 system message 展示（“Stop hook prevented continuation”） |
| hook 运行错误 / 超时 | 放行 break（不让坏 hook 锁死会话）；记审计 |

**防死循环（必须同批落地）**：
- payload 带 `stop_hook_active`：本轮已因 Stop 门禁续跑过，则下一次 Stop 时置 `true`，允许 hook 区分；
- 每个用户回合内 Stop 门禁最多触发 N 次（建议默认 8，可配置）；超限放行并提示；
- 与 `max_turns` 联动：门禁续跑也消耗 turn，天然有上界。

**必须同时修的三个“半接线”**：
1. `STOP_LIKE_EVENTS` 参与判断（或删除该常量，避免误导）；
2. `prevent_continuation` 在 core 被消费；
3. `run()` 收尾那次 `Stop` 要么并入门禁语义（仅在最后一次触发），要么明确改名为“用户回合结束”的独立事件，**避免同一语义两处触发**。

**兼容性**：`Stop` 从“每 task 一次”变为“每次模型想停一次”，审计日志量会上升；需在 audit 里区分 `phase: turn_end | user_turn_end`。

### 5.2 会话级动态 hook 注册表（P0，goal 与 mods 共用）

现状：`HookRegistry`（`config.py:23`）只有静态 `entries`，来自文件扫描；`for_event()` 只读静态列表。

设计（最小改动、不破坏静态加载）：

```
class HookRuntime:
    def add_session_hook(self, event, handler_spec, *, key=None) -> str
    def remove_session_hook(self, key) -> bool
    def list_session_hooks(self, event=None) -> list[...]
```

- 存放在 `HookRuntime` 实例（每个 agent 一个，见 `hook_runtime_for_agent`）上的独立列表；
- `run_event()` 合并顺序：**静态 entries 在前，会话动态在后**（CC 语义），并按 event 过滤；
- `key` 用于幂等（goal 用固定 key `goal:stop`，重复设置即替换）；
- 生命周期：agent 销毁/会话结束自动清空（对应 `SessionEnd`）；
- 禁止动态 hook 注册“守卫类”事件的 deny 能力绕过静态策略（安全边界见 §7）。

### 5.3 守卫 fail-closed（P1）

- `PreToolUse` / `PermissionRequest` / `UserPromptSubmit` 三个守卫事件：**配置错误、运行错误、超时、输出非法** → 默认 block（可被显式 `onFailure:continue` 降级）；
- 其它事件保持 non-blocking（一个日志 hook 不能拖死 agent）；
- 配置解析失败（`config.py add_issue`）要在守卫事件上体现为 fail-closed，而不是只进 `status()`。

### 5.4 结构化输出通道（P1，求值器必需）

- `HookHandler` 增加可选 `output_schema`（JSON Schema 子集：`type/required/properties/enum`）；
- `prompt` / `agent` 执行器在 `output_schema` 存在时走**严格解析**：非 JSON / 不合 schema → `status=invalid_output`，不回退 `additionalContext`；
- 求值器用 `{ok:boolean, reason:string, impossible?:boolean}`；
- 失败语义对齐 `f1a69959`：记 `evaluator_error`，按“放行或谨慎续跑”处理，绝不静默当成功。

### 5.5 为 goal 预留的接口（第 1 层怎么接，不在本文实现）

goal 落地时应当**只做三件事**，其余全部复用第 2 层：
1. 用户 `/goal <条件>` → `add_session_hook("Stop", {type:"prompt", prompt:<条件>, model:<求值模型>, output_schema:<三态>}, key="goal:stop")`；
2. Stop 门禁返回 blocked 时，goal 侧把 `iterations+1 / last_reason` 记到 `ActiveGoal`，并写 `goal_status` 转录事件；
3. met / impossible → `remove_session_hook("goal:stop")` + 写 `goal_status`。

**这意味着 goal 方案 `docs/20261028-GA-goal模式设计与实施方案.md` 需要改两处**：
- §6.1 不要直接复用 `_default_prompt_runner` 的宽解析；改为“复用其运行器 + 5.4 的严格 schema 通道”；
- §4.1 的 `handler.goal_guard` 旁路，若 5.1 落地，可简化为“在 Stop 门禁里注册 goal hook”，无需在 loop 里再插一个专属状态机（保留兜底亦可，但不应双写）。

### 5.6 为 mods 预留的接口（第 3 层怎么接，不在本文实现）

mods 只需消费 5.2：
- plugin manifest 声明 `hooks.json`；
- 插件加载时把其 hooks 以 `add_session_hook`（带 `source=plugin:<name>`）注册，卸载时 `remove`；
- 优先级：managed > user > plugin（对齐 `20261020-mods入口分析.md` §5）；
- hooks module（进程内 JS 扩展）是**独立执行面**，不在本文范围，但必须复用同一事件契约与输出 schema。

> 结论重申（引 mods 文档）：**不能“一次做完 mods 就白拿 hooks”，也不能只做 hooks 就说完成 mods。** 本文只做 hooks 语义，不碰 mods 装载层。

---

## 6. 分阶段落地计划（每步独立可测、可回退）

### 阶段 A（P0，必须先做）：Stop 门禁最小闭环
- 范围：`loop.py`（门禁调用）、`app.py`（收尾 Stop 语义归位）、`models.py`/`runtime.py`（`STOP_LIKE_EVENTS`/`prevent_continuation` 接线）
- 验收：
  - 单测：构造一个返回 `{"decision":"block","reason":"x"}` 的 Stop hook，验证 `agent_runner_loop` **没有结束**且下一轮输入含 `x`；
  - 单测：`{"preventContinuation":true}` 让循环结束且带 system message；
  - 单测：hook 抛错/超时 → 正常结束，不卡死；
  - 回归：现有 `tests/test_hooks_lifecycle.py`（断言 Stop 每 task 一次）需同步更新，避免与新语义冲突。

#### 阶段 A 实现结果（已落地，2026-10-29）

改动的文件与职责：

| 文件 | 改动 |
| --- | --- |
| `genericagent/core/handler.py` | 新增 `_stop_gate_active()` / `run_stop_gate()` / 每回合 `_stop_gate_count` / `stop_emitted`；Stop 走统一入口 `run_hook_event` |
| `genericagent/core/loop.py` | `agent_runner_loop` 在“模型想停”处调用门禁；先处理排队续跑、再做真停判断；`EXITED` 硬停不门禁 |
| `genericagent/core/app.py` | 收尾 Stop 降级为兜底：本回合门禁已发过则不再发，避免同义双发；兜底 payload 带 `phase: user_turn_end` |
| `genericagent/hooks/runner.py` | `ON_FAILURE_IGNORED_EVENTS` 由 `STOP_LIKE_EVENTS` 派生，消除重复常量 |
| `tests/test_hooks_stop_gate.py` | 新增 13 个用例（门禁三态、防死循环、错误放行、beep 一次/两次语义、无 hook 不变） |

**门禁触发点与顺序**（`loop.py`）：

1. `exit_reason.result == 'EXITED'` → 硬停，直接 `break`，不发门禁（app 收尾仍会兜底发 Stop）；
2. `handler._done_hooks` 非空 → 还有排队续跑，说明这一轮不是真停，**不触发门禁**；
3. 否则才是“模型真的想停”，调用 `handler.run_stop_gate(exit_reason)`。

**三态返回**：

- `"continue"`：`blocked`（`decision:block`），把 `reason` 注入下一轮输入并清空 `exit_reason`，继续循环；
- `"stop"`：`prevent_continuation`，停止并展示 `[Stop] reason`（`prevent_continuation` 优先于一般 blocked）；
- `"none"`：正常结束。

**Stop-beep 语义（本次修正的重点）**：Stop 在“agent 想停的那一刻”发，beep 就在此时响；若 hook 阻塞导致下一轮又停，会**再次**发 Stop 且 `stop_hook_active=True`（CC 语义），所以 beep 是“每次停一次”，不是“每用户回合一次”。app 收尾的 Stop 只在“本回合门禁**没发过**”时兜底补发（无 hook runtime、`EXITED`、异常、用户中途 abort），保证不漏 beep、也不双响。

**防死循环**：每回合门禁最多 8 次（`max_checks`）；`stop_hook_active` 随门禁次数递增；`max_turns` 仍是最外层上界。hook 报错/超时由 `run_hook_event` 吞错并返回良性 aggregate，门禁返回 `"none"` 放行，坏 hook 不锁会话。

**payload 契约**：门禁期 `phase: turn_end`、`source: turn_end`；app 兜底 `phase: user_turn_end`。`exit_reason` 统一为 `aborted`（`stop_sig`）或 `completed`。子 agent 走 `SubagentStop`，主 Stop 门禁对 `is_subagent` 关闭（`_stop_gate_active()` 为假）。

**验证命令与结果**：

```bash
python -m pytest tests/test_hooks_lifecycle.py tests/test_hooks_runtime.py \
  tests/test_hooks_tool_runtime.py tests/test_hooks_stop_gate.py -q
# 54 passed, 4 subtests passed
python -m pytest tests/test_agent_loop_parallel_tools.py tests/test_ga_subagent_tools.py \
  tests/test_ink_bridge.py tests/test_workflow_child_agent.py -q
# 283 passed, 21 subtests passed
python -m pytest tests/ -q
# 1915 passed, 4 skipped, 274 subtests passed
```

> 与阶段 A 原验收的一处差异：原计划“现有 `test_hooks_lifecycle.py` 断言 Stop 每 task 一次需同步更新”。实测无需改测试——因为正常直答路径下门禁正好发一次、app 兜底因 `stop_emitted` 不再发，行为仍是一次；只有真正发生 block 续跑或循环未走到门禁时才会多一次/由兜底补一次。

### 阶段 B（P0）：会话级动态 hook 注册表
- 范围：`runtime.py`（`add/remove/list`、`run_event` 合并）、`HookRuntime` 生命周期
- 验收：
  - `add_session_hook("Stop", ...)` 后 `run_event("Stop")` 能命中；`remove` 后不再命中；
  - 幂等 key：重复 add 只保留一条；
  - 不影响静态文件加载（现有 hooks 配置测试全绿）。

#### 阶段 B 实现结果（已落地，2026-10-29）

| 文件 | 改动 |
| --- | --- |
| `genericagent/hooks/config.py` | 新增 `parse_dynamic_handler(raw)`：复用文件 hook 的同一套校验生成 `HookHandler`，非法 spec 抛 `ValueError` |
| `genericagent/hooks/runtime.py` | `HookRuntime` 新增 `_session_hooks`；`add_session_hook/remove_session_hook/list_session_hooks/clear_session_hooks`；`run_event` 拆出 `_run_entries`，**静态在前、动态在后**，守卫事件上动态 hook 不能给 allow |
| `genericagent/hooks/__init__.py` | 导出 `parse_dynamic_handler` |
| `tests/test_hooks_session_registry.py` | 新增 10 个用例（增删查、幂等替换、事件过滤、清空、非法事件/spec、静态优先顺序、动态 allow 降级） |

**API 契约**（`HookRuntime`，每个 agent 一份）：

- `add_session_hook(event, handler_spec, *, key=None, matcher=None, emit=None) -> str`
  - `handler_spec` 可以是 `HookHandler` 或配置字典（与 `hooks.json` 同形），字典走 `parse_dynamic_handler` 校验；
  - `key` 幂等：同 key 再次 add 是**原位替换**（goal 用固定 key `goal:stop`，重复设置不会叠加）；
  - 返回 key（未传则生成 `session:<uuid>`）。
- `remove_session_hook(key) -> bool`、`list_session_hooks(event=None) -> list[dict]`、`clear_session_hooks() -> int`。
- `status_snapshot()` 新增 `sessionHooks` 字段，便于 `/status` 类 UI 展示动态 hook。

**合并顺序**：`run_event` 先跑静态 `registry.for_event(event)`，静态**已 blocked** 且是守卫事件则短路；否则再跑同事件的会话动态 hook。聚合仍走同一 `HookAggregate.add`，所以 `additional_context` 顺序天然是“静态在前、动态在后”。

**安全边界（硬约束，非仅文档）**：守卫事件（`UserPromptSubmit` / `PreToolUse` / `PermissionRequest`）上，动态 hook 产出的 `allow` 会被 `_neutralize_dynamic_allow` 就地降级为 `ask`，并在 `last_errors` 记一条 `downgraded`。两处 allow 都覆盖：`PreToolUse` 的 `permissionDecision` 与 `PermissionRequest` 的嵌套 `decision.behavior`。语义是“动态 hook 只能收紧，不能提权”。

**生命周期**：动态 hook 存在各自 runtime 上，随 agent 销毁回收；另提供 `clear_session_hooks()` 供显式 teardown。`SessionEnd` 事件 GA 目前全仓库没有发射点（历史现状），本阶段**未新增**该发射以免改变既有行为——goal 侧清理直接调 `clear_session_hooks()` 即可。

**测试命令与结果**：

```bash
python -m pytest tests/test_hooks_session_registry.py -q
# 10 passed
python -m pytest tests/test_hooks_lifecycle.py tests/test_hooks_runtime.py \
  tests/test_hooks_tool_runtime.py tests/test_hooks_stop_gate.py \
  tests/test_hooks_session_registry.py -q
# 64 passed, 4 subtests passed
python -m pytest tests/ -q
# 1925 passed, 4 skipped, 274 subtests passed
```

### 阶段 C（P1）：守卫 fail-closed
- 范围：`config.py` / `runtime.py` / `runner.py`
- 验收：守卫事件配置错误/执行错误 → blocked；非守卫事件仍 non-blocking；已有 `onFailure:continue` 显式降级可用。

#### 阶段 C 实现结果（已落地，2026-10-29）

**先纠正了文档原稿的一处偏差**（按 CC 303 源码复核）：

- 文档 §5.3 把 `UserPromptSubmit` 和 `PreToolUse`/`PermissionRequest` 并列为 fail-closed。源码显示这是**两个不同集合**：
  - **配置加载失败** fail-closed 集合 = `T4 = {PreToolUse, PermissionRequest}`（`m0062:2815`；理由 `m0062:2893`：“坏守卫可能正是权限门”）。
  - **执行失败**（error/timeout/无效输出）fail-closed 集合 = `Ilt()`（`m0459:158274`，`n6` 去掉 `PostToolBatch/TaskCreated/PreCompact`，含 `UserPromptSubmit` 等），由 `F1e`（`m0459:55482`）转 `blockingError`，`PermissionRequest` 同时返回 `behavior:deny`。
- GA 侧据此拆成两个常量：`GUARD_EVENTS`（执行失败，含 UserPromptSubmit）与 `CONFIG_GUARD_EVENTS = {PreToolUse, PermissionRequest}`（配置失败）。
- 另：文档 §3.8 提到 GA 拒绝 `PermissionRequest + agent`。CC 的 handler schema 五类对所有事件通用（`m0062:2704` `she()` 并集），说明这是 GA 自身能力限制、而非 CC 非法配置。所以这条**不计入守卫配置 fail-closed**，否则一个 CC 合法配置会让所有 PermissionRequest 被全量 deny。

| 文件 | 改动 |
| --- | --- |
| `genericagent/hooks/models.py` | 新增 `CONFIG_GUARD_EVENTS`；`HookHandler.on_failure_explicit`（区分“未设置”与显式 `continue`，守卫默认靠它区分） |
| `genericagent/hooks/config.py` | `HookRegistry.guard_config_errors` 记录守卫事件的配置错误；`_parse_file` 按事件折叠（排除 PermissionRequest+agent 策略性拒绝） |
| `genericagent/hooks/runner.py` | 把原先散在 command/http 两处 + runtime 一处的 onFailure 提升，统一成 `_apply_failure_policy`（在 `execute_handler` 末尾调用，五类 handler 一致） |
| `genericagent/hooks/runtime.py` | `run_event` 开头对 `CONFIG_GUARD_EVENTS` 的配置错误注入 blocking 结果（PermissionRequest 为 deny） |
| `tests/test_hooks_guard_fail_closed.py` | 新增 12 个用例（守卫执行失败默认 block / 显式 continue 放行 / 无效输出 / 超时 / 非守卫默认放行 / 非守卫 opt-in block / Stop 类永不提升 / 配置错误 fail-closed / UserPromptSubmit 配置错误不 fail-closed / PermissionRequest+agent 不 fail-closed） |

**统一后的失败策略（`_apply_failure_policy`）**：

| 事件类别 | 默认 | 显式 `onFailure:continue` | 显式 `onFailure:block` |
| --- | --- | --- | --- |
| Stop 类（Stop/SubagentStop/StopFailure/TaskCompleted/TeammateIdle） | 放行 | 放行 | **仍放行**（坏 beep 不锁会话） |
| 守卫事件（GUARD_EVENTS） | **阻止**（fail-closed） | 放行 | 阻止 |
| 其它事件 | 放行 | 放行 | 阻止 |

**测试命令与结果**：

```bash
python -m pytest tests/test_hooks_guard_fail_closed.py -q
# 12 passed, 4 subtests passed
python -m pytest tests/test_hooks_runtime.py tests/test_hooks_lifecycle.py \
  tests/test_hooks_tool_runtime.py tests/test_hooks_stop_gate.py \
  tests/test_hooks_session_registry.py tests/test_hooks_guard_fail_closed.py -q
# 76 passed, 8 subtests passed
python -m pytest tests/ -q
# 1937 passed, 4 skipped, 278 subtests passed
```

### 阶段 D（P1）：结构化输出通道
- 范围：`models.py`（`output_schema`）、`runner.py`（严格解析）
- 验收：合法三态 JSON 通过；非 JSON/缺字段 → `invalid_output` 且**不**退化为 additionalContext。

#### 阶段 D 实现结果（已落地，2026-10-29）

**契约来源（CC 303 源码核对）**：求值器系统提示词要求三种形状（`m0459:154361-154400`），`outputFormat` 为 `json_schema`、`required:["ok","reason"]`、`additionalProperties:false`（`m0459:154400+`）。判定落地（`m0459:154445-154490`）：

- `ok:true` → 成功放行（reason 仅记录）；
- `ok:false` 且 `impossible:true` → **成功但不阻塞**（goal 侧据此清目标）；
- `ok:false` 且非 impossible → `blocking`（reason 作为阻断原因）。
- JSON 解析失败 → `stderr:"JSON validation failed"`；schema 失败 → `"Schema validation failed"`（`m0459:154441-154451`）。这正是 `f1a69959` 里 14 条 `hook_non_blocking_error` 的来源。

| 文件 | 改动 |
| --- | --- |
| `genericagent/hooks/models.py` | `HookHandler.output_schema`（读 `outputSchema`，兼容 `output_schema`）；`HookAggregate.add` 解释求值三态（**仅限 prompt/agent 且声明了 output_schema**，避免普通 command hook 的 `{"ok":...}` 被误当决策） |
| `genericagent/hooks/runner.py` | 新增 `_strict_json`（容忍 ```json 围栏，不做事后截取）、`_validate_against_schema`（type/required/properties/enum/items/additionalProperties 子集，零新依赖）、`_parse_structured`；`_prompt_execute` 在 `output_schema` 存在时走严格解析并**直接 return，绝不回退 additionalContext** |
| `tests/test_hooks_structured_output.py` | 新增 15 用例（合法三态 / 代码围栏 / 非 JSON 不退化为 context / 缺字段 / 多余字段 / 类型错 / 无 schema 时普通文本仍可用 / 三态决策解释 / command hook 的 ok 不误判 / schema 子集校验） |

**严格解析要点**：

- 只剥“整段包住”的 ``` 围栏（对齐 CC `jB`=`m0045:969` 的 `trim().replace`），**不做**“取第一个 `{...}`”这类宽松提取——这正是 CC 的行为，也是 `f1a69959` 揭示的问题点。GA 额外的“宽松提取 + 重试”留给 goal 层按 `f1a69959` §6 自行叠加，不在本层隐式放宽。
- 无效输出在守卫事件上经阶段 C 的 `_apply_failure_policy` 自动转为 blocking；在 Stop 事件上按 Stop 类忽略集放行（不添加新的失败语义），求值器错误由 goal 层记 `evaluator_error` 处理。

**测试命令与结果**：

```bash
python -m pytest tests/test_hooks_structured_output.py -q
# 15 passed
python -m pytest tests/test_hooks_runtime.py tests/test_hooks_lifecycle.py \
  tests/test_hooks_tool_runtime.py tests/test_hooks_stop_gate.py \
  tests/test_hooks_session_registry.py tests/test_hooks_guard_fail_closed.py \
  tests/test_hooks_structured_output.py -q
# 91 passed, 8 subtests passed
python -m pytest tests/ -q
# 1952 passed, 4 skipped, 278 subtests passed
```

### 阶段 E（P2）：补齐高价值事件发射
- 建议优先：`SessionEnd`、`Notification`、`MessageDisplay`、`UserPromptExpansion`、`PreModelSwitch`/`PostModelSwitch`（修正 `event_status` 自报失真）。
- 每个事件都要有“发射点 + 载荷字段 + 测试”三件套，禁止只加词表不加发射。

### 阶段 F：goal 落地（另立方案，消费 A+B+D）
### 阶段 G：mods 落地（另立方案，消费 B）

---

## 7. 风险与安全边界

1. **Stop 语义变更影响面最大**：从“观测一次”变“可续跑”，若 hook 写错会造成无限续跑。必须同时上线“每回合最多 N 次 + stop_hook_active + max_turns 兜底”。
2. **动态 hook 不得突破静态安全策略**：会话级 `add_session_hook` 不允许注册能够 `allow` 的 `PermissionRequest`/`PreToolUse` 结果（只能 deny/ask 或加约束），防止运行时提权；这条要在 B 阶段写成硬断言。
3. **fail-closed 只限守卫事件**：误把 `Notification`/`Stop` 也 fail-closed 会让“坏 beep 拖死会话”（CC 明确 Stop 类忽略 onFailure=block，`runner.py:21` 已对齐）。
4. **审计与 UI 噪声**：Stop 触发频率上升，audit 需分 phase，UI 需合并展示，避免每轮刷屏。
5. **向后兼容**：默认无 Stop hook 时行为必须与现状**逐字节一致**（这是回归基线）。
6. **不碰云端**：mods 的 cloud / device forwarding、cloud `setMode` 一律不实现（CC 文档明确 GA 边界）。

---

## 8. 验收与 DoD

- [ ] 默认配置下，GA 行为与现状完全一致（无 hook 时零行为变化）
- [x] Stop 门禁：block 续跑 / preventContinuation 停止 / 错误放行，三类单测齐全（阶段 A 已完成）
- [x] 会话级动态 hook：add/remove/list + 幂等 + 生命周期（阶段 B 已完成）
- [x] 守卫 fail-closed：守卫事件错误默认阻塞，可显式降级（阶段 C 已完成；配置失败限 PreToolUse/PermissionRequest）
- [x] 结构化输出：invalid_output 不再静默吞掉（阶段 D 已完成；prompt/agent + outputSchema 严格解析）
- [ ] `event_status()` 与实际发射点一致（无“自称 integrated 但从未发射”）
- [ ] 全部现有 hooks 测试 + 新增测试通过；`pytest tests/test_hooks_*.py`

---

## 9. 复核后的“说法校准”（review 时请特别关注）

> 说明：下面是**我在上一轮口头分析里出现的偏差**的更正，不是对 `4099a2c` 文档的指责——
> 经全文核对，`4099a2c`（`docs/20261028-GA-hooks机制与CC303差异分析.md`）本身的结论是**准确的**。

1. **我上一轮说“`_done_hooks` 是现成的续跑挂点”是错的。** 复核：`_done_hooks` 全仓库只有初始化（`handler.py:377`）与读取（`loop.py:277-278`），**从未被写入**，是死机制。`4099a2c` 全文并未提到 `_done_hooks`，该错误归属在我，不在该文档。
2. **`4099a2c` 说“GA 已有看结果的机制，唯独 Stop 链路没接上”是准确的**，可补一处精度：`prevent_continuation` 没有直接读取方，它只汇入 `HookAggregate.blocked`；而 `.blocked` 仅在 `PreToolUse` / `UserPromptSubmit` / `PermissionRequest` / `PreCompact` / `TeammateIdle` 被消费，**Stop 类事件上没有任何消费**。
3. **`docs/20261028-GA-goal模式设计与实施方案.md` §6.1** 若直接复用 `_default_prompt_runner`，会继承 `allow_text=True` 的宽解析，重演 `f1a69959` 的 JSON 静默失败；必须叠加 §5.4 的严格 schema 通道。
4. **同文 §4.1 的 `handler.goal_guard` 旁路**：若 §5.1 的 Stop 门禁落地，goal 应优先复用门禁 + 会话级动态 Stop hook（§5.2），避免同一语义两套实现；旁路可作为兜底保留，但不应双写。

## 10. 本文不做的事

- 阶段 E–G 仍是设计稿，不改源码；阶段 A–D 已落地（见 §6 各阶段“已实现”小节）；
- 不实现 goal / mods，只定义它们与 hooks 的接口边界；
- 不改 33 事件词表本身（词表已对齐 CC）；
- 不引入 cloud / device / 远程 rollout 相关能力。
