# GA workflow 受控 subprocess 优化：实施记录（阶段 4 / 缺陷 B·E）

- 日期：2026-10-13
- 状态：**已实施，全量测试通过**
- 关联缺陷：B（guard 全禁 subprocess）、E（verify 被迫 runpy 同进程执行）
- 上游证据仓库：`D:\git_codes\claudecode283_hack`（CC 2.1.283 源码 + 剖析文档）
- 一句话：把 workflow workspace guard 的「全禁 subprocess」改为 CC 的模型——子代理**可以**在 workspace 内运行命令，
  并用可选的 per-agent `bashCommandClamp` 把可运行的命令**形式**收窄（fail-closed）。

---

## 1. 背景（缺陷 B / E）

- **B**：`workflow_workspace_guard.py` 把 `subprocess.Popen/run/call/check_call/check_output` 全部替换成
  `deny_process`，任何子进程抛 `PermissionError: workflow code_run subprocess is disabled by workspace guard`。
  子代理无法 `python x.py` / `pytest` / `git`，只能退化成 `runpy` 同进程执行。
- **E**：于是 verify 子代理被迫 `runpy.run_path(..., run_name="__main__")` 同进程执行——只对「纯自包含、
  不调用外部命令」的脚本成立，验证强度打折。
- 缺陷文档明确判定 B 是「设计缺陷」：**偏离 Claude Code**（CC 的 workflow 子代理可在 workspace 内自由跑命令）。

---

## 2. CC 的做法（源码证据）

CC 不「全禁」，而是用 **`bashCommandClamp`**（per-spawn 的权限规则）把子代理的 shell 收窄到固定命令形式：

| 证据 | 内容 |
| --- | --- |
| `claudecode283/modules/m0869_0xd12567a.js:667`（reconstructed/deminified 同址） | `agent()` 允许的 opts：`["schema","model","effort","isolation","agentType","disallowedTools","bashCommandClamp"]` |
| 同文件 `:1771-1782` | `bashCommandClamp` 形状校验：必须是**非空字符串数组**（tool 名大小写敏感、内容无首尾空白），否则 **refuse the spawn**（fail-closed） |
| 同文件 `:2073-2075` | clamp 无法生效（toolAlias / 子代理 tool pool 里没有 Bash）时**拒绝 spawn** |
| `reconstructed/deminified/m0357_0xc63ba04.js:110968-112051` | 运行时：命令命中 clamp 规则才放行；**不可验证**（替换/控制流/无法分解的复合命令）→ 拒绝；权限检查崩溃 → 拒绝（fail-closed） |
| `reconstructed/deminified/m0052_0xbfaae5d.js:189-190` | 文案：`"bashCommandClamp: no clamp rule matches this command"` / `"bashCommandClamp fail-closed: permission check crashed"` |
| `docs/20261012-复刻dynamic-workflow指南.md:427` | `bashCommandClamp` = `string[]`，`Bash(<前缀>)` 形式的权限规则，**收窄 Bash** |

**结论**：CC 的默认形态是「子代理能跑命令」（受常规权限约束）；需要收紧时用 `bashCommandClamp`
把可运行形式钉死，且**任何无法判定/异常一律拒绝**（fail-closed）。

---

## 3. 对齐设计

guard 的子进程策略改为三分支（`workflow_workspace_guard._build_subprocess_check`）：

| 场景 | 行为 |
| --- | --- |
| 默认（未设 clamp、未开 strict） | **允许**子进程，但：① cwd 被限定在 workspace 内；② 命中灾难性命令 denylist（`rm -rf`、`del /f /s`、`format`、`mkfs`、`diskpart`、`shutdown`、`dd of=/dev/`、fork bomb、`Remove-Item -Recurse -Force` 等）→ 拒绝。 |
| 设了 `bashCommandClamp` | 仅允许命令前导 token 匹配某条规则的形式；带 shell 结构（`;` `&&` `\|` `` ` `` `$(` `>` `<` 等）无法验证 → 拒绝（fail-closed）；不匹配任何规则 → 拒绝。 |
| `GA_WORKFLOW_GUARD_STRICT=1` | 恢复旧行为：**全禁**子进程（给想更严的 run 用）。 |

路径守卫（`builtins.open` / `io.open` / `os.open` / `os.remove` / `os.mkdir` / `os.rename` / `shutil.rmtree` …）
**保持不变**，继续把子代理自身的文件操作限制在 workspace 内。

> **安全取舍（重要）**：允许子进程后，子进程的文件访问**不再**受 in-process 路径守卫约束（进程内 monkeypatch
> 看不到子进程的系统调用）。因此 guard 是「尽力而为的引导」，**不是安全边界**——一个能跑 Python 的子代理本就能
> 通过 monkeypatch 看不到的路径触达 OS。这与 CC 一致（CC 也把 VM 沙箱定位为「不是安全边界」，
> 见 `docs/20261012-复刻dynamic-workflow指南.md:1222`）。需要更严时用 `bashCommandClamp` 或 `GA_WORKFLOW_GUARD_STRICT=1`。

---

## 4. 实施坐标

| 位置 | 改动 |
| --- | --- |
| `workflow_workspace_guard.py` | 重写：新增 `_DANGEROUS_COMMAND_PATTERNS` / `_SHELL_STRUCTURE` / `_normalize_command` / `_matches_rule` / `_build_subprocess_check`；`install()` 把 `subprocess.Popen.__init__` 换成带策略校验的包装（覆盖 `run/call/check_call/check_output`，因为它们都经 `Popen`），并包装 `os.system` / `os.popen`。 |
| `ga.py` `code_run(..., bash_clamp=None)` | 新增参数；`workspace_root` 时把 clamp 写进 `GA_WORKFLOW_BASH_CLAMP` 环境变量。 |
| `ga.py` `GenericAgentHandler.do_code_run` | 透传 `bash_clamp=getattr(self, "workflow_bash_clamp", None)`。 |
| `workflow_child_agent.py` `_build_handler` | 从 `job.metadata["options"]["bashCommandClamp"]` 取字符串数组，挂到 `handler.workflow_bash_clamp`。 |
| `workflow_runtime.py` `_handle_rpc`（agent 分支） | 校验 `bashCommandClamp`：必须是非空字符串数组，否则 `RuntimeError("... Refusing the spawn rather than running it un-clamped.")`（对齐 CC fail-closed）。 |
| `skills/workflow-authoring/SKILL.md` | 文档化 `bashCommandClamp`（§4 选项表 / 边界修订）。 |

> `code_run_header.py` 无需改：它已 `install(root)`；策略从环境变量读。
> `os.system`/`os.popen` 之前**根本没被拦**（旧 guard 只拦 `subprocess.*`），所以旧 guard 其实已被 `os.system` 绕过——
> 这也印证「全禁 subprocess」从来不是真正的安全边界。本次把它们一并纳入策略。

---

## 5. 验收与测试

`tests/test_workflow_workspace_guard.py`（重写，8 例全过）：

- 路径：workspace 内写入允许 / 外部写入拒绝 / `..` 穿越拒绝。
- 子进程：默认**允许**（`python -c print(1)` 成功）；灾难命令拒绝（`rm -rf /`）；`GA_WORKFLOW_GUARD_STRICT=1` 全禁。
- clamp：`["nonexistent-program-xyz"]` 放行该形式（后续以非 PermissionError 失败）、放行其它形式则拒绝；
  带 shell 结构（`echo hi && echo bye`, shell=True）拒绝。

`tests/test_workflow_runtime.py`：`test_runtime_accepts_bash_command_clamp`（透传到 job options）、
`test_runtime_rejects_malformed_bash_command_clamp`（`"pytest"` / `[""]` / `[" pytest "]` / `[1]` / 对象 → 抛错）。

`tests/test_workflow_child_agent.py`：`test_build_handler_carries_bash_command_clamp` /
`test_build_handler_without_clamp_leaves_it_unset`。

回归：`python -m unittest discover -s tests -p "test_*.py"` → **1340 tests OK (skipped=3)**。

---

## 6. 未做 / 后续

1. **verify 的「真实运行」契约**：现在 verify 子代理可以用 `code_run` 跑 `subprocess`（默认允许），
   不再需要 `runpy` 妥协；但「如何声明/校验一次真实执行」仍由工作流作者与 `requiredToolEvidence` 决定，未新增强制项。
2. **clamp 规则的语法**：GA 用「命令前缀字符串数组」（如 `["python","pytest","git status"]`），
   未采用 CC 的 `Bash(<前缀>)` 包裹语法（GA 无独立 Bash 工具，code_run 即执行面）。
3. **OS 级隔离**（容器/沙箱）未做；如需真正安全边界，应在宿主侧用 OS 隔离，而非 in-process guard。
4. **CC 云端会话**：不涉及，**复刻时暂时不需要参考 claudecode 云端会话的实现**。

---

## 7. 相关文档与证据

- 缺陷清点：`docs/20261012-ga-workflow-实测缺陷清点-待分析.md`（B/E）
- 阶段 1-3 记录：`docs/20261013-ga-workflow-多agent冲突-实施记录-阶段1-3.md`
- CC 侧：`claudecode283/modules/m0869_0xd12567a.js`、`reconstructed/deminified/m0357_0xc63ba04.js`、
  `reconstructed/deminified/m0052_0xbfaae5d.js`、`docs/20261012-复刻dynamic-workflow指南.md:427/1222`
- GA 侧：`workflow_workspace_guard.py`、`workflow_path_acl.py`、`assets/code_run_header.py`、`ga.py`
