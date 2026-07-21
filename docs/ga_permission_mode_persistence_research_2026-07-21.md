# GA 档位持久化（P2a）调研：Claude Code / Codex 对照

日期：2026-07-21  
范围：**主会话权限档位**写盘与启动加载；对照 CC / Codex 源码里「有没有这个概念」。  
**不含**：工具级 Always allow 规则编辑器全集、OS sandbox、workflow child 人审（已取消）。  
相关：`docs/ga_permission_modes_research_2026-07-20.md` §14 大件 #2。

> **产品结论（2026-07-21，同日追加）**：**不做 P2a 实现。**  
> 不要 persist 写回、不记住上次 UI 档位；新会话默认 **full_access** 已由代码  
> `permission_policy.DEFAULT_PERMISSION_MODE` 保证。下文保留为 CC/Codex 对照与  
> 概念说明；若将来要「可配置默认档 / 记住选择」再重开。

| 产品 | 本地路径 |
|------|----------|
| Claude Code | `D:\git_codes\claude-reviews-claude\claude-code-fork\src` |
| Codex | `D:\git_codes\codex\codex-rs` |
| GA | `D:\git_codes\GenericAgent` |

---

## 0. 什么叫「档位持久化」？（先讲清楚）

### 0.1 用大白话

GA 主会话有三档（用户在 `/permissions` 里选）：

| 档位 | 含义（粗） |
|------|------------|
| `read_only` | 只能看，不能改/跑 |
| `ask` | 要改/跑先问你（accept/deny） |
| `full_access` | 直接干 |

**现在**：你切到 `ask`，关掉 `ga` / 新开进程 → **又变回默认 `full_access`**。  
内存里改了，**磁盘上没有记住**。

**档位持久化** = 把「用户选中的默认权限档」**写到配置文件**，下次启动 **自动读回来**，不必每回手切。

```text
今天：  启动 → 永远 full_access → 用户手切 ask → 退出 → 再启动又是 full_access
P2a：   启动 → 读配置 defaultMode=ask → 直接 ask → 用户可再改 → 可选写回配置
```

### 0.2 它「不是」什么

| 概念 | 是不是档位持久化 | 说明 |
|------|------------------|------|
| 会话内 `/permissions` 切换 | ❌ | 只改当前进程内存（GA 已有） |
| 单次工具 accept/deny（P1） | ❌ | 只决定**这一次** tool call，不改默认档 |
| Always allow `Bash(git *)` 规则库 | ⚠️ 相关但更大 | 是 **规则持久化**，CC 有完整体系；P2a **可先不做** |
| 对话 transcript / session 恢复 | ❌ | 恢复的是聊天内容，不是「默认权限档」专用配置（虽可顺带带 mode） |
| Workflow child full_access 拍板 | ❌ | 与主会话默认档正交 |

### 0.3 两层「记住」（实现时常混谈）

| 层 | 记住什么 | 寿命 | 例子 |
|----|----------|------|------|
| **A. 默认档（default mode）** | 启动时用哪一档 | 跨进程、跨天 | settings 里 `defaultMode: ask` |
| **B. 会话档（session mode）** | 本次运行临时切到哪一档 | 进程内；退出丢 | Shift+Tab / `/permissions` 不写盘 |
| **C. 规则（allow/deny/ask rules）** | 某工具/路径永久策略 | 跨进程 | `allow: ["Edit"]`；比 A 细 |

**P2a 核心 = 层 A**（+ 启动时用 A 初始化 B）。  
层 C 可列为 P2a+ 或后续，不阻塞「默认档记住」。

---

## 1. 有没有这个概念？—— 结论先说

| 产品 | 有没有「默认权限档写盘」 | 叫什么 | 落盘位置（典型） |
|------|--------------------------|--------|------------------|
| **Claude Code** | **有，一等公民** | `permissions.defaultMode` | `~/.claude/settings.json`（user）/ 项目 `.claude/settings.json` 等 |
| **Codex** | **有，一等公民** | `approval_policy` + `sandbox_mode` / permission profile | `$CODEX_HOME/config.toml`（及 managed/project 层） |
| **GA 今天** | **无（仅预留）** | `set_permission_mode`；bridge `persist` **被忽略** | 无文件；默认常量 `full_access` |

所以：**我说的「档位持久化」不是 GA 自创黑话**，就是 CC 的 **defaultMode 落盘** + Codex 的 **approval/sandbox 写进 config.toml**。两边源码都有完整读写路径。

---

## 2. Claude Code：源码里的对应物

### 2.1 配置字段

`utils/settings/types.ts` — `permissions.defaultMode`：

- schema：permission mode 枚举（含 `default` / `plan` / `acceptEdits` / `dontAsk` 等；feature 下还有更多）  
- 描述：*Default permission mode when Claude Code needs access*

即：**启动/初始化时的默认权限模式**，正是「档位持久化」的层 A。

### 2.2 读：启动时用 settings 填 mode

`utils/permissions/permissionSetup.ts`：

- 读 `settings.permissions?.defaultMode`  
- 与 CLI `--permission-mode`、flag、远程限制等合并，得到本次会话初始 mode  
- `main.tsx` 注释也写明：mode 可由 **settings defaultMode 或 --permission-mode** 决定  

### 2.3 写：改设置会落盘

| 路径 | 作用 |
|------|------|
| `utils/settings/settings.ts` → `updateSettingsForSource` | 按 source（user/project/local…）写 settings 文件 |
| `utils/permissions/PermissionUpdate.ts` | `setMode` 类 update 可 `updateSettingsForSource(..., { permissions: { defaultMode } })` |
| `components/Settings/Config.tsx` | UI 改 defaultMode → 写 settings |
| `tools/ConfigTool` | agent/工具可改 `permissions.defaultMode` |
| `skills/bundled/updateConfig.ts` | 文档化可配 `defaultMode` |

### 2.4 重要区分：session vs 持久

CC 的权限 **update destination** 可以是：

- `session` — 只改当前会话（不写 defaultMode 文件，或仅内存）  
- `userSettings` / `projectSettings` / `localSettings` — **写盘**  

例如文件权限建议里常见：`setMode: acceptEdits, destination: 'session'` → **本次会话升级**，不等于永久改 defaultMode。  
用户在 Settings 里改 defaultMode → **永久**。

### 2.5 规则持久化（比档位更大，P2a 可后置）

同一 `permissions` 对象下还有 allow/deny/ask **规则表**，也经 `updateSettingsForSource` 落盘。  
这是层 C；**概念相邻但实现更重**。P2a 先只抄 **defaultMode** 即可。

---

## 3. Codex：源码里的对应物

### 3.1 配置字段（二维，不是单一 mode 名）

Codex 用户可见「Read Only / Ask / Full Access」背后大致是：

| 配置键 | 含义 |
|--------|------|
| `approval_policy` | 何时问人（如 `on-request` / `never` / `on-failure`…） |
| `sandbox_mode` 或 permission profile | 能碰什么（read-only / workspace-write / danger-full-access…） |

测试与文档中大量出现：

```toml
# $CODEX_HOME/config.toml 示例形态
approval_policy = "never"
sandbox_mode = "danger-full-access"
```

或 `on-request` + `read-only` 等组合。  
**写在 config.toml 里 = 跨启动记住** → 就是档位/策略持久化。

### 3.2 读：config 加载管线

`core/src/config/`：

- 多层：user `config.toml`、managed、project、CLI override  
- `Config` 上挂 `permissions.approval_policy`、sandbox / profile  
- 启动 thread/TUI 时用解析后的 **requirements + effective config**

### 3.3 写：ConfigEditsBuilder 持久化

`core/src/config/mod.rs` 等：

- `ConfigEditsBuilder` 修改并 **persist config.toml**  
- 错误文案可见：`failed to persist config.toml`  
- TUI `config_update.rs` 等可构造对 sandbox/approval 相关的 edits  

另外：thread 级 `thread/settings/update`、turn 覆盖可改 **当前 thread 的 next-turn 设置**（更像会话/线程级），与 **用户 home 默认 config** 是不同寿命；但 **默认策略仍来自 config.toml 持久层**。

### 3.4 和 GA 三档的对应（概念映射，非 1:1）

| GA 档 | 更接近 Codex 的组合（示意） |
|-------|---------------------------|
| `read_only` | 紧 sandbox + 常伴随需审批或限制写 |
| `ask` | `approval_policy ≈ on-request`（+ 非 full sandbox） |
| `full_access` | `approval_policy ≈ never` + 宽松 sandbox / full access |

GA **没有** OS sandbox，所以 P2a 只需持久化 **一个枚举 mode**，不必复刻 Codex 二维模型。

---

## 4. GA 今天差在哪

| 位置 | 现状 |
|------|------|
| `agentmain.set_permission_mode` | 只改 `self.permission_mode` + live handler；**无写文件** |
| `ink_bridge.permission_switch(..., persist=)` | 注释写明：`persist` **预留**；`_ = persist` **MVP 不落盘** |
| JSONL `set_permission_mode` | 协议已有可选 `persist?: boolean`（Ink `protocol.ts`） |
| 启动 | `DEFAULT_PERMISSION_MODE = full_access` 常量；**不读配置** |
| 配置文件 | **无** `permissions.json` / settings 中的 defaultMode |

→ 协议和 API **已经为持久化留了口子**，缺的是 **store + load + 真正尊重 persist**。

---

## 5. P2a 建议范围（调研结论 → 实现边界）

### 5.1 MVP（建议只做这些）

1. **存储**：用户级一份配置（推荐其一）  
   - `~/.genericagent/permissions.json` 或  
   - 与现有 GA 配置目录一致的路径（若已有 user config 根则挂靠）  
   - 最小字段：`{ "defaultMode": "full_access" | "ask" | "read_only" }`  
2. **启动加载**：`GenericAgent` 初始化时读 defaultMode → `set_permission_mode`  
3. **写入**：  
   - `set_permission_mode(mode, persist=True)` 或 bridge `persist: true` 时写盘  
   - `persist: false` / 省略 → **仅会话**（保持今天行为）  
4. **UI**：`/permissions` 切档默认 session-only；可选「记住为默认」勾选/二次确认（尤其 full_access）  
5. **单测**：写临时目录 → 新 agent 读回；persist=false 不写盘  

### 5.2 明确后置（不要塞进 P2a MVP）

| 项 | 原因 |
|----|------|
| 完整 allow/deny 规则编辑器 | CC 层 C，体量大 |
| 项目级 vs 用户级多层合并 | 可第二刀；MVP 用户级足够 |
| Always allow 单次工具永久放行 | 产品未要求；且与「仅 accept/deny」P1 简化一致 |
| 改默认常量不再 full_access | 产品已定默认 full_access；持久化的是 **用户覆盖** |
| Workflow 默认档 | 已拍板 child full_access，不跟主会话 defaultMode |

### 5.3 安全注意

- 持久化 **full_access** 应显式（用户点了记住才写），避免误写。  
- 配置文件权限：仅用户可读更佳（Unix 600 类；Windows 尽力）。  
- 损坏/未知 mode → fallback `full_access` 或 `read_only`（需产品定；建议 **fallback 默认 full_access** 与今天一致，并 log）。  

---

## 6. 源码索引

### Claude Code

- `utils/settings/types.ts` — `permissions.defaultMode` schema  
- `utils/settings/settings.ts` — `updateSettingsForSource`  
- `utils/permissions/permissionSetup.ts` — 启动解析 defaultMode  
- `utils/permissions/PermissionUpdate.ts` — setMode → 写 settings  
- `components/Settings/Config.tsx` — UI  
- `types/permissions.ts` — setMode / destination  

### Codex

- `$CODEX_HOME/config.toml` — `approval_policy` / `sandbox_mode`  
- `core/src/config/mod.rs` — Config + persist  
- `core/src/config/edit.rs` — `ConfigEditsBuilder`  
- `tui/src/config_update.rs` — TUI 侧 edits  
- `protocol` / app-server — thread 设置 vs 用户 config 分层  

### GA

- `agentmain.py` — `set_permission_mode`（内存）  
- `frontends/ink_bridge.py` — `permission_switch(..., persist=)` 忽略  
- `frontends/ink-ui/src/protocol.ts` — `persist?: boolean`  
- `permission_policy.py` — `DEFAULT_PERMISSION_MODE`  

---

## 7. 总结

1. **档位持久化** = 把主会话 **默认权限档** 写到磁盘，启动再读回来；不是单次审批，也不是 transcript。  
2. **Claude Code 有**：`permissions.defaultMode` + settings 多层写盘 + 启动加载；另有规则持久化（更大）。  
3. **Codex 有**：`config.toml` 的 `approval_policy` / `sandbox_mode`（及 profile）+ `ConfigEditsBuilder` 持久化。  
4. **GA 无实现、有预留**：`persist` 参数与协议字段已在；**按产品拍板暂不落地**。  
5. **现行行为已够用**：代码默认 `full_access`；会话内可切档；退出不记住——符合「不要写回、不要记住」的决策。

---

*本文为调研记录；P2a 实现已取消。不替代实现 PR。*
