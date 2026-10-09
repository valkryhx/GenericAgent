# Workflow 结果回交主 LLM 实施计划

**目标：** Ink workflow 完成后，将最终综合结果和有限的中间结构化结果回交主 LLM，由主 LLM 生成正常 assistant 回复；不默认注入完整子 agent transcript。

**架构：** GA 当前由 Ink bridge 在主 agent loop 之外执行 workflow，因此 `workflow_final` 只进入 UI 状态，没有成为主 LLM 上下文。终态时由宿主构建有字节上限的 handoff，携带原任务、最终结果、选定的中间结果及 `result.json` / `transcript.jsonl` 相对路径，再直接排入主 agent 队列，绕过普通 submit 的 workflow activation。transcript 持久化但按需读取。主 agent 的正常回答经现有 assistant 流显示在 Ink 聊天记录中。

**参考依据：** Step-Code 的 workflow tool 将 script 返回的值作为 tool result 交回主 LLM；`agent()` 返回子 agent 最终输出经解析/schema 校验的值，而不自动传完整 transcript。GA 本次 run 中 `agent_1/result.json` 约 16 KB、`transcript.jsonl` 约 816 KB；综合 agent 实际读取了结构化结果，不需要无差别注入完整工具轨迹。

## 改动范围

- `frontends/ink_bridge.py`：终态 handoff 构造、上下文预算、安全 artifact refs、主 agent 队列提交及 assistant 输出消费。
- `tests/test_ink_bridge.py`：handoff 内容/截断、成功失败终态、直接入队、防重复激活及状态时序测试。
- `frontends/ink-ui/src/App.tsx`、`state.ts`：仅当现有 `assistant_done` 路径不足以显示 handoff 回答时改动；避免 `workflow_final` 与 `assistant_done` 双显。
- `frontends/ink-ui/src/*.test.ts`：必要时补充 transcript 单次呈现的 reducer/App 回归测试。

## Task 1：测试并实现有界 handoff 构造

- [x] 先写测试：成功的 final payload 含 synthesis、结构化 intermediate result、result/transcript refs；断言 handoff 包含前述信息及原任务，但不含 transcript 正文。
- [x] 运行 `python -m unittest tests.test_ink_bridge.InkBridgeTest.test_workflow_handoff_includes_results_and_refs_without_transcripts -v`，确认由于实现缺失而 RED。
- [x] 实现构造器：安全地归一化 workspace 相对路径、只选择终态结果和中间结构化值、只提供 transcript 路径；应用 UTF-8 字节上限，超限时保留最终 synthesis/status/refs 并写明省略。
- [x] 重跑测试验证 GREEN；再用超大中间结果补充 RED/GREEN 截断测试。

## Task 2：终态结果回交主 LLM

- [x] 为成功和失败终态写 bridge 测试：完成 workflow 后，主 agent 接到 `source="workflow_handoff"` 的任务，最终回答发出唯一 `assistant_done`；失败终态包含错误/可用 refs；handoff 不调用 `bridge.submit()`，不重新激活 workflow。
- [x] 运行新增测试确认当前缺少 handoff 而 RED。
- [x] 统一完成、失败、取消、立即终态和 runtime exception 终态出口；通过 `agent.put_task()` 直接入队并启动现有 display consumer；由 consumer 在主 LLM 回复结束后发 idle 状态，避免 bridge 提前报告 idle。
- [x] 重跑测试验证 GREEN，覆盖 slash workflow 和自然语言 workflow 使用同一 handoff 出口、每个终态只入队一次、缺少 final artifact 时仍能说明失败。

## Task 3：Ink 可见性与去重

- [x] 检查现有 `assistant_done` 流程及测试，确认主 LLM 的 handoff 回复会直接显示在 transcript。
- [x] 若有缺口，先补 RED 测试，再最小修复 reducer/App；不从 `workflow_final` 再渲染第二份结果。
- [x] 运行聚焦 Ink Node test runner，验证完成结果单次出现且完成状态不遮挡回复。

## Task 4：验证与记录

- [x] `python -m unittest tests.test_ink_bridge`
- [x] `cd frontends/ink-ui && npm test`
- [x] `python -m unittest discover -s tests`
- [x] `git diff --check` 并审阅差异，确认没有纳入用户已有未跟踪文件。
- [x] 若本地 Ink/LLM 凭据可用，再做一次真实 UI smoke；不保存真实 key、header 或完整 transcript。

## 验收标准

1. workflow 任一终态均以有界上下文回交主 LLM，产生用户可见的正常 assistant 回复。
2. 最终综合结果和被下游消费的中间结构化结果进入 handoff；result/transcript refs 可按需读取。
3. 完整 transcript 不默认注入；截断不会丢失最终结论及 refs。
4. 不重跑 workflow、不重复显示、不错误提前标 idle；状态栏与详情面板保持原功能。
5. 聚焦 bridge 测试、Ink 测试和完整 Python unittest suite 通过。

## 实施记录（2026-10-08）

已直接在 `main` 工作区完成：

- `frontends/ink_bridge.py` 增加有界 workflow handoff：携带原始任务、最终结果、成功/缓存子任务的结构化结果，以及 workspace 内安全的 `result.json` / `transcript.jsonl` 相对引用；不注入 transcript 正文。
- handoff 统一限制为 64KB 以内，并优先保留最终结果、状态和路径引用；中间结果超限时只保留引用或裁剪。
- workflow 成功、失败、取消/中断等终态通过 `agent.put_task(..., source="workflow_handoff")` 进入主 agent 队列，绕过普通 `submit()`，不会再次触发 workflow activation。
- 只有主 agent 消费完成后才发 `assistant_done` 和 idle；无存活主 agent consumer 的测试 double 保留旧的终态 idle 兼容行为。
- 新增成功、失败、边界截断、transcript 不注入、主 agent 回复时序回归测试。

验证结果：

- `python -m unittest tests.test_ink_bridge -q`：101 tests，全部通过。
- `python -m unittest discover -s tests`：1226 tests，全部通过，3 skipped。

