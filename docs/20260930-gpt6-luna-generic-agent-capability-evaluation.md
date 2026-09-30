# GenericAgent 通用能力真实模型验收记录

- 日期：2026-09-30（Asia/Shanghai）
- 目标：使用本机 `llm.yaml` 中的真实 `gpt-6-luna` 与 `deepseek-v4.1-flash`，验证 GenericAgent 的 skills、TDD、subagent、multi-agent、动态 workflow 和 MCP 能力。
- 安全边界：API key 只从被 `.gitignore` 忽略的本地配置读取；本文不记录 key、Authorization header、原始敏感响应或 `mcp.json` 内容。所有代码写入均限制在临时 workspace。

## 结论

两个真实模型均完成了核心能力链路，GenericAgent 可以作为通用 agent 运行：

| 能力 | gpt-6-luna | deepseek-v4.1-flash |
|---|---:|---:|
| `using-superpowers` skill 加载 | 通过 | 通过 |
| `test-driven-development` skill 加载 | 通过 | 通过 |
| 中等难度 TDD（RED → GREEN → REFACTOR → GREEN） | 通过 | 通过 |
| 两个真实 subagent 并发启动和持久化结果 | 通过 | 通过 |
| 动态 workflow（并发 agents + synthesis） | 通过 | 通过 |
| 真实 Tavily MCP 搜索 | 通过 | 通过 |
| MCP 工具结果回传给 agent/synthesis | 通过 | 通过 |

## 测试过程与证据

### 1. `gpt-6-luna`：skills + 中等难度 TDD

通过 `NativeGPTChildAgentRunner(profile_name="gpt-6-luna")` 启动真实 workflow child。模型被要求：

1. 调用 `load_skill("using-superpowers")`；
2. 调用 `load_skill("test-driven-development")`；
3. 实现 `parse_env_lines(text)`，覆盖注释、空行、引号、重复键、非法行；
4. 先写 unittest 并观察 RED，再写实现得到 GREEN，最后重构并再次 GREEN。

实际结果：

- 两个 skill 均由模型真实调用并成功返回；
- RED：实现不存在时 `ModuleNotFoundError`，退出码 1，失败原因符合预期；
- GREEN：3 项测试通过；
- 重构后 GREEN：3 项测试再次通过；
- 只写入临时 workspace；
- workflow job 状态 `succeeded`。

模型输出了标记 `GA_GPT6_SUPERPOWERS_TDD_DONE`。

### 2. `gpt-6-luna`：动态 workflow + MCP

动态 workflow 包含三个 agent job：

- `skill-tdd`：加载 `test-driven-development`，实现 `slugify` 并完成 RED/GREEN；
- `mcp-search`：严格调用一次 `mcp__tavily__tavily_search`；
- `synthesis`：不再调用工具，综合前两个结果。

实际结果：

- workflow 状态 `succeeded`；
- 3 个 job 均 `succeeded`；
- MCP 搜索真实返回 OpenAI Responses API 文档摘要；
- synthesis 明确判定 skill 加载、TDD、MCP 均成功；
- 总耗时约 251 秒，说明真实模型/工具链路可用但不适合过短的 wait timeout。

模型输出了 `GA_GPT6_DYNAMIC_WORKFLOW_DONE`、`GA_GPT6_MCP_DONE` 和 `GA_GPT6_SYNTHESIS_DONE`。

### 3. `gpt-6-luna`：真实 multi-agent/subagent

父进程通过 `SubagentManager` 并发启动：

- `gpt6_subagent_a` → `GA_GPT6_SUBAGENT_A_DONE`；
- `gpt6_subagent_b` → `GA_GPT6_SUBAGENT_B_DONE`。

两者均满足：

- `turn_status=completed`；
- 结果 marker 写入 `output.txt`，对应 events 包含 `turn_completed`；
- `effective_ipc_mode=file`；
- state/artifact/output 均成功落盘，随后由父进程关闭。

### 4. `deepseek-v4.1-flash`：skills + 中等难度 TDD

使用同一 workflow 结构切换到 `deepseek-v4.1-flash`。模型实现了更完整的 `.env` 风格解析器：

- 12 个 unittest 用例；
- RED：缺少实现时 `ModuleNotFoundError`，退出码 1；
- GREEN：12/12 通过；
- REFACTOR：提取值清理和行内注释辅助逻辑；
- 重构后 GREEN：12/12 再次通过；
- 两个 skill 均成功加载，标记 `GA_DEEPSEEK_SUPERPOWERS_TDD_DONE`；
- workflow job 状态 `succeeded`，耗时约 67 秒。

### 5. `deepseek-v4.1-flash`：动态 workflow + MCP

同样运行三个并发/串联 job：skill-TDD、MCP search、synthesis。

实际结果：

- 3 个 job 均 `succeeded`；
- TDD agent 真实调用 `load_skill("test-driven-development")`，先 RED 再 GREEN；
- MCP agent 真实调用一次 `mcp__tavily__tavily_search`，返回 2 条结果；
- synthesis 收到前序结果并给出三项能力均成功的判断；
- workflow 状态 `succeeded`，耗时约 174 秒。

模型输出 `GA_DEEPSEEK_DYNAMIC_WORKFLOW_DONE`、`GA_DEEPSEEK_MCP_DONE` 和 `GA_DEEPSEEK_SYNTHESIS_DONE`。

### 6. `deepseek-v4.1-flash`：真实 multi-agent/subagent

父进程并发启动：

- `deepseek_subagent_a` → `GA_DEEPSEEK_SUBAGENT_A_DONE`；
- `deepseek_subagent_b` → `GA_DEEPSEEK_SUBAGENT_B_DONE`。

两者均 `turn_status=completed`、`process_status=waiting_reply`，使用 file IPC，结果落盘后正常关闭。模型输出中的中文在 Windows 子进程日志中出现编码显示异常，但标记和状态均正确，未影响协议或结果判定。

### 7. DeepSeek profile 切换验证

加载本地 YAML profiles 后，真实 GenericAgent 执行 `select_llm("deepseek-v4.1-flash")`。首次调用复现 `ambiguous`；修复后返回成功并选择 `deepseek-v4.1-flash/deepseek-v4.1-flash`（profile index 12）。随后以上各项 DeepSeek 真实测试均通过固定 profile `deepseek-v4.1-flash` 执行。

## 失败尝试与修正

### MCP discovery 短超时

最初的复杂 workflow harness 使用 20 秒 MCP discovery timeout。由于同一配置中的其他 SSE 服务超时，第一次检查误判 Tavily 不可用。独立 discovery 在更长窗口下发现了 15 个以上工具（包括 `mcp__tavily__tavily_search`）。后续测试设置了更宽的 discovery timeout，并成功完成真实 MCP 调用。

### 复杂 planner workflow 超时

复杂 planner + 多 agent + synthesis harness 曾在 planner 成功后达到 runtime deadline。随后将测试最小化为固定动态 workflow，减少 planner 修复回合和每个 agent 的最大 turn 数；两个模型均成功。这表明问题是测试负载/超时预算，而不是模型或 GenericAgent 基础能力失败。

### 精确模型选择歧义

本轮首先发现 gpt-6 profile 变体选择问题；随后在真实配置中发现更具体的 DeepSeek 情况：`deepseek-v4.1-flash` 与 `deepseek-v4.1-flash-chat` 共用同一个 API model 字符串。仅优先匹配 model 仍会匹配两个 profile，因此 `select_llm("deepseek-v4.1-flash")` 仍返回 `ambiguous`。

已按 TDD 增加两个回归场景（model 字符串不同的变体、profile 不同但 API model 相同的变体），先确认第二个测试失败，再修复为：精确 profile/backend display name → 唯一精确 API model → 子串匹配。真实 GenericAgent 重试现在选择了正确 profile，所有 6 项 model-selection 测试通过。

## 本地回归验证

已通过的聚焦测试：

- skills/load_skill：13 项，全部 `OK`；
- model selection：修复后 6 项，全部 `OK`；
- subagent manager/event bus/tools/worktree：125 项，全部 `OK`；
- workflow runtime/child/integration/scheduler：103 项，全部 `OK`；
- MCP runtime + skills：35 项，全部 `OK`。

一次完整 `unittest discover` 曾被交互式中止，不能将其记为全套通过；本报告只记录已实际完成的聚焦测试和真实 E2E 结果。

## 后续建议

- 对真实 MCP discovery 使用可配置、分服务的超时，避免一个慢 SSE 服务阻塞其他可用服务；
- workflow 的默认真实 E2E timeout 应根据模型响应和工具调用延迟设置，不能用 10 秒级启动阈值衡量完整任务；
- subagent 结果读取应继续使用持久化 artifact/state，不依赖 agent 是否仍在活跃列表；
- Windows 日志统一 UTF-8，避免中文摘要乱码，但不影响 marker、state 和 artifact 协议测试；
- 后续新增验收/故障文档继续遵循 `YYYYMMDD-xxxx.md` 命名约定。


## DeepSeek 完整 planner → workflow 闭环（2026-09-30）

在前面的固定 workflow 验收后，又用真实 `deepseek-v4.1-flash` 执行了完整的 prompt-guided planner 闭环。planner、所有 child agent 和 runtime 均显式绑定到 `deepseek-v4.1-flash`，没有继承 `active_profile`。

### Planner 阶段

任务是实现 `parse_env_text(text)` 与 `redact_sensitive(text)`，要求：

- 解析空行、注释、`KEY=VALUE`、单双引号、值内等号、行尾注释、重复键覆盖；
- 非法行抛出带行号的 `ValueError`；
- 脱敏 API key、Bearer token、邮箱；
- 严格执行 RED → GREEN → REFACTOR → GREEN；
- 真实调用 Tavily MCP 查询 Python 官方 `re` / `shlex` 文档；
- 加载 `using-superpowers` 与 `test-driven-development`；
- 只写临时 workspace，不读密钥文件、不读 `mcp.json`、不提交 Git。

第一次 planner 输出依赖项使用了自然语言 phase 标题，validator 正确拒绝；第二次改成 kebab-case label，但仍把有依赖的 agent 放进同一 phase，validator 再次拒绝；第三次加入“每个 phase 一个 agent”和“schema key 必须是 JavaScript identifier”约束后，planner 输出通过：

```text
plannerMode = prompt_guided
validation.ok = true
6 phases / 6 agents
```

最终计划为：

```text
understand
→ research-python
→ write-tests
→ implement
→ verify
→ synthesis
```

### Runtime 阶段

真实 runtime 执行结果：

- workflow status：`succeeded`；
- runtime status：`succeeded`；
- 6 个 job 全部 `succeeded`；
- 总耗时约 304 秒；
- runtime metadata 明确记录：`llmProfile=deepseek-v4.1-flash`、`llmModel=deepseek-v4.1-flash`；
- MCP discovery 发现 16 个工具，其中包含 `mcp__tavily__tavily_search`。

能力证据：

- `research-python` 真实调用 Tavily 多次，并抽取 `docs.python.org` 官方 `re` / `shlex` 页面；
- `write-tests` 真实加载 `using-superpowers` 和 `test-driven-development`，先写测试后观察 RED；
- `implement` 再次加载两个 skill，先达到 34/34 GREEN，然后尝试重构；
- `verify` 读取真实测试结果，发现重构后出现 8 个错误；
- `synthesis` 读取上游证据并保留失败事实，没有把 workflow job 的 `succeeded` 错误地当成业务验收通过。

### 业务验收结论

这次不能记为全功能通过。最终失败根因是实现 agent 重构时把 `_APIKEY_RE` 定义改名/移除，但 `redact_sensitive()` 仍引用旧名称，导致 8 个 `NameError`。这证明：

1. planner 能生成可执行的多阶段计划；
2. validator 能捕获结构错误；
3. runtime 能按依赖顺序调度 6 个真实 agent；
4. skills 与 MCP 能在 planner 生成的 workflow 中真实工作；
5. verification/synthesis 能捕获并报告下游实现缺陷；
6. 当前 workflow job 的 `succeeded` 表示 agent 回合完成，不等于任务业务验收通过，最终状态必须读取测试证据或 synthesis verdict。

这次失败属于被真实 workflow 捕获的下游代码缺陷，不是 planner、subagent、MCP 或模型连接失败。
