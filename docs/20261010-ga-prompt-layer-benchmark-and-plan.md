# GA 提示词分层对标与优化方案（pi / Codex / Step-Code）

日期：2026-10-10　范围：base prompt、subagent / multi-agent、workflow planner、workflow child

## 0. 结论摘要

GA 的提示词目前是**「单层厚、场景薄」**：`assets/sys_prompt.txt` 43 行把身份、边界、工作方式、
验证、工具、沟通都覆盖了，属于「够用但浅」；而 subagent / multi-agent / workflow 三个场景的指导
几乎全部落在工具 description 和 planner prompt 的**合同条目**上，缺少**编排策略**本身——什么时候
该 fan out、用哪种编排模式、把 agent 花在哪、规模多大、什么东西绝不能静默丢弃。

对标结论（按价值排序）：

| # | 缺口 | 参考实现 | GA 现状 | 后果 |
|---|---|---|---|---|
| 1 | base prompt 缺「工作方式层」 | Codex `gpt_5_2_prompt.md`（22KB：Personality / Autonomy and Persistence / Planning + 好计划 vs 坏计划示例 / Task execution / Validating your work / Ambition vs. precision / Presenting your work） | 43 行，无计划纪律、无 review 心智、无前端质量线 | 复杂任务中途失焦；用户说「审查」时给的是摘要而非按严重度排序的发现；HTML/UI 产物是「AI 味」默认样式 |
| 2 | planner prompt 只有合同机械，没有编排模式 | Step-Code `step-workflow.ts` 的 workflow 工具 description：opt-in 语义 + 单相模式（understand/design/review/research/migrate）+ 质量模式（adversarial verify / judge panel / loop-until-dry / multi-modal sweep / completeness critic）+ sizing + no silent caps | `_planner_prompt` 的 `orchestrationPolicy` 30+ 条里，绝大多数是 schema / artifact / capability / acceptance id 合同 | DAG 形状保守（两三个 agent 串行），不会用对抗验证、评审团、穷尽式搜索来提升结论质量 |
| 3 | workflow child 角色指令过薄 | Step-Code `agent-runner.ts` + pi `agents/*.md`（scout/planner/reviewer/worker 各有明确输出格式与工具白名单） | `role_instructions` 只有 4 条一句话 | 子代理输出格式漂移，下游/人类读者要重新猜结构 |
| 4 | 子代理提示词缺「共享环境」与收尾卫生 | Codex `templates/collab/experimental_prompt.md` 明确要求 spawn 时告诉子代理「你不是一个人」；并要求用 `close_agent` 收尾、慎选 `timeout_ms` | `subagent_prompts.py` 没有共享工作区警告，也没有收尾/槽位说明 | 并发子代理互相覆盖写入；子代理跑完不释放并发槽 |
| 5 | 提示词内容三处硬编码，无单一事实来源 | Step-Code `formatBuiltinAgentGuidance()` 从 `BUILTIN_AGENTS` 派生（工具描述不可能漂移）；pi 的 `toolSnippets`/`toolGuidelines` 由各工具模块自己贡献；Codex `ResolvedMessage` 用「目录 + 默认值」解析 | 同一条规则同时写在 `assets/sys_prompt.txt`、`assets/tools_schema.json`、`GA_AGENTS.md` | 改一处漏一处，文档与真实行为漂移 |

## 1. 调研对象（实际读过的源码）

### 1.1 Codex（`D:/git_codes/codex`）

- `codex-rs/core/gpt_5_2_prompt.md`（22KB）：`# How you work` → Personality / AGENTS.md spec /
  Autonomy and Persistence / Responsiveness / **Planning**（含高质量与低质量计划对照示例）/
  Task execution（编码纪律 15 条）/ Validating your work / **Ambition vs. precision** /
  Presenting your work + Final answer structure；`# Tool Guidelines`。
- `codex-rs/core/gpt-5.2-codex_prompt.md`（80 行，Codex CLI 附加层）：General（`rg` 优先）/
  Editing constraints（脏工作区纪律、不得回滚他人改动、不 amend、不 `git reset --hard`）/
  Plan tool（何时用、不写单步计划）/ Special user requests（**review 心智**）/ Frontend tasks
  （反 AI slop 清单）/ Final answer structure。
- `codex-rs/prompts/src/model_messages/multi_agent.rs`：**root** 与 **subagent** 两套角色文案，
  以及 `EXPLICIT_REQUEST_ONLY` / `PROACTIVE` 两种模式文案——即「角色」与「模式」是两根独立的轴。
- `codex-rs/prompts/src/multi_agent_instructions.rs`：把角色文案与**运行期能力**组合起来
  （并发槽位数、`wait_agent` 长等待提示、model override 提示），并用 `<multi_agent_role>` 标记包裹。
- `codex-rs/core/templates/collab/experimental_prompt.md`：多代理的四个正当用途、
  「必须告诉子代理它不是一个人」「跑日志类任务要禁止其再 fan out」「用完 close_agent」
  「`timeout_ms` 要合理缩放」。

### 1.2 pi（`D:/git_codes/pi`）

- `packages/coding-agent/src/core/system-prompt.ts`：系统提示词是**结构化分段**
  （`preamble` / `tools` / `rules` / `docs` / `skills` / `cwd`），每段可独立替换，并且有
  `diffSystemPromptSections()` 只推变化段——提示词更新不重发全量。
- `packages/coding-agent/src/core/tools/*.ts`：每个工具自带 `snippet` + `guidelines`，
  由 `buildRules()` 汇总成 rules 段；**规则跟着工具走**，没有中心化大 prompt。
- `examples/extensions/subagent/agents/{scout,planner,reviewer,worker}.md`：每个子代理一份
  systemPrompt + 明确输出格式 + 工具白名单 + 模型档位（scout 用 haiku、其余 sonnet）。
- `examples/extensions/subagent/prompts/{scout-and-plan,implement-and-review}.md`：
  把常用编排固化成提示词模板（`chain` + `{previous}` 占位符）。

### 1.3 Step-Code（`D:/git_codes/Step-Code`）

- `packages/coding-agent/src/features/workflow/step-workflow.ts`：workflow 工具 description 是本
  项目里最完整的编排提示词——**OPT-IN REQUIRED**（关键字 / 显式请求 / session 级
  `/ultraloop on` / saved workflow / skill，且「任务看起来大」**不构成** opt-in）、
  **When NOT to use**、**Single-phase patterns**（understand/design/review/research/migrate）、
  **Quality patterns**（adversarial verify / judge panel / loop-until-dry / multi-modal sweep /
  completeness critic）、**No silent caps**、**Sizing: default medium, under ~15 agents**、
  **Mechanics**（`parallel()` 是 barrier、`pipeline()` 无 barrier、schema 重试 3 次、
  budget fail-closed、`resumeFromRunId`、VM 无 Date/Math.random/network）。
- `packages/coding-agent/src/features/workflow/agent-runner.ts`：`buildAgentPrompt()` 给声明了
  schema 的子代理追加 `<workflow-structured-output>` 契约（32KiB 截断）——**结构化契约进 prompt**，
  而不是只放在 options 里等模型猜。
- `packages/coding-agent/src/features/workflow/hoh.ts`：Planner / Developer / QA 三个角色的
  JSON schema 与各自 prompt，含 `preservationConstraints`、`validationRequirements`、
  六个维度的 QA 证据与 `specCoverage`。
- `packages/coding-agent/src/features/step-subagent-agents.ts`：`BUILTIN_AGENTS` 目录
  （general / explore / review / planner，各带 tools 与 systemPrompt）+
  `formatBuiltinAgentGuidance()`——**工具描述从目录派生**，并显式提示
  「review/audit/exploration 优先用只读 agent，只有 general 能写文件」。

## 2. 逐场景差距

### 2.1 base prompt（`assets/sys_prompt.txt` / `_en`）

GA 已有 `# 身份 / # 能力与边界 / # 怎么工作 / # 验证纪律 / # 工具使用 / # 沟通与交付`。
对照 Codex `gpt_5_2_prompt.md`，缺的是**工作方式层**：

| Codex 段落 | GA 现状 | 建议 |
|---|---|---|
| Autonomy and Persistence：「除非用户明确只要计划/提问，否则默认用户要的是改动」 | `# 怎么工作` 有「自主性与持久性」，但没写「默认动手、不要把方案当交付」 | 补一句默认意图判定 |
| Planning：何时该计划、**好计划 vs 坏计划对照**、一次只一个 in_progress、计划不能过期 | 完全没有（GA 无 `update_plan`，最接近的是 `update_working_checkpoint`） | 新增 `# 规划与推进`，把 checkpoint 当计划载体 |
| Task execution：根因优先 / 最小改动 / 不修无关 bug / 不重复读回 / 不加许可头 / 不擅自 commit | 只有「先定位根因」「不要顺手修无关 bug」 | 新增 `# 代码与改动纪律` |
| Special user requests：用户说「review」时按代码审查输出（发现优先、按严重度、带 file:line、无发现也要说残余风险） | 没有 | 新增 `# 审查与产物质量` |
| Frontend tasks：反 AI slop（字体/配色/动效/背景要有意图，禁紫色默认） | 没有，但 GA 经常产出 HTML/UI | 同上 |
| Validating your work：从最贴近改动的最小验证开始、逐步扩大、没有测试的仓库不要凭空加测试 | 有「先跑最小验证」 | 补「没测试的仓库不要引入测试框架」 |

### 2.2 subagent / multi-agent（`subagent_prompts.py` + `assets/tools_schema.json`）

GA 已经有比想象中好的基础：`build_agent_role_usage_hint()` 区分 root/subagent，
`.ga/subagents` 有角色注册表，spawn_agent 的 description 已包含「任务契约」要素。缺的是：

1. **共享工作区警告**。Codex 把「必须告诉子代理你不是一个人，不要影响/回滚别人的工作」写成硬要求；
   GA 的 root hint 没有这句，子代理 hint 也没有「只改你负责的路径」。
2. **收尾卫生**。Codex 要求用完 `close_agent`；GA 有 `close_agent`（含 `cascade`），
   但提示词从不提，子代理跑完长期占槽。
3. **投递语义**。Codex 明确告诉子代理「你在 final 通道的输出会立刻回传给父代理」；
   GA 只说「最终回答」，没说这份回答是给调度器/父代理读的、要按契约字段写。
4. **并发槽位**。Codex 注入「There are N available concurrency slots」；GA 没有，
   模型容易一次 spawn 过多。
5. **角色目录的能力标注**。Step-Code 在 subagent 工具描述里列出内置 agent 及其能力档并提示
   「review/audit/exploration 优先只读 agent」；GA 的 `spawn_agent` description 没列出
   `permission_profile` / 工具档位该怎么选。

### 2.3 workflow planner（`workflow_planner.py::_planner_prompt`）

现状：`orchestrationPolicy` 有 30+ 条，几乎全是**合同机械**——taskType 只是提示、artifact 必须
声明 requiredChecks、capability 类不能写具体工具名、schemaRef 必须完整、action id 要对齐、
路径只能 workspace-relative、parallel 是 barrier……

缺的是 Step-Code 那一整套**编排策略**：

- **何时不用 workflow**（单文件改动、一次性查询、3 次检索能答完）；
- **命名的编排模式**（对抗验证 / 评审团 / 穷尽式搜索 / 多模态扫描 / 完整性批评者）；
- **把 agent 花在验证上，而不是只花在生成上**；
- **规模默认值**（Step-Code：default medium，~15 agents 以内）；
- **no silent caps**（丢弃/截断/抽样必须显式记录）；
- **每个 agent prompt 的要素**（目标、输入、输出契约、完成判据）；
- **phase 命名按交付物**（便于人类和 UI 读懂）。

### 2.4 workflow child（`workflow_child_agent.py::_build_prompt`）

现状：身份一行 + 4 条一句话角色指令（tests/implementation/verification/review）+ 依赖交接块。
对照 Step-Code / pi：

- 角色指令缺 `research`、`synthesis`、`understanding`、`contract`、`repair`、`summary`
  ——而 GA 的 `CODING_AGENT_ROLES` 里**本来就有**这些 canonical role，等于角色声明了但没指导；
- 缺输出/交接契约（「你的回答会被调度器和下游 agent 读」）；
- 缺共享工作区警告（同一个 run 的其他 child 与根代理共享 workspace）；
- 缺「不许编造证据」的显式表述（verification 那条有，research 没有）。

### 2.5 机制层：单一事实来源

三个参考实现都刻意避免「同一条规则写两遍」：

- Step-Code：`formatBuiltinAgentGuidance()` 从 `BUILTIN_AGENTS` 渲染工具描述；
- pi：`toolSnippets` / `toolGuidelines` 由工具模块自己贡献，`buildRules()` 汇总；
- Codex：`ResolvedMessage`（目录覆盖 + 默认值）+ 运行期能力注入。

GA 现在同一条规则散落在三处：`assets/sys_prompt.txt`（base）、`assets/tools_schema.json`
（工具 description）、`GA_AGENTS.md`（项目层）。这轮先做到「新增的编排策略只有一处定义」，
把全面收敛放进 P1。

## 3. 实施计划

### P0（本轮实施）

| 项 | 改动 | 验收 |
|---|---|---|
| P0-1 base prompt 补工作方式层 | `assets/sys_prompt.txt` + `sys_prompt_en.txt`：新增 `# 规划与推进`、`# 代码与改动纪律`、`# 审查与产物质量`，`# 验证纪律` 补「没测试不要引入测试框架」 | `tests/test_ga_agents_runtime.py::RepoPromptLayersTest` 新增断言（中英同步） |
| P0-2 subagent 提示词补共享环境与收尾 | `subagent_prompts.py`：root hint 加「共享工作区/互不重叠写入/收尾/并发槽」，subagent hint 加「只改自己的路径/回答会回传父代理/不要 fan out」 | `tests/test_agentmain_role_prompts.py` 扩展断言 |
| P0-3 planner 增补编排 playbook | `workflow_planner.py::_planner_prompt`：新增 `orchestrationPlaybook`（何时不用 / 模式 / 验证投入 / sizing / no silent caps / agent prompt 要素 / phase 命名） | `tests/test_workflow_prompt_guided_planner.py` 新增断言，且 repair 轮同样携带 |
| P0-4 workflow child 角色与交接契约 | `workflow_child_agent.py`：角色指令扩到 canonical roles，新增共享工作区与 handoff 契约行 | `tests/test_workflow_child_agent.py` 新增断言 |

### P1（下一轮）

- P1-1 单一事实来源：把工具档位/编排规则抽到独立模块，planner prompt、`GA_AGENTS.md`、
  工具 description 从它派生（对齐 Step-Code `formatBuiltinAgentGuidance`）。
- P1-2 子代理角色目录进 spawn 描述：把 `.ga/subagents` 的角色与能力档渲染进 `spawn_agent`
  description（对齐 Step-Code 的「review/audit 优先只读 agent」提示）。
- P1-3 pi 式结构化 prompt 分段 + 增量更新（避免每轮全量重发系统提示词）。

### P2（观察）

- P2-1 workflow 变成模型可调用工具（对齐 Step-Code 的 opt-in 语义），而不是只靠 UI `/workflow`。
- P2-2 计划规模反馈：宿主校验「DAG 是否与任务规模匹配」（例如研究型任务只出 1 个 agent 时提示）。

## 4. 验收方式

- 单元测试：`python -m unittest tests.test_ga_agents_runtime tests.test_agentmain_role_prompts
  tests.test_workflow_child_agent tests.test_workflow_prompt_guided_planner`
- 全量回归：`python -m unittest discover -s tests`
- 真实 E2E（可选，需用户授权 provider）：`GA_RUN_REAL_WORKFLOW_DEGRADED_E2E=1` 等既有真实用例。

## 5. 实施记录

见本文件末尾「实施记录（本轮）」。

## 5. 实施记录（本轮，2026-10-10）

### 已实施 P0-1 ~ P0-4

| 项 | 文件 | 具体改动 |
|---|---|---|
| P0-1 base prompt 工作方式层 | `assets/sys_prompt.txt`、`assets/sys_prompt_en.txt` | 新增 `# 规划与推进`（默认动手而不是交方案、用 `update_working_checkpoint` 当计划载体、一次只一件进行中、计划别过期、简单任务别套计划）、`# 代码与改动纪律`（根因优先、最小改动、不删测试、改前先读、改完不重复读回、不加许可头/注释、不擅自 commit）、`# 审查与产物质量`（review 按「发现优先 + 严重度 + file:line + 残余风险」、反 AI slop 前端线、文档独立可读）；`# 验证纪律` 补「本来没有测试的仓库不要凭空引入测试框架」 |
| P0-2 subagent / multi-agent 提示词 | `subagent_prompts.py` | root hint 补：共享工作目录、必须在 message 里告诉子代理「你不是一个人」、分配互不重叠的写入路径、子代理默认隔离上下文、并发槽位有限、长等待而非短轮询、完成后 `close_agent`（带子代理时 `cascade=true`）；subagent hint 补：只改自己负责的路径、不回滚别人的改动、编排工具已剥离、最终回答直接回传父代理、引用要标来源、不许编造数字/日期/引用 |
| P0-3 planner 编排 playbook | `workflow_planner.py::_planner_prompt` | 新增 `orchestrationPlaybook`：先选模式再填字段 + 六种模式（并行调研→综合 / 对抗验证 / 评审团 / 穷尽式搜索 / 多模态扫描 / 完整性批评者）、把 agent 花在验证上、规模默认中等约 15 个 agent、no silent caps、什么时候不要用 workflow、每个 agent prompt 的四要素、phase 按交付物命名；repair 规则补「不要靠静默删 agent / 检查来满足 validator」 |
| P0-4 workflow child 角色与交接契约 | `workflow_child_agent.py` | 角色指令从 4 条扩到 10 条 canonical role（新增 research / synthesis / understanding / contract / repair / summary），每条写明必须产出什么、什么算证据；`_build_prompt` 新增 `sharedWorkspace:` 与 `handoff:` 两行 |

### 证据：红→绿

新增断言所依赖的文本在 `HEAD` 中全部不存在，因此这些测试在改动前必然失败：

```
ABSENT-BEFORE  assets/sys_prompt.txt      规划与推进
ABSENT-BEFORE  assets/sys_prompt_en.txt   Planning and progress
ABSENT-BEFORE  subagent_prompts.py        你不是一个人在这个工作区
ABSENT-BEFORE  subagent_prompts.py        回传给父代理
ABSENT-BEFORE  workflow_planner.py        orchestrationPlaybook
ABSENT-BEFORE  workflow_child_agent.py    sharedWorkspace
```

### 证据：运行期真实装配

只 grep 源文件不够，下面是从真实装配函数里取出的成品提示词：

```
root  prompt 16211 chars  -> [GA_ROOT_AGENT_USAGE_HINT]；含 # 规划与推进 / # 代码与改动纪律 /
                             # 审查与产物质量 / 共享同一个工作目录 / close_agent
sub   prompt 15889 chars  -> [GA_SUBAGENT_USAGE_HINT]；含 回传给父代理 / 对你已剥离
child prompt  1496 chars  -> sharedWorkspace: / handoff: / research 角色指令
```

### 证据：测试

```
python -m unittest tests.test_ga_agents_runtime tests.test_agentmain_role_prompts tests.test_workflow_child_agent  ->  66 OK
python -m unittest tests.test_workflow_prompt_guided_planner                                                       ->  25 OK
python -m unittest discover -s tests -p "test_subagent*.py"                                                        -> 224 OK (skipped=2)
python -m unittest discover -s tests                                                                               -> 1378 OK (skipped=3)
```

### 已实施 P1-2（同一轮续做）

对齐 pi `examples/extensions/subagent/agents/*.md` 与 Step-Code
`features/step-subagent-agents.ts::BUILTIN_AGENTS` + `formatBuiltinAgentGuidance()`：

| 项 | 文件 | 具体改动 |
|---|---|---|
| 内置只读角色 | `subagent_roles.py` | 新增 `BUILTIN_ROLES` = `explore` / `plan` / `review`，均 `permission_profile=READ_ONLY`，各带 description / when_to_use / system_prompt，`source_path="builtin:<name>"`；`get()` 在文件查找 miss 后回落 builtin，`list_roles()` 项目角色优先、再追加未被覆盖的 builtin（同名项目角色始终胜出） |
| 能力注解 + 目录渲染 | `subagent_roles.py` | 新增 `role_capability_note()`（从 `permission_profile` 派生 read-only / can write，不靠手写名单）与 `format_role_catalog()`（渲染 `name (capability — description)`：能力档始终由 `permission_profile` 派生，描述只作细节，避免手写标签与边界脱节；中文用「各自的能力：…。」） |
| 目录进 spawn 描述 | `agentmain.py::_apply_subagent_role_schema` | roles 排序后 `agent_type.enum` 用角色名，描述里嵌入 `format_role_catalog(...)`，并补「审查 / 审计 / 探索类工作优先用只读角色，只有通用子智能体或声明了写权限的项目角色才能改文件」 |

为什么不能只列名字：选择发生在模型看到目录之前，所以能力档必须写进 schema 描述——与 Step-Code
把 `formatBuiltinAgentGuidance()` 渲染进 subagent 工具是同一个理由。

### 证据：P1-2 红→绿

```
RED   tests/test_subagent_roles.py          BUILTIN_ROLES / format_role_catalog / role_capability_note 尚不存在
RED   tests/test_agentmain_mcp_tools.py     agent_type.enum 在无 .ga/subagents 时为空、描述无目录
GREEN tests/test_subagent_roles.py          test_builtin_roles_exist_without_any_configuration 等 4 项
GREEN tests/test_agentmain_mcp_tools.py     test_load_tool_schema_publishes_the_builtin_role_catalog
GREEN tests/test_ga_subagent_tools.py       available_agent_types 断言更新为内置目录 ['explore','plan','review']
```

行为变化：`spawn_agent` 报 `unknown_agent_type` 时 `available_agent_types` 不再可能为空，
至少列出三个内置只读角色；`ga.py` 的 `agent_type` 校验因此也自动接受内置角色。

### 未做（按计划留给下一轮）

P1-1 单一事实来源收敛（工具档位 / 编排规则抽模块并派生）、P1-3 pi 式结构化分段与增量更新、
P2-1 workflow 工具化、P2-2 计划规模反馈。
