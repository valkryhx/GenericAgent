# GenericAgent Skill 自我进化方案可行性分析

日期：2026-07-27  
范围：当前 `main` 大更新后的 GenericAgent、历史 Skill self-evolution/sidecar 方案、本地 Claude Code 源码、本地 Codex 源码。  
结论先行：**可以继续实现，但不能按“让 agent 直接自动改自己的生产 Skill”这个口径实现。可靠路线应是 evidence-driven、sidecar-first、proposal/test/review/promote 分阶段演进。**

---

## 1. 一句话结论

之前的方案方向是对的：GenericAgent 已经有 Skill discovery/loading、Memory/SOP、workflow child agent、权限策略和 transcript/artifact 基础，确实具备继续做“使用后学习”的条件。

但“Skill 自我进化”必须拆开理解：

| 层级 | 含义 | 当前判断 |
|---|---|---|
| L0：Skill 发现/加载 | 找到 `SKILL.md`，按任务加载进上下文 | 已实现，当前 GA 已具备 |
| L1：使用后反思/sidecar 提醒 | 根据真实执行证据生成本地 overlay/suggestion，不改原 Skill | **最可靠，建议优先做** |
| L2：候选 patch + 审计 + 测试 | 生成候选 diff，在隔离区测试和审计后等待批准 | 可行，但需要权限/审计/rollback 基础 |
| L3：全自动改生产 Skill | Agent 自动判断、自动写入、自动发布生产 `SKILL.md` | **不可靠，不建议做默认能力** |

所以推荐继续实现，但目标应从“自我修改 Skill tree”修正为：

> Skill 可以提出改进、记录证据、生成候选、在隔离区验证；生产 Skill 的发布必须经过确定性审计和明确审批。

---

## 2. 资料来源与参考实现

### 2.1 GenericAgent 当前实现

关键实现点：

- `skills_runtime.py:13`：`SkillSpec` 是静态 Skill 描述对象，字段包括 `name/description/source/root/path/body/when_to_use/allowed_tools/version`。
- `skills_runtime.py:29`：默认发现根包含 `~/.claude/skills`、`~/.codex/skills` 和 `GA_SKILL_PATHS`。
- `skills_runtime.py:102`：支持根本身为 `SKILL.md`、`root/SKILL.md` 和递归 `root.rglob("SKILL.md")`。
- `skills_runtime.py:141`：`discover_skills()` 做解析、去重、返回静态 skill 列表。
- `skills_runtime.py:215`：`find_skill()` 只做名称精确匹配。
- `skills_runtime.py:224`：`load_skill_content()` 加载正文并替换路径变量与 `$ARGUMENTS`。
- `skills_runtime.py:249`：`build_skill_prompt()` 把 Skill index 注入系统提示，要求任务匹配时先调用 `load_skill`。
- `ga.py:572`：`do_load_skill()` 是 GA 的 Skill tool handler。
- `ga.py:586`：当前只把 `working['active_skill']` 设为 skill 名。
- `ga.py:587`：当前只额外保存 `active_skill_allowed_tools`。
- `workflow_child_agent.py:414-418`：workflow child agent 也会追加 skill prompt。
- `workflow_store.py:198-203`：workflow progress 已统计 `skillToolCalls`、`skillLoadEvents`、`loadedSkills`，但 `missingRequiredSkills` 当前仍是空数组。
- `memory/codex_session_distill_sop.md:17-38`：已有“先生成脱敏 packet，再由 LLM 判断是否调用 update 工具写候选；不要直接 patch 正式 SOP”的经验蒸馏路线。
- `memory/memory_management_sop.md:5`：Memory SOP 的核心约束是 “No Execution, No Memory”。

### 2.2 历史方案

旧方案已经包含一些正确约束：

- `docs/superpowers/plans/2026-05-27-skill-self-evolution-adoption.md`：提出 trace/reflection/patch/audit/evolve 的 self-evolution loop，并强调不要自动修改 source skills。
- `docs/superpowers/plans/2026-05-28-skill-overlay-sidecar.md`：提出非侵入式 sidecar overlay：不直接改 Claude/Codex production `SKILL.md`，把本地提醒和 patch candidates 放进 sidecar JSON。
- `docs/superpowers/papers/skill-self-evolution-agent-readable-notes.md`：指出当前 GA 能发现和加载 Claude/Codex 风格 `SKILL.md`，但缺少“使用后学习”的闭环。

我的判断：

- 5/27 的 self-evolution adoption 方案作为长期目标可以保留，但如果实现时过早进入“改原始 SKILL.md”，风险过高。
- 5/28 的 sidecar overlay 方案更稳，应作为第一阶段实现。
- `codex_session_distill` 的 candidate → evidence → promote 思路可作为 Skill evolution 的本地参考，不应绕过它直接写生产 Skill。

### 2.3 Claude Code 源码参考

本地参考路径：`D:\git_codes\claude-reviews-claude\claude-code-fork\src`

重点源码：

- `tools/SkillTool/SkillTool.ts:291-345`：SkillTool 输入 schema 和 buildTool，模型通过工具调用 Skill。
- `tools/SkillTool/SkillTool.ts:432-578`：SkillTool 权限检查，deny rules 优先，safe-property skill 才能自动允许。
- `tools/SkillTool/SkillTool.ts:580-838`：SkillTool call 主流程，记录 usage，区分 inline/fork，处理 allowedTools/model/effort override。
- `tools/SkillTool/SkillTool.ts:871-933`：`SAFE_SKILL_PROPERTIES` 白名单，防止未来新增 frontmatter 属性默认无权限。
- `skills/loadSkillsDir.ts:270-400`：把 `SKILL.md` 转成 `PromptCommand`，Skill 是统一 command registry 的一种。
- `skills/loadSkillsDir.ts:625-804`：加载 managed/user/project/additional/legacy skills，做去重与路径条件处理。
- `utils/processUserInput/processSlashCommand.tsx:871-918`：Skill 调用时记录 invoked skill，并解析 `allowedTools` 生成 command permission attachment。
- `utils/suggestions/skillUsageTracking.ts:13-55`：记录 Skill usage count 和 last used timestamp，并用 7 天 half-life 做 recency score。
- `utils/hooks/skillImprovement.ts:57-181`：每 5 个用户消息批次分析用户纠正/偏好，生成 skill improvement suggestion。
- `utils/hooks/skillImprovement.ts:183-267`：真正读取并重写 project `SKILL.md`，且要求保持 frontmatter 原样。
- `tools/AgentTool/runAgent.ts:576-645`：subagent frontmatter `skills` 预加载，解析 skill 名称并注入 meta user message。
- `tools/AgentTool/runAgent.ts:731-805`：sidechain transcript 记录子 agent 初始消息和后续 recordable messages。
- `services/compact/compact.ts:1487-1533`：compaction 后从 `getInvokedSkillsForAgent(agentId)` 恢复 invoked skills。

Claude Code 的关键启发：**Skill 不是“模型随便读一个 markdown”，而是 command registry + permission + usage tracking + invoked skill state + compaction preservation + subagent sidechain 的组合。**

### 2.4 Codex 源码参考

本地参考路径：`D:\git_codes\codex`

重点源码：

- `codex-rs/protocol/src/protocol.rs:123-133`：`Submission { id, op, trace }`，控制面命令带 id 和 trace。
- `codex-rs/protocol/src/protocol.rs:473-642`：`Op` 枚举覆盖 interrupt、user input、settings、approval、permissions、compact、rollback、review、shutdown 等控制操作。
- `codex-rs/protocol/src/protocol.rs:746-906`：approval policy 与 sandbox policy 建模。
- `codex-rs/protocol/src/protocol.rs:1122-1328`：`Event { id, msg }` 与 `EventMsg`，记录 turn/tool/approval/hook/shutdown 等生命周期事件。
- `codex-rs/core/src/session/mod.rs:1979-2281`：exec approval / patch approval / request permissions，默认失败或取消时安全拒绝。
- `codex-rs/core/src/session/mod.rs:1609-1766`：event 既发给前端，也写入 rollout trace / rollout item。
- `codex-rs/core/src/session/mod.rs:2527-2537`：conversation item 写入内存 history、rollout response items、raw response items。
- `codex-rs/core/src/session/turn_context.rs:49-98`：per-turn snapshot 固化 sub id、trace id、config、cwd、approval、permission profile、sandbox、skills、metadata。
- `codex-rs/core/src/tools/sandboxing.rs:38-61`：ApprovalStore 按序列化 key 缓存 review decision。
- `codex-rs/core/src/tools/sandboxing.rs:157-178`：`ExecApprovalRequirement` 明确 `Skip / NeedsApproval / Forbidden`。
- `codex-rs/core/src/tools/sandboxing.rs:286-377`：`Approvable / Sandboxable / ToolRuntime` 把 approval、sandbox preference 和 tool run 分层。
- `codex-rs/tui/src/app_event_sender.rs:23-44`：typed event sender 统一发送 TUI 内部事件。
- `codex-rs/tui/src/app_command.rs:25-110`：TUI 发往 runtime/app-server 的 typed command，包括 user turn、permissions response、approval response 等。

Codex 的关键启发：**自我进化不能靠 prompt 自律，而要靠 typed control-plane、approval、sandbox、append-only rollout、rollback/flush barrier。**

补充调研 Codex memory 后，还应把 Codex 的“memory-first”设计纳入本方案：

- `D:/git_codes/codex/codex-rs/core/src/agents_md.rs:1-16`：AGENTS.md 是层级项目说明，从 project root 到当前 cwd 收集，不越过项目根。
- `D:/git_codes/codex/codex-rs/core/src/agents_md.rs:37-44`：默认项目说明文件是 `AGENTS.md`，本地覆盖是 `AGENTS.override.md`，多段内容用 `--- project-doc ---` 分隔。
- `D:/git_codes/codex/codex-rs/core/src/agents_md.rs:94-144`：把 config user instructions、AGENTS.md 内容和 hierarchical message 合并成模型可见 user instructions。
- `D:/git_codes/codex/codex-rs/core/src/agents_md.rs:166-234`：读取 AGENTS.md 时受 `project_doc_max_bytes` 限制，多文件拼接时保留 source path。
- `D:/git_codes/codex/codex-rs/config/src/types.rs:258-332`：Codex 有 `MemoriesToml/MemoriesConfig`，默认 `generate_memories=true`、`use_memories=true`，并支持因外部上下文禁用。
- `D:/git_codes/codex/codex-rs/memories/write/src/start.rs:17-33`：memory pipeline 只在非 ephemeral、MemoryTool feature、root session 下运行。
- `D:/git_codes/codex/codex-rs/memories/write/src/phase1.rs:49-107`：Phase 1 从 eligible rollouts 中抽取 `raw_memory`、`rollout_summary`、`rollout_slug`。
- `D:/git_codes/codex/codex-rs/memories/write/src/phase1.rs:393-457`：抽取前过滤 developer messages、AGENTS.md 与 skill contextual fragments，避免把框架提示循环写入 memory。
- `D:/git_codes/codex/codex-rs/memories/write/templates/memories/stage_one_system.md:47-82`：明确 high-signal memory 与 non-goals；无高信号时允许 no-op。
- `D:/git_codes/codex/codex-rs/memories/write/src/phase2.rs:113-198`：Phase 2 用 git baseline workspace 做 consolidation，生成 diff 后由 consolidation agent 更新 memory folder。
- `D:/git_codes/codex/codex-rs/memories/write/templates/memories/consolidation.md:19-35`：memory folder 包含 `memory_summary.md`、`MEMORY.md`、`raw_memories.md`、`skills/*`、`rollout_summaries/*`。
- `D:/git_codes/codex/codex-rs/memories/read/templates/memories/read_path.md:18-49`：memory read path 是 progressive disclosure：先 summary，再 `MEMORY.md`，再 rollout summaries/skills。
- `D:/git_codes/codex/codex-rs/state/src/runtime/memories.rs:94-185`：Stage 1 只选择 `memory_mode='enabled'` 且足够 idle/stale 的 threads。
- `D:/git_codes/codex/codex-rs/state/src/runtime/memories.rs:417-460`：外部上下文污染时可把 thread 标成 `polluted`，必要时触发 Phase 2 forgetting。
- `D:/git_codes/codex/codex-rs/protocol/src/memory_citation.rs:5-19`：memory citation 包含 entries 与 rollout ids，用于把回答和被引用的 memory 证据关联起来。

这说明 Codex 并不是把每条经验都变成 Skill，而是先通过 rollout → raw memory → summary/handbook → optional skills 的渐进路径降低 token 成本和污染风险。

---

## 3. 当前 GA 到底缺什么

当前 GA 已经有 Skill loading，但还不是 self-evolution system。缺口主要在下面几类。

### 3.1 缺少 Skill 调用证据链

当前 `do_load_skill()` 只保存：

- `active_skill`
- `active_skill_allowed_tools`

还缺：

- `active_skill_path`
- `active_skill_source`
- `active_skill_root`
- `active_skill_hash`
- `skill_invocation_id`
- `tool_use_id`
- `agent_id`
- `permission_decision`
- `loaded_at`
- `args`

这会导致后续无法回答：

> 是哪个 agent、在什么任务、依据什么权限、加载了哪个版本的哪个 Skill？

没有这条证据链，就不应该让系统修改 Skill。

### 3.2 缺少 required skill enforcement

`workflow_store.py` 已经有：

- `skillToolCalls`
- `skillLoadEvents`
- `loadedSkills`
- `missingRequiredSkills`

但 `missingRequiredSkills` 目前固定为空数组。这意味着系统可以“看似支持 Skill”，但不能验证某个 workflow role 是否真的加载了应加载的 Skill。

这是论文笔记里提到的 silent-bypass 风险：

> Skill 看似存在，但 agent 实际没调用，或关键脚本没被使用。

### 3.3 缺少 sidecar overlay runtime

旧 sidecar 方案的核心价值是：

- 不改原始 `SKILL.md`
- 本地记录“这次执行后发现的提醒/坑/补丁候选”
- 加载 Skill 时把 overlay appendix 注入到正文末尾
- overlay 可审计、可删除、可晋升

当前 `skills_runtime.load_skill_content()` 还没有 overlay 注入点，所以还做不到“低风险学习”。

### 3.4 缺少候选 patch 的审计与发布状态机

若要从 overlay 进入 patch，需要状态机：

```text
observed evidence
  -> reflection candidate
  -> patch candidate
  -> deterministic audit
  -> sandbox apply
  -> test run
  -> review
  -> approved promotion
  -> rollback pointer
```

当前 GA 有一些基础：

- workflow artifact
- child transcript
- permission policy
- Codex distill candidate pipeline

但没有 Skill patch candidate store、audit report、promotion/rollback 记录。

### 3.5 权限仍偏工具级，不足以表达 Skill registry mutation

当前权限能控制工具读写，但 Skill evolution 需要额外权限域：

- can propose skill changes
- can write overlay sidecar
- can create patch candidate
- can apply patch in sandbox
- can request promotion
- can promote production skill
- can rollback skill

这些不应被普通 `file_write` 或 `full_access` 隐含授予。

---

## 4. 之前方案是否可行

### 4.1 可行部分

#### A. 记录 active skill metadata

可行，而且应该立刻做。

当前 `ga.py:586-587` 只保存名称和 allowed tools。扩展为完整 metadata 很低风险：

```python
working["active_skill"] = spec.name
working["active_skill_path"] = str(spec.path)
working["active_skill_source"] = spec.source
working["active_skill_root"] = str(spec.root)
working["active_skill_hash"] = sha256(spec.body)
working["active_skill_allowed_tools"] = list(spec.allowed_tools)
```

这不会改变行为，只增加可审计上下文。

#### B. sidecar overlay

非常可行，是最可靠的第一阶段。

建议新增：

- `skill_overlay_runtime.py`
- `tests/test_skill_overlay_runtime.py`
- `GA_SKILL_OVERLAY_ROOT`
- `load_skill_content()` 的 overlay appendix 注入

overlay 应该是附加信息，不覆盖原 `SKILL.md`：

```markdown
---
# 原始 SKILL.md 内容
---

<!-- GA skill overlay: local, evidence-based, non-authoritative -->
## Local GenericAgent overlay

- 2026-07-27: 在 Windows 下运行该 skill 时，先检查 ...
```

#### C. reflection update tool

可行，但必须只写候选，不直接 patch source skill。

工具可以叫：

- `skill_reflection_update`
- `skill_evolution_candidate_update`
- `stage_skill_reflection`

输入应包含：

- `skill_name`
- `skill_path/hash`
- `evidence`
- `lesson`
- `why`
- `how_to_apply`
- `confidence`
- `source_run_id`

输出写入 sidecar/candidate store，而不是直接改 `SKILL.md`。

#### D. deterministic audit

可行，而且应先做确定性版本：

- secret/path redaction
- 禁止保存一次性路径/密钥/用户隐私
- training literal 检测
- suspicious base64/payload 检测
- silent-bypass 检测
- forbidden tool escalation 检测
- frontmatter 保护
- allowed-tools widening 检测

LLM reviewer 可以后加，但不能替代确定性检查。

### 4.2 不可靠部分

#### A. 自动改生产 `SKILL.md`

不可靠。

原因：

- 一次任务的经验可能是偶然现象。
- 模型容易把当前路径、当前用户习惯、临时错误写成长久规则。
- 修改 Skill 会影响未来所有任务，错误是持久化的。
- 如果没有 version/hash/rollback，出了问题很难定位。

#### B. 依赖模型自己判断是否应该记忆

不可靠。

Memory SOP 已经写了 “No Execution, No Memory”。Skill evolution 更应如此：没有执行证据、测试输出、用户纠正或失败复盘，就不能写入长期 Skill。

#### C. 只靠 prompt 说“不要越权”

不可靠。

Claude Code 和 Codex 都没有只靠 prompt 做边界：

- Claude Code 有 deny-first permission pipeline、SkillTool 权限检查、allowedTools scoped capability、subagent 独立 tool context。
- Codex 有 typed Op/Event、approval request、sandbox policy、permission profile、rollout。

GA 也应在 dispatch/runtime 层 enforce。

#### D. 把 Skill 加载等同于 Skill 被正确使用

不可靠。

当前 GA 只能统计 load 事件，不能证明：

- agent 按 skill 要求执行了关键步骤；
- required skill 已加载；
- skill 中要求的测试/命令/审计已运行；
- skill 产出的经验确实来自真实执行。

这正是 silent-bypass 风险。

---

## 5. 推荐的当前思路

推荐把 Skill 自我进化改名或重新定义为：

> **Evidence-driven Skill Overlay and Promotion Pipeline**
>
> 证据驱动的 Skill 覆盖层与晋升流水线。

它不是“自动进化”，而是“可审计地学习”。

### 5.1 核心原则

1. **No Execution, No Skill Update**  
   没有执行证据，不产生候选经验。

2. **No Direct Production Mutation**  
   默认不直接改原始 `SKILL.md`。

3. **Sidecar First**  
   第一阶段只写 overlay/candidate sidecar。

4. **Deny First**  
   任何权限扩大、工具扩大、路径扩大默认拒绝。

5. **Scoped Capability**  
   Skill 的 allowed tools 只在当前 skill invocation 或 forked child agent scope 内生效。

6. **Append-only Audit Trail**  
   所有建议、候选、审计、测试、审批、晋升和回滚都写事件。

7. **Human-approved Promotion**  
   生产 Skill 发布需要明确批准，不能由后台 agent 自动完成。

### 5.2 分层架构

```text
Skill discovery/loading
  skills_runtime.py
        |
        v
Skill invocation audit
  skill_invocation event + active_skill metadata
        |
        v
Skill reflection candidate
  evidence + lesson + why/how + source run
        |
        v
Sidecar overlay
  non-authoritative local appendix
        |
        v
Patch candidate
  proposed diff + audit report + test plan
        |
        v
Sandbox/worktree validation
  apply in isolated copy + run tests
        |
        v
Review/promotion
  explicit approval + version/hash + rollback
```

### 5.3 数据模型草案

#### SkillInvocation

```json
{
  "event": "skill_invocation_loaded",
  "skillName": "verification-before-completion",
  "skillPath": "C:/Users/.../SKILL.md",
  "source": "claude",
  "root": "C:/Users/.../.claude/skills",
  "hash": "sha256:...",
  "agentId": "main",
  "toolUseId": "...",
  "allowedTools": ["Bash", "Read"],
  "permissionDecision": "allowed_read_only",
  "loadedAt": "2026-07-27T..."
}
```

#### SkillReflectionCandidate

```json
{
  "id": "skref_...",
  "skillName": "...",
  "skillHash": "sha256:...",
  "sourceRunId": "...",
  "evidence": [
    {
      "type": "test_output",
      "command": "python -m unittest discover -s tests",
      "exitCode": 0,
      "digest": "sha256:..."
    }
  ],
  "lesson": "Avoid passing pages to Read for non-PDF files.",
  "why": "The Read tool rejects empty pages for text files.",
  "howToApply": "When reading text files, omit pages entirely.",
  "confidence": "high",
  "audit": {
    "secrets": "none_detected",
    "pathSpecificity": "contains_project_path",
    "requiresHumanReview": true
  },
  "status": "candidate"
}
```

#### SkillOverlay

```json
{
  "skillName": "...",
  "sourceSkillHash": "sha256:...",
  "entries": [
    {
      "candidateId": "skref_...",
      "text": "When reading non-PDF files, omit the pages parameter entirely.",
      "evidenceCount": 2,
      "status": "active_overlay"
    }
  ]
}
```

#### SkillPatchCandidate

```json
{
  "id": "skpatch_...",
  "skillName": "...",
  "baseHash": "sha256:...",
  "patchPath": "temp/skill_evolution/.../patch.diff",
  "auditPath": "temp/skill_evolution/.../audit.json",
  "testPlan": ["python -m unittest tests.test_skills_runtime"],
  "testResults": [],
  "status": "staged"
}
```

---

## 6. 参考 Claude Code 后的具体建议

### 6.1 Skill 应统一成 command registry 的一类条目

Claude Code 把 `SKILL.md` 转成 `PromptCommand`，从而让：

- 用户 slash command
- 模型 SkillTool
- hooks
- allowedTools
- usage tracking
- compaction recovery

共享同一个 registry。

GA 当前已有 slash command 与 `load_skill`，但还没统一。建议逐步收敛：

```python
@dataclass(frozen=True)
class SkillCommand:
    name: str
    description: str
    when_to_use: str
    path: Path
    root: Path
    source: str
    version: str
    allowed_tools: tuple[str, ...]
    user_invocable: bool = True
    model_invocable: bool = True
    context: Literal["inline", "fork"] = "inline"
```

### 6.2 SkillTool 调用必须写审计事件

每次 `load_skill` 应写入 transcript/workflow journal：

- requested
- allowed/denied
- loaded
- completed
- failed

这样 compaction/resume 后可以恢复 invoked skills，也能为 self-evolution 提供证据。

### 6.3 allowed_tools 只能 scoped，不能改全局权限

Claude Code 的 Skill allowedTools 是 command permission attachment。GA 可以简化为：

```python
with permission_scope(skill_invocation_id, allowed_tools=spec.allowed_tools):
    inject_skill_content(...)
```

scope 结束后权限自动失效。

### 6.4 skill improvement 要 suggestion/apply 分离

Claude Code 有 `createSkillImprovementHook()` 和 `applySkillImprovement()`。GA 应借鉴前半段，但默认不要自动执行后半段。

推荐：

- 自动生成 suggestion；
- 写入 sidecar candidate；
- UI/CLI 展示 diff；
- 用户确认后才 apply；
- apply 后记录 base hash/new hash；
- 支持 rollback。

---

## 7. 参考 Codex 后的具体建议

### 7.1 建立 typed control-plane

不要让 Skill evolution 是普通文件写操作。建议新增明确 op/event：

```text
SkillEvolutionStart
SkillReflectionCandidateCreated
SkillOverlayUpdated
SkillPatchCandidateCreated
SkillAuditStarted
SkillAuditFinished
SkillSandboxTestStarted
SkillSandboxTestFinished
SkillPromotionRequested
SkillPromotionApproved
SkillVersionPromoted
SkillRollbackRequested
SkillRollbackCompleted
SkillEvolutionAborted
```

每个事件带 id、trace、agent、skill hash、artifact digest。

### 7.2 审批默认失败安全

Codex 的 approval response 超时/取消默认 abort。GA 也应这样：

- 用户未确认：不写生产 Skill。
- 审计失败：不 promote。
- 测试失败：不 promote。
- 权限不足：不 fallback 到全局写。

### 7.3 Skill registry mutation 是独立权限域

即使用户给了 `full_access`，也不应自动允许 Skill 发布。建议把权限拆开：

- filesystem write
- command exec
- network
- MCP mutating tool
- skill overlay write
- skill source patch
- skill promotion
- skill rollback

`skill promotion` 默认需要显式确认。

### 7.4 append-only rollout 是自我进化的前提

Codex 的 rollout/event model 说明，复杂 runtime 必须能 replay。Skill evolution 也需要：

- append-only event log
- artifact hash
- patch hash
- test output digest
- approval decision
- rollback marker

否则“自我进化”会变成不可解释的自修改。

---


## 8. Codex memory 对本方案的修正

补充 Codex memory 源码后，本方案应比原先更保守：**默认先做 memory consolidation，而不是默认走 Skill evolution。**

### 8.1 Codex 的核心模式：先 memory，后 skill

Codex memory 的形态不是“一个越来越长的提示词”，而是分层资产：

```text
rollout JSONL / response items / turn context
        ↓
Phase 1 raw memories + rollout summaries
        ↓
Phase 2 consolidation workspace
        ↓
memory_summary.md + MEMORY.md + rollout_summaries/* + optional skills/*
        ↓
read path progressive disclosure + citation/usage feedback
```

这个结构对 GA 的启发很强：

- 原始事实源应是会话/工具/测试事件，而不是模型口头总结。
- 第一层输出应是 raw memory 和 rollout summary，而不是直接改 Skill。
- `memory_summary.md` 应小而常驻，只做路由。
- `MEMORY.md` 应是可检索 handbook，不应每轮全量注入。
- `skills/*` 可以是 consolidation 的候选输出，但不应自动安装到 live Skill registry。

### 8.2 Memory 可以显著削弱 Skill 自我进化的需求

Codex 的设计说明：很多长期经验不需要进入 Skill。

| 经验类型 | Codex 式位置 | GA 推荐位置 |
|---|---|---|
| 用户偏好 | memory handbook | user/project memory |
| 项目入口、测试命令、目录规则 | AGENTS.md / memory | `AGENTS.md` / L3 SOP |
| 常见失败模式 | `MEMORY.md` + rollout summary | GA session distill candidate |
| 设计决策背景 | rollout summaries | docs / memory L3 |
| 高频流程 checklist | optional skill candidate | Skill overlay / candidate |
| 必须强制执行的流程 | Skill / runtime gate | required skill enforcement |

因此，Skill 自我进化的必要性应该被收窄为：

> 只有当经验是高频、跨任务、流程性强、需要工具权限/步骤约束、且 memory/SOP 不能稳定触发时，才考虑 Skill candidate。

### 8.3 引入 GA session distill，放在 Skill evolution 之前

当前 GA 已有 Codex session distill，但它只处理 Codex 历史。更直接的下一步应是 **GA session distill**：

1. 从 `session_transcript.py` 或 workflow journal 抽取任务 packet。
2. 过滤系统提示、Skill 原文、AGENTS.md/CLAUDE.md 注入片段、外部搜索上下文。
3. 提取 raw memory：用户偏好、项目事实、失败模式、验证命令、稳定流程。
4. 生成 rollout summary，保留可回溯 session id / turn id / artifact path。
5. 写入 memory candidate queue，而不是直接写 Skill。
6. evidence count / confidence 达标后晋升到 L2/L3 SOP。
7. 只有流程性强的条目再生成 pending Skill proposal。

这条路线比“每个 Skill 运行后都尝试反思更新”更省 token，也更不容易污染。

### 8.4 AGENTS.md 层级项目说明比项目 Skill 更适合承载约定

Codex 的 `AGENTS.md` 机制有几个值得 GA 借鉴的点：

- 从项目根到 cwd 层级加载，子目录规则覆盖父目录规则。
- 有 `AGENTS.override.md` 作为本地覆盖，不要求提交私人规则。
- 有 `project_doc_max_bytes` 预算，避免项目说明无限增长。
- 输出给模型时保留 source path，便于解释规则来源。

这能替代很多“项目约定类 Skill”。GA 可以把项目运行规则、测试入口、安全边界、子目录约定放到 AGENTS/CLAUDE 项目说明层，而不是做成 Skill。

Skill 更适合：

- 可调用流程；
- 有参数；
- 有工具权限边界；
- 有必跑验证；
- 可被 workflow role 声明为 required skill。

### 8.5 Compaction 应保存 replacement history，而不只是 summary 文本

Codex compaction 的关键点是持久化 `CompactedItem.replacement_history`，恢复时从最近 checkpoint 加 suffix 重建。GA 当前 compact 更偏“把 backend history 替换成 summary pair”。

对 Skill/memory 方案来说，这意味着：

- 长任务后提炼 memory 时，不应只读压缩摘要；应优先读结构化 transcript/rollout。
- compact 输出应尽量保留 message shape 和关键 tool evidence digest。
- 如果未来做 Skill evolution，必须记录 skill invocation event，而不是指望 compact summary 提到 Skill。

### 8.6 Memory extraction 必须过滤 framework 自身

Codex Phase 1 会过滤 developer messages、AGENTS.md 片段、skill contextual fragments。这一点对 GA 很重要。

如果 GA session distill 不过滤这些内容，会出现危险循环：

```text
system/skill/memory instruction
  → 被模型当成“用户经验”
  → 写入 memory
  → 下次又作为更强上下文注入
  → 规则自我放大
```

因此 GA 的 distill packet 至少要过滤：

- 系统 prompt；
- `build_skill_prompt()` 生成的 Skill listing；
- 已加载 Skill 原文；
- CLAUDE.md / AGENTS.md 注入片段；
- MCP/工具 schema；
- 外部网页/搜索结果中没有被用户确认的内容。

### 8.7 Memory mode / polluted state 可简化借鉴

Codex 用 `memory_mode='enabled'/'disabled'/'polluted'` 控制哪些 thread 可进入 memory pipeline。GA 可以简化为：

```text
memory_eligible: true | false
memory_pollution_reasons: [external_search, untrusted_repo, secret_like_output, user_opt_out]
```

如果一次任务包含大量外部搜索、未审计远程 Skill、不可信仓库内容或敏感输出，就只允许生成 transient notes，不允许自动进入长期 memory 或 Skill candidate。

### 8.8 Memory citation 可替代“盲目信任记忆”

Codex 的 memory citation 让回答能关联到具体 memory entries / rollout ids。GA 可以用更简单的格式：

```text
memory://<memory-file>#Lx-Ly
session://<session-id>/<turn-id>
workflow://<run-id>/<artifact>
```

用途：

- 统计 memory 使用次数；
- 判断哪些 memory 长期未命中，应清理；
- 回答“这条经验从哪里来”；
- Skill candidate promotion 时要求引用至少 N 个独立来源。

### 8.9 修正后的优先级

补充 Codex memory 后，本方案优先级应调整为：

1. **GA session rollout / transcript 结构化**：统一事实源。
2. **GA session distill**：从真实任务抽取 raw memory 与 rollout summary。
3. **memory_summary + MEMORY handbook**：先做 memory-first learning。
4. **memory citation / usage tracking**：让 memory 可裁剪、可解释。
5. **Skill invocation audit**：补齐 Skill 被使用的证据链。
6. **required skill enforcement**：解决 silent-bypass。
7. **bounded Skill overlay**：只给高价值 Skill 加少量提醒。
8. **manual Skill promotion**：未来少数成熟流程再晋升。

## 9. 推荐实现路线图

### Phase 0：文档和边界确认

目标：先把“不能宣传成可靠全自动自我进化”写清楚。

产物：

- 本文档。
- README 中用“Skill discovery/loading、overlay/candidate pipeline”描述，不再宣传自动 self-evolving Skill tree。

### Phase 0.5：Memory-first learning

目标：先把 GA 自己的任务经验沉淀到 memory candidate，而不是直接进入 Skill。

改动：

- 定义 GA session packet：任务目标、用户纠正、工具调用、验证命令、失败恢复、输出 artifact。
- 过滤 framework 自身：系统提示、Skill listing、Skill 原文、AGENTS/CLAUDE 注入片段、工具 schema、未确认外部搜索内容。
- 生成 `raw_memory` 与 `rollout_summary`，写入 candidate queue。
- 增加 memory eligibility / pollution 标记：外部搜索、不可信 repo、敏感输出、用户 opt-out 时不自动晋升。
- 渲染 `memory_summary.md` / `MEMORY.md` 或复用现有 L1/L3 SOP 架构。
- 引入 memory citation：memory 文件+行号、session id、workflow artifact。

测试：

- 系统提示和 Skill 原文不会被提取成 memory。
- 同一 session 不能刷高 evidence count。
- memory candidate 可追溯到 session/workflow 证据。
- polluted session 不进入长期 memory/Skill candidate 自动晋升。

### Phase 1：Skill invocation metadata

目标：让当前 Skill loading 可审计。

改动：

- 扩展 `ga.py.do_load_skill()` working memory 字段。
- `skills_runtime.py` 增加 `skill_hash()` helper。
- `session_transcript.py` / workflow journal 增加 `skill_invocation` event。
- `workflow_store.py` 的 `skillLoadEvents` 使用真实 event，而不是只从 tool call 统计。

测试：

- `tests/test_load_skill_tool.py`
- 新增 `tests/test_skill_invocation_audit.py`

### Phase 2：Sidecar overlay runtime

目标：低风险本地学习，不改原始 `SKILL.md`。

改动：

- 新增 `skill_overlay_runtime.py`
- 支持 `GA_SKILL_OVERLAY_ROOT`
- `load_skill_content()` 在正文后追加 overlay appendix
- overlay 绑定 `skill_name + source + path/hash`
- overlay 过期策略：base hash 变更时标记 stale

测试：

- `tests/test_skill_overlay_runtime.py`
- 覆盖 stale hash、无 overlay、多个 overlay、变量替换顺序。

### Phase 3：Reflection candidate update tool

目标：从执行证据生成候选经验。

改动：

- 新增工具：`skill_reflection_update`
- 只写 candidate/overlay，不写 source `SKILL.md`
- 候选必须包含 evidence、why、how_to_apply
- 集成 deterministic audit

测试：

- evidence 缺失时报错
- secret/path redaction
- 一次性事实不写入
- suspicious payload 拒绝

### Phase 4：Required skill enforcement

目标：消除 silent-bypass。

改动：

- workflow job/agent options 支持 `required_skills`
- runner preflight：确保 `load_skill` 可用
- runtime validation：检查 transcript 中是否出现 required skill load event
- `workflow_store.py` 填充真实 `missingRequiredSkills`

测试：

- required skill 未加载时 workflow progress 标记 missing
- required skill 加载成功时不标 missing
- child tool schema 删除 `load_skill` 时 preflight fail

### Phase 5：Patch candidate + audit

目标：允许生成候选 diff，但仍不发布。

改动：

- 新增 `skill_evolution.py`
- candidate patch artifact store
- deterministic audit：frontmatter preserve、allowed-tools widening、secret scan、forbidden patterns
- 生成 review markdown

测试：

- 修改 frontmatter 被拦截
- 扩大 allowed-tools 需要更高审批
- patch candidate 只写 temp/artifact

### Phase 6：Sandbox validation + manual promote

目标：在隔离区测试候选 Skill，手动晋升。

改动：

- worktree/tempdir apply candidate
- 执行指定 test plan
- 记录 test output digest
- 用户确认后 promote
- promote 写 base/new hash 和 rollback pointer

测试：

- test fail 不 promote
- approval missing 不 promote
- rollback 恢复旧 hash
- 并发 promote 有锁或冲突检测

---

## 10. 风险清单

### 10.1 Skill 污染

错误经验进入 Skill 后，会污染未来任务。缓解：sidecar-first、evidence count、stale hash、manual promote。

### 10.2 权限扩大

Skill 自己把 allowed-tools 从 read 扩成 write/exec/network。缓解：allowed-tools widening audit + explicit approval。

### 10.3 Silent bypass

Agent 加载了 Skill 但没按关键步骤执行。缓解：required_skills + transcript validation + checklist/test gate。

### 10.4 Training literal / 过拟合常量

模型把一次性路径、用户名、密钥形态、临时错误写入 Skill。缓解：deterministic scanner + human review。

### 10.5 供应链风险

外部 Skill search 或 remote Skill 可能包含恶意指令。缓解：只读审计、hash pinning、来源记录、默认不自动启用。

### 10.6 并发写冲突

多个 agent 同时更新 overlay/candidate。缓解：append-only candidate + file lock + atomic rename。

### 10.7 审计日志泄密

Skill evolution 证据可能包含 prompt、路径、命令输出。缓解：redaction、digest、最小化保存、敏感文件 denylist。

### 10.8 Workflow runtime sandbox 不够强

当前 workflow JS runtime 主要靠 token scan 约束，不是完整 sandbox。缓解：Skill evolution 不应依赖 JS runtime 自身安全；关键文件写入和 promote 必须在 Python host side enforce。

---

## 11. 最终建议

可以继续实现，但路线应明确收敛为：

1. **先补审计，不先补自动改写。**
2. **先做 sidecar overlay，不直接改 `SKILL.md`。**
3. **先做 deterministic audit，不依赖 LLM reviewer。**
4. **先做 required skill enforcement，不把“可加载”误认为“已使用”。**
5. **先做 candidate artifact，不直接 promote。**
6. **最后才做手动批准的 production Skill promotion。**

如果按这个路线，之前方案是可行的，而且能复用当前 GA 最近大更新后的基础设施：`llm.yaml` profile、workflow child agent、permission policy、session transcript、workflow artifact、Codex distill candidate pipeline。

如果按“模型自己发现问题 → 自己改生产 Skill → 未来自动相信新 Skill”的路线，则不可靠，且会放大权限、供应链和长期记忆污染风险。

**补充 Codex memory 后，推荐下一步实施的最小闭环应先 memory-first：**

```text
GA session packet / rollout summary
  + framework/self-prompt filtering
  + memory candidate queue
  + evidence/citation/usage tracking
  + L1/L3 SOP 渲染或 memory_summary/MEMORY handbook
  + load_skill 审计事件
  + active_skill_path/source/hash
  + bounded sidecar overlay（只处理高价值 Skill）
```

这一步能提供真实“使用后学习”的能力，同时避免把低频经验直接写进 Skill、避免 overlay 无限增长，也不越过生产 Skill 自动变更的安全边界。
