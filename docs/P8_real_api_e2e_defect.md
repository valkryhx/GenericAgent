# P8 真实 API 多 Agent Workflow 自测缺陷记录

> 日期：2026-06-11  
> 范围：P8/P5 Dynamic Workflow 真实 `gpt-5.5` 多 agent sandbox code E2E  
> 配置：`native_oai_config / gpt-native / gpt-5.5`  
> 临时脚本：`temp/run_p5_multi_agent_code_e2e.py`  
> 安全约束：未读取、未打印、未提交 `mykey.py` / `mykey.json` / `mcp.json`；真实 API artifacts 不提交。

---

## 1. 背景

在单 agent + 真实 MCP 搜集 + 中文报告 E2E 通过后，按 `docs/P8-e2e-next-steps.md` 中 Level 1 建议，执行更复杂的真实多 agent sandbox 代码开发 E2E。

目标任务是在临时 sandbox 中实现一个 Python URL / Markdown 链接处理 mini package：

- `url_utils.py`
- `test_url_utils.py`
- `README.md`
- `REVIEW.md`

最初 workflow 形态为：

```text
design
→ parallel(implementation, tests)
→ review
→ final
```

验收目标包括：

- 真实 `gpt-5.5` child agent 启动；
- 4-5 个 agent 完成；
- `phase()` / `log()` / `agent()` / `parallel()` 编排成功；
- `file_read` / `file_write` 工具调用成功；
- 所有 agent result/transcript artifact 存在；
- result JSON 不内联 `transcriptEvents`；
- sandbox 代码与测试生成；
- `python -m unittest discover -s <workspace>` 通过；
- `secretScan=[]`；
- final 包含 `GA_P5_MULTI_AGENT_CODE_E2E_DONE`。

---

## 2. 三轮真实自测结果

### 2.1 第一轮：workflow 安全预检拦截

结果摘要：

```text
passed=false
profileOk=true
name=gpt-native
model=gpt-5.5
secretScan=[]
error=ValueError: workflow script uses forbidden token
```

原因：workflow script prompt 文本中出现 `WorkflowRuntime.FORBIDDEN_SCRIPT_TOKENS` 中的保留词，触发 runtime 安全预检。

结论：这不是 provider/API 失败，也不是多 agent runtime 失败；暴露的是 workflow prompt/script 文本需要避开 runtime 安全扫描保留词。

### 2.2 第二轮：workflow 编排成功，但产物质量门禁失败

结果摘要：

```text
status=succeeded
finalStatus=succeeded
startedJobIds=[agent_1, agent_2, agent_3, agent_4, agent_5]
agent_completed=5
agent_registered=5
agent_started=5
workflow_phase=4
workflow_log=4
tool_allowed=22
deniedTools=[]
markerFound=true
unitTest.returncode=1
secretScan 有命中
```

已验证成功的链路：

- 真实 `gpt-5.5` provider 可用；
- 5 个 child agent 全部启动并完成；
- `parallel(implementation, tests)` 实际并发执行；
- `file_write` / `file_read` 工具调用成功；
- 生成 `url_utils.py`、`test_url_utils.py`、`README.md`、`REVIEW.md`；
- 所有 job artifact/transcript 存在；
- result JSON 不内联 `transcriptEvents`；
- final result 包含 `GA_P5_MULTI_AGENT_CODE_E2E_DONE`。

失败原因：implementation agent 与 tests agent 并行工作，契约未锁死，导致 API 细节不一致：

- implementation 返回三字段 `MarkdownLink` namedtuple，tests 期望二元 tuple；
- implementation 参数名为 `params_to_redact`，tests 调用 `sensitive_keys`；
- URL normalization 的 query 排序、fragment、trailing slash 行为与 tests 预期不一致。

review agent 实际在 `REVIEW.md` 中发现并指出测试与实现不一致，但 workflow 没有 `review -> repair -> retest` 闭环，final agent 只是汇总。

另有 `secretScan` 命中 URL 脱敏测试 fixture 中的 `api_key` / `token` 示例。该现象更像 fixture 误报，不是实际密钥泄漏，但在严格验收下仍导致失败。

### 2.3 第三轮：统一 API contract 后仍未通过产物门禁

修正：prompt 中加入更明确的 API contract，要求 implementation/tests 依据同一契约生成。

结果摘要：

```text
status=succeeded
finalStatus=succeeded
startedJobIds=[agent_1, agent_2, agent_3, agent_4, agent_5]
agent_completed=5
agent_registered=5
agent_started=5
workflow_phase=4
workflow_log=4
tool_allowed=17
deniedTools=[]
markerFound=true
unitTest.returncode=1
secretScan 命中 workspace/test_url_utils.py 与 workspace/url_utils.py
```

剩余 unittest 失败：

```text
FAIL: test_extracts_domain_when_scheme_is_missing
extract_domain("Example.COM:8080/path") 返回 ""，期望 "example.com"

FAIL: test_removes_only_one_trailing_slash_except_root
normalize_url("https://example.com/path//") 返回 "https://example.com/path"，期望 "https://example.com/path/"
```

review agent 再次有效识别 blocking correctness bug：

```text
schemeless host:port inputs are parsed as schemes
```

结论：统一 contract 后问题减少，但 workflow 仍没有 repair/retest 闭环；review 的发现无法驱动后续修复。

---

## 3. 核心缺陷判断

### 3.1 这不是 P8 runtime 底座失败

本次真实自测对 P8 dynamic workflow 底座是正向验证：

- 真实 `gpt-5.5` child agent 可用；
- 多 agent 编排可运行；
- `parallel()` 可运行；
- 工具调用可运行；
- 权限事件可记录；
- artifact/transcript 隔离可工作；
- final-result 可落盘；
- marker 可汇总。

失败点主要在 workflow 产品层和工程方法论层，而不是底层 runtime。

### 3.2 当前 workflow 不符合 TDD

当前 Level 1 workflow 将 `implementation` 与 `tests` 并行：

```text
design
→ parallel(implementation, tests)
→ review
→ final
```

这不符合 TDD。按 TDD，正确顺序应为：

```text
需求 / contract
→ 写测试
→ 运行测试确认红灯
→ 写实现
→ 运行测试确认绿灯
→ refactor / review
→ 再测试
→ final
```

并行写最终 tests 和 implementation 会导致双方根据高层 prompt 分别脑补 API 细节，从而产生契约漂移。

### 3.3 Review 有效，但缺少 repair/retest 闭环

本次 review agent 两次识别真实问题：

- 第二轮识别实现与测试 API contract 不一致；
- 第三轮识别 `host:port` schemeless 解析 bug。

但 workflow 结构是：

```text
review → final
```

缺少：

```text
review → repair → retest
```

因此 review 只能诊断，不能改变最终验收结果。

### 3.4 外部高质量 skills 没有真正参与

GA 当前能发现大量 Claude/Codex skills，包括：

- `using-superpowers`
- `tdd`
- `test-driven-development`
- `systematic-debugging`
- `requesting-code-review`
- `verification-before-completion`
- `subagent-driven-development`

但本次真实 E2E 基本没有使用这些 skills。

原因分三层：

1. **临时 harness 出于安全考虑裁剪了 tools schema。**  
   本次 `tools_schema_factory=selected_file_tools` 只开放 `file_read` / `file_write`，没有开放 `load_skill`，因此 child agent 无法实际加载 skill。

2. **workflow script 没有声明 agent 必须使用哪些 skills。**  
   即便 `workflow_child_agent.py` 会在 system prompt 拼接 `build_skill_prompt()` 的 skill index，这也只是软提示。

3. **GA workflow DSL 没有一等 skill policy。**  
   当前 `agent(prompt, options)` 没有类似 `skills: [...]`、`requireSkills: true` 的机制；runtime 也不会验证 child transcript 是否出现 `load_skill`。

结论：GA 不是没有 skills 能力，而是 skills 与 workflow 编排没有深度融合。

---

## 4. 具体暴露问题列表

### DEFECT-01：开发型 workflow 允许错误并行结构

**现象：** tests 与 implementation 并行执行，导致 API contract 漂移。

**影响：** 真实开发任务容易生成互相不匹配的实现和测试。

**建议：** 对 TDD/代码开发模板禁止最终 tests 与 implementation 并行。可并行的是 test plan、edge-case brainstorm、security review 等不直接落最终实现/测试的阶段。

### DEFECT-02：缺少 TDD red/green/refactor gate

**现象：** tests 生成后没有先运行并确认红灯；implementation 后也没有 workflow 内部 green gate。

**影响：** workflow 无法证明测试有效，也无法把失败结果反馈给 agent。

**建议：** 引入 host-side test gate，例如：

```text
run_python_unittest(workspace)
```

或 workflow DSL 内置受控测试原语。

### DEFECT-03：review 不能驱动 repair

**现象：** review agent 能发现 bug，但 workflow 没有 repair agent 和 retest 循环。

**影响：** review 成为报告型阶段，而不是质量闭环。

**建议：** 默认开发 workflow 加入：

```text
review -> repair -> retest -> final
```

并允许 1-3 轮 bounded repair loop。

### DEFECT-04：测试结果没有进入 workflow 上下文

**现象：** harness 在 workflow 结束后执行 unittest，child agents 看不到失败日志。

**影响：** repair 无法基于真实测试输出进行。

**建议：** 将测试运行作为 workflow 内部一等步骤；或者由 harness 把 `TEST_FAILURES.txt` 写入 sandbox，并启动 repair agent 读取修复。

### DEFECT-05：skills 仅软提示，不是 workflow 约束

**现象：** child system prompt 有 Available Skills listing，但没有强制加载。

**影响：** 高质量 skills 不能稳定参与工程 workflow。

**建议：** 增加 skill-aware agent options：

```js
agent(prompt, {
  label: 'tests',
  skills: ['using-superpowers', 'test-driven-development'],
  requireSkills: true
})
```

runtime 应确保 `load_skill` 可用，并记录/验证 skill loading。

### DEFECT-06：tools_schema 裁剪会静默禁用 skills

**现象：** 本次 harness 只开放 `file_read` / `file_write`，导致 `load_skill` 不可用。

**影响：** 即使 skill index 出现在 prompt 中，child agent 也不能加载完整 SKILL.md。

**建议：** 若 agent options 声明 `skills` 或 `requireSkills`，runtime/runner 必须自动包含 `load_skill`，或在启动前失败并报告配置错误。

### DEFECT-07：skill 使用没有进入验收指标

**现象：** summary 只统计 `toolCalls`、`deniedTools`、job 状态等，没有统计 `loadedSkills`。

**影响：** 无法判断 superpowers / TDD / verification skills 是否实际参与。

**建议：** 在 child transcript/result metadata 中记录：

```text
loadedSkills
requiredSkills
missingRequiredSkills
skillLoadRequired
```

并将其纳入 E2E 验收。

### DEFECT-08：secret scan 对 URL 脱敏 fixture 误报

**现象：** URL 脱敏代码/测试中出现 `api_key`、`token` 等参数名，触发 `api_key_field` 模式。

**影响：** 真实密钥未泄漏，但严格 `secretScan=[]` 门禁失败。

**建议：** 保持真实 key 扫描严格，同时让 fixture 使用安全占位值，或让扫描器识别 placeholder/demo 值并跳过 `.pyc/.pyo`。

### DEFECT-09：缺少工程 workflow 模板层

**现象：** GA 暴露的是 `phase/log/agent/parallel/pipeline` 低层 DSL，用户或 workflow 作者容易写出方法论错误的 workflow。

**影响：** runtime 可执行不代表 workflow 符合真实开发流程。

**建议：** 在 DSL 上方提供内置模板：

- `tdd-python-package`
- `bugfix-with-regression-test`
- `review-repair-retest`
- `research-then-implement`
- `multi-agent-code-review`

---

## 5. 建议的正确 TDD workflow 模板

建议下一版 Level 1.1 真实自测使用以下结构：

```text
Contract
→ Tests
→ Red
→ Implementation
→ Green
→ Review
→ Repair Loop（可选，最多 1-3 轮）
→ Final Verification
→ Final
```

伪代码：

```js
phase('Contract');
await agent('写 CONTRACT.md，明确 API contract', {
  label: 'contract',
  skills: ['using-superpowers', 'brainstorming']
});

phase('Tests');
await agent('加载 TDD skill，读取 CONTRACT.md，只写 test_url_utils.py，不写实现', {
  label: 'tests',
  skills: ['using-superpowers', 'test-driven-development'],
  requireSkills: true
});

phase('Red');
const red = await run_python_unittest(args.workspace);
if (red.passed) throw new Error('TDD red phase failed: tests unexpectedly pass');

phase('Implementation');
await agent('读取 CONTRACT.md 和 failing tests，写最小实现 url_utils.py', {
  label: 'implementation',
  skills: ['using-superpowers', 'test-driven-development'],
  requireSkills: true
});

phase('Green');
let green = await run_python_unittest(args.workspace);

phase('Repair');
let repairRounds = 0;
while (!green.passed && repairRounds < 2) {
  await write_file(args.workspace + '/TEST_FAILURES.txt', green.stderr);
  await agent('读取失败日志、源码、测试，修复到测试通过', {
    label: 'repair-' + repairRounds,
    skills: ['systematic-debugging'],
    requireSkills: true
  });
  green = await run_python_unittest(args.workspace);
  repairRounds++;
}
if (!green.passed) throw new Error('tests still failing after repair');

phase('Review');
await agent('加载 code review / verification skills，审查代码与测试', {
  label: 'review',
  skills: ['requesting-code-review', 'verification-before-completion'],
  requireSkills: true
});

phase('Final');
return {
  marker: 'GA_TDD_WORKFLOW_DONE',
  testsPassed: green.passed,
  repairRounds
};
```

---

## 6. 推荐修复路线

### P1：实现 skill-aware agent options

支持：

```js
agent(prompt, {
  label: 'tests',
  skills: ['using-superpowers', 'test-driven-development'],
  requireSkills: true
})
```

验收：

- required skills 被加载或预注入；
- transcript/result 记录 skill 使用；
- 缺失 required skill 时 job failed 或 workflow preflight failed；
- 若使用 `tools_schema_factory`，必须自动包含 `load_skill` 或明确报错。

### P1：实现 workflow 内部 test gate

先支持 Python unittest：

```text
run_python_unittest(workspace)
```

要求：

- 受控 cwd；
- timeout；
- stdout/stderr 截断；
- 结构化 result；
- 不允许任意 shell 命令；
- 可写入 `TEST_FAILURES.txt` 供 repair agent 读取。

### P1：新增 TDD sandbox 真实 E2E diagnostic

新的真实 E2E 应验证：

- tests agent 先写测试；
- Red phase 确认失败；
- implementation 后 Green；
- repair loop 至少可处理一次失败；
- required skills 被加载；
- final `unittest passed`；
- `secretScan=[]`；
- artifact/transcript 隔离保持。

### P2：fixture-aware secret scan

优化 opt-in E2E 的 secret scan：

- 跳过 `.pyc/.pyo`；
- 对 placeholder/demo 值降低误报；
- 对真实 Bearer、`sk-*`、`sk-ant-*`、JWT、长随机 token 继续严格失败；
- 将 fixture 命中与疑似真实 secret 命中分级输出。

### P2：内置工程 workflow templates

提供高层模板而不是只给裸 JS DSL：

- `tdd-python-package`
- `bugfix-with-regression-test`
- `review-repair-retest`
- `research-then-implement`
- `multi-agent-code-review`

---

## 7. 总体结论

本次真实 E2E 的价值在于证明：

```text
P8 dynamic workflow 底层 orchestration 可用，但 GA 还缺真正工程工作流语义。
```

要把 GA workflow 做成真正可用的开发系统，下一阶段重点应从“能调度多个 agent”升级为：

```text
skill-aware
TDD-aware
quality-gated
repair-loop-enabled
artifact-contract-driven
```

只有这样，GA workflow 才能从可运行的多 agent DSL 变成可靠的工程自动化工作流。
