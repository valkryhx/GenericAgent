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

## 遗留问题（需要拍板，本轮未改）

### 1. verifier 跑产物导致 `degraded`

verify agent 为了核验 HTML，用 `code_run` 执行了 `demo3_hello_html.py`（一种完全合理的核验手段），
于是 `hello.html` 被重新生成了一次。宿主如实记录：

```
artifact_path_collision: hello.html was written by multiple jobs: execute-task, verify-result
```

这条 issue 把 run 从 `succeeded` 降级成 `degraded`（验收仍然 passed、产物完好）。
问题在于 `verify` profile 是「read-only + execute」，而 `code_run` 天然能写文件，
所以「只读的 verifier」在跑产物时会不可避免地改动 workspace。

两种改法，取向不同：

- **A（推荐）**：ownership 只归第一个产出者；`role=verification` 的 job 重写一个已有路径
  记成「验证副作用」而不是 collision，不再降级。verifier 新建的文件仍然算它的证据。
- **B**：verify job 在 workspace 的临时副本里执行，改动不落回主 workspace。更干净，但要动 runtime 的 workspace 布局。

### 2. 是否让 LLM planner 成为 `/workflow` 的默认

现在生产默认是 deterministic（4 个固定模板）。要真正做到「按任务难度生成不同 DAG」，
需要把默认改成 `prompt_guided`（LLM planner），deterministic 退为 fallback。
代价：每次 `/workflow` 多一次 planner LLM 往返（延迟 + 成本），
好处：DAG 由任务本身决定，而不是命中哪个关键词。
这属于产品行为变更，等用户决定后再动 `build_workflow_planner_from_env` 的默认值
（注意 `tests/test_workflow_prompt_guided_planner.py::test_build_workflow_planner_from_env_defaults_to_deterministic`
断言的就是这个默认值，改默认要一起改）。

## 复现与回归

```bash
python -m unittest tests.test_workflow_planner_execution_intent tests.test_ink_bridge
python -m unittest discover -s tests -p "test_workflow*.py"
```

新增回归：`test_explicit_workflow_opt_in_executes_instead_of_only_planning`、
`test_unrecognised_task_without_explicit_opt_in_keeps_planning_template`、
以及 `tests/test_ink_bridge.py` 中对 `workflow_plan` 必须携带显式 activation 的断言。
