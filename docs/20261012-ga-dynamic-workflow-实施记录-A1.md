# GenericAgent Dynamic Workflow 复刻实施记录（A1 确定性闸门）

> 日期：2026-10-12
> 对应实施方案：`D:\git_codes\GenericAgent\docs\20261012-ga-dynamic-workflow-复刻实施方案.md`
> 对应 CC 源码调研：`D:\git_codes\claudecode283_hack\docs\20261012-复刻dynamic-workflow指南.md` §3.3
> CC 源码坐标：`m0870_0xd13b416.js` `LKt()` @711
> 范围：阶段 A / A1（G1）

## 1. 实施结论

A1 已完成并接入两条 GA workflow 路径：

- planner 路径：`WorkflowPlan` 渲染出的 JS 在 `validate_rendered_workflow_script()` 的 `node --check` 之后执行确定性检查；失败时返回 `ok=false`，`WorkflowController.create_planned_run()` 会追加 `invalid_workflow_script` issue。
- raw script 路径：`WorkflowRuntime.run()` 在启动 workflow worker 之前执行确定性检查；失败时沿用 runtime 既有异常处理，将 run 落为 `failed` 并写入 `final-result.json` / journal。

## 2. 代码变更

| 文件 | 变更 |
| --- | --- |
| `workflow_determinism.py` | 新增零依赖静态确定性闸门：`has_nondeterminism()` / `find_nondeterminism()` / `determinism_error()`，错误文案对齐 CC `LKt()`。 |
| `workflow_planner.py` | `validate_rendered_workflow_script()` 在语法检查通过后执行确定性检查。 |
| `workflow_runtime.py` | `WorkflowRuntime.run()` 在 `subprocess.Popen()` 之前检查 `run.script`；检查失败抛出 `RuntimeError`，由既有 `except` 路径统一落盘失败状态。 |
| `tests/test_workflow_determinism.py` | 新增 10 个单元测试，覆盖命中、放行、注释/字符串忽略、模板插值、CC 同款绕过面和错误文案。 |
| `tests/test_workflow_plan_validator.py` | 新增 planner preflight 拒绝非确定性脚本的回归测试。 |
| `tests/test_workflow_runtime.py` | 新增 raw script 被 runtime 拒绝、且生成失败 final result 的回归测试。 |
| `tests/test_workflow_controller.py` | 新增 planner 渲染路径经 controller 拒绝非确定性脚本的集成回归测试。 |

## 3. 对齐后的行为

命中并拒绝：

- `Date.now()`
- `Math.random()`
- 无参 `new Date()` / `new Date`

按 CC 语义放行：

- `new Date(1700000000000)` / `new Date(args.ts)`
- `Date["now"]()`、`const D = Date; D.now()`、`globalThis.Date.now()`
- 字符串、注释中的同名文本

错误文案：

```text
Workflow scripts must be deterministic: Date.now()/Math.random()/new Date() are unavailable (breaks resume). Stamp results after the workflow returns, or pass timestamps via args.
```

## 4. 验证记录

```bash
python -m unittest tests.test_workflow_determinism tests.test_workflow_plan_validator tests.test_workflow_runtime tests.test_workflow_controller
# Ran 160 tests ... OK

python -m unittest discover -s tests
# Ran 1409 tests ... OK (skipped=3)
```

另外已执行 `py_compile`，相关文件行尾均为 LF（CR 计数为 0）。

## 5. 已知限制

- GA 当前不引入 JS AST 解析依赖，`workflow_determinism.py` 是“剥离注释与字符串/模板字面量后的文本扫描”，不是完整 AST 检查；正则字面量等极端写法可能存在近似误差。CC 的 `LKt()` 本身也不是安全沙箱。
- 该闸门的目标是阻断模型常见的非确定性写法，不替代 VM 沙箱、权限边界和工具能力控制。
- `WorkflowController.create_draft()` 仍只负责创建 draft；raw script 的强制执行点是 `WorkflowRuntime.run()`，避免在创建阶段改变既有 API 返回契约。

## 6. 下一步与待决策

- **A2 `parallel()` / A3 `pipeline()` 已完成（2026-10-12）**：按用户决定直接采用 CC 语义，不保留兼容开关；见 `docs/20261012-ga-dynamic-workflow-实施记录-A2-A3.md`。
- A4 `meta` 契约强化属于 P1，需要在不新增 JS 解析依赖的前提下决定“轻量校验”还是引入解析器。
- 云端会话相关实现（`isolation:"remote"` / `Ho()` / 云端会话）不在 GA 复刻范围；本方案不依赖也不模拟该路径。

## 7. 后续实施查阅路径

- `D:\git_codes\claudecode283_hack\docs\20261012-复刻dynamic-workflow指南.md`
- `D:\git_codes\claudecode283_hack\claudecode283\extracted\dynamic-workflow-internals.md`
- `D:\git_codes\claudecode283_hack\claudecode283\extracted\workflow-tool-description.md`
- `D:\git_codes\claudecode283_hack\claudecode283\extracted\workflow-authoring-reference.md`
- `D:\git_codes\GenericAgent\docs\20261012-ga-dynamic-workflow-复刻实施方案.md`