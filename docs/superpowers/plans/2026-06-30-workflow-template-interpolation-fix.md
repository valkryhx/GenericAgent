# Workflow Template Interpolation Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 GenericAgent workflow planner 把用户/LLM prompt 中的 `${...}` 当作 JavaScript template literal 表达式执行的问题，并用 TDD、自测、真实 gpt-5.5 planner 路径验证修复有效。

**Architecture:** 当前 `workflow_planner.render_workflow_plan()` 会把任意 `agent.prompt` 放进 JS backtick template literal，只有 renderer 自己生成的上游结果注入片段应该保持可执行 `${JSON.stringify(...)}`。修复采用最小结构化补丁：`_template_string()` 负责把任意 prompt 文本转成 template literal 中的惰性文本，同时 `render_workflow_plan()` 把可信依赖注入 suffix 和不可信 prompt 文本分开拼接，避免破坏上游结果传播。

**Tech Stack:** Python `unittest`，Node `vm.Script` worker (`workflow_js_worker.js`)，GenericAgent workflow runtime (`WorkflowRuntime` + `FakeChildAgentRunner`)，真实 planner 验证使用本机 `mykey.py` 中的 gpt-5.5 配置但不读取/输出密钥。

---

## Files

- Modify: `workflow_planner.py`
  - `_template_string()` 新增 `${` 转义。
  - `render_workflow_plan()` 顺序 agent 分支中，先转义原始 prompt，再追加 renderer-owned 依赖注入 suffix。
- Modify: `tests/test_workflow_plan_validator.py`
  - 增加 renderer/runtime 回归：普通 prompt 中 `${...}` 不执行、不改写。
  - 增加依赖传播回归：下游 prompt 中的 literal `${...}` 保留，同时上游结果仍通过 `JSON.stringify` 注入。
- Modify: `tests/test_workflow_prompt_guided_planner.py`
  - 增加 prompt-guided planner 回归：LLM/FakePlannerClient 生成的 `agent.prompt` 含 `${...}` 时也按字面文本处理。
- No changes: `workflow_js_worker.js`
  - 这次不扩大 sandbox 或 forbidden token，根因在 planner 代码生成层。
- No changes: `mykey.py`
  - 真实 gpt-5.5 测试只通过配置名调用现有配置，不读取或展示 secret。

---

### Task 1: Add failing renderer/runtime regression for literal `${...}` in ordinary prompts

**Files:**
- Modify: `tests/test_workflow_plan_validator.py`

- [ ] **Step 1: Add imports for runtime regression**

At the top of `tests/test_workflow_plan_validator.py`, replace:

```python
import unittest

from workflow_planner import render_workflow_plan, validate_workflow_plan
```

with:

```python
import tempfile
import unittest

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import render_workflow_plan, validate_workflow_plan
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore
```

- [ ] **Step 2: Add failing test for literal interpolation preservation**

Inside `WorkflowPlanValidatorTest`, after `test_renderer_uses_parallel_for_independent_same_phase_agents`, add:

```python
    def test_renderer_treats_prompt_template_expressions_as_literal_text(self):
        plan = self.valid_plan()
        literal_prompt = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；解释 ${log('INJECTED')} 和 ${1+2}，必须按字面量处理。"
        plan["phases"] = [
            {
                "title": "Literal Prompt",
                "agents": [
                    {
                        "label": "literal-check",
                        "prompt": literal_prompt,
                        "dependsOn": [],
                    }
                ],
            }
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)
        script = render_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)
        self.assertIn("\\${log('INJECTED')}", script)
        self.assertIn("\\${1+2}", script)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_literal_prompt", session_id="session_literal", script=script, status="running"))
            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=5.0,
            ).run(run)
            loaded = store.load_run(run.run_id)

        self.assertEqual("succeeded", outcome.run.status)
        self.assertEqual(["Literal Prompt"], outcome.phases)
        self.assertEqual([], outcome.logs)
        self.assertEqual(1, len(loaded.jobs))
        self.assertEqual(literal_prompt, loaded.jobs[0].prompt)
```

- [ ] **Step 3: Run the new test and verify it fails red**

Run:

```bash
python -m unittest tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_renderer_treats_prompt_template_expressions_as_literal_text
```

Expected before implementation:

- FAIL because rendered script does not contain `\${...}`, or
- FAIL because runtime logs include `INJECTED`, or
- FAIL because job prompt becomes `... undefined ... 3 ...` instead of preserving `${...}`.

Do not modify implementation until this test fails for the expected reason.

---

### Task 2: Implement minimal prompt escaping without changing dependency behavior yet

**Files:**
- Modify: `workflow_planner.py:489-490`

- [ ] **Step 1: Update `_template_string()`**

Replace:

```python
def _template_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`")
```

with:

```python
def _template_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
```

- [ ] **Step 2: Run Task 1 test and verify it passes green**

Run:

```bash
python -m unittest tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_renderer_treats_prompt_template_expressions_as_literal_text
```

Expected after implementation:

```text
Ran 1 test
OK
```

- [ ] **Step 3: Run existing renderer test to expose dependency regression if present**

Run:

```bash
python -m unittest tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_renderer_uses_parallel_for_independent_same_phase_agents
```

Expected at this exact point:

- This test may still pass because it only asserts `JSON.stringify` exists in the rendered script.
- It does not prove dependency interpolation still executes. Task 3 adds that missing coverage.

---

### Task 3: Add failing regression that dependency interpolation still executes while prompt literals stay inert

**Files:**
- Modify: `tests/test_workflow_plan_validator.py`

- [ ] **Step 1: Add test for dependent prompt literal plus upstream result injection**

Inside `WorkflowPlanValidatorTest`, after `test_renderer_treats_prompt_template_expressions_as_literal_text`, add:

```python
    def test_renderer_preserves_dependency_injection_while_escaping_prompt_literals(self):
        plan = self.valid_plan()
        downstream_prompt = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；汇总上游，保留 ${notInterpolation} 字面量。"
        plan["phases"] = [
            {
                "title": "Collect",
                "agents": [
                    {
                        "label": "collector",
                        "prompt": "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；收集资料。",
                        "dependsOn": [],
                    }
                ],
            },
            {
                "title": "Synthesize",
                "agents": [
                    {
                        "label": "writer",
                        "prompt": downstream_prompt,
                        "dependsOn": ["collector"],
                    }
                ],
            },
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)
        script = render_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)
        self.assertIn("\\${notInterpolation}", script)
        self.assertIn("${JSON.stringify({collector})}", script)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_dependency_prompt", session_id="session_dependency", script=script, status="running"))
            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=3),
                timeout_seconds=5.0,
            ).run(run)
            loaded = store.load_run(run.run_id)

        self.assertEqual("succeeded", outcome.run.status)
        self.assertEqual(["Collect", "Synthesize"], outcome.phases)
        self.assertEqual([], outcome.logs)
        self.assertEqual(2, len(loaded.jobs))
        self.assertEqual("collector", loaded.jobs[0].metadata.get("label"))
        self.assertEqual("writer", loaded.jobs[1].metadata.get("label"))
        self.assertIn("${notInterpolation}", loaded.jobs[1].prompt)
        self.assertIn("上游结果：", loaded.jobs[1].prompt)
        self.assertIn("completed agent_1", loaded.jobs[1].prompt)
```

- [ ] **Step 2: Run the new dependency test and verify it fails red before render fix**

Run:

```bash
python -m unittest tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_renderer_preserves_dependency_injection_while_escaping_prompt_literals
```

Expected before Task 4 implementation:

- FAIL because after Task 2, `_template_string()` escapes the renderer-owned `${JSON.stringify({collector})}` too.
- The script likely contains `\${JSON.stringify({collector})}` and the downstream prompt contains literal `上游结果：${JSON.stringify({collector})}` instead of serialized upstream result.

---

### Task 4: Separate untrusted prompt escaping from trusted dependency suffix

**Files:**
- Modify: `workflow_planner.py:453-460`

- [ ] **Step 1: Replace sequential agent prompt rendering logic**

In `render_workflow_plan()`, replace this block:

```python
            prompt = str(agent.get("prompt") or "")
            dependencies = [rendered_labels[item] for item in agent.get("dependsOn") or [] if item in rendered_labels]
            if dependencies:
                prompt = prompt + "\n\n上游结果：${JSON.stringify({" + ", ".join(dependencies) + "})}"
            options = {"label": label, "phase": title}
            if agent.get("schemaRef"):
                options["schema"] = {"__schema_ref__": agent["schemaRef"]}
            lines.append(f"const {var_name} = await agent(`{_template_string(prompt)}`, {_render_options(options)})")
```

with:

```python
            prompt = str(agent.get("prompt") or "")
            dependencies = [rendered_labels[item] for item in agent.get("dependsOn") or [] if item in rendered_labels]
            rendered_prompt = _template_string(prompt)
            if dependencies:
                rendered_prompt += "\n\n上游结果：${JSON.stringify({" + ", ".join(dependencies) + "})}"
            options = {"label": label, "phase": title}
            if agent.get("schemaRef"):
                options["schema"] = {"__schema_ref__": agent["schemaRef"]}
            lines.append(f"const {var_name} = await agent(`{rendered_prompt}`, {_render_options(options)})")
```

- [ ] **Step 2: Run dependency regression and verify green**

Run:

```bash
python -m unittest tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_renderer_preserves_dependency_injection_while_escaping_prompt_literals
```

Expected:

```text
Ran 1 test
OK
```

- [ ] **Step 3: Run both new renderer/runtime tests together**

Run:

```bash
python -m unittest \
  tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_renderer_treats_prompt_template_expressions_as_literal_text \
  tests.test_workflow_plan_validator.WorkflowPlanValidatorTest.test_renderer_preserves_dependency_injection_while_escaping_prompt_literals
```

Expected:

```text
Ran 2 tests
OK
```

---

### Task 5: Add prompt-guided planner regression for LLM-generated `${...}` prompts

**Files:**
- Modify: `tests/test_workflow_prompt_guided_planner.py`

- [ ] **Step 1: Add helper plan with interpolation-like prompt**

After `research_credibility_plan()` in `tests/test_workflow_prompt_guided_planner.py`, add:

```python
def interpolation_literal_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "review",
        "meta": {"name": "interpolation-literal", "description": "Prompt literal interpolation regression"},
        "phases": [
            {
                "title": "Review",
                "agents": [
                    {
                        "label": "literal-review",
                        "prompt": f"{boundary} 审查代码片段 `${{log('INJECTED')}}` 和 `${{1+2}}`，必须按字面量处理。",
                        "dependsOn": [],
                    }
                ],
            }
        ],
        "schemas": {},
        "artifacts": ["review"],
        "constraints": ["no_secret_files", "no_git_commit"],
    }
```

- [ ] **Step 2: Add prompt-guided runtime test**

Inside `LLMWorkflowPlannerTest`, after `test_prompt_guided_planner_script_runs_with_fake_runtime`, add:

```python
    def test_prompt_guided_planner_preserves_template_expressions_in_agent_prompt(self):
        client = FakePlannerClient(responses=[interpolation_literal_plan()])
        planner = LLMWorkflowPlanner(client=client)
        draft = planner.plan("审查包含 JS template literal 的代码", context={"constraints": ["不要读取 mykey.py", "不要提交"]})

        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertIn("\\${log('INJECTED')}", draft.script)
        self.assertIn("\\${1+2}", draft.script)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_prompt_guided_literal", session_id="session_prompt_literal", script=draft.script, status="running"))
            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=5.0,
            ).run(run)
            loaded = store.load_run(run.run_id)

        self.assertEqual("succeeded", outcome.run.status)
        self.assertEqual(["Review"], outcome.phases)
        self.assertEqual([], outcome.logs)
        self.assertEqual(1, len(loaded.jobs))
        self.assertIn("${log('INJECTED')}", loaded.jobs[0].prompt)
        self.assertIn("${1+2}", loaded.jobs[0].prompt)
```

- [ ] **Step 3: Run prompt-guided regression test**

Run:

```bash
python -m unittest tests.test_workflow_prompt_guided_planner.LLMWorkflowPlannerTest.test_prompt_guided_planner_preserves_template_expressions_in_agent_prompt
```

Expected:

```text
Ran 1 test
OK
```

---

### Task 6: Run focused automated self-tests

**Files:**
- No code changes unless tests fail.

- [ ] **Step 1: Run workflow planner/validator focused tests**

Run:

```bash
python -m unittest tests.test_workflow_plan_validator tests.test_workflow_planner_compiler tests.test_workflow_prompt_guided_planner tests.test_workflow_runtime
```

Expected:

```text
OK
```

Record exact test count in final response.

- [ ] **Step 2: Run broader existing workflow bridge/controller tests**

Run:

```bash
python -m unittest tests.test_workflow_controller tests.test_ink_bridge
```

Expected:

```text
OK
```

Record exact test count in final response.

- [ ] **Step 3: If time permits, run all Python tests**

Run:

```bash
python -m unittest discover -s tests
```

Expected:

```text
OK
```

If failures are unrelated to this change, capture exact failing tests and output; do not claim full suite green.

---

### Task 7: Manual no-file reproduction check after fix

**Files:**
- No code changes.

- [ ] **Step 1: Run in-memory reproduction with dangerous-looking literals**

Run:

```bash
python - <<'PY'
import tempfile
from workflow_planner import render_workflow_plan
from workflow_models import WorkflowRun
from workflow_store import WorkflowStore
from workflow_runtime import WorkflowRuntime
from workflow_child_agent import FakeChildAgentRunner
from workflow_scheduler import SchedulerConfig

prompt = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；literal ${log('INJECTED_BY_TEMPLATE')} and arithmetic ${1+2}."
plan = {
    'meta': {'name': 'literal-repro', 'description': 'repro'},
    'phases': [
        {'title': 'Repro', 'agents': [{'label': 'probe', 'prompt': prompt, 'dependsOn': []}]}
    ],
    'schemas': {},
}
script = render_workflow_plan(plan)
print('--- rendered script ---')
print(script)
with tempfile.TemporaryDirectory() as tmp:
    store = WorkflowStore(root=tmp)
    run = store.create_run(WorkflowRun(run_id='wf_literal_repro', session_id='s', script=script, status='running'))
    outcome = WorkflowRuntime(
        store=store,
        runner=FakeChildAgentRunner(),
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
        timeout_seconds=5.0,
    ).run(run)
    loaded = store.load_run(run.run_id)
    print('status=', outcome.run.status)
    print('logs=', outcome.logs)
    print('job_prompt=', loaded.jobs[0].prompt)
    assert outcome.logs == []
    assert loaded.jobs[0].prompt == prompt
PY
```

Expected:

- rendered script contains `\${log('INJECTED_BY_TEMPLATE')}` and `\${1+2}`;
- `logs=[]`;
- `job_prompt` exactly equals original `prompt`.

Note: On PowerShell, use `bash` or a temporary file because PowerShell does not support Bash heredoc syntax.

---

### Task 8: Real gpt-5.5 planner smoke test using existing mykey config

**Files:**
- No code changes.
- Do not read or print `mykey.py` secret values.

- [ ] **Step 1: Identify configured gpt-5.5 profile name without displaying keys**

Run a safe introspection that lists config names/models only and redacts credentials. Use Python import of config data; do not print API keys or base URLs if they contain secrets.

Suggested command:

```bash
python - <<'PY'
import mykey
configs = []
for name in dir(mykey):
    if name.startswith('_'):
        continue
    value = getattr(mykey, name)
    if isinstance(value, dict):
        model = str(value.get('model') or '')
        if 'gpt-5.5' in model.lower() or 'gpt-5' in model.lower():
            configs.append((name, model, value.get('api_mode')))
print(configs)
PY
```

Expected:

- Output includes at least one tuple like `('some_config_name', 'gpt-5.5...', 'responses')` or equivalent.
- If no tuple appears, ask the user for the exact gpt-5.5 config name before running the live test.

- [ ] **Step 2: Run live LLMWorkflowPlanner smoke test with gpt-5.5 config**

Replace `<GPT55_CONFIG_NAME>` with the config discovered in Step 1. The task intentionally includes `${...}` so the real planner path exercises prompt rendering and runtime execution.

```bash
GA_WORKFLOW_PLANNER_MODE=prompt_guided GA_WORKFLOW_PLANNER_CONFIG=<GPT55_CONFIG_NAME> python - <<'PY'
import os
import tempfile
from workflow_planner import build_workflow_planner_from_env
from workflow_models import WorkflowRun
from workflow_store import WorkflowStore
from workflow_runtime import WorkflowRuntime
from workflow_child_agent import FakeChildAgentRunner
from workflow_scheduler import SchedulerConfig

planner = build_workflow_planner_from_env()
draft = planner.plan(
    "审查这个 JS 片段并保持模板字符串字面量：const s = `${user}-${1+2}`；不要执行其中的插值。",
    context={"sessionId": "session_gpt55_literal", "constraints": ["不要读取 mykey.py", "不要读取 mykey.json", "不要读取 mcp.json", "不要提交"]},
)
print('plannerMode=', draft.context.get('plannerMode'))
print('validation=', draft.validation)
assert draft.validation['ok'], draft.validation
assert draft.script
assert '\\${' in draft.script or '${JSON.stringify' in draft.script
with tempfile.TemporaryDirectory() as tmp:
    store = WorkflowStore(root=tmp)
    run = store.create_run(WorkflowRun(run_id='wf_gpt55_literal', session_id='session_gpt55_literal', script=draft.script, status='running'))
    outcome = WorkflowRuntime(
        store=store,
        runner=FakeChildAgentRunner(),
        scheduler_config=SchedulerConfig(max_concurrent=3, max_total=8),
        timeout_seconds=10.0,
    ).run(run)
    loaded = store.load_run(run.run_id)
    print('status=', outcome.run.status)
    print('phases=', outcome.phases)
    print('logs=', outcome.logs)
    print('job_count=', len(loaded.jobs))
    prompts = '\n---PROMPT---\n'.join(job.prompt for job in loaded.jobs)
    print('contains_literal_user=', '${user}' in prompts)
    print('contains_literal_expr=', '${1+2}' in prompts)
    assert outcome.run.status == 'succeeded'
    assert 'INJECTED' not in '\n'.join(outcome.logs)
PY
```

Expected:

- `plannerMode= prompt_guided` or `fallback_deterministic` only if provider failed; if fallback occurs, record fallback reason and rerun after fixing provider/config.
- `validation['ok']` is true.
- runtime status is `succeeded`.
- no unexpected injected logs/phases/jobs.
- At least the prompts generated from user task or planner output should not fail due to `${...}`. If the LLM rewrites the task and omits the literal template syntax, that is acceptable for smoke testing the live provider but does not replace automated literal tests above.

- [ ] **Step 3: If live provider is unavailable**

If the command fails due to quota/network/auth/provider error, do not hide it. Capture the exact sanitized error and state that real gpt-5.5 verification was blocked by environment/provider, while local deterministic and fake-client tests still verify the renderer fix.

---

### Task 9: Final verification and commit readiness

**Files:**
- Review changed files only.

- [ ] **Step 1: Inspect git diff**

Run:

```bash
git diff -- workflow_planner.py tests/test_workflow_plan_validator.py tests/test_workflow_prompt_guided_planner.py
```

Expected:

- Diff only contains the escaping fix and tests described above.
- No secret files, config files, or unrelated formatting changes appear.

- [ ] **Step 2: Run final focused tests one more time**

Run:

```bash
python -m unittest tests.test_workflow_plan_validator tests.test_workflow_planner_compiler tests.test_workflow_prompt_guided_planner tests.test_workflow_runtime tests.test_workflow_controller tests.test_ink_bridge
```

Expected:

```text
OK
```

- [ ] **Step 3: Prepare commit only if requested**

Do not commit unless the user explicitly asks. If committing later, use Chinese Conventional Commit style, for example:

```bash
git add workflow_planner.py tests/test_workflow_plan_validator.py tests/test_workflow_prompt_guided_planner.py docs/superpowers/plans/2026-06-30-workflow-template-interpolation-fix.md
git commit -m "fix(workflow): 修复 prompt 模板插值转义边界

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- TDD: Tasks 1, 3, and 5 add failing tests before implementation or before completing dependent fix.
- Fix: Tasks 2 and 4 modify only `workflow_planner.py` and preserve dependency injection semantics.
- Self-test: Tasks 6, 7, and 9 define focused and broader test commands.
- Real gpt-5.5: Task 8 defines a live planner smoke test using existing `mykey.py` config without reading or printing secrets.

**Placeholder scan:**
- No TBD/TODO/fill-in-later placeholders.
- `<GPT55_CONFIG_NAME>` is an explicit runtime substitution discovered in Task 8 Step 1, not an implementation placeholder.

**Type/name consistency:**
- Test class names match existing files: `WorkflowPlanValidatorTest`, `LLMWorkflowPlannerTest`.
- Imports match existing module names: `WorkflowRuntime`, `WorkflowStore`, `WorkflowRun`, `SchedulerConfig`, `FakeChildAgentRunner`.
- Existing helper `valid_plan()` is reused in `tests/test_workflow_plan_validator.py`.
