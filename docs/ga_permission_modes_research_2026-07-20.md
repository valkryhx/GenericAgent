# GA 执行权限等级设计调研

日期：2026-07-20  
范围：Claude Code（本地 fork）+ Codex（`codex-rs`）权限/审批机制对照，以及如何在 GenericAgent 中落地类似执行等级。

> **现行产品决策（2026-07-20）**  
> 1. **不引入 OS sandbox**（无 seatbelt / seccomp / Windows restricted token）。  
> 2. 用户可见档位对齐 Codex **三档**：`read_only` / `ask`（Ask for approval）/ `full_access`。  
> 3. **默认档：`full_access`**（兼容今天「工具全开」行为；用户可切到 ask / read_only）。  
> 4. **不做**独立的 “Approve for me” 档（classifier / guardian 留作后续可选项，不进 MVP）。  
> 5. 能力边界 = **工具分类 + 路径策略 + allow|ask|deny**，不是内核沙箱。

---

## 1. 结论摘要

| 产品 | 用户可见的「等级」 | 底层实际模型 | 命令 / 切换入口 |
|------|-------------------|--------------|-----------------|
| **Codex** | Read Only / Default / Full Access（Windows 含 Read Only；另有 Auto-review） | **二维**：`AskForApproval` × `PermissionProfile`/`SandboxPolicy` | `/permissions` 弹窗；CLI `--approval-mode` / `--sandbox` |
| **Claude Code** | Default / Accept edits / Plan / Bypass Permissions（内部还有 Don’t Ask、Auto） | **一维 mode** + **规则表**（allow/deny/ask）+ 可选 classifier | `Shift+Tab` 循环；`/permissions` 管规则；settings `defaultMode` |
| **GA 现状（P0+P1 后，2026-07-21）** | 主 agent **有**三档：`read_only` / `ask` / `full_access`（默认 full_access） | 主路径 `PermissionModePolicy` + `dispatch` 门控 + **`permission_runtime` 阻塞审批**（accept/deny） | `/permissions` 切档；ask 行内统一审批 overlay；workflow 仍 inherit→allow；`workflow_approve` 仍是脚本级审批 |

**对 GA 的建议（已收敛）**：产品三档对齐 Codex 主路径——**Read Only / Ask for approval / Full Access**；实现上用 **「策略档位 + 工具决策 allow|ask|deny」**，**不**做 OS 级 sandbox。审批 UI 复用 Ink panel/selector；门控挂在 `GenericAgentHandler.dispatch`（已有 workflow 钩子）。

### 1.1 为什么三档比四档更合适（当前）

| 方案 | 优点 | 缺点 |
|------|------|------|
| **三档（推荐）** | 与 Codex 主 UI 一致；心智清晰；实现面小；无 classifier 成本 | 没有「半自动代批」档，工作区写/shell 在 ask 下会更啰嗦 |
| 四档（+ Approve for me） | 接近 CC `acceptEdits` / Codex Auto-review | 多一档规则与测试；要么做成 acceptEdits 子集，要么引入代审模型 |

**结论**：MVP 用三档。若日后用户嫌 ask 太吵，再加 **会话级 “Accept workspace edits” 开关** 或第四档，不必现在就做。

---

## 2. Claude Code 权限机制

### 2.1 模式枚举与展示

源码：

- `claude-code-fork/src/types/permissions.ts` — `EXTERNAL_PERMISSION_MODES` / `INTERNAL_PERMISSION_MODES`
- `claude-code-fork/src/utils/permissions/PermissionMode.ts` — 标题、短标题、颜色、符号

| Mode | UI Title | 语义（能力边界） |
|------|----------|------------------|
| `default` | Default | 按规则 + 工具自身 `checkPermissions`；需要时弹审批 |
| `acceptEdits` | Accept edits | 工作区内文件编辑类操作自动放行（`filesystem.ts` 中 cwd 内 write 直接 allow） |
| `plan` | Plan Mode | 规划态；写操作/执行受限，退出 plan 再确认（`ExitPlanMode` 工具/UI） |
| `bypassPermissions` | Bypass Permissions | 几乎全部自动 allow（仍尊重 **deny 规则** 与 **safetyCheck** 敏感路径） |
| `dontAsk` | Don't Ask | 不弹窗：原应 `ask` 的变为 `deny`（非交互/CI 友好） |
| `auto`（feature 门控） | Auto mode | classifier 代审：安全则执行，风险则拒并可能换路径（非纯 YOLO） |

`dontAsk` **不在 Shift+Tab 循环里**（`getNextPermissionMode.ts` 注释：Not exposed in UI cycle yet）。

循环顺序（普通用户）：

```
default → acceptEdits → plan → [bypassPermissions if available] → [auto if available] → default
```

入口：`PromptInput.tsx` 绑定 `chat:cycleMode` → `cyclePermissionMode()`。

### 2.2 `/permissions` 命令

- `commands/permissions/index.ts`：`name: 'permissions'`，别名 `allowed-tools`
- 描述：*Manage allow & deny tool permission rules*
- UI：`PermissionRuleList`（本地 JSX 命令），管理 **规则**，不是单纯切换 mode

规则模型（`types/permissions.ts`）：

- `PermissionBehavior`: `allow` | `deny` | `ask`
- 规则来源：`userSettings` / `projectSettings` / `localSettings` / `flagSettings` / `policySettings` / `cliArg` / `command` / `session`
- 更新操作：`addRules` / `replaceRules` / `removeRules` / `setMode` / `addDirectories` / `removeDirectories`
- 持久化目标：`userSettings` | `projectSettings` | `localSettings` | `session` | `cliArg`

### 2.3 决策管线（后端）

核心：`utils/permissions/permissions.ts` → `hasPermissionsToUseTool` / 内部步骤。

典型顺序（简化）：

1. **Deny 规则**优先  
2. 工具自身 `checkPermissions`（Bash/PowerShell/Edit 等）  
3. **Ask 规则**、**safetyCheck**（`.git/`、`.claude/`、shell 配置等）— 对 bypass 也免疫  
4. **Mode 短路**  
   - `bypassPermissions`（或 plan 且「原本可 bypass」）→ allow  
   - `acceptEdits` + 路径在 working dir → 编辑 allow  
   - `dontAsk` 且结果为 ask → **改成 deny**  
   - `auto` → classifier（失败可 fail-closed / 回退提示）  
5. 工具级 always-allow 规则  
6. 其余 `passthrough` → `ask`，前端弹 PermissionRequest

相关文件：

| 路径 | 职责 |
|------|------|
| `utils/permissions/permissions.ts` | 总决策 |
| `utils/permissions/filesystem.ts` | 路径/编辑与 acceptEdits |
| `utils/permissions/pathValidation.ts` | 路径校验 |
| `utils/permissions/permissionSetup.ts` | mode 迁移、bypass 可用性 |
| `utils/permissions/permissionsLoader.ts` | 从 settings 加载规则 |
| `tools/BashTool/bashPermissions.ts` 等 | 工具专属策略 |
| `components/permissions/**` | 审批弹窗 UI |
| `components/PromptInput/PromptInputFooterLeftSide.tsx` | footer 显示当前 mode |

### 2.4 与用户四档的粗映射

| 用户文案 | Claude Code 最接近 |
|----------|-------------------|
| Read Only | `plan`（规划/限制写）+ 读工具只读；**不是** OS sandbox |
| Ask for approval | `default` |
| Approve for me | `acceptEdits`（自动批编辑）或 feature 的 `auto`（AI 代审） |
| Full Access | `bypassPermissions`（注意：仍拦 deny 规则与 safetyCheck） |

---

## 3. Codex 权限机制

### 3.1 产品预设（用户看到的）

源码：`codex-rs/utils/approval-presets/src/lib.rs` → `builtin_approval_presets()`

| id | Label | Approval | Profile | 说明（源码 description） |
|----|-------|----------|---------|---------------------------|
| `read-only` | **Read Only** | `OnRequest` | `read_only` | 可读工作区；**改文件或上网需审批** |
| `auto` | **Default** | `OnRequest` | `workspace` | 可读写工作区并跑命令；**上网或改工作区外文件需审批** |
| `full-access` | **Full Access** | `Never` | `Disabled`（无沙箱） | 可改工作区外文件并上网且**不询问**；慎用 |

TUI 弹窗标题：`Update Model Permissions`（`permission_popups.rs`）。  
Windows 才默认展示 Read Only 项（`include_read_only = cfg!(target_os = "windows")`）；非 Windows snapshot 常只见 Default + Full Access。

**Full Access 二次确认**（`open_full_access_confirmation`）：

- 文案：*Enable full access?* 强调可编辑任意文件、带网络跑命令、无审批  
- 选项：仅本 session / 持久记住 / 取消  

可选 **Auto-review**（Guardian feature）：在 Default 预设上把 `ApprovalsReviewer` 换成自动评审（近似「Approve for me」）。

### 3.2 底层二维模型

#### A. `AskForApproval`（何时问人）

`protocol/src/protocol.rs`：

| 变体 | 序列化名 | 行为 |
|------|----------|------|
| `UnlessTrusted` | `untrusted` | 仅「已知安全且只读」自动过；其余问用户 |
| `OnFailure` | （deprecated） | 沙箱内先跑，失败再升级问用户 |
| `OnRequest` | 默认 | **模型/策略决定**何时请求审批 |
| `Granular(...)` | `granular` | 细粒度开关：sandbox_approval / rules / skill / request_permissions / mcp_elicitations |
| `Never` | | 永不问用户；失败直接回模型 |

CLI：`--approval-mode` → `untrusted | on-failure | on-request | never`  
（`utils/cli/src/approval_mode_cli_arg.rs`）

#### B. `SandboxMode` / `SandboxPolicy`（能碰到什么）

`config_types.rs`：

- `read-only`（默认）
- `workspace-write`
- `danger-full-access`

`SandboxPolicy` 展开（`protocol.rs`）：

- `DangerFullAccess` — 无限制  
- `ReadOnly { network_access }`  
- `WorkspaceWrite { writable_roots, network_access, exclude_tmpdir... }`  
- `ExternalSandbox { network_access }`  

保护路径：`.git` / `.agents` / `.codex` 等 metadata 在可写 root 下仍受限（`permissions.rs`）。

CLI：`--sandbox` / `-s` → 上述三档。

### 3.3 UI / 命令

| 表面 | 行为 |
|------|------|
| `/permissions` | `slash_dispatch` → `open_permissions_popup()` |
| 状态条 / history | 显示当前权限摘要（`status/card.rs`、`history_cell/session.rs` 提到 `/permissions`） |
| 审批 overlay | `bottom_pane/approval_overlay.rs`；事件 `approval_events.rs` |
| 权限请求 | `request_permissions` 协议 + app-server 往返 |

### 3.4 与用户四档的映射

| 用户文案 | Codex |
|----------|--------|
| Read Only | preset `read-only` |
| Ask for approval | preset `Default`（`OnRequest` + workspace） |
| Approve for me | **Auto-review / Guardian**（有 feature 时）；或会话内「Always allow this pattern」类放行 |
| Full Access | preset `full-access` + 确认弹窗 |

---

## 4. GA 现状（插入点）

### 4.1 主 agent：几乎无门控

- `assets/tools_schema.json` 静态工具：`code_run`、`file_read`/`file_write`/`file_patch`、`web_scan`/`web_execute_js`、agent 协作、`ask_user`、skill 等  
- `ga.py`：`GenericAgentHandler` 直接 `do_*`；`code_run` 用 `subprocess.Popen`，**无**用户确认  
- Ink 主路径：`frontends/ink_bridge.py` 提交任务 → agent 循环，**无** per-tool 审批协议  

### 4.2 已有「权限」只服务 workflow

`workflow_permissions.py`：

| Profile | 行为 |
|---------|------|
| `inherit-current-permissions`（默认） | 一律 `allow` |
| `read_only` | 允许 `file_read`/`web_scan`/`ask_user`/`load_skill` 及名称像只读的 MCP；拒绝 write/exec |
| `restricted_mcp` | 静态工具放行；MCP 按 allow/deny 名单 |
| `explicit_approval` | 一律 `ask` |

挂载点：`GenericAgentHandler.dispatch`：

```python
if policy is not None:
    decision = self._check_workflow_permission(tool_name, args or {})
    if decision.action != 'allow':
        status = 'approval_required' if decision.action == 'ask' else 'error'
        ...
```

注意：

1. 主会话 **未** 挂 `workflow_permission_policy` → 无门控  
2. `ask` 目前主要变成 tool result 状态，**没有**完整的「阻塞工具 → Ink 弹窗 → 用户选 allow once/session/always → 恢复」闭环  
3. Workflow 的 `workflow_approve` 是 **run 级脚本审批**，不是工具级  

### 4.3 可复用的前端模式

Ink UI 已有：

- slash 命令 + panel（`/model`、`/mcp`、`/workflows`）  
- `selector` / footer panel  
- workflow status bar 快捷键  
- bridge JSONL 双向命令  

适合新增：`/permissions` 列表面板 + `permission_request` 事件 + `permission_response` 命令。

---

## 5. 产品定义：GA 三档（对齐 Codex，无 OS sandbox）

**对外三档固定文案**，对内稳定 id：

| id | 用户文案 | 默认行为 | 对标 Codex |
|----|----------|----------|------------|
| `read_only` | **Read Only** | 只读类工具 allow；写/执行/有副作用网络 **deny**（结果回模型，说明当前为 Read Only） | preset `read-only`（用策略表模拟，非 OS sandbox） |
| `ask` | **Ask for approval** | 只读 allow；写/执行/MCP 写类 **ask**（弹窗）；敏感路径强制 ask | preset `Default` 的「会问人」侧（无 sandbox 约束） |
| `full_access` | **Full Access** | 默认 allow；**进入前二次确认**；可选 session-only / remember | preset `full-access`（`Never` 问人） |

> **无 OS sandbox 时的诚实边界**  
> Codex 的 Default 在沙箱内可「先跑再失败升级」；GA 没有沙箱时，**不能**假装 workspace-write 在 OS 层被关住。  
> 因此 GA 的 `ask` = **策略上先问人**，而不是「沙箱内自动跑」。这与用户说的 Ask for approval 一致，也避免假安全感。

### 5.1 工具分类建议（三档矩阵）

| 类别 | 工具示例 | `read_only` | `ask` | `full_access` |
|------|----------|-------------|-------|---------------|
| 只读 | `file_read`, `web_scan`, `list_agents`, `read_agent_result`, `load_skill` | allow | allow | allow |
| 工作区写 | `file_write`, `file_patch`（路径 ∈ workspace） | **deny** | **ask** | allow |
| 工作区外写 | 同上，路径 ∉ workspace | **deny** | **ask**（可默认 deny 更严） | allow |
| 执行 | `code_run` | **deny** | **ask** | allow |
| 浏览器副作用 | `web_execute_js` | **deny** | **ask** | allow |
| 协作/子 agent | `spawn_agent`, `send_message`, … | **deny** 或 ask | **ask** | allow |
| 用户交互 | `ask_user` | allow | allow | allow |
| MCP 读类 | 名称像 read/list/get/… | allow | allow | allow |
| MCP 其它 | 其余 `mcp__*` | **deny** | **ask** | allow |
| 敏感路径 | `.git/**`、密钥、系统目录 | **deny** | **ask** | allow* |

\* `full_access` 仍可保留可选 **硬 deny 名单**（产品开关），默认关闭以贴合 Codex Full Access 体验。

### 5.2 三档用户心智（一句话）

```
Read Only     → 只能看，不能改、不能跑
Ask           → 能看；要改/跑先问我
Full Access   → 别问，直接干（进档前警告一次）
```

### 5.2 审批粒度（对齐两边）

用户在 ask 弹窗中可选：

| 选项 | 语义 | 持久化 |
|------|------|--------|
| Allow once | 仅本次 tool_use | 无 |
| Allow for session | 本会话同类规则 | 内存 / session transcript |
| Always allow | 写入用户配置 | `~/.genericagent/permissions.json` 或项目级 |
| Deny | 拒绝，结果回模型 | 无 |

规则字符串可先做薄版：`ToolName` 或 `ToolName(prefix*)`（学 CC 的 Bash 前缀规则，第一期可只做整工具级）。

---

## 6. 架构设计（GA）

### 6.1 模块划分

```
permission_policy.py          # 档位、工具分类、evaluate(tool, args, ctx) → allow|ask|deny
permission_store.py           # 加载/保存 rules + default mode（user/project/session）
permission_runtime.py         # 挂到 handler：阻塞 ask、等待 UI 决策、应用 once/session/always
frontends/ink_bridge.py       # 协议：permission_status / set_permission_mode / permission_request / permission_response
frontends/ink-ui/             # /permissions 面板 + 审批 overlay + footer 档位指示
```

### 6.2 评估上下文

```python
@dataclass
class PermissionContext:
    mode: Literal["read_only", "ask", "full_access"]
    workspace_roots: list[Path]          # 默认 repo / cwd
    session_allow_rules: list[str]
    persistent_allow_rules: list[str]
    persistent_deny_rules: list[str]
    full_access_confirmed: bool          # full_access 二次确认
```

### 6.3 决策伪代码

```text
evaluate(tool, args, ctx):
  if matches(deny_rules): return deny
  if matches(allow_rules session|persistent): return allow
  cat = classify(tool, args, workspace_roots)
  table = MODE_TABLE[ctx.mode][cat]
  if table == allow: return allow
  if table == deny: return deny
  return ask   # UI
```

### 6.4 与现有 workflow 权限的关系（摘要）

| 层级 | 策略 |
|------|------|
| 主 agent | 新 `PermissionContext.mode`（默认 `full_access`；用户 `/permissions` 可改） |
| workflow child | 继续 `permissionProfile` 字段；默认 `inherit-current-permissions` **应映射到主会话当前 mode**（今天是「无条件 allow」，与 full_access 默认兼容） |
| workflow 脚本审批 | 保持 `workflow_approve`，与工具权限正交 |

详细改动量评估见 **§13 Workflow 改动调研**。

### 6.5 Bridge 协议草案

```ts
// UI → bridge
{ type: 'permission_status' }
{ type: 'set_permission_mode', mode: 'read_only'|'ask'|'full_access', persist?: boolean }
{ type: 'permission_response', requestId: string, decision: 'allow_once'|'allow_session'|'always'|'deny' }

// bridge → UI
{ type: 'permission_status', mode, rulesSummary, workspaceRoots }
{ type: 'permission_request', requestId, toolName, argsPreview, reason, mode }
{ type: 'permission_mode_changed', mode }
```

执行路径：

1. `dispatch` 得 `ask` → emit `permission_request`，**阻塞**该 tool（线程 Event / future）  
2. UI 弹窗；用户选择  
3. `permission_response` → set Event → tool 继续或返回 denial  
4. 任务 `abort`/`/stop` 时必须 cancel 所有 pending permission wait  

### 6.6 Ink UI

| 元素 | 设计 |
|------|------|
| `/permissions` | **三选一**列表（学 Codex `Update Model Permissions`），显示当前项、说明、Full Access 警告 |
| Shift+Tab（可选） | 循环 `ask ↔ read_only`；**不**默认循环进 full_access（需 `/permissions` 显式选 + 确认） |
| Footer / activity | 短标签：`RO` / `Ask` / `FULL`（颜色：灰 / 默认 / 红） |
| 审批弹窗 | 仅 `ask` 档需要；盖在输入区上方：工具名、参数摘要、Allow once / session / always / Deny |
| Full Access 确认 | 独立确认页：Session only / Remember / Cancel |

### 6.7 配置持久化

建议路径：

- 用户级：`~/.genericagent/permissions.json`  
- 项目级（可选）：`.genericagent/permissions.json` 或写进现有项目配置  

示例：

```json
{
  "defaultMode": "ask",
  "fullAccessRemember": false,
  "allow": ["file_read", "web_scan"],
  "deny": [],
  "workspaceRoots": []
}
```

CLI / 环境变量（后续）：

- `GA_PERMISSION_MODE=read_only|ask|full_access`  
- 启动 flag 覆盖 session default  

---

## 7. 分阶段落地

### Phase 0 — 观测（0.5–1 天）

- 在 `dispatch` 增加 **shadow 日志**：若启用某 mode 会如何决策（不拦截）  
- 统计主会话工具调用分布，校准分类表  

### Phase 1 — MVP 门控 + 三档切换（优先）

1. `permission_policy.py` + 三档矩阵单测  
2. `GenericAgentHandler.dispatch` **始终**走 policy（主 agent）  
3. Ink：`/permissions` 三选一切换（先不做复杂规则编辑）  
4. `ask`：**阻塞审批**（推荐）；否则 ask 体验塌成 deny  
5. Full Access 二次确认  
6. 单测：policy 表 + bridge 协议 + UI panel  

**兼容策略（已拍板）**：默认 **`full_access`**（行为≈今天无门控）。用户主动切到 `ask` / `read_only` 才变严。默认改 `ask` 若要做，另开产品决策，不在 MVP 范围。

**P0 实际交付（2026-07-21）**：上表 1–3、5–6 已落地。  
**P1 实际交付（2026-07-21）**：第 4 点（ask 阻塞审批 + 统一 accept/deny UI）已落地；live 验证见 §14.4。完整「已交付 / 剩余大件」见 **§14**。

### Phase 2 — 规则与会话记忆

- Allow once / session / always  
- `/permissions` 规则列表（薄版 CC PermissionRuleList）  
- transcript 记录 permission 事件便于 resume  

### Phase 3 — 继承与 workflow 对齐（见 §13）

- `inherit-current-permissions` 真继承主会话 mode（**小～中**，建议紧跟 P0）  
- child agent / spawn_agent 继承或降级  
- workflow 内 `ask` 阻塞审批（**大**，单独切片，勿绑 MVP）  
- MCP 名单与 `restricted_mcp` 合并  

### Phase 4 — 非目标 / 远期

- OS sandbox：**MVP 明确不做**；仅作远期可选项记录  
- 参考 Codex `SandboxPolicy` 仅在未来有明确需求时评估  

---

## 8. 默认值与安全建议

| 场景 | 建议默认 |
|------|----------|
| 交互 Ink 日常（MVP） | **`full_access`**（已拍板，兼容现状） |
| 用户手动收紧 | `ask` 或 `read_only` |
| 自动化 / CI | 可 env 强制 `read_only` 或 `ask` 且 ask→deny（无 UI） |
| 子 agent / workflow inherit | 继承 parent 当前 mode（默认即 full_access） |

硬性建议：

1. **deny 优先于 mode**  
2. Full Access **不得** Shift+Tab 误触  
3. 密钥路径 / `.git/hooks` 等 safetyCheck 在 full_access 仍可配置强制 ask  
4. permission wait 必须可被 `/stop` 取消  

---

## 9. 关键文件索引

### Claude Code

- `src/types/permissions.ts`
- `src/utils/permissions/PermissionMode.ts`
- `src/utils/permissions/permissions.ts`
- `src/utils/permissions/getNextPermissionMode.ts`
- `src/utils/permissions/filesystem.ts`
- `src/commands/permissions/*`
- `src/components/permissions/**`
- `src/components/PromptInput/PromptInput.tsx`（cycle mode）

### Codex

- `codex-rs/utils/approval-presets/src/lib.rs`
- `codex-rs/utils/cli/src/approval_mode_cli_arg.rs`
- `codex-rs/utils/cli/src/sandbox_mode_cli_arg.rs`
- `codex-rs/protocol/src/protocol.rs`（`AskForApproval`, `SandboxPolicy`）
- `codex-rs/protocol/src/config_types.rs`（`SandboxMode`）
- `codex-rs/protocol/src/permissions.rs`
- `codex-rs/tui/src/chatwidget/permission_popups.rs`
- `codex-rs/tui/src/chatwidget/slash_dispatch.rs`
- `codex-rs/tui/src/bottom_pane/approval_overlay.rs`

### GA

- `workflow_permissions.py` — 现有 profile 引擎  
- `ga.py` — `GenericAgentHandler.dispatch` 门控挂点  
- `agent_loop.py` — `BaseHandler.dispatch`  
- `assets/tools_schema.json` — 工具清单  
- `frontends/ink_bridge.py` / `frontends/ink-ui/src/protocol.ts` — 协议扩展点  
- `frontends/ink-ui/src/slashCommands.ts` — 注册 `/permissions`  

---

## 10. 开放问题（实现前可再拍板）

1. ~~默认 mode~~ → **已拍板：`full_access`**  
2. `code_run` 在 `ask` 下是否要做「可信只读命令自动过」（可选，非必须）？  
3. 项目级规则是否进 git（可共享）还是仅 user 级？  
4. MCP 未知工具：默认 `ask` 还是 `deny`？（建议在 `ask` 档用 ask，在 `read_only` 用 deny）  
5. 与 `ask_user` 是否共用同一套 UI 通道？  
6. 日后是否加「Accept workspace edits」子开关（不新增第四主档）？  
7. 阶段 B 中 parent=`ask` 时，workflow child 在无审批 UI 前：降级 `read_only` 还是仍 allow-all？（建议 **降级 read_only** 更安全，或文档写明「ask 对 workflow 暂不收紧」）  

---

## 11. 建议的下一步实现切片

**Slice P0（可单独 PR，workflow 几乎不动）** — **主路径已交付（2026-07-21）**；清单与缺口见 **§14**

1. `permission_policy.py` + 单测（**三档** × 工具分类矩阵） ✅  
2. 主 handler 挂 policy；**默认 `full_access`** ✅  
3. bridge + Ink `/permissions` 三选一（footer 指示可选/未强调） ✅  
4. `read_only` 对写/exec **deny with message**（不依赖审批 UI 也能交付） ✅  
5. （可选同 PR）**P0b** inherit 真继承 + run 快照 `parentPermissionMode` ❌ 未做  

**Slice P1** — **已交付主路径（2026-07-21）**（大件 #1；验证见 §14.4）

6. `permission_request` 阻塞审批 UI（主 agent `ask` 档真正可用） ❌  
7. 从其它档切入 Full Access 的确认（**默认启动 full_access 不弹**） ✅ 已随 P0  
8. session allow 列表 ❌  

**Slice P2** — **未做**（大件 #2 / #3）

9. 持久规则 / 档位持久化 ❌  
10. workflow 内 ask 审批（§13 阶段 C，大） ❌  
 

---

## 12. 决策记录：三档 + 默认 full_access + 无 OS sandbox（采纳）

**决定**：

1. GA 权限 MVP 采用 Codex 式 **三档**（`read_only` / `ask` / `full_access`）。  
2. **默认 `full_access`**（与当前「工具直接执行」兼容）。  
3. **不做** OS sandbox；**不做** Approve-for-me 主档。  

**理由**：

1. **对齐 Codex 主 UI**——用户看到的就是 Read Only / Ask for approval / Full Access。  
2. **默认 full_access**——现有用户/测试/workflow 默认行为不变；收紧是显式选择。  
3. **实现面可控**——复用 `dispatch` 钩子与 `workflow_permissions` 的 allow/deny/ask 雏形。  
4. **无假安全感**——`ask` = 先问再跑，不假装有 OS 沙箱。  
5. **Full Access 二次确认**——从其它档切入 full_access 时警告；**会话启动默认 full_access 不必每次弹窗**（否则破坏「默认不打扰」）。  

**非目标（MVP）**：

- OS / 容器 sandbox  
- AI classifier 自动审批  
- 完整 CC 式 `/permissions` 规则编辑器  
- 强制改 workflow 默认 profile 名（见 §13）  

---

## 13. Workflow 改动调研：大不大？

### 13.1 现状链路（已具备门控骨架）

```
WorkflowRun.permission_profile  (默认 "inherit-current-permissions")
        │
        ▼
AgentScheduler.register_agent  → job.metadata["permissionProfile" | "permissionPolicyVersion"]
        │
        ▼
WorkflowChildAgentRunner._build_handler
        │  handler.workflow_permission_policy = ToolPermissionPolicy(profile=...)
        │  handler.workflow_permission_context / event_callback
        ▼
GenericAgentHandler.dispatch
        │  policy.evaluate → allow | deny | ask
        ▼
do_* / MCP
```

关键文件：

| 文件 | 已有能力 |
|------|----------|
| `workflow_models.py` | `DEFAULT_PERMISSION_PROFILE = "inherit-current-permissions"`；run 序列化 `permissionProfile` |
| `workflow_permissions.py` | `ToolPermissionPolicy`：`inherit`→allow；`read_only`；`restricted_mcp`；`explicit_approval`→ask |
| `workflow_child_agent.py` | 创建 handler 时注入 policy + 审计事件 |
| `workflow_scheduler.py` | 把 run 的 profile 写入 job metadata；permission 事件落盘 |
| `workflow_store.py` | `append_permission_event`；toolSummary allowed/denied |
| `workflow_runtime.py` | cache key 含 permission 字段 |
| `ga.py` `dispatch` | policy 非空才评估 |
| Ink `workflowPanel.ts` | 只读展示 `Permission: <profile>` |
| 测试 | `tests/test_workflow_permissions.py`、`test_workflow_permission_inheritance_e2e.py` 等 |

**重要缺口**（测试已写明）：

- `inherit-current` = **无条件 allow**，并非读取主 agent 档位  
- `explicit_approval` → `ask` **没有交互 UI**，只返回 `approval_required` / deny 事件  

### 13.2 改动量总表

| 范围 | 改动大小 | 说明 |
|------|----------|------|
| **P0 只做主 agent 三档，workflow 默认不动** | **小** | workflow 继续 inherit→allow；与默认 full_access **行为一致**，现有 E2E 基本不用改 |
| **P0.5 inherit 真继承主会话 mode** | **小～中** | 映射表 + child 注入时读 parent mode；默认 full_access 时仍≈今天 |
| **P1 workflow 子 agent 支持 `ask` 阻塞审批** | **大** | child 多线程/多 job；要跨 bridge 排队审批、超时、取消、与 workflow run 生命周期绑定 |
| **改默认 profile 名 / 废弃旧 profile** | **中** | 动 models、cache key、大量测试与文档；**不建议 MVP 做** |
| **Ink 展示三档名** | **小** | panel 把 `inherit-current-permissions` 显示成「继承：Full Access」等 |
| **planner 的 read_only 语义** | **小** | `workflow_planner.py` 已有 `read_write_mode = "read_only"` 字符串，与工具 policy 是另一概念，勿混淆；可后续统一 |

### 13.3 推荐策略：分阶段，workflow 先「搭便车」

#### 阶段 A — 主 agent only（**workflow 改动 ≈ 0～极小**）

- 新模块服务主会话；`GenericAgentHandler` 主路径挂 `permission_policy`  
- **不改** `DEFAULT_PERMISSION_PROFILE`  
- child 仍 `ToolPermissionPolicy(profile=inherit-current)` → allow  
- 效果：主 Ink 会话有三档；**workflow 行为与今天完全一致**  
- 风险：用户主会话切到 `read_only`，workflow child 仍可写——文档声明「workflow 默认独立 / 仍全开」或尽快做阶段 B  

**预估**：workflow 相关 **0～0.5 人日**（最多改展示文案）。

#### 阶段 B — `inherit-current` 真继承（**小改动，强烈建议紧跟 P0**）

映射（内部 mode → 现有/扩展 profile 行为）：

| 主会话 mode | inherit 解析结果 |
|-------------|------------------|
| `full_access` | allow-all（与今天 inherit 相同） |
| `read_only` | 复用现有 `read_only` 评估 |
| `ask` | 见下：阶段 B 可先 **降级为 read_only 或 deny-on-write**；完整 ask 放阶段 C |

最小实现：

```python
# workflow_child_agent._build_handler 附近
profile = job.metadata.get("permissionProfile") or DEFAULT_PERMISSION_PROFILE
if profile == INHERIT_CURRENT_PERMISSIONS:
    parent_mode = resolve_parent_session_mode()  # full_access | ask | read_only
    effective = map_mode_to_policy(parent_mode)  # full_access→allow-all; read_only→read_only; ask→?
else:
    effective = profile  # 显式 read_only / restricted_mcp / ...
handler.workflow_permission_policy = ToolPermissionPolicy(profile=effective, ...)
```

`resolve_parent_session_mode()` 来源建议：

- Ink bridge / `GenericAgent` 上挂 `session_permission_mode`  
- 创建 workflow run 时写入 `run.metadata["parentPermissionMode"]`（**快照**），避免 run 中途用户改档导致 child 行为漂移  

显式 `run.permission_profile = "read_only"` 仍优先生效（覆盖 inherit）。

**预估**：**1～2 人日**（含单测扩展 inheritance e2e；默认 full_access 路径旧测试应仍绿）。

#### 阶段 C — workflow 内 `ask` 真审批（**大改动，单独切片**）

难点：

1. Child 在 **后台线程** 跑，`dispatch` 同步 generator；`ask` 需 future/Event 阻塞  
2. **多 job 并行** 时多个 `permission_request` 要排队或按 jobId 区分  
3. UI 需展示「workflow job X 请求执行 code_run」  
4. run cancel / timeout 必须取消 pending permission  
5. 无 Ink 的 headless workflow：`ask` 应 fail-closed（deny）或 CLI 回调  

**预估**：**3～6 人日+**，且依赖主会话 P1 审批协议已稳。  
MVP **不要**和三档切换绑在同一 PR。

### 13.4 旧 profile 与三档如何共存

| 现有 profile | 与三档关系 | MVP 建议 |
|--------------|------------|----------|
| `inherit-current-permissions` | → 映射主会话 mode | **保留名字**；修语义（阶段 B） |
| `read_only` | = 三档 `read_only` | 保留；实现可委托同一 evaluate |
| `explicit_approval` | 类似全程 ask | 保留；待阶段 C 才有 UI |
| `restricted_mcp` | 正交（MCP 名单） | 保留；可与任意 mode 叠加 options |

**不要**在 MVP 把 `DEFAULT_PERMISSION_PROFILE` 改成 `full_access` 字符串——会动 cache key、journal、文档与一堆断言。  
语义上：`inherit + parent full_access` ≡ full_access 即可。

### 13.5 测试影响

| 套件 | 阶段 A | 阶段 B | 阶段 C |
|------|--------|--------|--------|
| `test_workflow_permissions.py` | 不动 | 可能加 inherit 映射用例 | 加 ask 阻塞 mock |
| `test_workflow_permission_inheritance_e2e.py` | 不动（仍 expect allow） | **改/增**：parent read_only 时 child 拒写 | 大改 |
| `test_workflow_runtime` cache key | 不动 | 若 metadata 增加 parentMode 需确认 cache 是否纳入 | — |
| `test_ink_bridge` workflow | 不动 | 可选断言 metadata 快照 | 审批协议测试 |
| 主 agent 新测 | 新增 | — | — |

### 13.6 结论（直接回答「改动大不大」）

| 问题 | 答案 |
|------|------|
| 只上主会话三档 + 默认 full_access，workflow 怎么办？ | **几乎不用改**，行为兼容。 |
| 想让「主会话切到 Read Only 时 workflow 也收紧」？ | **小改**（阶段 B 真 inherit + run 快照），约 1～2 人日。 |
| 想让 workflow 工具也弹 Ask 审批？ | **大改**（阶段 C），单独排期，勿阻塞 MVP。 |
| 会不会推翻现有 workflow_approve？ | **不会**。脚本级审批与工具级权限正交。 |

**推荐落地顺序**：

```
P0  主 agent 三档（默认 full_access）+ /permissions UI          ← 已交付（见 §14）
P0b inherit 真继承 + run 快照 parentMode     ← workflow 小改，强烈建议同里程碑
P1  主 agent ask 阻塞审批 UI                 ← 已交付主路径 + 三级 live 验证（§14.4）
P2  workflow ask 审批 / 规则持久化 / 可选收紧默认  ← 未做（大件 #2/#3）
```

---

## 14. 落地状态与剩余大件（2026-07-21 更新）

> 本节把「已经合进仓库的」和「明确没做、须单独推进的」写死，避免调研正文里 Phase/Slice 与真实交付混淆。  
> 代码入口：`permission_policy.py`、`permission_runtime.py`、`ga.py` `dispatch`、`agentmain.set_permission_mode`、`frontends/ink_bridge.py`、`frontends/ink-ui/src/permissionPanel.ts`、`frontends/ink-ui/src/approvalPanel.ts`。  
> 相关提交示例：`feat(permission): 三档权限（read_only/ask/full_access，默认 full_access）`；P1 阻塞审批实现见同日变更（尚未强制要求单 commit）。

### 14.1 P0 已交付（主会话纵切）

| 项 | 状态 | 说明 |
|----|------|------|
| 三档引擎 | ✅ | `read_only` / `ask` / `full_access`；默认 **`full_access`** |
| 工具读/写分类 + evaluate | ✅ | `allow` / `deny` / `ask`；未知静态工具按 mutating |
| 主 handler 门控 | ✅ | `permission_mode_policy`；workflow policy 优先 |
| `read_only` 写/执行 deny | ✅ | 工具 body 不执行；结果 `status=error` + permission deny |
| `ask` 写/执行（P0 时） | ✅→被 P1 替代 | 旧半交付 `approval_required` **已废弃为「等人」语义**；见 §14.4 |
| `full_access` 全 allow | ✅ | 与历史「工具全开」兼容 |
| 运行中切档 | ✅ | `set_permission_mode` 同步 live handler |
| Ink `/permissions` 三选一 | ✅ | 列表 + 切到 Full Access 二次确认 |
| bridge 协议（切档） | ✅ | `permission_status` / `set_permission_mode`（session-only） |
| 非 full_access 系统提示 hint | ✅ | accept/deny 文案；避免模型空转重试被拒工具 |
| OS sandbox | ❌ 非目标 | 产品决策明确不做 |

### 14.2 P1 已交付（ask 行内阻塞审批，2026-07-21）

| 项 | 状态 | 说明 |
|----|------|------|
| `permission_runtime` | ✅ | Future 挂起 + `requestId`；`resolve` / `cancel_all`；未知 decision→deny |
| `dispatch` 阻塞路径 | ✅ | ask → emit `permission_request` → wait → accept 执行 / deny 不执行 |
| bridge 协议（审批） | ✅ | 事件 `permission_request` / `permission_request_settled`；命令 `permission_response`（仅 `accept`\|`deny`） |
| Ink 统一审批 overlay | ✅ | `approvalPanel.ts` 二选一；Esc=deny；审批优先于切档面板 |
| 无 UI / headless | ✅ | fail-closed **deny**（无 emit 不放行） |
| `/stop` 与 pending | ✅ | `abort` → `cancel_all()` 按 deny 解开 pending 并停轮 |
| session/always / 按工具特化页 | ❌ 明确不做 | 仍属后续可选；不进 P1 |

细节与产品拍板：`docs/ga_permission_ask_blocking_approval_research_2026-07-21.md`（§0.1 / §0.2 / §12 验证进度）。

### 14.3 仍未做的大件（原三大件 #2 / #3 + P0b）

| # | 大件 | 当前行为（缺口） | 目标行为 | 体量 / 依赖 | 建议切片 |
|---|------|------------------|----------|------------|----------|
| **1** | **ask 档行内阻塞式审批 UI** | — | — | **已交付主路径**（§14.2 / §14.4） | **P1 ✅** |
| **2** | **档位（与规则）持久化** | `persist` 预留但不写盘；重启回代码默认 **`full_access`** | ~~写盘记住上次档~~ → **产品取消（2026-07-21）**：不要写回、不记住 UI 切档；默认最高权限已由 `DEFAULT_PERMISSION_MODE=full_access` 满足。调研留底：`docs/ga_permission_mode_persistence_research_2026-07-21.md` | — | **取消 / 非目标** |
| **3** | **workflow 子 agent 内 ask 审批** | child 默认 **full_access**（inherit→allow）；无 child 人审 UI | **产品拍板（2026-07-21）：不做**——workflow worker 必须可无人值守跑完；人审只在主会话。详见 `docs/ga_workflow_permission_inherit_research_2026-07-21.md` §0.1 | — | **取消 / 非目标** |

**刻意区分（避免误读）**：

- **已有**：`/permissions` 切**档位**、进 Full Access 的**二次确认** —— 这是 mode 切换 UI，**不是**单次工具审批。  
- **已有**：ask 行内 **accept/deny** overlay —— 这是单次 tool call 审批（P1）。  
- **已有**：`workflow_approve` —— **run 级脚本**审批，与工具级 `permission_request` **正交**。  
- **ask 现语义（P1 后）**：主会话 ask ≈「写/执行前弹统一 accept/deny；同意才执行，拒绝/无 UI/`/stop` 均不执行」。

### 14.4 验证进度（2026-07-21）

| 层 | 手段 | 脚本 / 命令 | 结果 |
|----|------|-------------|------|
| 单测 | `unittest` permission / bridge / agentmain / ink approvalPanel | `python -m unittest …`；`npm run test`（ink-ui） | 相关用例已对齐阻塞语义 |
| Live L1 | 同进程 `runtime.resolve` + 真模型 grok-4.5 | `PYTHONPATH=. python temp/_live_ask_approval_grok.py` | **PASSED** deny 不落盘 / accept 落盘 |
| Live L2 | 同进程 `bridge.permission_response`（JSONL 同款命令，不直触 Future） | `PYTHONPATH=. python temp/_live_ask_approval_bridge_grok.py` | **PASSED** |
| Live L3 | **真子进程** `ink_bridge.py` stdin/stdout JSONL 管道 | `PYTHONPATH=. python temp/_live_ask_approval_subprocess_jsonl_grok.py` | **PASSED**（最接近 Ink） |
| 真 Ink 键盘 E2E | 人工 / 虚拟终端键入 | — | **未做**（非阻断；协议层 L3 已覆盖） |

L3 要点：父进程只经 JSONL 发 `model_switch` / `set_permission_mode ask` / `submit` / `permission_response`；子进程 emit `permission_request` → fake-ink 回包；断言 deny 文件不存在、accept 内容为 `subproc-ask-accept`。

### 14.5 后续推进原则

1. **三大件现状（2026-07-21）**：#1 ask 阻塞审批 **已交付**；#2 档位持久化 **取消**（默认 full_access 已够，不写回/不记住）；#3 workflow child 人审 **取消**（child = full_access）。  
2. 权限主路径可视为 **收口**：主会话三档 + P1 审批；默认 full_access；workflow 全权不弹窗。可选后续：workflow 启动提示、真 Ink 键盘 E2E、显式 read_only run 文档。  
   - Workflow 拍板：`docs/ga_workflow_permission_inherit_research_2026-07-21.md`  
   - 持久化调研（已取消实现）：`docs/ga_permission_mode_persistence_research_2026-07-21.md`  
3. 主会话无 UI / headless：`ask` 必须 fail-closed（deny）。**workflow child** 不走 ask 人审。  
4. 实现承诺仍以具体 PR 为准；本节固定 **范围边界与验证证据**。

---

*本文档为调研与设计记录，不包含实现承诺。实现时以本仓库 `AGENTS.md` / 安全规则为准：不落地恶意 payload，门控以防御与可控自动化为目的。*
