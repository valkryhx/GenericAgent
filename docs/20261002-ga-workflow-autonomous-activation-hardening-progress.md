# GA Workflow 自主激活与 Ink UI 可靠性改造进度（2026-10-02）

依据 `docs/superpowers/plans/20261001-ga-workflow-autonomous-activation-hardening.md`，直接在 `main` 分阶段实施。现有 `.premerge-doc-backup-20261001/` 与用户计划文档保持原样。

## 状态

- Phase A：已实现，专项测试通过。
- Phase B：已实现，runtime/child 与 capability preflight 专项测试通过。
- Phase C：已实现 guidance 与 terminal event；Ink UI 原有 progress snapshot/status bar 逻辑已由组件测试覆盖。
- Phase D：程序化 Ink UI suite/typecheck 与真实 DeepSeek + Tavily Ink UI E2E 均通过。

## 已完成实现

- `workflow_activation.py` 对每个 turn 独立判定；多动作 + 顺序信号才语义自动激活，plan-only/普通问答抑制；显式 `/workflow` 和 session on/off 可用。
- deterministic mixed planner 生成 Research → Artifact Writer → Verify 执行 packet，声明 Tavily、file I/O、action、write scope、deliverables、acceptance checks 和 `executionContract`。
- `validate_workflow_plan` fail-closed 校验 plan-only、action mapping、tool assignment/evidence、artifact writer/scope/deliverable/checks 与 DAG dependency；`taskType` 不单独触发这些门禁。
- `render_workflow_plan` 将 execution contract 能力元数据传入 agent options；prompt-guided planner 新增 execute-vs-plan、parallel/pipeline、bounded retry/budget、terminal status、host capability snapshot guidance。
- `WorkflowController` 持久化 execution contract；Native child runner 对每个 run 进行 required tool preflight、固定 capability schema snapshot 并在结束后清理缓存；required tool 缺失时 child 启动前失败并写结构化事件。
- workflow 缺省 workspace 创建于 run artifact 目录并持久化；resume 继承源 run workspace；cache key 区分原始 args 类型并加入规范化 workspace path。
- runtime 成功前检查 required tool transcript evidence、artifact workspace containment、artifact existence/read-back；成功写 `workflow_finished`，失败终态包含 integration/audit/blocking 信息。
- 用户指南与本文件已记录自动激活和排障契约；AGENTS.md 指向完整设计/验收参考。

## 验证记录

- `tests.test_workflow_activation`、planner/validator/verification 专项通过。
- `tests.test_workflow_runtime` + `tests.test_workflow_child_agent`：修正后 93 项通过；新增 workspace、evidence、artifact、terminal 回归用例。
- Ink UI：`npm test` 378/378 通过；`npm run typecheck` 通过。
- Python 全量首次运行：1134 项，4 个失败。已修复 cache key 原始 args 类型回归，并更新与新增终态事件/忙碌提示相符的旧断言；**修复后的全量 Python 套件需要重新运行确认**。
- 真实 E2E 脚本：`frontends/ink-ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts`；使用真实 `deepseek-v4.1-flash`、Tavily MCP 和 Ink UI 普通自然语言自动激活 workflow。最终通过 run：`wf_de81ea966e56400d895515bccbea8c73`，状态 `succeeded`，artifact 为 workspace 内的 `artifacts/pathlib_write_text.html`，UI 回到 idle。用户侧 LLM 记录的单次 DeepSeek 请求约 7 秒；真实 E2E 的额外耗时来自 planner/child 多阶段工具执行、MCP、写文件和 host 验证，不是 DeepSeek 请求卡顿。

## 本轮已收敛的真实故障与修复

1. DeepSeek 计划已经在 `executionContract` 声明 action/tool/artifact，但 agent packet 重复字段缺失：新增仅按显式 contract 归一化，不从 taskType 或自然语言猜能力。
2. `required_tool_evidence` 被错误地作为没有 command/adapter 的 host command/schema check：归一化为 execution contract 的 transcript 证据，不再触发无效 adapter。
3. artifact verification check 只有 id 没有 path：从唯一显式 artifact 绑定路径；多 artifact 歧义继续 fail-closed。
4. `artifact_readback` 原先只接受 writer 的 `file_read`：现在接受显式 file-read agent，或 verifier 对目标 artifact 的成功 `code_run` readback；其他 required tool 仍要求精确调用。
5. transcript agent label 匹配改为大小写和连续空白归一化，避免 `Artifact verifier` / `Artifact Verifier` 误判。
6. `artifact_structure`、`source_count` 等机器检查由 host adapter 执行，source_count 默认要求至少 2 个 URL；固定 machine-check id 被归一化为 artifact adapter，避免模型把它声明成无法提供证据的 schema。

## 完成门槛与最终验证

- `python -m unittest discover -s tests`：1144 项通过，3 项跳过。
- `cd frontends/ink-ui && npm test`：378/378 通过。
- `cd frontends/ink-ui && npm run typecheck`：通过。
- 真实 Ink UI E2E：deepseek-v4.1-flash + Tavily，普通自然语言自动激活；最终 `succeeded`，包含 `workflow_capability_snapshot`、实际 Tavily evidence、`workflow_finished`，artifact 位于 workspace，UI idle。
- 详细脱敏记录见 `docs/20261002-ga-workflow-autonomous-activation-hardening-reference.md`。

不记录真实 API key、Authorization、raw response 或包含 credential 的日志。
