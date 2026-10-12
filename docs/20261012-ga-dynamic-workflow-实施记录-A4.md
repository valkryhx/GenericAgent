# GenericAgent Dynamic Workflow 实施记录：A4 `meta` 契约强化

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 阶段：A4（对应实施方案 G4）
> 上游参考：Claude Code 2.1.283 真实源码（只读）
> 状态：✅ 已完成

---

## 0. 本阶段目标

把 GA 原先“正则剥掉 `export const meta`”的弱处理，替换为 Claude Code（下称 CC）同语义的脚本契约：

1. 脚本解析前先做大小上限检查；
2. `export const meta = {...}` 必须是脚本第一条语句；
3. `meta` 初始化值必须是纯字面量；
4. 校验 `name` / `description` / `title` / `whenToUse` / `phases`；
5. 校验失败时在进入 Node `vm.Script` 前拒绝；
6. planner 路径和 raw-script 路径都接入同一契约。

本阶段只复刻本地 workflow 脚本契约，不涉及 CC 云端会话。

---

## 1. CC 源码证据

### 1.1 脚本入口

`claudecode283\modules\m0862_0xd11f1fb.js`：

| CC 符号 | 位置 | 职责 |
| --- | --- | --- |
| `Fy()` | @3670 | 脚本解析、首语句校验、meta 求值、返回 `meta + scriptBody` |
| `U()` | 同段 | 判断首语句是否为 `export const meta = ObjectExpression` |
| `O()` | @5927 | 纯字面量白名单求值 |
| `j()` | 同段 | `ObjectExpression` 属性求值 |
| `q()` | 同段 | 属性键读取与保留键拒绝 |
| `M()` | 同段 | `meta` 字段校验 |
| `K()` | 同段 | `phases` 规范化 |
| `C()` | 同段 | 解析错误文案与源码窗口 |

关键实现事实：

- acorn 选项为 `ecmaVersion: "latest"`、`sourceType: "module"`、`allowAwaitOutsideFunction: true`、`allowReturnOutsideFunction: true`。
- 默认 `maxBytes` 为 `524288`。
- 首语句必须同时满足：
  - `ExportNamedDeclaration`
  - `declaration.type === "VariableDeclaration"`
  - `declaration.kind === "const"`
  - `declaration.declarations.length === 1`
  - `declarator.id.type === "Identifier"`
  - `declarator.id.name === "meta"`
  - `declarator.init.type === "ObjectExpression"`
- 执行时 CC 使用 `Fy()` 返回的 `scriptBody`，不是把整个脚本连同 `export` 一起塞进 VM。
- CC 在 WorkflowTool 中先 `Fy(script)`，再对 `g.scriptBody` 调确定性闸门 `LKt()`；GA 的接入顺序与之一致。

### 1.2 纯字面量规则

允许：

- `Literal`
- `ArrayExpression`
  - 不允许 sparse；
  - 不允许 `SpreadElement`。
- `ObjectExpression`
  - 只允许普通 `Property`；
  - 不允许 computed key；
  - 不允许 method / accessor。
- `TemplateLiteral`
  - 不允许 `${...}` 插值。
- `UnaryExpression`
  - 只允许负数 `-Literal(number)`。

禁止的保留键：

- `__proto__`
- `constructor`
- `prototype`

### 1.3 字段规则

| 字段 | 规则 |
| --- | --- |
| `name` | 必填，非空 string |
| `description` | 必填，非空 string |
| `title` | 可选；仅非空 string 保留 |
| `whenToUse` | 可选；string 即保留 |
| `phases` | 可选数组；只保留 `{ title: string, detail?: string, model?: string }` |

---

## 2. GA 落地改动

### 2.1 根依赖

新增：

- `package.json`
- `package-lock.json`

依赖：

- `acorn`（当前锁定 `8.19.0`）

根 `node_modules/` 已加入 `.gitignore`；仓库只提交 manifest 与 lockfile。

### 2.2 `workflow_script_contract.js`

新增共享 Node 契约模块，逐条复刻 `Fy()/U()/O()/j()/q()/M()/K()`：

- `parseWorkflowScript(script, { maxBytes })`
- `formatParseError(error, source)`
- CLI `node workflow_script_contract.js --check`
  - stdin 读取脚本；
  - stdout 输出 JSON `{ ok, meta, scriptBody }` 或 `{ ok: false, error }`；
  - 供 Python planner preflight 调用。

### 2.3 `workflow_js_worker.js`

改动：

- 删除 `transformScript()` 正则剥离逻辑；
- 启动执行前调用 `parseWorkflowScript()`；
- 校验失败抛：

```text
Invalid workflow script: <CC 风格错误>
```

- 校验通过后，只把 `parsed.scriptBody` 包进 async IIFE；
- 之后才创建 sandbox / `vm.Script`。

这保证 raw script 不能绕过 meta 契约进入 VM。

### 2.4 `workflow_planner.py`

新增 `_workflow_script_contract_preflight(source)`：

- 调用 `workflow_script_contract.js --check`；
- 使用 UTF-8 读取 Node 输出，避免 Windows GBK 解码中文脚本文案；
- 返回 CC 风格结构化错误。

`validate_rendered_workflow_script()` 顺序改为：

1. 空脚本检查；
2. Node 契约检查；
3. 对 `scriptBody` 做 `node --check`；
4. 对 `scriptBody` 做 `determinism_error()`；
5. 全部通过才返回 `{ ok: true }`。

`WorkflowController.create_planned_run()` 的 `invalid_workflow_script` 分支因此同时覆盖 meta 契约与确定性错误。

---

## 3. 测试与迁移

### 3.1 新增测试

`tests/test_workflow_meta_contract.py`，共 20 条：

- 合法 meta；
- 合法 phases / title / whenToUse / 负数；
- meta 不是第一条；
- 非 `export const`；
- 非对象 meta；
- 变量初始化；
- 函数调用；
- spread；
- computed key；
- method / accessor；
- 保留键；
- 模板插值；
- sparse array；
- 缺 `name` / `description`；
- 超长脚本；
- planner preflight 拒绝；
- runtime 在 VM 前拒绝；
- runtime 拒绝缺失 meta。

### 3.2 旧测试迁移

以下测试文件原先大量使用“裸 JS body”作为 workflow script，已统一迁移为 CC 合法的 `meta + body`：

- `tests/test_workflow_runtime.py`
- `tests/test_ink_bridge.py`
- `tests/test_p8_provider_anomaly_e2e.py`
- `tests/test_workflow_permission_inheritance_e2e.py`
- `tests/test_workflow_plan_validator.py`
- `tests/test_workflow_controller.py`

迁移原则：

- 不降低生产校验；
- 测试继续覆盖原 body 语义；
- 新 A4 测试单独覆盖“缺少 meta / 非法 meta”拒绝路径。

---

## 4. 验证记录

```bash
node --check workflow_js_worker.js
node --check workflow_script_contract.js
python -m py_compile workflow_planner.py workflow_runtime.py workflow_determinism.py

python -m unittest tests.test_workflow_meta_contract tests.test_workflow_determinism \
  tests.test_workflow_plan_validator tests.test_workflow_controller \
  tests.test_workflow_runtime tests.test_ink_bridge
# Ran 298 tests ... OK

python -m unittest discover -s tests
# Ran 1429 tests ... OK (skipped=3)

npm --prefix frontends/ink-ui run typecheck
npm --prefix frontends/ink-ui run test
# tests 404, pass 404, fail 0
```

文档与新文件已检查为 LF、无 BOM；`git diff --check` 通过。

---

## 5. 边界与后续

- 云端会话（`isolation:"remote"` / `Ho()` / 云端会话）不在 A4 范围。**【复刻时暂时不需要参考 claudecode 云端会话的实现】**
- GA 的 `create_draft()` 仍负责持久化草稿；权威 pre-VM 闸门在 approve 后的 `WorkflowRuntime.run()`。若未来要在草稿创建阶段立即拒绝，可直接复用 `validate_rendered_workflow_script()`，无需新增第二套契约。
- 下一阶段建议继续 B1 `budget`、B2 子 agent 取消语义、C1 `/workflows` 历史恢复、C2 状态行文案。

---

## 6. 查阅路径

- `D:\git_codes\GenericAgent\docs\20261012-ga-dynamic-workflow-复刻实施方案.md`
- `D:\git_codes\GenericAgent\docs\20261012-ga-dynamic-workflow-实施记录-A1.md`
- `D:\git_codes\GenericAgent\docs\20261012-ga-dynamic-workflow-实施记录-A2-A3.md`
- `D:\git_codes\claudecode283_hack\docs\20261012-复刻dynamic-workflow指南.md`
- `D:\git_codes\claudecode283_hack\claudecode283\extracted\dynamic-workflow-internals.md`
- `D:\git_codes\claudecode283_hack\claudecode283\modules\m0862_0xd11f1fb.js`
- `D:\git_codes\claudecode283_hack\claudecode283\modules\m1569_0xdceb889.js`
