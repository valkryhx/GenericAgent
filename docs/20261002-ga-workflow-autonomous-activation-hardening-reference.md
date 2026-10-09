# GA Workflow 自主激活与真实 DeepSeek 验收记录（2026-10-02）

## 范围

本记录对应 `docs/superpowers/plans/20261001-ga-workflow-autonomous-activation-hardening.md` 的最后一轮真实验收。所有真实模型测试使用本机已配置的 `deepseek-v4.1-flash`，不记录 API key、Authorization 或原始响应。

## 真实测试

测试入口：`frontends/ink-ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts`

测试输入：

> 使用 Tavily 搜索 Python 官方文档中 pathlib.Path.write_text 的行为，然后生成一份介绍该 API 的 HTML 文件并验证文件

测试方式：从 Ink UI 输入框直接提交普通自然语言，不显式输入 `/workflow`，验证自主激活、planner、child runtime、Tavily、artifact 和 UI 终态。

最终通过 run：

- profile：`deepseek-v4.1-flash`
- status：`succeeded`
- UI：`idle`
- artifact：workspace 内的 `artifacts/pathlib_write_text.html`
- artifact size：13781 bytes
- 事件包含：`workflow_planned`、`workflow_started`、`workflow_capability_snapshot`、`agent_started`、`tool_allowed`、`agent_completed`、`state_mutation_observed`、`workflow_finished`
- Tavily：execution contract evidence 已满足

LLM 侧观测到单次 DeepSeek 请求约 7 秒。真实 E2E 的总耗时还包括 planner、三个 child agent 的工具调用、MCP、写文件、验证和 host acceptance，因此这次问题不能归因为 DeepSeek 请求慢。

## 失败链路与根因

真实复跑先后暴露了 7 类 GA 缺陷：

1. execution contract 与 agent packet 重复声明不一致；
2. `required_tool_evidence` 被当成不可执行的 generic verification command/schema；
3. artifact verification check 没有从显式 artifact contract 继承 path；
4. readback 只绑定 writer，忽略显式 verifier；
5. agent label 匹配大小写敏感；
6. `source_count` 等 host 可执行检查没有 adapter；
7. 模型将 `source_count` 标成 schema，导致 GA 等待不存在的 schema evidence。

这些错误都发生在 GA planner normalization、runtime evidence evaluation 或 check adapter 层，而不是模型请求超时。每个缺陷都先增加了可复现回归测试，再做最小修复。

## 关键硬化规则

- execution contract 是 action/tool/artifact ownership 的唯一来源；只做显式映射，不从 taskType 推断能力。
- artifact verification check 在单 artifact 时绑定唯一 path；多 artifact 无法唯一绑定时保持 fail-closed。
- `artifact_exists`、`artifact_readback`、`artifact_structure`、`source_count` 由 host machine adapter 执行。
- `source_count` 默认要求至少 2 个 URL，并将计数与脱敏 evidence 一并保留。
- `required_tool_evidence` 对 Tavily/file_write 等工具仍要求 transcript 精确调用；仅在 artifact readback 场景允许 verifier 对目标文件的成功 `code_run` 读取作为受限等价证据。
- agent label 比较归一化大小写与连续空白，但不会忽略 label 身份。
- workflow terminal event 和 Ink UI terminal projection 必须在 succeeded/failed 两种路径都可观察。

## 验证结果

- Python：`python -m unittest discover -s tests` → 1144 passed，3 skipped。
- Ink UI：`npm test` → 378/378 passed。
- TypeScript：`npm run typecheck` → passed。
- 真实 DeepSeek + Tavily + Ink UI E2E → passed。

## 残余限制

本轮没有把所有任意自然语言 verification 变成通用解析器；未知 check id、无法唯一绑定路径或缺少 transcript evidence 的计划仍会 fail-closed。这是预期安全行为，后续应继续通过显式 schema/adapter 扩展，而不是放宽为“模型说做过就算”。
