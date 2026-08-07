# GA Workflow 缺陷复核与修复顺序

日期：2026-08-05

范围：GenericAgent Dynamic Workflow 的 planner、受限 JS runtime、workflow child agent、P8 真实 API harness，以及与 workflow 直接相关的安全和 artifact 边界。

第 1-7 节基于提交历史、现有实现、确定性测试和最小只读探针整理，记录修复前的缺陷判断；第 8 节记录后续代码修复和验证结果。报告不代表真实 API 在所有 provider、网络和任务类型下的行为。

## 1. 结论摘要

当前 workflow 的底层调度链路基本可用：JS worker、phase/log/agent/parallel/pipeline、child agent artifact、transcript 隔离、schema fallback、默认工具继承和 resume/cache 均有测试或历史真实 E2E 证据。

但 workflow 仍不能作为可靠的工程质量闭环使用。最严重的问题是：workflow 只要脚本正常返回就可以被标记为 succeeded，测试失败不会自动阻止成功，也不会自动进入 repair/retest。除此之外，planner 的 coding 并行约束、脚本安全预检、secret scanner 和 workspace 传递仍存在边界缺陷。

## 2. 文档与历史来源

### 2.1 主要分析文档

1. `docs/claude_code_dynamic_workflow_reference.md`

   - 首次提交：`a165858`，提交信息为 `docs(workflow): 记录 Claude Code 动态工作流参考分析`。
   - 重点结论：只并行独立任务；最终 tests 与 implementation 不应并行；测试 gate 应由 host/runtime 控制；review 应连接 repair/retest；schema 失败应有 fallback；子 agent transcript 和 progress 应独立记录。
2. `docs/P8_real_api_e2e_defect.md`

   - 首次提交：`8289ce7`，提交信息为 `docs(workflow): 记录真实多 agent E2E 缺陷`。
   - 真实多 agent E2E 记录了错误并行、缺少 TDD gate、review 无法驱动 repair、harness secretScan 误报以及 skill/tool 能力继承等问题。
3. `docs/GA_workflow_defect_optimization_plan.md`

   - 该文档将 Test Gate 列为阶段四，将 TDD workflow template 与 Repair Loop 列为阶段五；截至本次检查，代码中仍未出现对应的 `runPythonUnittest`、`TEST_FAILURES` 或 workflow test-gate 原语。

### 2.2 早期原始研究材料

被 `.gitignore` 忽略的本地目录：

`动态工作流原理分析与子智能体会话存储的细节与ga复刻路线图/`

其中的 `dynamic-workflow-实现.md`、`ga-dynamic-workdlow-复刻.md` 和 `dynamic-workfows的子智能体会话存储细节.md` 是 2026-05-30 至 2026-05-31 左右形成的研究材料。它们基于 Claude Code Dynamic Workflow 的公开介绍和实际 workflow/subagent artifact，早于当前 GA subagent 控制面实现，不属于 Git 历史中的已跟踪文档。

另外发现被忽略的 `docs/recent_workflow_commit_review.md`。该审查中的 `${...}` 模板插值问题已经由 `f26b076` 修复；其中关于 `mykey.json` 和 `mcp.json` 校验不完整的结论在当前代码中仍成立。

## 3. 当前仍存在的缺陷

### P0-1：没有受控 Test Gate，失败测试可以被标记为成功

**证据：** `workflow_runtime.py:146-154` 在收到 JS worker 的 `done` 消息后直接设置 `run.status = "succeeded"`。worker 当前只暴露 `agent`、`phase`、`log`、`parallel` 和 `pipeline`，没有 `runPythonUnittest` 或等价的 host gate。

**最小复现：** Fake child 返回 `verificationPassed=false`，workflow 最终结果仍为：

```text
runtimeStatus=succeeded
returnedVerificationPassed=false
jobStatus=succeeded
```

**影响：** workflow 的 succeeded 只表示编排脚本结束，不表示代码、测试或验收条件成立。真实工程任务可能带着失败测试生成“成功”报告。

### P0-2：没有执行阶段的 review -> repair -> retest 闭环

当前 `workflow_planner.py:319-335` 的 `repair_attempts` 只针对 planner 生成的 WorkflowPlan JSON 做 validator repair。它不会读取 child agent 生成的代码、测试失败日志或 review 结果，也不会启动 repair agent 后重新运行测试。

**影响：** review 可以发现错误，但发现结果不能改变最终产物；P8 文档中记录的 API contract 漂移和解析 bug 仍可能被汇总成最终成功结果。

### P1-1：coding 并行禁令可以通过省略 role 绕过

`workflow_planner.py:435-437` 只有在同一 phase 中出现 `role=tests` 和 `role=implementation` 时才拒绝并行。当前 planner 的 `requiredShape` 也没有把 `role` 声明为必填字段。

**最小复现：** 同一 coding phase 中放入 labels 为 `tests`、`implementation` 的两个 agent，但省略 role，validator 返回：

```json
{"ok": true, "issues": []}
```

**影响：** LLM 计划只要省略 role，就可以恢复历史上导致 contract 漂移的错误拓扑。

### P1-2：脚本安全预检扫描了 prompt 数据，误伤普通输入

`workflow_runtime.py:22-32` 定义了 `process`、`import`、`fetch` 等禁用词，`workflow_runtime.py:312-315` 又把整个 script 文本直接扫描。agent prompt 中出现普通句子 `please process this ordinary prompt` 时，runtime 会抛出：

```text
ValueError: workflow script uses forbidden token: process
```

planner validator 对 agent prompt 也采用相同类型的关键词扫描。

**影响：** 合法任务可能在 worker 启动前失败；提示词只要讨论进程、导入、fetch 或相关技术概念，就可能被误判为脚本能力越界。

### P1-3：planner 的敏感文件安全边界校验不完整

`workflow_planner.py:456-457` 目前只检查 prompt 是否包含 `mykey.py` 和 `不要提交`，没有强制检查 `mykey.json`、`mcp.json`。

**最小复现：** prompt 仅包含 `不要读取 mykey.py；不要提交` 时，validator 返回 `ok=true`。

**影响：** planner prompt 中声明的安全契约和实际 validator 不一致，计划可能在缺少完整敏感文件边界的情况下被接受。

### P1-4：P8 secret scanner 对安全 fixture 误报，并直接阻断 harness

`tests/p8_real_api_e2e.py:37-44` 保留了泛化的 `api_key_field` 规则，`scan_for_secret_material()` 会扫描 workspace 中所有文件，并在 `main()` 中用 `not summary["secretScan"]` 直接决定整体通过与否。

**最小复现：** 临时 fixture 内容为 `api_key=demo`、`token=placeholder` 时，scanner 仍报告 `api_key_field` 命中。

**影响：** 这是 P8 测试 harness 的门禁缺陷，不等价于真实密钥泄漏；它会掩盖 workflow 功能结果并造成错误失败。

### P1-5：workflow args 中的 workspace 没有自动传给 child agent

JS worker 能收到 `args`，但 `AgentScheduler.register_agent()` 的 job metadata 没有保存 workspace；`workflow_child_agent.py:445-446` 固定使用 `temp/workflow_child_agents/<job_id>` 作为 child cwd。

**影响：** 使用相对路径的 coding workflow 可能在错误目录读写。当前 E2E 通过 prompt 拼接完整 workspace 路径规避了问题，但这不是 runtime contract。

### P2-1：process subagent 与 workflow child agent 仍是两套运行体系

GA 既有传统 `agentmain.py --task` subagent，也有 `NativeGPTChildAgentRunner` workflow child。两者的状态、artifact、通知和生命周期契约并未完全统一。

**影响：** 当前不构成已复现的 workflow 主路径失败，但会增加权限、workspace、取消、结果归档和 UI 观测行为不一致的风险。建议在 P0/P1 修复后单独收敛。

## 4. 已确认修复或不应重复修复的问题

1. 默认 deterministic coding plan 已采用 `Understand -> Tests -> Implementation -> Verification` 顺序，见 `workflow_planner.py:126-170`。
2. `${...}` prompt 模板插值已转义，见 `workflow_planner.py:539-540`；对应 literal prompt 回归测试已通过。
3. child agent 默认工具、`load_skill` 和 discovered MCP tools 的继承已恢复，相关测试覆盖完整工具 schema、skill 和 MCP 调用。
4. schema validation fallback、provider error 脱敏、resume/cache 上下文边界以及近期 external kill 状态覆盖问题已有实现和回归测试。
5. 当前 GA 的 skill contract 是 optional skill awareness：skill 可见、可调用、调用可审计，但“不调用 skill”本身不失败。因此不能把“没有强制 load_skill”直接判定为当前 contract 的缺陷。

## 5. 按严重度的修复顺序

### 第一阶段：P0，先保证 workflow 不会错误报成功

1. **实现受控 Test Gate。**

   - 增加 host-side `runPythonUnittest`，只允许结构化参数，不接受任意 shell 字符串。
   - 校验 workspace 必须位于允许的 sandbox 根目录内。
   - 增加 timeout、stdout/stderr 截断和敏感文件拒绝。
   - 将 gate 结果写入 `TEST_RED_RESULT.json`、`TEST_GREEN_RESULT.json` 或等价 artifact。
   - 增加 `workflow_test_gate_started/completed/failed` journal 事件。
2. **将最终 succeeded 与 gate 结果绑定。**

   - JS worker 正常结束只能表示脚本完成。
   - workflow 只有在显式 gate 通过、所有必需 job 成功、最终结果结构有效时才能标记 succeeded。
   - gate 失败必须进入 failed 状态，并在 final result 中保留结构化失败原因。
3. **实现 bounded repair/retest。**

   - 测试失败后写入 `TEST_FAILURES.txt` 或结构化失败 artifact。
   - 启动 repair child agent，明确其读取失败日志、源码和测试的范围。
   - 每轮 repair 后重新执行同一个受控 gate，最多允许固定轮数。
   - 超出轮数后 failed，不允许由 final/synthesis agent 覆盖失败状态。

### 第二阶段：P1，封住 planner、预检和验收边界

4. **修复 coding topology validator。**

   - 将 `role` 加入 WorkflowPlan schema 并设为必填，或由 label/prompt 推断后统一校验。
   - 对 coding 任务禁止同阶段最终 tests 与 implementation 并行，不依赖 role 是否存在。
   - 增加省略 role、改用别名 label、同阶段依赖等负向测试。
5. **分离脚本代码扫描与 prompt 数据。**

   - 不再对完整 script 文本做简单关键词拒绝。
   - 优先让 renderer 使用结构化数据或 JSON 字符串传递 prompt，再对真正的脚本结构做 AST/语法级白名单校验。
   - 保留对真实 runtime 能力越界的拒绝，并补充普通 prompt 中 `process/import/fetch` 等词汇的回归测试。
6. **补全敏感文件边界和 secret hygiene。**

   - validator 统一要求 `mykey.py`、`mykey.json`、`mcp.json` 和 no-commit 边界。
   - P8 scanner 改为高置信 key 格式阻断；`api_key=demo`、`token=placeholder` 等 fixture 只做 warning 或明确排除。
   - 真实 provider key、JWT、Bearer token 等高置信格式仍必须阻断。
7. **统一 workspace contract。**

   - workflow runtime 对 workspace 做规范化和越界校验。
   - 将经校验的 workspace 显式写入 job metadata，并由 child runner 作为 cwd 使用。
   - prompt 中的完整路径只作为辅助信息，不能代替 runtime 传递。

### 第三阶段：P2，收敛架构和扩展验证

8. **统一传统 subagent 与 workflow child 的生命周期契约。**

   - 统一 run/job 状态、取消、通知、transcript、artifact 和 workspace 语义。
   - 复用已有 subagent v2 控制面能力，避免维护两套相近协议。
9. **补充真实 API TDD workflow E2E。**

   - 真实模型只用于验证 planner/child 行为；测试 gate 必须由 host 控制。
   - 至少覆盖：先红、实现、绿、review 发现错误、repair 后重新测试、最终失败和最终成功两条路径。
   - 真实 API E2E 仍保持 opt-in，不将 provider 波动误判为 workflow 逻辑缺陷。

## 6. 初始调查验证记录（修复前基线）

本次运行的命令和结果：

```text
python -m unittest discover -s tests -p 'test_workflow*.py'
Ran 168 tests in 30.099s
OK (skipped=1)

python -m unittest tests.test_p8_real_api_harness tests.test_p8_real_api_e2e_diagnostic tests.test_p8_real_api_stress_e2e tests.test_p8_real_api_stability_e2e
Ran 32 tests in 0.276s
OK
```

本节记录的是缺陷调查阶段：当时未调用真实 LLM API，仅使用现有确定性测试和临时目录探针验证结构性缺陷；当时未修改代码，检查前工作区仅有用户已有的 `.gitignore` 改动。修复后的验证结果见第 8 节。

## 7. 参考资料

1. `docs/claude_code_dynamic_workflow_reference.md`，提交 `a165858`。
2. `docs/P8_real_api_e2e_defect.md`，提交 `8289ce7`。
3. `docs/GA_workflow_defect_optimization_plan.md`。
4. `docs/dynamic-workflows-implementation-roadmap.md`。
5. 本地忽略文件 `docs/recent_workflow_commit_review.md`，仅作为补充审查记录，不属于当前 Git 提交历史。

## 8. 修复进度（2026-08-05）

### P0-1：受控 Test Gate

状态：**已修复，已完成确定性回归验证。**

已实现：

- JS worker 暴露结构化 `runPythonUnittest` RPC；host 只执行固定的 `python -m unittest discover`，不接受 shell 字符串。
- runtime 校验 workflow args 中的可信 `workspacePath`，拒绝 workspace 越界、相对路径 `..`、敏感文件入口、超出上限的 timeout 和非文件名 pattern。
- gate 结果写入 `test-gates/gate-N.json`；失败输出写入 `TEST_FAILURES.txt`；journal 记录 `workflow_test_gate_started`、`workflow_test_gate_completed` 和失败 gate 的 `workflow_test_gate_failed`。
- `expect: 'fail'` 支持 TDD red gate：测试实际失败但满足预期时，`gatePassed` 为 true，不会误判 workflow 失败。
- required gate 未通过时，即使 JS worker 正常返回，runtime 也会写入 `failed` 终态；显式顶层 `verificationPassed: false` 同样不能被标记为成功。

验证：

```text
python -m unittest discover -s tests -p 'test_workflow*.py'
Ran 175 tests in 31.383s
OK (skipped=1)
```

新增覆盖包括：失败 gate 阻断成功、通过 gate、预期失败 red gate、workspace 越界、timeout、输出截断、显式失败 verification 结果。

### P0-2：review -> repair -> retest

状态：**已修复，已完成确定性回归验证。**

已实现：

- JS worker 增加 bounded `repairAndRetest` 原语：先执行 required unittest gate，失败后读取 `TEST_FAILURES.txt` 的约定位置并启动 repair child，再使用同一 `gateKey` 重跑相同测试。
- `maxAttempts` 由 DSL 限制为 `0..3`；达到上限仍未通过时，runtime 以 `failed` 终态结束，不能由最后的 repair/synthesis 返回值覆盖。
- 同一 gate 的中间失败只作为 repair 证据保存；同一 `gateKey` 的最终 retest 通过后，workflow 才能成功。独立 gate 的失败仍会阻止成功。
- repair child 使用现有 scheduler/job artifact/transcript 链路，label 为 `repair-N`，失败测试结果和每一轮 gate artifact 可审计。

验证：

```text
python -m unittest discover -s tests -p 'test_workflow*.py'
Ran 177 tests in 33.819s
OK (skipped=1)
```

新增覆盖包括：repair 修改 workspace 后同 gate 复测成功，以及 repair 达到上限后保留失败终态。

真实 LLM 验证：

```text
profile: luna
model: gpt-5.6-luna
runStatus: succeeded
gate-1: passed=false, gatePassed=false
repair child: repair-1, status=succeeded, tools=file_read/file_write
gate-2: passed=true, gatePassed=true, same gateKey
repairAttempts: 1
```

真实验证 harness：`tests/real_workflow_p0_e2e.py`，默认跳过，使用 `GA_RUN_REAL_P0_WORKFLOW_E2E=1` opt-in；本次真实运行通过。另有 `tests.test_workflow_real_llm_integration` 使用同一 profile 完成真实 child 短 prompt 验证。

最终收尾验证：

```text
python -m unittest discover -s tests
Ran 886 tests in 166.715s
OK (skipped=3)

python -m unittest tests.test_p8_real_api_harness tests.test_p8_real_api_e2e_diagnostic tests.test_p8_real_api_stress_e2e tests.test_p8_real_api_stability_e2e
Ran 32 tests in 0.264s
OK
```

### P1-1：coding plan 的 role 绕过

状态：**已修复，已完成确定性和真实 LLM 验证。**

已实现：

- coding workflow 的每个 agent 必须声明 canonical `role`；缺失时产生 `missing_coding_role`，未知或别名 role 产生 `invalid_coding_role`。
- canonical role 集合包括 `understanding`、`contract`、`tests`、`implementation`、`verification`、`review`、`repair`、`summary`、`synthesis`。
- topology validator 对 role 做规范化后继续拒绝同一 phase 的 `tests` 与 `implementation` 并行；research/review 等非 coding 计划仍可使用自由并行拓扑。
- deterministic coding plan 的 `understand` agent 补充 `role=understanding`；LLM planner 的 orchestration policy 和 `requiredShape` 同步声明 role contract。

TDD 验证：

```text
RED: 缺省 role 和 test-writer 别名均被旧 validator 错误接受；3 个新增测试失败。
GREEN: python -m unittest tests.test_workflow_plan_validator tests.test_workflow_prompt_guided_planner tests.test_workflow_planner_compiler
Ran 25 tests
OK
```

覆盖包括：缺省 role、非 canonical role、planner repair 传递 `missing_coding_role`、以及修复后的 coding script 不含 `await parallel([`。

真实 LLM 验证：

```text
profile: luna
model: gpt-5.6-luna
plannerMode: prompt_guided
calls: 1
phaseTitles: Understand -> Tests -> Implementation -> Verification
roles: understanding, tests, implementation, verification
validation.ok: true
hasParallel: false
```

最终回归：

```text
python -m unittest discover -s tests
Ran 889 tests in 164.421s
OK (skipped=3)
```

### P1-2：脚本安全预检扫描 prompt 数据，误伤普通输入

状态：**已修复，已完成确定性、真实 LLM 和 GA Ink bridge 入口验证。**

根因确认：runtime 的 `_scan_script()` 在 Node worker 启动前把完整 workflow script 当作代码文本扫描；而 `agent(...)` 的 prompt 只是脚本中的字符串数据。planner validator 又对每个 agent prompt 重复扫描同一组 `require/import/process/fetch/...` 关键词。于是普通技术描述会在真正的 VM capability boundary 之前失败，且失败原因与实际执行能力无关。

参考 Codex/Claude Code 源码后的结论：两者均把安全决策放在实际能力边界，而不是对完整 prompt 做关键词拒绝。Codex 的 exec 路径由 `core/src/exec.rs` 构造 `ExecRequest` 后统一进入 sandboxing，并结合 `PermissionProfile`、approval policy 和 exec policy；Claude Code 通过 `src/hooks/toolPermission` 的 tool permission context 与 `src/utils/sandbox/sandbox-adapter.ts` 对具体工具执行做 permission/sandbox 控制。未发现与 GA 原预检等价的“扫描整个 workflow/prompt 文本并按禁词拒绝”机制。

修复内容：

- 删除 `WorkflowRuntime` 的 `FORBIDDEN_SCRIPT_TOKENS`、`_scan_script()` 及 `run()` 调用。
- 删除 planner validator 对 agent prompt 的 `forbidden_token` 扫描；保留 workflow plan 结构、canonical role、依赖和同 phase topology 校验。
- 移除真实 planner harness 中已经失真的“rendered script 不得包含禁词”断言。
- 保留实际安全边界：Node `vm` context 不提供 `process`、`require`、`fetch`、`XMLHttpRequest`、`WebSocket`、`Deno`、`Bun`，并禁用 string/wasm code generation；host RPC 仍校验参数、workspace、敏感文件、timeout、审批和 test gate。
- planner 的敏感文件 prompt 契约属于 P1-3，已在后续修复中删除；runtime 的 host-side test gate 不受影响。

TDD 验证：

```text
RED:
  test_runtime_allows_script_words_inside_agent_prompt
    -> ValueError: workflow script uses forbidden token: import
  test_allows_script_words_in_prompt_when_safety_boundary_is_present
    -> validator returned forbidden_token for import/process/fetch

GREEN:
  python -m unittest tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_allows_script_words_inside_agent_prompt tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_allows_script_words_in_prompt_when_safety_boundary_is_present
  Ran 2 tests
  OK

  python -m unittest tests.test_workflow_runtime
  Ran 55 tests
  OK

  python -m unittest tests.test_workflow_plan_validator tests.test_workflow_prompt_guided_planner tests.test_workflow_planner_compiler
  Ran 26 tests
  OK

  python -m unittest tests.test_ink_bridge
  Ran 92 tests
  OK
```

覆盖结论：普通 prompt 可以包含 `process/import/fetch` 并完成 child agent 调用；workflow runtime 仍观测到受限能力为 `undefined`；GA Ink bridge 的 approve 入口访问 `process.env` 仍由 VM 失败并进入 failed/idle 终态。

真实 LLM 验证：

```text
profile: luna
model: gpt-5.6-luna
plannerMode: prompt_guided
planner.validationOk: true
planner.issueCodes: []
planner.promptContainsScriptWords: true
runtime.status: succeeded
runtime.model: gpt-5.6-luna
runtime.jobStatuses: succeeded
  runtime.responseNonEmpty: true
```

### P1-3：planner 的敏感文件安全边界 prompt 预检不完整

状态：**已修复，已完成 TDD、确定性回归和真实 LLM 验证。**

根因确认：`workflow_planner.py` 的 validator 只检查 agent prompt 是否包含 `mykey.py` 和 `不要提交`，遗漏 `mykey.json`、`mcp.json`；这既不能形成实际文件访问权限，也会把没有按模板背诵提示的合法计划判为无效。默认 workflow child 的 `inherit-current-permissions` 本身按工具放行，prompt 文本检查无法阻止 `file_read` 访问路径。

修复内容：

- 删除 `validate_workflow_plan()` 的 `missing_safety_boundary` 检查。
- 删除 Native planner client 和真实 planner harness 中要求每个 `agent.prompt` 携带敏感文件提示的硬性输出要求。
- 删除 LLM planner orchestration policy 中对应的 prompt 契约。
- 删除 deterministic planner 的 `_prompt_boundary()` 及其固定敏感文件文本注入。
- 保留 workflow plan 的 `constraints` 元数据，以及 runtime 的 workspace、路径、超时和敏感文件 test gate；这些不依赖 prompt 背诵。

TDD 验证：

```text
RED:
  6 个新增/调整回归测试按预期失败：
  - 普通 prompt 被 missing_safety_boundary 拒绝
  - process/import/fetch prompt 被 missing_safety_boundary 拒绝
  - deterministic planner 注入 mykey.py 等固定文本
  - LLM planner 请求和 Native client 仍包含硬性敏感文件要求

GREEN:
  python -m unittest tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_rejects_undefined_dependency_and_schema_without_prompt_boundary tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_accepts_agent_prompt_without_sensitive_file_template tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_allows_script_words_in_prompt_without_safety_boundary tests.test_workflow_planner_compiler.WorkflowPlannerCompilerTest.test_deterministic_planner_does_not_inject_sensitive_prompt_boundary tests.test_workflow_prompt_guided_planner.LLMWorkflowPlannerTest.test_prompt_guided_request_does_not_require_sensitive_prompt_boundary tests.test_workflow_prompt_guided_planner.NativeWorkflowPlannerClientTest.test_native_client_uses_yaml_session_and_parses_json_without_markdown
  Ran 6 tests
  OK

  python -m unittest tests.test_workflow_plan_validator tests.test_workflow_prompt_guided_planner tests.test_workflow_planner_compiler tests.test_workflow_runtime tests.test_workflow_permissions
  Ran 94 tests
  OK
```

真实 LLM 验证：

```text
profile: luna
model: gpt-5.6-luna
request: minimal review WorkflowPlan with plain agent prompt
agent.prompt: 审查当前任务并返回 findings。
validationOk: true
issueCodes: []
```

备注：完整四场景 `GA_RUN_REAL_PROMPT_PLANNER_E2E=1` harness 也确认了 `luna / gpt-5.6-luna` 配置，但在第二个场景被已有 renderer 缺陷阻断：LLM 返回 schema key `scope-baseline`，renderer 生成非法 JavaScript `const scope-baseline = ...`。该问题与 P1-3 无关，未在本次修改中扩大处理范围。

最终回归：

```text
python -m unittest discover -s tests
Ran 894 tests in 165.352s
OK (skipped=3)
```

### P1-4：P8 secret scanner 对安全 fixture 误报

状态：**已修复，已完成 TDD、确定性全量回归和真实 `gpt-5.6-luna` P8 验证。**

根因确认：`tests/p8_real_api_e2e.py` 原先让同一组 `SECRET_PATTERNS` 同时负责输出脱敏和 blocking artifact scan。其 `api_key_field`、`x_api_key` 以及任意 `sk-*` 规则会扫描 prompt、transcript、tool schema 和测试 fixture，因此 `api_key=demo`、`token=placeholder` 这类安全文本也会直接使 `summary["passed"]` 变为 false；`.pyc/.pyo` 也会被当作普通文本扫描。

修复内容：

- 将通用规则保留在 `SANITIZE_PATTERNS`，仅用于日志、结果和错误输出脱敏；保留 `SECRET_PATTERNS` 兼容别名。
- 新增 `BLOCKING_SECRET_PATTERNS`，blocking scanner 只检查明确 provider 前缀的 Anthropic/OpenAI key、GitHub PAT、JWT 和长 Bearer token；不再因 `api_key`、`token`、`secret`、`password` 或 `x-api-key` 字段名及其普通值阻断。
- `scan_for_secret_material()` 跳过 `.pyc` 和 `.pyo`，避免编译产物误报。
- 主 P8、stress、stability 三个 harness 都输出 `scannerMode: "high-confidence-only"`；真实高置信格式仍会让 `secretScan` 阻断通过。

TDD 验证：

```text
RED:
  安全 fixture 和 .pyc/.pyo 误报；高置信 provider/JWT/Bearer 规则不完整；三个 summary 缺少 scannerMode。

GREEN:
  python -m unittest tests.test_p8_real_api_harness tests.test_p8_real_api_e2e_diagnostic tests.test_p8_real_api_stress_e2e tests.test_p8_real_api_stability_e2e
  Ran 36 tests
  OK
```

确定性全量回归：

```text
python -m unittest discover -s tests
Ran 898 tests in 162.457s
OK (skipped=3)
```

真实 LLM P8 验证：

```text
GA_RUN_REAL_API_E2E=1
GA_WORKFLOW_LLM_PROFILE=luna
GA_REAL_API_CONFIG=luna
GA_REAL_API_EXPECTED_NAME=luna
GA_REAL_API_EXPECTED_MODEL=gpt-5.6-luna
python tests/p8_real_api_e2e.py

exit=0
passed=true
profile=luna / gpt-5.6-luna
required cases=all passed
scannerMode=high-confidence-only
secretScan=[]
```

结论：P1-4 已闭环。普通 prompt、transcript、tool schema 和安全 fixture 不再掩盖 workflow 能力结果；明确的高置信 secret 仍保留 blocking 保护。

### P1-5：workflow args 的 workspace 未自动传给 child agent（2026-08-06）

状态：**已修复，已完成 TDD、确定性全量回归和真实 `gpt-5.6-luna` workspace 验证。**

根因确认：`WorkflowRuntime.run(..., args=...)` 和 JS worker 已经能够接收 workflow args，但 `AgentScheduler` 原先只将 args 用于 `argsHash`，没有把 workspace 写入 run/job metadata；`NativeGPTChildAgentRunner._child_cwd()` 因而始终回退到 `temp/workflow_child_agents/<job_id>`。child agent 使用 `file_read`、`file_write` 或 `code_run` 的相对路径时，实际工作目录可能与 workflow workspace 不一致。

修复内容：

- 新增统一 workspace 规范化：优先使用 `args.workspacePath`，兼容 `args.workspace`；使用 `expanduser().resolve()` 得到绝对路径。
- workspace 参数存在时要求其为已存在目录；同时提供两个字段但解析到不同目录时拒绝歧义输入。
- 将规范化路径写入 `run.metadata["workspacePath"]`、普通 job metadata 和 cached job metadata。
- `NativeGPTChildAgentRunner._child_cwd(job)` 使用 job metadata 中的 workspace；未提供 workspace args 时保留旧的临时目录 fallback。
- 保留 P0 test gate 的独立 workspace 校验和路径边界逻辑；本修复不把 workspace 限制到仓库目录，系统临时目录仍可作为合法 workspace。

TDD 验证：

```text
RED:
  4 个 P1-5 回归测试按预期失败（2 个 KeyError、1 个固定临时 cwd 断言失败、1 个缺失目录未被拒绝）。

GREEN:
  python -m unittest tests.test_workflow_scheduler.WorkflowSchedulerTest.test_register_agent_records_canonical_workspace_from_runtime_args tests.test_workflow_child_agent.NativeGPTChildAgentRunnerTest.test_child_cwd_uses_workspace_path_from_job_metadata tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_passes_workspace_args_to_child_job_metadata tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_rejects_missing_workspace_directory_before_child_start
  Ran 4 tests
  OK

  python -m unittest tests.test_workflow_scheduler tests.test_workflow_child_agent tests.test_workflow_runtime
  Ran 99 tests
  OK

  边界回归：cached job workspace metadata、workspacePath/workspace 冲突拒绝
  Ran 2 tests
  OK
```

确定性全量回归：

```text
python -m unittest discover -s tests
Ran 904 tests in 180.732s
OK (skipped=3)
```

真实 LLM 验证：

```text
GA_RUN_REAL_P0_WORKFLOW_E2E=1
GA_WORKFLOW_LLM_PROFILE=luna
python tests/real_workflow_p0_e2e.py

passed=true
profile=luna / gpt-5.6-luna
runStatus=succeeded
repair child status=succeeded
gate-1: passed=false, gatePassed=false
gate-2: passed=true, gatePassed=true
```

在同一 `luna / gpt-5.6-luna` 配置下另执行了一次临时 P1-5 workspace probe：workspace 为空，child 只被要求用相对路径写入 `p1_5_cwd_probe.txt`。结果为 `runStatus=succeeded`、`jobStatus=succeeded`，`runWorkspacePath` 与 `jobWorkspacePath` 完全相同，探针在目标 workspace 中存在且内容匹配，child response 非空。

结论：P1-5 的已复现缺陷在 runtime contract 层已闭环。确定性测试确认路径规范化、metadata 传播、cached job 传播和非法 workspace 拒绝；真实 API 测试确认 child 的相对路径确实落在 args 指定 workspace。未提供 workspace args 的旧调用仍使用原有 fallback，因此该修复没有改变旧调用的默认目录语义。

### P2-1：process subagent 与 workflow child agent 控制面不统一（2026-08-07）

状态：**已修复并完成确定性回归和真实 `gpt-5.6-luna` 跨引擎 E2E。** 本轮没有合并两个执行器：process subagent 仍由 `SubagentManager`/`agentmain.py` 执行，workflow child 仍由 `WorkflowRuntime`/`AgentScheduler` 执行；统一的是只读控制面、状态/事件投影、结果引用和能力边界。

实施结果：

- 公共 runtime model：以 engine-scoped opaque `execution_id` 区分 `process_agent`、`workflow_run` 和 `workflow_child`；`AgentRecord`、`AgentResultRecord`、`AgentCapabilities`、`AgentEventBatch` 保留 `source_status`、`cached`、`partial` 和 engine-specific metadata。
- process read seam：`c60b842` 新增 `list_agent_snapshots()`，adapter 使用 `probe_agent()` 枚举，不写 `state.json` 或 registry；process spawn、wait、mailbox、resume、interrupt/close 和旧 `list_agents()` 行为未改。`5f1a687` 投影 process record、事件和 artifact/transcript 引用。
- workflow source contract：`a4c8000`/`8dfd7e3` 增加 bounded `childSummary`/`executionOutcome`、handled child failure 的 `partial` 投影、bounded transcript reader 和 per-run `.journal.lock` sequence 分配；并发 writer 通过进程内 path lock 加 OS lock，stable event id 不再依赖调用方预分配 sequence。
- workflow identity：当前 GA 继续使用 `run_id + job_id`，不伪造 Claude 的 `logical_key`、`attempt_id` 或 attempt counter；resume/cache 来源通过 `cachedFromRunId`/`cachedFromJobId` 保留。
- read adapters：`ProcessSubagentAdapter` 与 `WorkflowChildAdapter` 分别投影原始执行器；`75a3cfb` 的 `UnifiedAgentControl` 合并 records/events/results，使用 `process` 和 `workflow:<run_id>` source cursor，并按 opaque execution ID 路由。
- Ink projection：`53fb7ba` 保留 legacy `workflow_*` 事件，additive 增加 `agent_snapshot`/`agent_event`；reducer 对 snapshot/event 去重，status bar 能显示 common `partial` 而不改 raw workflow 状态。
- lifecycle boundary：`ff3d2eb` 只在 capability 已声明时路由动作。process 保留真实 interrupt/close/message/followup/resume/attach/detach；workflow run 的 stop/cancel 保持 run scope；workflow child 的 cancel/close/message/resume 不伪装为 run 操作，返回结构化 `unsupported_capability` 且无副作用。

真实 API E2E：

```text
python -m unittest tests.real_p2_1_agent_control_e2e -v
1 test, skipped=1, no network request (opt-in absent)

python tests/real_p2_1_agent_control_e2e.py
skipped=true, no runner created

GA_RUN_REAL_P2_1_E2E=1 python tests/real_p2_1_agent_control_e2e.py
exit=0
passed=true
profile=luna
model=gpt-5.6-luna
provider=gpt-super-responses
recordKinds=process_agent, workflow_child, workflow_run
projectedStatuses=process:succeeded, workflow_child:succeeded, workflow_run:succeeded
eventCount=8
cursorKeys=process, workflow:wf_p2_1_luna
artifactCounts=process:1, workflowChild:1, workflowRun:1
workflowChildCapabilities=artifacts, events, read, result
```

该 harness 串行启动一个真实 process child 和一个真实 workflow child，均要求 bounded marker 输出；marker、prompt、transcript、完整 workspace 路径和 provider secret 不进入摘要。facade 查询确认三条 execution ID 不冲突，三类 record 均保留 workspace/permission metadata，process 与 workflow child 均提供 artifact/transcript/result 引用；process capability 保留 11 个既有动作，workflow child 未声明 process-only lifecycle 或 mailbox/resume。

回归验证：

```text
python -m unittest discover -s tests
Ran 942 tests in 159.726s
OK (skipped=3)

python -m unittest tests.test_agent_runtime_models tests.test_subagent_manager tests.test_agent_control_process tests.test_workflow_models tests.test_workflow_store tests.test_workflow_scheduler tests.test_workflow_controller tests.test_workflow_runtime tests.test_agent_control_workflow tests.test_agent_control -v
Ran 227 tests in 34.549s
OK

cd frontends/ink-ui
npm test
tests=367, pass=367, fail=0, skipped=0
npm run typecheck
exit=0
```

结论：P2-1 的统一 read/control contract 已闭环。公共 identity、status、event cursor、result/artifact 引用、workspace/permission metadata 和 capability-aware lifecycle 均有确定性覆盖，并由真实 `gpt-5.6-luna` process/workflow E2E 验证；不再把“两个执行器相同”误认为“控制面事实可统一”。

本轮明确保留的非目标：workflow child mailbox、独立 child-level cancel、独立 child resume、物理 artifact 目录迁移、跨引擎第二套 durable unified journal，以及把 Claude 的 logical key/attempt 模型伪造到当前 GA。后续若实现 workflow-specific `skip`/`retry`，必须单独定义 capability、scope、状态迁移和审计事件。

#### P2-1 补充：GA UI workflow 最终结果未形成真实 E2E 闭环（2026-08-07）

状态：**已修复，并完成 TDD、确定性全量回归、真实 `gpt-5.6-luna` 跨引擎复测和真实 Ink bridge/reducer E2E。**

对既有结论的修正：`0411451` 的真实 E2E harness 能证明 process/workflow 公共控制面的 record、event cursor、capability 与 artifact/result reader 可用；`437e360` 对这些事实的记录仍然成立。但该 harness 没有经过 `GenericAgentBridge._workflow_final_payload()` 和 Ink `applyBridgeEvent()`，因此不能证明 GA UI 已收到 workflow 最终结果。此前“P2-1 已完整闭环”的表述范围过宽，应收窄为“统一 read/control contract 已闭环”；UI 最终交付需由本补充验证单独证明。

根因确认：`WorkflowRuntime` 的 `succeeded`、`failed`、`killed` 三条终态路径都先 `save_run(run)`，再调用 `write_final_result(run, payload)`。后者写入 `final-result.json` 并只在当前内存对象上设置 `run.result_ref`，没有再次持久化 `run.json`/`state.json`。bridge 在 runtime 返回后重新 `load_run()`，因此获得 `result_ref=None`，并把 `workflow_final` 降级为：

```json
{"artifactError":"missing_ref"}
```

此时 child transcript 与 `final-result.json` 实际都已包含结果，但 Ink reducer 的 `workflowResults[runId]` 只能保存 fallback payload，无法交付真实结果。

修复内容：

- 保留每条终态路径原有的第一次 `save_run()`，不改变 external-kill guard 和终态竞争语义。
- 在三条路径的 `write_final_result()` 后各增加一次 `save_run()`，持久化由 artifact writer 设置的 `result_ref`。
- 不修改 `WorkflowStore.write_final_result()` 的低层副作用，不让 bridge 猜测默认 artifact 路径，也不改变 final payload schema。
- 新增真实 Node worker + `FakeChildAgentRunner` + `GenericAgentBridge` seam 回归。
- 新增 opt-in `tests/real_ink_workflow_final_delivery_e2e.ts`：使用生产 `startBridge()` 走 UI `model_switch → workflow_draft → workflow_approve → workflow_final` 协议，并将所有事件送入生产 Ink reducer。

TDD 验证：

```text
RED:
  succeeded / failed / external-killed 三类 run 重载后的 result_ref 均为 None；
  真实 runtime-to-bridge seam 同样在 durable result_ref 断言处失败。
  Ran 4 tests in 0.818s
  FAILED (failures=4)

GREEN:
  python -m unittest \
    tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_executes_phase_log_and_agent_script_with_fake_runner \
    tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_marks_run_failed_when_worker_errors \
    tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_observes_external_kill_state \
    tests.test_ink_bridge.InkBridgeTest.test_workflow_final_reads_persisted_artifact_from_real_runtime -v
  Ran 4 tests in 0.860s
  OK
```

确定性回归：

```text
workflow runtime/store/controller/bridge:
  Ran 183 tests in 42.336s
  OK

P2-1 common control plane:
  Ran 227 tests in 39.297s
  OK

python -m unittest discover -s tests
  Ran 943 tests in 158.299s
  OK (skipped=3)

cd frontends/ink-ui
npm test
  tests=367, pass=367, fail=0, skipped=0
npm run typecheck
  exit=0
```

真实 `gpt-5.6-luna` Ink UI E2E：

```text
GA_RUN_REAL_INK_WORKFLOW_FINAL_E2E=1
GA_WORKFLOW_LLM_PROFILE=luna
.\frontends\ink-ui\node_modules\.bin\tsx.cmd tests\real_ink_workflow_final_delivery_e2e.ts

exit=0
passed=true
model=luna/gpt-5.6-luna
provider=gpt-super-responses
persistedResultRef=final-result.json
eventCount=27
modelSelected=true
terminalSucceeded=true
finalContainsMarker=true
finalHasNoArtifactError=true
reducerContainsMarker=true
transcriptContainsMarker=true
commonSnapshotSeen=true
commonEventSeen=true
```

该 E2E 使用真实 UI bridge client 切换模型，真实 workflow child 只输出 bounded marker；随后同时检查 child transcript artifact、bridge `workflow_final`、Ink reducer `workflowResults[runId]`、terminal `workflow_run.resultRef` 和 common agent snapshot/event。默认未设置 opt-in 时，脚本在调用 `startBridge()` 前退出，确认不会误发网络请求。

真实跨引擎 harness 复测：

```text
GA_RUN_REAL_P2_1_E2E=1 python tests/real_p2_1_agent_control_e2e.py
exit=0
passed=true
profile=luna
model=gpt-5.6-luna
provider=gpt-super-responses
issues=[]
recordKinds=process_agent, workflow_child, workflow_run
eventCount=8
artifactCounts=process:1, workflowRun:1, workflowChild:1
```

结论：`0411451` 所覆盖的统一控制面在修复后仍通过，但它不足以单独证明 GA UI 结果交付。现在 runtime durable state、bridge final payload、真实 child transcript 和 Ink reducer 已由同一条真实 E2E 链路贯通，`artifactError=missing_ref` 的已复现缺陷已闭环。对应提交：`1a91d41`、`ff7d047`。

#### P2-1 当前 main 复测（2026-08-07）

在当前 `main`（包含 `1a91d41`、`ff7d047` 和 `c6ca8f3`）上重新执行了完整验证：

```text
P2-1 focused Python:
  python -m unittest tests.test_agent_runtime_models tests.test_subagent_manager tests.test_agent_control_process tests.test_workflow_models tests.test_workflow_store tests.test_workflow_scheduler tests.test_workflow_controller tests.test_workflow_runtime tests.test_agent_control_workflow tests.test_agent_control -v
  Ran 227 tests in 36.367s
  OK

Python full regression (second complete run):
  python -m unittest discover -s tests
  Ran 946 tests in 164.862s
  OK (skipped=3)

Ink UI:
  npm test -> 367 tests, pass=367, fail=0, skipped=0
  npm run typecheck -> exit=0
```

第一次 Python 全量运行中，非 P2-1 的 `ChannelSlowSubscriberTest.test_a_stalled_subscriber_does_not_delay_delivery_to_a_healthy_one` 因时序只收到 28 条事件而失败；该用例随后独立连续 5 次通过，第二次完整全量运行也通过。该现象作为 realtime IPC 负载抖动保留，不改变 P2-1 验证结论，也未修改其代码。

真实 `gpt-5.6-luna` 跨引擎 E2E：

```text
GA_RUN_REAL_P2_1_E2E=1 python tests/real_p2_1_agent_control_e2e.py
passed=true
profile=luna
model=gpt-5.6-luna
provider=gpt-super-responses
recordKinds=process_agent, workflow_child, workflow_run
eventCount=9
cursorKeys=process, workflow:wf_p2_1_luna
artifactCounts=process:1, workflowRun:1, workflowChild:1
issues=[]
```

真实 Ink workflow final delivery E2E：

```text
GA_RUN_REAL_INK_WORKFLOW_FINAL_E2E=1
GA_WORKFLOW_LLM_PROFILE=luna
tsx tests/real_ink_workflow_final_delivery_e2e.ts
passed=true
model=luna/gpt-5.6-luna
provider=gpt-super-responses
persistedResultRef=final-result.json
eventCount=1153
modelSelected=true
terminalSucceeded=true
finalContainsMarker=true
finalHasNoArtifactError=true
reducerContainsMarker=true
transcriptContainsMarker=true
commonSnapshotSeen=true
commonEventSeen=true
```

本次复测确认：统一控制面在最新 workflow result 持久化修复后仍然通过；真实 UI bridge/reducer 链路能够收到最终结果，且没有回退为 `artifactError=missing_ref`。P2-1 的实现、验证和文档状态同步已完成。
