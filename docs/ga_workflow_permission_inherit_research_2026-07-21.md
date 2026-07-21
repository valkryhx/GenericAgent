# GA Workflow / Subagent 权限继承调研（P0b）

日期：2026-07-21  
范围：对照 **Claude Code** 与 **Codex** 的 subagent / 多 agent 权限控制与继承；**并收敛 GA 产品拍板：workflow 子 agent 默认 full_access、不做人审**。  
**不含**：档位持久化（大件 #2 / P2a）。  
**大件 #3（workflow 内 ask 阻塞 UI）**：按 §0.1 产品拍板 **不做 / 降级为非目标**（见下文）。  
相关总文档：`docs/ga_permission_modes_research_2026-07-20.md` §13–§14。

| 产品 | 本地路径 |
|------|----------|
| Claude Code | `D:\git_codes\claude-reviews-claude\claude-code-fork\src` |
| Codex | `D:\git_codes\codex\codex-rs` |
| GA | `D:\git_codes\GenericAgent` |

---

## 0. 一句话结论

| 产品 | 子 agent 权限从哪来 | 相对 parent 能否更松 | 无 UI / 后台 |
|------|---------------------|----------------------|--------------|
| **Codex** | **先克隆 parent 有效 config**，再叠 role 层；**最后用 parent turn 的 live `approval_policy` / `permission_profile` / sandbox / cwd 再刷一遍** | 对 **审批/沙箱/profile** 基本 **不能更松**：runtime 字段以 **parent turn 为准** 压过 role | 与主会话同一 approval 通道（同进程 AgentControl）；失败默认 Abort/Deny 体系 |
| **Claude Code** | 默认 **继承 parent `toolPermissionContext`**；agent 定义可设 `permissionMode` / `disallowedTools` / `allowedTools` | **不能**用 agent mode 压过 parent 的 `bypassPermissions` / `acceptEdits` /（feature）`auto`；可在更严 parent 下再收紧 | **async / headless**：`shouldAvoidPermissionPrompts` → 先 hook，否则 **deny**（fail-closed）；`bubble` 可把提示冒泡到 parent 终端 |
| **GA 现状** | workflow child：`ToolPermissionPolicy(profile=run.permission_profile)`；默认 `inherit-current-permissions` | 名写 inherit，实现是 **无条件 allow** | workflow ask 仍非阻塞；主会话 P1 已有阻塞，**child 未接** |

**对 GA 的含义（调研层）**：Codex/CC 倾向 **child 不比 parent 更松**，且后台无 UI 时 **deny 而非弹窗**。  
**对 GA 的含义（产品层，见 §0.1）**：用户明确选择 **workflow 子 agent = full_access、永不阻塞等人**——这是 **体验优先** 的拍板，与「严格继承 parent」不同；实现上应 **写死默认 allow-all + 禁止 child 人审**，而不是做假 inherit 或半套 ask UI。

### 0.1 产品拍板（2026-07-21）：workflow subagent = full_access，不做人审

> **用户判断**：workflow 里的 subagent **肯定不能**做成「每个写/执行都等人批」；否则多 job / 并行 / 长脚本会完全不可用。  
> **拍板**：workflow 子 agent **就是 full_access（工具放行）**；**不**做 child 行内 accept/deny。

| 项 | 决策 |
|----|------|
| 默认能力 | **full_access**（写/执行/MCP 按主会话工具表放行，不弹审批） |
| 人审 UI | **禁止**作为 workflow child 路径；大件 #3 从「必做」改为 **非目标 / 取消** |
| 与主会话三档关系 | 主会话 `read_only` / `ask` **只约束主 agent**；**默认不**把主会话 ask 传导成 child 弹窗或 child 只读（见下「可选收紧」） |
| `inherit-current-permissions` 名 | 可保留兼容；**语义定为「workflow 自治 full_access」**，不再假装「继承 parent mode」；文档与 reason 字符串写清楚，避免误读 |
| 可选收紧（非默认） | 显式 `permission_profile=read_only` / `restricted_mcp` 仍可给 **特定 run** 用（脚本/测试/高危）；**默认路径不走** |
| `explicit_approval` profile | **废弃或映射为 allow**（或仅记审计事件），**禁止**再变成阻塞 UI；与拍板冲突的旧设计作废 |
| 安全叙事 | 「跑 workflow ≈ 用户已授权这段自动化全权执行」；若用户要保守，应 **别开 workflow** 或 **显式 read_only run**，而不是 child 弹 N 次 |

**为什么同意这个拍板（工程侧）**：

1. **交互模型不匹配**：主会话 ask 是「人在环、一步一确认」；workflow 是「编排后批量跑」。把 P1 审批搬进每个 child tool，等于把批处理打回交互式，**友好度归零**。  
2. **无靠谱 UI 挂点**：child 在后台线程/多 job；即便做排队审批，用户也难理解「job 3 的 file_write」。CC 对 async 默认 **deny** 而不是弹窗——说明业界也认为 **后台不宜等人**；GA 选 full_access 是另一极：**后台直接干**（信任 workflow 发起者）。  
3. **与今天行为兼容**：当前 inherit→allow 其实已是 full_access；拍板是 **承认并产品化**，去掉「以后要继承 parent / 要做 child ask」的错误预期。  
4. **风险可控点在入口**：真正该守的是 **谁能启动 workflow、脚本是否可信、可选 read_only run**，而不是 child 内部假审批。

**与 Codex/CC 的差异（有意）**：两边更偏「child ≤ parent」。GA workflow 选择 **「child = 自动化全权」**。这不是实现不了继承，而是 **产品不要继承带来的卡顿/弹窗**。主会话三档 + P1 审批 **仍然有价值**，作用域是 **人手操的主 agent**，不是 workflow worker。

**原 P0b「真继承 parent mode」**：在拍板后 **降级 / 取消为默认路径**。若仍要做安全加固，优先顺序改为：

1. **文档 + 语义澄清**（inherit = full_access，不继承 ask）— 必做、极小  
2. **可选** 显式 `read_only` run 保持可用 — 已有  
3. **可选** 启动 workflow 时若主会话是 `read_only`，**提示/二次确认**「子 agent 仍将 full_access」— 体验护栏，非门控  
4. ~~child 阻塞审批 UI~~ — **不做**  
5. ~~parent ask → child 降级 read_only~~ — **默认不做**（与 full_access 拍板冲突；仅当未来要「保守模式」再议）

---

## 1. Codex：spawn 时如何定 child 权限

### 1.1 关键代码

| 文件 | 职责 |
|------|------|
| `core/src/tools/handlers/multi_agents_common.rs` | `build_agent_spawn_config` / `apply_spawn_agent_runtime_overrides` |
| `core/src/tools/handlers/multi_agents_v2/spawn.rs` | spawn 流水线：base config → model/role → **再次** runtime overrides |
| `core/src/agent/role.rs` | `apply_role_to_config`：role 配置层叠在 session 之上 |
| `core/src/config/agent_roles.rs` | 加载 built-in / 用户 role 定义 |

### 1.2 继承模型（两层）

注释写得很直白（`multi_agents_common.rs`）：

> 从 parent 的 effective config 起步，再刷新 turn 上的 runtime 字段：**model、reasoning、approval policy、sandbox、cwd**。  
> 跳过这步会让 child 与 parent 在 **approval / cwd / sandbox** 上不一致。

**`apply_spawn_agent_runtime_overrides`** 明确拷贝：

- `config.permissions.approval_policy` ← `turn.approval_policy`
- `shell_environment_policy`
- `codex_linux_sandbox_exe`
- `cwd`
- `config.permissions.set_permission_profile(turn.permission_profile())`

### 1.3 Spawn 顺序（V2）与「谁赢」

`multi_agents_v2/spawn.rs` 实际顺序：

```text
build_agent_spawn_config(parent turn)     # 已含一次 runtime overrides
  ├─ 非 full-history fork：model 覆盖 + apply_role_to_config(role)
  └─ full-history fork：禁止 agent_type/model/effort 覆盖（整史 fork 锁死继承）
apply_spawn_agent_service_tier(...)
apply_spawn_agent_runtime_overrides(...)  # ★ 再次用 parent turn 刷审批/沙箱/profile/cwd
apply_spawn_agent_overrides(depth)        # 深度相关 feature 开关
AgentControl.spawn_agent_with_metadata(config, ...)
```

要点：

1. **Role 可以改模型、指令、技能、以及 role 文件里写的 sandbox 等配置层**。  
2. 但 **live turn 的 approval_policy + permission_profile + sandbox 句柄 + cwd 在 role 之后再次写入** → 对「用户当前这一轮选了什么审批/沙箱」，**parent turn 优先于 role**。  
3. 因此 Codex 的 subagent **默认不会比 parent 当前会话更「野」**；role 更像「在 parent 安全边界内的任务特化」，而不是独立降权/越权通道。  
4. Full-history fork 进一步禁止改 agent_type/model/effort，强调 **上下文 + 身份继承**。

### 1.4 与 UI 审批的关系

Codex 子 thread 仍挂在同一 `AgentControl` / session 服务上，审批走既有 **pending oneshot + 事件** 路径（见既有 ask 调研文档），不是「child 无条件 YOLO」。  
P0b 不需要复刻 AgentControl；只需学 **spawn 时快照 parent 策略上界**。

---

## 2. Claude Code：AgentTool / runAgent 权限

### 2.1 关键代码

| 文件 | 职责 |
|------|------|
| `tools/AgentTool/AgentTool.tsx` | spawn 参数、`workerPermissionContext`、工具池组装 |
| `tools/AgentTool/runAgent.ts` | `agentGetAppState`：mode 覆盖规则、async 禁弹窗、allowedTools 作用域 |
| `tools/AgentTool/loadAgentsDir.ts` | 自定义 agent frontmatter：`permissionMode` / `disallowedTools` |
| `tools/AgentTool/built-in/*` | Explore/Plan 等 **禁写工具表** |
| `utils/permissions/permissions.ts` | `shouldAvoidPermissionPrompts` → headless **deny** |

### 2.2 Mode 继承与覆盖（核心规则）

`runAgent.ts`：

```text
base = parent appState.toolPermissionContext

if agent.permissionMode 有定义
   AND parent.mode ∉ { bypassPermissions, acceptEdits, auto? }
then child.mode = agent.permissionMode
else child.mode = parent.mode   # parent 的「放行档」优先，不能被 agent 改掉
```

产品语义：

| Parent mode | Agent 声明 mode | 结果 |
|-------------|-----------------|------|
| `default` / `plan` 等 | `acceptEdits` / `plan` / … | **可用 agent 覆盖**（在更严/任务化方向） |
| `bypassPermissions` | 任意 | **仍 bypass**（parent 优先） |
| `acceptEdits` | 任意 | **仍 acceptEdits** |
| `auto`（feature） | 任意 | **仍 auto**（不覆盖） |

解读：**parent 已选择「少问我 / 代批」时，子 agent 不能偷偷变成更啰嗦的交互模式来绕过；更重要的是不能在 parent 收紧时由 agent 单方面改回 bypass。**  
（实现上写的是「这些 parent mode 时不应用 agent override」，等价于 **高特权 parent 档粘住**。）

### 2.3 工具池 vs 规则

`AgentTool.tsx`：

- Worker 用 **`assembleToolPool(workerPermissionContext, mcp.tools)`** 独立组装工具集，注释写明 **不受 parent 工具限制列表拖累**（避免 parent 被裁工具后 worker 缺工具）。  
- 但 `workerPermissionContext.mode` 默认 `selectedAgent.permissionMode ?? 'acceptEdits'`（组装工具时的 mode 视图）。  
- **Fork 路径**：为 prompt cache 一致性，传 **parent 精确 tools**，且 fork 的 permissionMode 常为 `bubble`。  
- **`allowedTools`**：写入 child 的 **session alwaysAllowRules**；**保留** SDK `cliArg` 级 allow，**清空** parent 的 session allow 泄漏。

### 2.4 内置 agent 的「硬收紧」

不只靠 mode，还靠 **工具黑名单**：

| Agent | 机制 |
|-------|------|
| **Explore** | `disallowedTools`：Agent / ExitPlanMode / FileEdit / FileWrite / NotebookEdit |
| **Plan** | 同样禁写类 + 系统提示强调只读规划 |
| **claudeCodeGuide** | `permissionMode: 'dontAsk'`（不弹窗：该 ask 的变 deny 语义） |

→ CC 的「只读子 agent」= **mode + 工具表 + 提示** 三层，不单靠 inherit 字符串。

### 2.5 无 UI / 后台：fail-closed

`runAgent.ts` 设 `shouldAvoidPermissionPrompts`：

| 场景 | 行为 |
|------|------|
| 显式 `canShowPermissionPrompts` | 按调用方 |
| `permissionMode === 'bubble'` | **要**弹（冒泡到 parent 终端） |
| 默认 | async → 避免弹窗；sync → 可弹 |
| async 且仍可弹 | `awaitAutomatedChecksBeforeDialog`（先自动检查再打扰用户） |

`permissions.ts`：若仍需交互且 `shouldAvoidPermissionPrompts`：

1. 先跑 PermissionRequest hooks（给 headless 一个放行/拒绝机会）  
2. 无 hook 决策 → **`behavior: 'deny'`**，reason `asyncAgent` / *Permission prompts are not available*

与 GA 主会话 P1 的 **无 emit → deny** 同构。

### 2.6 对 GA 可抄 / 不可抄

| 抄 | 不抄（P0b） |
|----|-------------|
| Parent 模式作为 **上界/快照** | 完整 `bubble` 冒泡 UI |
| async/child 无 UI → deny | 按 agent 类型的多套 PermissionRequest 页 |
| 只读子任务用 **工具黑名单或 read_only profile** | SDK cliArg / alwaysAllow 规则系统 |
| agent 可 **额外收紧**，不能无声 **变松于 parent 安全意图** | GrowthBook / classifier auto |

---

## 3. GA 现状（精确缺口）

### 3.1 链路（已有骨架）

```text
WorkflowRun.permission_profile  默认 "inherit-current-permissions"
        │
        ▼
AgentScheduler → job.metadata["permissionProfile" | "permissionPolicyVersion"]
        │
        ▼
WorkflowChildAgentRunner._build_handler
        │  ToolPermissionPolicy(profile=...)
        │  未设置 permission_mode_policy / permission_runtime
        ▼
GenericAgentHandler.dispatch
        │  若 workflow_permission_policy 非空 → 只走 workflow 策略
        │  （主会话 P1 runtime 路径被跳过）
        ▼
do_* / MCP
```

### 3.2 `inherit-current` 的假实现

`workflow_permissions.py`：

```python
if self.profile == INHERIT_CURRENT_PERMISSIONS:
    return self._decision("allow", "inherit_current", tool_name)
```

→ **名字是 inherit，语义是 allow-all**。  
测试 `test_workflow_permission_inheritance_e2e.py` 验证的是 **profile 写入 metadata / 事件 / cache 分区**，不是「parent 为 read_only 时 child 不能写」。

### 3.3 dispatch 优先级

`ga.py`：

1. **`workflow_permission_policy` 优先**；非 allow 时 workflow ask 仍 **非阻塞** `approval_required`。  
2. 否则主会话 `permission_mode_policy` + `permission_runtime`（P1）。

因此即使用户在 Ink 主会话切到 `read_only` / `ask`，**默认 workflow child 仍全开写/执行**——与 Codex「parent turn 刷审批/沙箱」和 CC「继承 toolPermissionContext」都不一致。

### 3.4 两类「子 agent」不要混谈

| 类型 | 入口 | 权限现状 |
|------|------|----------|
| **Workflow child** | `workflow_child_agent` + `ToolPermissionPolicy` | 本调研主战场；inherit 假 |
| **文件 IO subagent**（`agentmain` 后台 task 目录） | 独立进程/handler | **通常不带** workflow policy；是否继承主会话 mode 另题，P0b 可先 **不扩** |
| **主会话** | Ink / bridge | P0 三档 + P1 阻塞审批 **已交付** |

P0b 文档与实现默认只钉 **workflow child**；文件 IO subagent 若在后续要对齐，单开切片。

---

## 4. 对照表（实现语义）

| 维度 | Codex | Claude Code | GA 今天 | GA 拍板后（§0.1） |
|------|-------|-------------|---------|-------------------|
| 默认能力 | parent 上界 | parent context | 名 inherit、实 allow | **full_access（产品化 allow）** |
| 任务特化收紧 | role 层 | mode / disallowedTools | 显式 read_only 等 | **保留显式 profile**；默认不收紧 |
| 比 parent 更松 | 审批上否 | bypass 等否 | 能 | **允许**（主会话 ask 不挡 child） |
| ask / 人审 | 统一 approval | sync 可弹；async deny | child 无阻塞 UI | **永不对人审**；#3 取消 |
| 无 UI | 同 session | deny | 无 runtime | **直接执行**（full_access） |
| 持久化 | config/role | settings | 无 | 非本切片（#2） |

---

## 5. 拍板后的实现取向（取代原「真继承」P0b）

### 5.1 产品规则（现行）

1. **默认** `inherit-current-permissions` ≡ **full_access / allow-all**（与今天代码行为一致，**改文档与命名预期**，不必再「修成继承」）。  
2. **禁止** workflow child 路径接入 `permission_runtime` 阻塞审批；`dispatch` 在 workflow policy 下 **不得** wait 用户。  
3. **`explicit_approval` → ask**：若保留 profile，只允许 **非阻塞** 信号或 **直接当 allow/deny 策略**；**禁止**做成 Ink 弹窗队列（与 §0.1 冲突则删/映射 allow）。  
4. **显式** `read_only` / `restricted_mcp` 仍可用：给测试、审计、用户主动收紧的 run；**不是**默认。  
5. 主会话 `read_only` / `ask`：**不**自动改变默认 workflow child 能力；可选 UX：启动 workflow 时一行提示「子任务将以 Full Access 运行」。  
6. 安全边界放在 **启动 workflow 的人** + **可选 profile**，不放在 child 逐步审批。

### 5.2 映射表（拍板后）

| 配置 | child 行为 |
|------|------------|
| 默认 / `inherit-current-permissions` | **allow-all（full_access）** |
| 显式 `read_only` | 现有 read_only 评估 |
| 显式 `restricted_mcp` | 现有 MCP 限制 |
| 显式 `explicit_approval` | **建议映射 allow** 或移除；**不做** UI ask |
| 主会话 mode 任意 | **默认忽略**（不继承） |

### 5.3 建议工程切片（小，替代原 P0b 大改）

| 项 | 动作 | 优先级 |
|----|------|--------|
| 文档 | 总文档 §13/§14、本文 §0.1：写死 full_access、#3 非目标 | **P0** |
| 代码注释 / reason | `inherit_current` reason 改为 `workflow_full_access`（可选，避免测试碎） | 小 |
| `ga.py` | 注释标明 workflow 路径永不挂 runtime | 小 |
| Ink | workflow 面板展示 `Permission: full_access (workflow)` | 可选 |
| 启动提示 | 主会话非 full_access 时确认/提示 | 可选 |
| 单测 | 锁定「默认 inherit → file_write allow」；显式 read_only 仍 deny | 回归 |

**不再做**：parent mode 快照、`ask→read_only` 降级、child `permission_request` 协议。

### 5.4 明确不做

| 项 | 原因 |
|----|------|
| workflow child 阻塞审批 UI（原大件 #3 / P2b） | **产品拍板取消** |
| inherit 真继承 parent mode（原 P0b 主方案） | 与 full_access 拍板冲突 |
| 文件 IO subagent 强制 full_access 以外策略 | 另入口；默认可同样按「自动化全权」理解，单开若需要 |
| 档位 persist | 大件 #2，正交 |

### 5.5 与旧「阶段 B/C」关系

```text
旧阶段 B（inherit 真继承）  →  默认路径取消；仅保留「显式 read_only run」能力
旧阶段 C（child ask UI）    →  取消 / 非目标（§0.1）
现行默认                    →  workflow child = full_access，不等人
```

---

## 6. 建议验收用例（拍板后）

| # | 场景 | 期望 |
|---|------|------|
| 1 | 默认 inherit，任意主会话 mode | child `file_write` **allow**（无 permission_request） |
| 2 | 显式 `permission_profile=read_only` | child 写/执行 **deny**；读 allow |
| 3 | workflow 路径 | **不**调用 `permission_runtime.wait_for_decision` |
| 4 | 文档/UI 文案 | 不宣称「子 agent 继承 ask / 会弹审批」 |

---

## 7. 工作量与风险

| 项 | 估计 |
|----|------|
| 文档对齐 + 注释/文案 | **0.5 人日内** |
| 可选启动提示 / panel | **小** |
| ~~真继承 + 单测大改~~ | **取消** |

风险：

- **安全**：主会话 read_only 时 workflow 仍可写——须在 UI/文档 **明示**，避免用户以为全局只读。  
- **误读 inherit 名字**：长期可考虑 rename 为 `workflow-full-access`（cache key 变更，另开小 PR）。  
- 与 Codex/CC「child≤parent」不一致：接受为 **产品差异**，不是实现遗漏。

---

## 8. 源码索引（深挖）

### Codex

- `core/src/tools/handlers/multi_agents_common.rs` — runtime overrides  
- `core/src/tools/handlers/multi_agents_v2/spawn.rs` — spawn 顺序  
- `core/src/agent/role.rs` — role 层  
- `core/src/config/agent_roles.rs` — role 加载  

### Claude Code

- `tools/AgentTool/runAgent.ts` — mode 覆盖 / async deny 标志  
- `tools/AgentTool/AgentTool.tsx` — worker 工具池 / fork  
- `tools/AgentTool/loadAgentsDir.ts` — frontmatter permissionMode  
- `tools/AgentTool/built-in/exploreAgent.ts` / `planAgent.ts` — 禁写  
- `utils/permissions/permissions.ts` — `shouldAvoidPermissionPrompts`  

### GA

- `workflow_permissions.py` — inherit → allow（拍板后 = workflow full_access）  
- `workflow_child_agent.py` `_build_handler`  
- `workflow_models.py` — `DEFAULT_PERMISSION_PROFILE`  
- `ga.py` `dispatch` — workflow vs 主会话优先级  
- `docs/ga_permission_modes_research_2026-07-20.md` §13–§14  

---

## 9. 总结

1. **Codex / CC** 调研结论仍成立：业界默认 **child≤parent**，后台 **少弹窗**（CC 甚至 deny）。  
2. **GA 产品拍板（§0.1）**：workflow subagent = **full_access**，**不做人审**——体验优先，与调研默认不同，**有意为之**。  
3. 今天代码的 inherit→allow **正好贴合拍板**；要改的是 **叙事与路线图**（取消「真继承 / child ask UI」为必做），不是硬做成 parent 只读继承。  
4. 主会话三档 + P1 审批 **只管人手主 agent**；workflow 是 **已授权的批处理通道**。  
5. 可选保留 **显式 read_only run**；大件 #2 持久化仍独立；大件 #3 **取消**。

---

*本文为调研与产品拍板记录，不替代实现 PR。实现时以仓库代码与 `AGENTS.md` 安全规则为准。*
