# GenericAgent 复刻 Claude Code Dynamic Workflow 实施方案

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 目标读者：在本仓库实施该方案的 Codex / 开发者
> 上游源码调研（Claude Code 2.1.283 真实源码，只读参考）：见 §2 的绝对路径清单
> 范围：**不重写 GA 的 workflow 主干**，只做「语义对齐 + 缺口补齐 + 验收」

---

## 0. 结论先行

GA 的 dynamic workflow 主干已经建成，且大部分能力与 Claude Code（下称 CC）对齐。
本方案只补 **10 条语义级差异/缺口**，目标是"行为一致"，不是"代码照搬"。

**已经对齐、无需重做**：

- planner → 计划 → JS worker → scheduler → store → Ink UI 全链路；
- `agent()` / `parallel()` / `pipeline()` / `phase()` / `log()` / `args` 的宿主侧实现；
- 权限 profile / 工具边界（`workflow_permissions.py` / `workflow_tool_profiles.py`）；
- journal / cache / `resume_from_run_id`（`workflow_store.py` / `workflow_scheduler.py`）；
- `/workflows` 列表 / 详情 / 状态行（`workflowList` / `workflowPanel.ts` / `workflowStatusBar.ts`）；
- schema → 结构化输出 + 重试（`workflow_verification.py` / `workflow_check_adapters.py`）。

**待补齐（本方案范围）**：见 §4 缺口清单。

---

## 1. 背景：CC 与 GA 的架构对照

### 1.1 两条生成路径（关键差异）

- **CC**：模型**直接写 JS 编排脚本** → `Fy()` 解析（meta 第一条 + 纯字面量）→ `LKt()` 确定性检查 → `vm.Script` 执行。
- **GA**：模型写 **WorkflowPlan(JSON)** → `validate_workflow_plan()` → `render_workflow_plan()` 渲染成 JS → `workflow_js_worker.js` 执行。

GA 的 plan 路径更受限、更安全（无循环/条件/动态 fan-out），但**语义要对齐 CC**。
GA 同时保留了 `WorkflowController.create_draft(session_id, script)` 的 **raw script** 路径——这条路径必须补齐 CC 的契约与确定性检查。

### 1.2 GA 的 DSL 现状（`workflow_js_worker.js`）

| API | GA 现状 | CC 语义 | 是否需改 |
| --- | --- | --- | --- |
| `agent(prompt, opts)` | RPC 回宿主 | 同 | 否 |
| `parallel(items)` | `allSettled`（单项失败→`null`） | 同 | ✅ 已对齐（A2） |
| `pipeline(items, ...stages)` | 每 item 独立穿全部 stage（无 barrier），`null` 短路 | 同 | ✅ 已对齐（A3） |
| `phase(name)` / `log(msg)` / `console` | event | 同 | 否 |
| `args` | 注入 | 同 | 否 |
| `runPythonUnittest` / `repairAndRetest` | **GA 独有** | CC 无 | 保留 |
| `budget` | **缺（暂缓）** | `{total, spent(), remaining()}` | 暂缓（B1，用户决定先跳过） |
| `workflow(nameOrRef, args)` | **缺** | 一层嵌套 | 可选（B2） |

---

## 2. 上游源码调研文档（实施时逐条查阅）

> 以下路径都在 `D:\git_codes\claudecode283_hack\`（CC 2.1.283 真实源码 + 研究文档，**只读参考**）。

| 路径 | 回答什么 | 何时查 |
| --- | --- | --- |
| `docs\20261012-复刻dynamic-workflow指南.md` | **主文档**：CC dynamic workflow 全链路（脚本生成/契约/VM/DSL/调度/journal/UI）+ 复刻蓝图 + 坑清单 + **§14 移植对照（9 条必对齐语义）** | 每阶段先读 |
| `claudecode283\extracted\dynamic-workflow-internals.md` | 逐条源码坐标的剖析；**§9 UI 与交互** | 做 UI 时读 |
| `claudecode283\extracted\workflow-tool-description.md` | CC 喂给模型的 **tool description 原文**（何时该用 workflow） | 做 C3 时读 |
| `claudecode283\extracted\workflow-authoring-reference.md` | CC 喂给模型的 **DSL 编写参考原文**（约 17K 字符） | 做 C3 时读 |
| `claudecode283\modules\m0869_0xd12567a.js` | CC 引擎：`agent`(jo@43713) / `parallel`(Ko@74546) / `pipeline`(zo@75362) / `workflow`(yo@30215) / journal(Gt@21165) / 预算(ct@40291) / 主循环(Io@78833) | 做 A2/A3/B1/B2 时读 |
| `claudecode283\modules\m1569_0xdceb889.js` | CC WorkflowTool：`validateInput`(@62456) / `call`(@65948) / `inputSchema`(@61624) / `he`(@55937) | 做 A1/A4 时读 |
| `claudecode283\modules\m0862_0xd11f1fb.js` | CC 脚本解析 `Fy()`(@3670) + 纯字面量求值 `O()`(@5927) | 做 A4 时读 |
| `claudecode283\modules\m0870_0xd13b416.js` | CC 确定性闸门 `LKt()`(@711) | 做 A1 时读 |
| `claudecode283\modules\m1075_0xd34f8d5.js` | CC `/workflows` 命令定义 | 做 C1 时读 |
| `claudecode283\modules\m1906_0xe19cc02.js` | CC `/workflows` 历史列表对话框 | 做 C1 时读 |
| `claudecode283\modules\m1460_0xd973957.js` | CC workflow 详情对话框（进度树 + 按需读 agent 转录） | 做 C1 时读 |
| `claudecode283\modules\m1456_0xd96e09a.js` | CC 进度树折叠 helpers | 做 C1 时读 |
| `claudecode283\modules\m2000_0xe395211.js` | CC 状态行 | 做 C2 时读 |
| `claudecode283\extracted\README.md` | 上游文档索引 | 索引 |

**GA 侧已有分析**（同仓库 `docs\`）：`20261009-ga-workflow-stability-stepcode-borrow.md`（Step-Code 借鉴 + §11 schema 修复）、`20261001-workflow-reference-architecture-comparison.md`（架构对比）、`20261010-ga-ink-workflow-progress-ui.md`（Ink 进度 UI）、`GA_workflow_user_guide.md`、`workflow_ui.md`。

---

## 3. 架构对照（CC ↔ GA 文件映射）

| 层 | CC 2.1.283 | GA 现状 | 目标 |
| --- | --- | --- | --- |
| 触发 / opt-in | ultracode 关键词 + feature gate | `workflow_activation.py` + `/workflow` | 保持 |
| 生成 | 模型直接写 JS | `workflow_planner.py`（plan JSON）→ `render_workflow_plan()` | 保持（可选加 raw-script 提示） |
| 契约 | `export const meta` 第一条 + 纯字面量 | `workflow_script_contract.js`（acorn）+ worker 只执行 `scriptBody` | **A4 ✅** |
| 校验 | acorn + `LKt()` 确定性 | `workflow_determinism.py` + 契约 preflight + `node --check` | **A1/A4 ✅** |
| 执行 | `vm.Script`（Node） | `workflow_js_worker.js`（Node `vm`） | 保持 |
| DSL | agent/parallel/pipeline/workflow/phase/log/budget/args | agent/parallel/pipeline/phase/log/args + runPythonUnittest/repairAndRetest | **A2/A3/B1/B2** |
| 调度 | 信号量 + agent 上限 + 预算 | `AgentScheduler`（max_concurrent/max_total） | 保持 |
| 缓存 / resume | 链式 sha256 + 最长未变前缀 | hash(script/args/call/prompt/options) + `resume_from_run_id` | 保持（见 G6） |
| 存储 | runId.json + journal.jsonl | run.json/state.json/script.js/journal.jsonl/agents/* | 保持 |
| 权限 | isolation + disallowedTools | `workflow_permissions.py` profiles | 保持 |
| UI | `/workflows` 列表+详情+状态行 | workflowList/workflowPanel/workflowStatusBar | **C1/C2** |

---

## 4. 缺口清单（按优先级）

| # | 缺口 | 优先级 | CC 证据 | GA 目标文件 |
| --- | --- | --- | --- | --- |
| G1 | 无**确定性闸门** | **P0** | `m0870 @711 LKt` | 新增校验（§5 A1） |
| G2 | `parallel()` 用 `Promise.all`（单项失败→整批 reject）；CC 是 `allSettled`→单项 `null` | **P0** | `m0869 @74546 Ko` | `workflow_js_worker.js` |
| G3 | `pipeline()` 是"逐 stage 批量 `Promise.all`"（每 stage 有 barrier）；CC 是每 item 独立穿全部 stage（无 barrier） | **P0** | `m0869 @75362 zo` | `workflow_js_worker.js` |
| G4 | `meta` 未强制"第一条语句 + 纯字面量" | P1 | `m0862 @3670/@5927` | `workflow_js_worker.js` + `workflow_planner.py` + `workflow_script_contract.js` |
| G5 | 无 `budget`（token 预算对象）——**暂缓** | P1 | `m0869 @40291 ct` | `workflow_js_worker.js` + `workflow_runtime.py` |
| G6 | resume 键是否已实现"最长未变前缀"（CC 用链式 sha256） | P1 | `m0869 @20900 lo` | `workflow_scheduler.py`（先评估现状） |
| G7 | 子 agent 取消语义：确认是"永不 settle"而非 throw ——**已实施（B2）** | P1 | `m0869 @43713 jo` | `workflow_runtime.py` |
| G8 | `workflow()` 嵌套（CC 支持一层，二层拒绝） | **已实施（C3/G8）** | `m0869 @30215 yo` / `@29905 tsr` | `workflow_js_worker.js` + `workflow_runtime.py` + `workflow_scheduler.py` |
| G9 | 无 CC 的 **Workflow 工具入口 + authoring reference**（模型直接写 JS 的入口） | **已实施（C3/G8）** | `m1569 @61628/@62439/@65948`；`m1502 @60/@199`；`extracted/workflow-{tool-description,authoring-reference}.md` | `assets/tools_schema*.json` + `ga.py` + `frontends/ink_bridge.py` + `skills/workflow-authoring/SKILL.md` |
| G10 | `/workflows` "再跑历史"四条路径的完备性 + 状态行文案 ——**已实施（C1/C2）** | P2 | 指南 §9.5 / `m2000 @10379` | `ink_bridge.py` / `workflowPanel.ts` / `workflowStatusBar.ts` |

> 截至 2026-10-12：G1/G2/G3/G4 已按 CC 语义实施（A1~A4）；G7 已实施（B2）；G10 已实施（C1/C2）；G5（`budget`）**暂缓**（用户决定先跳过）；G6 已确认 GA 已支持"最长未变前缀"；G8/G9 已实施（见 `docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`）。C3/G8 的源码证据、差异与接线设计见 `docs/20261012-ga-dynamic-workflow-C3-G8-调研补充.md`。
> 实施记录：`A1` / `A2-A3` / `A4` / `B2` / `C1-C2`（同目录 `20261012-ga-dynamic-workflow-实施记录-*.md`）。

---

## 5. 分阶段实施

> 约定：每项走 GA 的 TDD 习惯（先写 `tests/`，再改实现），并在 `docs/` 留实施记录。
> 真实 API 测试必须 opt-in（`GA_RUN_REAL_LLM_TESTS=1` / `GA_RUN_REAL_API_E2E=1`），默认不烧 key。

### 阶段 A：语义对齐（P0，低风险高收益，先做）

#### A1 确定性闸门（G1）

> 实施状态（2026-10-12）：✅ 已完成。实施记录见 docs/20261012-ga-dynamic-workflow-实施记录-A1.md。

**目标**：脚本进入 VM 前，静态拒绝 `Date.now()` / `Math.random()` / 无参 `new Date()`，报错文案对齐 CC。
**CC 规则**（详见指南 §3.3）：三条 AST 规则；`Date["now"]` / 别名 / `globalThis.Date.now` **不**命中；`new Date(x)` **放行**；解析失败不判非确定性。
**改动点**：

- 新增 `workflow_determinism.py`，暴露 `has_nondeterminism(script: str) -> bool`；
- 接入点：`WorkflowController.create_draft`（raw script）+ `WorkflowRuntime.run`（执行前）+ `validate_rendered_workflow_script`。

**实现选择**（择一，推荐 b）：

- (a) Python 侧正则镜像三条规则 —— 零依赖，但**只是近似**（必须写清局限）。
- (b) **Node 侧 acorn**（与 CC 完全一致）——在 `workflow_js_worker.js` 里加 `checkDeterminism(script)`，或加独立 `workflow_determinism.js`。注意：Node 自带能力**做不到 AST**，需引入 `acorn` 依赖。
- (c) 运行时兜底：在 worker sandbox 里把 `Date.now` / `Math.random` / 无参 `new Date` 换成抛错——比静态检查更强，但**改变语义**（CC 不这么做），仅作防御纵深。

**TDD**：`tests/test_workflow_determinism.py`——命中/不命中用例各 ≥6（含 `new Date(x)` 放行、`Date["now"]` 不命中、`const D=Date;D.now()` 不命中）。
**验收**：raw script 含 `Date.now()` → run 被拒，文案与 CC 一致；`new Date(123)` 通过。

#### A2 `parallel()` 语义对齐（G2）

> 实施状态（2026-10-12）：✅ 已完成，按用户决定直接采用 CC 语义。实施记录见 `docs/20261012-ga-dynamic-workflow-实施记录-A2-A3.md`。

**目标**：单项失败 → 该项变 `null`，整批**不 reject**（CC 用 `Promise.allSettled`）。
**CC 证据**：`m0869 @74546 Ko`。
**已实施**：`workflow_js_worker.js` 的 `parallel` 已按 CC 原版实现：

```js
parallel: async (items) => {
  if (!Array.isArray(items)) throw new TypeError('parallel() expects an array of functions')
  if (items.length === 0) return []
  for (const item of items) {
    if (typeof item !== 'function') {
      throw new TypeError('parallel() expects an array of functions, not promises. Wrap each call: () => agent(...)')
    }
  }
  const settled = await Promise.allSettled(items.map(item => {
    try { return item() } catch (error) { return Promise.reject(error) }
  }))
  return settled.map(entry => entry.status === 'fulfilled' ? entry.value : null)
},
```

**行为变化**：旧 GA 契约是“单项失败拖垮整批并取消 pending job”；新契约是“失败项为 `null`，其他项继续，run 可正常返回”。
**TDD**：已覆盖 3 类场景——单项 agent 失败、thunk 同步 throw、provider 429；均返回 `null` 且保留其他项产物。
**验收**：`const r = await parallel([ok, fail, ok]);` 不中断，`r[1] === null`。

#### A3 `pipeline()` 语义对齐（G3）

> 实施状态（2026-10-12）：✅ 已完成，按用户决定直接采用 CC 语义。实施记录见 `docs/20261012-ga-dynamic-workflow-实施记录-A2-A3.md`。

**目标**：**每个 item 独立穿完所有 stage**，stage 之间**无 barrier**；某 item 的 stage 返回 `null` 时，该 item 后续 stage **短路**（CC 语义）。
**CC 证据**：`m0869 @75362 zo`。
**已实施**：`workflow_js_worker.js` 的 `pipeline` 已按 CC 原版实现：

```js
pipeline: async (items, ...stages) => {
  if (!Array.isArray(items)) throw new TypeError('pipeline() expects an array as the first argument')
  for (const stage of stages) {
    if (typeof stage !== 'function') {
      throw new TypeError('pipeline() stages must be functions: pipeline(items, item => ..., result => ...)')
    }
  }
  if (items.length === 0) return []
  const settled = await Promise.allSettled(items.map(async (item, index) => {
    let value = await item
    for (const stage of stages) {
      if (value === null) break
      value = await stage(value, item, index)
    }
    return value
  }))
  return settled.map(entry => entry.status === 'fulfilled' ? entry.value : null)
},
```

**注意**：GA 的 `render_workflow_plan` 目前只 emit `parallel` + `agent`，本项主要影响 raw-script 路径；但 worker DSL 本身已与 CC 对齐。
**TDD**：已覆盖无 barrier（item A 的 stage2 注册早于 item B 的 stage1 完成）、`null` 短路、单项 stage throw 映射为 `null`。
**验收**：无 barrier；`null` 短路生效；单项失败不影响其他 item。

#### A4 `meta` 契约强化（G4）

> 实施状态（2026-10-12）：✅ 已完成。实施记录见 `docs/20261012-ga-dynamic-workflow-实施记录-A4.md`。

**目标**：`meta` 必须是**第一条语句**且**纯字面量**（白名单：Literal / Array / Object / TemplateLiteral 无插值）。
**CC 证据**：`m0862 @3670 Fy`（第一条语句）/ `@5927 O`（字面量求值）/ `M()/K()`（字段校验）。
**已落地改动**：

- 新增根目录 `package.json` + `package-lock.json`，依赖 `acorn`；根 `node_modules/` 已加入 `.gitignore`。
- 新增 `workflow_script_contract.js`：逐条复刻 `Fy()/U()/O()/j()/q()/M()/K()`，支持 CLI `--check` 供 Python preflight 调用。
- `workflow_js_worker.js` 在创建 sandbox / `vm.Script` 前解析契约，校验失败抛 `Invalid workflow script: ...`；执行时只注入 `scriptBody`，不再用正则剥 `export`。
- `workflow_planner.py::validate_rendered_workflow_script()` 先跑契约检查，再对 `scriptBody` 跑 `node --check` 与确定性闸门；`WorkflowController.create_planned_run()` 的 `invalid_workflow_script` 路径已覆盖。
- 旧 runtime/Ink/provider/permission 测试脚本统一迁移为 `export const meta = {...}` 首语句；新增 `tests/test_workflow_meta_contract.py`，合法/非法覆盖 20 条。

**契约细节**：

- 首语句必须精确是 `export const meta = {...}`：`ExportNamedDeclaration` + `VariableDeclaration(kind=const, 恰好 1 个 declarator)` + `Identifier(meta)` + `ObjectExpression`。
- 纯字面量白名单：`Literal` / `ArrayExpression`（禁 sparse、禁 spread）/ `ObjectExpression`（禁 computed、method、accessor）/ 无插值 `TemplateLiteral` / 负数 `-Literal(number)`。
- 禁止 `__proto__` / `constructor` / `prototype` 键。
- `name`、`description` 必填非空 string；`title`、`whenToUse`、`phases` 按 CC `M()/K()` 规则规范化。
- 最大脚本长度 524288（CC `Fy()` 的 `maxBytes`）。

**TDD**：`tests/test_workflow_meta_contract.py`（20 条，覆盖第一条语句、非 const、非对象、变量、函数调用、spread、computed、method/accessor、保留键、模板插值、sparse、缺字段、超长、planner/runtime 拒绝）。
**验收**：非法 meta 在进 VM 前被拒并给出精确错误；planner 与 raw-script 两条路径都已接入。

### 阶段 B：能力补齐（P1）

#### B1 `budget`（G5，暂缓）

> **状态：暂缓（用户决定先跳过，后续再评估）。** 本阶段先做 B2/C1/C2，`budget` 保持缺口，
> 待 raw-script 路径真正需要"硬预算上限"时再补。注意 GA 现在没有 `budget`，
> 脚本里 `while (budget.remaining())` 这类写法会直接 `ReferenceError`（不是静默降级）。

**目标**：脚本里可读 `budget.{total, spent(), remaining()}`；`total` 未设时 `remaining() === Infinity`；`agent()` 超预算抛错。
**CC 证据**：`m0869 @40291 ct`（预算检查）/ 指南 §5.5、§6.4。
**改动点**：`workflow_js_worker.js` 注入 `budget`（由宿主经 `start` 消息传入 total）；`workflow_runtime.py` 统计 `totalTokens` 并回传。
**注意**：CC 的 `budget` 是**硬上限**（超了 `agent()` 直接抛），但**已 in-flight 的结果保留**。
**TDD**：`tests/test_workflow_budget.py`——设 total、超限、`remaining()` 行为。
**验收**：`while(budget.remaining())` 在无 total 时靠 agent 上限兜底（指南 §11.6 坑 5）。

#### B2 子 agent 取消语义（G7）——**已实施**

> **状态：已实施**（见 `docs/20261012-ga-dynamic-workflow-实施记录-B2.md`）。

**目标**：对齐 CC 的"取消 = 永不 settle 的 Promise"（不是 throw），否则 `parallel`/`pipeline` 会把取消吞成 `null` 后继续跑。
**CC 证据**：`m0869 @43713 jo`（`if (D.abortController?.signal.aborted) return new Promise(() => {});`）、
`@74546 Ko`（parallel 入口同一守卫）、`@75362 zo`（pipeline 入口同一守卫）、`m0869 @3419 Io`（顶层 `Promise.race` 于 abort）。
**改动点**：`workflow_runtime.py`
1. `_complete_pending_rpc`：`cancelled`/`killed` 的 job **不回任何 `rpc_result`**（保持 pending = 永不 settle），
   只有 `failed` 才回 `ok:false`（`parallel`/`pipeline` 把失败映射成 `null` 是 CC 原生行为，不要误改）。
2. `_raise_if_externally_killed` → **`_raise_if_externally_stopped`**：`killed` **与** `cancelled` 都算外部停止
   （原来只认 `killed`，导致 `workflow_controller.cancel()` 对 running 运行无效，工作流会一直跑到 deadline）。
3. `run()` 的异常分支：`cancelled` 不再被改写成 `failed`，终态保留 `killed`/`cancelled`，
   并分别落 `workflow_killed` / `workflow_cancelled` 事件。
**TDD**：`tests/test_workflow_runtime.py::WorkflowCancellationSemanticsTest`（4 条）——取消后脚本不推进、`parallel` 不被吞成 `null`、
外部 `cancel` 能停住 running 工作流、`_complete_pending_rpc` 对取消 job 不发帧。
**验收**：kill/stop/cancel 后脚本不再推进；`python -m unittest tests.test_workflow_runtime tests.test_ink_bridge tests.test_workflow_controller` 全绿。

#### B3 `workflow()` 嵌套（G8）——**已实施**

> **状态：已实施**（见 `docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`）。
> 源码证据、差异与详细接线见 `docs/20261012-ga-dynamic-workflow-C3-G8-调研补充.md` §5。

**目标**：脚本内可 `await workflow(nameOrRef, args)` 跑另一个 workflow，**只允许一层**；二层拒绝；父 run 停止时子 run 一起停止。
**CC 证据**：`m0869 @30215 yo` / `@29905 tsr`（拒绝桩）；`dynamic-workflow-internals.md §5.1.1 / §6`。
**改动点**：
- `workflow_js_worker.js` 注入 `workflow(nameOrRef, args)`（RPC 回宿主）；
- `workflow_runtime.py` 新增 `workflow` RPC：解析 named workflow / inline script / scriptPath，用嵌套 `WorkflowRuntime` 执行；
- 嵌套 runtime 设 `workflow_depth=1`、`parent_run_id=外层 runId`、`job_id_prefix=nested_<runId>_`；
- `workflow_scheduler.py` 增加 `job_id_prefix`，避免共用 runner 的 `agent_1` 状态键冲突；
- `_raise_if_externally_stopped()` 同时观察父 run 状态。
**验收**：一层嵌套成功并返回子结果、二层嵌套被拒、嵌套 run 的 metadata 含 `parentRunId`、停止外层能停住内层。

### 阶段 C：UI/UX 与提示（P2）

#### C1 `/workflows` "再跑历史"四条路径（G10）——**已实施**

> **状态：已实施**（见 `docs/20261012-ga-dynamic-workflow-实施记录-C1-C2.md`）。

**目标**：对齐 CC 的 4 条路径（指南 §9.5）：

1. 暂停后恢复（注入 `Resume the paused workflow by calling: Workflow({scriptPath, resumeFromRunId...})` 指令 → 模型再调 workflow）；
2. 另存为命名 workflow；
3. 失败/被杀后按 `<task-notification>` 的 `<recovery>` 恢复；
4. 工具侧 `runId` 运行操作——**CC 本 build 关闭，GA 不需要**。

**实际改动**：
1. **命名 workflow 库**（CC 路径 ②）：`workflow_store.py` 新增
   `save_named_workflow / load_named_workflow / list_named_workflows`，落盘 `<root>/named-workflows/<slug>.json`，
   重名默认拒绝（= CC "Press Enter again to overwrite"），`overwrite=True` 才覆盖。
2. **桥接命令**：`workflow_save` / `workflow_run_named` / `workflow_named_list`
   （`workflow_run_named` = CC `Workflow({name, args})`）。
3. **恢复指令文案**（CC 路径 ③）：`_workflow_run_payload()` 对
   `failed/killed/cancelled/interrupted` 的 run 附带 `recovery.command` / `recovery.message` / `recovery.agentTranscriptsDir`。
4. **前端**：`protocol.ts` / `state.ts` / `inputController.ts` / `localCommandTranscript.ts` / `workflowPanel.ts`。
   **`s` 改为 save（对齐 CC m1906），`x` 保持 stop**；`--overwrite` 是非模态等价物。
5. **路径 ④不做**：CC 本 build 关闭（`m1569 @55937 he()` 恒 `undefined`）。
**验收**：`/workflow resume RUN_ID` 命中缓存、不重跑已完成的 agent；`/workflow save` → `/workflow run <name>` 能复跑；
重名返回 `workflow_name_exists`。

#### C2 状态行文案（G10）——**已实施**

> **状态：已实施**（见 `docs/20261012-ga-dynamic-workflow-实施记录-C1-C2.md`）。

**目标**：对齐 CC 的状态行信息层级（CC 运行中 `Running in background · /workflows to monitor and save`；终态 `✓ Completed in <dur> · N agents · X tokens` / `✗ Failed` / `✗ Stopped`）。
**CC 证据**：`m2000 @10379`。
**GA 现状**：`workflowStatusBar.ts` 目前是进度条式 `N/M agents done · <tokens>`，**没有** CC 的 `/workflows to monitor and save` 提示（全仓 grep 无此文案）。
**实际改动**：`workflowStatusBar.ts`——保留 GA 更丰富的 `N/M agents done` 第二行，
第一行运行中改为 `Enter · /workflows · x stop`。**没有逐字照抄** `Running in background · /workflows to monitor and save`：
CC 那行不显示 workflow 名字，而 GA 单行要同时容纳名字 + 进度，78 列终端实测逐字照抄会把
`1/2 agents done` 截断成 `1/2 agents d…`；因此用短入口 + 把 `Enter view` 缩成 `Enter` 腾宽度。
**验收**：运行中状态行能看到 `/workflows` 入口；终态能区分 completed/failed/stopped（云端 `remote_launched` **不复刻**）。

#### C3 CC 风格 `workflow` 工具入口 + authoring reference（G9）——**已实施**

> **状态：已实施**（见 `docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`）。
> 源码证据、差异与详细接线见 `docs/20261012-ga-dynamic-workflow-C3-G8-调研补充.md` §1~§4。

**目标**：把 CC 的模型入口真正对齐：新增模型可见的 `workflow` 工具，让模型在明确 opt-in 后**直接写 JS 脚本**；`/workflow <task>` 保留为兼容的 planner 路径。
**CC 证据**：
- `m1569_0xdceb889.js @61628 inputSchema`：`script` / `scriptPath` / `name` / `args` / `resumeFromRunId` / `runId` / `remote`；
- `@62439 validateInput`：feature gate、named-only、scriptPath 可读性、resume 校验、`Fy()` meta 契约、`LKt()` 确定性闸门；
- `@64578 checkPermissions`：默认 `ask`；
- `@65948 call`：后台启动并立即返回 `{status:"async_launched", taskId, runId, workflowName, summary, transcriptDir, scriptPath}`；
- `m1502_0xdb4a986.js @60/@199 xnr(e)`：有 skill 系统时只给短提示让模型 `load_skill` authoring reference；GA 有 `load_skill`，走该分支。
**GA 改动点**：
- `assets/tools_schema.json` / `_cn.json` 增加 `workflow`；
- `ga.py::do_workflow` 调用 bridge 注入的 launcher；
- `frontends/ink_bridge.py::workflow_launch_from_tool`：绕过主 agent busy 检查，校验脚本后创建 run、标记 running、追加 `workflow_started`、启动既有 `_run_workflow_runtime` watcher，立即返回 `async_launched`；
- 新增 `skills/workflow-authoring/SKILL.md`，`skills_runtime.get_default_skill_roots()` 发现仓库 `skills/`；
- `workflow` 加入 `subagent_capabilities.ORCHESTRATION_TOOLS`，禁止 workflow 子 agent 递归起 workflow。
**本地化要求**：**照抄原文 + 标注来源**，不要臆造 API；GA 多了 `runPythonUnittest` / `repairAndRetest`，少了 `budget` / CC `model` / `effort` / `isolation` / `agentType` / `disallowedTools` / `bashCommandClamp`，引用时必须只暴露 GA 真实支持的 API。
**验收**：工具 schema 可见、`load_skill('workflow-authoring')` 可加载、合法脚本后台启动并出现在 `/workflows`、非法脚本不进 VM、完成后主 agent 收到 `<workflow_handoff>`、子 agent schema 不含 `workflow`。

---

## 6. 验收标准（整体）

- [x] A1~A4 全部落地，且 `python -m unittest discover -s tests` 全绿（1461 tests OK, skipped=3）；
- [x] `workflow_js_worker.js` 的 `parallel`/`pipeline` 语义与 CC 一致（A2/A3 测试）；
- [x] raw script 走完整契约（meta + 确定性）后才进 VM；
- [x] **B2 子 agent 取消语义**与 CC 一致（`WorkflowCancellationSemanticsTest` 4 条）；
- [x] **C1 `/workflows` 历史复用**（命名 workflow 库 + `workflow_save`/`workflow_run_named` + recovery 文案）；
- [x] **C2 状态行**运行中含 `/workflows` 入口（`npm ... run test` 405 tests pass）；
- [x] **C3 `workflow` 工具入口**：模型可直接写 JS，`workflow-authoring` skill 可加载，工具启动的 run 复用 `/workflows` 监控/保存/重跑；
- [x] **B3/G8 `workflow()` 一层嵌套**：一层成功、二层拒绝、父 run 停止传播、job id 不冲突；
- [ ] 真实 E2E（opt-in，`GA_RUN_REAL_API_E2E=1`）：raw script + planner 路径各一条，含 resume 命中缓存
      —— 测试资产已存在（`tests/real_*.py`），**本轮未跑真实 API**（需显式 opt-in + 真实 key）；
- [x] `npm --prefix frontends/ink-ui run typecheck` 与 `... run test` 全绿（405 tests pass）；
- [x] 每个阶段在 `docs/` 留实施记录（遵循 GA 的 `YYYYMMDD-*.md` 约定）。

---

## 7. 不做的事（边界）

- **不复刻 CC 的云端会话**：`isolation:"remote"` / `Ho()` / `Uo` / `WP()` 是被关掉的死代码路径（指南 §5.1.1）。**【复刻时暂时不需要参考 claudecode 云端会话的实现】**
- **不照搬 CC 的标识符**：CC 是 minify 产物，名字带证据等级；GA 用 GA 自己的命名。
- **不把静态检查当安全边界**：确定性闸门只挡"模型顺手写 `Date.now()`"，不是沙箱（指南 §3.3）。
- **不默认开真实 API 测试**。
- **不打印/提交真实 key**（遵守 `AGENTS.md` 安全规则）。

---

## 8. 风险与注意事项

1. **`parallel` 改语义是行为变更（已实施）**：GA 旧脚本若依赖"失败即整批 reject"会受影响；本仓库已按用户决定直接切换到 CC 语义，并同步更新测试与实施记录。
2. **`pipeline` 无 barrier（已实施）**：若有下游逻辑依赖"全量 stage-N 完成后才进 stage-N+1"，行为会改变；本仓库已同步更新测试与实施记录。
3. **确定性检查的实现成本**：GA 无 Python JS 解析器；Node 侧加 acorn 是额外依赖。若走正则近似，务必在文档里写清局限。
4. **planner vs raw script 双路径**：契约/确定性检查要**两条路径都接**，不能只接一条。
5. **resume 键（G6）**：先确认 GA 现状是否已能复用"最长未变前缀"（P8 记录说已支持），再决定是否改成 CC 的链式 sha256。

---

## 附：一页速查（给实施者）

- **先读**：`docs\20261012-复刻dynamic-workflow指南.md` §14（9 条必对齐语义）+ §3（契约/确定性）+ §5（DSL 语义）。
- **改哪个文件**：见 §3 架构对照 + §4 缺口清单的"GA 目标文件"列。
- **怎么验收**：见 §6。
- **别踩的坑**：见指南 §11.6 + 本方案 §8。
