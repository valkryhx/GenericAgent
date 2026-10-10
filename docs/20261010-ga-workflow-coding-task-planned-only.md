# /workflow 编码任务只产出 PLAN.md：根因与修复（2026-10-10）

## 现象

用户在 GA ink 输入：

```
/workflow 分别用python写3个demo：1.hello word程序 2. 1-100内的质数 3. html展示你好二字 然后检验结果
```

运行目录 `temp/workflow-runs/wf_622266234f1345359d4e5f999758b922` 只有 `PLAN.md`，
3 个 demo 一个都没生成；UI 全程显示 `0/1 agents done`；最终 run 状态是 **succeeded**。

## 结论：不是执行中断，是计划里从来没有执行步骤

根因链（每一环都有落盘证据）：

1. **UI 把 `/workflow ` 前缀剥掉** 再发 `workflow_plan`（`frontends/ink-ui/src/inputController.ts`），
   所以 planner 拿到的 taskText 是 `分别用python写3个demo：...`，看不到用户的显式 opt-in。
2. **生产路径用的是 deterministic planner**：`ink_bridge._make_workflow_planner()` →
   `build_workflow_planner_from_env()`，而 `GA_WORKFLOW_PLANNER_MODE` 默认 `deterministic`，
   LLM planner（prompt_guided/real）是 opt-in。证据：`run.json` 里 `"plannerMode": "deterministic"`。
3. **关键词分类器把任务判成 planning**：`WorkflowPlanner.classify()` 的 coding 分支只认
   `实现/修复/开发/修改/implement/fix/code`。任务用的是「写…程序」「检验结果」，一个都不匹配，
   于是落到 `else` → `taskType: planning`、`readWriteMode: read_only`。
   证据：`workflow-draft.json` 的 `classification`。
4. **planning 模板只有一个 planner job**：`_build_plan()` 的兜底返回是
   `label=planner`、prompt=`任务：… 制定最小执行计划和验证建议。`。
5. 运行时忠实地执行了这个 1-job DAG，子代理写出 `PLAN.md`，run 报 succeeded、1/1。

**所以没有任何东西失败**——计划本身就不包含「写 demo」这一步。用户看到的是「workflow 没执行完」，
实际是「workflow 只被安排去规划」。

## UI 的 `0/1` 是什么

`1` = 这个计划里有多少个 agent（job），`0` = 已经结束的个数。
所以 `0/1` 的意思是「这个 workflow 只有 1 个 agent，还没跑完」——它说的是实话，
只是因为那个唯一的 agent 是 planner，看起来才荒谬。它是分类缺陷的显示面，不是另一个 bug。

## 架构：先规划完整 DAG，再执行（不是边跑边规划）

GA workflow 是两个分离的阶段：

1. **规划阶段（宿主侧）**：`WorkflowPlanner` / `LLMWorkflowPlanner` 产出 `WorkflowPlan`
   （phases → agents + `dependsOn`，即真正的 DAG），渲染成 `script.js`，经
   `validate_rendered_workflow_script()` 预检。
2. **执行阶段**：runtime 按依赖波次调度 job。

注意有两个东西都叫 planner，这是混淆来源：一个是**构建 DAG 的宿主 planner**，
另一个是 planning 模板里**那个 label 叫 planner 的 job**。

「是不是永远只有两步」——不是，但生产默认下确实只有 4 种固定形状：

| taskType | 计划形状 |
|---|---|
| planning | 1 job：`planner`（只写计划，不干活） |
| research | 2 jobs：source-discovery → synthesis |
| coding | 4 jobs：understand → write-tests → implement → verify |
| mixed | 3 jobs：research-sources → write-html → verify-html |

真正的「按任务难度动态生成 DAG」只在 `GA_WORKFLOW_PLANNER_MODE=prompt_guided|real` 时存在
（LLM 写 DAG），而那是 opt-in。测试脚本和探针都设了这个变量，所以它们跑的是 LLM planner，
与用户 `./ga` + `/workflow` 的真实路径不一致。

## 修复：显式 `/workflow` 必须执行，不能塌缩成「只规划」

三处改动，都避免新增关键词猜测：

1. `frontends/ink_bridge.py::workflow_plan()`：每条 `workflow_plan` 命令都是用户的显式 opt-in
   （UI 只在 `/workflow <task>` 时发它），所以在 caller 没给 activation 时补上
   `activation = {action: "requested", mode: "explicit"}`。自动推荐路径自己带 activation，
   `setdefault` 不会覆盖它。
2. `workflow_planner.WorkflowPlanner.classify()`：读 `context["activation"]`。显式 opt-in 且
   没匹配到 research/coding/review 时，不再落进 `planning`，而是 `taskType: general`
   （类型仍然只是提示，不触发硬门禁）。
3. `_build_plan()`：新增 `general` 模板 —— `execute-task`（role=implementation，
   toolProfile=authoring，capabilities=[file_read, file_write, execute]）→ `verify-result`
   （toolProfile=verify，VERIFICATION_SCHEMA 严格校验）。

没有替任务猜工具名（`capabilities` 由宿主解析成实际连上的工具），也没有猜产物路径，
所以 `executionContract.artifacts` 保持为空——产物形状在这里本来就不可能知道。
唯一声明的必需检查是 verifier 的结构化结论（`verification_schema`），
**没有**声明 `python_unittest`：那正是「研究型计划被套上代码门禁」的老毛病。

保留不变的行为（有测试守着）：纯文本 `解释刘国梁` 仍然是 planning/单 planner；
`只规划不要执行` 仍然是 planning；`用 tavily 搜索…写 html 并验证` 仍然是 mixed 三 agent。

## 真实 E2E 验证

用真实 bridge + 真实 LLM（默认 profile）+ 真实 deterministic planner 跑用户那条任务：

```
planned run wf_9d097fb199824a1eb4bb37776ef8a2f1 mode workflow taskType general plannerMode deterministic
progress execute-task:running
progress execute-task:done, verify-result:running
FINAL status degraded outcome succeeded acceptance passed
elapsed 94s
```

产物（`temp/workflow-runs/wf_9d097fb199824a1eb4bb37776ef8a2f1/`）：

```
  45  demo1_hello.py          print("Hello World")
 371  demo2_primes.py         1-100 质数（25 个）
 781  demo3_hello_html.py     生成 HTML 的脚本
 614  hello.html              <h1>你好</h1>
```

修复前同一条任务只产出 `PLAN.md`；修复后 3 个 demo + HTML 全部落盘，验收 passed。

## 第二轮：两个遗留问题都已修复

### A. 删掉 `plannerMode: deterministic`（计划必须由模型产出）

对照 Step-Code：它的 workflow **没有确定性计划**——`workflow` 工具拿到的就是模型自己写的
编排脚本（`step-workflow.ts` 的 `script` / `scriptPath` / `name` 三选一），不存在"从 N 个模板里挑一个"的
规划器。固定模板集也谈不上 dynamic：它只能命中关键词选形状，选错就等于用户要的活没被安排。

改动：

- `build_workflow_planner_from_env()` 默认改为 `prompt_guided`，即 `LLMWorkflowPlanner`。
  未设置 `GA_WORKFLOW_PLANNER_MODE` 时不再回退到模板规划器。
- `GA_WORKFLOW_PLANNER_MODE=deterministic` **被删除**：显式设置它会直接抛错并说明原因
  （"a fixed template set is not a dynamic workflow"），而不是被静默忽略。
- `WorkflowPlanner`（模板规划器）只保留为**规划模型报错时的内部 fallback**，
  该路径把 run 标成 `fallback_deterministic` 并降级，永远不会被当成正常计划。
- `workflow_controller` 里 metadata 缺省值 `deterministic` 改成 `unknown`，
  不再用一个已经删掉的名字当兜底。

### B. verifier 跑产物不再算「抢占写入」

**先回答那个问题：不是提示词没写好，是能力边界问题。** 三层事实：

1. `verify` tool profile 只 deny `file_write`/`file_patch`，**allow `execute`**
   （`workflow_tool_profiles.WORKFLOW_TOOL_PROFILES["verify"] = {file_write}`）。
2. `code_run` 属于 `execute` 类，是任意 Python；`workflow_path_acl.check_tool_call` 对 `code_run`
   只做**包含性**校验（cwd 与解析出的写入目标必须在 workspace 内），不做只读性校验。
3. 于是"只读的 verifier"一旦**执行被测产物**（最强的核验手段），就必然改动 workspace。

计划里的 prompt（"不要修改产物"）和 `workflow_child_agent` 的 verification role instruction 都只是建议，
没有任何东西在执行期拦它——所以问题不在提示词，而在"允许 execute 的 profile 声称自己是只读"这个矛盾。
Step-Code 的 `qa` profile 同样是 `READ_ONLY_TOOLS + run_command`，它不出这个症状只是因为
它根本没有产物归属/碰撞的概念。

改动（`workflow_scheduler`）：共享路径按**非验证写入者**的数量分类：

- 两个 *产出者* 写同一路径 → 仍然是 `artifactCollisions` + `artifact_path_collision` issue（真问题，保留）；
- 一个产出者 + 验证者（或纯验证者）写同一路径 → 记入 `verificationSideEffects` +
  `verification_side_effect` 事件，**不再进 workflowIssues，因此不再降级**。

`frontends/ink_bridge.py` 的 handoff 也带上 `verificationSideEffects`，并说明它是"同一个交付物被核验
重新生成"，不是第二份产物——否则下游 LLM 会把 `hello.html` 当成两个人的输出。

### 第二轮真实 E2E（默认路径，模型规划）

```
plannerMode prompt_guided | taskType coding | mode workflow
progress Python and HTML Implementation:running
progress ... , Verification Runner:running
FINAL status succeeded | outcome succeeded | acceptance passed | issues []
elapsed 199s
```

产物：`src/hello_world.py`、`src/primes_1_100.py`、`src/hello.html`；
`artifactCollisions`/`verificationSideEffects`/`workflowIssues` 全为空。
注意 phase 名（`Python and HTML Implementation` → `Verification Runner`）是模型自己起的，
不再是那 4 个模板之一；`taskType` 也由模型判成 `coding`——关键词分类器当初判的是 `planning`。

## 复现与回归

```bash
python -m unittest tests.test_workflow_planner_execution_intent tests.test_ink_bridge
python -m unittest discover -s tests -p "test_workflow*.py"
```

新增回归：

- `test_explicit_workflow_opt_in_executes_instead_of_only_planning`、
  `test_unrecognised_task_without_explicit_opt_in_keeps_planning_template`，以及
  `tests/test_ink_bridge.py` 中对 `workflow_plan` 必须携带显式 activation 的断言（第一轮）；
- `test_build_workflow_planner_from_env_defaults_to_the_model_planner`、
  `test_deterministic_planner_mode_is_rejected`（A：默认模型规划、deterministic 抛错）；
- `test_a_verifier_running_the_artifact_is_a_side_effect_not_a_collision`、
  `test_a_verification_only_path_is_never_reported_as_a_collision`（B：验证写入记 side effect）。

真实 E2E 探针：`frontends/ink-ui/scripts/_probe_workflow_default_planner.ts`
（走默认路径、不设 `GA_WORKFLOW_PLANNER_MODE`）。
